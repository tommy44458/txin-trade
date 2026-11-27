"""Whitelisted, deterministic indicator tools callable by the analysis agent."""

from datetime import datetime
from decimal import Decimal

from .analysis import calculate, strategy_for
from .current_candle import current_candle_context
from .follow_up import build_follow_up_plan
from .optional_indicators import FUNCTIONS as OPTIONAL_FUNCTIONS
from .optional_indicators import bollinger, fibonacci
from .position_advice import build_position_options
from .strategy_engine import compare_timeframes as compare_closed_timeframes
from .strategy_engine import other_timeframe
from .support_levels import annotate_liquidity, order_book_evidence, wilder_atr
from .support_levels_v3 import annotate_forming_sweep, historical_levels_v3


def series(candles: list[dict], field: str) -> list[Decimal]:
    return [Decimal(c[field]) for c in candles]


def ema(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period:
        raise ValueError(f"EMA{period} needs {period} closed candles")
    current = sum(values[:period]) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
    return current


def trend_ema(candles: list[dict], _quote: dict, _params: dict) -> dict:
    closes = series(candles, "close")
    e20, e50 = ema(closes, 20), ema(closes, 50)
    direction = "bullish" if closes[-1] > e20 > e50 else "bearish" if closes[-1] < e20 < e50 else "mixed"
    return {"ema20": str(e20), "ema50": str(e50), "last_close": str(closes[-1]), "direction": direction}


def rsi(candles: list[dict], _quote: dict, params: dict) -> dict:
    period = params["period"]
    closes = series(candles, "close")
    if len(closes) < period + 1:
        raise ValueError("RSI warmup insufficient")
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    value = (Decimal(50) if avg_gain == avg_loss == 0 else Decimal(100) if avg_loss == 0
             else Decimal(100) - Decimal(100) / (1 + avg_gain / avg_loss))
    return {"period": period, "value": str(value.quantize(Decimal("0.01")))}


def macd(candles: list[dict], _quote: dict, _params: dict) -> dict:
    closes = series(candles, "close")
    if len(closes) < 35:
        raise ValueError("MACD warmup insufficient")
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    # Full signal line from the sequence of MACD values after both EMAs warm up.
    fast_state = sum(closes[:12]) / 12
    slow_state = sum(closes[:26]) / 26
    for value in closes[12:26]:
        fast_state = Decimal(2) / 13 * value + Decimal(11) / 13 * fast_state
    macd_values = [fast_state - slow_state]
    for value in closes[26:]:
        fast_state = Decimal(2) / 13 * value + Decimal(11) / 13 * fast_state
        slow_state = Decimal(2) / 27 * value + Decimal(25) / 27 * slow_state
        macd_values.append(fast_state - slow_state)
    signal = ema(macd_values, 9)
    line = fast - slow
    return {"line": str(line), "signal": str(signal), "histogram": str(line - signal), "parameters": "12,26,9"}


def volatility_atr(candles: list[dict], _quote: dict, params: dict) -> dict:
    period = params["period"]
    closes = series(candles, "close")
    atr = wilder_atr(candles, period)
    return {"period": period, "atr": str(atr), "atr_pct": str((atr / closes[-1] * 100).quantize(Decimal("0.01")))}


def swing_points(candles: list[dict], _quote: dict, params: dict) -> dict:
    width = params["width"]
    highs, lows = series(candles, "high"), series(candles, "low")
    points = []
    for i in range(width, len(candles) - width):
        neighborhood = slice(i - width, i + width + 1)
        if lows[i] == min(lows[neighborhood]):
            points.append({"kind": "low", "price": str(lows[i]), "confirmed_at": candles[i + width]["close_time"]})
        if highs[i] == max(highs[neighborhood]):
            points.append({"kind": "high", "price": str(highs[i]), "confirmed_at": candles[i + width]["close_time"]})
    return {"width": width, "recent_points": points[-12:]}


def support_resistance(candles: list[dict], quote: dict, params: dict) -> dict:
    reference = Decimal(quote["price"])
    evidence = quote.get("order_book_evidence") or order_book_evidence(quote.get("order_book"), reference)
    request = params["_request"]
    if "tick_size" not in quote:
        raise ValueError("V3 requires the exchange tick size in the market snapshot")
    cutoff = datetime.fromisoformat(quote["observed_at"])
    context_rows = params.get("_context_candles")
    for rows in (candles, context_rows):
        if rows and (cutoff.tzinfo is None or any(
                datetime.fromisoformat(row["close_time"]).tzinfo is None or
                datetime.fromisoformat(row["close_time"]) > cutoff for row in rows)):
            raise ValueError("V3 candle closes after the analysis cutoff")
    calculated = historical_levels_v3(candles, params["timeframe"], reference,
                                      Decimal(str(quote["tick_size"])), request["market_id"])
    secondary_timeframe = other_timeframe(params["timeframe"])
    secondary = (historical_levels_v3(context_rows, secondary_timeframe, reference,
                                     Decimal(str(quote["tick_size"])), request["market_id"])
                 if context_rows and len(context_rows) >= 60 else None)
    return {"method": calculated["algorithm_version"],
            "context_version": calculated["context_version"],
            "levels": annotate_forming_sweep(
                annotate_liquidity(calculated["levels"], evidence),
                current_candle_context(candles, quote, params["timeframe"])["candle"], reference),
            "recently_invalidated_levels": calculated["recently_invalidated_levels"],
            "invalidation_rule": calculated["invalidation_rule"],
            "sweep_rule": calculated["sweep_rule"],
            "secondary_timeframe_context": {"timeframe": secondary_timeframe,
                "status": "available" if secondary else "unavailable",
                "data": secondary},
            "order_book": evidence, "atr_method": "Wilder14",
        "reference_price": str(reference), "reference_time": quote["observed_at"],
        "last_closed_trend": calculate(candles, params["timeframe"])["trend"],
        "tick_size": calculated["tick_size"], "source_time": calculated["as_of"],
        "active_zone_count": calculated["active_zone_count"],
        "invalidated_zone_count": calculated["invalidated_zone_count"],
        "quality_flags": calculated["quality_flags"]}


def volume_signal(candles: list[dict], _quote: dict, _params: dict) -> dict:
    volumes = series(candles, "volume")
    baseline = sum(volumes[-21:-1]) / 20
    return {"last_volume": str(volumes[-1]), "average_previous_20": str(baseline),
            "relative_volume": str((volumes[-1] / baseline).quantize(Decimal("0.01"))) if baseline else None}


def rolling_vwap(candles: list[dict], _quote: dict, params: dict) -> dict:
    period = params["period"]
    chosen = candles[-period:]
    total_volume = sum(Decimal(c["volume"]) for c in chosen)
    if total_volume <= 0:
        raise ValueError("VWAP volume is zero")
    weighted = sum(
        ((Decimal(c["high"]) + Decimal(c["low"]) + Decimal(c["close"])) / 3)
        * Decimal(c["volume"])
        for c in chosen
    )
    return {"period": period, "rolling_vwap": str(weighted / total_volume), "note": "rolling window, not exchange session VWAP"}


def funding_context(_candles: list[dict], quote: dict, _params: dict) -> dict:
    return {"mark_price": quote.get("mark_price"), "index_price": quote.get("index_price"),
            "last_funding_rate": quote.get("last_funding_rate"), "next_funding_time": quote.get("next_funding_time")}


def compare_timeframes(candles: list[dict], _quote: dict, params: dict) -> dict:
    if params.get("_prepared", {}).get("timeframe_context") is not None:
        return params["_prepared"]["timeframe_context"]
    request = params["_request"]
    context_candles = params.get("_context_candles")
    if not context_candles:
        raise ValueError("Cross-timeframe comparison requires closed context candles")
    primary = calculate(candles, request["timeframe"])
    context = (calculate(context_candles, other_timeframe(request["timeframe"]))
               if len(context_candles) >= 60 else None)
    return compare_closed_timeframes(primary, context, request["timeframe"])


def strategy_candidates(candles: list[dict], quote: dict, params: dict) -> dict:
    request = params["_request"]
    prepared = params.get("_prepared", {})
    metrics = dict(prepared["metrics"]) if "metrics" in prepared else calculate(candles, request["timeframe"])
    level_result = (prepared["levels"] if "levels" in prepared else
                    support_resistance(candles, quote, {"timeframe": request["timeframe"], "_request": request}))
    metrics["levels"] = level_result["levels"]
    metrics["level_algorithm_version"] = level_result["method"]
    context_candles = params.get("_context_candles")
    context = compare_timeframes(candles, quote, params) if context_candles else compare_closed_timeframes(
        metrics, None, request["timeframe"])
    # The directional hypothesis never filters or labels evidence (see agent_context).
    return {"atr14": metrics["atr14"], "candidates": strategy_for(metrics, None,
                                       request.get("risk_tolerance"), quote,
                                       request.get("leverage", 5), context,
                                       quote.get("event_risk", "unavailable"),
                                       request.get("trading_style")),
            "follow_up_plan": build_follow_up_plan(level_result["levels"], quote,
                                                   request["timeframe"],
                                                   metrics["last_candle_at"]),
            "current_candle": current_candle_context(candles, quote, request["timeframe"])}


def evaluate_positions(candles: list[dict], quote: dict, params: dict) -> dict:
    request = params["_request"]
    positions = params.get("_positions")
    if request.get("kind") != "positions" or not positions:
        raise ValueError("Position evaluation requires selected position snapshots")
    prepared = params.get("_prepared", {})
    metrics = dict(prepared["metrics"]) if "metrics" in prepared else calculate(candles, request["timeframe"])
    level_result = (prepared["levels"] if "levels" in prepared else
                    support_resistance(candles, quote, {
                        "timeframe": request["timeframe"], "_request": request}))
    context_candles = params.get("_context_candles")
    context = compare_timeframes(candles, quote, params) if context_candles else compare_closed_timeframes(
        metrics, None, request["timeframe"])
    return build_position_options(positions, quote, level_result["levels"],
                                  context["market_state"], Decimal(metrics["atr14"]),
                                  None,
                                  request.get("risk_tolerance"),
                                  request.get("account_equity_usdt"))


TOOL_FUNCTIONS = {
    "trend_ema": trend_ema,
    "rsi": rsi,
    "macd": macd,
    "bollinger": bollinger,
    "volatility_atr": volatility_atr,
    "swing_points": swing_points,
    "support_resistance": support_resistance,
    "volume_signal": volume_signal,
    "rolling_vwap": rolling_vwap,
    "fibonacci": fibonacci,
    "funding_context": funding_context,
    "compare_timeframes": compare_timeframes,
    "strategy_candidates": strategy_candidates,
    "evaluate_positions": evaluate_positions,
    **OPTIONAL_FUNCTIONS,
}

TOOL_DESCRIPTIONS = {
    "trend_ema": "Determine direction from EMA20, EMA50, and the latest confirmed close.",
    "rsi": "Measure price momentum and potential exhaustion. Choose a period.",
    "macd": "Check 12/26 EMA momentum against a 9-period signal line.",
    "bollinger": "On demand: examine SMA/population-standard-deviation bands, bandwidth and percent_b. Specify period and standard-deviation multiplier. A band touch does not prove reversal.",
    "volatility_atr": "Measure true-range volatility for risk and level width. Choose a period.",
    "swing_points": "Find confirmed local highs and lows. Choose confirmation width.",
    "support_resistance": "Calculate v3 confirmed pivot lifecycles and independent touches using the selected snapshot timeframe; report a separate near-price depth snapshot. The timeframe is bound by the server, not chosen by the model.",
    "volume_signal": "Compare latest closed candle volume with the previous 20 candles.",
    "rolling_vwap": "Calculate rolling volume-weighted typical price. Choose a period.",
    "fibonacci": "On demand: confirmed pivot A-B retracements/swing extensions and A-B-C trend extensions with disclosed timestamps, linear-price scale and exchange-tick rounding. Choose lookback, confirmation width and direction; never substitutes for v3 zones.",
    "adx_dmi": "On demand: Wilder-smoothed ADX strength and +DI/-DI directional balance. Choose DI and ADX periods. ADX alone is not direction or an entry rule.",
    "obv": "On demand: cumulative signed whole-candle base-asset volume and a recent-window participation change. Initial OBV is zero at the frozen history start; not taker order flow or automatically verified divergence.",
    "donchian": "On demand: rolling high/low channel plus the prior channel excluding the last closed bar, to inspect a breakout without self-referential boundaries. These are channel bounds, not v3 zones.",
    "keltner": "On demand: SMA-seeded EMA close plus/minus Wilder ATR times a multiplier. Inspect a volatility envelope; touching it does not prove reversal.",
    "stochastic": "On demand: raw high/low-range close position and SMA-smoothed %K/%D. Flat ranges yield null, not invented momentum; overbought/oversold may persist in a trend.",
    "funding_context": "Inspect futures mark/index prices and the published latest funding rate.",
    "compare_timeframes": "Compare the primary and next three closed higher-frame SMA trend states; report available background without using personal bias.",
    "strategy_candidates": "Generate numerically checked long/short or wait scenarios from confirmed levels, leverage and user preferences.",
    "evaluate_positions": "Calculate bounded review actions for the selected position snapshots, current quote and qualified v3 levels; never place orders.",
}


def tool_schema(name: str) -> dict:
    properties = {"reason": {"type": "string", "description": "The unresolved market hypothesis this calculation checks and what result could change your assessment"}}
    if name in {"rsi", "volatility_atr"}:
        properties["period"] = {"type": "integer", "enum": [14, 21]}
    elif name == "rolling_vwap":
        properties["period"] = {"type": "integer", "enum": [20, 30]}
    elif name == "bollinger":
        properties["period"] = {"type": "integer", "enum": [20, 30, 50]}
        properties["multiplier"] = {"type": "number", "enum": [1.5, 2, 2.5, 3]}
    elif name == "swing_points":
        properties["width"] = {"type": "integer", "enum": [2, 3, 4]}
    elif name == "fibonacci":
        properties.update({"lookback": {"type": "integer", "enum": [80, 160, 240]},
                           "width": {"type": "integer", "enum": [2, 3, 4]},
                           "direction": {"type": "string", "enum": ["auto", "up", "down"]}})
    elif name == "adx_dmi":
        properties.update({key: {"type": "integer", "enum": [14, 21]} for key in ("period", "adx_period")})
    elif name == "obv":
        properties["period"] = {"type": "integer", "enum": [20, 50, 100]}
    elif name == "donchian":
        properties["period"] = {"type": "integer", "enum": [20, 55]}
    elif name == "keltner":
        properties.update({"period": {"type": "integer", "enum": [20, 30]},
                           "atr_period": {"type": "integer", "enum": [14, 21]},
                           "multiplier": {"type": "number", "enum": [1.5, 2, 2.5, 3]}})
    elif name == "stochastic":
        properties.update({"period": {"type": "integer", "enum": [14, 21]},
                           "smooth_k": {"type": "integer", "enum": [1, 3]},
                           "smooth_d": {"type": "integer", "enum": [3, 5]}})
    return {"type": "function", "name": name, "description": TOOL_DESCRIPTIONS[name], "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


TOOL_SCHEMAS = [tool_schema(name) for name in TOOL_FUNCTIONS]


def validate_tool_parameters(name: str, args: dict) -> None:
    """Validate schema types before either computation or cache reuse."""
    for key, spec in tool_schema(name)["parameters"]["properties"].items():
        value = args[key]
        kind = spec["type"]
        if (kind == "integer" and type(value) is not int or
                kind == "number" and (type(value) not in {int, float} or not Decimal(str(value)).is_finite()) or
                kind == "string" and not isinstance(value, str)):
            raise ValueError("Tool parameter outside allowed values")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError("Tool parameter outside allowed values")


def execute_tool(name: str, args: dict, candles: list[dict], quote: dict, request: dict,
                 context_candles: list[dict] | None = None,
                 positions: list[dict] | None = None, *, prepared: dict | None = None) -> dict:
    if name not in TOOL_FUNCTIONS:
        raise ValueError("Unapproved tool")
    if not isinstance(args, dict):
        raise TypeError("Invalid tool arguments")
    # The model never selects which candle series a level calculation uses. Accept
    # matching explicit arguments from older callers, but bind new calls to the
    # request snapshot and continue rejecting an attempted cross-timeframe mix.
    if name == "support_resistance":
        if "timeframe" in args and args["timeframe"] != request["timeframe"]:
            raise ValueError("Tool timeframe differs from snapshot")
        args = {**args, "timeframe": request["timeframe"]}
    schema = tool_schema(name)["parameters"]["properties"]
    allowed = set(schema) | ({"timeframe"} if name == "support_resistance" else set())
    if set(args) != allowed or not isinstance(args.get("reason"), str) or not args["reason"].strip():
        raise ValueError("Invalid tool arguments")
    validate_tool_parameters(name, args)
    if name == "evaluate_positions" and (request.get("kind") != "positions" or not positions):
        raise ValueError("Position tool is unavailable for this request")
    result = TOOL_FUNCTIONS[name](candles, quote, {**args, "_request": request,
                                                  "_context_candles": context_candles,
                                                  "_positions": positions,
                                                  "_prepared": prepared or {}})
    return {"tool": name, "reason": args["reason"][:180], "parameters": {k: v for k, v in args.items() if k != "reason"}, "result": result}


def default_tool_trace(candles: list[dict], quote: dict, request: dict,
                       context_candles: list[dict] | None = None,
                       positions: list[dict] | None = None) -> list[dict]:
    defaults = [
        ("trend_ema", {"reason": "確認目前方向與均線排列"}),
        ("volatility_atr", {"reason": "衡量近期波動，避免把單一價格當成支撐壓力", "period": 14}),
        ("support_resistance", {"reason": "取得已確認的支撐與壓力區間", "timeframe": request["timeframe"]}),
        ("volume_signal", {"reason": "比較最新成交量與近期基準"}),
        ("funding_context", {"reason": "檢查合約標記價格與資金費率背景"}),
    ]
    if context_candles:
        defaults.append(("compare_timeframes", {"reason": "核對主週期與上層已收盤趨勢是否一致"}))
    defaults.append(("strategy_candidates", {"reason": "核對多空方向、進場、止損與目標是否形成有效情境"}))
    if positions:
        defaults.append(("evaluate_positions", {"reason": "核對所選持倉的保護、結構與可選動作"}))
    return [execute_tool(name, args, candles, quote, request, context_candles, positions)
            for name, args in defaults]
