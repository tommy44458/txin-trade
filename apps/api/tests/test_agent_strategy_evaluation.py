import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from evaluate_agent_strategy import evaluate_case, review_flags, synthetic_cases

from trade_helper.agent import _model_tool_result, agent_context
from trade_helper.technical_snapshot import prepare_analysis_evidence

from .test_technical_snapshot import fixture


@pytest.mark.parametrize("observation", [0, 1])
def test_september_30_2116_retains_original_resistance_and_unconfirmed_crossing(observation):
    case = json.loads((Path(__file__).parent / "fixtures" / "btc_20260930_2116.json").read_text())
    quote = case["observations"][observation]["quote"]
    candles, context = case["candles"], case["context_candles"]
    request = case["observations"][observation]["request"]
    trace = prepare_analysis_evidence(request, candles, quote, context)
    model = agent_context(request, candles, quote, context, prepared_trace=trace)
    evidence = model["precomputed_evidence"]
    primary = evidence["support_resistance"]
    secondary = primary["secondary_timeframe_context"]["data"]
    # A live price above a zone must not hide the original resistance or turn
    # its historical role into confirmed support. This caused the old report.
    for data, identifier, lower, upper, last_relation in (
            (primary, "zone_840d2803f17398da", "85011.30", "85307.70", "inside"),
            (secondary, "zone_c677837b62039e17", "84743.30", "85382.30", "below")):
        zone = next(z for z in data["levels"] if z["id"] == identifier)
        assert zone["kind"] == "resistance"
        assert (zone["low"], zone["high"]) == (lower, upper)
        assert zone["price_relation"] == "above"
        assert zone["last_closed_relation"] == last_relation
        assert zone["price_test_state"] == "intrabar_crossed"
        assert zone["consecutive_closes_beyond"] == 0
        assert zone["role_reversal_confirmed"] is False
    first_cross = next(z for z in primary["levels"] if z["id"] == "zone_ac06bca058b93a13")
    assert first_cross["price_test_state"] == "first_close_beyond"
    assert first_cross["consecutive_closes_beyond"] == 1
    cutoff = datetime.fromisoformat(quote["observed_at"])
    for timeframe, count in (("1h", 72), ("4h", 60)):
        table = evidence["technical_snapshot"]["timeframes"][timeframe]["recent_closed_candles"]
        assert len(table["rows"]) == count
        assert all(datetime.fromisoformat(row[0]) <= cutoff for row in table["rows"])
    assert model["current_candle"]["quote_price"] == quote["price"]
    assert evidence["technical_snapshot"]["timeframes"]["1h"]["metrics"]["last_close"] == "85259.80"
    assert not any(key in model for key in ("expected_side", "expected_action", "future_outcome"))


@pytest.mark.parametrize("candidate", [
    {"id": "wait", "type": "wait", "title": "等待方向確認", "reason": "你必須觀望"},
    {"id": "range:short:105", "type": "range", "side": "short", "entry": "105",
     "stop_loss": "106", "take_profit": "101", "risk_metrics": {"net_risk_reward": "3.5"},
     "title": "符合你的偏好所以做空", "reason": "應優先做空", "trigger": "Python 建議進場",
     "preference_fit": "aligned", "risk_fit": "earlier_candidate", "counter_evidence": ["假設性建議"]},
])
def test_python_recommendations_are_not_sent_as_market_evidence(candidate):
    raw = {"tool": "strategy_candidates", "result": {"atr14": "2.5", "candidates": [candidate]}}
    before = deepcopy(raw)
    supplied = _model_tool_result(raw)
    assert raw == before
    clean = supplied["candidates"][0]
    assert clean["id"] == candidate["id"]
    assert {"title", "reason", "trigger", "preference_fit", "risk_fit", "counter_evidence"}.isdisjoint(clean)
    for key in ("side", "entry", "stop_loss", "take_profit", "risk_metrics"):
        if key in candidate:
            assert clean[key] == candidate[key]
    assert supplied["reference_scope"] == "preference_filtered_non_exhaustive_numerical_templates"


def test_a_chosen_direction_only_adds_the_traders_question_and_never_changes_evidence():
    request, candles, context, quote = fixture()
    payloads = []
    for bias in ("bullish", "bearish", None):
        selected = request | {"trading_style": "left", "risk_tolerance": "medium", "directional_bias": bias}
        trace = prepare_analysis_evidence(selected, candles, quote, context)
        payloads.append(agent_context(selected, candles, quote, context, prepared_trace=trace))
    assert [payload.get("trader_question") for payload in payloads] == [
        {"direction": "long"}, {"direction": "short"}, None]
    without = [{key: value for key, value in payload.items() if key != "trader_question"} for payload in payloads]
    assert without[0] == without[1] == without[2]
    # Python's entry templates are no longer part of the model input.
    assert "strategy_candidates" not in payloads[0]["precomputed_evidence"]
    assert "情境方向與你的判斷相反" not in json.dumps(payloads[0], ensure_ascii=False)


