from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from trade_helper import live_market_context
from trade_helper.agent import agent_context
from trade_helper.api import app
from trade_helper.indicators import support_resistance
from trade_helper.market import MAIN_HISTORY_LIMIT
from trade_helper.support_levels_v3 import historical_levels_v3
from trade_helper.technical_snapshot import prepare_analysis_evidence
from trade_helper.timeframes import advance_candle, candle_close, candle_open

from .test_support_levels import candles_with_swings

MARKET = "binance:perp:BTCUSDT"


def rows(timeframe="1h", count=MAIN_HISTORY_LIMIT, *, now=None):
    now = now or datetime.now(UTC)
    end = candle_open(now, timeframe)
    data = candles_with_swings(count)
    for index, row in enumerate(data):
        opened = advance_candle(end, timeframe, -(count - index))
        row["open_time"] = opened.isoformat()
        row["close_time"] = candle_close(opened, timeframe).isoformat()
    return data


@pytest.fixture
def feeds(monkeypatch):
    now = datetime.now(UTC)
    primary = rows(now=now)
    secondary = rows("4h", now=now)
    opened = datetime.fromisoformat(primary[-1]["close_time"]) + timedelta(milliseconds=1)
    forming = {
        "open_time": opened.isoformat(),
        "close_time": (opened + timedelta(hours=1) - timedelta(milliseconds=1)).isoformat(),
        "fetched_at": now.isoformat(), "open": "100", "high": "110", "low": "90",
        "close": "100", "volume": "20",
    }
    quote = {"price": "100", "mark_price": "100", "observed_at": now.isoformat()}
    calls = []

    def candles(market_id, timeframe, *, limit):
        assert market_id == MARKET and limit == MAIN_HISTORY_LIMIT
        calls.append((timeframe, limit))
        return primary if timeframe == "1h" else secondary if timeframe == "4h" else rows(timeframe, now=now)

    monkeypatch.setattr(live_market_context, "_now", lambda: now)
    monkeypatch.setattr(live_market_context, "fetch_candles", candles)
    monkeypatch.setattr(live_market_context, "fetch_forming_candle", lambda *_: forming)
    monkeypatch.setattr(live_market_context, "fetch_quote", lambda *_: deepcopy(quote))
    monkeypatch.setattr(live_market_context, "fetch_tick_size", lambda *_: Decimal("0.01"))
    return {"primary": primary, "secondary": secondary, "forming": forming,
            "quote": quote, "calls": calls, "now": now}


def test_chart_context_is_fresh_precomputed_v3_and_matches_agent_evidence(feeds):
    with TestClient(app) as client:
        response = client.get("/api/v1/market-context", params={"market_id": MARKET})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["history"]["closed_candle_count"] == MAIN_HISTORY_LIMIT == 1000
        assert body["history"]["status"] == "complete"
        assert body["candles"] == feeds["primary"][-300:]
        assert len(body["chart_candles"]) == 301
        assert all(row["closed"] for row in body["chart_candles"][:-1])
        assert body["chart_candles"][-1]["closed"] is False
        assert body["forming_candle"] == feeds["forming"]
        assert body["current_candle"]["confirmation"] == "unclosed"
        assert body["secondary_timeframe_context"]["status"] == "available"
        assert sorted(feeds["calls"]) == [("1h", 1000), ("4h", 1000)]
        expected = support_resistance(feeds["primary"], feeds["quote"] | {
            "tick_size": "0.01", "forming_candle": feeds["forming"]},
                                      {"timeframe": "1h", "_request": {"market_id": MARKET},
                                       "_context_candles": feeds["secondary"]})
        # Liquidity fields are optional on the analysis endpoint; structural
        # boundaries and lifecycle metadata match exactly without book evidence.
        for actual, agent in zip(body["levels"], expected["levels"], strict=True):
            assert all(agent[key] == value for key, value in actual.items())
        assert body["level_metadata"]["source_time"] == feeds["primary"][-1]["close_time"]
        assert body["level_metadata"]["reference_time"] == body["as_of"]
        assert len(body["market_snapshot_sha256"]) == 64
        original = body["market_snapshot_sha256"]
        feeds["quote"]["price"] = "101"
        refreshed = client.get("/api/v1/market-context", params={"market_id": MARKET, "refresh": "1"})
        assert refreshed.status_code == 200
        assert refreshed.json()["quote"]["price"] == "101"
        assert refreshed.json()["market_snapshot_sha256"] != original


