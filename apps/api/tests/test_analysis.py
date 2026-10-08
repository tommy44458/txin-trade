import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_helper.agent import (
    _final_reasoning,
    analyze_with_tools,
    fallback_analysis,
    strategy_instructions,
)
from trade_helper.analysis import (
    StaleMarketDataError,
    build_report,
    calculate,
    position_review,
    strategy_for,
)
from trade_helper.current_candle import current_candle_context
from trade_helper.follow_up import build_follow_up_plan
from trade_helper.indicators import execute_tool, tool_schema
from trade_helper.report_contract import REPORT_VERSION, validate_report
from trade_helper.risk import configured_cost_scenario
from trade_helper.strategy_engine import VERSION as STRATEGY_VERSION
from trade_helper.strategy_engine import compare_timeframes
from trade_helper.support_levels_v3 import VERSION, historical_levels_v3
from trade_helper.timeframes import advance_candle, candle_close, candle_open


def sample_candles(count=80, recent=False, timeframe="1h"):
    start = (advance_candle(candle_open(datetime.now(UTC), timeframe), timeframe, -count)
             if recent else candle_open(datetime(2026, 1, 1, tzinfo=UTC), timeframe))
    result = []
    for i in range(count):
        close = Decimal(100 + i) + Decimal((i % 9) - 4) * 2
        opened = advance_candle(start, timeframe, i)
        result.append({
            "open_time": opened.isoformat(),
            "close_time": candle_close(opened, timeframe).isoformat(),
            "open": str(close - 1), "high": str(close + 3), "low": str(close - 3),
            "close": str(close), "volume": "100",
        })
    return result


def test_pivots_only_use_confirmed_candles():
    candles = sample_candles()
    metrics = calculate(candles, "1h")
    assert metrics["candle_count"] == 80
    assert metrics["atr14"] != "0"
    levels = historical_levels_v3(candles, "1h", Decimal(candles[-1]["close"]),
                                  Decimal("0.1"), "binance:perp:BTCUSDT")["levels"]
    assert levels
    assert all(level["confirmed_at"] <= metrics["last_candle_at"] for level in levels)


def test_level_calculation_rejects_missing_or_invalid_candles():
    import pytest

    candles = sample_candles()
    with pytest.raises(ValueError, match="gap"):
        calculate(candles[:30] + candles[31:], "1h")
    candles = sample_candles()
    candles[30]["high"] = "1"
    with pytest.raises(ValueError, match="OHLCV"):
        calculate(candles, "1h")


def test_preference_does_not_change_market_calculation():
    candles = sample_candles(recent=True)
    quote = {"price": candles[-1]["close"], "tick_size": "0.1", "snapshot_hash": "a" * 64,
             "observed_at": datetime.now(UTC).isoformat()}
    base = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "directional_bias": None, "risk_tolerance": None}
    bullish_request = base | {"directional_bias": "bullish", "risk_tolerance": "low", "leverage": 5}
    bearish_request = base | {"directional_bias": "bearish", "risk_tolerance": "high", "leverage": 10}
    bullish = build_report(bullish_request, candles, quote, [], fallback_analysis(bullish_request, candles, quote))
    bearish = build_report(bearish_request, candles, quote, [], fallback_analysis(bearish_request, candles, quote))
    assert bullish["metrics"] == bearish["metrics"]
    left_request = base | {"trading_style": "left", "leverage": 5}
    right_request = base | {"trading_style": "right", "leverage": 5}
    left = build_report(left_request, candles, quote, [],
                        fallback_analysis(left_request, candles, quote))
    right = build_report(right_request, candles, quote, [],
                         fallback_analysis(right_request, candles, quote))
    assert left["metrics"] == right["metrics"]
    assert left["preference_assessment"]["trading_style"] == "left"
    assert right["preference_assessment"]["trading_style"] == "right"
    assert bullish["strategy_level_algorithm_version"] == VERSION
    assert bullish["metrics"]["level_algorithm_version"] == VERSION
    assert all(level["algorithm_version"] == VERSION for level in bullish["metrics"]["levels"])
    assert bullish["analysis_mode"] == "rules_only"
    assert bullish["events_status"] == "not_integrated"


