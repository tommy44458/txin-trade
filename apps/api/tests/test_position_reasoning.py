import json
from types import SimpleNamespace

import pytest

from trade_helper.agent import _model_tool_result, agent_context, strategy_instructions
from trade_helper.position_reasoning import validate_position_reason
from trade_helper.technical_snapshot import prepare_analysis_evidence

from .test_technical_snapshot import final_response, fixture

LEVELS = [
    {"id": "support-a", "kind": "support", "low": "96", "high": "98", "zone_state": "active"},
    {"id": "resistance-a", "kind": "resistance", "low": "102", "high": "104", "zone_state": "active"},
]


def test_position_reason_must_name_verified_zone_prices():
    validate_position_reason("現價從支撐 96–98 區間回升，空單方向需重新評估。", LEVELS)
    with pytest.raises(ValueError, match="verified price zone"):
        validate_position_reason("價格正自附近支撐反彈，現在平倉。", LEVELS)
    with pytest.raises(ValueError, match="verified price zone"):
        validate_position_reason("附近支撐 95–98 反彈，現在平倉。", LEVELS)
    with pytest.raises(ValueError, match="verified price zone"):
        validate_position_reason("壓力 96–98 附近轉弱。", LEVELS)


def test_prices_adjacent_to_chinese_text_and_repeated_verified_boundary():
    validate_position_reason("1H壓力102–104尚未收復，續抱空單。若站穩壓力上界104，改評方向。",
                             LEVELS, primary_timeframe="1h")
    with pytest.raises(ValueError, match="verified price zone"):
        validate_position_reason("壓力上界104尚未收復。", LEVELS)
    with pytest.raises(ValueError, match="verified price zone"):
        validate_position_reason("壓力102–104尚未收復。若站穩壓力上界105，改評方向。", LEVELS)


@pytest.mark.parametrize("primary,secondary", [("1h", "4h"), ("4h", "1h"),
                                               ("4h", "12h"), ("12h", "1d"), ("1d", "3d"),
                                               ("1d", "1w"), ("1d", "1M")])
def test_position_reason_verifies_explicit_secondary_timeframe(primary, secondary):
    context = {"timeframe": secondary, "data": {"levels": [
        {"kind": "resistance", "low": "119.6900", "high": "121.0700", "zone_state": "active"}]}}
    validate_position_reason(f"現價在{secondary.upper()}壓力119.6900–121.0700內。"
                             f"若收復{primary.upper()}壓力102–104，改評續抱。",
                             LEVELS, primary_timeframe=primary, secondary_context=context)
    for invalid in (f"{primary.upper()}壓力119.6900–121.0700內，續抱。",
                    "壓力119.6900–121.0700內，續抱。",
                    f"{secondary.upper()}壓力102–104內，續抱。",
                    f"{secondary.upper()}支撐119.6900–121.0700內，續抱。",
                    f"{secondary.upper()}壓力119.6800–121.0700內，續抱。"):
        with pytest.raises(ValueError, match="verified price zone"):
            validate_position_reason(invalid, LEVELS, primary_timeframe=primary, secondary_context=context)
    context["data"]["levels"][0]["zone_state"] = "invalidated"
    with pytest.raises(ValueError, match="verified price zone"):
        validate_position_reason(f"{secondary.upper()}壓力119.6900–121.0700內，續抱。",
                                 LEVELS, primary_timeframe=primary, secondary_context=context)


def test_prices_for_another_kind_do_not_validate_an_unpriced_zone():
    with pytest.raises(ValueError, match="verified price zone"):
        validate_position_reason("支撐96–98反彈，但上方壓力可能阻擋。", LEVELS)


def test_secondary_zone_position_reason_passes_model_and_saved_report_checks(monkeypatch):
    from trade_helper.agent import analyze_with_tools
    from trade_helper.analysis import build_report
    from trade_helper.report_contract import validate_report

    request, candles, context, quote = fixture()
    request.update(kind="positions", risk_tolerance="high", trading_style="left")
    positions = [{"id": "p1", "version": 1, "market_id": request["market_id"],
                  "side": "short", "leverage": 20, "margin_mode": "isolated",
                  "entry_price": "180", "quantity": "1", "entry_time": None,
                  "stop_loss": None, "take_profit": None}]
    trace = prepare_analysis_evidence(request, candles, quote, context, positions)
    levels = next(x["result"] for x in trace if x["tool"] == "support_resistance")
    zone = next(z for z in levels["secondary_timeframe_context"]["data"]["levels"]
                if z["kind"] == "resistance")
    payload = json.loads(final_response(trace))
    payload["evidence_tools"].append("evaluate_positions")
    payload["position_decisions"] = {"p1": {"decision": "hold",
        "reason": f"建議續抱空單。4H壓力{zone['low']}–{zone['high']}提供重新檢查的價格區間。"}}
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        output=[], output_text=json.dumps(payload, ensure_ascii=False))))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr("trade_helper.agent.OpenAI", lambda **kwargs: client)
    decision = analyze_with_tools(request, candles, quote, context, positions, prepared_trace=trace)
    report = build_report(request, candles, quote, positions, decision, context)
    validate_report(report)
    # A hold without an exit plan is kept; its plan is reported as absent.
    assert report["position_reviews"][0]["agent_decision"] == payload["position_decisions"]["p1"] | {"exit_plan": None}


def test_position_reason_excludes_missing_protection_and_venue_caveats():
    for reason in ("沒有止損所以平倉。", "BingX 和 Binance 價差較大。", "無保護續抱風險大。"):
        with pytest.raises(ValueError, match="excluded risk or venue"):
            validate_position_reason(reason, LEVELS)


def test_agent_receives_risk_preference_without_optional_protection_fields():
    request = {"market_id": "binance:perp:SOLUSDT", "timeframe": "1h",
               "risk_tolerance": "high"}
    candles = [{"close": "100", "close_time": "2026-09-28T17:59:59.999000+00:00"}]
    quote = {"price": "101", "observed_at": "2026-09-28T18:00:00+00:00"}
    position = {"id": "p1", "version": 1, "market_id": request["market_id"],
                "side": "short", "leverage": 20, "margin_mode": "isolated",
                "entry_price": "102", "quantity": "1", "entry_time": None,
                "stop_loss": None, "take_profit": None, "source": "bingx",
                "contract_type": "standard"}
    context = agent_context(request, candles, quote, None, [position])
    selected = context["selected_positions"][0]
    assert context["risk_tolerance"] == "high"
    assert selected["side"] == "short"
    assert "stop_loss" not in selected
    assert "take_profit" not in selected
    assert "source" not in selected
    assert "contract_type" not in selected
    prompt = strategy_instructions(True, prompt_locale="zh-TW")
    assert "不能只因槓桿高" in prompt
    assert "區間低價與高價" in prompt


def test_position_tool_keeps_protection_diagnostics_out_of_model_context():
    execution = {"tool": "evaluate_positions", "result": {
        "version": "position_advice_v1", "quote_time": "2026-09-28T18:00:00+00:00",
        "valuation_price": "101", "market_state": "bearish", "positions": [{
            "position_id": "p1", "version": 1, "default_action_id": "p1:review_protection",
            "candidates": [{"reason": "沒有止損與止盈", "current_stop": None}],
        }],
    }}
    visible = _model_tool_result(execution)
    assert visible["market_state"] == "bearish"
    assert visible["positions"] == [{"position_id": "p1", "version": 1}]
    assert "止損" not in str(visible)
    assert "candidates" in execution["result"]["positions"][0]
