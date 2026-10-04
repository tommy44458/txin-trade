"""Reconcile finished reports with the candles that followed them.

Each report yields items to judge: a market report's entry plan (or its decision
to stand aside), and each position's hold or close decision. Items are judged on
closed 5-minute candles, a batch at a time, so a long-running trade only ever
fetches the candles it has not seen. The rules are deliberately conservative:
a candle that touches both the stop and the target is a loss, a stop is filled
at its price, and fees and slippage are ignored (the screen says so). Reports
are never changed; results live in analysis_outcomes under RULES_VERSION.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from .timeframes import advance_candle, candle_open

RULES_VERSION = "outcome_rules_v1"
# A waiting entry starts only as its trigger_rule says; without one it cannot be judged.
TRIGGERS = {"touch", "close_above", "close_below"}
RESOLUTION = "5m"
STEP = timedelta(minutes=5)
# Calendar windows: short-timeframe calls are often held for days.
MAX_TRACK = timedelta(days=30)
WAIT_CANDLES, WAIT_MIN = 12, timedelta(days=1)
OBSERVE_CANDLES, OBSERVE_MIN = 24, timedelta(days=7)


def _price(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _window(start: datetime, timeframe: str, candles: int, minimum: timedelta) -> datetime:
    by_candles = advance_candle(candle_open(start, timeframe), timeframe, candles + 1)
    return max(by_candles, start + minimum)


def first_candle(at: datetime) -> datetime:
    """The first whole 5-minute candle after `at`; prices before the report never count."""
    opened = candle_open(at, RESOLUTION)
    return opened if opened == at else opened + STEP


def items_for(report: dict) -> list[dict]:
    """What a report committed to, in terms the candles can settle."""
    quote = report.get("quote") or {}
    observed = quote.get("observed_at")
    timeframe = report.get("timeframe")
    if not observed or not timeframe:
        return []
    start = _at(observed)
    base = {"market_id": report.get("market_id"), "timeframe": timeframe, "start": start.isoformat()}
    items = []
    plan = report.get("entry_decision")
    if isinstance(plan, dict) and plan.get("action") in {"open_now", "wait_for_entry"}:
        entry, stop, target = (_price(plan.get(key)) for key in ("entry_price", "stop_loss", "take_profit"))
        side = plan.get("side")
        if side in {"long", "short"} and entry and stop and target and stop != entry:
            waiting = plan["action"] == "wait_for_entry"
            rule = plan.get("trigger_rule") if waiting else None
            trigger = ({"type": rule["type"], "price": str(_price(rule.get("price")))}
                       if isinstance(rule, dict) and rule.get("type") in TRIGGERS and _price(rule.get("price"))
                       else None)
            items.append(base | {
                "key": "entry", "kind": "entry", "action": plan["action"], "side": side,
                "entry": str(entry), "stop": str(stop), "target": str(target), "trigger": trigger,
                # Older plans described their condition only in words: no price rule to check.
                "unverifiable": waiting and trigger is None,
                "wait_until": _window(start, timeframe, WAIT_CANDLES, WAIT_MIN).isoformat() if waiting else None,
                "deadline": (start + MAX_TRACK).isoformat()})
    elif isinstance(plan, dict) and plan.get("action") == "stand_aside" and _price(quote.get("price")):
        items.append(base | {
            "key": "entry", "kind": "observe", "action": "stand_aside", "reference": str(_price(quote["price"])),
            "deadline": _window(start, timeframe, OBSERVE_CANDLES, OBSERVE_MIN).isoformat()})
    reference = _price(quote.get("mark_price")) or _price(quote.get("price"))
    for review in report.get("position_reviews") or []:
        decision = review.get("agent_decision")
        side = review.get("side")
        if not isinstance(decision, dict) or side not in {"long", "short"} or not reference:
            continue
        key = f"position:{review.get('position_id')}"
        if decision.get("decision") == "close_now":
            items.append(base | {
                "key": key, "kind": "close", "side": side, "reference": str(reference),
                "deadline": _window(start, timeframe, OBSERVE_CANDLES, OBSERVE_MIN).isoformat()})
            continue
        plan = decision.get("exit_plan")
        if decision.get("decision") != "hold" or not isinstance(plan, dict):
            continue
        invalidation = plan.get("invalidation") or {}
        exit_price = _price(invalidation.get("price"))
        if not exit_price or exit_price == reference:
            continue
        targets = [_price(item.get("price")) for item in plan.get("take_profits") or [] if isinstance(item, dict)]
        targets = [price for price in targets if price]
        items.append(base | {
            "key": key, "kind": "hold", "side": side, "reference": str(reference),
            "invalidation": str(exit_price), "confirmation": invalidation.get("confirmation", "touch"),
            "target": str(min(targets, key=lambda price: abs(price - reference))) if targets else None,
            "deadline": (start + MAX_TRACK).isoformat()})
    return items


def new_state(item: dict) -> dict:
    entered = item["kind"] == "entry" and item["action"] == "open_now"
    return {"through": first_candle(_at(item["start"])).isoformat(), "done": False,
            "entered_at": item["start"] if entered else None,
            "high": None, "low": None, "last_close": None}


def _r(item: dict, exit_price: Decimal, fill: str | None = None) -> str:
    """Gain in units of the distance to the stop (or, for a hold, to its invalidation)."""
    if item["kind"] == "entry":
        start, risk_at = Decimal(fill or item["entry"]), Decimal(item["stop"])
    else:
        start, risk_at = Decimal(item["reference"]), Decimal(item["invalidation"])
    risk = abs(start - risk_at)
    gain = (exit_price - start) if item["side"] == "long" else (start - exit_price)
    return str((gain / risk).quantize(Decimal("0.01")))


def _pct(value: Decimal, reference: Decimal) -> str:
    return str(((value - reference) / reference * 100).quantize(Decimal("0.01")))


def _finish(state: dict, result: str, at: str, **details) -> dict:
    return state | {"done": True, "result": result, "resolved_at": at, **details}


def _closes_primary(candle: dict, timeframe: str) -> bool:
    """True when this 5-minute candle is the last one of a primary-timeframe candle."""
    ends = _at(candle["open_time"]) + STEP
    return candle_open(ends, timeframe) == ends


def step(item: dict, state: dict, candles: list[dict]) -> dict:
    """Advance an item over closed 5-minute candles that follow `state['through']`."""
    state = dict(state)
    for candle in candles:
        if state["done"]:
            break
        opened = _at(candle["open_time"])
        if opened < _at(state["through"]):
            continue
        high, low, close = Decimal(candle["high"]), Decimal(candle["low"]), Decimal(candle["close"])
        state["through"] = (opened + STEP).isoformat()
        if item["kind"] == "entry":
            state = _entry_candle(item, state, opened, high, low, close)
            if (state["entered_at"] is None and not state["done"]
                    and (item.get("trigger") or {}).get("type") in {"close_above", "close_below"}):
                state = _close_trigger(item, state, candle, close)
        elif item["kind"] == "hold":
            state = _hold_candle(item, state, candle, high, low, close)
        else:
            state = _extremes(state, high, low, close)
    return state


def _extremes(state: dict, high: Decimal, low: Decimal, close: Decimal) -> dict:
    return state | {"high": str(max(high, Decimal(state["high"]))) if state["high"] else str(high),
                    "low": str(min(low, Decimal(state["low"]))) if state["low"] else str(low),
                    "last_close": str(close)}


def _close_trigger(item, state, candle, close):
    """A close-confirmed entry fills at the close of the primary candle that confirms it."""
    rule = item["trigger"]
    level = Decimal(rule["price"])
    if not _closes_primary(candle, item["timeframe"]):
        return state
    if not (close > level if rule["type"] == "close_above" else close < level):
        return state
    stop, target = Decimal(item["stop"]), Decimal(item["target"])
    long = item["side"] == "long"
    # A close already past the stop or the target leaves no trade to take.
    if (close <= stop or close >= target) if long else (close >= stop or close <= target):
        return _finish(state, "not_triggered", (_at(candle["open_time"]) + STEP).isoformat())
    return state | {"entered_at": (_at(candle["open_time"]) + STEP).isoformat(), "fill": str(close),
                    "high": None, "low": None}


def _entry_candle(item, state, opened, high, low, close):
    entry, stop, target = Decimal(item["entry"]), Decimal(item["stop"]), Decimal(item["target"])
    long = item["side"] == "long"
    at = (opened + STEP).isoformat()
    fill = state.get("fill")
    if state["entered_at"] is None:
        if item["wait_until"] and opened >= _at(item["wait_until"]):
            return _finish(state, "not_triggered", item["wait_until"])
        if (item.get("trigger") or {"type": "touch"})["type"] != "touch":
            return state  # Close-confirmed entries start in _close_trigger, after this candle.
        if not low <= entry <= high:
            return state
        state = state | {"entered_at": opened.isoformat()}
        # On the entry candle the order of prices is unknown: its stop counts, its target does not.
        if (low <= stop) if long else (high >= stop):
            return _finish(_extremes(state, high, low, close), "loss", at, exit_price=str(stop),
                           r_multiple=_r(item, stop))
        return _extremes(state, high, low, close)
    if opened < _at(state["entered_at"]):
        return state  # The confirming candle itself: the trade starts at its close.
    state = _extremes(state, high, low, close)
    stopped = (low <= stop) if long else (high >= stop)
    reached = (high >= target) if long else (low <= target)
    if stopped:  # Also when both were touched in this candle.
        return _finish(state, "loss", at, exit_price=str(stop), r_multiple=_r(item, stop, fill))
    if reached:
        return _finish(state, "win", at, exit_price=str(target), r_multiple=_r(item, target, fill))
    return state


def _hold_candle(item, state, candle, high, low, close):
    invalidation = Decimal(item["invalidation"])
    target = Decimal(item["target"]) if item["target"] else None
    long = item["side"] == "long"
    at = (_at(candle["open_time"]) + STEP).isoformat()
    state = _extremes(state, high, low, close)
    if item["confirmation"] == "close":
        broken = _closes_primary(candle, item["timeframe"]) and (
            close < invalidation if long else close > invalidation)
        exit_price = close
    else:
        broken = (low <= invalidation) if long else (high >= invalidation)
        exit_price = invalidation
    reached = target is not None and ((high >= target) if long else (low <= target))
    if broken:  # Also when the target was touched in the same candle.
        return _finish(state, "invalidated", at, exit_price=str(exit_price), r_multiple=_r(item, exit_price))
    if reached:
        return _finish(state, "target_hit", at, exit_price=str(target), r_multiple=_r(item, target))
    return state


def settle(item: dict, state: dict) -> dict:
    """Close an item whose window has passed, once its candles reach the deadline."""
    if state["done"] or _at(state["through"]) < due(item, state):
        return state
    if item["kind"] == "entry":
        if state["entered_at"] is None:
            return _finish(state, "not_triggered", (item["wait_until"] or item["deadline"]))
        last = Decimal(state["last_close"] or state.get("fill") or item["entry"])
        return _finish(state, "expired", item["deadline"], exit_price=str(last),
                       r_multiple=_r(item, last, state.get("fill")))
    if item["kind"] == "hold":
        last = Decimal(state["last_close"]) if state["last_close"] else Decimal(item["reference"])
        return _finish(state, "expired", item["deadline"], exit_price=str(last), r_multiple=_r(item, last))
    if not state["high"]:
        return state
    reference = Decimal(item["reference"])
    up, down = _pct(Decimal(state["high"]), reference), _pct(Decimal(state["low"]), reference)
    end = _pct(Decimal(state["last_close"]), reference)
    if item["kind"] == "observe":
        return _finish(state, "observed", item["deadline"], max_up_pct=up, max_down_pct=down, end_pct=end)
    # After closing a long, a lower price means closing was right.
    against = Decimal(end) < 0 if item["side"] == "long" else Decimal(end) > 0
    return _finish(state, "good_close" if against else "early_close", item["deadline"],
                   max_up_pct=up, max_down_pct=down, end_pct=end)


def due(item: dict, state: dict) -> datetime:
    """The last moment this item still needs candles for."""
    if item["kind"] == "entry" and state["entered_at"] is None and item["wait_until"]:
        return min(_at(item["wait_until"]), _at(item["deadline"]))
    return _at(item["deadline"])