def test_profitable_stop_and_reached_stop_are_handled():
    position = {"id": "pos", "version": 2, "entry_price": "100", "quantity": "2", "side": "long", "leverage": 5, "stop_loss": "110", "take_profit": "140"}
    review = position_review(position, {"price": "120"})
    assert review["unrealized_pnl_usdt"] == "40"
    assert review["remaining_risk_reward"] == "2.00"
    reached = position_review(position, {"price": "105"})
    assert "請確認實際成交" in reached["messages"][0]


def test_short_mark_price_and_leverage_are_theoretical():
    position = {"id": "short", "version": 1, "side": "short", "leverage": 10,
                "margin_mode": "isolated", "entry_price": "100", "quantity": "2",
                "stop_loss": "110", "take_profit": "80"}
    review = position_review(position, {"price": "90", "mark_price": "92"})
    assert review["valuation_price_type"] == "mark"
    assert review["unrealized_pnl_usdt"] == "16"
    assert review["theoretical_initial_margin_usdt"] == "20"
    assert review["return_on_theoretical_margin_pct"] == "80.00"


def test_tool_trace_uses_request_leverage_and_rejects_wrong_timeframe():
    candles = sample_candles(recent=True)
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h",
               "leverage": 10, "directional_bias": None, "risk_tolerance": None}
    quote = {"price": candles[-1]["close"], "tick_size": "0.1", "mark_price": candles[-1]["close"],
             "observed_at": datetime.now(UTC).isoformat()}
    run = execute_tool("strategy_candidates", {"reason": "Check candidates"}, candles, quote, request)
    assert run["tool"] == "strategy_candidates"
    assert run["result"]["candidates"]
    import pytest
    assert set(tool_schema("support_resistance")["parameters"]["properties"]) == {"reason"}
    bound = execute_tool("support_resistance", {"reason": "Check selected levels"},
                         candles, quote, request)
    assert bound["parameters"]["timeframe"] == "1h"
    assert bound["result"]["method"] == VERSION
    with pytest.raises(ValueError, match="timeframe"):
        execute_tool("support_resistance", {"reason": "Check levels", "timeframe": "4h"},
                     candles, quote, request)


def detailed_explanation(result):
    return {
        "agent_stance": "wait", "position_decisions": {},
        "entry_decision": {"action": "stand_aside", "side": None, "entry_price": None, "stop_loss": None, "take_profit": None, "trigger": None, "invalidation": None, "reason": "目前先觀望，沒有完整價位方案。", "basis_level_ids": []},
        "macro_outlook": {"stance": "neutral", "reason": "缺少完整宏觀實際數據，暫判中性。", "evidence_ids": []},
        "supporting_evidence": "已確認區間提供觀察依據，但跨週期確認尚未完整，因此先觀望。",
        "counter_evidence": "若價格穿透原區間，原來的阻力假設需重新檢查。",
        "preference_impact": "偏好僅影響候選取捨，不改變行情證據。",
        "follow_up_reasons": {
            item["id"]: "用新的行情區分區間回收或直接穿透，並核對量能和另一週期。"
            for item in result["follow_up_plan"]
        },
    }


def test_agent_analysis_does_not_fall_back_to_python_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    candles = sample_candles(recent=True)
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h"}
    quote = {"price": candles[-1]["close"], "observed_at": datetime.now(UTC).isoformat()}
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        analyze_with_tools(request, candles, quote)


def test_agent_uses_precomputed_evidence_without_required_tool_roundtrips(monkeypatch):
    candles = sample_candles(recent=True)
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 5}
    quote = {"price": candles[-1]["close"], "tick_size": "0.1", "snapshot_hash": "a" * 64,
             "observed_at": datetime.now(UTC).isoformat()}
    outputs = iter([
        SimpleNamespace(output=[], output_text='{"market":"趨勢資料有限。","levels":"採信已確認波段。","strategy":"維持條件式觀望。","evidence_tools":["support_resistance","strategy_candidates"],"strategy_decision":"wait"}'),
    ])
    class FakeResponses:
        def create(self, **_kwargs):
            response = next(outputs)
            if not response.output:
                result = execute_tool("strategy_candidates", {"reason": "Fixture"},
                                      candles, quote, request)["result"]
                response.output_text = json.dumps(
                    json.loads(response.output_text) | detailed_explanation(result),
                    ensure_ascii=False)
            return response
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr("trade_helper.agent.OpenAI", lambda **_kwargs: SimpleNamespace(responses=FakeResponses()))
    result = analyze_with_tools(request, candles, quote)
    assert result["mode"] == "openai_assisted"
    assert result["strategy_decision"] == "wait"
    assert [run["tool"] for run in result["tool_trace"]] == [
        "technical_snapshot", "funding_context", "support_resistance", "strategy_candidates"]
    assert result["analysis_execution"]["model_requests"] == 1
    report = build_report(request, candles, quote, [], result)
    assert report["strategies"][0]["type"] == "wait"
    assert result["tool_trace"][2]["result"]["method"] == VERSION