def test_changing_preferences_keeps_price_and_indicator_evidence_identical():
    request, candles, context, quote = fixture()
    payloads = []
    for style, bias, risk in (("left", "bearish", "high"), ("right", "bullish", "low"),
                             ("left", "bullish", "medium"), ("right", "bearish", "high")):
        selected = request | {"trading_style": style, "directional_bias": bias, "risk_tolerance": risk}
        trace = prepare_analysis_evidence(selected, candles, quote, context)
        payload = agent_context(selected, candles, quote, context, prepared_trace=trace)
        assert (payload["trading_style"], payload["risk_tolerance"]) == (style, risk)
        # The direction is only ever the trader's question, never a raw preference field.
        assert "directional_bias" not in payload
        assert list(payload).index("precomputed_evidence") < list(payload).index("risk_tolerance")
        payloads.append(payload)
    for payload in payloads[1:]:
        assert payload["current_candle"] == payloads[0]["current_candle"]
        for name in ("technical_snapshot", "support_resistance", "compare_timeframes", "funding_context"):
            assert payload["precomputed_evidence"][name] == payloads[0]["precomputed_evidence"][name]


def test_evaluation_fixtures_include_symmetric_rejections_and_confirmed_crossings():
    cases = {case["id"]: case for case in synthetic_cases(datetime(2026, 9, 30, 13, 26, tzinfo=UTC))}
    for first, second in (("resistance_rejection", "support_rejection"),
                          ("accepted_breakout_against_short_bias", "accepted_breakdown_against_long_bias")):
        one, two = cases[first]["snapshot"], cases[second]["snapshot"]
        assert Decimal(one["quote"]["price"]) + Decimal(two["quote"]["price"]) == 200
        for a, b in zip(one["candles"], two["candles"], strict=True):
            assert Decimal(a["high"]) + Decimal(b["low"]) == 200
            assert Decimal(a["close"]) + Decimal(b["close"]) == 200
        for case_id in (first, second):
            case = cases[case_id]
            s = case["snapshot"]
            trace = prepare_analysis_evidence(case["request"], s["candles"], s["quote"], s["context_candles"])
            levels = next(run["result"] for run in trace if run["tool"] == "support_resistance")
            if "rejection" in case_id:
                assert any(zone["price_relation"] == "inside" for zone in levels["levels"])
            else:
                assert any(zone["consecutive_closes_beyond"] == 2
                           for zone in levels["recently_invalidated_levels"])
            # Labels/rubrics are never sent to the model as expected answers.
            model_input = agent_context(case["request"], s["candles"], s["quote"], s["context_candles"], prepared_trace=trace)
            assert "rubric" not in model_input and "avoid_immediate_side" not in model_input


def test_review_flags_are_audit_results_and_never_rewrite_model_decisions():
    case = {"rubric": {"avoid_immediate_side": "short"}}
    report = {"entry_decision": {"action": "open_now", "side": "short"}}
    original = deepcopy(report)
    assert review_flags(case, report)
    assert report == original
    assert review_flags(case, {"entry_decision": {"action": "wait_for_entry", "side": "short"}}) == []


def test_failed_model_validation_is_preserved_for_review_without_provider_details(monkeypatch):
    case = synthetic_cases()[0]
    raw = '{"market": "測試輸出缺少其餘必要欄位"}'
    def failed(*args, **kwargs):
        from trade_helper import agent
        return agent._final_reasoning(raw, [])
    monkeypatch.setattr("evaluate_agent_strategy.analyze_with_tools", failed)
    result = evaluate_case(case)
    assert result["contract_valid"] is False
    assert result["model_attempts"] == [raw]
    assert "AI explanation is missing fields" in result["error_detail"]
    def provider_failure(*args, **kwargs):
        raise RuntimeError("Sensitive provider request details")
    monkeypatch.setattr("evaluate_agent_strategy.analyze_with_tools", provider_failure)
    result = evaluate_case(case)
    assert result["error_detail"] == "模型工具流程失敗：RuntimeError"
    assert "Sensitive" not in str(result)
