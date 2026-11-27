from decimal import Decimal

import pytest

from trade_helper.support_levels_v3 import historical_levels_v3

from .test_support_levels import candles_with_swings


def test_v3_waits_for_confirmation_and_keeps_formation_atr() -> None:
    candles = candles_with_swings()
    initial = historical_levels_v3(candles[:60], "1h", Decimal(100))
    after = historical_levels_v3(candles, "1h", Decimal(100))
    support_initial = next(zone for zone in initial["levels"] if zone["kind"] == "support")
    support_after = next(zone for zone in after["levels"] if zone["kind"] == "support")
    assert support_initial["id"] == support_after["id"]
    assert support_initial["formation_atr"] == support_after["formation_atr"]
    assert support_after["pivot_count"] == 2
    assert support_after["independent_touch_count"] == 1
    assert support_after["revision"] == 2

    candles = candles_with_swings(82)
    candles[78]["low"] = "92"
    before_confirmation = historical_levels_v3(candles[:80], "1h", Decimal(100))
    after_confirmation = historical_levels_v3(candles, "1h", Decimal(100))
    assert all(zone["center"] != "92" for zone in before_confirmation["levels"])
    assert any(zone["center"] == "92" for zone in after_confirmation["levels"])


def test_v3_repeated_bars_inside_zone_count_once_until_rearmed() -> None:
    candles = candles_with_swings(100)
    for index in range(50, 56):
        candles[index]["low"] = "95.1"
        candles[index]["close"] = "95.2"
    result = historical_levels_v3(candles, "1h", Decimal(100))
    support = next(zone for zone in result["levels"] if zone["kind"] == "support")
    assert support["independent_touch_count"] == 2


def test_v3_two_closes_beyond_zone_invalidate_it() -> None:
    candles = candles_with_swings()
    for index in (60, 61):
        candles[index].update({"open": "94", "high": "96", "low": "93", "close": "94"})
    result = historical_levels_v3(candles, "1h", Decimal(100))
    assert result["invalidated_zone_count"] >= 1
    assert all(zone["center"] != "95.1" for zone in result["levels"])


@pytest.mark.parametrize("kind", ["support", "resistance"])
def test_quote_inside_or_across_original_zone_does_not_hide_or_rewrite_it(kind) -> None:
    candles = candles_with_swings()
    original = next(z for z in historical_levels_v3(candles, "1h", Decimal(100))["levels"]
                    if z["kind"] == kind)
    for quote in (Decimal(original["center"]), Decimal(original["low"]) - Decimal("0.1"),
                  Decimal(original["high"]) + Decimal("0.1")):
        result = historical_levels_v3(candles, "1h", quote)
        zone = next(z for z in result["levels"] if z["id"] == original["id"])
        assert (zone["low"], zone["high"], zone["revision"], zone["kind"]) == (
            original["low"], original["high"], original["revision"], kind)
        assert zone["consecutive_closes_beyond"] == 0
        assert zone["zone_state"] == "active"
        assert zone["role_reversal_confirmed"] is False
    inside = historical_levels_v3(candles, "1h", Decimal(original["center"]))
    assert next(z for z in inside["levels"] if z["id"] == original["id"])["price_relation"] == "inside"


@pytest.mark.parametrize("kind,close,op,high,low", [
    ("resistance", "106", "100", "107", "99"),
    ("support", "94", "100", "101", "93"),
])
def test_first_close_crossing_remains_visible_and_second_is_only_invalidated_context(
        kind, close, op, high, low) -> None:
    candles = candles_with_swings()
    original = next(z for z in historical_levels_v3(candles[:78], "1h", Decimal(100))["levels"]
                    if z["kind"] == kind)
    for index in (78, 79):
        candles[index].update(open=op, high=high, low=low, close=close)
    once = historical_levels_v3(candles[:79], "1h", Decimal(close))
    zone = next(z for z in once["levels"] if z["id"] == original["id"])
    assert zone["consecutive_closes_beyond"] == 1
    assert zone["zone_state"] == "active"
    assert zone["break_evidence"] == [{"at": candles[78]["close_time"], "close": close,
                                       "low": original["low"], "high": original["high"],
                                       "revision": original["revision"]}]
    twice = historical_levels_v3(candles, "1h", Decimal(close))
    assert all(z["id"] != original["id"] for z in twice["levels"])
    failed = next(z for z in twice["recently_invalidated_levels"] if z["id"] == original["id"])
    assert failed["zone_state"] == "invalidated"
    assert failed["invalidated_at"] == candles[79]["close_time"]
    assert failed["consecutive_closes_beyond"] == len(failed["break_evidence"]) == 2
    assert failed["kind"] == kind and failed["role_reversal_confirmed"] is False