def test_agent_receives_cross_timeframe_evidence_and_only_ranks_validated_ids(monkeypatch):
    candles = sample_candles(recent=True)
    context_candles = sample_candles(recent=True, timeframe="4h")
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 5}
    quote = {"price": candles[-1]["close"], "tick_size": "0.1", "snapshot_hash": "a" * 64,
             "observed_at": datetime.now(UTC).isoformat()}
    run = execute_tool("strategy_candidates", {"reason": "Check valid IDs"}, candles, quote,
                       request, context_candles)
    candidate_ids = [candidate["id"] for candidate in run["result"]["candidates"]]
    response_text = json.dumps({
        "market": "兩個週期以已收盤資料比較。", "levels": "引用確認過的區間。",
        "strategy": "僅排序已驗證候選。", "evidence_tools": ["support_resistance",
                                                     "compare_timeframes", "strategy_candidates"],
        "candidate_order": candidate_ids, "strategy_decision": "candidate",
        **detailed_explanation(run["result"])}, ensure_ascii=False)
    outputs = iter([SimpleNamespace(output=[], output_text=response_text)])

    class FakeResponses:
        def create(self, **_kwargs):
            return next(outputs)

    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr("trade_helper.agent.OpenAI", lambda **_kwargs: SimpleNamespace(responses=FakeResponses()))
    decision = analyze_with_tools(request, candles, quote, context_candles)
    assert [item["tool"] for item in decision["tool_trace"]] == [
        "technical_snapshot", "funding_context", "support_resistance", "compare_timeframes",
        "strategy_candidates"]
    report = build_report(request, candles, quote, [], decision, context_candles)
    assert [item["id"] for item in report["strategies"]] == candidate_ids
    assert report["timeframe_context"]["context_timeframe"] == "4h"


def test_agent_direction_is_independent_of_python_candidate_gate():
    candles = sample_candles(recent=True)
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h",
               "leverage": 5, "risk_tolerance": "high"}
    quote = {"price": candles[-1]["close"], "tick_size": "0.1",
             "snapshot_hash": "a" * 64, "observed_at": datetime.now(UTC).isoformat()}
    decision = fallback_analysis(request, candles, quote)
    decision["mode"] = "openai_assisted"
    decision["agent_stance"] = "short"
    decision["reasoning"]["agent_stance"] = "short"
    decision["reasoning"]["entry_decision"] = {"action": "stand_aside", "side": None, "entry_price": None, "stop_loss": None, "take_profit": None, "trigger": None, "invalidation": None, "reason": "目前先觀望，沒有完整價位方案。", "basis_level_ids": []}
    decision["reasoning"]["macro_outlook"] = {
        "stance": "neutral", "reason": "宏觀資料不足，暫不選方向。", "evidence_ids": []}
    decision["reasoning"]["position_decisions"] = {}
    decision["reasoning"]["strategy"] = "現價走弱，偏空。"
    report = build_report(request, candles, quote, [], decision)
    assert report["agent_stance"] == "short"
    assert report["strategies"] == next(run["result"]["candidates"] for run in decision["tool_trace"]
                                         if run["tool"] == "strategy_candidates")
    validate_report(report)


