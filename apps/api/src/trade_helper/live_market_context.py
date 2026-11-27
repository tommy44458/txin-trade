"""Public, fresh chart evidence without an AI analysis or account credentials.

The same closed-candle lookback and V3 algorithm are used by the analysis worker.
The current quote locates zones; only closed candles change their lifecycle.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from fastapi import APIRouter, HTTPException, Response

from .current_candle import current_candle_context
from .market import (
    MAIN_HISTORY_LIMIT,
    fetch_candles,
    fetch_forming_candle,
    fetch_quote,
    fetch_tick_size,
)
from .market_catalog import is_us_equity, validate_market_id
from .market_store import validate_candle
from .models import Timeframe
from .strategy_engine import other_timeframe
from .support_levels_v3 import annotate_forming_sweep, historical_levels_v3
from .timeframes import FIXED_SECONDS, analysis_timeframes, higher_timeframes
from .us_equity_session import session_context

VERSION = "live_market_context_v1"
CHART_CANDLE_LIMIT = 300
SOURCE = "Binance USDⓈ-M perpetual public API"
router = APIRouter(prefix="/api/v1")


class MarketContextError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_quote(quote: dict) -> datetime:
    try:
        observed = datetime.fromisoformat(quote["observed_at"])
        price = Decimal(str(quote["price"]))
        if observed.tzinfo is None or not price.is_finite() or price <= 0:
            raise ValueError("Invalid quote")
        age = _now() - observed
        if age < -timedelta(seconds=3):
            raise ValueError("Future quote")
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise MarketContextError("QUOTE_INVALID", "現價資料無效，請重新整理。") from exc
    if age > timedelta(seconds=180):
        raise MarketContextError("QUOTE_STALE", "現價資料超過 180 秒，請重新整理。")
    return observed


def _validate_history(rows: list[dict], timeframe: str, cutoff: datetime) -> None:
    if not isinstance(rows, list) or len(rows) < 60:
        raise MarketContextError("CANDLES_INSUFFICIENT", "不足 60 根已收盤 K 線，暫無法計算支撐壓力。")
    step = timedelta(seconds=FIXED_SECONDS[timeframe])
    previous = None
    try:
        for row in rows:
            validate_candle(row, timeframe)
            opened = datetime.fromisoformat(row["open_time"])
            closed = datetime.fromisoformat(row["close_time"])
            if closed > cutoff or closed > _now():
                raise ValueError("Unclosed candle supplied as historical evidence")
            if previous is not None and opened - previous != step:
                raise ValueError("History has a gap or duplicate")
            previous = opened
        latest = datetime.fromisoformat(rows[-1]["close_time"])
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise MarketContextError("CANDLES_INVALID", "K 線資料有缺漏或尚未收盤，請重新整理。") from exc
    if _now() - latest > step + timedelta(minutes=3):
        raise MarketContextError("CANDLES_STALE", "已收盤 K 線資料過期，請重新整理。")


def _history_metadata(rows: list[dict], timeframe: str) -> dict:
    return {
        "requested_candles": MAIN_HISTORY_LIMIT,
        "closed_candle_count": len(rows),
        "chart_closed_candle_count": min(len(rows), CHART_CANDLE_LIMIT),
        "duration_hours": len(rows) * (FIXED_SECONDS[timeframe] // 3600),
        "start_at": rows[0]["open_time"], "end_at": rows[-1]["close_time"],
        "status": "complete" if len(rows) == MAIN_HISTORY_LIMIT else "partial",
        "history_quality": getattr(rows, 'history_quality', None),
    }


def build_market_context(market_id: str, timeframe: str) -> dict:
    return collect_market_context(market_id, timeframe)[0]


def collect_market_context(market_id: str, timeframe: str) -> tuple[dict, dict]:
    """The chart context, and the validated inputs it was computed from for further evidence."""
    secondary_timeframe = other_timeframe(timeframe)
    # Fetch unconfirmed OHLC before freezing the reference quote, as the Agent
    # worker does. It never joins the closed rows sent to the V3 algorithm.
    with ThreadPoolExecutor(max_workers=3) as pool:
        primary_future = pool.submit(fetch_candles, market_id, timeframe,
                                     limit=MAIN_HISTORY_LIMIT)
        secondary_future = pool.submit(fetch_candles, market_id, secondary_timeframe,
                                       limit=MAIN_HISTORY_LIMIT)
        forming_future = pool.submit(fetch_forming_candle, market_id, timeframe)
        try:
            candles = primary_future.result()
        except Exception as exc:
            raise MarketContextError("MARKET_DATA_UNAVAILABLE", "行情來源暫時無法取得，請重新整理。") from exc
        try:
            secondary_rows = secondary_future.result()
        except Exception:  # noqa: BLE001 -- optional second timeframe, no fabricated history
            secondary_rows = None
        try:
            forming = forming_future.result()
        except Exception:  # noqa: BLE001 -- the quote remains usable without unconfirmed OHLC
            forming = None
    try:
        quote = fetch_quote(market_id)
        tick_size = fetch_tick_size(market_id)
    except Exception as exc:
        raise MarketContextError("MARKET_DATA_UNAVAILABLE", "行情來源暫時無法取得，請重新整理。") from exc
    cutoff = _validate_quote(quote)
    _validate_history(candles, timeframe, cutoff)
    reference = Decimal(str(quote["price"]))
    try:
        calculated = historical_levels_v3(candles, timeframe, reference, tick_size, market_id)
    except (ValueError, ArithmeticError) as exc:
        raise MarketContextError("LEVEL_CALCULATION_FAILED", "支撐壓力資料暫時無法計算，請重新整理。") from exc
    secondary = {"timeframe": secondary_timeframe, "status": "unavailable",
                 "reason": "MARKET_DATA_UNAVAILABLE", "data": None}
    valid_secondary = None
    if secondary_rows is not None:
        try:
            _validate_history(secondary_rows, secondary_timeframe, cutoff)
            valid_secondary = secondary_rows
            data = historical_levels_v3(secondary_rows, secondary_timeframe, reference,
                                        tick_size, market_id)
            secondary = {"timeframe": secondary_timeframe, "status": "available", "data": data,
                         "history": _history_metadata(secondary_rows, secondary_timeframe)}
        except MarketContextError as exc:
            secondary["reason"] = exc.code
        except (ValueError, ArithmeticError):
            secondary["reason"] = "LEVEL_CALCULATION_FAILED"
    if forming is not None:
        try:
            validate_candle(forming, timeframe)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            forming = None
    current = current_candle_context(candles, quote | {"forming_candle": forming}, timeframe)
    forming = current["candle"]
    chart_candles = candles[-CHART_CANDLE_LIMIT:]
    levels = annotate_forming_sweep(calculated["levels"], forming, reference)
    fingerprint = sha256(json.dumps({"market_id": market_id, "timeframe": timeframe,
                                     "candles": candles, "quote": quote,
                                     "forming_candle": forming},
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    context = {
        "version": VERSION, "status": "ready", "market_id": market_id, "timeframe": timeframe,
        "analysis_timeframes": list(analysis_timeframes(timeframe)),
        "context_timeframes": list(higher_timeframes(timeframe)),
        "source": SOURCE, "as_of": quote["observed_at"], "quote": quote,
        "market_snapshot_sha256": fingerprint,
        "candles": chart_candles, "forming_candle": forming,
        "chart_candles": [row | {"closed": True} for row in chart_candles] + (
            [forming | {"closed": False}] if forming is not None else []),
        "current_candle": current, "levels": levels,
        "recently_invalidated_levels": calculated["recently_invalidated_levels"],
        "level_metadata": {
            **{key: calculated[key] for key in (
                "algorithm_version", "context_version", "tick_size", "active_zone_count",
                "invalidated_zone_count", "invalidation_rule", "sweep_rule", "quality_flags")},
            "source_time": calculated["as_of"], "reference_price": str(reference),
            "reference_time": quote["observed_at"],
        },
        "history": _history_metadata(candles, timeframe),
        "level_price_context": {
            "reference_price": str(reference), "reference_time": quote["observed_at"],
            "inside_zone_ids": [zone["id"] for zone in levels if zone["price_relation"] == "inside"],
            "testing_zone_ids": [zone["id"] for zone in levels
                                 if zone["price_test_state"] != "not_testing"],
        },
        "secondary_timeframe_context": secondary,
        "asset_class": "us_equity" if is_us_equity(market_id) else "crypto",
    }
    if context["asset_class"] == "us_equity":
        context["us_equity_session"] = session_context(datetime.fromisoformat(quote["observed_at"]),
                                                       candles, timeframe)
    return context, {"candles": candles, "secondary_candles": valid_secondary,
                     "secondary_timeframe": secondary_timeframe, "quote": quote,
                     "tick_size": tick_size, "forming_candle": forming}


@router.get("/market-context")
def market_context(response: Response, market_id: str, timeframe: Timeframe = "1h",
                   refresh: bool = False):
    # Every request is fresh; refresh is accepted for an explicit UI refresh.
    # There is no cache containing quotes, unfinished candles, or account data.
    response.headers["Cache-Control"] = "no-store"
    try:
        validate_market_id(market_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        return build_market_context(market_id, timeframe)
    except MarketContextError as exc:
        raise HTTPException(503, {"code": exc.code, "message": str(exc),
                                  "market_id": market_id, "timeframe": timeframe},
                            headers={"Cache-Control": "no-store"}) from exc