def test_close_returning_inside_resets_break_evidence_without_losing_zone() -> None:
    candles = candles_with_swings()
    candles[-2].update(open="100", high="107", low="99", close="106")
    candles[-1].update(open="106", high="107", low="104", close="105")
    result = historical_levels_v3(candles, "1h", Decimal(105))
    zone = next(z for z in result["levels"] if z["kind"] == "resistance")
    assert zone["price_relation"] == zone["last_closed_relation"] == "inside"
    assert zone["consecutive_closes_beyond"] == 0
    assert zone["break_evidence"] == []


def test_v3_rejects_missing_candles_and_bad_tick() -> None:
    candles = candles_with_swings()
    with pytest.raises(ValueError, match="gap"):
        historical_levels_v3(candles[:30] + candles[31:], "1h", Decimal(100))
    with pytest.raises(ValueError, match="Tick size"):
        historical_levels_v3(candles, "1h", Decimal(100), Decimal(0))


def _zone(result: dict, kind: str) -> dict:
    return next(zone for zone in result["levels"] if zone["kind"] == kind and zone["pivot_count"] == 2)


def test_untouched_equal_pivots_mark_untaken_liquidity_beyond_the_zone() -> None:
    result = historical_levels_v3(candles_with_swings(), "1h", Decimal(100))
    support, resistance = _zone(result, "support"), _zone(result, "resistance")
    assert support["liquidity_beyond"] == {
        "side": "below", "boundary": support["low"], "basis": "equal_pivots", "status": "untaken",
        "taken_at": None, "label": "inferred_from_price_structure_not_order_data"}
    assert resistance["liquidity_beyond"]["side"] == "above"
    assert resistance["liquidity_beyond"]["boundary"] == resistance["high"]
    assert support["sweep_count"] == 0 and support["sweep_evidence"] == []
    assert result["context_version"] == "v3_price_location_context_v3"


@pytest.mark.parametrize("kind,update,extreme", [
    ("support", {"low": "93", "volume": "300"}, "93"),
    ("resistance", {"high": "107", "volume": "300"}, "107"),
])
def test_wick_beyond_the_outer_edge_that_closes_back_is_a_sweep(kind, update, extreme) -> None:
    candles = candles_with_swings()
    candles[60].update(update)
    zone = _zone(historical_levels_v3(candles, "1h", Decimal(100)), kind)
    assert zone["zone_state"] == "active" and zone["consecutive_closes_beyond"] == 0
    assert zone["sweep_count"] == 1
    sweep = zone["sweep_evidence"][0]
    assert (sweep["type"], sweep["extreme"], sweep["close"], sweep["at"]) == (
        "wick", extreme, "100", candles[60]["close_time"])
    assert Decimal(sweep["depth_atr"]) > 0
    assert sweep["relative_volume"] == "3.00"
    assert zone["liquidity_beyond"]["status"] == "swept"
    assert zone["liquidity_beyond"]["taken_at"] == candles[60]["close_time"]


def test_one_close_beyond_then_a_reclaim_is_a_sweep_and_two_closes_are_not() -> None:
    candles = candles_with_swings()
    candles[60].update(open="100", high="100", low="93", close="94")
    candles[61].update(open="94", high="100", low="93.5", close="100")
    zone = _zone(historical_levels_v3(candles, "1h", Decimal(100)), "support")
    assert zone["zone_state"] == "active"
    sweep = zone["sweep_evidence"][-1]
    assert (sweep["type"], sweep["extreme"], sweep["at"]) == (
        "reclaimed_after_one_close", "93", candles[61]["close_time"])
    assert zone["liquidity_beyond"]["status"] == "swept"

    held = historical_levels_v3(candles[:61], "1h", Decimal(94))
    zone = next(z for z in held["levels"] if z["id"] == _zone(
        historical_levels_v3(candles_with_swings(), "1h", Decimal(100)), "support")["id"])
    assert zone["sweep_count"] == 0
    assert zone["liquidity_beyond"]["status"] == "closed_beyond"


def test_forming_candle_sweep_needs_the_quote_back_on_the_near_side() -> None:
    from trade_helper.support_levels_v3 import annotate_forming_sweep

    levels = historical_levels_v3(candles_with_swings(), "1h", Decimal(100))["levels"]
    support = next(level for level in levels if level["kind"] == "support")
    below = str(Decimal(support["low"]) - 1)
    forming = {"open": "100", "high": "101", "low": below, "close": "100"}
    back = annotate_forming_sweep(levels, forming, Decimal(100))
    still_beyond = annotate_forming_sweep(levels, forming, Decimal(below))
    unknown = annotate_forming_sweep(levels, None, Decimal(100))
    assert next(z for z in back if z["id"] == support["id"])["forming_candle_swept"] is True
    assert next(z for z in still_beyond if z["id"] == support["id"])["forming_candle_swept"] is False
    assert next(z for z in unknown if z["id"] == support["id"])["forming_candle_swept"] is None
