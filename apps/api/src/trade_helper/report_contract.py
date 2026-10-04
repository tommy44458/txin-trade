"""Validate newly produced reports without rewriting immutable legacy reports."""

from datetime import datetime
from decimal import Decimal
from re import fullmatch
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from .current_candle import current_candle_context
from .derivatives_context import validate_derivatives_context
from .entry_decision import validate_entry_decision
from .follow_up import build_follow_up_plan
from .fomc_actual import PARSER_VERSION as FOMC_PARSER_VERSION
from .fomc_actual import parse_target_range, release_day
from .macro_actuals import validate_snapshot_actual
from .macro_context import build_macro_context
from .news import SOURCE as NEWS_SOURCE
from .news import official_release_url
from .news_evidence import validate_news_evidence_pack
from .position_advice import VERSION as POSITION_ADVICE_VERSION
from .position_advice import build_position_options
from .position_reasoning import validate_position_reason
from .reasoning_evidence import validate_reason_numbers
from .risk import VERSION as RISK_VERSION
from .risk import validate_cost_scenario
from .strategy_engine import CONTEXT_VERSION, build_candidates, other_timeframe
from .strategy_engine import VERSION as STRATEGY_VERSION
from .support_levels_v3 import VERSION as LEVEL_VERSION
from .timeframes import analysis_timeframes, higher_timeframes

REPORT_VERSION = "analysis_report_v3_10"


def _positive(value: str) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("Report price must be positive and finite")
    return number


