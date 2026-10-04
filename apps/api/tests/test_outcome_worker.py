import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from trade_helper import outcome_worker
from trade_helper.api import app
from trade_helper.config import local_user_id
from trade_helper.db import connect, init_db, utc_now

pytestmark = pytest.mark.usefixtures("pg_schema")
OBSERVED = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def schema():
    init_db()


def add_report(analysis_id: str, report: dict, kind: str = "market"):
    with connect() as db:
        db.execute(
            """INSERT INTO analyses (id,user_id,idempotency_key,request_hash,request_json,status,phase,
                                     report_json,created_at,completed_at)
               VALUES (?,?,?,?,?,'completed','done',?,?,?)""",
            (analysis_id, local_user_id(), analysis_id, "hash", json.dumps({"kind": kind}),
             json.dumps(report), OBSERVED.isoformat(), OBSERVED.isoformat()))
        db.commit()


def market_report(entry):
    return {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h",
            "quote": {"observed_at": OBSERVED.isoformat(), "price": "100", "mark_price": "100"},
            "entry_decision": entry, "position_reviews": [],
            "preference_assessment": {"risk_tolerance": "high", "trading_style": "left"},
            "analysis_execution": {"model": "gpt-test", "prompt_version": "contract_strategy_v33"}}


def fake_market(path):
    """5-minute candles that follow `path(index) -> (high, low, close)`."""
    calls = []

    def fetch(market_id, timeframe, start, end):
        calls.append((start, end))
        assert timeframe == "5m" and start < end
        rows, at = [], start
        while at < end:
            index = int((at - OBSERVED) / timedelta(minutes=5))
            high, low, close = path(index)
            rows.append({"open_time": at.isoformat(), "high": str(high), "low": str(low), "close": str(close)})
            at += timedelta(minutes=5)
        return rows
    return fetch, calls


def test_finished_reports_are_enrolled_once_judged_and_summarized():
    win = {"action": "open_now", "side": "long", "entry_price": "100", "stop_loss": "98", "take_profit": "104"}
    add_report("ana_win", market_report(win))
    add_report("ana_none", market_report(None))
    fetch, calls = fake_market(lambda index: (105, 99.5, 104) if index >= 10 else (101, 99.5, 100))
    now = OBSERVED + timedelta(hours=3, minutes=2)
    outcome_worker.run_once(now=now, fetch=fetch)
    # The report with nothing to judge is marked once and never fetched for.
    assert len(calls) == 1 and calls[0] == (OBSERVED, OBSERVED + timedelta(hours=3))
    with connect() as db:
        rows = {row["analysis_id"]: row for row in db.execute("SELECT * FROM analysis_outcomes").fetchall()}
    assert rows["ana_win"]["status"] == "resolved" and rows["ana_win"]["result"] == "win"
    assert rows["ana_win"]["r_multiple"] == "2.00"
    assert rows["ana_none"]["kind"] == "none" and rows["ana_none"]["result"] == "not_applicable"
    outcome_worker.run_once(now=now + timedelta(hours=1), fetch=fetch)
    assert len(calls) == 1  # Nothing pending and nothing new to enroll.

    client = TestClient(app)
    summary = client.get("/api/v1/outcomes/summary?group=risk").json()
    assert summary["overall"]["entries"] == {"wins": 1, "losses": 0, "expired": 0, "not_triggered": 0,
                                             "unverifiable": 0, "win_rate": "100.0", "average_r": "2.00",
                                             "total_r": "2.00"}
    # The report was made with an older prompt, so the current version has no results yet.
    current = client.get("/api/v1/outcomes/summary?version=current").json()
    assert current["overall"]["total"] == 0 and current["current_versions"] == ["contract_strategy_v35"]
    assert client.get("/api/v1/outcomes?version=current").json()["total"] == 0
    assert summary["assumptions"] == {"resolution": "5m", "same_candle": "loss", "fees_and_slippage": "excluded"}
    assert summary["groups"][0]["key"] == "high"
    listed = client.get("/api/v1/outcomes").json()
    assert listed["total"] == 1 and listed["items"][0]["result"] == "win"
    assert listed["items"][0]["market_id"] == "binance:perp:BTCUSDT"
    assert client.get("/api/v1/analyses/ana_win/outcomes").json()["items"][0]["r_multiple"] == "2.00"
    assert client.get("/api/v1/outcomes/summary?group=secret").status_code == 422


