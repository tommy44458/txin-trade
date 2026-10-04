"""Bounded agent loop. Only whitelisted Python indicator functions can be called."""

import json
from datetime import datetime
from time import monotonic

from openai import OpenAI

from .analysis import calculate
from .anthropic_api_bridge import AnthropicApiError
from .chatgpt_plan_auth import ChatGPTPlanError
from .claude_code_bridge import ClaudeCodeError
from .codex_bridge import CodexError
from .credential_store import CredentialStoreError
from .current_candle import current_candle_context
from .derivatives_context import compact_derivatives
from .entry_decision import validate_entry_decision
from .indicators import default_tool_trace
from .macro_context import build_macro_context
from .model_providers import ModelProviderError, ModelSession, analysis_timeout_seconds, tool_name
from .model_report import read_model_report
from .position_advice import compact_positions
from .position_reasoning import validate_position_reason
from .prompts import PromptBundle, resolve_prompt, task_prompt_version
from .reasoning_evidence import validate_reason_numbers
from .strategy_engine import other_timeframe
from .technical_snapshot import (
    additional_tool_schemas,
    compact_indicator_values,
    compact_technical_snapshot,
    execute_additional_indicator,
    prepare_analysis_evidence,
)
from .timeframes import analysis_timeframes, higher_timeframes


def fallback_analysis(request: dict, candles: list[dict], quote: dict, note: str | None = None,
                      context_candles: list[dict] | None = None,
                      positions: list[dict] | None = None) -> dict:
    trace = default_tool_trace(candles, quote, request, context_candles, positions)
    trend = calculate(candles, request["timeframe"])["trend"]
    ema_trend = trace[0]["result"]["direction"]
    support_count = sum(level["kind"] == "support" for level in trace[2]["result"]["levels"])
    resistance_count = sum(level["kind"] == "resistance" for level in trace[2]["result"]["levels"])
    book_status = {"snapshot": "可用", "unavailable": "無資料", "stale": "已過期",
                   "misaligned": "與報價不一致", "invalid": "無效"}.get(
                       trace[2]["result"]["order_book"]["status"], "未知")
    comparison = next((item["result"] for item in trace if item["tool"] == "compare_timeframes"), None)
    relation_text = {"aligned": "兩週期方向一致。", "conflict": "兩週期方向相反，策略觀望。",
                     "range": "兩週期方向皆混合，僅檢查區間候選。",
                     "uncertain": "跨週期方向未確認，策略觀望。"}.get(
                         comparison["relation"] if comparison else "", "另一週期資料未提供。")
    position_run = next((item["result"] for item in trace if item["tool"] == "evaluate_positions"), None)
    choices = ({item["position_id"]: item["default_action_id"]
                for item in position_run["positions"]} if position_run else {})
    candidate_run = next(item["result"] for item in trace if item["tool"] == "strategy_candidates")
    candidates = candidate_run["candidates"]
    live = candidate_run["current_candle"]
    actionable = [candidate for candidate in candidates if candidate["type"] != "wait"]
    style_note = {
        "left": "本次採左側提前區間測試，尚無收盤確認；",
        "right": "本次採右側確認方式，須等待所選週期收盤；",
    }.get(request.get("trading_style"), "未指定左／右側，沿用一般條件式情境；")
    strategy_text = (
        "Python 候選已通過方向、區間邊界與成本後風報比檢核；"
        "目前只列條件式情境，仍須依所選進場風格核對觸發條件。"
        if actionable else
        "Python 候選未通過方向、區間或風險檢核，因此維持觀望；"
        "等跨週期與價格條件重新確認後再分析。"
    )
    return {
        "mode": "rules_only", "tool_trace": trace,
        "strategy_decision": "candidate" if actionable else "wait",
        "position_choices": choices,
        "reasoning": {
            "market": (f"所選週期 MA20／MA50 判為{ '偏多' if trend == 'bullish' else '偏空' if trend == 'bearish' else '方向混合'}；"
                       f"EMA20／EMA50 判為{ '偏多' if ema_trend == 'bullish' else '偏空' if ema_trend == 'bearish' else '方向混合'}。"
                       + ("兩種指標結果不同，策略方向仍以 MA 與跨週期規則為主。" if trend != ema_trend else "")
                       + relation_text
                       + ("正在形成的 K 線尚未收盤；分析時現價高於開盤。" if
                          live["price_vs_open"] == "above" else
                          "正在形成的 K 線尚未收盤；分析時現價低於開盤。" if
                          live["price_vs_open"] == "below" else
                          "目前只取得現價，未取得同一週期的盤中 K 線。" if
                          live["status"] != "available" else
                          "正在形成的 K 線尚未收盤，不能當成收盤趨勢。")),
            "levels": (f"依 v3 已確認轉折、區間生命週期與獨立觸及，找出 {support_count} 個支撐與 {resistance_count} 個壓力；"
                       f"委託簿快照狀態為 {book_status}，只作近價輔助，不代表多空持倉。"),
            "strategy": style_note + strategy_text + relation_text,
            "supporting_evidence": "；".join(candidate["reason"] for candidate in candidates),
            "counter_evidence": "；".join(dict.fromkeys(
                warning for candidate in candidates for warning in candidate.get("counter_evidence", [])
            )) or "目前沒有可執行候選，不能把等待解讀為即將反轉；新收盤或區間失效都可能改變本次結論。",
            "preference_impact": style_note + "方向與風險偏好只篩選候選，不能改變市場證據；槓桿只用於候選的理論保證金風險。",
            "follow_up_reasons": {
                item["id"]: ("重新評估區間是否產生阻力或直接被穿透；"
                             "需要更新所選週期的量能、收盤位置與跨週期方向。"
                             if item["kind"] == "price_zone" else
                             "用新的已收盤資料區分原情境延續或失效，再核對是否仍需觀望。")
                for item in candidate_run["follow_up_plan"]
            },
            "evidence_tools": [item["tool"] for item in trace],
        },
        "fallback_reason": note,
    }



