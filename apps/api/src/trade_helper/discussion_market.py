"""A small new public observation alongside a discussion's frozen analysis.

This is not another analysis: no catalog, account, indicator, zone, or macro
refresh occurs. The pair comes from the authorized saved discussion context,
never from the user's question. The worker calls this synchronous entry point
outside its SQLite transaction; both public reads share a hard async deadline.
"""

import asyncio
import math
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, InvalidOperation, localcontext

import httpx

from .discussion_snapshot import NOT_REFRESHED as SNAPSHOT_NOT_REFRESHED
from .discussion_snapshot import VERSION as SNAPSHOT_MODULE_VERSION
from .discussion_snapshot import build_discussion_snapshot
from .timeframes import ANALYSIS_TIMEFRAMES, FIXED_SECONDS, candle_open

VERSION = "discussion_live_market_v1"
# v2 adds a fresh snapshot: zones, indicators, derivatives, flows and liquidations.
SNAPSHOT_VERSION = "discussion_live_market_v2"
SNAPSHOT_MAX_SECONDS = 25.0
SOURCE = "binance_usdt_perpetual"
BASE_URL = "https://fapi.binance.com"
QUOTE_PATH = "/fapi/v2/ticker/price"
CANDLES_PATH = "/fapi/v1/klines"
MAX_TIMEOUT_SECONDS = 8.0
QUOTE_MAX_AGE_SECONDS = 60
QUOTE_FUTURE_TOLERANCE_SECONDS = 2
CANDLE_BOUNDARY_GRACE_SECONDS = 5
RECENT_CANDLE_LIMIT = 8
NOT_REFRESHED = ["support_resistance", "indicators", "higher_timeframes", "market_reference", "fund_flows",
                 "positions", "macro"]
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _InvalidData(Exception):
    def __init__(self, code: str):
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


def _frozen_pair(context: dict) -> tuple[str, str, str] | None:
    if not isinstance(context, dict):
        return None
    subject = context.get("subject")
    submitted = context.get("submitted_input", {})
    if not isinstance(subject, dict) or subject.get("type") != "analysis":
        return None
    submitted = submitted if isinstance(submitted, dict) else {}
    selected = {}
    for field in ("market_id", "timeframe"):
        first, second = subject.get(field), submitted.get(field)
        if first is not None and second is not None and first != second:
            return None
        selected[field] = first if first is not None else second
    market_id, timeframe = selected["market_id"], selected["timeframe"]
    if (not isinstance(market_id, str) or not market_id.startswith("binance:perp:") or
            not isinstance(timeframe, str) or timeframe not in ANALYSIS_TIMEFRAMES):
        return None
    symbol = market_id[len("binance:perp:"):]
    # Membership was verified when the original analysis was created. Looking
    # up the catalog here could start additional requests outside our deadline.
    if (not 2 <= len(symbol) <= 64 or not symbol.endswith("USDT") or
            not all(character.isalnum() or character == "_" for character in symbol)):
        return None
    return market_id, symbol, timeframe


