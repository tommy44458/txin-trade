import json

import pytest

from trade_helper import cloud_commands
from trade_helper.cloud_commands import CommandFailed, run_command
from trade_helper.config import local_user_id
from trade_helper.db import connect, init_db

REPORT = {
    "market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "generated_at": "2026-10-02T00:00:00+00:00",
    "agent_stance": "wait", "entry_decision": {"action": "wait_for_entry", "side": "long"},
    "quote": {"price": "100", "observed_at": "2026-10-02T00:00:00+00:00", "higher_timeframe_candles": ["x"] * 50},
    "reasoning": {"market": "Range.", "strategy": "Wait.", "direction_assessment": {
        "long": {"verdict": "conditional", "reason": "Needs a close above 101."}}},
    "tool_trace": [{"tool": "technical_snapshot", "result": {"large": "x" * 5000}}],
}


def save(analysis_id: str, report: dict | None = REPORT, status: str = "completed"):
    with connect() as db:
        db.execute(
            "INSERT INTO analyses(id,user_id,idempotency_key,request_hash,request_json,positions_json,status,phase,"
            "report_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (analysis_id, local_user_id(), analysis_id, "h",
             json.dumps({"kind": "market", "market_id": "binance:perp:BTCUSDT", "timeframe": "1h"}), "[]",
             status, "done", json.dumps(report) if report else None,
             "2026-10-02T00:00:00+00:00", "2026-10-02T00:01:00+00:00"))


@pytest.fixture(autouse=True)
def database():
    init_db()


def test_status_and_lists_are_compact_projections():
    save("ana_one")
    assert run_command({"operation": "status.read"}, "cloud:1")["app"] == "txinTrade"
    listed = run_command({"operation": "analyses.list", "limit": 5}, "cloud:2")["analyses"]
    assert listed == [{"id": "ana_one", "status": "completed", "kind": "market",
                       "market_id": "binance:perp:BTCUSDT", "timeframe": "1h",
                       "created_at": listed[0]["created_at"], "completed_at": listed[0]["completed_at"],
                       "agent_stance": "wait", "entry_action": "wait_for_entry"}]
    assert run_command({"operation": "positions.list"}, "cloud:3") == {"positions": []}


def test_a_report_is_trimmed_to_what_a_phone_needs():
    save("ana_detail")
    detail = run_command({"operation": "analyses.get", "analysis_id": "ana_detail"}, "cloud:4")["analysis"]
    assert detail["report"]["reasoning"]["direction_assessment"]["long"]["verdict"] == "conditional"
    assert detail["report"]["quote"] == {"price": "100", "observed_at": "2026-10-02T00:00:00+00:00"}
    serialized = json.dumps(detail)
    assert "tool_trace" not in serialized and "higher_timeframe_candles" not in serialized


def test_missing_records_unknown_operations_and_oversized_results_fail_with_relay_codes(monkeypatch):
    with pytest.raises(CommandFailed) as missing:
        run_command({"operation": "analyses.get", "analysis_id": "ana_missing"}, "cloud:5")
    assert missing.value.code == "local_unavailable"
    for command in ({"operation": "files.read"}, {"operation": "analyses.list", "limit": 500}, {}):
        with pytest.raises(CommandFailed) as rejected:
            run_command(command, "cloud:6")
        assert rejected.value.code == "unsupported_operation"
    monkeypatch.setattr(cloud_commands, "MAX_RESULT_BYTES", 10)
    with pytest.raises(CommandFailed) as large:
        run_command({"operation": "status.read"}, "cloud:7")
    assert large.value.code == "local_error"


def test_remote_analysis_uses_saved_preferences_and_never_duplicates_a_retried_dispatch(monkeypatch):
    monkeypatch.setattr(cloud_commands, "trading_preferences", lambda: {
        "risk_tolerance": "high", "trading_style": "right", "trading_strategy": "extreme_reversal",
        "leverage": 10, "directional_bias": None})
    command = {"operation": "analyses.start", "market_id": "binance:perp:BTCUSDT", "timeframe": "4h",
               "output_locale": "en-US"}
    first = run_command(command, "cloud:00000000-0000-4000-8000-000000000001")
    again = run_command(command, "cloud:00000000-0000-4000-8000-000000000001")
    assert first["analysis_id"] == again["analysis_id"] and first["status"] == "queued"
    with connect(readonly=True) as db:
        rows = db.execute("SELECT request_json FROM analyses").fetchall()
    assert len(rows) == 1
    request = json.loads(rows[0]["request_json"])
    assert (request["kind"], request["timeframe"], request["output_locale"]) == ("market", "4h", "en-US")
    assert (request["risk_tolerance"], request["leverage"]) == ("high", 10)
    # The strategy decides the entry style; the old saved style is not applied.
    assert (request["trading_strategy"], request["trading_style"]) == ("extreme_reversal", "left")


def test_remote_analysis_ignores_a_saved_entry_style_without_a_strategy(monkeypatch):
    monkeypatch.setattr(cloud_commands, "trading_preferences", lambda: {
        "risk_tolerance": None, "trading_style": "right", "trading_strategy": None, "leverage": 5,
        "directional_bias": None})
    run_command({"operation": "analyses.start", "market_id": "binance:perp:BTCUSDT", "timeframe": "1h",
                 "output_locale": "zh-TW"}, "cloud:00000000-0000-4000-8000-000000000002")
    with connect(readonly=True) as db:
        request = json.loads(db.execute("SELECT request_json FROM analyses").fetchone()["request_json"])
    assert request["trading_style"] is None and request["trading_strategy"] is None
