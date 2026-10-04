from datetime import UTC, datetime, timedelta

from trade_helper.outcomes import items_for, new_state, settle, step

START = datetime(2026, 10, 1, 0, 2, 30, tzinfo=UTC)  # Mid-candle: evaluation starts at 00:05.


def candles(*rows, start=datetime(2026, 10, 1, 0, 5, tzinfo=UTC)):
    """Consecutive 5-minute candles from (high, low, close) rows."""
    return [{"open_time": (start + timedelta(minutes=5 * index)).isoformat(),
             "high": str(high), "low": str(low), "close": str(close)}
            for index, (high, low, close) in enumerate(rows)]


def report(entry=None, reviews=(), timeframe="1h", price="100"):
    return {"market_id": "binance:perp:BTCUSDT", "timeframe": timeframe,
            "quote": {"observed_at": START.isoformat(), "price": price, "mark_price": price},
            "entry_decision": entry, "position_reviews": list(reviews)}


def plan(action="open_now", side="long", entry="100", stop="98", target="104", rule="touch"):
    value = {"action": action, "side": side, "entry_price": entry, "stop_loss": stop, "take_profit": target}
    if action == "wait_for_entry" and rule:
        value["trigger_rule"] = {"type": rule, "price": entry}
    return value


def run(item, rows, **kwargs):
    return settle(item, step(item, new_state(item), candles(*rows, **kwargs)))


def test_an_entry_wins_at_its_target_and_loses_at_its_stop_in_r():
    [item] = items_for(report(plan()))
    won = run(item, [(101, 99.5, 100.5), (104.2, 100, 104)])
    assert won["result"] == "win" and won["r_multiple"] == "2.00" and won["exit_price"] == "104"
    lost = run(item, [(101, 99.5, 100.5), (100, 97.5, 98)])
    assert lost["result"] == "loss" and lost["r_multiple"] == "-1.00"


def test_a_candle_touching_both_the_stop_and_the_target_is_a_loss():
    [item] = items_for(report(plan(side="short", stop="102", target="96")))
    both = run(item, [(102.5, 95.5, 99)])
    assert both["result"] == "loss" and both["r_multiple"] == "-1.00"


def test_a_waiting_entry_expires_untriggered_or_starts_once_its_price_trades():
    [item] = items_for(report(plan("wait_for_entry", entry="97", stop="95", target="101")))
    # 1H: max(12 candles, 1 day) = 1 day of 5-minute candles.
    untouched = run(item, [(100.5, 99, 100)] * 300)
    assert untouched["result"] == "not_triggered"
    assert untouched["resolved_at"] == item["wait_until"]
    started = run(item, [(100, 98, 99), (98, 96.5, 97.5), (101.5, 97, 101)])
    assert started["result"] == "win" and started["r_multiple"] == "2.00"


def test_on_the_entry_candle_the_stop_counts_and_the_target_does_not():
    [item] = items_for(report(plan("wait_for_entry", entry="97", stop="95", target="99")))
    stopped = run(item, [(97.5, 94.5, 95)])
    assert stopped["result"] == "loss"
    unresolved = step(item, new_state(item), candles((99.5, 96.5, 99)))
    assert unresolved["entered_at"] and not unresolved["done"]


def test_a_trade_still_open_after_thirty_days_is_marked_to_market():
    [item] = items_for(report(plan()))
    month = 30 * 24 * 12
    expired = run(item, [(101, 99.5, 101)] * (month + 1))
    assert expired["result"] == "expired" and expired["r_multiple"] == "0.50"


def test_standing_aside_records_the_largest_moves_afterwards_without_a_verdict():
    [item] = items_for(report({"action": "stand_aside"}))
    week = 7 * 24 * 12
    observed = run(item, [(103, 99, 101)] + [(101, 95, 100)] * week)
    assert observed["result"] == "observed"
    assert (observed["max_up_pct"], observed["max_down_pct"], observed["end_pct"]) == ("3.00", "-5.00", "0.00")


def hold(confirmation="close", invalidation="98", target="104", side="long"):
    return {"position_id": "p1", "side": side, "agent_decision": {"decision": "hold", "exit_plan": {
        "invalidation": {"price": invalidation, "confirmation": confirmation},
        "take_profits": [{"price": target}, {"price": "110"}]}}}


def test_a_close_confirmed_hold_ignores_a_dip_inside_the_hour_and_ends_on_the_hourly_close():
    [item] = items_for(report(reviews=[hold()]))
    assert item["target"] == "104"  # The nearest take-profit.
    # 00:05 to 00:55 dips below 98 but the 00:55 candle (the 1H close) closes at 99.
    dip = [(100, 97, 99)] * 11
    assert not run(item, dip)["done"]
    broken = run(item, dip + [(99, 96.5, 97)] * 12)
    assert broken["result"] == "invalidated" and broken["exit_price"] == "97" and broken["r_multiple"] == "-1.50"
    touch = items_for(report(reviews=[hold("touch")]))[0]
    assert run(touch, dip)["result"] == "invalidated"
    reached = run(item, [(104.5, 99, 104)])
    assert reached["result"] == "target_hit" and reached["r_multiple"] == "2.00"