def _decimal(value: object, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise _InvalidData("INVALID_RESPONSE")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise _InvalidData("INVALID_RESPONSE") from None
    if (not number.is_finite() or positive and number <= 0 or
            nonnegative and number < 0):
        raise _InvalidData("INVALID_RESPONSE")
    return number


def _timestamp(value: object) -> datetime:
    if type(value) is not int or not 0 <= value <= 253402300799999:
        raise _InvalidData("INVALID_TIMESTAMP")
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        raise _InvalidData("INVALID_TIMESTAMP") from None


def _quote(payload: object, symbol: str, observed: datetime) -> dict:
    if not isinstance(payload, dict):
        raise _InvalidData("INVALID_RESPONSE")
    if payload.get("symbol") != symbol:
        raise _InvalidData("SYMBOL_MISMATCH")
    price = _decimal(payload.get("price"), positive=True)
    exchange = _timestamp(payload["time"]) if "time" in payload else None
    if exchange is not None:
        if (observed - exchange).total_seconds() > QUOTE_MAX_AGE_SECONDS:
            raise _InvalidData("STALE_QUOTE")
        if (exchange - observed).total_seconds() > QUOTE_FUTURE_TOLERANCE_SECONDS:
            raise _InvalidData("FUTURE_QUOTE")
    return {"price": str(price), "observed_at": observed.isoformat(),
            "exchange_at": exchange.isoformat() if exchange is not None else None,
            "tick_size": None}


def _candle(item: object, timeframe: str) -> tuple[dict, datetime, datetime]:
    if not isinstance(item, list) or len(item) != 12:
        raise _InvalidData("INVALID_CANDLES")
    opened, closed = _timestamp(item[0]), _timestamp(item[6])
    expected_close = opened + timedelta(seconds=FIXED_SECONDS[timeframe], milliseconds=-1)
    if candle_open(opened, timeframe) != opened or closed != expected_close:
        raise _InvalidData("INVALID_CANDLES")
    prices = {key: _decimal(item[index], positive=True) for key, index in
              (("open", 1), ("high", 2), ("low", 3), ("close", 4))}
    if (prices["high"] < max(prices.values()) or prices["low"] > min(prices.values())):
        raise _InvalidData("INVALID_CANDLES")
    volume = _decimal(item[5], nonnegative=True)
    quote_volume = _decimal(item[7], nonnegative=True)
    taker_volume = _decimal(item[9], nonnegative=True)
    taker_quote_volume = _decimal(item[10], nonnegative=True)
    trade_count = item[8]
    if (type(trade_count) is not int or trade_count < 0 or taker_volume > volume or
            taker_quote_volume > quote_volume):
        raise _InvalidData("INVALID_CANDLES")
    candle = {"open_time": opened.isoformat(), "close_time": closed.isoformat(),
              **{key: str(number) for key, number in prices.items()}, "volume": str(volume),
              "quote_volume": str(quote_volume), "trade_count": trade_count,
              "taker_buy_volume": str(taker_volume),
              "taker_buy_quote_volume": str(taker_quote_volume)}
    return candle, opened, closed


def _candles(payload: object, timeframe: str, observed: datetime) -> dict:
    if not isinstance(payload, list) or not payload or len(payload) > 10:
        raise _InvalidData("EMPTY_CANDLES" if payload == [] else "INVALID_CANDLES")
    recent, forming, previous = [], None, None
    current_open = candle_open(observed, timeframe)
    latest_open = None
    for item in payload:
        candle, opened, closed = _candle(item, timeframe)
        if previous is not None and opened != previous + timedelta(seconds=FIXED_SECONDS[timeframe]):
            raise _InvalidData("CANDLE_GAP")
        if opened > observed:
            raise _InvalidData("INVALID_CANDLES")
        previous, latest_open = opened, opened
        if closed + timedelta(milliseconds=1) <= observed:
            recent.append(candle | {"is_closed": True})
        else:
            if forming is not None or opened != current_open:
                raise _InvalidData("INVALID_CANDLES")
            forming = candle | {"is_closed": False, "observed_at": observed.isoformat()}
    previous_open = current_open - timedelta(seconds=FIXED_SECONDS[timeframe])
    if (latest_open < previous_open or
            forming is None and (observed - current_open).total_seconds() > CANDLE_BOUNDARY_GRACE_SECONDS):
        raise _InvalidData("STALE_CANDLES")
    return {"forming_candle": forming, "recent_closed_candles": recent[-RECENT_CANDLE_LIMIT:],
            "observed_at": observed.isoformat()}


def _invalid_json_constant(_value: str):
    raise ValueError("Invalid JSON number")


async def _read(client: httpx.AsyncClient, component: str, symbol: str, timeframe: str,
                deadline: float, results: dict, errors: dict) -> None:
    path = QUOTE_PATH if component == "quote" else CANDLES_PATH
    params = {"symbol": symbol} if component == "quote" else {
        "symbol": symbol, "interval": timeframe, "limit": 10,
    }
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        response = await client.get(f"{BASE_URL}{path}", params=params, timeout=remaining)
        if response.status_code in (429, 418):
            raise _InvalidData("RATE_LIMITED" if response.status_code == 429 else "IP_BANNED")
        if response.status_code < 200 or response.status_code >= 300:
            raise _InvalidData("HTTP_ERROR")
        if len(response.content) > 128_000:
            raise _InvalidData("INVALID_RESPONSE")
        payload = response.json(parse_float=Decimal, parse_constant=_invalid_json_constant)
        observed = _now()
        result = _quote(payload, symbol, observed) if component == "quote" else (
            _candles(payload, timeframe, observed))
        if time.monotonic() >= deadline:
            raise TimeoutError
        results[component] = result
    except (TimeoutError, httpx.TimeoutException):
        errors[component] = "TIMEOUT"
    except httpx.HTTPError:
        errors[component] = "NETWORK_ERROR"
    except _InvalidData as error:
        errors[component] = error.code
    except (ValueError, TypeError, DecimalException, OverflowError):
        errors[component] = "INVALID_RESPONSE"


async def _collect(symbol: str, timeframe: str, deadline: float, results: dict, errors: dict) -> None:
    # Async cancellation bounds the whole response, including a slow body;
    # individual httpx timeouts alone only bound each connection/read phase.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        errors.update(quote="TIMEOUT", candles="TIMEOUT")
        return
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                await asyncio.gather(*(_read(client, component, symbol, timeframe, deadline,
                                            results, errors) for component in ("quote", "candles")))
    except TimeoutError:
        for component in ("quote", "candles"):
            if component not in results and component not in errors:
                errors[component] = "TIMEOUT"


def _comparison(context: dict, quote: dict | None) -> dict | None:
    frozen = context.get("quote")
    if not isinstance(frozen, dict):
        original = context.get("original_report")
        frozen = original.get("quote") if isinstance(original, dict) else None
    if not isinstance(frozen, dict):
        return None
    try:
        analysis = _decimal(frozen.get("price"), positive=True)
        percentage = None
        if quote is not None:
            current = _decimal(quote["price"], positive=True)
            with localcontext() as arithmetic:
                arithmetic.prec = max(50, len(analysis.as_tuple().digits),
                                      len(current.as_tuple().digits))
                percentage = str((current - analysis) / analysis * 100)
        return {"analysis_price": str(analysis), "change_since_analysis_pct": percentage}
    except (_InvalidData, DecimalException, ValueError):
        return None


def unavailable_snapshot(reason: str) -> dict:
    return {"version": SNAPSHOT_MODULE_VERSION, "status": "unavailable", "reason": reason}


def fetch_discussion_market(context: dict, *, timeout: float = MAX_TIMEOUT_SECONDS,
                            snapshot: bool = False) -> dict | None:
    """Fetch a new observation without modifying any saved analysis evidence.

    With snapshot, the market's zones, indicators, derivatives and flows are recomputed in
    parallel within a longer budget; the quote and recent candles keep their own short deadline.
    """
    pair = _frozen_pair(context)
    if pair is None:
        return None
    market_id, symbol, timeframe = pair
    requested = _now().isoformat()
    started = time.monotonic()
    snapshot_budget = (float(min(max(timeout, 0), SNAPSHOT_MAX_SECONDS))
                       if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                       and (not isinstance(timeout, float) or math.isfinite(timeout)) else 0)
    snapshot_future = None
    if snapshot and snapshot_budget > 0:
        pool = ThreadPoolExecutor(max_workers=1)
        snapshot_future = pool.submit(build_discussion_snapshot, market_id, timeframe, snapshot_budget)
        pool.shutdown(wait=False)
    results, errors = {}, {}
    budget = (float(min(max(timeout, 0), MAX_TIMEOUT_SECONDS))
              if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
              and (not isinstance(timeout, float) or math.isfinite(timeout)) else 0)
    deadline = time.monotonic() + max(0, budget)
    if budget <= 0:
        errors.update(quote="TIMEOUT", candles="TIMEOUT")
    else:
        coroutine = _collect(symbol, timeframe, deadline, results, errors)
        try:
            asyncio.run(coroutine)
        except (RuntimeError, OSError, ValueError, TypeError, httpx.HTTPError):
            # This entry point runs in the synchronous discussion worker. Safe
            # failure is preferable to exposing an event-loop or transport error.
            coroutine.close()
            for component in ("quote", "candles"):
                if component not in results:
                    errors.setdefault(component, "NETWORK_ERROR")
    quote, candles = results.get("quote"), results.get("candles", {})
    observations = [result["observed_at"] for result in results.values()]
    observation = {"version": VERSION, "status": "available" if len(results) == 2 else (
        "partial" if results else "unavailable"), "market_id": market_id, "timeframe": timeframe,
        "source": SOURCE, "requested_at": requested,
        "observed_at": max(observations) if observations else None, "quote": quote,
        "forming_candle": candles.get("forming_candle"),
        "recent_closed_candles": candles.get("recent_closed_candles", []),
        "comparison": _comparison(context, quote), "errors": errors,
        "data_basis": "new_public_market_observation", "not_refreshed": list(NOT_REFRESHED)}
    if not snapshot:
        return observation
    fresh = unavailable_snapshot("TIMEOUT")
    if snapshot_future is not None:
        try:
            fresh = snapshot_future.result(timeout=max(0.0, started + snapshot_budget - time.monotonic()))
        except FutureTimeout:
            pass
        except Exception:  # noqa: BLE001 -- never publish upstream payloads
            fresh = unavailable_snapshot("SNAPSHOT_FAILED")
    refreshed = fresh.get("status") in {"available", "partial"}
    return observation | {"version": SNAPSHOT_VERSION, "snapshot": fresh,
                          "not_refreshed": list(SNAPSHOT_NOT_REFRESHED if refreshed else NOT_REFRESHED)}
