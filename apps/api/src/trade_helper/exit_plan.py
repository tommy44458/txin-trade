"""Keep the checkable parts of an Agent-authored exit plan for a held position.

A hold decision should say when the position stops being worth holding. The plan
is the Agent's; this module only drops parts that cannot be shown or watched
honestly (wrong side of the price, off the tick grid, too far away, unverified
zones). It never invents a price and never changes an exchange order.
"""

from decimal import Decimal, InvalidOperation

VERSION = "exit_plan_v1"
CONFIRMATIONS = {"close", "touch"}
MAX_TAKE_PROFITS = 3
MAX_CONDITION_LENGTH = 300


def _price(value: object, tick: Decimal) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        price = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0 or price % tick != 0:
        return None
    return price


def _beyond(side: str, price: Decimal, reference: Decimal, *, adverse: bool) -> bool:
    """True when `price` lies on the losing (adverse) or winning side of `reference`."""
    below = price < reference
    return below if (side == "long") == adverse else price > reference


def sanitize_exit_plan(plan: object, *, side: str, price: Decimal, tick: Decimal,
                       atr: Decimal, levels: list[dict]) -> dict | None:
    """Return the plan reduced to valid parts, or None without a usable invalidation."""
    if not isinstance(plan, dict) or side not in {"long", "short"}:
        return None
    reach = max(atr * 6, price * Decimal("0.1"))

    def usable(value: object, *, adverse: bool) -> Decimal | None:
        level = _price(value, tick)
        if level is None or abs(level - price) > reach:
            return None
        return level if _beyond(side, level, price, adverse=adverse) else None

    invalidation = plan.get("invalidation")
    if not isinstance(invalidation, dict):
        return None
    exit_price = usable(invalidation.get("price"), adverse=True)
    confirmation = invalidation.get("confirmation")
    if exit_price is None or confirmation not in CONFIRMATIONS:
        return None
    condition = invalidation.get("condition")
    condition = (condition.strip() if isinstance(condition, str) and condition.strip()
                 and len(condition) <= MAX_CONDITION_LENGTH else None)

    stop = usable(plan.get("protective_stop"), adverse=True)

    take_profits = []
    supplied = plan.get("take_profits")
    for item in supplied if isinstance(supplied, list) else []:
        if not isinstance(item, dict) or len(take_profits) == MAX_TAKE_PROFITS:
            continue
        target = usable(item.get("price"), adverse=False)
        portion = item.get("portion_pct")
        if isinstance(portion, bool) or not isinstance(portion, int) or not 1 <= portion <= 100:
            portion = None
        if target is not None:
            take_profits.append({"price": str(target), "portion_pct": portion})
    # Nearest target first, so the list reads in the order price would reach it.
    take_profits.sort(key=lambda item: abs(Decimal(item["price"]) - price))
    if sum(item["portion_pct"] or 0 for item in take_profits) > 100:
        take_profits = [item | {"portion_pct": None} for item in take_profits]

    known = {level["id"] for level in levels if isinstance(level.get("id"), str)}
    ids = plan.get("basis_level_ids")
    basis = ([item for item in dict.fromkeys(i for i in ids if isinstance(i, str)) if item in known][:3]
             if isinstance(ids, list) else [])

    return {
        "version": VERSION,
        "invalidation": {"price": str(exit_price), "confirmation": confirmation,
                         "condition": condition},
        "protective_stop": str(stop) if stop is not None else None,
        "take_profits": take_profits,
        "basis_level_ids": basis,
    }
