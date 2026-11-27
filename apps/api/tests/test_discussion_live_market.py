"""Per-turn public market evidence never rewrites a frozen analysis or session."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from trade_helper import discussions
from trade_helper.api import app
from trade_helper.db import SCHEMA_VERSION, connect, init_db
from trade_helper.prompts import PromptBundle
from trade_helper.prompts_artifacts import load_prompt_artifact, save_prompt_artifact

from .test_discussion_streaming import event_state
from .test_discussions import analysis, macro, send

MARKET = "binance:perp:BTCUSDT"
URL = "/api/v1/discussions/analysis/analysis-one"


def _bundle(version=4, instructions="Synthetic fresh market policy"):
    return PromptBundle.build(
        task="discussion", instructions=instructions, policy_version="synthetic_policy",
        translation_version="synthetic_translation", schema_version="discussion_markdown_v1",
        prompt_version=f"professional_discussion_v{version}", prompt_locale="en-US",
        response_locale="zh-TW", template_sha256="a" * 64,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(discussions, "resolve_prompt", lambda *_args, **_kwargs: _bundle())
    with TestClient(app) as value:
        yield value


def _market(price="50100", *, status="available"):
    observed = "2026-10-02T01:00:00.000000+00:00"
    return {
        "version": "discussion_live_market_v1", "status": status,
        "market_id": MARKET, "timeframe": "1h",
        "source": "binance_usdt_perpetual",
        "requested_at": "2026-10-02T00:59:59.000000+00:00", "observed_at": observed,
        "quote": {"price": price, "observed_at": observed, "tick_size": "0.1"},
        "forming_candle": {"open": "50000", "high": "50200", "low": "49900",
                           "close": price, "is_closed": False},
        "recent_closed_candles": [],
        "comparison": {"analysis_price": "50000", "change_since_analysis_pct": str(
            (Decimal(price) - Decimal(50000)) / Decimal(50000) * 100)},
        "errors": {},
        "not_refreshed": ["support_resistance", "indicators", "higher_timeframes",
                          "positions", "macro"],
    }


def _reply(monkeypatch, seen):
    def generate(context, messages, *, timeout, instructions=None, on_text=None):
        seen.append((deepcopy(context), deepcopy(messages), timeout, instructions))
        return "Mock market explanation", "fake", "fixture-model"

    monkeypatch.setattr(discussions, "generate_reply", generate)


def _assistant(client):
    return client.get(URL).json()["messages"][-1]


def _baseline():
    with connect(readonly=True) as db:
        row = db.execute("SELECT report_json,snapshot_json,positions_json FROM analyses").fetchone()
        frozen = db.execute("SELECT context_json FROM discussion_sessions").fetchone()
    return row, frozen


def test_new_turn_gets_one_fresh_quote_and_history_keeps_its_original_timestamp(client, monkeypatch):
    analysis(kind="positions")
    seen, fetched = [], []
    prices = iter(("50100", "50200"))

    def fetch(context, *, timeout):
        assert 0 < timeout <= 8
        assert context["quote"]["price"] == "50000"
        fetched.append(deepcopy(context))
        return _market(next(prices))

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    _reply(monkeypatch, seen)
    request_id = str(uuid4())
    first = send(client, request_id=request_id)
    assert first.status_code == 202
    baseline = _baseline()
    assert first.json()["messages"][-1]["live_market"] is None
    assert not fetched
    assert discussions.run_once()
    saved = _assistant(client)
    assert saved["live_market"] == _market("50100")
    assert seen[0][0]["live_market"] == saved["live_market"]
    assert seen[0][0]["quote"]["price"] == "50000"
    assert seen[0][0]["position_snapshot"]
    assert _baseline() == baseline
    # Page reads, SSE reads, duplicate submits, and empty worker polls cannot
    # cause a second network request or regenerate an answer.
    assert send(client, request_id=request_id).status_code == 202
    assert client.get(URL).status_code == 200
    assert event_state(client.get(URL + "/stream").text)["messages"][-1]["live_market"] == saved["live_market"]
    assert not discussions.run_once()
    assert len(fetched) == 1
    send(client, message="Does the newer price change the view?")
    assert discussions.run_once()
    assert len(fetched) == 2
    assert seen[-1][0]["live_market"]["quote"]["price"] == "50200"
    previous = seen[-1][1][1]
    assert previous["role"] == "assistant" and previous["live_market"] == _market("50100")
    assert _assistant(client)["live_market"] == _market("50200")
    assert _baseline() == baseline


def test_reader_and_model_run_outside_sql_transaction_and_evidence_is_saved_before_stream(
    client, monkeypatch,
):
    analysis()
    send(client)
    evidence = _market()

    def fetch(_context, *, timeout):
        with connect() as db:
            db.execute("UPDATE analyses SET request_hash=request_hash")
        assert 0 < timeout <= 8
        return evidence

    def generate(context, _messages, *, on_text, **_kwargs):
        with connect() as db:
            row = db.execute("SELECT live_market_json FROM discussion_messages WHERE role='assistant'").fetchone()
            assert json.loads(row["live_market_json"]) == evidence
        on_text("First visible sentence")
        state = client.get(URL).json()
        assert state["busy"] and state["messages"][-1]["status"] == "running"
        assert state["messages"][-1]["live_market"] == evidence
        assert state["messages"][-1]["content"] == "First visible sentence"
        assert discussions._stream_state("analysis", "analysis-one", "local-demo")["messages"][-1]["live_market"] == evidence
        return "First visible sentence. Final answer", "fake", "fixture-model"

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    monkeypatch.setattr(discussions, "generate_reply", generate)
    assert discussions.run_once()
    assert _assistant(client)["status"] == "completed"


def test_retry_clears_old_price_and_gets_fresh_evidence_same_message_id(client, monkeypatch):
    analysis()
    queued = send(client).json()["messages"][-1]
    prices = iter(("50100", "50200"))
    fetched = []

    def fetch(_context, *, timeout):
        price = next(prices)
        fetched.append(price)
        return _market(price)

    def fail(_context, _messages, *, on_text, **_kwargs):
        on_text("Unfinished answer")
        raise TimeoutError("provider-secret")

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    monkeypatch.setattr(discussions, "generate_reply", fail)
    assert discussions.run_once()
    assert _assistant(client)["live_market"] == _market("50100")
    path = URL + f"/messages/{queued['id']}/retry"
    retry = client.post(path).json()["messages"][-1]
    assert retry["id"] == queued["id"] and retry["content"] == ""
    assert retry["live_market"] is None
    assert client.post(path).json()["messages"][-1]["live_market"] is None
    assert fetched == ["50100"]
    seen = []
    _reply(monkeypatch, seen)
    assert discussions.run_once()
    assert fetched == ["50100", "50200"]
    assert _assistant(client)["live_market"] == _market("50200")
    assert seen[0][1] == [{"role": "user", "content": "這次支撐為什麼重要？", "sequence": 1}]


@pytest.mark.parametrize("result", [_market(), None])
def test_lost_claim_during_fetch_never_calls_model_or_overwrites_retry(client, monkeypatch, result):
    analysis()
    queued = send(client).json()["messages"][-1]
    replacement = []

    def fetch(_context, *, timeout):
        with connect() as db:
            db.execute("UPDATE discussion_messages SET lease_until=? WHERE id=?",
                       ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), queued["id"]))
        assert client.post(URL + f"/messages/{queued['id']}/retry").status_code == 202
        new = discussions.claim_next(timeout=240)
        assert discussions._persist_live_market(new, _market("50300"))
        replacement.append(new)
        return result

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    monkeypatch.setattr(discussions, "generate_reply", lambda *_args, **_kwargs: pytest.fail("stale model call"))
    assert discussions.run_once()
    assert _assistant(client)["status"] == "running"
    assert _assistant(client)["live_market"] == _market("50300")
    assert discussions._finish(replacement[0], content="Replacement reply")


def test_expired_claim_cannot_save_fresh_data_or_start_model(client, monkeypatch):
    analysis()
    queued = send(client).json()["messages"][-1]

    def fetch(_context, *, timeout):
        with connect() as db:
            db.execute("UPDATE discussion_messages SET lease_until=? WHERE id=?",
                       ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), queued["id"]))
        return _market()

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    monkeypatch.setattr(discussions, "generate_reply", lambda *_args, **_kwargs: pytest.fail("expired model call"))
    assert discussions.run_once()
    assert _assistant(client)["live_market"] is None


def test_unexpected_reader_failure_is_safe_unavailable_and_answer_can_continue(client, monkeypatch):
    analysis()
    send(client)

    def fetch(*_args, **_kwargs):
        raise RuntimeError("secret-api-key raw-upstream-payload")

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    seen = []
    _reply(monkeypatch, seen)
    assert discussions.run_once()
    message = _assistant(client)
    assert message["status"] == "completed"
    assert message["live_market"]["status"] == "unavailable"
    assert message["live_market"]["quote"] is None
    assert message["live_market"]["errors"] == {"market": "LIVE_MARKET_UNAVAILABLE"}
    assert "secret" not in json.dumps(message)
    assert seen[0][0]["live_market"] == message["live_market"]


def test_partial_reader_evidence_is_saved_as_partial_without_inventing_candles(client, monkeypatch):
    analysis()
    send(client)
    partial = _market(status="partial") | {"forming_candle": None, "errors": {"candles": "MARKET_UNAVAILABLE"}}
    monkeypatch.setattr(discussions, "fetch_discussion_market", lambda *_args, **_kwargs: partial)
    seen = []
    _reply(monkeypatch, seen)
    assert discussions.run_once()
    assert _assistant(client)["live_market"] == partial
    assert seen[0][0]["live_market"] == partial


def test_macro_never_refreshes_public_market_and_keeps_live_market_null(client, monkeypatch):
    macro()
    send(client, "macro-one", "What changed?", subject_type="macro")
    monkeypatch.setattr(discussions, "fetch_discussion_market", lambda *_args, **_kwargs: pytest.fail("macro fetch"))
    seen = []
    _reply(monkeypatch, seen)
    assert discussions.run_once()
    assert "live_market" not in seen[0][0]
    assert client.get("/api/v1/discussions/macro/macro-one").json()["messages"][-1]["live_market"] is None


@pytest.mark.parametrize("legacy_version", [1, 2, 3, None])
def test_old_queued_prompt_is_not_rewritten_or_given_unexpected_fresh_market(
    client, monkeypatch, legacy_version,
):
    analysis()
    queued = send(client).json()["messages"][-1]
    with connect() as db:
        legacy = _bundle(legacy_version, "Immutable legacy instructions") if legacy_version else None
        identifier = save_prompt_artifact(db, legacy) if legacy else None
        db.execute("UPDATE discussion_messages SET prompt_artifact_id=? WHERE id=?", (identifier, queued["id"]))
    monkeypatch.setattr(discussions, "fetch_discussion_market", lambda *_args, **_kwargs: pytest.fail("legacy fetch"))
    seen = []
    _reply(monkeypatch, seen)
    assert discussions.run_once()
    assert "live_market" not in seen[0][0]
    assert _assistant(client)["live_market"] is None
    if legacy:
        assert seen[0][3] == legacy.instructions
        with connect(readonly=True) as db:
            assert load_prompt_artifact(db, identifier).to_dict() == legacy.to_dict()


def test_fetch_budget_is_part_of_total_reply_deadline(client, monkeypatch):
    analysis()
    send(client)
    clock = {"now": 100.0}
    monkeypatch.setattr(discussions, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(discussions, "analysis_timeout_seconds", lambda: 10)
    fetched = []

    def fetch(_context, *, timeout):
        fetched.append(timeout)
        clock["now"] += 3.5
        return _market()

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    seen = []
    _reply(monkeypatch, seen)
    assert discussions.run_once()
    assert fetched == [8]
    assert seen[0][2] == 6.5


def test_context_setup_consumes_the_same_fetch_and_model_budget(client, monkeypatch):
    analysis()
    send(client)
    clock = {"now": 100.0}
    monkeypatch.setattr(discussions, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(discussions, "analysis_timeout_seconds", lambda: 10)
    original_input = discussions._model_input

    def model_input(job):
        context = original_input(job)
        clock["now"] += 4
        return context

    def fetch(_context, *, timeout):
        assert timeout == 6
        clock["now"] += 2
        return _market()

    monkeypatch.setattr(discussions, "_model_input", model_input)
    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    seen = []
    _reply(monkeypatch, seen)
    assert discussions.run_once()
    assert seen[0][2] == 4


def test_exhausted_fetch_budget_does_not_start_a_model_call(client, monkeypatch):
    analysis()
    send(client)
    clock = {"now": 100.0}
    monkeypatch.setattr(discussions, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(discussions, "analysis_timeout_seconds", lambda: 2)

    def fetch(_context, *, timeout):
        assert timeout == 2
        clock["now"] += 2
        return _market(status="partial")

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    monkeypatch.setattr(discussions, "generate_reply", lambda *_args, **_kwargs: pytest.fail("out of time"))
    assert discussions.run_once()
    saved = _assistant(client)
    assert saved["status"] == "failed" and saved["error"]["code"] == "DISCUSSION_TIMEOUT"
    assert saved["live_market"]["status"] == "partial"


def test_schema_seven_upgrade_preserves_messages_and_adds_null_evidence(client):
    analysis()
    send(client)
    with connect() as db:
        db.execute("ALTER TABLE discussion_messages DROP COLUMN live_market_json")
        db.execute("DELETE FROM schema_migrations WHERE version>=8")
        db.execute("PRAGMA user_version=7")
        old = db.execute("SELECT * FROM discussion_messages ORDER BY sequence").fetchall()
        session = db.execute("SELECT * FROM discussion_sessions").fetchone()
        analysis_row = db.execute("SELECT * FROM analyses").fetchone()
    init_db()
    init_db()
    with connect(readonly=True) as db:
        current = db.execute("SELECT * FROM discussion_messages ORDER BY sequence").fetchall()
        assert [{k: v for k, v in row.items() if k != "live_market_json"} for row in current] == old
        assert all(row["live_market_json"] is None for row in current)
        assert db.execute("SELECT * FROM discussion_sessions").fetchone() == session
        assert db.execute("SELECT * FROM analyses").fetchone() == analysis_row
        assert db.execute("PRAGMA user_version").fetchone()["user_version"] == SCHEMA_VERSION


@pytest.mark.parametrize("version,snapshot,budget", [(8, False, 8), (9, True, 25)])
def test_newer_prompts_ask_for_a_fresh_snapshot_with_a_longer_budget(
        client, monkeypatch, version, snapshot, budget):
    monkeypatch.setattr(discussions, "resolve_prompt", lambda *_args, **_kwargs: _bundle(version))
    analysis()
    calls = []

    def fetch(context, *, timeout, **options):
        calls.append((timeout, options))
        market = _market()
        if options.get("snapshot"):
            market |= {"version": "discussion_live_market_v2",
                       "snapshot": {"version": "discussion_market_snapshot_v1", "status": "available"}}
        return market

    monkeypatch.setattr(discussions, "fetch_discussion_market", fetch)
    seen = []
    _reply(monkeypatch, seen)
    send(client)
    assert discussions.run_once()
    (timeout, options), = calls
    assert 0 < timeout <= budget
    assert options == ({"snapshot": True} if snapshot else {})
    saved = _assistant(client)["live_market"]
    assert ("snapshot" in saved) is snapshot
    assert seen[0][0]["live_market"] == saved


def test_reader_failure_under_a_newer_prompt_saves_an_unavailable_snapshot(client, monkeypatch):
    monkeypatch.setattr(discussions, "resolve_prompt", lambda *_args, **_kwargs: _bundle(9))
    analysis()

    def broken(*_args, **_kwargs):
        raise RuntimeError("private upstream detail")

    monkeypatch.setattr(discussions, "fetch_discussion_market", broken)
    _reply(monkeypatch, [])
    send(client)
    assert discussions.run_once()
    saved = _assistant(client)["live_market"]
    assert saved["version"] == "discussion_live_market_v2" and saved["status"] == "unavailable"
    assert saved["snapshot"]["status"] == "unavailable"
    assert "private" not in json.dumps(saved)
