"""Validate an Agent-authored entry plan without selecting its trade direction."""

from decimal import Decimal, InvalidOperation

from .exit_plan import plan_reach
from .risk import costed_risk

ACTIONS = {"open_now", "wait_for_entry", "stand_aside"}


def _price(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Agent entry plan has invalid price")  # noqa: TRY004 - repairable model output
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Agent entry plan has invalid price") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("Agent entry plan has invalid price")
    return price


def _reason(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 1000


def validate_entry_decision(plan: dict, quote: dict, levels: list[dict], atr: str,
                            agent_stance: str, leverage: int,
                            risk_tolerance: str | None = None) -> dict | None:
    """Return cost reference for a coherent plan; this is not a strategy whitelist."""
    if (not isinstance(plan, dict) or not isinstance(plan.get("action"), str) or
            plan["action"] not in ACTIONS):
        raise ValueError("Agent entry decision is invalid")
    if not _reason(plan.get("reason")):
        raise ValueError("Agent entry decision lacks a clear reason")
    ids = plan.get("basis_level_ids")
    if (not isinstance(ids, list) or len(ids) > 3 or
            any(not isinstance(item, str) for item in ids) or
            len(ids) != len(set(ids)) or
            not set(ids).issubset({item["id"] for item in levels})):
        raise ValueError("Agent entry plan cites an unverified level")
    action = plan["action"]
    if action == "stand_aside":
        if (plan.get("side") is not None or
                any(plan.get(field) is not None for field in
                    ("entry_price", "stop_loss", "take_profit", "trigger", "invalidation")) or ids):
            raise ValueError("Stand-aside decision may not include an order plan")
        return None
    side = plan.get("side")
    if not isinstance(side, str) or side not in {"long", "short"} or side != agent_stance:
        raise ValueError("Agent entry side conflicts with strategy direction")
    if (not _reason(plan.get("trigger")) or
            not _reason(plan.get("invalidation"))):
        raise ValueError("Agent entry plan lacks trigger or invalidation")
    entry, stop, target = (_price(plan.get(name)) for name in
                           ("entry_price", "stop_loss", "take_profit"))
    quote_price = _price(str(quote["price"]))
    tick = _price(str(quote["tick_size"]))
    volatility = _price(str(atr))
    if any(price % tick != 0 for price in (entry, stop, target)):
        raise ValueError("Agent entry prices do not match market tick size")
    rule = plan.get("trigger_rule")
    if action == "wait_for_entry" and rule is not None and (
            not isinstance(rule, dict) or rule.get("type") not in {"touch", "close_above", "close_below"}
            or _price(rule.get("price")) % tick != 0):
        raise ValueError("Agent trigger rule is invalid")
    exit_rule = plan.get("invalidation_rule")
    if exit_rule is not None:
        exit_price = _price(exit_rule.get("price")) if isinstance(exit_rule, dict) else None
        if (exit_price is None or exit_rule.get("confirmation") not in {"close", "touch"}
                or exit_price % tick != 0
                or not (stop <= exit_price < entry if side == "long" else entry < exit_price <= stop)):
            raise ValueError("Agent invalidation rule is invalid")
    if action == "open_now" and entry != quote_price:
        raise ValueError("Immediate entry must use the analysis quote")
    if action == "wait_for_entry" and abs(entry - quote_price) > max(
            volatility * 4, quote_price * Decimal("0.05")):
        raise ValueError("Conditional entry is too far from the snapshot")
    if not (stop < entry < target if side == "long" else target < entry < stop):
        raise ValueError("Agent entry prices have the wrong direction")
    # A high risk tolerance asks for a wider stop and a farther target.
    max_distance = plan_reach(entry, volatility, risk_tolerance)
    if max(abs(entry - stop), abs(target - entry)) > max_distance:
        raise ValueError("Agent entry stop or target is too far from the snapshot")
    return costed_risk(side, entry, stop, target, leverage, quote)
