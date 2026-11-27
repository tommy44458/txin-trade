from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from trade_helper import liquidation_map
from trade_helper.liquidation_map import (
    HOURS,
    compact_liquidation_map,
    estimate_liquidation_map,
    fetch_liquidation_map,
)

START = datetime(2026, 11, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"


def hourly(count: int, low: str = "99.9", high: str = "100.1") -> list[dict]:
    return [{"open_time": (START + timedelta(hours=i)).isoformat(),
             "close_time": (START + timedelta(hours=i + 1) - timedelta(milliseconds=1)).isoformat(),
             "open": "100", "high": high, "low": low, "close": "100",
             "volume": "10", "quote_volume": "1000"} for i in range(count)]


def interest(values: list[float]) -> list[dict]:
    return [{"symbol": SYMBOL, "timestamp": int((START + timedelta(hours=i)).timestamp() * 1000),
             "sumOpenInterest": str(value)} for i, value in enumerate(values)]


def cutoff(count: int) -> datetime:
    return START + timedelta(hours=count, seconds=30)


def side_total(result: dict, side: str) -> float:
    column = 2 if side == "long" else 3
    return sum(float(row[column]) for row in result["histogram"])


def test_rising_open_interest_places_both_sides_at_each_leverage_tier() -> None:
    count = 100
    result = estimate_liquidation_map(hourly(count), interest([1000 + i for i in range(count + 1)]),
                                      SYMBOL, Decimal(100), cutoff(count), Decimal("0.1"))
    assert result["status"] == "partial" and result["hours"] == count
    below = {row["low"] for row in result["long_clusters_below"]}
    above = {row["low"] for row in result["short_clusters_above"]}
    # 100x, 50x, 25x and 10x with a 0.5% maintenance margin.
    assert below == {"99.5", "98.5", "96.5", "90.5"}
    assert above == {"100.5", "101.5", "103.5", "109.5"}
    # 100 new contracts at $100 each side.
    assert side_total(result, "long") == pytest.approx(10_000, abs=2)
    assert side_total(result, "short") == pytest.approx(10_000, abs=2)
    assert result["cumulative_within"]["1pct"] == {"long_usd": "1500", "short_usd": "1500"}
    assert [row["low"] for row in result["near_long_clusters"]] == ["98.5", "99.5"]
    assert [row["low"] for row in result["near_short_clusters"]] == ["101.5", "100.5"]
    assert result["cumulative_within"]["10pct"] == {"long_usd": "10000", "short_usd": "10000"}
    assert result["long_clusters_below"][0]["share_of_side_pct"] == "30.00"
    assert Decimal(result["modelled_share_of_open_interest_pct"]) == pytest.approx(
        Decimal(100 / 1100 * 100), abs=Decimal("0.01"))


def test_price_trading_through_a_level_removes_it() -> None:
    count = 100
    candles = hourly(count)
    candles[90].update(low="99")
    result = estimate_liquidation_map(candles, interest([1000 + i for i in range(count + 1)]),
                                      SYMBOL, Decimal(100), cutoff(count), Decimal("0.1"))
    # The 100x longs opened before hour 90 are gone; hour 90's own and later ones remain.
    hundred = next(row for row in result["long_clusters_below"] if row["low"] == "99.5")
    assert float(hundred["notional_usd"]) == pytest.approx(10 * 0.15 * 100, abs=1)
    assert side_total(result, "short") == pytest.approx(10_000, abs=2)


def test_falling_open_interest_shrinks_positions_and_the_quote_removes_crossed_levels() -> None:
    count = 80
    values = [1000 + i for i in range(41)] + [1040 - 10 * i for i in range(1, 41)]
    result = estimate_liquidation_map(hourly(count), interest(values), SYMBOL, Decimal(100),
                                      cutoff(count))
    assert side_total(result, "long") < 4000 * 0.9
    crossed = estimate_liquidation_map(hourly(count), interest([1000 + i for i in range(count + 1)]),
                                       SYMBOL, Decimal(99), cutoff(count))
    assert all(float(row["low"]) < 99 for row in crossed["long_clusters_below"])


def test_short_history_and_gaps_are_not_filled() -> None:
    short = estimate_liquidation_map(hourly(40), interest([1000 + i for i in range(41)]),
                                     SYMBOL, Decimal(100), cutoff(40))
    assert short == {"version": "estimated_liquidation_map_v1", "status": "unavailable",
                     "reason": "insufficient_history", "hours": 40}
    rows = interest([1000 + i for i in range(101)])
    del rows[50]
    gap = estimate_liquidation_map(hourly(100), rows, SYMBOL, Decimal(100), cutoff(100))
    assert gap["hours"] == 98
    with pytest.raises(ValueError):
        estimate_liquidation_map(hourly(100), [r | {"symbol": "ETHUSDT"} for r in rows],
                                 SYMBOL, Decimal(100), cutoff(100))
    future = estimate_liquidation_map(hourly(100), interest([1000 + i for i in range(101)]),
                                      SYMBOL, Decimal(100), cutoff(80))
    assert future["hours"] == 80


def test_fetch_reuses_hourly_snapshot_candles_and_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("TRADE_LIQUIDATION_MAP_ENABLED", "1")
    count = HOURS
    calls = []

    def get(url, params, timeout):
        calls.append((url, params["period"], params["limit"]))
        return httpx.Response(200, json=interest([1000 + i for i in range(count + 1)]),
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(liquidation_map, "symbol_for", lambda _market: SYMBOL)
    monkeypatch.setattr(liquidation_map.httpx, "get", get)
    monkeypatch.setattr(liquidation_map, "fetch_candles",
                        lambda *_args, **_kw: pytest.fail("snapshot candles should be reused"))
    result = fetch_liquidation_map("binance:perp:BTCUSDT", cutoff(count), Decimal(100),
                                   Decimal("0.1"), hourly(count))
    assert result["status"] == "available" and result["market_id"] == "binance:perp:BTCUSDT"
    assert calls == [(f"{liquidation_map.BASE_URL}/futures/data/openInterestHist", "1h", HOURS)]
    assert "histogram" in result and "histogram" not in compact_liquidation_map(result)
    assert result["assumptions"]["leverage_tiers"] == {"10": 0.3, "25": 0.3, "50": 0.25, "100": 0.15}

    def broken(url, params, timeout):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(liquidation_map.httpx, "get", broken)
    failed = fetch_liquidation_map("binance:perp:BTCUSDT", cutoff(count), Decimal(100))
    assert failed["status"] == "unavailable" and failed["reason"] == "source_unavailable_or_invalid"
    monkeypatch.setenv("TRADE_LIQUIDATION_MAP_ENABLED", "0")
    assert fetch_liquidation_map("binance:perp:BTCUSDT", cutoff(count), Decimal(100))["reason"] == "disabled"