@pytest.mark.parametrize("kind", ["support", "resistance"])
def test_inside_or_intrabar_crossing_does_not_hide_or_reverse_original_zone(feeds, kind):
    initial = historical_levels_v3(feeds["primary"], "1h", Decimal(100), Decimal("0.01"), MARKET)
    original = next(zone for zone in initial["levels"] if zone["kind"] == kind)
    with TestClient(app) as client:
        for price, state in ((original["center"], "inside"),
                             (original["low"], "inside"), (original["high"], "inside"),
                             (str(Decimal(original["low"]) - Decimal("0.01")),
                              "intrabar_crossed" if kind == "support" else "not_testing"),
                             (str(Decimal(original["high"]) + Decimal("0.01")),
                              "intrabar_crossed" if kind == "resistance" else "not_testing")):
            feeds["quote"]["price"] = price
            # An unfinished candle crossing the zone must not invalidate it.
            feeds["forming"]["close"] = price
            body = client.get("/api/v1/market-context", params={"market_id": MARKET}).json()
            zone = next(item for item in body["levels"] if item["id"] == original["id"])
            assert (zone["low"], zone["high"], zone["kind"], zone["revision"]) == (
                original["low"], original["high"], kind, original["revision"])
            assert zone["zone_state"] == "active"
            assert zone["consecutive_closes_beyond"] == 0
            assert zone["price_test_state"] == state
            if state == "inside":
                assert original["id"] in body["level_price_context"]["inside_zone_ids"]
            assert not zone["role_reversal_confirmed"]


def test_one_closed_crossing_stays_active_and_two_are_context_only_even_quote_returns_inside(feeds):
    original = next(zone for zone in historical_levels_v3(
        feeds["primary"], "1h", Decimal(100), Decimal("0.01"), MARKET)["levels"]
                    if zone["kind"] == "resistance")
    feeds["primary"][-1].update(open="100", high="108", low="99", close="107")
    feeds["quote"]["price"] = original["center"]
    with TestClient(app) as client:
        first = client.get("/api/v1/market-context", params={"market_id": MARKET}).json()
        zone = next(item for item in first["levels"] if item["id"] == original["id"])
        assert zone["zone_state"] == "active" and zone["consecutive_closes_beyond"] == 1
        assert zone["price_relation"] == "inside"
        assert original["id"] in first["level_price_context"]["inside_zone_ids"]
        feeds["primary"][-2].update(open="100", high="108", low="99", close="107")
        second = client.get("/api/v1/market-context", params={"market_id": MARKET}).json()
        assert all(item["id"] != original["id"] for item in second["levels"])
        invalidated = next(item for item in second["recently_invalidated_levels"]
                           if item["id"] == original["id"])
        assert invalidated["zone_state"] == "invalidated"
        assert invalidated["price_relation"] == "inside"
        assert original["id"] not in second["level_price_context"]["inside_zone_ids"]


@pytest.mark.parametrize("case,code", [
    ("few", "CANDLES_INSUFFICIENT"), ("gap", "CANDLES_INVALID"),
    ("duplicate", "CANDLES_INVALID"), ("future", "CANDLES_INVALID"),
    ("stale", "CANDLES_STALE"), ("quote_stale", "QUOTE_STALE"),
    ("quote_future", "QUOTE_INVALID"), ("quote_nan", "QUOTE_INVALID"),
])
def test_unusable_market_evidence_has_safe_specific_error_without_fake_levels(feeds, case, code):
    data = feeds["primary"]
    if case == "few":
        del data[:-59]
    elif case == "gap":
        del data[50]
    elif case == "duplicate":
        data[51] = dict(data[50])
    elif case == "future":
        opened = datetime.fromisoformat(data[-1]["open_time"]) + timedelta(hours=1)
        data.append(data[-1] | {"open_time": opened.isoformat(),
                               "close_time": (opened + timedelta(hours=1) - timedelta(milliseconds=1)).isoformat()})
    elif case == "stale":
        del data[-5:]
    elif case == "quote_stale":
        feeds["quote"]["observed_at"] = (feeds["now"] - timedelta(minutes=4)).isoformat()
    elif case == "quote_future":
        feeds["quote"]["observed_at"] = (feeds["now"] + timedelta(minutes=1)).isoformat()
    elif case == "quote_nan":
        feeds["quote"]["price"] = "NaN"
    with TestClient(app) as client:
        result = client.get("/api/v1/market-context", params={"market_id": MARKET})
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["detail"]["code"] == code
    assert "levels" not in result.json()


def test_partial_history_is_marked_and_unavailable_secondary_does_not_fabricate_context(feeds):
    del feeds["primary"][:-80]
    del feeds["secondary"][:-20]
    with TestClient(app) as client:
        result = client.get("/api/v1/market-context", params={"market_id": MARKET}).json()
    assert result["history"]["closed_candle_count"] == 80
    assert result["history"]["status"] == "partial"
    assert result["secondary_timeframe_context"]["status"] == "unavailable"
    assert result["secondary_timeframe_context"]["reason"] == "CANDLES_INSUFFICIENT"
    assert result["secondary_timeframe_context"]["data"] is None


