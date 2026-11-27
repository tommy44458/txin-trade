"""Chronological, auditable candidate zones for active analysis.

No level in this module is a reversal probability or an execution signal.
"""

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from hashlib import sha256
from statistics import median

from .timeframes import TIMEFRAME_LADDER, advance_candle

VERSION = "confirmed_pivot_lifecycle_v3"
CONTEXT_VERSION = "v3_price_location_context_v3"
PIVOT_WIDTH = 2
CLUSTER_ATR = Decimal("0.35")
PADDING_ATR = Decimal("0.15")
REARM_ATR = Decimal("0.50")
SWEEP_EVIDENCE_LIMIT = 3
VOLUME_BASELINE = 20


def _round_down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _round_up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _atr_at_each_close(candles: list[dict], period: int = 14) -> list[Decimal | None]:
    results: list[Decimal | None] = [None] * len(candles)
    previous_close = Decimal(candles[0]["close"])
    ranges = []
    for index in range(1, len(candles)):
        high, low, close = (Decimal(candles[index][field]) for field in ("high", "low", "close"))
        true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        ranges.append(true_range)
        previous_close = close
        if index == period:
            results[index] = sum(ranges) / period
        elif index > period:
            results[index] = (results[index - 1] * (period - 1) + true_range) / period
    return results


def _validate(candles: list[dict], timeframe: str, tick_size: Decimal) -> None:
    if timeframe not in TIMEFRAME_LADDER or len(candles) < 60:
        raise ValueError("V3 requires at least 60 closed candles in a supported timeframe")
    if not tick_size.is_finite() or tick_size <= 0:
        raise ValueError("Tick size must be positive")
    previous_open = None
    for candle in candles:
        opened = datetime.fromisoformat(candle["open_time"])
        closed = datetime.fromisoformat(candle["close_time"])
        if previous_open is not None and opened != advance_candle(previous_open, timeframe):
            raise ValueError("V3 candle series has a gap or duplicate")
        if closed <= opened:
            raise ValueError("V3 candle close precedes open")
        values = [Decimal(candle[field]) for field in ("open", "high", "low", "close")]
        if any(not value.is_finite() for value in values):
            raise ValueError("V3 candle has nonfinite OHLC")
        op, high, low, close = values
        if low <= 0 or high < max(op, close, low) or low > min(op, close):
            raise ValueError("V3 candle has invalid OHLC")
        previous_open = opened


def _approach_excursion(kind: str, close: Decimal, zone: dict) -> bool:
    distance = REARM_ATR * zone["formation_atr"]
    return (close > zone["high"] + distance if kind == "support"
            else close < zone["low"] - distance)


def _far_close(kind: str, close: Decimal, zone: dict) -> bool:
    return close < zone["low"] if kind == "support" else close > zone["high"]


def _pivot_at(candles: list[dict], confirmed_index: int, kind: str) -> Decimal | None:
    pivot_index = confirmed_index - PIVOT_WIDTH
    if pivot_index < PIVOT_WIDTH:
        return None
    field = "low" if kind == "support" else "high"
    prices = [Decimal(candle[field]) for candle in candles[pivot_index - PIVOT_WIDTH:
                                                           confirmed_index + 1]]
    pivot = prices[PIVOT_WIDTH]
    if kind == "support":
        return pivot if pivot < min(prices[:PIVOT_WIDTH]) and pivot <= min(prices[-PIVOT_WIDTH:]) else None
    return pivot if pivot > max(prices[:PIVOT_WIDTH]) and pivot >= max(prices[-PIVOT_WIDTH:]) else None


def _new_zone(price: Decimal, kind: str, confirmed_index: int, candles: list[dict],
              atr: Decimal, tick_size: Decimal, timeframe: str, market_id: str) -> dict:
    confirmed_at = candles[confirmed_index]["close_time"]
    key = f"{VERSION}|{market_id}|{timeframe}|{kind}|{confirmed_at}|{price}"
    padding = atr * PADDING_ATR
    low, high = _round_down(price - padding, tick_size), _round_up(price + padding, tick_size)
    return {"id": "zone_" + sha256(key.encode()).hexdigest()[:16], "kind": kind,
            "low": low, "high": high, "prices": [price], "formation_atr": atr,
            "created_at": confirmed_at, "confirmed_at": confirmed_at,
            "last_pivot_index": confirmed_index - PIVOT_WIDTH,
            "revision": 1, "pivot_count": 1, "touches": [], "armed": False,
            "break_streak": 0, "break_evidence": [], "invalidated_at": None,
            "sweeps": [], "taken": None}