_VALIDATION_ERRORS = frozenset({
    'AI analysis exceeded the tool-call limit',
    'AI candidate order cites unvalidated candidates',
    'AI detailed explanation is incomplete',
    'AI detailed explanation must use qualitative evidence',
    'AI explanation is missing fields',
    'AI explanation must use validated numeric report fields',
    'AI explanation may not invent a trading probability',
    'AI explanation attributes a number to the wrong indicator or timeframe',
    'AI explanation omits required Python evidence',
    'AI explanation references uncalled tools',
    'AI explanation text is invalid',
    'AI explanation was not valid JSON',
    'AI follow-up reasons must cite all validated triggers',
    'AI follow-up reasons must use validated numeric fields',
    'AI explanation references an unverified time',
    'AI position choice is not a validated action',
    'AI position decisions must cover selected positions',
    'AI position decision is invalid',
    'AI position level claim lacks a verified price zone',
    'AI position reason includes excluded risk or venue claims',
    'AI position choices must cover selected positions',
    'AI report lacks detailed explanation or follow-up reasons',
    'AI skipped required level or strategy calculation',
    'AI strategy decision is invalid',
    'AI macro outlook is invalid',
    'Agent entry decision is invalid',
    'Agent entry decision lacks a clear reason',
    'Agent entry plan cites an unverified level',
    'Stand-aside decision may not include an order plan',
    'Agent entry side conflicts with strategy direction',
    'Agent entry plan lacks trigger or invalidation',
    'Agent entry plan has invalid price',
    'Agent entry prices do not match market tick size',
    'Immediate entry must use the analysis quote',
    'Conditional entry is too far from the snapshot',
    'Agent entry prices have the wrong direction',
    'Agent entry stop or target is too far from the snapshot',
    'Invalid tool arguments',
    'Market analysis cannot select position actions',
    'Position tool is unavailable for this request',
    'Tool call budget exceeded',
    'Tool parameter outside allowed values',
    'Tool timeframe differs from snapshot',
    'Unapproved tool',
})


def model_failure_reason(exc: Exception) -> str:
    """Only expose known application validation messages, never provider payloads."""
    if isinstance(exc, (ModelProviderError, CodexError, ClaudeCodeError, AnthropicApiError, ChatGPTPlanError,
                        CredentialStoreError)):
        return str(exc)
    if isinstance(exc, RuntimeError) and str(exc) == "OPENAI_API_KEY and OPENAI_MODEL are required for Agent analysis":
        return "尚未設定 OPENAI_API_KEY 或 OPENAI_MODEL，無法產生 Agent 策略分析"
    if isinstance(exc, ValueError) and str(exc) in _VALIDATION_ERRORS:
        return "模型回應處理失敗：" + str(exc)
    return "模型工具流程失敗：" + type(exc).__name__