def test_agent_can_recommend_immediate_entry_independent_of_python_candidates():
    candles = sample_candles(recent=True)
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h",
               "leverage": 5, "risk_tolerance": "high"}
    quote = {"price": candles[-1]["close"], "tick_size": "0.1",
             "snapshot_hash": "a" * 64, "observed_at": datetime.now(UTC).isoformat()}
    decision = fallback_analysis(request, candles, quote)
    entry = Decimal(quote["price"])
    plan = {"action": "open_now", "side": "short", "entry_price": str(entry),
            "stop_loss": str(entry + 2), "take_profit": str(entry - 5),
            "trigger": "現價已符合空方條件。",
            "invalidation": "價格突破上方結構或觸及止損。",
            "reason": "現價與市場結構支持立即評估空單。",
            "basis_level_ids": []}
    decision["mode"] = "openai_assisted"
    decision["agent_stance"] = "short"
    decision["reasoning"]["agent_stance"] = "short"
    decision["reasoning"]["entry_decision"] = plan
    decision["reasoning"]["macro_outlook"] = {
        "stance": "neutral", "reason": "宏觀資料不足，暫不選方向。", "evidence_ids": []}
    decision["reasoning"]["position_decisions"] = {}
    decision["reasoning"]["strategy"] = "現在開空單，按結構設止損與止盈。"
    report = build_report(request, candles, quote, [], decision)
    assert report["entry_decision"]["action"] == "open_now"
    assert report["entry_decision"]["side"] == "short"
    assert report["entry_risk_reference"]["net_risk_reward"] is not None
    validate_report(report)
    altered = deepcopy(report)
    altered["entry_decision"]["stop_loss"] = str(entry - 1)
    with pytest.raises(ValueError):
        validate_report(altered)


def test_strategy_uses_qualified_v3_zones_and_rejects_v2():
    support = {"kind": "support", "low": "98", "high": "100", "center": "99",
               "algorithm_version": VERSION, "pivot_count": 2,
               "independent_touch_count": 2, "zone_state": "active"}
    resistance = {**support, "kind": "resistance", "low": "110", "high": "112", "center": "111"}
    metrics = {"trend": "bullish", "atr14": "2", "levels": [support, resistance],
               "level_algorithm_version": VERSION, "ma20": "101", "ma50": "99",
               "last_candle_at": datetime.now(UTC).isoformat()}
    quote = {"price": "104", "tick_size": "0.1"}
    context = compare_timeframes(metrics, metrics, "1h")
    assert strategy_for(metrics, None, "medium", quote, context=context)[0]["type"] == "pullback"
    metrics["levels"] = [{**support, "independent_touch_count": 0}, resistance]
    assert strategy_for(metrics, None, "medium", quote, context=context)[0]["type"] == "wait"
    metrics["level_algorithm_version"] = "confirmed_pivot_cluster_v2"
    import pytest
    with pytest.raises(ValueError, match="requires v3"):
        strategy_for(metrics, None, "medium", quote, context=context)


def candidate_fixture(trend="bullish", resistance_low="110"):
    support = {"kind": "support", "low": "98", "high": "100", "center": "99",
               "algorithm_version": VERSION, "pivot_count": 2,
               "independent_touch_count": 2, "zone_state": "active"}
    resistance = {**support, "kind": "resistance", "low": resistance_low,
                  "high": str(Decimal(resistance_low) + 2),
                  "center": str(Decimal(resistance_low) + 1)}
    return ({"trend": trend, "atr14": "2", "levels": [support, resistance],
             "level_algorithm_version": VERSION, "ma20": "101", "ma50": "99",
             "last_candle_at": datetime.now(UTC).isoformat()},
            {"price": "104", "tick_size": "0.1"})


@pytest.mark.parametrize(("bias", "risk", "expected_type", "fit"), [
    ("bullish", "low", "pullback", "aligned"),
    ("bullish", "medium", "pullback", "aligned"),
    ("bullish", "high", "pullback", "aligned"),
    ("bearish", "low", "wait", None),
    ("bearish", "medium", "pullback", "conflict"),
    ("bearish", "high", "pullback", "conflict"),
    (None, None, "pullback", "unspecified"),
])
def test_bias_risk_matrix_preserves_v3_levels(bias, risk, expected_type, fit):
    metrics, quote = candidate_fixture()
    before = metrics["levels"].copy()
    candidate = strategy_for(metrics, bias, risk, quote, context=compare_timeframes(metrics, metrics, "1h"))[0]
    assert candidate["type"] == expected_type
    assert metrics["levels"] == before
    if fit:
        assert candidate["preference_fit"] == fit
        assert candidate["entry"] == "100"
        assert candidate["take_profit"] == "110"
        assert Decimal(candidate["stop_loss"]) < Decimal(candidate["entry"])


