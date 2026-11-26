import pytest
from fastapi import HTTPException

from trade_helper import liquidation_context as module
from trade_helper.liquidation_context import liquidation_context

THROUGH = 1_795_708_800_000


def totals(long_usd=0.0, short_usd=0.0):
    return {"long_usd": long_usd, "short_usd": short_usd, "long_count": 2, "short_count": 1,
            "max_usd": max(long_usd, short_usd)}


def summary(coverage=100.0, bybit_connected=True, stale=False):
    window = {"symbol": {"okx": totals(1_200_000.4, 300_000), "bybit": totals(2_500_000, 0)},
              "market": {"okx": totals(40e6, 10e6), "bybit": totals(60e6, 20e6)},
              "coverage_pct": {"okx": coverage, "bybit": coverage}}
    return {"generated_at": THROUGH + 60_000, "symbol": "BTCUSDT", "through": THROUGH, "stale": stale,
            "sources": {"okx": {"connected": True, "complete": False, "follows_symbol": True, "error": None},
                        "bybit": {"connected": bybit_connected, "complete": True, "follows_symbol": True,
                                  "error": None}},
            "windows": {"1h": window, "4h": window, "24h": window},
            "hourly": [{"t": THROUGH - (24 - i) * 3_600_000, "long_usd": 10.4, "short_usd": 0} for i in range(24)]}


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("TRADE_LIQUIDATIONS_ENABLED", "1")
    state = {"answer": summary(), "calls": [], "down": False}

    def fetch(path):
        state["calls"].append(path)
        if state["down"]:
            raise HTTPException(503, {"code": "smart_money_unavailable"})
        return state["answer"]

    monkeypatch.setattr(module, "fetch_cloud", fetch)
    return state


def test_the_pair_and_the_market_per_exchange_and_window(cloud):
    context = liquidation_context("binance:perp:BTCUSDT")
    assert cloud["calls"] == ["/smart-money/liquidations?symbol=BTCUSDT"]
    assert context["status"] == "available" and context["symbol"] == "BTCUSDT"
    assert context["windows"]["1h"]["symbol"]["okx"] == {
        "long_usd": 1200000, "short_usd": 300000, "long_count": 2, "short_count": 1, "largest_usd": 1200000}
    assert context["windows"]["24h"]["market"]["bybit"]["long_usd"] == 60000000
    assert context["sources"]["okx"] == {"connected": True, "complete": False, "follows_symbol": True}
    assert len(context["hourly_24h"]) == 24 and context["hourly_24h"][0]["long_usd"] == 10
    assert any("undercount" in note for note in context["notes"])


def test_gaps_and_dropped_streams_make_the_data_partial_not_quiet(cloud):
    cloud["answer"] = summary(coverage=60.0)
    assert liquidation_context("binance:perp:BTCUSDT")["status"] == "partial"
    # One stream followed through the hour is enough; the dropped one stays visible.
    cloud["answer"] = summary(bybit_connected=False)
    context = liquidation_context("binance:perp:BTCUSDT")
    assert context["status"] == "available" and context["sources"]["bybit"]["connected"] is False
    cloud["answer"] = summary(stale=True)
    assert liquidation_context("binance:perp:BTCUSDT")["status"] == "partial"
    # Never connected in the whole day: nothing to say about liquidations.
    cloud["answer"] = summary(coverage=0.0)
    assert liquidation_context("binance:perp:BTCUSDT")["status"] == "unavailable"


def test_outages_and_old_clouds_are_unavailable(cloud, monkeypatch):
    cloud["down"] = True
    assert liquidation_context("binance:perp:ETHUSDT") == {
        "version": module.VERSION, "source": module.SOURCE, "status": "unavailable", "symbol": "ETHUSDT"}
    cloud["down"] = False
    cloud["answer"] = {"symbol": "BTCUSDT"}
    assert liquidation_context("binance:perp:BTCUSDT")["status"] == "unavailable"
    monkeypatch.setenv("TRADE_LIQUIDATIONS_ENABLED", "0")
    cloud["calls"].clear()
    assert liquidation_context("binance:perp:BTCUSDT")["status"] == "unavailable"
    assert cloud["calls"] == []
