import pytest


@pytest.fixture
def sqlite_db(local_preferences_are_isolated):
    """The autouse fixture already assigns a private SQLite file per test."""


@pytest.fixture
def pg_schema(sqlite_db):
    """Compatibility fixture name while domain tests migrate to SQLite."""


@pytest.fixture(autouse=True)
def local_preferences_are_isolated(monkeypatch, tmp_path):
    # Tests must not inherit the user's selected model provider or alter desktop
    # preferences. An accidental Codex invocation would consume real allowance.
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "db" / "trade_helper.sqlite3"))
    monkeypatch.setenv("APP_DESKTOP", "0")


@pytest.fixture(autouse=True)
def market_catalog_is_offline(monkeypatch, local_preferences_are_isolated):
    """Existing market tests use verified fixture metadata, never the live catalog."""
    import os
    import time
    from pathlib import Path

    from trade_helper import market_catalog

    payload = {"symbols": [
        {"symbol": f"{base}USDT", "baseAsset": base, "quoteAsset": "USDT",
         "marginAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL",
         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"}]}
        for base in ("BTC", "ETH", "SOL", "ADA", "SUI")
    ]}
    snapshot = market_catalog._parse_exchange_info(payload, time.time())
    # Tests that point APP_DATA_DIR elsewhere miss the cache below; serve the
    # same fixture instead of reaching Binance, which also blocks CI regions.
    live_get = market_catalog.httpx.get

    def offline_exchange_info(url, *args, **kwargs):
        if str(url).endswith("/fapi/v1/exchangeInfo"):
            return market_catalog.httpx.Response(200, json=payload, request=market_catalog.httpx.Request("GET", url))
        return live_get(url, *args, **kwargs)

    monkeypatch.setattr(market_catalog.httpx, "get", offline_exchange_info)
    monkeypatch.setattr(market_catalog, "_cache", snapshot)
    monkeypatch.setattr(market_catalog, "_cache_path",
                        Path(os.environ["APP_DATA_DIR"]).resolve() / "market_catalog.json")
    monkeypatch.setattr(market_catalog, "_last_failure_at", 0.0)
    original = market_catalog.MARKETS[:]
    market_catalog.MARKETS[:] = [dict(row) for row in snapshot.markets]
    yield
    market_catalog.MARKETS[:] = original


@pytest.fixture(autouse=True)
def no_public_derivatives_network_in_unit_tests(monkeypatch):
    from trade_helper.timeframes import higher_timeframes

    monkeypatch.setenv('TRADE_DERIVATIVES_CONTEXT_ENABLED', '0')
    # BTC/ETH reference candles are public network reads too; dedicated tests enable them.
    monkeypatch.setenv('TRADE_MARKET_REFERENCE_ENABLED', '0')
    # Fund flows come from the txinTrade cloud; dedicated tests serve them from a fake.
    monkeypatch.setenv('TRADE_FUND_FLOWS_ENABLED', '0')
    # Tests never let a model search the web.
    monkeypatch.setenv('TRADE_WEB_SEARCH_ENABLED', '0')

    def unavailable_higher(_market, timeframe='1h', *, context_candles=None):
        frames = higher_timeframes(timeframe)
        return {frame: {'candles': context_candles or [], 'reason': None}
                if frame == frames[0] and context_candles else
                {'candles': [], 'reason': 'test_not_provided'} for frame in frames}

    monkeypatch.setattr('trade_helper.worker.fetch_higher_timeframe_candles', unavailable_higher)


@pytest.fixture(autouse=True)
def no_live_macro_model_from_existing_worker_tests(monkeypatch):
    # Macro-specific tests replace this with isolated evidence and a fake model.
    # Adding a shared optional macro stage must not authorize a real account call
    # in pre-existing worker tests that only mock the trading Agent.
    monkeypatch.setattr('trade_helper.worker.ensure_macro_interpretation', lambda cutoff, **_kwargs: {
        'status': 'insufficient', 'stale': False, 'interpretation': None,
        'dataset_version': 'test-empty-macro', 'as_of': cutoff.isoformat(),
        'evidence_count': 0, 'coverage': {}, 'needs_update': False, 'cached': False, 'error': None,
    })


@pytest.fixture(autouse=True)
def no_live_discussion_market_from_existing_unit_tests(monkeypatch):
    # New worker-side quote refreshes must not open public HTTP in older tests.
    # Dedicated tests replace this stub with explicit isolated market fixtures.
    from trade_helper import discussions

    monkeypatch.setattr(discussions, "fetch_discussion_market",
                        lambda context, **_kwargs: discussions._live_market_unavailable(
                            context, discussions.utc_now()))
