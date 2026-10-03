from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .exit_plan import sanitize_exit_plan
from .macro_context import build_macro_context
from .macro_interpretation import saved_macro_outlook
from .model_report import entry_cost_reference
from .position_advice import VERSION as POSITION_ADVICE_VERSION
from .position_advice import build_position_options, compact_positions, stop_exposure
from .report_contract import REPORT_VERSION
from .risk import VERSION as RISK_VERSION
from .risk import configured_cost_scenario
from .strategy_engine import VERSION as STRATEGY_VERSION
from .strategy_engine import build_candidates, compare_timeframes, other_timeframe
from .support_levels import wilder_atr
from .support_levels_v3 import VERSION as LEVEL_VERSION
from .timeframes import FIXED_SECONDS, TIMEFRAME_LADDER, advance_candle, analysis_timeframes


class StaleMarketDataError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def average(values: list[Decimal]) -> Decimal:
    return sum(values) / Decimal(len(values))


def calculate(candles: list[dict], timeframe: str) -> dict:
    if timeframe not in TIMEFRAME_LADDER:
        raise ValueError("Unsupported timeframe")
    if len(candles) < 60:
        raise ValueError("At least 60 closed candles are required")
    previous_open = None
    for candle in candles:
        open_time = datetime.fromisoformat(candle["open_time"])
        if previous_open is not None and open_time != advance_candle(previous_open, timeframe):
            raise ValueError("Closed candle series has a gap or duplicate")
        previous_open = open_time
        open_price, high, low, close, volume = (Decimal(candle[field]) for field in
                                                 ("open", "high", "low", "close", "volume"))
        if low <= 0 or volume < 0 or high < max(open_price, close, low) or low > min(open_price, close):
            raise ValueError("Closed candle has invalid OHLCV")
    closes = [Decimal(c["close"]) for c in candles]
    last = closes[-1]
    atr = wilder_atr(candles)
    ma20 = average(closes[-20:])
    ma50 = average(closes[-50:])
    trend = "bullish" if last > ma20 > ma50 else "bearish" if last < ma20 < ma50 else "mixed"
    return {
        "last_close": str(last), "ma20": str(ma20), "ma50": str(ma50),
        "atr14": str(atr), "trend": trend,
        "last_candle_at": candles[-1]["close_time"],
        "candle_count": len(candles),
    }


def hypothesis_assessment(bias: str | None, assessment: dict | None) -> dict | None:
    """Look up the blind AI verdict for the direction the user hypothesized."""
    side = {"bullish": "long", "bearish": "short"}.get(bias)
    item = (assessment or {}).get(side) if side else None
    return {"side": side, **item} if isinstance(item, dict) else None


def strategy_for(metrics: dict, bias: str | None, risk: str | None, quote: dict,
                 leverage: int = 5, context: dict | None = None,
                 event_risk: str = "unavailable",
                 trading_style: str | None = None) -> list[dict]:
    context = context or compare_timeframes(metrics, None, metrics.get("timeframe", "1h"))
    if quote.get("news_risk") == "recent_fomc_release":
        event_risk = "recent_fomc_release"
    return build_candidates(metrics, context, bias, risk, quote, leverage, event_risk,
                            trading_style)


