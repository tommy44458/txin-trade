"""Keep the checkable parts of the extreme-reversal map a strategy skill asks for.

With the extreme reversal strategy the model marks, ahead of time, the extreme
zones worth ambushing over the next hours and days. This module never invents a
zone or a price. It drops zones on the wrong side of the snapshot quote, off the
tick grid, too far away or without a stated basis, and adds the arithmetic the
model is told not to write: each zone's distance from the quote and the R multiple
of each target, measured from the middle of the zone.

The grade also has to agree with the entry plan: an order needs grade A or B and
an immediate entry needs grade A. A plan that contradicts the grade is replaced by
standing aside rather than shown as advice.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .exit_plan import plan_reach

VERSION = "reversal_map_v1"
STRATEGY = "extreme_reversal"
GRADES = ("A", "B", "C")
WINDOWS = ("0_6h", "6_24h", "1_3d")
SIDES = ("long", "short")
TIERS = ("first", "second")
CONFIDENCE = ("high", "medium", "low")
STATUSES = ("candidate", "triggered", "invalidated")
MAX_ZONES_PER_SLOT = 2
MAX_TARGETS = 2
TEXT_LIMIT = 400
LONG_TEXT_LIMIT = 800
HUNDREDTH = Decimal("0.01")


def _price(value: object, tick: Decimal) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        price = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0 or price % tick != 0:
        return None
    return price


def _text(value: object, limit: int = TEXT_LIMIT) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _pct(value: Decimal, reference: Decimal) -> str:
    return str((value / reference * 100).quantize(HUNDREDTH, ROUND_HALF_UP))


def _zone(item: object, quote: Decimal, tick: Decimal, reach: Decimal) -> dict | None:
    if not isinstance(item, dict):
        return None
    side, window, tier = item.get("side"), item.get("window"), item.get("tier")
    if side not in SIDES or window not in WINDOWS or tier not in TIERS:
        return None
    low, high = _price(item.get("low"), tick), _price(item.get("high"), tick)
    basis = _text(item.get("basis"))
    if low is None or high is None or basis is None:
        return None
    low, high = min(low, high), max(low, high)
    status = item.get("status") if item.get("status") in STATUSES else "candidate"
    # A long zone sits at or below the quote and a short zone at or above it; a zone
    # price has already broken through can only be reported as invalidated.
    wrong_side = low > quote if side == "long" else high < quote
    if wrong_side and status != "invalidated":
        return None
    gap = max(quote - high, 0) if side == "long" else max(low - quote, 0)
    if gap > reach or high - low > reach:
        return None
    middle = ((low + high) / 2 / tick).to_integral_value(ROUND_HALF_UP) * tick
    stop = _price(item.get("stop_loss"), tick)
    if stop is not None and (not (stop < low if side == "long" else stop > high)
                             or abs(middle - stop) > reach):
        stop = None
    targets = []
    for value in item.get("targets") if isinstance(item.get("targets"), list) else []:
        target = _price(value, tick)
        if (target is None or target in targets or abs(target - middle) > reach
                or not (target > high if side == "long" else target < low)):
            continue
        targets.append(target)
    targets = sorted(targets, key=lambda price: abs(price - middle))[:MAX_TARGETS]
    risk = abs(middle - stop) if stop is not None else None
    return {
        "side": side, "window": window, "tier": tier, "low": str(low), "high": str(high),
        "basis": basis, "confidence": item.get("confidence") if item.get("confidence") in CONFIDENCE else None,
        "confirmation": _text(item.get("confirmation")), "status": status,
        "stop_loss": str(stop) if stop is not None else None,
        "targets": [{"price": str(target),
                     "r_multiple": str((abs(target - middle) / risk).quantize(HUNDREDTH, ROUND_HALF_UP))
                     if risk else None} for target in targets],
        "reference_entry": str(middle),
        "distance_pct": _pct(-gap if side == "long" else gap, quote),
        "quote_in_zone": low <= quote <= high,
    }


def read_reversal_map(value: object, quote: dict, atr: object) -> dict | None:
    """The map's checkable parts, or None when the model gave neither a grade nor a usable zone."""
    if not isinstance(value, dict):
        return None
    try:
        price = Decimal(str(quote["price"]))
        tick = Decimal(str(quote["tick_size"]))
        volatility = Decimal(str(atr))
    except (InvalidOperation, KeyError, TypeError):
        return None
    if not all(number.is_finite() and number > 0 for number in (price, tick, volatility)):
        return None
    # Zones for the next three days may sit further away than an entry plan's stop.
    reach = plan_reach(price, volatility, "high")
    supplied = value.get("zones") if isinstance(value.get("zones"), list) else []
    zones, slots = [], {}
    for item in supplied:
        zone = _zone(item, price, tick, reach)
        if zone is None:
            continue
        slot = slots.setdefault((zone["side"], zone["window"]), set())
        if zone["tier"] in slot or len(slot) >= MAX_ZONES_PER_SLOT:
            continue
        slot.add(zone["tier"])
        zones.append(zone)
    zones.sort(key=lambda zone: (WINDOWS.index(zone["window"]), SIDES.index(zone["side"]),
                                 TIERS.index(zone["tier"])))
    grade = value.get("grade") if value.get("grade") in GRADES else None
    if grade is None and not zones:
        return None
    paths = value.get("paths") if isinstance(value.get("paths"), dict) else {}
    return {
        "version": VERSION, "grade": grade,
        "grade_reason": _text(value.get("grade_reason"), LONG_TEXT_LIMIT),
        "zones": zones, "dropped_zones": len(supplied) - len(zones),
        "paths": {key: _text(paths.get(key), LONG_TEXT_LIMIT) for key in ("up_first", "down_first")},
        "cancel_condition": _text(value.get("cancel_condition"), LONG_TEXT_LIMIT),
        "data_limits": _text(value.get("data_limits"), LONG_TEXT_LIMIT),
        "reference_price": str(price),
    }


def _stand_aside(reason: str) -> dict:
    return {"action": "stand_aside", "side": None, "entry_price": None, "stop_loss": None,
            "take_profit": None, "trigger": None, "trigger_rule": None, "invalidation": None,
            "invalidation_rule": None, "reason": reason, "basis_level_ids": []}


def apply_strategy(reasoning: dict, request: dict, quote: dict, atr: object) -> dict:
    """Attach the checked map for the extreme reversal strategy; other analyses carry none."""
    reasoning = dict(reasoning)
    supplied = reasoning.pop("reversal_map", None)
    if request.get("trading_strategy") != STRATEGY:
        return reasoning
    reversal_map = read_reversal_map(supplied, quote, atr)
    reasoning["reversal_map"] = reversal_map
    plan = reasoning.get("entry_decision")
    action = plan.get("action") if isinstance(plan, dict) else None
    grade = reversal_map["grade"] if reversal_map else None
    if grade is None or action not in {"open_now", "wait_for_entry"}:
        return reasoning
    if grade == "C" or (grade == "B" and action == "open_now"):
        # An order the grade does not support is not shown as advice.
        reversal_map["plan_adjusted"] = "grade_c_plan" if grade == "C" else "open_now_needs_grade_a"
        reasoning["entry_decision"] = _stand_aside(
            reversal_map["grade_reason"] or plan.get("reason") or "")
        reasoning["agent_stance"] = "wait"
    return reasoning
