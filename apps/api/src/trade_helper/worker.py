import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import httpx

from .agent import analyze_with_tools
from .analysis import StaleMarketDataError, build_report, validate_market_freshness
from .analysis_progress import ProgressRecorder, evidence_progress, market_progress
from .config import assert_local_mode
from .db import connect, init_db, utc_now
from .derivatives_context import fetch_derivatives_context
from .desktop_updates import (
    finish_desktop_execution,
    register_desktop_execution,
    update_is_draining,
)
from .discussions import run_once as run_discussion_once
from .error_locale import analysis_failure_message, model_error_message, system_error_message
from .events import event_snapshot
from .fund_flows import fund_flows_context, unavailable_fund_flows
from .macro_interpretation import analysis_macro_interpretation, ensure_macro_interpretation
from .market import (
    MAIN_HISTORY_LIMIT,
    fetch_candles,
    fetch_forming_candle,
    fetch_higher_timeframe_candles,
    fetch_order_book,
    fetch_quote,
    fetch_tick_size,
)
from .market_reference import fetch_market_reference
from .model_providers import analysis_timeout_seconds
from .news import news_snapshot
from .news_evidence import build_news_evidence_pack
from .position_advice import compact_positions
from .risk import configured_cost_scenario
from .strategy_engine import other_timeframe
from .support_levels import order_book_evidence
from .support_levels_v3 import VERSION as LEVEL_VERSION
from .technical_snapshot import prepare_analysis_evidence