def position_review(position: dict, quote: dict, account_equity_usdt: str | None = None) -> dict:
    price = Decimal(quote.get("mark_price") or quote["price"])
    entry = Decimal(position["entry_price"])
    quantity = Decimal(position["quantity"])
    direction = Decimal(1) if position["side"] == "long" else Decimal(-1)
    pnl = (price - entry) * quantity * direction
    leverage = int(position.get("leverage") or 1)
    theoretical_margin = entry * quantity / leverage
    stop = Decimal(position["stop_loss"]) if position["stop_loss"] else None
    target = Decimal(position["take_profit"]) if position["take_profit"] else None
    previous_stop = (Decimal(position["previous_stop_loss"])
                     if position.get("previous_stop_loss") else None)
    liquidation = (Decimal(position["exchange_liquidation_price"])
                   if position.get("exchange_liquidation_price") else None)
    liquidation_distance = ((price - liquidation) * direction / price * 100
                            if liquidation is not None else None)
    messages = []
    if liquidation_distance is not None and liquidation_distance <= 0:
        messages.append("參考標記價已達紀錄中的交易所強平價；請立即向交易所核對實際部位狀態")
    profitable_stop = stop is not None and (stop - entry) * direction > 0
    stop_reached = stop is not None and (price - stop) * direction <= 0
    if stop is None:
        messages.append("尚未設定止損")
    elif stop_reached:
        messages.append("目前價格已達登記止損，請確認實際成交")
    if profitable_stop:
        messages.append("登記止損位於進場價的獲利側；僅為價格條件，不保證成交或鎖定獲利")
    if target is None:
        messages.append("尚未設定止盈")
    elif (price >= target and direction > 0) or (price <= target and direction < 0):
        messages.append("目前價格已達登記止盈，請確認實際成交")
    remaining_rr = None
    original_rr = None
    if stop is not None and target is not None:
        original_risk = (entry - stop) * direction
        original_reward = (target - entry) * direction
        if original_risk > 0 and original_reward > 0:
            original_rr = str((original_reward / original_risk).quantize(Decimal("0.01")))
        to_stop = (price - stop) * direction
        to_target = (target - price) * direction
        if to_stop > 0 and to_target > 0:
            remaining_rr = str((to_target / to_stop).quantize(Decimal("0.01")))
    risk_delta = None
    if stop is not None and previous_stop is not None:
        # Negative means the revised stop reduces the price distance at risk from the same mark.
        risk_delta = str((previous_stop - stop) * direction * quantity)
    return {"position_id": position["id"], "version": position["version"],
            "side": position["side"], "leverage": leverage, "margin_mode": position.get("margin_mode", "isolated"),
            "valuation_price_type": "mark" if quote.get("mark_price") else "last_trade",
            "unrealized_pnl_usdt": str(pnl),
            "theoretical_initial_margin_usdt": str(theoretical_margin),
            "return_on_theoretical_margin_pct": str((pnl / theoretical_margin * 100).quantize(Decimal("0.01"))),
            "original_risk_reward": original_rr,
            "remaining_risk_reward": remaining_rr,
            "profitable_side_stop": profitable_stop, "manual_stop_reached": stop_reached,
            "previous_stop_loss": str(previous_stop) if previous_stop is not None else None,
            "stop_risk_delta_usdt": risk_delta,
            "entry_time": position.get("entry_time"),
            "user_note": position.get("notes"),
            "exchange_liquidation_price": str(liquidation) if liquidation is not None else None,
            "distance_to_exchange_liquidation_pct": (
                str(liquidation_distance.quantize(Decimal("0.01")))
                if liquidation_distance is not None else None),
            "messages": messages, "action": "review" if messages else "maintain",
            "note": "手動登記部位；保證金與報酬率僅為理論估算，不含費用、資金費或維持保證金；強平價只顯示用戶手動登記的交易所數值，不代表即時帳戶狀態"} | (
                stop_exposure(position, quote, account_equity_usdt) or {})


def validate_market_freshness(request: dict, candles: list[dict], quote: dict,
                              context_candles: list[dict] | None = None) -> tuple[dict, dict | None]:
    metrics = calculate(candles, request["timeframe"])
    observed = datetime.fromisoformat(quote["observed_at"])
    if datetime.now(UTC) - observed > timedelta(seconds=180):
        raise StaleMarketDataError("QUOTE_STALE", "分析用報價快照超過 180 秒，請重新分析")
    last_close = datetime.fromisoformat(metrics["last_candle_at"])
    if last_close > observed or last_close > datetime.now(UTC):
        raise ValueError("Primary candle close is later than the analysis snapshot")
    max_candle_age = timedelta(seconds=FIXED_SECONDS[request["timeframe"]], minutes=3)
    if datetime.now(UTC) - last_close > max_candle_age:
        raise StaleMarketDataError("CANDLES_STALE", "已收盤 K 線資料過期，請重新分析")
    context_metrics = (calculate(context_candles, other_timeframe(request["timeframe"]))
                       if context_candles and len(context_candles) >= 60 else None)
    if context_metrics is not None:
        context_close = datetime.fromisoformat(context_metrics["last_candle_at"])
        if context_close > observed or context_close > datetime.now(UTC):
            raise ValueError("Context candle close is later than the analysis snapshot")
        context_age = timedelta(seconds=FIXED_SECONDS[other_timeframe(request["timeframe"])], minutes=3)
        if datetime.now(UTC) - context_close > context_age:
            raise StaleMarketDataError("CONTEXT_CANDLES_STALE", "另一週期的已收盤 K 線過期，請重新分析")
    return metrics, context_metrics


