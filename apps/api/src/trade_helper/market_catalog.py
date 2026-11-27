"""Verified Binance USDT perpetual catalog shared by local API and workers.

Crypto perpetuals and the TradFi perpetuals that track US-listed stocks and ETFs are
listed; each market carries its asset_class so data that only exists for crypto can be
skipped for stocks.

One exchangeInfo response supplies both listed symbols and their price ticks.
Successful snapshots survive restarts; failures never invent a supported market.
"""

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from .config import data_dir
from .platform_process import exclusive_file_lock

BASE_URL = "https://fapi.binance.com"
CACHE_TTL_SECONDS = 15 * 60
MAX_STALE_SECONDS = 24 * 60 * 60
RETRY_DELAY_SECONDS = 30
# contractType and underlyingType per asset class. Other TradFi kinds (Hong Kong, Korean
# and Chinese equities, commodities, FX, pre-IPO) are not supported yet.
ASSET_CLASSES = {("PERPETUAL", None): "crypto", ("TRADIFI_PERPETUAL", "EQUITY"): "us_equity"}
_lock = threading.Lock()
_cache = None
_cache_path = None
_last_failure_at = 0.0

# Compatibility for older official-news importers. This is not an API catalog
# or a validation allowlist. A verified fetch replaces its contents in place.
MARKETS = [
    {"id": f"binance:perp:{base}USDT", "exchange": "Binance", "symbol": f"{base}/USDT",
     "market_type": "linear_perpetual", "settlement_asset": "USDT"}
    for base in ("BTC", "ETH", "SOL", "ADA", "SUI")
]


class MarketCatalogUnavailable(Exception):
    """Safe explanation, without an upstream URL or response body."""

    def __init__(self):
        super().__init__("暫時無法取得幣安合約交易對，請檢查網路後重試")


@dataclass(frozen=True)
class CatalogSnapshot:
    markets: tuple[dict, ...]
    updated_at: str
    fetched_at: float
    status: str = "fresh"

    def find(self, market_id: str) -> dict | None:
        return next((row for row in self.markets if row["id"] == market_id), None)


