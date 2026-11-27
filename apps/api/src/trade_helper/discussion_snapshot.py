"""A fresh, compact market snapshot for a follow-up question about a market analysis.

The same calculations an analysis makes, on newly fetched public data: v3 zones with
their sweeps on the primary and secondary timeframe, the default indicators, open interest,
funding and positioning, order flow, liquidations, the estimated liquidation map and fund
flows. It never changes the saved analysis. Positions, macro data, news and the two larger
context timeframes are not refreshed.
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from decimal import Decimal
from time import monotonic

from .analysis import calculate
from .derivatives_context import compact_derivatives, fetch_derivatives_context
from .fund_flows import fund_flows_context
from .higher_timeframes import calculate_context_indicators
from .liquidation_context import liquidation_context
from .liquidation_map import compact_liquidation_map, fetch_liquidation_map
from .live_market_context import MarketContextError, collect_market_context
from .optional_indicators import bollinger
from .order_flow import order_flow_context
from .technical_snapshot import compact_indicator_values

VERSION = "discussion_market_snapshot_v1"
ZONE_FIELDS = ("id", "kind", "low", "high", "center", "timeframe", "confirmed_at", "zone_state",
               "price_relation", "price_test_state", "consecutive_closes_beyond", "pivot_count",
               "independent_touch_count", "last_touch_at", "evidence_score", "sweep_count",
               "last_sweep_at", "sweep_evidence", "liquidity_beyond", "forming_candle_swept")
ZONE_LIMIT = 8
NOT_REFRESHED = ["larger_context_timeframes", "market_reference", "positions", "macro", "news"]


def _zones(levels: list[dict]) -> list[dict]:
    return [{key: zone[key] for key in ZONE_FIELDS if key in zone} for zone in levels[:ZONE_LIMIT]]


def _support_resistance(calculated: dict | None, levels: list[dict] | None = None) -> dict:
    if not calculated:
        return {"status": "unavailable"}
    return {"status": "available", "source_time": calculated.get("as_of") or calculated.get("source_time"),
            "levels": _zones(levels if levels is not None else calculated["levels"]),
            "recently_invalidated_levels": _zones(calculated.get("recently_invalidated_levels", [])),
            "invalidation_rule": calculated.get("invalidation_rule"),
            "sweep_rule": calculated.get("sweep_rule")}


def _indicators(rows: list[dict] | None, timeframe: str, quote: dict) -> dict:
    if not rows or len(rows) < 60:
        return {"status": "unavailable", "timeframe": timeframe}
    try:
        indicators = calculate_context_indicators(rows, quote)
        indicators["bollinger"] = bollinger(rows, quote, {"period": 20, "multiplier": "2"})
        metrics = calculate(rows, timeframe)
    except (ValueError, KeyError, ArithmeticError):
        return {"status": "unavailable", "timeframe": timeframe, "reason": "calculation_failed"}
    return compact_indicator_values({"status": "available", "timeframe": timeframe,
                                     "metrics": metrics, "indicators": indicators})


def build_discussion_snapshot(market_id: str, timeframe: str, timeout: float) -> dict:
    """Fresh evidence for one follow-up turn; any part that fails or runs late is reported, never filled."""
    deadline = monotonic() + timeout
    try:
        context, inputs = collect_market_context(market_id, timeframe)
    except MarketContextError as exc:
        return {"version": VERSION, "status": "unavailable", "reason": exc.code}
    quote, candles = inputs["quote"], inputs["candles"]
    secondary_rows, secondary_timeframe = inputs["secondary_candles"], inputs["secondary_timeframe"]
    cutoff = datetime.fromisoformat(quote["observed_at"])
    reference = Decimal(str(quote["price"]))
    pool = ThreadPoolExecutor(max_workers=4)
    futures = {
        "derivatives_context": pool.submit(fetch_derivatives_context, market_id, cutoff, timeframe),
        "liquidation_context": pool.submit(liquidation_context, market_id),
        "liquidation_map": pool.submit(fetch_liquidation_map, market_id, cutoff, reference,
                                       inputs["tick_size"], candles if timeframe == "1h" else None),
        "fund_flows_context": pool.submit(fund_flows_context, market_id),
    }
    pool.shutdown(wait=False)
    errors = {}
    secondary = context["secondary_timeframe_context"]
    snapshot = {
        "version": VERSION, "market_id": market_id, "as_of": quote["observed_at"],
        "reference_price": str(reference), "primary_timeframe": timeframe,
        "secondary_timeframe": secondary_timeframe,
        "quote": {key: quote.get(key) for key in ("price", "mark_price", "index_price",
                                                  "last_funding_rate", "next_funding_time",
                                                  "observed_at")},
        "current_candle": context["current_candle"],
        "asset_class": context.get("asset_class", "crypto"),
        "us_equity_session": context.get("us_equity_session"),
        "support_resistance": {
            timeframe: _support_resistance(context["level_metadata"] | {
                "recently_invalidated_levels": context["recently_invalidated_levels"]},
                context["levels"]),
            secondary_timeframe: _support_resistance(secondary.get("data")),
        },
        "indicators": {timeframe: _indicators(candles, timeframe, quote),
                       secondary_timeframe: _indicators(secondary_rows, secondary_timeframe, quote)},
        "order_flow": order_flow_context(candles, timeframe, secondary_rows, secondary_timeframe),
    }
    for name, future in futures.items():
        try:
            value = future.result(timeout=max(0.0, deadline - monotonic()))
        except FutureTimeout:
            snapshot[name], errors[name] = None, "TIMEOUT"
            continue
        except Exception:  # noqa: BLE001 -- an optional source never fails the whole snapshot
            snapshot[name], errors[name] = None, "SOURCE_UNAVAILABLE"
            continue
        snapshot[name] = (compact_derivatives(value) if name == "derivatives_context" else
                          compact_liquidation_map(value) if name == "liquidation_map" else value)
    return snapshot | {"status": "partial" if errors else "available", "errors": errors,
                       "not_refreshed": list(NOT_REFRESHED)}
