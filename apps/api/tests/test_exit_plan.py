import json
from decimal import Decimal
from types import SimpleNamespace

from trade_helper.exit_plan import sanitize_exit_plan
from trade_helper.technical_snapshot import prepare_analysis_evidence

from .test_technical_snapshot import final_response, fixture

LEVELS = [{"id": "support-a", "kind": "support", "low": "96", "high": "98"},
          {"id": "resistance-a", "kind": "resistance", "low": "104", "high": "106"}]


def plan(**changes):
    value = {
        "invalidation": {"price": "96", "confirmation": "close", "condition": "A 1h close below 96."},
        "protective_stop": "95.5",
        "take_profits": [{"price": "106", "portion_pct": 50}, {"price": "104", "portion_pct": 25}],
        "basis_level_ids": ["support-a", "resistance-a"],
    }
    return value | changes


def check(value, side="long", price="100"):
    return sanitize_exit_plan(value, side=side, price=Decimal(price), tick=Decimal("0.1"),
                              atr=Decimal(1), levels=LEVELS)


def test_long_plan_keeps_checked_parts_and_orders_targets_nearest_first():
    assert check(plan()) == {
        "version": "exit_plan_v1",
        "invalidation": {"price": "96", "confirmation": "close", "condition": "A 1h close below 96."},
        "protective_stop": "95.5",
        "take_profits": [{"price": "104", "portion_pct": 25}, {"price": "106", "portion_pct": 50}],
        "basis_level_ids": ["support-a", "resistance-a"],
    }


def test_short_plan_mirrors_the_sides():
    short = plan(invalidation={"price": "104", "confirmation": "touch"}, protective_stop="105",
                 take_profits=[{"price": "97", "portion_pct": None}])
    result = check(short, side="short")
    assert result["invalidation"] == {"price": "104", "confirmation": "touch", "condition": None}
    assert result["protective_stop"] == "105"
    assert result["take_profits"] == [{"price": "97", "portion_pct": None}]
    # The long plan's levels are on the wrong side for a short.
    assert check(plan(), side="short") is None


def test_plan_without_a_usable_invalidation_is_dropped():
    for invalidation in ({"price": "101", "confirmation": "close"},   # wrong side
                         {"price": "96", "confirmation": "wick"},     # unknown confirmation
                         {"price": "96.05", "confirmation": "close"},  # off the tick grid
                         {"price": "80", "confirmation": "close"},    # beyond the snapshot range
                         {"price": 96, "confirmation": "close"},      # not a price string
                         None):
        assert check(plan(invalidation=invalidation)) is None
    assert check("hold") is None
    assert check(plan(), side="flat") is None


def test_invalid_stop_targets_and_ids_are_dropped_without_inventing_values():
    result = check(plan(
        protective_stop="101",
        take_profits=[{"price": "99", "portion_pct": 10}, {"price": "104.05"},
                      {"price": "103", "portion_pct": 0}, {"price": "102", "portion_pct": True},
                      "105", {"price": "101"}],
        basis_level_ids=["made-up", {"id": "support-a"}, "support-a", "support-a"],
    ))
    assert result["protective_stop"] is None
    # Targets on the wrong side or off the tick grid go; invalid portions become unknown.
    assert result["take_profits"] == [{"price": "101", "portion_pct": None},
                                      {"price": "102", "portion_pct": None},
                                      {"price": "103", "portion_pct": None}]
    assert result["basis_level_ids"] == ["support-a"]


def test_portions_over_one_hundred_percent_are_cleared_and_overlong_conditions_omitted():
    result = check(plan(take_profits=[{"price": "104", "portion_pct": 60}, {"price": "106", "portion_pct": 60}],
                        invalidation={"price": "96", "confirmation": "close", "condition": "x" * 301}))
    assert [item["portion_pct"] for item in result["take_profits"]] == [None, None]
    assert result["invalidation"]["condition"] is None


def test_hold_reports_its_exit_plan_and_close_now_reports_none(monkeypatch):
    from trade_helper.agent import analyze_with_tools
    from trade_helper.analysis import build_report
    from trade_helper.report_contract import validate_report

    request, candles, context, quote = fixture()
    request.update(kind="positions", risk_tolerance="high", trading_style="left")
    position = {"version": 1, "market_id": request["market_id"], "leverage": 20,
                "margin_mode": "isolated", "entry_price": "180", "quantity": "1",
                "entry_time": None, "stop_loss": None, "take_profit": None}
    positions = [position | {"id": "p1", "side": "short"}, position | {"id": "p2", "side": "short"}]
    trace = prepare_analysis_evidence(request, candles, quote, context, positions)
    zone = next(z for z in next(x["result"] for x in trace if x["tool"] == "support_resistance")["levels"]
                if z["kind"] == "resistance")
    payload = json.loads(final_response(trace))
    payload["evidence_tools"].append("evaluate_positions")
    short_plan = {"invalidation": {"price": "188", "confirmation": "close",
                                   "condition": "A 1h close above 188."},
                  "protective_stop": "189.5",
                  "take_profits": [{"price": "175", "portion_pct": 50}, {"price": zone["high"], "portion_pct": 25}],
                  "basis_level_ids": [zone["id"]]}
    payload["position_decisions"] = {
        "p1": {"decision": "hold", "reason": "Hold the short.", "exit_plan": short_plan},
        "p2": {"decision": "close_now", "reason": "Close the short.", "exit_plan": short_plan},
    }
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        output=[], output_text=json.dumps(payload, ensure_ascii=False))))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr("trade_helper.agent.OpenAI", lambda **kwargs: client)
    decision = analyze_with_tools(request, candles, quote, context, positions, prepared_trace=trace)
    report = build_report(request, candles, quote, positions, decision, context)
    validate_report(report)

    held, closed = (review["agent_decision"] for review in report["position_reviews"])
    assert held["exit_plan"] == {
        "version": "exit_plan_v1",
        "invalidation": {"price": "188", "confirmation": "close", "condition": "A 1h close above 188."},
        "protective_stop": "189.5",
        "take_profits": [{"price": zone["high"], "portion_pct": 25}, {"price": "175", "portion_pct": 50}],
        "basis_level_ids": [zone["id"]],
    }
    assert closed == {"decision": "close_now", "reason": "Close the short.", "exit_plan": None}
    # The saved reasoning carries the same checked decisions as the reviews.
    assert report["reasoning"]["position_decisions"] == {"p1": held, "p2": closed}