def test_closing_was_right_when_price_then_moved_against_the_position():
    closing = {"position_id": "p2", "side": "long", "agent_decision": {"decision": "close_now"}}
    [item] = items_for(report(reviews=[closing]))
    week = 7 * 24 * 12
    assert run(item, [(101, 94, 95)] * (week + 1))["result"] == "good_close"
    assert run(item, [(108, 99, 107)] * (week + 1))["result"] == "early_close"


def test_reports_without_a_checkable_plan_have_nothing_to_judge():
    assert items_for(report(None)) == []
    assert items_for(report(plan(stop="100"))) == []  # No distance to the stop.
    assert items_for(report(reviews=[{"position_id": "p", "side": "long",
                                      "agent_decision": {"decision": "hold", "exit_plan": None}}])) == []


def test_a_close_confirmed_entry_starts_at_the_confirming_close_not_at_a_touch():
    [item] = items_for(report(plan("wait_for_entry", entry="102", stop="99", target="108", rule="close_above")))
    # Inside the 00:00 hour price trades through 102 but the hour closes at 101.5: no entry.
    hour = [(102.5, 100.5, 101)] * 10 + [(102.2, 101, 101.5)]
    assert run(item, hour)["entered_at"] is None
    # The 01:00 hour closes at 103: filled at 103, risk 4, target +5 = +1.25R.
    confirmed = run(item, hour + [(103, 101.5, 102.5)] * 11 + [(103.2, 102.5, 103)] + [(108.5, 103, 108)])
    assert confirmed["result"] == "win" and confirmed["fill"] == "103" and confirmed["r_multiple"] == "1.25"


def test_a_close_confirmed_entry_does_not_lose_to_a_dip_it_never_entered():
    [item] = items_for(report(plan("wait_for_entry", side="short", entry="99", stop="101", target="95",
                                   rule="close_below")))
    # A spike to 101.5 before any close below 99: nothing was entered, so nothing was lost.
    # The 00:55 candle closes the hour at 98.8: filled there, risk 2.2, so the 95 target is +1.73R.
    spiked = run(item, [(101.5, 99.5, 100)] * 10 + [(100, 98.5, 98.8)] + [(98.8, 94.5, 95)])
    assert spiked["result"] == "win" and spiked["r_multiple"] == "1.73"


def test_an_old_waiting_plan_without_a_price_rule_is_not_judged():
    [item] = items_for(report(plan("wait_for_entry", rule=None)))
    assert item["unverifiable"] is True and item["trigger"] is None
    [touch] = items_for(report(plan("wait_for_entry")))
    assert touch["unverifiable"] is False and touch["trigger"] == {"type": "touch", "price": "100"}


def ruled(confirmation, price="99", **changes):
    return plan(**changes) | {"invalidation_rule": {"price": price, "confirmation": confirmation}}


def test_a_touch_invalidation_rule_ends_the_trade_before_its_stop():
    [item] = items_for(report(ruled("touch")))
    assert item["exit_rule"] == {"price": "99", "confirmation": "touch"}
    cut = run(item, [(100.5, 98.8, 99.2)])
    assert (cut["result"], cut["r_multiple"], cut["exit_reason"]) == ("loss", "-0.50", "invalidation")
    # Through the rule and the stop in one candle: the rule comes first.
    assert run(item, [(100, 97.5, 98)])["r_multiple"] == "-0.50"
    # Through the rule and the target in one candle: the conservative rule wins.
    assert run(item, [(104.5, 98.9, 104)])["exit_reason"] == "invalidation"


def test_a_close_invalidation_rule_waits_for_the_hourly_close():
    [item] = items_for(report(ruled("close")))
    dip = [(100.2, 98.6, 99.5)] * 11  # Below 99 inside the hour, the hour closes at 99.5.
    assert not run(item, dip)["done"]
    broken = run(item, dip + [(99.5, 98.4, 98.6)] * 12)
    assert (broken["result"], broken["exit_price"], broken["r_multiple"]) == ("loss", "98.6", "-0.70")
    assert run(item, [(104.2, 99.5, 104)])["exit_reason"] == "target"


def test_an_invalidation_rule_outside_entry_and_stop_is_ignored():
    for price in ("97", "100", "101"):  # Beyond the stop, at the entry, above the entry.
        assert items_for(report(ruled("touch", price)))[0]["exit_rule"] is None
