"""A follow-up's fresh snapshot: the same evidence an analysis computes, on new public data."""

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trade_helper import discussion_market as live
from trade_helper import discussion_snapshot as snap
from trade_helper.discussion_context import build_discussion_input
from trade_helper.support_levels_v3 import historical_levels_v3

from .test_discussion_market import CONTEXT, NOW, mock_http
from .test_support_levels import candles_with_swings

MARKET = "binance:perp:BTCUSDT"


def _four_hour(rows):
    start = datetime.fromisoformat(rows[0]["open_time"])
    return [row | {"open_time": (start + timedelta(hours=4 * i)).isoformat(),
                   "close_time": (start + timedelta(hours=4 * i + 4)).isoformat()}
            for i, row in enumerate(rows)]


def _inputs(secondary=True):
    candles = candles_with_swings(100)
    observed = datetime.fromisoformat(candles[-1]["close_time"]) + timedelta(minutes=5)
    quote = {"price": "100", "mark_price": "100.1", "index_price": "100", "last_funding_rate": "0.0001",
             "next_funding_time": (observed + timedelta(hours=3)).isoformat(),
             "observed_at": observed.isoformat(), "binance_symbol": "BTCUSDT"}
    calculated = historical_levels_v3(candles, "1h", Decimal(100), Decimal("0.1"), MARKET)
    context = {
        "levels": calculated["levels"],
        "recently_invalidated_levels": calculated["recently_invalidated_levels"],
        "level_metadata": {"source_time": calculated["as_of"], "sweep_rule": calculated["sweep_rule"],
                           "invalidation_rule": calculated["invalidation_rule"]},
        "current_candle": {"status": "quote_only"},
        "secondary_timeframe_context": {"timeframe": "4h", "status": "available", "data": calculated}
        if secondary else {"timeframe": "4h", "status": "unavailable", "data": None},
    }
    return context, {"candles": candles, "secondary_candles": _four_hour(candles[-80:]) if secondary else None,
                     "secondary_timeframe": "4h", "quote": quote, "tick_size": Decimal("0.1"),
                     "forming_candle": None}


@pytest.fixture
def sources(monkeypatch):
    calls = {}

    def record(name, value, delay=0.0):
        def source(*args, **_kwargs):
            calls[name] = args
            time.sleep(delay)
            return value
        return source

    monkeypatch.setattr(snap, "collect_market_context", lambda market, timeframe: _inputs())
    monkeypatch.setattr(snap, "fetch_derivatives_context", record("derivatives", {
        "status": "available", "timeframes": {"1h": {"status": "available", "observations": [1, 2]}},
        "positioning": {"period": "1h", "status": "available",
                        "global_accounts": {"status": "available", "observations": [1]}}}))
    monkeypatch.setattr(snap, "liquidation_context", record("liquidations", {"status": "available"}))
    monkeypatch.setattr(snap, "fetch_liquidation_map", record("map", {
        "status": "available", "near_long_clusters": [], "histogram": [["1", "2", "3", "4"]]}))
    monkeypatch.setattr(snap, "fund_flows_context", record("flows", {"version": "fund_flows_context_v3"}))
    return calls


def test_snapshot_recomputes_zones_indicators_and_reads_every_source_compactly(sources) -> None:
    result = snap.build_discussion_snapshot(MARKET, "1h", 5)
    assert result["status"] == "available" and result["errors"] == {}
    assert result["version"] == "discussion_market_snapshot_v1"
    assert result["reference_price"] == "100" and result["quote"]["last_funding_rate"] == "0.0001"
    primary = result["support_resistance"]["1h"]
    assert primary["status"] == "available" and primary["levels"]
    assert len(primary["levels"]) <= snap.ZONE_LIMIT
    zone = primary["levels"][0]
    assert {"liquidity_beyond", "sweep_count", "sweep_evidence"} <= zone.keys()
    assert "touch_evidence" not in zone and "break_evidence" not in zone
    assert primary["sweep_rule"].startswith("trade_beyond_outer_edge")
    assert result["support_resistance"]["4h"]["status"] == "available"
    indicators = result["indicators"]["1h"]
    assert indicators["status"] == "available"
    assert {"rsi", "bollinger", "rolling_vwap"} <= indicators["indicators"].keys()
    assert indicators["metrics"]["candle_count"] == 100
    assert result["order_flow"]["primary"]["timeframe"] == "1h"
    assert "observations" not in result["derivatives_context"]["timeframes"]["1h"]
    assert "observations" not in result["derivatives_context"]["positioning"]["global_accounts"]
    assert "histogram" not in result["liquidation_map"]
    assert result["fund_flows_context"] == {"version": "fund_flows_context_v3"}
    # The hourly candles are reused for the map; the tick size rounds its bands.
    assert sources["map"][3] == Decimal("0.1") and len(sources["map"][4]) == 100
    assert result["not_refreshed"] == ["larger_context_timeframes", "market_reference", "positions",
                                       "macro", "news"]
    json.dumps(result, allow_nan=False)


def test_a_late_or_failing_source_is_reported_not_filled(sources, monkeypatch) -> None:
    def slow(*_args, **_kwargs):
        time.sleep(1)
        return {"version": "fund_flows_context_v3"}

    def broken(*_args, **_kwargs):
        raise RuntimeError("upstream payload with secrets")

    monkeypatch.setattr(snap, "fund_flows_context", slow)
    monkeypatch.setattr(snap, "liquidation_context", broken)
    started = time.monotonic()
    result = snap.build_discussion_snapshot(MARKET, "1h", 0.3)
    assert time.monotonic() - started < 0.9
    assert result["status"] == "partial"
    assert result["errors"] == {"fund_flows_context": "TIMEOUT", "liquidation_context": "SOURCE_UNAVAILABLE"}
    assert result["fund_flows_context"] is None and result["liquidation_context"] is None
    assert "secrets" not in json.dumps(result)


