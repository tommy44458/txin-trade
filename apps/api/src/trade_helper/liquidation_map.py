"""An estimate of where leveraged positions opened in recent weeks would be force-closed.

Binance publishes neither positions nor their leverage, so this is a model, not position
data. Each hourly rise in open interest is treated as new contracts opened near that hour's
average price: one long and one short per contract, spread over common leverage tiers.
A modelled position is removed when price later trades through its liquidation price, and
every position shrinks in proportion when open interest falls for other reasons. What is
left shows where forced selling (below price) or forced buying (above price) would cluster.
"""

import math
import os
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import pairwise

import httpx

from .market import BASE_URL, fetch_candles, symbol_for
from .market_catalog import is_us_equity

VERSION = "estimated_liquidation_map_v1"
PERIOD = "1h"
HOUR_MS = 3_600_000
# Binance keeps open interest statistics for 30 days and returns at most 500 rows.
HOURS = 500
MIN_HOURS = 72
LEVERAGE_TIERS = ((10, 0.30), (25, 0.30), (50, 0.25), (100, 0.15))
# Binance lists stock perpetuals at 5x to 20x; their brackets are not public without a key.
EQUITY_LEVERAGE_TIERS = ((5, 0.30), (10, 0.40), (20, 0.30))
MAINTENANCE_RATE = 0.005
BIN_PCT = Decimal("0.25")
RANGE_PCT = Decimal(15)
DISTANCES_PCT = (1, 2, 3, 5, 10)
CLUSTER_LIMIT = 5
NEAR_PCT = 3
NEAR_LIMIT = 3
SOURCE = "Binance USD-M openInterestHist (1h) and 1h klines; modelled, not position data"
def assumptions(tiers: tuple = LEVERAGE_TIERS) -> dict:
    return {
        "entry": "hourly volume-weighted average price of the hour open interest rose",
        "sides": "every new contract opens one long and one short",
        "leverage_tiers": {str(leverage): weight for leverage, weight in tiers},
        "maintenance_margin_rate": MAINTENANCE_RATE,
        "closing": "open interest declines not explained by modelled liquidations close every position pro rata",
        "history": "only positions opened inside the fetched hours; older positions are unknown",
    }
INTERPRETATION_LIMIT = (
    "A model of Binance positions from open interest changes and assumed leverage, not real orders, "
    "stops or positions, and not other exchanges. Clusters show where forced selling (long "
    "liquidations below price) or forced buying (short liquidations above price) would likely "
    "accelerate a move; price may sweep them and reverse, or never reach them.")


def unavailable(market_id: str, cutoff: datetime, reason: str) -> dict:
    return {"version": VERSION, "status": "unavailable", "reason": reason, "market_id": market_id,
            "as_of": cutoff.isoformat(), "source": SOURCE}


def _price(value: float, tick: Decimal | None, up: bool) -> str:
    if tick is None or tick <= 0:
        return format(Decimal(repr(value)).quantize(Decimal(1).scaleb(
            Decimal(repr(value)).adjusted() - 5)), "f")
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return format((Decimal(repr(value)) / tick).to_integral_value(rounding=rounding) * tick, "f")


def _usd(value: float) -> str:
    return str(round(value))


def _pct(value: float) -> str:
    return format(Decimal(repr(value)).quantize(Decimal("0.01")), "f")