def test_gross_ratio_does_not_pass_if_costed_ratio_fails():
    metrics, quote = candidate_fixture(resistance_low="106.1")
    metrics["levels"][0]["low"] = "96"
    metrics["levels"][1]["low"] = "106.8"
    metrics["levels"][1]["center"] = "107.8"
    metrics["levels"][1]["high"] = "108.8"
    # Gross 6.8/4.5 = 1.51, while adverse fills and fees lower the net ratio.
    assert Decimal("6.8") / Decimal("4.5") > Decimal("1.5")
    assert strategy_for(metrics, None, "medium", quote,
                        context=compare_timeframes(metrics, metrics, "1h"))[0]["type"] == "wait"


def test_bearish_candidate_uses_short_price_order_and_net_ratio():
    metrics, quote = candidate_fixture(trend="bearish")
    candidate = strategy_for(metrics, "bearish", "medium", quote,
                             context=compare_timeframes(metrics, metrics, "1h"))[0]
    assert candidate["type"] == "pullback"
    assert candidate["side"] == "short"
    assert Decimal(candidate["take_profit"]) < Decimal(candidate["entry"]) < Decimal(candidate["stop_loss"])
    assert candidate["risk_reward"] == candidate["risk_metrics"]["net_risk_reward"]
    assert Decimal(candidate["risk_reward"]) < Decimal(candidate["gross_risk_reward"])


def test_report_contract_rejects_mutated_level_trace_and_wrong_cost():
    candles = sample_candles(recent=True)
    quote = {"price": candles[-1]["close"], "tick_size": "0.1", "snapshot_hash": "a" * 64,
             "observed_at": datetime.now(UTC).isoformat()}
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 5}
    report = build_report(request, candles, quote, [], fallback_analysis(request, candles, quote))
    validate_report(report)
    original = report["metrics"]["levels"]
    report["metrics"]["levels"] = []
    with pytest.raises(ValueError, match="differ from the v3 tool"):
        validate_report(report)
    report["metrics"]["levels"] = original
    report["strategy_level_algorithm_version"] = "confirmed_pivot_cluster_v2"
    with pytest.raises(ValueError, match="version is invalid"):
        validate_report(report)


def test_report_contract_recomputes_candidate_prices_and_costs():
    metrics, quote = candidate_fixture()
    now = datetime.now(UTC).isoformat()
    metrics["last_candle_at"] = now
    for index, level in enumerate(metrics["levels"]):
        level.update(id=f"zone-{index}", timeframe="1h", confirmed_at=now)
    quote.update(observed_at=now, cost_scenario=configured_cost_scenario())
    metrics["last_close"] = quote["price"]
    context = compare_timeframes(metrics, metrics, "1h")
    metrics["market_state"] = context["market_state"]
    # Evidence never depends on the user's directional hypothesis.
    candidate = strategy_for(metrics, None, "medium", quote, context=context)[0]
    plan = build_follow_up_plan(metrics["levels"], quote, "1h", now)
    live = current_candle_context([{"close": metrics["last_close"],
                                    "close_time": now}], quote, "1h")
    report = {"follow_up_plan": plan, "current_candle": live,
              "report_schema_version": REPORT_VERSION,
              "analysis_kind": "market", "analysis_mode": "rules_only",
              "position_advice_version": None, "position_snapshot": [], "position_reviews": [],
              "strategy_level_algorithm_version": VERSION,
              "strategy_rule_version": STRATEGY_VERSION, "strategy_source": "validated_python_candidates",
              "risk_rule_version": "cost_scenario_v1", "market_id": "binance:perp:BTCUSDT",
              "timeframe": "1h", "market_snapshot_sha256": "a" * 64,
              "cost_assumptions": quote["cost_scenario"], "quote": quote,
              "metrics": metrics, "strategies": [candidate], "timeframe_context": context,
              "analysis_leverage": 5,
              "preference_assessment": {"directional_bias": "bullish", "risk_tolerance": "medium"},
              "tool_trace": [{"tool": "support_resistance", "result": {
                  "method": VERSION, "levels": metrics["levels"],
                  "reference_price": quote["price"], "reference_time": now, "source_time": now}},
                             {"tool": "compare_timeframes", "result": context},
                             {"tool": "strategy_candidates", "result": {"candidates": [deepcopy(candidate)], "follow_up_plan": plan,
                                                                     "current_candle": live}}]}
    validate_report(report)
    candidate["risk_metrics"]["net_loss_per_unit_usdt"] = "1"
    with pytest.raises(ValueError, match="strategy data differ"):
        validate_report(report)
    candidate["risk_metrics"] = strategy_for(metrics, "bullish", "medium", quote,
                                               context=context)[0]["risk_metrics"]
    candidate["stop_loss"] = "97.55"
    with pytest.raises(ValueError, match="strategy data differ"):
        validate_report(report)