def _merge(zone: dict, price: Decimal, confirmed_index: int, candles: list[dict],
           tick_size: Decimal) -> None:
    padding = zone["formation_atr"] * PADDING_ATR
    zone["low"] = min(zone["low"], _round_down(price - padding, tick_size))
    zone["high"] = max(zone["high"], _round_up(price + padding, tick_size))
    zone["prices"].append(price)
    zone["confirmed_at"] = candles[confirmed_index]["close_time"]
    zone["last_pivot_index"] = confirmed_index - PIVOT_WIDTH
    zone["pivot_count"] += 1
    zone["revision"] += 1


def _boundary(zone: dict) -> Decimal:
    """The outer edge: stops of the side defending the zone sit beyond it."""
    return zone["low"] if zone["kind"] == "support" else zone["high"]


def _beyond(zone: dict, price: Decimal) -> bool:
    return price < zone["low"] if zone["kind"] == "support" else price > zone["high"]


def _relative_volume(volumes: list[Decimal], index: int) -> str | None:
    baseline = volumes[max(0, index - VOLUME_BASELINE):index]
    if len(baseline) < 5 or median(baseline) <= 0:
        return None
    return str((volumes[index] / median(baseline)).quantize(Decimal("0.01")))


def _sweep(zone: dict, candles: list[dict], index: int, volumes: list[Decimal],
           previous_streak: int) -> dict | None:
    """A trade beyond the outer edge that closed back: a wick, or one close beyond then a reclaim."""
    candle = candles[index]
    close = Decimal(candle["close"])
    if _far_close(zone["kind"], close, zone):
        return None
    field, pick = ("low", min) if zone["kind"] == "support" else ("high", max)
    if previous_streak == 1:
        kind = "reclaimed_after_one_close"
        extreme = pick(Decimal(candles[index - 1][field]), Decimal(candle[field]))
    elif _beyond(zone, Decimal(candle[field])):
        kind = "wick"
        extreme = Decimal(candle[field])
    else:
        return None
    depth = abs(_boundary(zone) - extreme) / zone["formation_atr"]
    return {"at": candle["close_time"], "type": kind, "extreme": str(extreme), "close": str(close),
            "depth_atr": str(depth.quantize(Decimal("0.01"))),
            "relative_volume": _relative_volume(volumes, index), "revision": zone["revision"]}


def _liquidity(zone: dict) -> dict:
    """Where the zone's stops are presumed to rest, and whether price has taken them since the last pivot."""
    taken = zone["taken"]
    fresh = taken is None or taken["index"] <= zone["last_pivot_index"]
    return {"side": "below" if zone["kind"] == "support" else "above",
            "boundary": str(_boundary(zone)),
            "basis": "equal_pivots" if zone["pivot_count"] >= 2 else "single_pivot",
            "status": "untaken" if fresh else taken["kind"],
            "taken_at": None if fresh else taken["at"],
            "label": "inferred_from_price_structure_not_order_data"}


def annotate_forming_sweep(levels: list[dict], forming: dict | None, reference: Decimal) -> list[dict]:
    """Mark zones the unclosed candle has traded beyond while the quote is back on the near side.

    Intrabar only: the candle can still close beyond, so this never changes the zone's lifecycle.
    """
    result = []
    for level in levels:
        swept = None
        if isinstance(forming, dict) and level.get("zone_state") == "active":
            low, high = Decimal(level["low"]), Decimal(level["high"])
            if level["kind"] == "support":
                swept = Decimal(forming["low"]) < low and reference >= low
            else:
                swept = Decimal(forming["high"]) > high and reference <= high
        result.append({**level, "forming_candle_swept": swept})
    return result


def _relation(price: Decimal, zone: dict) -> str:
    return "below" if price < zone["low"] else "above" if price > zone["high"] else "inside"


def _edge_distance(zone: dict, reference: Decimal) -> Decimal:
    return max(zone["low"] - reference, reference - zone["high"], Decimal(0))