def _reported_macro_outlook(state: dict | None, model_outlook: dict | None,
                           output_locale: str) -> dict | None:
    saved = saved_macro_outlook(state)
    if saved is None:
        return None
    interpretation = state.get("interpretation") or {}
    source_locale = interpretation.get("output_locale", interpretation.get("response_locale", "zh-TW"))
    # Reuse the saved economic decision and provenance. When its prose is in
    # another language, retain the Agent's requested-language expression of
    # that same stance instead of overwriting it with the old source paragraph.
    if (source_locale != output_locale and isinstance(model_outlook, dict)
            and model_outlook.get("stance") == saved["stance"]
            and isinstance(model_outlook.get("reason"), str)
            and model_outlook["reason"].strip()):
        return saved | {"reason": model_outlook["reason"]}
    return saved


def _with_exit_plan(agent_decision: object, position: dict, price: Decimal, quote: dict,
                    metrics: dict) -> object:
    """A hold carries the checked parts of its exit plan; closing now needs none."""
    if not isinstance(agent_decision, dict):
        return agent_decision
    plan = None
    if agent_decision.get("decision") == "hold":
        plan = sanitize_exit_plan(
            agent_decision.get("exit_plan"), side=position["side"], price=price,
            tick=Decimal(str(quote["tick_size"])), atr=Decimal(metrics["atr14"]),
            levels=metrics["levels"])
    return {key: value for key, value in agent_decision.items() if key != "exit_plan"} | {"exit_plan": plan}