def test_forming_feed_failure_still_returns_quote_and_closed_evidence(feeds, monkeypatch):
    def unavailable(*_args):
        raise httpx.ReadTimeout("private exchange request detail")
    monkeypatch.setattr(live_market_context, "fetch_forming_candle", unavailable)
    with TestClient(app) as client:
        result = client.get("/api/v1/market-context", params={"market_id": MARKET}).json()
    assert result["forming_candle"] is None
    assert result["current_candle"]["status"] == "quote_only"
    assert len(result["chart_candles"]) == 300
    assert all(row["closed"] for row in result["chart_candles"])


def test_network_error_is_not_leaked_and_unsupported_timeframe_cannot_fetch(feeds, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise httpx.ReadTimeout("secret upstream detail")
    monkeypatch.setattr(live_market_context, "fetch_candles", unavailable)
    with TestClient(app) as client:
        result = client.get("/api/v1/market-context", params={"market_id": MARKET})
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "MARKET_DATA_UNAVAILABLE"
        assert "secret" not in result.text
        assert client.get("/api/v1/market-context", params={"market_id": MARKET, "timeframe": "1m"}).status_code == 422
        assert client.get("/api/v1/market-context", params={"market_id": "binance:perp:FAKEUSDT"}).status_code == 422


@pytest.mark.parametrize("case", ["bad_period", "closed"])
def test_bad_or_already_closed_forming_row_is_never_chart_current_candle(feeds, case):
    if case == "bad_period":
        feeds["forming"]["close_time"] = (datetime.fromisoformat(
            feeds["forming"]["close_time"]) + timedelta(hours=3)).isoformat()
    else:
        feeds["forming"].update(feeds["primary"][-1])
    with TestClient(app) as client:
        result = client.get("/api/v1/market-context", params={"market_id": MARKET}).json()
    assert result["forming_candle"] is None
    assert len(result["chart_candles"]) == 300


def test_1000_candles_produce_longer_summary_but_bounded_agent_ohlcv_tables(feeds):
    request = {"market_id": MARKET, "timeframe": "1h", "leverage": 5}
    quote = feeds["quote"] | {"tick_size": "0.01", "forming_candle": feeds["forming"]}
    prepared = prepare_analysis_evidence(request, feeds["primary"], quote, feeds["secondary"])
    context = agent_context(request, feeds["primary"], quote, feeds["secondary"], prepared_trace=prepared)
    frames = context["precomputed_evidence"]["technical_snapshot"]["timeframes"]
    assert frames["1h"]["candle_count"] == frames["4h"]["candle_count"] == 1000
    assert len(frames["1h"]["recent_closed_candles"]["rows"]) == 72
    assert len(frames["4h"]["recent_closed_candles"]["rows"]) == 60
    for frame in frames.values():
        if frame["status"] != "available":
            continue
        month = next(item for item in frame["price_action_summary"] if item["window"] == "30d")
        assert month["status"] == "complete"
    assert "candles" not in context and "context_candles" not in context


def test_current_candle_close_boundary_is_not_unconfirmed_for_chart_or_agent(feeds, monkeypatch):
    cutoff = datetime.fromisoformat(feeds["forming"]["close_time"])
    feeds["quote"]["observed_at"] = cutoff.isoformat()
    monkeypatch.setattr(live_market_context, "_now", lambda: cutoff)
    request = {"market_id": MARKET, "timeframe": "1h", "leverage": 5}
    agent = agent_context(request, feeds["primary"],
                          feeds["quote"] | {"forming_candle": feeds["forming"]}, feeds["secondary"])
    with TestClient(app) as client:
        chart = client.get("/api/v1/market-context", params={"market_id": MARKET}).json()
    assert agent["current_candle"] == chart["current_candle"]
    assert chart["current_candle"]["status"] == "quote_only"
    assert chart["forming_candle"] is None


def test_four_hour_chart_uses_four_hour_lookback_and_forming_period(feeds):
    opened = datetime.fromisoformat(feeds["secondary"][-1]["close_time"]) + timedelta(milliseconds=1)
    feeds["forming"]["open_time"] = opened.isoformat()
    feeds["forming"]["close_time"] = (opened + timedelta(hours=4) - timedelta(milliseconds=1)).isoformat()
    with TestClient(app) as client:
        response = client.get("/api/v1/market-context", params={"market_id": MARKET, "timeframe": "4h"})
    assert response.status_code == 200
    body = response.json()
    assert body["history"]["duration_hours"] == 4000
    assert body["candles"] == feeds["secondary"][-300:]
    assert body["current_candle"]["timeframe"] == "4h"
    assert body["current_candle"]["status"] == "available"
    assert all(zone["timeframe"] == "4h" for zone in body["levels"])
    assert body["secondary_timeframe_context"]["timeframe"] == "12h"
    assert body["secondary_timeframe_context"]["history"]["duration_hours"] == 12000