class ModelAnalysisError(RuntimeError):
    """A safe model failure with a stable code for the UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_LOG = logging.getLogger(__name__)
# Fund flows are context, never a reason to fail or hold up an analysis.
FUND_FLOWS_WAIT_SECONDS = 20


def _required_market_fetch(fetch):
    """Retry a brief exchange outage before failing the analysis without a stale quote."""
    for attempt in range(3):
        try:
            return fetch()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
        except httpx.TransportError:
            if attempt == 2:
                raise
        time.sleep(0.4 * (attempt + 1))


def _macro_at(cutoff: datetime, user_id: str, output_locale: str = "zh-TW") -> dict:
    try:
        return analysis_macro_interpretation(ensure_macro_interpretation(
            cutoff, user_id=user_id, output_locale=output_locale))
    except Exception:  # noqa: BLE001 -- macro cache failures never prevent market strategy analysis
        return {"version": "macro_interpretation_cache_v1", "status": "failed", "stale": False,
                "dataset_version": None, "as_of": cutoff.isoformat(), "evidence_count": 0,
                "coverage": {}, "interpretation": None, "needs_update": True, "cached": False,
                "error": {"code": "MACRO_CONTEXT_UNAVAILABLE",
                          "message": ("The saved macro interpretation is unavailable for this analysis."
                                      if output_locale == "en-US" else
                                      "AI 宏觀解讀暫時無法取得，本次策略未引用已存宏觀解讀。"),
                          "retryable": True}}


def run_once() -> bool:
    with connect() as db:
        if update_is_draining(db):
            return False
        now = utc_now()
        db.execute("UPDATE analyses SET status='queued', phase='retrying' WHERE status='running' AND lease_until < ? AND phase IN ('macro','fetching','calculating')", (now,))
        db.execute("""UPDATE analyses SET status='failed', phase='done',
                   error_code='MODEL_RESULT_UNKNOWN', error_message=CASE
                   WHEN json_extract(request_json,'$.output_locale')='en-US'
                   THEN 'The model request was interrupted. Check usage before starting a new analysis.'
                   ELSE '模型呼叫中斷，請檢查用量後重新分析' END, completed_at=?
                   WHERE status='running' AND lease_until < ? AND phase='model'""", (now, now))
        # BEGIN IMMEDIATE serializes the claim before a second worker can read it.
        row = db.execute("SELECT * FROM analyses WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            db.commit()
            return False
        lease_until = (datetime.now(UTC) + timedelta(seconds=analysis_timeout_seconds() * 2 + 120)).isoformat()
        db.execute("UPDATE analyses SET status='running', phase='macro', started_at=?, lease_until=? WHERE id=?", (utc_now(), lease_until, row["id"]))
        update_execution = register_desktop_execution(db, "analyses", row["id"])
        db.commit()
    job_id = row["id"]
    progress = ProgressRecorder(job_id)
    response_locale = "zh-TW"
    stage = "prompt"
    try:
        request = json.loads(row["request_json"])
        response_locale = request.get("output_locale", "zh-TW")
        from .prompts import resolve_prompt
        from .prompts_artifacts import load_prompt_artifact, save_prompt_artifact

        with connect() as db:
            if row.get("prompt_artifact_id"):
                bundle = load_prompt_artifact(db, row["prompt_artifact_id"])
            else:
                # Old queued jobs predate instruction snapshots. Bind them once
                # with their legacy Chinese response locale, then retain it.
                task = ("strategy_positions" if request["kind"] == "positions"
                        else "strategy_market")
                bundle = resolve_prompt(task, response_locale=request.get("output_locale", "zh-TW"),
                                        inputs=request)
                artifact_id = save_prompt_artifact(db, bundle)
                db.execute("UPDATE analyses SET prompt_artifact_id=? WHERE id=?",
                           (artifact_id, job_id))
        response_locale = bundle.response_locale
        request["output_locale"] = response_locale
        # Generate shared macro interpretation before fetching current prices,
        # so a first-time macro request cannot age the market quote snapshot.
        stage = "macro"
        _macro_at(datetime.now(UTC), row["user_id"], bundle.response_locale)
        with connect() as db:
            db.execute("UPDATE analyses SET phase='fetching' WHERE id=?", (job_id,))
        stage = "candles"
        candles = _required_market_fetch(lambda: fetch_candles(
            request["market_id"], request["timeframe"], limit=MAIN_HISTORY_LIMIT))
        stage = "context_candles"
        context_candles = _required_market_fetch(lambda: fetch_candles(
            request["market_id"], other_timeframe(request["timeframe"]), limit=MAIN_HISTORY_LIMIT))
        stage = "higher_candles"
        higher_candles = fetch_higher_timeframe_candles(
            request["market_id"], request["timeframe"], context_candles=context_candles)
        try:
            forming_candle = fetch_forming_candle(request["market_id"], request["timeframe"])
        except Exception:  # noqa: BLE001 - quote still provides current price if candle feed fails
            forming_candle = None
        stage = "quote"
        quote = _required_market_fetch(lambda: fetch_quote(request["market_id"]))
        progress.record(market=market_progress(request, quote))
        quote["higher_timeframe_candles"] = higher_candles
        quote["forming_candle"] = forming_candle
        stage = "tick_size"
        quote["tick_size"] = str(_required_market_fetch(
            lambda: fetch_tick_size(request["market_id"])))
        quote["cost_scenario"] = configured_cost_scenario()
        stage = "events"
        events = event_snapshot(datetime.fromisoformat(quote["observed_at"]))
        quote["event_risk"] = events["risk"]
        quote["events_status"] = events["status"]
        quote["event_context"] = events
        cutoff = datetime.fromisoformat(quote["observed_at"])
        # Fund flows come from the cloud in one cached request; fetch them while the
        # derivatives and market reference are read, so they add no waiting.
        flows_pool = ThreadPoolExecutor(max_workers=1)
        fund_flows = flows_pool.submit(fund_flows_context, request["market_id"])
        flows_pool.shutdown(wait=False)
        stage = "derivatives"
        quote["derivatives_context"] = fetch_derivatives_context(
            request["market_id"], cutoff, request["timeframe"])
        stage = "market_reference"
        quote["market_reference"] = fetch_market_reference(
            request["market_id"], request["timeframe"], candles, context_candles, cutoff)
        stage = "fund_flows"
        try:
            quote["fund_flows_context"] = fund_flows.result(timeout=FUND_FLOWS_WAIT_SECONDS)
        except TimeoutError:
            quote["fund_flows_context"] = unavailable_fund_flows(request["market_id"])
        stage = "news"
        news = news_snapshot(cutoff, request["market_id"])
        news["evidence_pack"] = build_news_evidence_pack(cutoff, request["market_id"])
        quote["news_context"] = news
        quote["news_risk"] = news["risk"]
        # Check exactly the frozen quote cutoff. Usually the same version is
        # reused; a newly published/revised value is interpreted once if needed.
        quote["macro_interpretation"] = _macro_at(cutoff, row["user_id"], bundle.response_locale)
        try:
            book = fetch_order_book(request["market_id"])
        except Exception:  # noqa: BLE001 - order-book context is optional, never fabricated
            book = None
        stage = "snapshot"
        positions = json.loads(row["positions_json"])
        snapshot_json = json.dumps({"candles": candles, "context_candles": context_candles,
                                    "quote": quote, "order_book": book, "events": events,
                                    "news": news, "positions": compact_positions(positions)},
                                   sort_keys=True, separators=(",", ":"))
        quote["snapshot_hash"] = sha256(snapshot_json.encode()).hexdigest()
        quote["order_book_evidence"] = order_book_evidence(book, Decimal(quote["price"]))
        with connect() as db:
            db.execute("UPDATE analyses SET phase='calculating', snapshot_json=? WHERE id=?",
                       (snapshot_json, job_id))
            db.commit()
        stage = "preparation"
        validate_market_freshness(request, candles, quote, context_candles)
        prepared = prepare_analysis_evidence(request, candles, quote, context_candles, positions)
        try:
            progress.record(evidence=evidence_progress(request, prepared))
        except (KeyError, TypeError, StopIteration):
            pass  # Progress is a convenience; the report carries the same evidence.
        with connect() as db:
            db.execute("UPDATE analyses SET phase='model' WHERE id=?", (job_id,))
            db.commit()
        stage = "model"
        try:
            decision = analyze_with_tools(request, candles, quote, context_candles, positions,
                                          prepared_trace=prepared, prompt_bundle=bundle,
                                          on_text=progress.model_text)
        except Exception as model_exc:  # noqa: BLE001 - never expose provider payloads
            safe_reason = model_error_message(model_exc, response_locale)
            code = "MODEL_CALL_FAILED"
            raise ModelAnalysisError(code, safe_reason) from None
        stage = "report"
        report = build_report(request, candles, quote, positions, decision, context_candles, events, news)
        report["output_locale"] = bundle.response_locale
        report["analysis_execution"] = {
            **(report.get("analysis_execution") or {}), "prompt_bundle": bundle.metadata(),
        }
        level_result = next(run["result"] for run in reversed(decision["tool_trace"])
                            if run["tool"] == "support_resistance")
        # Preserve the existing read endpoint for reports created before this promotion.
        shadow = {"status": "ready", "algorithm_version": LEVEL_VERSION,
                  "market_id": request["market_id"], "as_of": level_result["source_time"],
                  "tick_size": level_result["tick_size"], "tick_size_source": "Binance PRICE_FILTER",
                  "levels": level_result["levels"],
                  "active_zone_count": level_result["active_zone_count"],
                  "invalidated_zone_count": level_result["invalidated_zone_count"],
                  "quality_flags": level_result["quality_flags"],
                  "used_for_strategy": True,
                  "market_snapshot_sha256": quote["snapshot_hash"],
                  "price_evidence": {"status": "available", "source": "closed OHLC candles",
                                     "closed_candle_count": len(candles)},
                  "trade_evidence": {"status": "unavailable",
                                     "reason": "no price-level executed-trade history"},
                  "book_evidence": quote["order_book_evidence"]}
        stage = "save_report"
        with connect() as db:
            completed_at = datetime.now(UTC)
            # The separate v4 experiment only supports its original 1H/4H
            # backtest scope; extended production analysis continues to use v3.
            v4_status = "queued" if request["timeframe"] in {"1h", "4h"} else "not_requested"
            db.execute("UPDATE analyses SET status='completed', phase='done', report_json=?, shadow_json=?, completed_at=?, lease_until=NULL, v4_status=? WHERE id=? AND status='running'", (json.dumps(report), json.dumps(shadow), completed_at.isoformat(), v4_status, job_id))
            db.commit()
    except ModelAnalysisError as exc:
        with connect() as db:
            db.execute("UPDATE analyses SET status='failed', phase='done', error_code=?, error_message=?, completed_at=?, lease_until=NULL WHERE id=? AND status='running'", (exc.code, str(exc)[:300], utc_now(), job_id))
            db.commit()
    except StaleMarketDataError as exc:
        with connect() as db:
            db.execute("UPDATE analyses SET status='failed', phase='done', error_code=?, error_message=?, completed_at=?, lease_until=NULL WHERE id=? AND status='running'", (exc.code, system_error_message(exc.code, response_locale), utc_now(), job_id))
            db.commit()
    except Exception as exc:  # noqa: BLE001 - never persist an unknown upstream exception payload
        # Log only application-controlled stage and exception class. Provider
        # messages can contain request data or credentials.
        _LOG.error("analysis_failed id=%s stage=%s exception=%s", job_id, stage,
                   type(exc).__name__)
        code = "MARKET_DATA_UNAVAILABLE" if isinstance(exc, httpx.HTTPError) else "ANALYSIS_FAILED"
        message = (system_error_message(code, response_locale) if code == "MARKET_DATA_UNAVAILABLE"
                   else analysis_failure_message(stage, exc, response_locale))
        with connect() as db:
            db.execute("UPDATE analyses SET status='failed', phase='done', error_code=?, error_message=?, completed_at=?, lease_until=NULL WHERE id=? AND status='running'", (code, message, utc_now(), job_id))
            db.commit()
    finally:
        finish_desktop_execution(update_execution)
    return True


def run_work_once(*, discussions_first: bool = False, translations_first: bool = False) -> bool:
    from .macro_interpretation import run_macro_translation_once

    workers = ((run_macro_translation_once, run_once, run_discussion_once) if translations_first
               else (run_discussion_once, run_macro_translation_once, run_once)
               if discussions_first else (run_once, run_macro_translation_once, run_discussion_once))
    for work in workers:
        if work():
            return True
    return False


def main() -> None:
    assert_local_mode()
    init_db()
    once = "--once" in sys.argv
    priority = 0
    while True:
        # Give all three queues a turn even when the other two are busy.
        # --once still prioritizes a submitted market/position analysis.
        worked = run_work_once(discussions_first=priority == 1, translations_first=priority == 2)
        priority = (priority + 1) % 3
        if once:
            return
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