def test_stale_market_inputs_have_distinct_failure_codes():
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 5}
    candles = sample_candles(recent=True)
    quote = {"price": "100", "tick_size": "0.1", "snapshot_hash": "a" * 64,
             "observed_at": (datetime.now(UTC) - timedelta(seconds=181)).isoformat()}
    with pytest.raises(StaleMarketDataError) as exc:
        build_report(request, candles, quote, [], {})
    assert exc.value.code == "QUOTE_STALE"
    quote["observed_at"] = datetime.now(UTC).isoformat()
    with pytest.raises(StaleMarketDataError) as exc:
        build_report(request, sample_candles(), quote, [], {})
    assert exc.value.code == "CANDLES_STALE"



def test_report_preserves_snapshot_when_model_finishes_after_first_minute():
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 5}
    candles = sample_candles(recent=True)
    for candle in candles:
        for key in ("open_time", "close_time"):
            candle[key] = (datetime.fromisoformat(candle[key]) - timedelta(minutes=2)).isoformat()
    observed = (datetime.now(UTC) - timedelta(seconds=75)).isoformat()
    quote = {"price": candles[-1]["close"], "tick_size": "0.1",
             "snapshot_hash": "a" * 64, "observed_at": observed}
    decision = fallback_analysis(request, candles, quote)
    report = build_report(request, candles, quote, [], decision)
    assert report["quote"]["observed_at"] == observed
    assert report["analysis_mode"] == "rules_only"

def test_context_candle_cannot_close_after_quote_snapshot():
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 5}
    primary = sample_candles(recent=True)
    context = sample_candles(recent=True, timeframe="4h")
    context[-1]["close_time"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    quote = {"price": "100", "tick_size": "0.1", "snapshot_hash": "a" * 64,
             "observed_at": datetime.now(UTC).isoformat()}
    with pytest.raises(ValueError, match="later than the analysis snapshot"):
        build_report(request, primary, quote, [], {}, context)


def test_strategy_prompt_requires_tool_grounded_public_rationale():
    market_prompt = strategy_instructions(False, prompt_locale="zh-TW")
    position_prompt = strategy_instructions(True, prompt_locale="zh-TW")
    assert "support_resistance" in market_prompt
    assert "strategy_candidates" in market_prompt
    assert "volume_signal" in market_prompt
    assert "current_candle" in market_prompt
    assert "盤中累積量" in market_prompt
    assert "非常激進" in market_prompt
    assert "Python 指標、價位和風險估算都是參考資料" in market_prompt
    assert "什麼情況會讓你改看法" in market_prompt
    assert "內部思考過程" in market_prompt
    assert "evaluate_positions" in position_prompt
    assert "position_decisions" in position_prompt
    assert "現在平倉" in position_prompt

    trace = [{"tool": "support_resistance"}, {"tool": "strategy_candidates"}]
    response = {"market": "趨勢待確認。", "levels": "採信確認區間。",
                "strategy": "反面證據尚未排除，維持觀望。",
                "evidence_tools": ["support_resistance"], "strategy_decision": "wait"}
    with pytest.raises(ValueError, match="omits required Python evidence"):
        _final_reasoning(json.dumps(response, ensure_ascii=False), trace)
