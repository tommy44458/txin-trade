import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from trade_helper.api import app
from trade_helper.db import connect
from trade_helper.worker import run_once

from .test_analysis import sample_candles

pytestmark = pytest.mark.usefixtures("pg_schema")


def test_analysis_idempotency_and_position_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("APP_ANALYSIS_TIMEOUT_SECONDS", "480")
    with TestClient(app) as client:
        position = client.post("/api/v1/positions", json={
            "market_id": "binance:perp:BTCUSDT", "entry_price": "100", "quantity": "2",
            "stop_loss": "90", "take_profit": "130", "side": "short", "leverage": 10, "margin_mode": "isolated",
        }).json()
        body = {"kind": "positions", "market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 10, "position_ids": [position["id"]], "directional_bias": "bullish", "risk_tolerance": "low", "trading_style": "right"}
        headers = {"Idempotency-Key": "same-request-key"}
        first = client.post("/api/v1/analyses", json=body, headers=headers)
        second = client.post("/api/v1/analyses", json=body, headers=headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert client.post("/api/v1/analyses", json=body | {"risk_tolerance": "high"}, headers=headers).status_code == 409
        assert client.post("/api/v1/analyses", json=body | {"trading_style": "left"}, headers=headers).status_code == 409
        assert client.post("/api/v1/analyses", json=body | {"trading_style": "scalp"}, headers=headers).status_code == 422
        with connect() as db:
            stored = db.execute("SELECT positions_json FROM analyses WHERE id=?", (first.json()["id"],)).fetchone()
            assert json.loads(stored["positions_json"])[0]["version"] == 1
        changed = client.patch(f"/api/v1/positions/{position['id']}", json={
            "market_id": position["market_id"], "entry_price": "110", "quantity": "2",
            "stop_loss": "90", "take_profit": "130", "side": "short", "leverage": 10, "margin_mode": "isolated", "expected_version": 1,
        })
        assert changed.status_code == 200
        assert changed.json()["version"] == 2
        monkeypatch.setattr("trade_helper.worker.fetch_candles", lambda _, timeframe, *, limit: sample_candles(recent=True, timeframe=timeframe))
        higher_cutoff = datetime.now(UTC)
        monkeypatch.setattr("trade_helper.worker.fetch_quote", lambda *_: {
            "price": "120", "mark_price": "119", "observed_at": higher_cutoff.isoformat()})
        monkeypatch.setattr("trade_helper.worker.fetch_forming_candle", lambda *_: None)
        monkeypatch.setattr("trade_helper.worker.fetch_order_book", lambda *_: None)
        monkeypatch.setattr("trade_helper.worker.fetch_tick_size", lambda *_: Decimal("0.1"))
        from trade_helper.agent import fallback_analysis

        from .test_higher_timeframes import higher_rows
        higher = {tf: {"candles": higher_rows(tf, higher_cutoff)} for tf in ("12h", "1d")}

        def higher_with_context(_market, timeframe, *, context_candles):
            assert timeframe == '1h'
            higher['4h'] = {'candles': context_candles}
            return higher

        monkeypatch.setattr("trade_helper.worker.fetch_higher_timeframe_candles", higher_with_context)

        def fake_agent(request, candles, quote, context_candles, selected_positions, *, prepared_trace, prompt_bundle,
                       on_text=None):
            assert prompt_bundle.response_locale == request['output_locale'] == 'zh-TW'
            with connect() as db:
                job = db.execute("SELECT lease_until FROM analyses WHERE status='running'").fetchone()
            lease_remaining = datetime.fromisoformat(job["lease_until"]) - datetime.now(UTC)
            assert lease_remaining.total_seconds() > 580
            # While the AI writes, the screen already has the quote, the levels and its first section.
            on_text('{"market": "價格在 120 附近，\\"多空\\"拉鋸', )
            running = client.get(f"/api/v1/analyses/{first.json()['id']}").json()
            assert running["status"] == "running" and running["report"] is None
            assert running["progress"]["market"]["price"] == "120"
            assert running["progress"]["evidence"]["trend"] in {"bullish", "bearish", "neutral", "mixed"}
            assert running["progress"]["draft"]["market"] == {"text": '價格在 120 附近，"多空"拉鋸',
                                                              "complete": False}
            decision = fallback_analysis(request, candles, quote,
                                         context_candles=context_candles,
                                         positions=selected_positions)
            assert prepared_trace[0]["tool"] == "technical_snapshot"
            assert all(frame["status"] == "available" for frame in
                       prepared_trace[0]["result"]["higher_timeframe_context"]["timeframes"].values())
            decision["tool_trace"] = prepared_trace
            decision["mode"] = "openai_assisted"
            decision["agent_stance"] = "short"
            decision["reasoning"]["agent_stance"] = "short"
            decision["reasoning"]["entry_decision"] = {"action": "stand_aside", "side": None, "entry_price": None, "stop_loss": None, "take_profit": None, "trigger": None, "invalidation": None, "reason": "目前先觀望，沒有完整價位方案。", "basis_level_ids": []}
            decision["reasoning"]["macro_outlook"] = {
                "stance": "neutral", "reason": "宏觀實際數據尚不完整，暫不選方向。",
                "evidence_ids": []}
            decisions = {item["id"]: {"decision": "close_now",
                         "reason": "現價已觸及登記條件，先核對實際持倉，若仍持有則現在平倉。"}
                         for item in selected_positions}
            decision["position_decisions"] = decisions
            decision["reasoning"]["position_decisions"] = decisions
            return decision

        monkeypatch.setattr("trade_helper.worker.analyze_with_tools", fake_agent)
        assert run_once()
        result = client.get(f"/api/v1/analyses/{first.json()['id']}").json()
        assert result["status"] == "completed"
        assert result["progress"] is None  # The finished report replaces the progress.
        assert result["report"]["position_reviews"][0]["version"] == 1
        assert result["report"]["position_reviews"][0]["leverage"] == 10
        assert result["report"]["position_reviews"][0]["unrealized_pnl_usdt"] == "-38"
        assert len(result["report"]["tool_trace"]) == 6
        assert result["report"]["technical_snapshot"]["timeframes"]["4h"]["status"] == "available"
        assert result["report"]["tool_trace"][-1]["tool"] == "evaluate_positions"
        assert result["report"]["position_reviews"][0]["advice"]["kind"] == "verify_execution"
        assert result["report"]["analysis_mode"] == "openai_assisted"
        history = client.get("/api/v1/analyses").json()
        saved = next(item for item in history if item["id"] == result["id"])
        assert saved["report"] == result["report"]
        assert saved["status"] == "completed"
        # Reconciliation is a separate record; the report itself never changes.
        assert client.get(f"/api/v1/analyses/{first.json()['id']}/outcomes").json() == {"items": []}
        assert client.get("/api/v1/outcomes/summary").json()["overall"]["total"] == 0
        assert result["report"]["position_reviews"][0]["agent_decision"]["decision"] == "close_now"
        assert result["submitted_input"]["trading_style"] == "right"
        assert result["report"]["preference_assessment"]["trading_style"] == "right"
        assert result["report"]["strategy_level_algorithm_version"] == "confirmed_pivot_lifecycle_v3"
        assert result["report"]["metrics"]["level_algorithm_version"] == "confirmed_pivot_lifecycle_v3"
        with connect() as db:
            raw = db.execute("SELECT snapshot_json FROM analyses WHERE id=?", (first.json()["id"],)).fetchone()["snapshot_json"]
        assert len(json.loads(raw)["candles"]) == 80
        assert json.loads(raw)["quote"]["higher_timeframe_candles"] == higher
        assert result["report"]["technical_snapshot"]["higher_timeframe_context"]["timeframes"]["1d"]["candle_count"] == 180
        assert json.loads(raw)["positions"][0]["id"] == position["id"]
        assert "user_id" not in json.loads(raw)["positions"][0]
        assert sha256(raw.encode()).hexdigest() == result["report"]["market_snapshot_sha256"]
        shadow = client.get(f"/api/v1/analyses/{first.json()['id']}/level-shadow")
        assert shadow.status_code == 200
        assert shadow.json()["status"] == "ready"
        assert shadow.json()["algorithm_version"] == "confirmed_pivot_lifecycle_v3"
        assert shadow.json()["used_for_strategy"] is True
        assert shadow.json()["levels"] == result["report"]["metrics"]["levels"]
        assert shadow.json()["market_snapshot_sha256"] == result["report"]["market_snapshot_sha256"]
        assert shadow.json()["price_evidence"]["closed_candle_count"] == 80
        assert shadow.json()["trade_evidence"]["status"] == "unavailable"
        assert shadow.json()["book_evidence"]["status"] == "unavailable"
        assert "shadow" not in result["report"]
        assert result["v4_status"] == "queued"
        assert client.get(f"/api/v1/analyses/{first.json()['id']}/level-shadow/v4").json()["status"] == "queued"
        from trade_helper.shadow_v4 import run_once as run_shadow

        def unavailable(*_args):
            raise ValueError("test missing minute data")

        monkeypatch.setattr("trade_helper.shadow_v4.build_v4_shadow", unavailable)
        assert run_shadow()
        failed_shadow = client.get(f"/api/v1/analyses/{first.json()['id']}").json()
        assert failed_shadow["status"] == "completed"
        assert failed_shadow["report"] == result["report"]
        assert failed_shadow["v4_status"] == "failed"
        assert failed_shadow["v4_shadow"]["status"] == "unavailable"


@pytest.mark.parametrize(("source", "expected_code"), [
    ("quote", "QUOTE_STALE"),
    ("candles", "CANDLES_STALE"),
])
def test_worker_reports_expired_market_data_with_specific_error(monkeypatch, tmp_path,
                                                                 source, expected_code):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app) as client:
        created = client.post("/api/v1/analyses", json={
            "kind": "market", "market_id": "binance:perp:BTCUSDT", "timeframe": "1h"},
            headers={"Idempotency-Key": f"stale-{source}-request"}).json()
        monkeypatch.setattr("trade_helper.worker.fetch_candles", lambda _, timeframe, *, limit: sample_candles(
            recent=source != "candles", timeframe=timeframe))
        observed = datetime.now(UTC) - timedelta(minutes=4 if source == "quote" else 0)
        monkeypatch.setattr("trade_helper.worker.fetch_quote", lambda *_: {
            "price": "120", "observed_at": observed.isoformat()})
        monkeypatch.setattr("trade_helper.worker.fetch_forming_candle", lambda *_: None)
        monkeypatch.setattr("trade_helper.worker.fetch_order_book", lambda *_: None)
        monkeypatch.setattr("trade_helper.worker.fetch_tick_size", lambda *_: Decimal("0.1"))
        assert run_once()
        result = client.get(f"/api/v1/analyses/{created['id']}").json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == expected_code
        assert "請重新分析" in result["error"]["message"]


def test_worker_retries_transient_market_failure_before_analysis(monkeypatch):
    import httpx

    from trade_helper.worker import _required_market_fetch

    monkeypatch.setattr("trade_helper.worker.time.sleep", lambda *_: None)
    attempts = 0

    def temporary_failure():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary connection failure")
        return {"price": "100"}

    assert _required_market_fetch(temporary_failure) == {"price": "100"}
    assert attempts == 3


def test_worker_reports_exchange_failure_without_generic_analysis_error(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = client.post("/api/v1/analyses", json={
            "kind": "market", "market_id": "binance:perp:BTCUSDT", "timeframe": "1h"},
            headers={"Idempotency-Key": "market-upstream-unavailable"}).json()
        request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/klines")
        response = httpx.Response(451, request=request)

        def unavailable(*_args, **_kwargs):
            raise httpx.HTTPStatusError("upstream response", request=request, response=response)

        monkeypatch.setattr("trade_helper.worker.fetch_candles", unavailable)
        assert run_once()
        result = client.get(f"/api/v1/analyses/{created['id']}").json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "MARKET_DATA_UNAVAILABLE"
        assert result["error"]["message"] == "Binance 行情暫時無法取得，請稍後重新分析。"
        assert result["report"] is None


def test_worker_reports_provider_processing_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = client.post("/api/v1/analyses", json={
            "kind": "market", "market_id": "binance:perp:BTCUSDT", "timeframe": "1h"},
            headers={"Idempotency-Key": "entry-reason-validation"}).json()
        monkeypatch.setattr("trade_helper.worker.fetch_candles", lambda _, timeframe, *, limit: sample_candles(
            recent=True, timeframe=timeframe))
        monkeypatch.setattr("trade_helper.worker.fetch_quote", lambda *_: {
            "price": "120", "mark_price": "120", "observed_at": datetime.now(UTC).isoformat()})
        monkeypatch.setattr("trade_helper.worker.fetch_forming_candle", lambda *_: None)
        monkeypatch.setattr("trade_helper.worker.fetch_order_book", lambda *_: None)
        monkeypatch.setattr("trade_helper.worker.fetch_tick_size", lambda *_: Decimal("0.1"))
        def invalid_model(*_args, **_kwargs):
            raise ValueError("Agent entry decision lacks a clear reason")
        monkeypatch.setattr("trade_helper.worker.analyze_with_tools", invalid_model)
        assert run_once()
        result = client.get(f"/api/v1/analyses/{created['id']}").json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "MODEL_CALL_FAILED"
        assert result["error"]["message"] == (
            "模型回應處理失敗：Agent entry decision lacks a clear reason")
        assert result["report"] is None


def test_worker_saves_agent_report_despite_optional_tool_and_semantic_mismatches(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setenv('APP_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    calls = []
    raw = {'market': 'MA20 為99999，依現價重新評估。', 'strategy': '考慮做空。',
           'agent_stance': 'short', 'evidence_tools': ['an_unknown_reference'],
           'entry_decision': {'action': 'open_now', 'side': 'short', 'entry_price': '120.123',
                              'stop_loss': '125.123', 'take_profit': '115.123'},
           'macro_outlook': {'stance': 'bearish', 'reason': 'Agent 的宏觀判斷。', 'evidence_ids': []}}
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(output=[SimpleNamespace(type='function_call', name='rsi',
                arguments=json.dumps({'timeframe': '12h', 'period': 999, 'reason': '檢查背景'}), call_id='unavailable')])
        assert json.loads(kwargs['input'][-1]['output'])['status'] == 'unavailable'
        return SimpleNamespace(output=[], output_text=json.dumps(raw))
    monkeypatch.setattr('trade_helper.agent.OpenAI', lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create)))
    monkeypatch.setattr('trade_helper.worker.fetch_candles', lambda _, tf, *, limit: sample_candles(recent=True, timeframe=tf))
    monkeypatch.setattr('trade_helper.worker.fetch_quote', lambda *_: {'price': '120', 'observed_at': datetime.now(UTC).isoformat()})
    monkeypatch.setattr('trade_helper.worker.fetch_forming_candle', lambda *_: None)
    monkeypatch.setattr('trade_helper.worker.fetch_order_book', lambda *_: None)
    monkeypatch.setattr('trade_helper.worker.fetch_tick_size', lambda *_: Decimal('0.1'))
    with TestClient(app) as client:
        created = client.post('/api/v1/analyses', json={'kind': 'market', 'market_id': 'binance:perp:BTCUSDT', 'timeframe': '1h'},
                              headers={'Idempotency-Key': 'nonblocking-agent-report'}).json()
        assert run_once()
        result = client.get(f"/api/v1/analyses/{created['id']}").json()
        assert result['status'] == 'completed', result.get('error')
        assert result['report']['reasoning']['market'] == raw['market']
        assert result['report']['entry_decision']['entry_price'] == '120.123'
        assert result['report']['entry_decision']['trigger'] is None
        assert result['report']['entry_risk_reference'] is not None
        assert len(calls) == 2