def build_report(request: dict, candles: list[dict], quote: dict, positions: list[dict],
                 decision: dict, context_candles: list[dict] | None = None, events: dict | None = None,
                 news: dict | None = None) -> dict:
    metrics, context_metrics = validate_market_freshness(request, candles, quote, context_candles)
    technical_runs = [run["result"] for run in decision["tool_trace"]
                      if run["tool"] == "technical_snapshot"]
    technical_snapshot = technical_runs[-1] if technical_runs else None
    higher_metrics = ({frame: data.get("metrics") for frame, data in technical_snapshot["timeframes"].items()
                       if frame != request["timeframe"] and data.get("status") == "available"}
                      if technical_snapshot else {})
    timeframe_context = compare_timeframes(metrics, context_metrics, request["timeframe"], higher_metrics)
    level_runs = [run for run in decision["tool_trace"] if run["tool"] == "support_resistance"]
    level_result = level_runs[-1]["result"]
    metrics["levels"] = level_result["levels"]
    metrics["level_algorithm_version"] = LEVEL_VERSION
    metrics["order_book"] = level_result["order_book"]
    metrics["level_reference_price"] = level_result["reference_price"]
    metrics["level_reference_time"] = level_result["reference_time"]
    metrics["market_state"] = timeframe_context["market_state"]
    # Python cards match the evidence the model saw: no directional hypothesis.
    strategies = strategy_for(metrics, None, request.get("risk_tolerance"),
                              quote, request.get("leverage", 5), timeframe_context,
                              quote.get("event_risk", "unavailable"),
                              request.get("trading_style"))
    candidate_runs = [run for run in decision["tool_trace"] if run["tool"] == "strategy_candidates"]
    order = decision.get("candidate_order")
    if order is not None:
        by_id = {candidate["id"]: candidate for candidate in strategies}
        if isinstance(order, list) and all(isinstance(item, str) for item in order) and set(order) == set(by_id) and len(order) == len(by_id):
            strategies = [by_id[candidate_id] for candidate_id in order]
    strategy_source = "validated_python_candidates"
    bias = request.get("directional_bias")
    trend = metrics["trend"]
    market_state = timeframe_context["market_state"]
    consistency = ("unspecified" if bias is None else "insufficient" if market_state not in
                   {"bullish", "bearish"} else "aligned" if bias == market_state else "conflict")
    cost_assumptions = quote.get("cost_scenario") or configured_cost_scenario()
    position_snapshot = compact_positions(positions)
    position_reviews = []
    agent_decisions = {}
    if positions:
        options = build_position_options(position_snapshot, quote, metrics["levels"],
                                         market_state, Decimal(metrics["atr14"]),
                                         None,
                                         request.get("risk_tolerance"),
                                         request.get("account_equity_usdt"))
        choices = decision.get("position_choices") or {}
        by_position = {item["position_id"]: item for item in options["positions"]}
        for position in position_snapshot:
            item = by_position[position["id"]]
            selected = next((candidate for candidate in item["candidates"]
                             if candidate["id"] == choices.get(position["id"], item["default_action_id"])), None)
            if selected is None:
                selected = next(candidate for candidate in item["candidates"] if candidate["id"] == item["default_action_id"])
            review = position_review(position, quote, request.get("account_equity_usdt"))
            review["action"] = selected["kind"]
            review["advice"] = selected | {"selection_source": "python_reference"}
            review["available_actions"] = item["candidates"]
            if decision["mode"] == "openai_assisted":
                agent_decision = _with_exit_plan(
                    decision.get("position_decisions", {}).get(position["id"]), position,
                    Decimal(options["valuation_price"]), quote, metrics)
                review["agent_decision"] = agent_decisions[position["id"]] = agent_decision
            position_reviews.append(review)
    entry_plan = decision["reasoning"].get("entry_decision") if decision["mode"] == "openai_assisted" else None
    entry_risk_reference = entry_cost_reference(entry_plan, quote, request.get("leverage", 5))
    reasoning = dict(decision["reasoning"])
    if agent_decisions:
        # The saved reasoning and each review carry the same checked exit plan.
        reasoning["position_decisions"] = {**(reasoning.get("position_decisions") or {}), **agent_decisions}
    if "macro_interpretation" in quote:
        reasoning["macro_outlook"] = _reported_macro_outlook(
            quote["macro_interpretation"], reasoning.get("macro_outlook"),
            request.get("output_locale", "zh-TW"))
    report = {
        "market_id": request["market_id"], "timeframe": request["timeframe"],
        "analysis_timeframes": list(analysis_timeframes(request["timeframe"])),
        "analysis_kind": "positions" if positions else "market",
        "position_advice_version": POSITION_ADVICE_VERSION if positions else None,
        "position_snapshot": position_snapshot,
        "account_equity_usdt": request.get("account_equity_usdt"),
        "report_schema_version": REPORT_VERSION, "risk_rule_version": RISK_VERSION,
        "strategy_rule_version": STRATEGY_VERSION, "strategy_source": strategy_source,
        "timeframe_context": timeframe_context,
        "cost_assumptions": cost_assumptions,
        "strategy_level_algorithm_version": LEVEL_VERSION,
        "generated_at": datetime.now(UTC).isoformat(), "data_source": "Binance USDⓈ-M perpetual public API",
        "market_snapshot_sha256": quote.get("snapshot_hash"),
        "analysis_mode": decision["mode"], "market_type": "linear_perpetual",
        "analysis_leverage": request.get("leverage", 5),
        "quote": {key: value for key, value in quote.items()
                  if key not in {"order_book", "order_book_evidence", "snapshot_hash", "event_context", "news_context", "macro_interpretation"}} |
                 {"cost_scenario": cost_assumptions},
        "metrics": metrics, "strategies": strategies,
        "preference_assessment": {"directional_bias": bias, "risk_tolerance": request.get("risk_tolerance"),
                                  "trading_style": request.get("trading_style"),
                                  "market_trend": market_state, "primary_trend": trend,
                                  "consistency": consistency,
                                  "hypothesis_assessment": hypothesis_assessment(
                                      bias, decision["reasoning"].get("direction_assessment")
                                      if decision["mode"] == "openai_assisted" else None)},
        "position_reviews": position_reviews,
        "follow_up_plan": candidate_runs[-1]["result"]["follow_up_plan"],
        "current_candle": candidate_runs[-1]["result"]["current_candle"],
        "reasoning": reasoning, "tool_trace": decision["tool_trace"],
        "technical_snapshot": technical_snapshot,
        "analysis_execution": decision.get("analysis_execution"),
        "derivatives_context": quote.get("derivatives_context"),
        "market_reference": quote.get("market_reference"),
        "fund_flows_context": quote.get("fund_flows_context"),
        "agent_stance": decision.get("agent_stance"),
        "entry_decision": entry_plan,
        "entry_risk_reference": entry_risk_reference,
        "macro_context": build_macro_context(
            datetime.fromisoformat(quote["observed_at"]), events, news),
        "macro_interpretation": quote.get("macro_interpretation"),
        "fallback_reason": decision.get("fallback_reason"),
        "model_note": decision["reasoning"]["market"],
        "events_status": events["status"] if events else "not_integrated",
        "event_context": events,
        "news_context": news,
        "limitations": ["僅已核對的官方宏觀公布值和 FOMC 決策可作宏觀背景；SEC 摘要與 Jev 分類尚未成為策略方向證據，市場共識預期值未接入" if news else "官方宏觀實際值依來源可用性納入；市場共識預期值及廣泛新聞尚未接入" if events else "新聞與美國經濟事件尚未接入", "委託簿為單次近價快照，掛單可撤銷，不能推論多空持倉或長期支撐", "v3 阻滯回測不等於策略績效或反轉機率", "策略風報比含示例費用與滑價；實際費率、成交與資金費可能不同", "持倉損益未扣費用或資金費", "未計算強平價", "趨勢與 v3 支撐壓力只使用已收盤 K 線；現價及未收盤 K 線僅作盤中觀察"],
    }
    return report
