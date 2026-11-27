"""Binance perpetuals that track US stocks: catalog, session, and the sources that do not apply."""

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trade_helper import market_catalog
from trade_helper.us_equity_session import regular_minutes, session_context

ROWS = [
    {"symbol": "BTCUSDT", "baseAsset": "BTC", "contractType": "PERPETUAL", "underlyingType": "COIN"},
    {"symbol": "ANTUSDT", "baseAsset": "ANT", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY"},
    {"symbol": "SPYUSDT", "baseAsset": "SPY", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY"},
    {"symbol": "QQQUSDT", "baseAsset": "QQQ", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY"},
    {"symbol": "COINUSDT", "baseAsset": "COIN", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY"},
    {"symbol": "XAUUSDT", "baseAsset": "XAU", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "COMMODITY"},
    {"symbol": "700USDT", "baseAsset": "700", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "HK_EQUITY"},
    {"symbol": "ANTHROPICUSDT", "baseAsset": "ANTHROPIC", "contractType": "TRADIFI_PERPETUAL",
     "underlyingType": "PREMARKET"},
]


def payload():
    return {"symbols": [row | {"quoteAsset": "USDT", "marginAsset": "USDT", "status": "TRADING",
                               "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}
                        for row in ROWS]}


@pytest.fixture
def stocks(monkeypatch):
    snapshot = market_catalog._parse_exchange_info(payload(), time.time())
    monkeypatch.setattr(market_catalog, "_cache", snapshot)
    return snapshot


def test_catalog_lists_us_stocks_with_their_asset_class_and_skips_other_tradfi(stocks):
    classes = {row["binance_symbol"]: row["asset_class"] for row in stocks.markets}
    assert classes == {"BTCUSDT": "crypto", "ANTUSDT": "us_equity", "SPYUSDT": "us_equity",
                       "QQQUSDT": "us_equity", "COINUSDT": "us_equity"}
    assert market_catalog.is_us_equity("binance:perp:ANTUSDT")
    assert not market_catalog.is_us_equity("binance:perp:BTCUSDT")
    assert not market_catalog.is_us_equity("binance:perp:XAUUSDT")
    assert not market_catalog.is_us_equity("not a market")


def test_disk_cache_keeps_the_asset_class_and_old_caches_read_as_crypto(stocks, tmp_path):
    path = tmp_path / "market_catalog.json"
    market_catalog._save_disk(path, stocks)
    loaded = market_catalog._load_disk(path)
    assert {row["binance_symbol"]: row["asset_class"] for row in loaded.markets} == {
        row["binance_symbol"]: row["asset_class"] for row in stocks.markets}
    saved = json.loads(path.read_text())
    for row in saved["markets"]:
        row.pop("asset_class")
    saved["markets"] = [row for row in saved["markets"] if row["binance_symbol"] == "BTCUSDT"]
    path.write_text(json.dumps(saved))
    assert [row["asset_class"] for row in market_catalog._load_disk(path).markets] == ["crypto"]


def test_bingx_positions_never_map_to_a_stock_perpetual(stocks):
    from trade_helper.bingx import normalize_positions

    rows = [{"symbol": "COIN-USDT", "positionAmt": "1", "positionSide": "LONG", "positionId": "1",
             "avgPrice": "10", "leverage": "5", "updateTime": 1}]
    positions, skipped = normalize_positions("perpetual", rows)
    assert positions == [] and skipped == 1


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.mark.parametrize("moment,state", [
    ("2026-11-30T10:00:00-05:00", "regular"),
    ("2026-11-30T05:00:00-05:00", "pre_market"),
    ("2026-11-30T17:00:00-05:00", "after_hours"),
    ("2026-11-30T02:00:00-05:00", "overnight"),
    ("2026-11-28T12:00:00-05:00", "weekend"),
    ("2026-11-26T12:00:00-05:00", "holiday"),
    ("2026-11-27T12:00:00-05:00", "regular"),
    ("2026-11-27T13:30:00-05:00", "after_hours"),
    ("2026-07-03T11:00:00-04:00", "holiday"),
])
def test_session_state_follows_the_nyse_calendar(moment, state):
    context = session_context(at(moment))
    assert context["status"] == "available" and context["state"] == state


def test_next_open_and_last_close_skip_weekends_holidays_and_early_closes():
    thanksgiving = session_context(at("2026-11-26T12:00:00-05:00"))
    assert thanksgiving["next_regular_open"] == "2026-11-27T14:30:00+00:00"
    assert thanksgiving["last_regular_close"] == "2026-11-25T21:00:00+00:00"
    saturday = session_context(at("2026-11-28T12:00:00-05:00"))
    # The day after Thanksgiving closes at 13:00.
    assert saturday["last_regular_close"] == "2026-11-27T18:00:00+00:00"
    assert saturday["next_regular_open"] == "2026-11-30T14:30:00+00:00"
    assert saturday["hours_to_next_open"] == 45.5
    assert session_context(at("2026-11-27T12:00:00-05:00"))["early_close_today"] is True
    assert session_context(at("2028-03-01T12:00:00-05:00"))["status"] == "calendar_unavailable"
    assert session_context(at("2027-12-27T12:00:00-05:00"))["status"] == "calendar_unavailable"


def test_recent_candles_report_how_much_traded_in_regular_hours():
    start = at("2026-11-30T00:00:00-05:00")
    candles = [{"open_time": (start + timedelta(hours=i)).isoformat()} for i in range(24)]
    context = session_context(start + timedelta(hours=24, minutes=1), candles, "1h")
    assert context["recent_candles"] == {"timeframe": "1h", "count": 24, "regular_session_share_pct": 27.1}
    weekend = [{"open_time": (at("2026-11-28T00:00:00-05:00") + timedelta(hours=i)).isoformat()}
               for i in range(24)]
    assert session_context(at("2026-11-29T00:01:00-05:00"), weekend, "1h")[
        "recent_candles"]["regular_session_share_pct"] == 0.0
    assert regular_minutes(at("2026-11-30T09:00:00-05:00"), at("2026-11-30T10:00:00-05:00")) == 30


def test_crypto_only_sources_are_not_applicable_for_stocks(stocks, monkeypatch):
    from trade_helper import fund_flows, liquidation_context

    monkeypatch.setenv("TRADE_FUND_FLOWS_ENABLED", "1")
    monkeypatch.setattr(fund_flows, "fetch_cloud", lambda *_: pytest.fail("no cloud request for a stock"))
    monkeypatch.setattr(liquidation_context, "fetch_cloud", lambda *_: pytest.fail("no cloud request"))
    flows = fund_flows.fund_flows_context("binance:perp:COINUSDT")
    assert flows["status"] == "not_applicable" and flows["pair_asset"]["status"] == "not_applicable"
    liquidations = liquidation_context.liquidation_context("binance:perp:ANTUSDT")
    assert liquidations["status"] == "not_applicable" and liquidations["symbol"] == "ANTUSDT"


def test_a_stock_is_referenced_against_spy_and_qqq(stocks, monkeypatch):
    from trade_helper import market_reference

    monkeypatch.setenv("TRADE_MARKET_REFERENCE_ENABLED", "1")
    asked = []

    def candles(market_id, interval, limit):
        asked.append(market_id)
        raise ValueError("offline in tests")

    monkeypatch.setattr(market_reference, "fetch_candles", candles)
    now = datetime(2026, 11, 30, 16, tzinfo=UTC)
    stock = market_reference.fetch_market_reference("binance:perp:ANTUSDT", "1h", [], [], now)
    assert set(stock["references"]) == {"binance:perp:SPYUSDT", "binance:perp:QQQUSDT"}
    assert set(asked) == {"binance:perp:SPYUSDT", "binance:perp:QQQUSDT"}
    spy = market_reference.fetch_market_reference("binance:perp:SPYUSDT", "1h", [], [], now)
    assert spy["references"]["binance:perp:SPYUSDT"] == {"status": "self", "reason": "analyzed_market"}
    crypto = market_reference.fetch_market_reference("binance:perp:BTCUSDT", "1h", [], [], now)
    assert set(crypto["references"]) == {"binance:perp:BTCUSDT", "binance:perp:ETHUSDT"}


def test_the_liquidation_map_assumes_lower_leverage_for_stocks(stocks, monkeypatch):
    import httpx

    from trade_helper import liquidation_map

    from .test_liquidation_map import cutoff, hourly, interest

    monkeypatch.setenv("TRADE_LIQUIDATION_MAP_ENABLED", "1")
    monkeypatch.setattr(liquidation_map.httpx, "get", lambda url, params, timeout: httpx.Response(
        200, json=[row | {"symbol": "ANTUSDT"} for row in interest([1000 + i for i in range(501)])],
        request=httpx.Request("GET", url)))
    result = liquidation_map.fetch_liquidation_map("binance:perp:ANTUSDT", cutoff(500), Decimal(100),
                                                   Decimal("0.1"), hourly(500))
    assert result["assumptions"]["leverage_tiers"] == {"5": 0.3, "10": 0.4, "20": 0.3}
    # 20x and 10x longs sit 4.5% and 9.5% below an entry of 100; 5x is beyond the 15% range,
    # and nothing is modelled at 50x or 100x.
    assert {row["low"] for row in result["long_clusters_below"]} == {"95.5", "90.5"}


def test_stock_prompts_explain_sessions_and_inapplicable_sources():
    from trade_helper.prompts import resolve_prompt

    for task in ("strategy_market", "discussion"):
        for locale in ("en-US", "zh-TW"):
            text = resolve_prompt(task, prompt_locale=locale, response_locale=locale).instructions
            assert "us_equity_session" in text and "not_applicable" in text and "SPY" in text
