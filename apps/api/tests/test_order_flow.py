from datetime import UTC, datetime, timedelta

from trade_helper.order_flow import order_flow, order_flow_context

START = datetime(2026, 9, 30, tzinfo=UTC)


def candle(i, open_, close, quote, buy):
    at = START + timedelta(hours=i)
    return {"open_time": at.isoformat(), "close_time": (at + timedelta(hours=1)).isoformat(),
            "open": str(open_), "high": str(max(open_, close)), "low": str(min(open_, close)),
            "close": str(close), "volume": "1", "quote_volume": str(quote), "taker_buy_quote_volume": str(buy)}


def test_cvd_adds_taker_buys_minus_sells_and_compares_with_price():
    # 23 quiet candles, then price rising while takers sold more than they bought.
    candles = [candle(i, 100, 100, 1000, 500) for i in range(23)] + [candle(23, 100, 102, 1000, 300)]
    flow = order_flow(candles, "1h")["windows"]
    assert flow["last_1"] == {
        "status": "available", "candles": 1, "from": candles[-1]["open_time"], "to": candles[-1]["close_time"],
        "taker_buy_quote": "300", "taker_sell_quote": "700", "cvd_quote": "-400", "buy_share_pct": "30.00",
        "price_change_pct": "2.00", "price_vs_flow": "opposite"}
    assert flow["last_24"]["cvd_quote"] == "-400" and flow["last_24"]["buy_share_pct"] == "49.17"
    assert flow["last_6"]["price_vs_flow"] == "opposite"


def test_rising_price_on_net_buying_moves_the_same_way_and_flat_windows_say_so():
    rising = [candle(i, 100 + i, 101 + i, 1000, 700) for i in range(6)]
    assert order_flow(rising, "4h")["windows"]["last_6"]["price_vs_flow"] == "same_direction"
    flat = [candle(i, 100, 100, 1000, 500) for i in range(6)]
    assert order_flow(flat, "4h")["windows"]["last_6"]["price_vs_flow"] == "flat"


def test_missing_taker_volume_or_history_is_unavailable_not_zero():
    old = [{k: v for k, v in candle(i, 100, 101, 1000, 600).items() if k != "taker_buy_quote_volume"}
           for i in range(24)]
    assert order_flow(old, "1h")["windows"]["last_6"] == {
        "status": "unavailable", "reason": "missing_taker_volume", "candles": 6}
    short = [candle(i, 100, 101, 1000, 600) for i in range(5)]
    assert order_flow(short, "1h")["windows"]["last_6"]["reason"] == "insufficient_candles"


def test_context_timeframe_is_included_only_when_fetched():
    candles = [candle(i, 100, 101, 1000, 600) for i in range(24)]
    context = order_flow_context(candles, "1h", candles, "4h")
    assert context["primary"]["timeframe"] == "1h" and context["context"]["timeframe"] == "4h"
    assert order_flow_context(candles, "1h", None, "4h")["context"] is None