def _final_reasoning(raw: str, trace: list[dict], *, require_detail: bool = False,
                     macro_context: dict | None = None, quote: dict | None = None,
                     leverage: int = 5, risk_tolerance: str | None = None) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("AI explanation was not valid JSON") from exc
    required = {"market", "levels", "strategy", "evidence_tools", "strategy_decision"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("AI explanation is missing fields")
    called = {item["tool"] for item in trace}
    for run in trace:
        if run["tool"] == "technical_snapshot":
            for frame in run["result"]["timeframes"].values():
                called.update(name for name, result in frame.get("indicators", {}).items()
                              if result.get("status") != "unavailable")
    evidence = value["evidence_tools"]
    if not isinstance(evidence, list) or not evidence or any(name not in called for name in evidence):
        raise ValueError("AI explanation references uncalled tools")
    required_evidence = {"support_resistance", "strategy_candidates"}
    required_evidence.update({"compare_timeframes", "evaluate_positions"} & called)
    if not required_evidence.issubset(set(evidence)):
        raise ValueError("AI explanation omits required Python evidence")
    if value["strategy_decision"] not in {"candidate", "wait"}:
        raise ValueError("AI strategy decision is invalid")
    if "candidate_order" in value:
        candidate_runs = [run for run in trace if run["tool"] == "strategy_candidates"]
        allowed = {candidate["id"] for candidate in candidate_runs[-1]["result"]["candidates"]} if candidate_runs else set()
        order = value["candidate_order"]
        if not isinstance(order, list) or len(order) != len(allowed) or set(order) != allowed:
            raise ValueError("AI candidate order cites unvalidated candidates")
    for field in ("market", "levels", "strategy"):
        sentence = value[field]
        if not isinstance(sentence, str) or not sentence.strip() or len(sentence) > 1200:
            raise ValueError("AI explanation text is invalid")
    position_runs = [run["result"] for run in trace if run["tool"] == "evaluate_positions"]
    options = {item["position_id"]: item for item in position_runs[-1]["positions"]} if position_runs else {}
    position_choices = value.get("position_choices")
    if position_choices is not None:
        if not isinstance(position_choices, dict) or set(position_choices) != set(options):
            raise ValueError("AI position choices must cover selected positions")
        if any(position_choices[position_id] not in {candidate["id"] for candidate in item["candidates"]}
               for position_id, item in options.items()):
            raise ValueError("AI position choice is not a validated action")
    elif options:
        position_choices = {position_id: item["default_action_id"] for position_id, item in options.items()}
    else:
        position_choices = {}
    if require_detail:
        for field in ("supporting_evidence", "counter_evidence"):
            sentence = value.get(field)
            if (not isinstance(sentence, str) or not sentence.strip()
                    or len(sentence) > 1600):
                raise ValueError("AI detailed explanation is incomplete")
        if value.get("agent_stance") not in {"long", "short", "wait"}:
            raise ValueError("AI strategy decision is invalid")
        macro = value.get("macro_outlook")
        valid_ids = {item["id"] for item in (macro_context or {}).get("directional_evidence", [])}
        if (not isinstance(macro, dict) or
                macro.get("stance") not in {"bullish", "bearish", "neutral"} or
                not isinstance(macro.get("reason"), str) or
                not macro["reason"].strip() or len(macro["reason"]) > 1200 or
                not isinstance(macro.get("evidence_ids"), list) or
                len(macro["evidence_ids"]) != len(set(macro["evidence_ids"])) or
                not set(macro["evidence_ids"]).issubset(valid_ids) or
                (macro["stance"] != "neutral" and not macro["evidence_ids"])):
            raise ValueError("AI macro outlook is invalid")
        entry_plan = value.get("entry_decision")
        candidate_runs = [run["result"] for run in trace if run["tool"] == "strategy_candidates"]
        level_runs = [run.get("result", {}) for run in trace if run["tool"] == "support_resistance"]
        if not candidate_runs or not level_runs:
            raise ValueError("AI skipped required level or strategy calculation")
        risk_reference = validate_entry_decision(
            entry_plan, quote or {}, level_runs[-1].get("levels", []),
            candidate_runs[-1].get("atr14", "1"), value["agent_stance"], leverage, risk_tolerance)
        position_decisions = value.get("position_decisions", {})
        # A null optional map carries no decision when no positions were selected.
        if position_decisions is None and not options:
            position_decisions = {}
        if not isinstance(position_decisions, dict) or set(position_decisions) != set(options):
            raise ValueError("AI position decisions must cover selected positions")
        for decision in position_decisions.values():
            if (not isinstance(decision, dict) or decision.get("decision") not in {"hold", "close_now"}
                    or not isinstance(decision.get("reason"), str)
                    or not decision["reason"].strip() or len(decision["reason"]) > 1200):
                raise ValueError("AI position decision is invalid")
            validate_position_reason(
                decision["reason"], level_runs[-1].get("levels", []),
                secondary_context=level_runs[-1].get("secondary_timeframe_context"))
        required.update({"supporting_evidence", "counter_evidence", "agent_stance", "macro_outlook", "entry_decision"})
    else:
        position_decisions = value.get("position_decisions", {})
        entry_plan, risk_reference = None, None
    for field in ('market', 'levels', 'strategy', 'supporting_evidence', 'counter_evidence'):
        if field in value:
            validate_reason_numbers(value[field], trace, quote,
                                    {'entry_plan': entry_plan, 'risk_reference': risk_reference})
    result = {field: value[field] for field in required}
    if "candidate_order" in value:
        result["candidate_order"] = value["candidate_order"]
    if options:
        result["position_choices"] = position_choices
    if require_detail or position_decisions:
        result["position_decisions"] = position_decisions
    return result


def agent_context(request: dict, candles: list[dict], quote: dict,
                  context_candles: list[dict] | None,
                  positions: list[dict] | None = None,
                  prepared_trace: list[dict] | None = None) -> dict:
    """Provide as-of official calendar facts; unverified news stays out of strategy evidence."""
    news = quote.get("news_context")
    context = {
        "market_id": request["market_id"], "timeframe": request["timeframe"],
        "timeframe_plan": {
            "primary_timeframe": request["timeframe"],
            "context_timeframes": list(higher_timeframes(request["timeframe"])),
            "analysis_timeframes": list(analysis_timeframes(request["timeframe"])),
            "background_review_order": list(reversed(higher_timeframes(request["timeframe"]))),
            "primary_role": "entry_and_position_timing",
            "context_role": "larger_trend_background_not_a_direction_vote",
        },
        "leverage": request.get("leverage"),
        "selected_positions": [
            {key: row[key] for key in ("id", "version", "market_id", "side", "leverage",
                                       "margin_mode", "entry_price", "quantity", "entry_time",
                                       "exchange_liquidation_price")}
            for row in compact_positions(positions, include_notes=False)
        ] if positions else [],
        "quote_time": quote["observed_at"],
        "derivatives_context": compact_derivatives(quote.get("derivatives_context")),
        "market_reference": quote.get("market_reference"),
        "fund_flows_context": quote.get("fund_flows_context"),
        "last_closed_candle": candles[-1]["close_time"],
        "current_candle": (current_candle_context(candles, quote, request["timeframe"])
                           if candles and "close" in candles[-1] and "price" in quote else None),
        "context_timeframe": other_timeframe(request["timeframe"]) if context_candles else None,
        "context_last_closed_candle": context_candles[-1]["close_time"] if context_candles else None,
        "official_event_context": quote.get("event_context"),
        "monthly_macro_context": build_macro_context(
            datetime.fromisoformat(quote["observed_at"]),
            quote.get("event_context"), quote.get("news_context")),
        "official_announcement_risk": {
            "risk": news["risk"], "source_status": news["source_status"],
            "strategy_news_evidence": "verified_publication_only" if
            news.get("evidence_pack", {}).get("events") else "not_ready",
            "verified_release_events": news.get("evidence_pack", {}).get("events", []),
            "coverage_status": news.get("evidence_pack", {}).get("coverage_status", "unknown"),
        } if news else None,
    }
    if prepared_trace:
        # Actual values already appear in the monthly evidence. Do not send the
        # full database records and a second copy of every recent observation.
        if context["official_event_context"] is not None:
            context["official_event_context"] = {
                key: value for key, value in context["official_event_context"].items()
                if key != "official_actuals"}
        macro = context["monthly_macro_context"]
        context["monthly_macro_context"] = {
            **macro,
            "latest_actuals": [item for item in macro["latest_actuals"] if not item["within_month"]],
            "directional_evidence": [{key: value for key, value in item.items()
                                      if key not in {"content_hash", "document_id"}}
                                     for item in macro["directional_evidence"]],
        }
        evidence = {}
        for execution in prepared_trace:
            result = _model_tool_result(execution)
            if execution["tool"] == "technical_snapshot":
                result = compact_technical_snapshot(result)
            elif execution["tool"] == "strategy_candidates":
                result = {key: value for key, value in result.items()
                          if key not in {"current_candle", "follow_up_plan"}}
            evidence[execution["tool"]] = result
        context["precomputed_evidence"] = evidence
    saved_macro = quote.get("macro_interpretation")
    if saved_macro is not None:
        shared = {key: value for key, value in saved_macro.items()
                  if key not in {"interpretation", "coverage", "cached", "needs_update", "version"}}
        interpretation = saved_macro.get("interpretation")
        if (saved_macro.get("status") == "succeeded" and not saved_macro.get("stale")
                and interpretation is not None):
            shared["interpretation"] = {key: value for key, value in interpretation.items()
                                        if key not in {"execution", "evidence"}}
            shared["interpretation"]["evidence"] = [
                {key: value for key, value in item.items()
                 if key not in {"evidence", "body", "statement_text", "policy_text", "content_hash", "version", "observed_at"}}
                for item in interpretation["evidence"]]
            # The shared interpretation carries its exact numeric evidence.
            # Avoid sending a second copy for the trading model to reinterpret.
            context["monthly_macro_context"] = {
                key: value for key, value in context["monthly_macro_context"].items()
                if key not in {"directional_evidence", "latest_actuals", "official_publications"}}
        else:
            shared["interpretation"] = None
        context["saved_macro_interpretation"] = shared
    # Keep facts first in the serialized input. The user's directional hypothesis
    # is withheld so it cannot anchor the decision; the model assesses long and
    # short separately and the report compares that with the hypothesis.
    context.update({"risk_tolerance": request.get("risk_tolerance"),
                    "trading_style": request.get("trading_style")})
    return context



STRATEGY_PROMPT_VERSION = task_prompt_version("strategy_market")


def strategy_instructions(has_positions: bool | dict, *, response_locale: str | None = None,
                          prompt_locale: str = "en-US") -> str:
    """Compatibility wrapper: old bool calls and new typed analysis requests."""
    request = has_positions if isinstance(has_positions, dict) else {}
    positions = (request.get("kind") == "positions" or bool(request.get("position_ids"))) if request else bool(has_positions)
    return resolve_prompt(
        "strategy_positions" if positions else "strategy_market",
        response_locale=response_locale or request.get("output_locale", "zh-TW"),
        prompt_locale=prompt_locale, inputs=request,
    ).instructions


def _model_tool_result(execution: dict) -> dict:
    """Expose numerical references without Python recommendations anchoring the model."""
    result = execution["result"]
    if execution["tool"] == "strategy_candidates":
        fields = {"id", "type", "side", "entry", "stop_loss", "take_profit", "targets",
                  "risk_reward", "gross_risk_reward", "leverage", "risk_metrics",
                  "risk_on_theoretical_margin_pct", "evidence_level_ids", "expires_at"}
        return {"atr14": result["atr14"],
                "reference_scope": "preference_filtered_non_exhaustive_numerical_templates",
                "candidates": [{key: value for key, value in candidate.items() if key in fields}
                               for candidate in result["candidates"]]}
    if execution["tool"] != "evaluate_positions":
        return result
    return {
        "version": result["version"],
        "quote_time": result["quote_time"],
        "valuation_price": result["valuation_price"],
        "market_state": result["market_state"],
        "positions": [
            {"position_id": item["position_id"], "version": item["version"]}
            for item in result["positions"]
        ],
    }


def analyze_with_tools(request: dict, candles: list[dict], quote: dict,
                       context_candles: list[dict] | None = None,
                       positions: list[dict] | None = None, *,
                       prepared_trace: list[dict] | None = None,
                       prompt_bundle: PromptBundle | None = None) -> dict:
    deadline = monotonic() + analysis_timeout_seconds()
    session = ModelSession(openai_factory=OpenAI)
    try:
        return _analyze_with_session(request, candles, quote, context_candles, positions,
                                     prepared_trace=prepared_trace, session=session, deadline=deadline,
                                     prompt_bundle=prompt_bundle)
    finally:
        session.close()


def _analyze_with_session(request: dict, candles: list[dict], quote: dict,
                         context_candles: list[dict] | None,
                         positions: list[dict] | None, *,
                         prepared_trace: list[dict] | None, session: ModelSession,
                         deadline: float, prompt_bundle: PromptBundle | None = None) -> dict:
    model = session.model

    # A multi-tool analysis may need several model turns; the quote remains an as-of snapshot.
    client = session.client
    task = "strategy_positions" if positions or request.get("kind") == "positions" else "strategy_market"
    bundle = prompt_bundle or resolve_prompt(task, response_locale=request.get("output_locale", "zh-TW"),
                                             inputs=request)
    if bundle.task != task:
        raise ValueError("Analysis prompt task differs from the submitted request")
    instructions = bundle.instructions
    trace = list(prepared_trace) if prepared_trace is not None else prepare_analysis_evidence(
        request, candles, quote, context_candles, positions)
    snapshot = trace[0]["result"]
    inputs: list = [{"role": "user", "content": json.dumps(
        agent_context(request, candles, quote, context_candles, positions, trace),
        ensure_ascii=False, separators=(",", ":"))}]
    additional_calls = 0
    additional_indicator_cache: dict = {}
    model_requests = 0
    usage = {"input_tokens": 0, "output_tokens": 0}
    usage_available = True
    # Precomputed evidence needs no model turn. Optional calls then the final report; no repair.
    for turn in range(6):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("AI analysis time budget exceeded")
        if session.uses_local_agent:
            def run_indicator(name, arguments):
                nonlocal additional_calls
                additional_calls += 1
                if additional_calls > 4:
                    raise ValueError("Tool call budget exceeded")
                execution = execute_additional_indicator(
                    tool_name(name), arguments, request, candles, quote, context_candles, snapshot,
                    cache=additional_indicator_cache)
                trace.append(execution)
                return compact_indicator_values(_model_tool_result(execution))

            response = session.analyze_local_agent(instructions=instructions,
                context=inputs[0]["content"], tools=additional_tool_schemas(snapshot),
                tool_handler=run_indicator, timeout=remaining)
        else:
            response = client.responses.create(
                timeout=min(90, remaining),
                model=model, instructions=instructions, input=inputs,
                tools=additional_tool_schemas(snapshot),
                tool_choice="auto" if additional_calls < 4 else "none", parallel_tool_calls=False,
            )
        model_requests += 1
        response_usage = getattr(response, "usage", None)
        for key in usage:
            count = getattr(response_usage, key, None)
            if isinstance(count, int):
                usage[key] += count
            else:
                usage_available = False
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            reasoning = read_model_report(response.output_text, trace, output_locale=bundle.response_locale)
            return {"mode": "openai_assisted", "tool_trace": trace,
                "strategy_decision": reasoning["strategy_decision"],
                "position_choices": reasoning.get("position_choices", {}),
                "agent_stance": reasoning["agent_stance"],
                "position_decisions": reasoning.get("position_decisions", {}),
                "candidate_order": reasoning.get("candidate_order"),
                "reasoning": reasoning, "fallback_reason": None,
                "analysis_execution": {"pipeline": "precompute_then_reason_v1",
                    "prompt_version": bundle.prompt_version,
                    "prompt_bundle": bundle.metadata(), "output_locale": bundle.response_locale,
                    "provider": session.provider,
                    "provider_diagnostics": getattr(response, "provider_diagnostics", None),
                    "model": model, "prompt_sha256": bundle.instructions_sha256,
                    "precomputed_evidence_count": sum(
                        item.get("execution_source") == "precomputed" for item in trace),
                    "initial_indicators": snapshot.get("initial_indicator_selection", {}).get("names", []),
                    "initial_indicator_calculations": sum(
                        result.get("execution_source") == "precomputed_selected"
                        for frame in snapshot["timeframes"].values()
                        for result in frame.get("indicators", {}).values()),
                    "additional_tool_calls": additional_calls, "model_requests": model_requests,
                    "input_tokens": usage["input_tokens"] if usage_available else None,
                    "output_tokens": usage["output_tokens"] if usage_available else None}}
        inputs.extend(response.output)
        for call in calls:
            additional_calls += 1
            try:
                if additional_calls > 4:
                    raise ValueError("Tool call budget exceeded")
                execution = execute_additional_indicator(
                    tool_name(call.name), json.loads(call.arguments), request, candles, quote,
                    context_candles, snapshot, cache=additional_indicator_cache)
            except (ValueError, TypeError, KeyError) as exc:
                # An optional tool failure is evidence unavailability, not report failure.
                inputs.append({"type": "function_call_output", "call_id": call.call_id,
                               "output": json.dumps({"status": "unavailable", "reason": type(exc).__name__,
                                   "message": "Use the precomputed evidence and finish the report without this optional tool."})})
                continue
            trace.append(execution)
            inputs.append({"type": "function_call_output", "call_id": call.call_id,
                           "output": json.dumps(compact_indicator_values(_model_tool_result(execution)),
                                                ensure_ascii=False, separators=(",", ":"))})
    raise ValueError("AI analysis exceeded the tool-call limit")
