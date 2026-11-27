"""Recent liquidations for the analyzed pair and the whole market.

The cloud keeps Bybit's and OKX's public liquidation streams open and adds them into
five-minute buckets; this reads one cached summary for the pair. Binance refuses the cloud's
connections, so its liquidations are not included. It never raises, so an
outage only marks the data unavailable. Coverage is part of the data: a window the
streams were not connected for is missing, not quiet.
"""

import os
from datetime import UTC, datetime

from fastapi import HTTPException

from .market import symbol_for
from .market_catalog import is_us_equity
from .smart_money import fetch_cloud

VERSION = "liquidation_context_v1"
SOURCE = "Bybit and OKX public liquidation streams, collected by the txinTrade cloud"
EXCHANGES = ("bybit", "okx")
WINDOWS = ("1h", "4h", "24h")
# A stream connected for less of the last hour than this does not make the data complete.
COMPLETE_PCT = 90
NOTES = [
    ("long_usd is long positions closed by force (forced selling); short_usd is short positions closed "
     "by force (forced buying). Amounts are notional US dollars."),
    ("Bybit sends every liquidation for its 80 USDT perpetuals with the most turnover (follows_symbol says "
     "whether this pair is one); OKX publishes only part of its liquidations, so its totals undercount. "
     "Binance liquidations are not included."),
    ("coverage_pct is how much of each window a stream was connected; missing coverage is missing data, "
     "not a quiet market."),
]


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms else None


def _totals(raw: dict) -> dict:
    return {"long_usd": round(raw["long_usd"]), "short_usd": round(raw["short_usd"]),
            "long_count": raw["long_count"], "short_count": raw["short_count"],
            "largest_usd": round(raw["max_usd"])}


def unavailable_liquidations(market_id: str) -> dict:
    return {"version": VERSION, "source": SOURCE, "status": "unavailable", "symbol": symbol_for(market_id)}


def liquidation_context(market_id: str) -> dict:
    symbol = symbol_for(market_id)
    if is_us_equity(market_id):
        # The cloud follows crypto contracts on Bybit and OKX; it has nothing for stocks.
        return {"version": VERSION, "source": SOURCE, "status": "not_applicable", "reason": "us_equity",
                "symbol": symbol}
    if os.getenv("TRADE_LIQUIDATIONS_ENABLED", "1") == "0":
        return unavailable_liquidations(market_id)
    try:
        summary = fetch_cloud(f"/smart-money/liquidations?symbol={symbol}")
        windows = {
            name: {
                "symbol": {e: _totals(summary["windows"][name]["symbol"][e]) for e in EXCHANGES},
                "market": {e: _totals(summary["windows"][name]["market"][e]) for e in EXCHANGES},
                "coverage_pct": {e: summary["windows"][name]["coverage_pct"][e] for e in EXCHANGES},
            } for name in WINDOWS
        }
        sources = {e: {key: summary["sources"][e][key] for key in ("connected", "complete", "follows_symbol")}
                   for e in EXCHANGES}
        hourly = [{"hour": _iso(point["t"]), "long_usd": round(point["long_usd"]),
                   "short_usd": round(point["short_usd"])} for point in summary["hourly"]]
    except (HTTPException, KeyError, TypeError, ValueError):
        return unavailable_liquidations(market_id)
    covered = [windows["24h"]["coverage_pct"][e] for e in EXCHANGES]
    if not any(covered):
        return unavailable_liquidations(market_id)
    # One exchange followed through the last hour is enough; the other's gaps stay visible.
    complete = any(windows["1h"]["coverage_pct"][e] >= COMPLETE_PCT and sources[e]["connected"]
                   for e in EXCHANGES)
    return {"version": VERSION, "source": SOURCE,
            "status": "available" if complete and not summary.get("stale") else "partial",
            "symbol": symbol, "through": _iso(summary.get("through")), "notes": NOTES,
            "sources": sources, "windows": windows, "hourly_24h": hourly}