def _public_zone(zone: dict, timeframe: str, as_of: str, reference: Decimal,
                 last_close: Decimal) -> dict:
    age = datetime.fromisoformat(as_of) - datetime.fromisoformat(zone["confirmed_at"])
    if timeframe == "1M":
        last = datetime.fromisoformat(as_of)
        confirmed = datetime.fromisoformat(zone["confirmed_at"])
        age_bars = max(0, (last.year - confirmed.year) * 12 + last.month - confirmed.month)
    else:
        from .timeframes import FIXED_SECONDS

        age_bars = max(0, int(age.total_seconds() // FIXED_SECONDS[timeframe]))
    score = Decimal(min(len(zone["touches"]), 4) * 2 + min(zone["pivot_count"], 3))
    score += Decimal(1) / (1 + Decimal(age_bars) / 30)
    relation = _relation(reference, zone)
    far_side = "below" if zone["kind"] == "support" else "above"
    test_state = ("invalidated" if zone["invalidated_at"] else
                  "first_close_beyond" if zone["break_streak"] == 1 else
                  "inside" if relation == "inside" else
                  "intrabar_crossed" if relation == far_side else "not_testing")
    return {"id": zone["id"], "kind": zone["kind"],
            "low": str(zone["low"]), "high": str(zone["high"]),
            "center": str(median(zone["prices"])), "timeframe": timeframe,
            "method": VERSION, "algorithm_version": VERSION,
            "created_at": zone["created_at"], "confirmed_at": zone["confirmed_at"],
            "as_of": as_of, "source_time": as_of, "revision": zone["revision"],
            "zone_state": "invalidated" if zone["invalidated_at"] else "active",
            "invalidated_at": zone["invalidated_at"],
            "price_relation": relation, "price_test_state": test_state,
            "last_closed_relation": _relation(last_close, zone),
            "consecutive_closes_beyond": zone["break_streak"],
            "break_evidence": zone["break_evidence"],
            "role_reversal_confirmed": False,
            "pivot_count": zone["pivot_count"],
            "independent_touch_count": len(zone["touches"]),
            "last_touch_at": zone["touches"][-1]["at"] if zone["touches"] else None,
            "touch_evidence": zone["touches"],
            "sweep_count": len(zone["sweeps"]),
            "last_sweep_at": zone["sweeps"][-1]["at"] if zone["sweeps"] else None,
            "sweep_evidence": zone["sweeps"][-SWEEP_EVIDENCE_LIMIT:],
            "liquidity_beyond": _liquidity(zone),
            "formation_atr": str(zone["formation_atr"]), "age_bars": age_bars,
            "evidence_score": str(score.quantize(Decimal("0.01"))),
            "evidence_label": "ranking_heuristic_not_probability",
            "distance_from_reference": str(abs(Decimal(median(zone["prices"])) - reference))}


def historical_levels_v3(candles: list[dict], timeframe: str, reference_price: Decimal,
                         tick_size: Decimal = Decimal("0.01"),
                         market_id: str = "unspecified") -> dict:
    """Use only the supplied closed candles; each zone carries its formation-time ATR."""
    _validate(candles, timeframe, tick_size)
    if not reference_price.is_finite() or reference_price <= 0:
        raise ValueError("Reference price must be positive")
    atrs = _atr_at_each_close(candles)
    volumes = [Decimal(candle["volume"]) for candle in candles]
    zones: list[dict] = []
    for index, candle in enumerate(candles):
        high, low, close = (Decimal(candle[field]) for field in ("high", "low", "close"))
        for zone in zones:
            if zone["invalidated_at"] is not None or candle["close_time"] <= zone["confirmed_at"]:
                continue
            if zone["armed"] and low <= zone["high"] and high >= zone["low"]:
                zone["touches"].append({"at": candle["close_time"],
                                        "revision": zone["revision"],
                                        "low": str(zone["low"]), "high": str(zone["high"])})
                zone["armed"] = False
            elif not zone["armed"] and _approach_excursion(zone["kind"], close, zone):
                zone["armed"] = True
            previous_streak = zone["break_streak"]
            sweep = _sweep(zone, candles, index, volumes, previous_streak)
            if sweep is not None:
                zone["sweeps"].append(sweep)
                zone["taken"] = {"index": index, "at": candle["close_time"], "kind": "swept"}
            zone["break_streak"] = previous_streak + 1 if _far_close(
                zone["kind"], close, zone) else 0
            if zone["break_streak"] == 1:
                zone["taken"] = {"index": index, "at": candle["close_time"], "kind": "closed_beyond"}
            if zone["break_streak"]:
                zone["break_evidence"].append({"at": candle["close_time"], "close": str(close),
                                                "low": str(zone["low"]), "high": str(zone["high"]),
                                                "revision": zone["revision"]})
            else:
                zone["break_evidence"] = []
            if zone["break_streak"] >= 2:
                zone["invalidated_at"] = candle["close_time"]
                zone["armed"] = False
        atr = atrs[index]
        if atr is None or atr <= 0:
            continue
        for kind in ("support", "resistance"):
            price = _pivot_at(candles, index, kind)
            if price is None:
                continue
            matches = [zone for zone in zones if zone["kind"] == kind
                       and zone["invalidated_at"] is None
                       and abs(price - Decimal(median(zone["prices"]))) <=
                       CLUSTER_ATR * zone["formation_atr"]]
            if matches:
                target = min(matches, key=lambda zone: abs(price - Decimal(median(zone["prices"]))))
                _merge(target, price, index, candles, tick_size)
            else:
                target = _new_zone(price, kind, index, candles, atr, tick_size,
                                   timeframe, market_id)
                target["armed"] = _approach_excursion(kind, close, target)
                zones.append(target)
    as_of = candles[-1]["close_time"]
    current_atr = atrs[-1]
    last_close = Decimal(candles[-1]["close"])
    selected = []
    for kind in ("support", "resistance"):
        eligible = [zone for zone in zones if zone["kind"] == kind and zone["invalidated_at"] is None
                    and ((zone["high"] < reference_price if kind == "support"
                          else zone["low"] > reference_price)
                         or _edge_distance(zone, reference_price) <= 2 * current_atr)]
        published = [_public_zone(zone, timeframe, as_of, reference_price, last_close)
                     for zone in eligible]
        published.sort(key=lambda zone: (
            0 if zone["price_relation"] == "inside" else
            1 if zone["price_test_state"] in {"first_close_beyond", "intrabar_crossed"} else 2,
            -(Decimal(zone["evidence_score"]) / (1 +
              Decimal(zone["distance_from_reference"]) / current_atr)),
            Decimal(zone["distance_from_reference"])))
        # Do not hide a tested zone because the quote entered/crossed its bounds.
        # Keep all overlaps; the remaining per-side list is still bounded.
        tested = [zone for zone in published if zone["price_test_state"] != "not_testing"]
        others = [zone for zone in published if zone not in tested]
        selected.extend(tested + others[:3])
    recent_cutoff = advance_candle(datetime.fromisoformat(candles[-7]["open_time"]), timeframe) - timedelta(milliseconds=1)
    recently_invalidated = [zone for zone in zones if zone["invalidated_at"] is not None
                            and datetime.fromisoformat(zone["invalidated_at"]) >= recent_cutoff
                            and _edge_distance(zone, reference_price) <= 3 * current_atr]
    recently_invalidated.sort(key=lambda zone: (_edge_distance(zone, reference_price),
                                                -datetime.fromisoformat(zone["invalidated_at"]).timestamp()))
    return {"algorithm_version": VERSION, "market_id": market_id,
            "context_version": CONTEXT_VERSION,
            "as_of": as_of, "tick_size": str(tick_size),
            "levels": selected, "active_zone_count": sum(zone["invalidated_at"] is None for zone in zones),
            "invalidated_zone_count": sum(zone["invalidated_at"] is not None for zone in zones),
            "recently_invalidated_levels": [_public_zone(zone, timeframe, as_of, reference_price,
                                                         last_close) for zone in recently_invalidated[:4]],
            "invalidation_rule": "two_consecutive_closed_candles_beyond_original_boundary_not_retest_confirmation",
            "sweep_rule": "trade_beyond_outer_edge_then_close_back_wick_or_one_close_reclaimed",
            "quality_flags": ["closed_candles_only", "price_structure_only",
                              "not_a_reversal_probability", "historical_kind_not_current_price_side",
                              "quote_crossing_does_not_invalidate_zone"]}