def test_market_data_failure_makes_the_whole_snapshot_unavailable(monkeypatch) -> None:
    def failing(*_args):
        raise snap.MarketContextError("CANDLES_STALE", "stale")

    monkeypatch.setattr(snap, "collect_market_context", failing)
    assert snap.build_discussion_snapshot(MARKET, "1h", 5) == {
        "version": "discussion_market_snapshot_v1", "status": "unavailable", "reason": "CANDLES_STALE"}


def test_missing_secondary_candles_mark_only_that_timeframe_unavailable(sources, monkeypatch) -> None:
    monkeypatch.setattr(snap, "collect_market_context", lambda *_: _inputs(secondary=False))
    result = snap.build_discussion_snapshot(MARKET, "1h", 5)
    assert result["support_resistance"]["4h"] == {"status": "unavailable"}
    assert result["indicators"]["4h"] == {"status": "unavailable", "timeframe": "4h"}
    assert result["indicators"]["1h"]["status"] == "available"


def test_an_indicator_failure_marks_only_that_timeframe(sources, monkeypatch) -> None:
    context, inputs = _inputs()
    inputs["secondary_candles"] = candles_with_swings(100)  # hourly bars labelled 4h
    monkeypatch.setattr(snap, "collect_market_context", lambda *_: (context, inputs))
    result = snap.build_discussion_snapshot(MARKET, "1h", 5)
    assert result["indicators"]["4h"] == {"status": "unavailable", "timeframe": "4h",
                                          "reason": "calculation_failed"}
    assert result["support_resistance"]["1h"]["status"] == "available"


@pytest.fixture
def observation(monkeypatch):
    monkeypatch.setattr(live, "_now", lambda: NOW)
    mock_http(monkeypatch)


def test_live_market_v2_carries_the_snapshot_beside_the_quick_observation(observation, monkeypatch) -> None:
    seen = []

    def build(market_id, timeframe, timeout):
        seen.append((market_id, timeframe, timeout))
        return {"version": "discussion_market_snapshot_v1", "status": "available",
                "not_refreshed": list(snap.NOT_REFRESHED)}

    monkeypatch.setattr(live, "build_discussion_snapshot", build)
    result = live.fetch_discussion_market(CONTEXT, timeout=60, snapshot=True)
    assert seen == [(MARKET, "1h", live.SNAPSHOT_MAX_SECONDS)]
    assert result["version"] == "discussion_live_market_v2"
    assert result["quote"]["price"] == "101.12345678901234567890123456789"
    assert result["snapshot"]["status"] == "available"
    assert result["not_refreshed"] == list(snap.NOT_REFRESHED)
    assert "snapshot" not in live.fetch_discussion_market(CONTEXT)


def test_a_slow_snapshot_ends_at_its_budget_and_keeps_the_old_not_refreshed_list(observation, monkeypatch) -> None:
    def build(*_args):
        time.sleep(1.5)
        return {"status": "available"}

    monkeypatch.setattr(live, "build_discussion_snapshot", build)
    started = time.monotonic()
    result = live.fetch_discussion_market(CONTEXT, timeout=0.4, snapshot=True)
    assert time.monotonic() - started < 1.2
    assert result["snapshot"] == {"version": "discussion_market_snapshot_v1", "status": "unavailable",
                                  "reason": "TIMEOUT"}
    assert result["not_refreshed"] == list(live.NOT_REFRESHED)


def test_history_keeps_an_old_reply_quote_but_not_its_snapshot() -> None:
    old = {"version": "discussion_live_market_v2", "quote": {"price": "99"},
           "snapshot": {"status": "available", "support_resistance": {"1h": {"levels": ["big"]}}}}
    current = {"version": "discussion_live_market_v2", "quote": {"price": "101"},
               "snapshot": {"status": "available", "support_resistance": {"1h": {"levels": ["fresh"]}}}}
    supplied = json.loads(build_discussion_input(
        {"subject": {"type": "analysis"}, "live_market": current},
        [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1", "live_market": old},
         {"role": "user", "content": "q2"}]))
    history = supplied["conversation"][1]["live_market"]
    assert history["quote"] == {"price": "99"} and history["snapshot"] == "omitted_from_history"
    assert supplied["live_market"]["snapshot"]["support_resistance"]["1h"]["levels"] == ["fresh"]
    assert datetime.now(UTC)  # the projection never needs the wall clock


@pytest.mark.parametrize("locale", ["en-US", "zh-TW"])
def test_strategy_and_discussion_prompts_explain_sweeps_the_map_and_the_snapshot(locale) -> None:
    from trade_helper.prompts import resolve_prompt

    strategy = resolve_prompt("strategy_market", prompt_locale=locale, response_locale=locale)
    discussion = resolve_prompt("discussion", prompt_locale=locale, response_locale=locale)
    for text in (strategy.instructions, discussion.instructions):
        assert "sweep_evidence" in text and "liquidity_beyond" in text and "liquidation_map" in text
    assert "snapshot.as_of" in discussion.instructions
    assert "omitted_from_history" in discussion.instructions
    assert "snapshot.as_of" not in strategy.instructions
