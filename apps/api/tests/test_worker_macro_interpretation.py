from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event

import pytest
from fastapi.testclient import TestClient

from trade_helper import macro_interpretation as macro
from trade_helper import worker
from trade_helper.agent import agent_context, fallback_analysis, strategy_instructions
from trade_helper.api import app

from .test_analysis import sample_candles
from .test_macro_interpretation import evidence, make_macro_data

MARKET = "binance:perp:BTCUSDT"


@pytest.fixture
def macro_data(monkeypatch):
    return make_macro_data(monkeypatch)


def worker_feeds(monkeypatch):
    trade_contexts = []
    monkeypatch.setattr(worker, "ensure_macro_interpretation", macro.ensure_macro_interpretation)
    monkeypatch.setattr(worker, "fetch_candles", lambda _market, timeframe, *, limit:
                        sample_candles(recent=True, timeframe=timeframe))
    monkeypatch.setattr(worker, "fetch_quote", lambda *_: {
        "price": "120", "mark_price": "120", "observed_at": datetime.now(UTC).isoformat()})
    monkeypatch.setattr(worker, "fetch_forming_candle", lambda *_: None)
    monkeypatch.setattr(worker, "fetch_order_book", lambda *_: None)
    monkeypatch.setattr(worker, "fetch_tick_size", lambda *_: Decimal("0.1"))

    def trade_agent(request, candles, quote, context, positions, *, prepared_trace, prompt_bundle=None,
                    on_text=None):
        trade_contexts.append(agent_context(request, candles, quote, context, positions, prepared_trace))
        decision = fallback_analysis(request, candles, quote, context_candles=context, positions=positions)
        decision["mode"] = "openai_assisted"
        decision["tool_trace"] = prepared_trace
        # A second AI may disagree in its text; the report's macro section must
        # quote the shared saved AI macro result, not create another interpretation.
        decision["reasoning"]["macro_outlook"] = {
            "stance": "bullish", "reason": "This is not the saved macro interpretation", "evidence_ids": []}
        return decision

    monkeypatch.setattr(worker, "analyze_with_tools", trade_agent)
    return trade_contexts


def queue(client, identifier):
    response = client.post("/api/v1/analyses", json={"kind": "market", "market_id": MARKET, "timeframe": "1h"},
                           headers={"Idempotency-Key": identifier})
    assert response.status_code == 202
    return response.json()["id"]


def test_cold_worker_two_ensures_create_exactly_one_saved_macro_interpretation(macro_data, monkeypatch):
    contexts = worker_feeds(monkeypatch)
    with TestClient(app) as client:
        identifier = queue(client, "cold-macro-request")
        assert worker.run_once()
        job = client.get(f"/api/v1/analyses/{identifier}").json()
        assert job["status"] == "completed"
        saved = job["report"]["macro_interpretation"]
        assert saved["status"] == "succeeded" and saved["cached"]
        assert contexts[0]["saved_macro_interpretation"]["interpretation"]["id"] == saved["interpretation"]["id"]
        assert "directional_evidence" not in contexts[0]["monthly_macro_context"]
        assert job["report"]["reasoning"]["macro_outlook"]["stance"] == "neutral"
        assert job["report"]["reasoning"]["macro_outlook"]["reason"] == saved["interpretation"]["outlook"]["summary"]
        assert len(macro_data[1]) == 1
        second_id = queue(client, "second-same-macro-request")
        assert worker.run_once()
        second = client.get(f"/api/v1/analyses/{second_id}").json()
        assert second["report"]["macro_interpretation"]["interpretation"]["id"] == saved["interpretation"]["id"]
        assert len(macro_data[1]) == 1


def test_worker_reuses_event_page_result_and_later_updates_do_not_rewrite_old_report(macro_data, monkeypatch):
    snapshot, calls = macro_data
    worker_feeds(monkeypatch)
    with TestClient(app) as client:
        client.post("/api/v1/events/macro-interpretation/ensure", json={})
        page = client.get("/api/v1/events/macro-interpretation").json()
        identifier = queue(client, "page-macro-request")
        assert worker.run_once()
        before = deepcopy(client.get(f"/api/v1/analyses/{identifier}").json()["report"])
        assert before["macro_interpretation"]["interpretation"]["id"] == page["interpretation"]["id"]
        assert len(calls) == 1
        snapshot.update(evidence("3.2"))
        client.post("/api/v1/events/macro-interpretation/ensure", json={})
        assert len(calls) == 2
        assert client.get(f"/api/v1/analyses/{identifier}").json()["report"] == before


def test_event_page_post_and_worker_share_running_lease_and_one_generation(macro_data, monkeypatch):
    _snapshot, calls = macro_data
    contexts = worker_feeds(monkeypatch)
    started, release = Event(), Event()
    generate = macro._generate

    def blocked(data, **kwargs):
        started.set()
        assert release.wait(5)
        return generate(data, **kwargs)

    monkeypatch.setattr(macro, "_generate", blocked)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        identifier = queue(client, "concurrent-page-and-worker")
        page = pool.submit(client.post, "/api/v1/events/macro-interpretation/ensure", json={})
        assert started.wait(3)
        analysis = pool.submit(worker.run_once)
        assert client.get("/api/v1/events/macro-interpretation").json()["status"] == "running"
        release.set()
        assert page.result(timeout=5).status_code == 200
        assert analysis.result(timeout=5)
        report = client.get(f"/api/v1/analyses/{identifier}").json()["report"]
        assert report["macro_interpretation"]["status"] == "succeeded"
        assert len(calls) == 1 and len(contexts) == 1


def test_macro_failure_remains_visible_and_does_not_prevent_trading_agent(macro_data, monkeypatch):
    contexts = worker_feeds(monkeypatch)
    failed_calls = []

    def fail(_data, **_kwargs):
        failed_calls.append(1)
        raise TimeoutError("private model detail")

    monkeypatch.setattr(macro, "_generate", fail)
    with TestClient(app) as client:
        identifier = queue(client, "macro-unavailable-but-strategy-request")
        assert worker.run_once()
        job = client.get(f"/api/v1/analyses/{identifier}").json()
    assert job["status"] == "completed" and len(contexts) == 1
    assert job["report"]["macro_interpretation"]["status"] == "failed"
    assert job["report"]["macro_interpretation"]["interpretation"] is None
    assert job["report"]["reasoning"]["macro_outlook"] is None
    assert contexts[0]["saved_macro_interpretation"]["interpretation"] is None
    assert len(failed_calls) == 1  # The second ensure does not repeat a failed call.
    assert "private" not in str(job)


def test_strategy_prompt_uses_shared_macro_and_unavailable_is_not_neutral_fallback():
    instructions = strategy_instructions(False, prompt_locale="zh-TW")
    assert "saved_macro_interpretation" in instructions
    assert "宏觀背景" in instructions
    english = strategy_instructions(False)
    assert "macro background" in english and "macro_outlook=null" in english
    assert "macro_outlook=null" in instructions