class LevelEvidenceV3(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["support", "resistance"]
    low: str
    high: str
    center: str
    timeframe: Literal["1h", "4h", "12h", "1d", "3d", "1w", "1M"]
    algorithm_version: str
    zone_state: Literal["active"]
    pivot_count: int
    independent_touch_count: int
    confirmed_at: str

    @model_validator(mode="after")
    def check_zone(self):
        low, high, center = map(_positive, (self.low, self.high, self.center))
        if not low <= center <= high or self.algorithm_version != LEVEL_VERSION:
            raise ValueError("Invalid v3 level geometry or version")
        if self.pivot_count < 1 or self.independent_touch_count < 0:
            raise ValueError("Invalid v3 evidence counts")
        datetime.fromisoformat(self.confirmed_at)
        return self


class StrategyCandidateV3(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: Literal["wait", "pullback", "breakout", "range"]
    title: str
    reason: str
    status: Literal["waiting"]
    confirmation_state: Literal["not_applicable", "awaiting_close", "awaiting_zone_test"]
    expires_at: str
    strategy_rule_version: str
    side: Literal["long", "short"] | None = None
    entry_style: Literal["left", "right"] = "right"
    entry: str | None = None
    stop_loss: str | None = None
    take_profit: str | None = None
    risk_reward: str | None = None
    leverage: int | None = None
    level_algorithm_version: str | None = None
    risk_metrics: dict | None = None
    targets: list[dict] | None = None
    invalidation: str | None = None
    counter_evidence: list[str] | None = None

    @model_validator(mode="after")
    def check_candidate(self):
        if self.type == "wait":
            if any(value is not None for value in (self.side, self.entry, self.stop_loss,
                                                   self.take_profit, self.risk_reward, self.risk_metrics,
                                                   self.targets)) or self.confirmation_state != "not_applicable":
                raise ValueError("Wait may not carry executable prices")
        elif any(value is None for value in (self.side, self.entry, self.stop_loss, self.take_profit,
                                             self.risk_reward, self.leverage, self.risk_metrics,
                                             self.targets, self.invalidation, self.counter_evidence)):
            raise ValueError("Conditional candidate lacks numerical evidence")
        elif (self.confirmation_state != (
                "awaiting_zone_test" if self.entry_style == "left" else "awaiting_close")
              or not 1 <= len(self.targets) <= 2):
            raise ValueError("Conditional candidate has invalid confirmation state or targets")
        if self.strategy_rule_version != STRATEGY_VERSION:
            raise ValueError("Strategy rule version is invalid")
        datetime.fromisoformat(self.expires_at)
        return self


class AnalysisReportV3(BaseModel):
    model_config = ConfigDict(extra="allow")

    report_schema_version: str
    strategy_level_algorithm_version: str
    risk_rule_version: str
    strategy_rule_version: str
    strategy_source: Literal["validated_python_candidates", "agent_wait"]
    market_id: str
    timeframe: Literal["1h", "4h", "12h", "1d"]
    analysis_kind: Literal["market", "positions"]
    analysis_mode: Literal["rules_only", "openai_assisted"]
    agent_stance: Literal["long", "short", "wait"] | None = None
    entry_decision: dict | None = None
    entry_risk_reference: dict | None = None
    reasoning: dict = {}
    macro_context: dict | None = None
    macro_interpretation: dict | None = None
    position_advice_version: str | None
    position_snapshot: list[dict]
    account_equity_usdt: str | None = None
    position_reviews: list[dict]
    market_snapshot_sha256: str
    cost_assumptions: dict
    quote: dict
    metrics: dict
    timeframe_context: dict
    preference_assessment: dict
    analysis_leverage: int
    strategies: list[StrategyCandidateV3]
    tool_trace: list[dict]
    follow_up_plan: list[dict]
    current_candle: dict
    technical_snapshot: dict | None = None
    analysis_execution: dict | None = None
    derivatives_context: dict | None = None
    events_status: str = "not_integrated"
    event_context: dict | None = None
    news_context: dict | None = None

    @model_validator(mode="after")
    def check_report(self):
        if (self.report_schema_version != REPORT_VERSION or
                self.strategy_level_algorithm_version != LEVEL_VERSION or
                self.risk_rule_version != RISK_VERSION or
                self.strategy_rule_version != STRATEGY_VERSION or
                not fullmatch(r"[0-9a-f]{64}", self.market_snapshot_sha256)):
            raise ValueError("Report schema, algorithm or snapshot version is invalid")
        if (self.analysis_mode == "openai_assisted" and
                (self.agent_stance not in {"long", "short", "wait"} or
                 self.agent_stance != self.reasoning.get("agent_stance"))):
            raise ValueError("Agent stance is missing or inconsistent")
        validate_cost_scenario(self.cost_assumptions)
        if self.derivatives_context is not None or self.quote.get('derivatives_context') is not None:
            if self.derivatives_context != self.quote.get('derivatives_context'):
                raise ValueError('Derivatives context differs from quoted snapshot')
            validate_derivatives_context(self.derivatives_context, self.market_id,
                                         datetime.fromisoformat(self.quote['observed_at']))
        if self.analysis_mode == "openai_assisted":
            for field in ('market', 'levels', 'strategy'):
                validate_reason_numbers(self.reasoning.get(field, ''), self.tool_trace, self.quote,
                                        {'entry_plan': self.entry_decision, 'risk_reference': self.entry_risk_reference})
            for field in ('supporting_evidence', 'counter_evidence'):
                validate_reason_numbers(self.reasoning.get(field, ''), self.tool_trace, self.quote,
                                        {'entry_plan': self.entry_decision, 'risk_reference': self.entry_risk_reference})
        if self.event_context is not None:
            if (self.event_context.get("status") != self.events_status or
                    self.event_context.get("risk") != self.quote.get("event_risk") or
                    datetime.fromisoformat(self.event_context["cutoff"]) !=
                    datetime.fromisoformat(self.quote["observed_at"])):
                raise ValueError("Report event context differs from the market cutoff")
            for actual in self.event_context.get("official_actuals", []):
                validate_snapshot_actual(actual, datetime.fromisoformat(self.event_context["cutoff"]))
            for event in self.event_context.get("events", []):
                if datetime.fromisoformat(event["ingested_at"]) > datetime.fromisoformat(
                        self.event_context["cutoff"]):
                    raise ValueError("Report cites an event learned after the cutoff")
                if event.get("actual_value") is not None and event.get("scheduled_at") and (
                        datetime.fromisoformat(event["scheduled_at"]) >
                        datetime.fromisoformat(self.event_context["cutoff"])):
                    raise ValueError("Unreleased event carries an actual value")
                result = event.get("official_result")
                if result is not None:
                    cutoff = datetime.fromisoformat(self.event_context["cutoff"])
                    published = datetime.fromisoformat(result["published_at"])
                    ingested = datetime.fromisoformat(result["ingested_at"])
                    parsed = parse_target_range(result.get("evidence_sentence", ""))
                    if (event.get("source") != "fed" or event.get("kind") != "fomc" or
                            result.get("parser_version") != FOMC_PARSER_VERSION or
                            release_day(result.get("source_url", "")) !=
                            event.get("scheduled_date") or
                            not fullmatch(r"[0-9a-f]{64}", result.get("content_hash", "")) or
                            not result.get("document_id") or published.tzinfo is None or
                            ingested.tzinfo is None or published > cutoff or ingested > cutoff or
                            published.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                            != event.get("scheduled_date") or parsed is None or
                            any(parsed[key] != result.get(key) for key in
                                ("decision", "lower_pct", "upper_pct", "evidence_sentence")) or
                            event.get("actual_value") !=
                            f"{result['lower_pct']}–{result['upper_pct']}%" or
                            event.get("actual_unit") != "target_range_percent"):
                        raise ValueError("Invalid or future FOMC official result")
                elif event.get("actual_value") is not None and event.get("source") == "fed":
                    raise ValueError("FOMC actual value lacks official result")
        if self.news_context is not None:
            news = self.news_context
            cutoff = datetime.fromisoformat(news["cutoff"])
            if (cutoff != datetime.fromisoformat(self.quote["observed_at"]) or
                    news.get("risk") != self.quote.get("news_risk")):
                raise ValueError("Report news context differs from the market cutoff")
            if news.get("evidence_pack") is not None:
                validate_news_evidence_pack(news["evidence_pack"], cutoff=cutoff,
                                            market_id=self.market_id)
            for item in news.get("items", []) + news.get("archive", []):
                if (datetime.fromisoformat(item["published_at"]) > cutoff or
                        datetime.fromisoformat(item["ingested_at"]) > cutoff):
                    raise ValueError("Report cites news learned after the cutoff")
                if (item.get("source") != NEWS_SOURCE or
                        not official_release_url(item.get("source_url", "")) or
                        item.get("version", 0) < 1 or
                        not fullmatch(r"[0-9a-f]{64}", item.get("content_hash", ""))):
                    raise ValueError("Report cites an unapproved news source or version")
        if self.macro_context is not None:
            expected_macro = build_macro_context(
                datetime.fromisoformat(self.quote["observed_at"]),
                self.event_context, self.news_context)
            if self.macro_context != expected_macro:
                raise ValueError("Monthly macro context differs from verified report evidence")
        if self.analysis_mode == "openai_assisted" and self.macro_interpretation is None:
            if self.macro_context is None:
                raise ValueError("Agent report lacks monthly macro context")
            outlook = self.reasoning.get("macro_outlook")
            allowed = {item["id"] for item in (self.macro_context or {}).get("directional_evidence", [])}
            if (not isinstance(outlook, dict) or
                    outlook.get("stance") not in {"bullish", "bearish", "neutral"} or
                    not isinstance(outlook.get("reason"), str) or
                    not outlook["reason"].strip() or
                    len(outlook["reason"]) > 1200 or
                    not isinstance(outlook.get("evidence_ids"), list) or
                    len(outlook["evidence_ids"]) != len(set(outlook["evidence_ids"])) or
                    not set(outlook["evidence_ids"]).issubset(allowed) or
                    (outlook["stance"] != "neutral" and not outlook["evidence_ids"])):
                raise ValueError("Agent macro outlook lacks verified directional evidence")
        if self.analysis_mode == "openai_assisted":
            if self.entry_decision != self.reasoning.get("entry_decision"):
                raise ValueError("Agent entry decision differs from reasoning")
            expected_entry_risk = validate_entry_decision(
                self.entry_decision, self.quote, self.metrics["levels"],
                self.metrics["atr14"], self.agent_stance, self.analysis_leverage,
                self.preference_assessment.get("risk_tolerance"))
            if self.entry_risk_reference != expected_entry_risk:
                raise ValueError("Agent entry risk reference differs from snapshot")
        elif self.entry_decision is not None or self.entry_risk_reference is not None:
            raise ValueError("Rules-only report may not carry an Agent entry decision")
        if self.quote.get("cost_scenario") != self.cost_assumptions:
            raise ValueError("Report and snapshot cost assumptions differ")
        if not 1 <= len(self.strategies) <= 3 or self.metrics.get("level_algorithm_version") != LEVEL_VERSION:
            raise ValueError("Report strategy count or level version is invalid")
        levels = [LevelEvidenceV3.model_validate(level) for level in self.metrics["levels"]]
        if any(level.timeframe != self.timeframe or
               datetime.fromisoformat(level.confirmed_at) > datetime.fromisoformat(self.metrics["last_candle_at"])
               for level in levels):
            raise ValueError("Level timeframe or confirmation time differs from report")
        runs = [run["result"] for run in self.tool_trace if run["tool"] == "support_resistance"]
        if not runs or runs[-1].get("method") != LEVEL_VERSION or runs[-1].get("levels") != self.metrics["levels"]:
            raise ValueError("Report levels differ from the v3 tool result")
        if (Decimal(runs[-1]["reference_price"]) != Decimal(self.quote["price"]) or
                runs[-1]["reference_time"] != self.quote["observed_at"] or
                runs[-1]["source_time"] != self.metrics["last_candle_at"]):
            raise ValueError("Report levels are not from the quoted candle snapshot")
        if datetime.fromisoformat(self.metrics["last_candle_at"]) > datetime.fromisoformat(
                self.quote["observed_at"]):
            raise ValueError("Report primary candle closes after the quote snapshot")
        context = self.timeframe_context
        if (context.get("version") != CONTEXT_VERSION or
                context.get("primary_timeframe") != self.timeframe or
                context.get("context_timeframe") != other_timeframe(self.timeframe) or
                context.get("analysis_timeframes") != list(analysis_timeframes(self.timeframe)) or
                context.get("context_timeframes") != list(higher_timeframes(self.timeframe)) or
                context.get("primary_last_candle_at") != self.metrics["last_candle_at"] or
                context.get("market_state") != self.metrics.get("market_state")):
            raise ValueError("Report cross-timeframe context is invalid")
        context_runs = [run["result"] for run in self.tool_trace if run["tool"] == "compare_timeframes"]
        if context["relation"] == "unavailable":
            if context_runs:
                raise ValueError("Unavailable context may not cite comparison results")
        elif (not context_runs or context_runs[-1] != context or
              datetime.fromisoformat(context["context_last_candle_at"]) >
              datetime.fromisoformat(self.quote["observed_at"])):
            raise ValueError("Report cross-timeframe evidence differs from tool result")
        technical_runs = [run["result"] for run in self.tool_trace
                          if run["tool"] == "technical_snapshot"]
        if self.technical_snapshot is not None or technical_runs:
            from .technical_snapshot import VERSION as TECHNICAL_VERSION

            snapshot = self.technical_snapshot
            if (not snapshot or len(technical_runs) != 1 or technical_runs[0] != snapshot or
                    snapshot.get("version") != TECHNICAL_VERSION or
                    snapshot.get("market_id") != self.market_id or
                    snapshot.get("primary_timeframe") != self.timeframe or
                    snapshot.get("as_of") != self.quote["observed_at"] or
                    snapshot.get("market_snapshot_sha256") != self.market_snapshot_sha256 or
                    snapshot.get("data_basis") != "closed_candles" or
                    snapshot.get("analysis_timeframes") != list(analysis_timeframes(self.timeframe)) or
                    snapshot.get("context_timeframes") != list(higher_timeframes(self.timeframe)) or
                    set(snapshot.get("timeframes", {})) != set(analysis_timeframes(self.timeframe))):
                raise ValueError("Technical indicators differ from the analysis snapshot")
            frames = snapshot["timeframes"]
            if "higher_timeframe_context" in snapshot or "higher_timeframe_candles" in self.quote:
                from .higher_timeframes import build_higher_timeframe_context
                if snapshot.get("higher_timeframe_context") != build_higher_timeframe_context(
                        self.quote, self.timeframe):
                    raise ValueError("Higher timeframe context differs from the analysis snapshot")
            primary = frames[self.timeframe]
            if (primary.get("status") != "available" or
                    any(self.metrics.get(key) != value for key, value in primary["metrics"].items())):
                raise ValueError("Technical indicators differ from the primary timeframe")
            for frame in frames.values():
                if frame.get("status") == "available" and (
                        frame["last_closed_at"] != frame["metrics"]["last_candle_at"] or
                        frame["candle_count"] != frame["metrics"]["candle_count"] or
                        datetime.fromisoformat(frame["last_closed_at"]) >
                        datetime.fromisoformat(self.quote["observed_at"])):
                    raise ValueError("Technical indicators contain future candle data")
            secondary = frames[other_timeframe(self.timeframe)]
            comparison_available = (secondary.get("status") == "available" and
                                    secondary.get("candle_count", 0) >= 60 and
                                    secondary.get("metrics", {}).get("trend") is not None)
            if ((context["relation"] != "unavailable") != comparison_available or
                    comparison_available and any(
                        secondary["metrics"][key] != context[field] for key, field in (
                            ("trend", "context_trend"), ("ma20", "context_ma20"),
                            ("ma50", "context_ma50"), ("last_candle_at", "context_last_candle_at")))):
                raise ValueError("Technical indicators differ from the context timeframe")
        style = self.preference_assessment.get("trading_style")
        if style not in {None, "left", "right"}:
            raise ValueError("Report trading style is invalid")
        # Evidence is built without the directional hypothesis, which the model never sees.
        expected = build_candidates(self.metrics, context, None,
                                    self.preference_assessment.get("risk_tolerance"),
                                    self.quote, self.analysis_leverage,
                                    self.quote.get("event_risk", "unavailable"), style)
        candidate_runs = [run["result"]["candidates"] for run in self.tool_trace
                          if run["tool"] == "strategy_candidates"]
        if not candidate_runs or candidate_runs[-1] != expected:
            raise ValueError("Report candidates differ from deterministic Python tool result")
        plan = build_follow_up_plan(self.metrics["levels"], self.quote, self.timeframe,
                                    self.metrics["last_candle_at"])
        plan_runs = [run["result"].get("follow_up_plan") for run in self.tool_trace
                     if run["tool"] == "strategy_candidates"]
        if self.follow_up_plan != plan or not plan_runs or plan_runs[-1] != plan:
            raise ValueError("Follow-up plan differs from validated Python evidence")
        live = current_candle_context(
            [{"close": self.metrics["last_close"],
              "close_time": self.metrics["last_candle_at"]}], self.quote, self.timeframe)
        live_runs = [run["result"].get("current_candle") for run in self.tool_trace
                     if run["tool"] == "strategy_candidates"]
        if self.current_candle != live or not live_runs or live_runs[-1] != live:
            raise ValueError("Current candle context differs from validated snapshot")
        actual = [strategy.model_dump(exclude_unset=True) for strategy in self.strategies]
        if self.strategy_source == "validated_python_candidates":
            if len(actual) != len(expected) or {item["id"] for item in actual} != {item["id"] for item in expected}:
                raise ValueError("Report strategy IDs differ from validated candidates")
            by_id = {item["id"]: item for item in expected}
            if any(item != by_id[item["id"]] for item in actual):
                raise ValueError("Report strategy data differ from validated candidates")
        elif (len(actual) != 1 or actual[0]["type"] != "wait" or
              actual[0]["id"] != "agent_wait"):
            raise ValueError("Agent wait must have no executable candidate")
        position_runs = [run["result"] for run in self.tool_trace
                         if run["tool"] == "evaluate_positions"]
        if self.analysis_kind == "market":
            if (self.position_snapshot or self.position_reviews or position_runs or
                    self.position_advice_version or self.account_equity_usdt is not None):
                raise ValueError("Market report may not contain position advice")
        else:
            if (self.position_advice_version != POSITION_ADVICE_VERSION or
                    not 1 <= len(self.position_snapshot) == len(self.position_reviews) or
                    any(position.get("market_id") != self.market_id
                        for position in self.position_snapshot)):
                raise ValueError("Invalid position advice snapshot")
            if (self.account_equity_usdt is not None and
                    _positive(self.account_equity_usdt) > Decimal(1000000000000)):
                raise ValueError("Report account equity is out of bounds")
            options = build_position_options(self.position_snapshot, self.quote,
                                             self.metrics["levels"], context["market_state"],
                                             Decimal(self.metrics["atr14"]),
                                             None,
                                             self.preference_assessment.get("risk_tolerance"),
                                             self.account_equity_usdt)
            if not position_runs or position_runs[-1] != options:
                raise ValueError("Position advice differs from Python tool evidence")
            from .analysis import position_review  # Imported here to avoid an import cycle.
            for position, review, item in zip(self.position_snapshot, self.position_reviews,
                                              options["positions"], strict=True):
                expected_review = position_review(position, self.quote, self.account_equity_usdt)
                if any(review.get(key) != value for key, value in expected_review.items()
                       if key != "action"):
                    raise ValueError("Position diagnostics differ from snapshot")
                if self.analysis_mode == "openai_assisted":
                    agent_decision = review.get("agent_decision")
                    if agent_decision != self.reasoning.get("position_decisions", {}).get(position["id"]):
                        raise ValueError("Agent position decision differs from reasoning")
                    if (not isinstance(agent_decision, dict) or
                            agent_decision.get("decision") not in {"hold", "close_now"} or
                            not isinstance(agent_decision.get("reason"), str) or
                            not agent_decision["reason"].strip() or
                            len(agent_decision["reason"]) > 1200):
                        raise ValueError("Agent position decision is invalid")
                    validate_position_reason(
                        agent_decision["reason"], self.metrics["levels"],
                        primary_timeframe=self.timeframe,
                        secondary_context=runs[-1].get("secondary_timeframe_context"))
                selected = next((candidate for candidate in item["candidates"]
                                 if candidate["id"] == review.get("advice", {}).get("id")), None)
                if (selected is None or review.get("action") != selected["kind"] or
                        review["advice"] != selected | {"selection_source": "python_reference"} or
                        review.get("available_actions") != item["candidates"]):
                    raise ValueError("Position advice selects an unvalidated action")
        return self


def validate_report(report: dict) -> None:
    AnalysisReportV3.model_validate(report)