def test_a_long_trade_is_fetched_a_few_days_at_a_time_until_it_settles():
    add_report("ana_long", market_report({"action": "open_now", "side": "short", "entry_price": "100",
                                          "stop_loss": "103", "take_profit": "90"}))
    fetch, calls = fake_market(lambda index: (100.5, 99, 99.5))
    now = OBSERVED + timedelta(days=10)
    while outcome_worker.run_once(now=now, fetch=fetch):
        pass
    with connect() as db:
        row = db.execute("SELECT * FROM analysis_outcomes").fetchone()
    assert row["status"] == "pending" and json.loads(row["state_json"])["through"] == now.isoformat()
    assert all(end - start <= timedelta(days=3) for start, end in calls) and len(calls) == 4


def test_market_data_that_keeps_failing_ends_the_item_as_unavailable():
    add_report("ana_gone", market_report({"action": "open_now", "side": "long", "entry_price": "100",
                                          "stop_loss": "98", "take_profit": "104"}))

    def broken(*_args):
        raise ValueError("Historical candles missing")

    now = OBSERVED + timedelta(hours=1)
    for attempt in range(outcome_worker.MAX_FAILURES):
        outcome_worker.run_once(now=now + attempt * outcome_worker.RETRY, fetch=broken)
    with connect() as db:
        assert db.execute("SELECT status FROM analysis_outcomes").fetchone()["status"] == "unavailable"
    assert utc_now()


def test_sharing_is_off_until_turned_on_and_sends_only_results_and_versions(monkeypatch):
    import httpx

    from trade_helper import outcome_upload
    from trade_helper.local_settings import patch_preferences

    add_report("ana_share", market_report({"action": "open_now", "side": "long", "entry_price": "100",
                                           "stop_loss": "98", "take_profit": "104"}))
    fetch, _ = fake_market(lambda index: (105, 99.5, 104))
    monkeypatch.setattr(outcome_upload, "session_record", lambda: {"session_token": "st_x"})
    monkeypatch.setattr(outcome_upload, "cloud_origin", lambda: "https://cloud.test")
    sent = []

    def post(url, *, json, headers, timeout):
        sent.append((url, json, headers))
        return httpx.Response(200, json={"received": len(json["records"])}, request=httpx.Request("POST", url))

    outcome_worker.run_once(now=OBSERVED + timedelta(hours=1), fetch=fetch)
    assert outcome_upload.upload_once(post=post) == 0 and sent == []  # Off by default.
    patch_preferences({"share_outcomes": True})
    assert TestClient(app).get("/api/v1/settings").json()["share_outcomes"] is True
    assert outcome_upload.upload_once(post=post) == 1
    url, body, headers = sent[0]
    assert url == "https://cloud.test/api/v1/outcomes" and headers == {"Authorization": "Bearer st_x"}
    [record] = body["records"]
    assert set(record) == {"outcome_id", "rules_version", "kind", "action", "side", "result", "r_multiple",
                           "max_up_pct", "max_down_pct", "end_pct", "risk_tolerance", "trading_style",
                           "market_id", "timeframe", "model", "provider", "prompt_version", "policy_version",
                           "app_version", "report_day", "resolved_at"}
    assert record["result"] == "win" and record["prompt_version"] == "contract_strategy_v33"
    assert "100" not in json.dumps(record)  # No entry or position prices.
    assert outcome_upload.upload_once(post=post) == 0  # Each outcome is shared once.

    deleted = []
    monkeypatch.setattr(outcome_upload.httpx, "request", lambda method, url, **kwargs: deleted.append(method)
                        or httpx.Response(200, json={"deleted": 1}, request=httpx.Request(method, url)))
    result = TestClient(app).post("/api/v1/outcomes/sharing/delete").json()
    assert result == {"enabled": False, "shared": 0, "deleted": 1} and deleted == ["DELETE"]
    assert TestClient(app).get("/api/v1/outcomes/sharing").json() == {"enabled": False, "shared": 0}


def test_the_list_can_leave_out_plans_whose_condition_cannot_be_checked():
    wait = {"action": "wait_for_entry", "side": "long", "entry_price": "99", "stop_loss": "97",
            "take_profit": "103"}  # No trigger_rule: an older plan.
    add_report("ana_old_wait", market_report(wait))
    outcome_worker.run_once(now=OBSERVED + timedelta(hours=1), fetch=fake_market(lambda index: (100, 99.5, 100))[0])
    client = TestClient(app)
    assert client.get("/api/v1/outcomes").json()["items"][0]["result"] == "unverifiable"
    assert client.get("/api/v1/outcomes?judged=1").json() == {"items": [], "total": 0}
    assert client.get("/api/v1/outcomes/summary").json()["overall"]["entries"]["unverifiable"] == 1