def estimate_liquidation_map(candles: list[dict], open_interest: list[dict], symbol: str,
                             reference: Decimal, cutoff: datetime,
                             tick_size: Decimal | None = None, tiers: tuple = LEVERAGE_TIERS) -> dict:
    """The modelled liquidations still outstanding at the cutoff, from closed hourly data only."""
    cutoff_ms = int(cutoff.timestamp() * 1000)
    totals = {}
    for row in open_interest:
        value = float(row["sumOpenInterest"])
        if row["symbol"] != symbol or not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid open interest observation")
        stamp = int(row["timestamp"])
        if stamp <= cutoff_ms:
            totals[stamp] = value
    bars = {}
    for candle in candles:
        opened = int(datetime.fromisoformat(candle["open_time"]).timestamp() * 1000)
        if datetime.fromisoformat(candle["close_time"]) <= cutoff:
            bars[opened] = candle
    # [side, liquidation price, coins, entry price]
    positions: list[list] = []
    hours, first, last = 0, None, None
    for before, after in pairwise(sorted(totals)):
        candle = bars.get(before)
        if after - before != HOUR_MS or candle is None:
            continue
        high, low = float(candle["high"]), float(candle["low"])
        liquidated = {"long": 0.0, "short": 0.0}
        kept = []
        for position in positions:
            side, price = position[0], position[1]
            if (side == "long" and low <= price) or (side == "short" and high >= price):
                liquidated[side] += position[2]
            else:
                kept.append(position)
        positions = kept
        change = totals[after] - totals[before]
        if change < 0:
            for side in ("long", "short"):
                residual = max(0.0, -change - liquidated[side])
                factor = max(0.0, 1 - residual / totals[before])
                for position in positions:
                    if position[0] == side:
                        position[2] *= factor
        elif change > 0:
            volume, quote = float(candle["volume"]), float(candle.get("quote_volume") or 0)
            entry = quote / volume if quote > 0 and volume > 0 else (
                (high + low + float(candle["close"])) / 3)
            for leverage, weight in tiers:
                coins = change * weight
                positions.append(["long", entry * (1 - 1 / leverage + MAINTENANCE_RATE), coins, entry])
                positions.append(["short", entry * (1 + 1 / leverage - MAINTENANCE_RATE), coins, entry])
        hours += 1
        first = before if first is None else first
        last = after
    if hours < MIN_HOURS:
        return {"version": VERSION, "status": "unavailable", "reason": "insufficient_history",
                "hours": hours}
    price = float(reference)
    # Price may already have traded through some levels during the unfinished hour.
    positions = [p for p in positions if (p[0] == "long" and p[1] < price) or
                 (p[0] == "short" and p[1] > price)]
    width = price * float(BIN_PCT) / 100
    limit = price * float(RANGE_PCT) / 100
    bins: dict[int, dict] = {}
    for side, level, coins, entry in positions:
        if abs(level - price) > limit:
            continue
        slot = bins.setdefault(math.floor(level / width), {"long": 0.0, "short": 0.0})
        slot[side] += coins * entry

    def edges(index: int) -> tuple[str, str]:
        return _price(index * width, tick_size, False), _price((index + 1) * width, tick_size, True)

    def clusters(side: str, within_pct: float | None = None, count: int = CLUSTER_LIMIT) -> list[dict]:
        near = (lambda index: abs((index + 0.5) * width / price - 1) * 100 <= within_pct
                ) if within_pct else (lambda _index: True)
        rows = sorted(((index, slot[side]) for index, slot in bins.items()
                       if slot[side] > 0 and near(index)),
                      key=lambda row: row[1], reverse=True)[:count]
        whole = sum(slot[side] for slot in bins.values()) or 1
        return [{"low": edges(index)[0], "high": edges(index)[1], "notional_usd": _usd(value),
                 "distance_pct": _pct(((index + 0.5) * width / price - 1) * 100),
                 "share_of_side_pct": _pct(value / whole * 100)} for index, value in rows]

    within = {}
    for distance in DISTANCES_PCT:
        floor, ceiling = price * (1 - distance / 100), price * (1 + distance / 100)
        within[f"{distance}pct"] = {
            "long_usd": _usd(sum(p[2] * p[3] for p in positions if p[0] == "long" and p[1] >= floor)),
            "short_usd": _usd(sum(p[2] * p[3] for p in positions if p[0] == "short" and p[1] <= ceiling)),
        }
    modelled = sum(p[2] for p in positions) / 2
    latest_oi = totals[last]
    return {
        "version": VERSION, "status": "available" if hours >= HOURS - 24 else "partial",
        "period": PERIOD, "hours": hours,
        "from": datetime.fromtimestamp(first / 1000, UTC).isoformat(),
        "to": datetime.fromtimestamp(last / 1000, UTC).isoformat(),
        "reference_price": str(reference), "bin_pct": str(BIN_PCT), "range_pct": str(RANGE_PCT),
        "long_clusters_below": clusters("long"), "short_clusters_above": clusters("short"),
        "near_pct": NEAR_PCT,
        "near_long_clusters": clusters("long", NEAR_PCT, NEAR_LIMIT),
        "near_short_clusters": clusters("short", NEAR_PCT, NEAR_LIMIT),
        "cumulative_within": within,
        "modelled_share_of_open_interest_pct": _pct(modelled / latest_oi * 100),
        "histogram": [[*edges(index), _usd(slot["long"]), _usd(slot["short"])]
                      for index, slot in sorted(bins.items())],
    }


def fetch_liquidation_map(market_id: str, cutoff: datetime, reference: Decimal,
                          tick_size: Decimal | None = None,
                          hourly_candles: list[dict] | None = None) -> dict:
    """Read the last 500 hours of open interest; reuse the snapshot's hourly candles when it has them."""
    if os.getenv("TRADE_LIQUIDATION_MAP_ENABLED", "1") != "1":
        return unavailable(market_id, cutoff, "disabled")
    try:
        symbol = symbol_for(market_id)
        response = httpx.get(f"{BASE_URL}/futures/data/openInterestHist", params={
            "symbol": symbol, "period": PERIOD, "limit": HOURS,
            "endTime": int(cutoff.timestamp() * 1000)}, timeout=8)
        response.raise_for_status()
        candles = (hourly_candles if hourly_candles is not None and len(hourly_candles) >= HOURS
                   else fetch_candles(market_id, PERIOD, limit=HOURS))
        tiers = EQUITY_LEVERAGE_TIERS if is_us_equity(market_id) else LEVERAGE_TIERS
        result = estimate_liquidation_map(candles, response.json(), symbol, reference, cutoff,
                                          tick_size, tiers)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError, OverflowError):
        return unavailable(market_id, cutoff, "source_unavailable_or_invalid")
    return {"market_id": market_id, "as_of": cutoff.isoformat(), "source": SOURCE,
            "assumptions": assumptions(tiers), "interpretation_limit": INTERPRETATION_LIMIT, **result}


def compact_liquidation_map(data: dict | None) -> dict | None:
    """The model reads clusters and totals; the per-bin histogram is for the chart."""
    if not isinstance(data, dict):
        return data
    return {key: value for key, value in data.items() if key != "histogram"}
