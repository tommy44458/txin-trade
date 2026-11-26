"""Taker order flow from the frozen candles: the cumulative volume delta (CVD).

Binance klines carry the quote volume that market orders bought (taker buys); the rest of
each candle's volume was sold by market orders. Their difference added up over recent
closed candles is the cumulative volume delta. It is computed from the candles already in
the snapshot, so it adds no request and a report can always be recomputed.
"""

from decimal import Decimal

VERSION = "order_flow_v1"
# The latest closed candle, a short swing and about a day on an hourly chart.
WINDOWS = (1, 6, 24)
SOURCE = "Binance kline taker buy quote volume of closed candles"
INTERPRETATION_LIMIT = (
    "Taker flow is Binance market orders on this pair only. Buying into a falling price or selling into "
    "a rising one can mean absorption by resting orders, but it can also be hedging or arbitrage; it "
    "describes who was aggressive, not who will win.")


def _window(candles: list[dict], count: int) -> dict:
    part = candles[-count:]
    if len(part) < count:
        return {"status": "unavailable", "reason": "insufficient_candles", "candles": count}
    if any(c.get("quote_volume") is None or c.get("taker_buy_quote_volume") is None for c in part):
        # Older snapshots stored no taker volume; missing data is never read as zero.
        return {"status": "unavailable", "reason": "missing_taker_volume", "candles": count}
    quote = sum(Decimal(c["quote_volume"]) for c in part)
    buy = sum(Decimal(c["taker_buy_quote_volume"]) for c in part)
    sell = quote - buy
    delta = buy - sell
    change = (Decimal(part[-1]["close"]) / Decimal(part[0]["open"]) - 1) * 100
    if not delta or not change:
        relation = "flat"
    else:
        relation = "same_direction" if (delta > 0) == (change > 0) else "opposite"
    return {"status": "available", "candles": count, "from": part[0]["open_time"], "to": part[-1]["close_time"],
            "taker_buy_quote": str(buy.quantize(Decimal(1))), "taker_sell_quote": str(sell.quantize(Decimal(1))),
            "cvd_quote": str(delta.quantize(Decimal(1))),
            "buy_share_pct": str((buy / quote * 100).quantize(Decimal(".01"))) if quote > 0 else None,
            "price_change_pct": str(change.quantize(Decimal(".01"))), "price_vs_flow": relation}


def order_flow(candles: list[dict], timeframe: str) -> dict:
    return {"timeframe": timeframe, "windows": {f"last_{n}": _window(candles, n) for n in WINDOWS}}


def order_flow_context(candles: list[dict], timeframe: str, context_candles: list[dict] | None,
                       context_timeframe: str | None) -> dict:
    """CVD on the primary timeframe and, when it was fetched, the context timeframe."""
    return {"version": VERSION, "source": SOURCE, "unit": "quote currency (USDT)",
            "interpretation_limit": INTERPRETATION_LIMIT, "primary": order_flow(candles, timeframe),
            "context": order_flow(context_candles, context_timeframe) if context_candles and context_timeframe
            else None}