def valid_market_id_format(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("binance:perp:"):
        return False
    symbol = value[len("binance:perp:"):]
    # Binance also lists Unicode asset names. Membership remains authoritative.
    return (2 <= len(symbol) <= 64 and symbol.endswith("USDT") and
            all(character.isalnum() or character == "_" for character in symbol))


def _parse_exchange_info(payload: object, fetched_at: float) -> CatalogSnapshot:
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise TypeError("Invalid exchange catalog")
    markets, seen = [], set()
    for row in payload["symbols"]:
        if not isinstance(row, dict):
            raise TypeError("Invalid exchange symbol")
        contract = row.get("contractType")
        asset_class = ASSET_CLASSES.get((contract, None) if contract == "PERPETUAL"
                                        else (contract, row.get("underlyingType")))
        if row.get("status") != "TRADING" or asset_class is None or row.get("quoteAsset") != "USDT":
            continue
        symbol, base, quote, margin = (row.get(key) for key in
                                      ("symbol", "baseAsset", "quoteAsset", "marginAsset"))
        market_id = f"binance:perp:{symbol}"
        if (not isinstance(base, str) or not base or symbol != f"{base}{quote}" or
                margin != quote or not valid_market_id_format(market_id) or symbol in seen):
            raise ValueError("Invalid linear perpetual metadata")
        tick = next((rule.get("tickSize") for rule in row.get("filters", [])
                     if isinstance(rule, dict) and rule.get("filterType") == "PRICE_FILTER"), None)
        try:
            tick_value = Decimal(str(tick))
        except InvalidOperation as exc:
            raise ValueError("Invalid price tick") from exc
        if not tick_value.is_finite() or tick_value <= 0:
            raise ValueError("Invalid price tick")
        seen.add(symbol)
        markets.append({"id": market_id, "exchange": "Binance", "symbol": f"{base}/{quote}",
                        "binance_symbol": symbol, "base_asset": base, "quote_asset": quote,
                        "margin_asset": margin, "settlement_asset": margin,
                        "market_type": "linear_perpetual", "tick_size": str(tick_value),
                        "asset_class": asset_class})
    if not markets:
        raise ValueError("No supported perpetual symbols")
    markets.sort(key=lambda market: market["base_asset"])
    return CatalogSnapshot(tuple(markets), datetime.fromtimestamp(fetched_at, UTC).isoformat(),
                           fetched_at)


def _load_disk(path: Path) -> CatalogSnapshot | None:
    try:
        if path.stat().st_size > 5_000_000:
            return None
        saved = json.loads(path.read_text())
        fetched_at = float(saved["fetched_at"])
        if fetched_at > time.time() + 60:
            return None
        # Caches written before stocks were listed hold crypto only.
        rows = [{"symbol": row["binance_symbol"], "baseAsset": row["base_asset"],
                 "quoteAsset": row["quote_asset"], "marginAsset": row["margin_asset"],
                 "status": "TRADING",
                 **({"contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY"}
                    if row.get("asset_class") == "us_equity" else {"contractType": "PERPETUAL"}),
                 "filters": [{"filterType": "PRICE_FILTER", "tickSize": row["tick_size"]}]}
                for row in saved["markets"]]
        return _parse_exchange_info({"symbols": rows}, fetched_at)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def _save_disk(path: Path, snapshot: CatalogSnapshot) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".market-catalog-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as destination:
            json.dump({"version": 1, "fetched_at": snapshot.fetched_at,
                       "markets": snapshot.markets}, destination, ensure_ascii=False)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _process_lock(path: Path):
    # Local workers share the same catalog instead of refreshing once per process.
    with exclusive_file_lock(path.with_suffix(".lock")):
        yield


def _install(snapshot: CatalogSnapshot, path: Path) -> CatalogSnapshot:
    global _cache, _cache_path
    _cache, _cache_path = snapshot, path
    MARKETS[:] = [dict(row) for row in snapshot.markets]
    return snapshot


def get_catalog() -> CatalogSnapshot:
    global _last_failure_at
    path = data_dir() / "market_catalog.json"
    with _lock:
        now = time.time()
        cached = _cache if _cache_path == path else None
        if cached is not None and now - cached.fetched_at < CACHE_TTL_SECONDS:
            return cached
        if cached is not None and now - _last_failure_at < RETRY_DELAY_SECONDS:
            if now - cached.fetched_at <= MAX_STALE_SECONDS:
                return replace(cached, status="stale")
            raise MarketCatalogUnavailable()
        with _process_lock(path):
            disk = _load_disk(path)
            if disk is not None and (cached is None or disk.fetched_at > cached.fetched_at):
                cached = _install(disk, path)
            if cached is not None and now - cached.fetched_at < CACHE_TTL_SECONDS:
                return cached
            try:
                response = httpx.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10)
                response.raise_for_status()
                snapshot = _parse_exchange_info(response.json(), time.time())
            except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
                _last_failure_at = now
                if cached is not None and now - cached.fetched_at <= MAX_STALE_SECONDS:
                    return replace(cached, status="stale")
                raise MarketCatalogUnavailable() from exc
            _last_failure_at = 0.0
            _install(snapshot, path)
            try:
                _save_disk(path, snapshot)
            except OSError:
                pass  # Current verified data remains usable if cache writing fails.
            return snapshot


def market_for(market_id: str) -> dict:
    if not valid_market_id_format(market_id):
        raise ValueError("Unsupported market ID")
    market = get_catalog().find(market_id)
    if market is None:
        raise ValueError("This Binance perpetual market is not currently available")
    return market


def is_us_equity(market_id: str) -> bool:
    """Whether the market tracks a US stock or ETF; unknown markets count as crypto."""
    try:
        return market_for(market_id).get("asset_class") == "us_equity"
    except (MarketCatalogUnavailable, ValueError, KeyError):
        return False


def validate_market_id(market_id: str) -> str:
    market_for(market_id)
    return market_id
