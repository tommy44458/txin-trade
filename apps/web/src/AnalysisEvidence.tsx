import { economicMetricLabel } from "./economicLabels";
import type { DirectionAssessmentData } from "./DirectionAssessment";
import { pythonReferenceText } from "./pythonReferenceText";
import { uiText, uiLocale, languageName, type UiLocale } from "./i18n/index.ts";
import { useEffect, useState } from "react";
import TechnicalIndicators, {
  type TechnicalSnapshot,
  type AnalysisExecution,
} from "./TechnicalIndicators";
import type { MacroInterpretationStatus } from "./MacroInterpretationPanel";
import { timeframeCode } from "./timeframes";

type ToolRun = {
  tool: string;
  reason: string;
  parameters: Record<string, unknown>;
  result: Record<string, unknown>;
  execution_source?: string;
};
export type Reasoning = {
  market: string;
  levels: string;
  strategy: string;
  evidence_tools: string[];
  supporting_evidence?: string;
  counter_evidence?: string;
  agent_stance?: "long" | "short" | "wait";
  direction_assessment?: DirectionAssessmentData;
  macro_outlook?: {
    stance: "bullish" | "bearish" | "neutral";
    reason: string;
    evidence_ids: string[];
  } | null;
};
export type EntryDecision = {
  action: "open_now" | "wait_for_entry" | "stand_aside";
  side: "long" | "short" | null;
  entry_price: string | null;
  stop_loss: string | null;
  take_profit: string | null;
  trigger: string | null;
  invalidation: string | null;
  reason: string;
  basis_level_ids: string[];
};
export type EntryRiskReference = {
  net_risk_reward: string | null;
  risk_on_theoretical_margin_pct: string;
};
export type MacroContext = {
  coverage: "partial" | "insufficient";
  from: string;
  cutoff: string;
  directional_evidence: Array<{
    id: string;
    kind: string;
    source_url: string;
    published_at: string;
    decision?: string;
    lower_pct?: string;
    upper_pct?: string;
    label?: string;
    metric?: string;
    period?: string;
    value?: string;
    unit?: string;
    previous_value?: string | null;
    time_basis?: string;
  }>;
  missing_actuals: string[];
};
export type CurrentCandle = {
  status: "available" | "quote_only";
  confirmation: "unclosed";
  timeframe: string;
  quote_time: string;
  quote_price: string;
  last_closed_close: string;
  price_vs_last_close: "above" | "below" | "equal";
  change_from_last_close_pct: string;
  candle?: {
    open_time: string;
    close_time: string;
    open: string;
    high: string;
    low: string;
    close: string;
    volume: string;
  } | null;
  price_vs_open?: "above" | "below" | "equal" | null;
  change_from_open_pct?: string | null;
  quote_inside_observed_range?: boolean | null;
};

const planPrice = (value: string | null) =>
  value == null ? uiText("未提供") : `$${Number(value).toLocaleString("en-US")} USDT`;

const names: Record<string, string> = {
  get trend_ema() { return uiText("EMA 趨勢"); },
  get rsi() { return uiText("RSI 動能"); },
  macd: "MACD",
  get bollinger() { return uiText("布林通道"); },
  get volatility_atr() { return uiText("ATR 波動"); },
  get swing_points() { return uiText("已確認波段"); },
  get support_resistance() { return uiText("支撐壓力"); },
  get volume_signal() { return uiText("成交量"); },
  get rolling_vwap() { return uiText("滾動 VWAP"); },
  get fibonacci() { return uiText("斐波那契回撤"); },
  get adx_dmi() { return uiText("平均趨向指標 ADX / DMI"); },
  get obv() { return uiText("能量潮 OBV"); },
  get donchian() { return uiText("唐奇安通道"); },
  get keltner() { return uiText("肯特納通道"); },
  get stochastic() { return uiText("隨機指標"); },
  get funding_context() { return uiText("資金費與標記價"); },
  get compare_timeframes() { return uiText("主週期與大週期比較"); },
  get strategy_candidates() { return uiText("策略候選"); },
  get evaluate_positions() { return uiText("持倉風險與動作"); },
  get technical_snapshot() { return uiText("常用指標與大週期背景"); },
};

function resultSummary(run: ToolRun): string {
  const result = run.result;
  if (run.tool === "technical_snapshot") {
    const snapshot = result as unknown as TechnicalSnapshot;
    const frames = snapshot.analysis_timeframes ?? Object.keys({
      ...snapshot.timeframes, ...snapshot.higher_timeframe_context?.timeframes,
    });
    return uiText("已預先計算 {{p0}} 常用指標{{p1}}；請查看各週期資料狀態。", { p0: frames.map(timeframeCode).join("／"), p1: result.higher_timeframe_context ? uiText("，另附大週期趨勢、量能與現價位置") : "" });
  }
  if (run.tool === "support_resistance" && Array.isArray(result.levels)) {
    const support = result.levels.filter(
      (level: { kind?: string }) => level.kind === "support",
    ).length;
    const resistance = result.levels.filter(
      (level: { kind?: string }) => level.kind === "resistance",
    ).length;
    return (
      uiText("已確認支撐") + " " +
      support +
      " " + uiText("區、壓力") + " " +
      resistance +
      " " + uiText("區；策略使用 v3 區間。")
    );
  }
  if (run.tool === "strategy_candidates" && Array.isArray(result.candidates)) {
    const titles = result.candidates
      .map((candidate: { title?: string; type?: string }) => pythonReferenceText(candidate.title, { type: candidate.type, origin: "python" }))
      .filter(Boolean);
    return (
      uiText("已計算") + " " +
      titles.length +
      " " + uiText("個參考情境：") +
      (titles.join("、") || uiText("無有效情境")) +
      "。"
    );
  }
  if (run.tool === "compare_timeframes") {
    const relation: Record<string, string> = {
      aligned: uiText("方向一致"),
      conflict: uiText("方向衝突"),
      range: uiText("區間盤整"),
      uncertain: uiText("方向未確認"),
      unavailable: uiText("資料不足"),
    };
    return (
      uiText("已收盤週期比較：") +
      (relation[String(result.relation)] ?? uiText("請查看原始結果")) +
      "。"
    );
  }
  if (run.tool === "volume_signal")
    return (
      uiText("所選週期最新成交量相對前期基準：") +
      (result.relative_volume ?? uiText("無法計算")) +
      " " + uiText("倍。")
    );
  if (run.tool === "evaluate_positions" && Array.isArray(result.positions))
    return uiText("已檢核") + " " + result.positions.length + " " + uiText("筆所選持倉的候選動作。");
  if (run.tool === "trend_ema")
    return (
      uiText("EMA 方向：") +
      (result.direction === "bullish"
        ? uiText("偏多")
        : result.direction === "bearish"
          ? uiText("偏空")
          : uiText("混合")) +
      uiText("；方向標籤僅供 Agent 綜合判斷參考。")
    );
  if (run.tool === "funding_context")
    return uiText("已讀取標記價、指數價與資金費背景；不單獨用來推定價格方向。");
  return uiText("指標計算已完成；可展開查看參數與原始結果。");
}

export default function AnalysisEvidence({
  mode,
  outputLocale = "zh-TW",
  reasoning,
  runs,
  fallbackReason,
  entryDecision,
  entryRiskReference,
  analysisLeverage,
  macroContext,
  macroInterpretation,
  currentCandle,
  snapshotObservedAt,
  generatedAt,
  execution,
  positionAnalysis = false,
  riskTolerance,
}: {
  mode: string;
  outputLocale?: UiLocale;
  reasoning?: Reasoning;
  runs?: ToolRun[];
  fallbackReason?: string | null;
  entryDecision?: EntryDecision | null;
  entryRiskReference?: EntryRiskReference | null;
  analysisLeverage?: number;
  macroContext?: MacroContext | null;
  macroInterpretation?: MacroInterpretationStatus | null;
  currentCandle?: CurrentCandle;
  snapshotObservedAt?: string;
  generatedAt?: string;
  positionAnalysis?: boolean;
  execution?: AnalysisExecution | null;
  riskTolerance?: string | null;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  if (!reasoning || !runs?.length) return null;
  const strategyReason = mode === "rules_only"
    ? pythonReferenceText(reasoning.strategy, { origin: "python" })
    : reasoning.strategy;
  const savedMacro = macroInterpretation?.status === "succeeded" && !macroInterpretation.stale
    ? macroInterpretation.interpretation : null;
  const macroEvidence = savedMacro?.evidence ?? macroContext?.directional_evidence ?? [];
  const cited = new Set(reasoning.evidence_tools);
  const technical = runs.find((run) => run.tool === "technical_snapshot")
    ?.result as TechnicalSnapshot | undefined;
  const precomputedCount = runs.filter(
    (run) => run.execution_source === "precomputed",
  ).length;
  const snapshotAge = snapshotObservedAt
    ? (Math.max(now, generatedAt ? Date.parse(generatedAt) : 0) -
        Date.parse(snapshotObservedAt)) /
      1000
    : 0;
  const direction =
    reasoning.agent_stance === "long"
      ? uiText("偏多")
      : reasoning.agent_stance === "short"
        ? uiText("偏空")
        : uiText("觀望");
  const action = positionAnalysis
    ? uiText("持倉判斷依據")
    : !entryDecision
      ? uiText("AI 市場判斷")
      : entryDecision.action === "stand_aside"
        ? uiText("觀望")
        : entryDecision.action === "wait_for_entry"
          ? uiText("等待{{p0}}條件", { p0: entryDecision.side === "long" ? uiText("做多") : entryDecision.side === "short" ? uiText("做空") : uiText("進場") })
          : uiText("{{p0}}{{p1}}單", { p0: snapshotAge > 60 ? uiText("當時建議開") : uiText("現在開"), p1: entryDecision.side === "long" ? uiText("多") : entryDecision.side === "short" ? uiText("空") : "" });
  return (
    <section
      className="panel evidence-panel ai-report"
      aria-label={uiText("AI 分析報告")}
    >
      <header className="ai-report-header">
        <div>
          <span>{mode === "openai_assisted" ? uiText("AI 分析") : uiText("規則參考")}</span>
          <small className="analysis-language">{uiText("報告語言")}: {languageName(outputLocale)}</small>
        </div>
        <time dateTime={snapshotObservedAt}>
          {snapshotObservedAt &&
            new Date(snapshotObservedAt).toLocaleString(uiLocale(), {
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
              hour12: false,
            })}
        </time>
      </header>
      {fallbackReason && (
        <div className="warning" role="status">{uiText("AI 未完成，本次為計算備援結果：")}{" "}{fallbackReason}
        </div>
      )}
      <div className="ai-decision">
        <div className="decision-eyebrow">
          <span>{positionAnalysis ? uiText("持倉分析") : uiText("交易建議")}</span>
          {reasoning.agent_stance && (
            <span className={`stance-pill ${reasoning.agent_stance}`}>
              {direction}
            </span>
          )}
        </div>
        <h2>{action}</h2>
        <p lang={outputLocale}>
          {positionAnalysis
            ? reasoning.market
            : entryDecision?.reason || strategyReason || reasoning.market}
        </p>
        <div className="decision-context">
          {currentCandle && (
            <span>{uiText("分析時現價")}<b>{planPrice(currentCandle.quote_price)}</b>
            </span>
          )}
          <span>
            {Number.isFinite(snapshotAge) && snapshotAge > 60
              ? uiText("歷史分析 · 可重新分析更新")
              : uiText("本次分析快照")}
          </span>
        </div>
      </div>
      {!positionAnalysis &&
        entryDecision &&
        entryDecision.action !== "stand_aside" && (
          <div className="ai-plan">
            <dl className="plan-prices">
              {[
                [uiText("參考進場"), entryDecision.entry_price],
                [uiText("止損"), entryDecision.stop_loss],
                [uiText("止盈"), entryDecision.take_profit],
              ].map(([label, price]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{planPrice(price)}</dd>
                </div>
              ))}
            </dl>
            <div className="plan-conditions">
              <div>
                <span>{uiText("進場條件")}</span>
                <p lang={entryDecision.trigger ? outputLocale : uiLocale()}>{entryDecision.trigger || uiText("模型未提供")}</p>
              </div>
              <div>
                <span>{uiText("方案失效")}</span>
                <p lang={entryDecision.invalidation ? outputLocale : uiLocale()}>{entryDecision.invalidation || uiText("模型未提供")}</p>
              </div>
            </div>
            {/* A high tolerance asks for room: say what that costs when the stop is hit. */}
            {riskTolerance === "high" && <p className="plan-risk-wide">{uiText("依你選擇的高風險承擔，止損放得較寬、止盈看得較遠；止損觸發時虧損也較大，請確認倉位大小承受得起。")}</p>}
            {entryRiskReference && (
              <details className="cost-reference">
                <summary>{uiText("成本與風報比參考")}</summary>
                <p>{uiText("示例成本後風報比")}{" "}
                  {entryRiskReference.net_risk_reward ?? uiText("未提供")}{uiText("；以")}{" "}
                  {analysisLeverage ?? "—"}{" "}{uiText("× 估算的理論保證金風險")}{" "}
                  {entryRiskReference.risk_on_theoretical_margin_pct}{uiText("%。未指定倉位大小，未送出委託。")}</p>
              </details>
            )}
          </div>
        )}
      <div className="ai-reasons">
        <article>
          <div>
            <h3>{uiText("建議理由")}</h3>
            <p lang={reasoning.supporting_evidence || strategyReason ? outputLocale : uiLocale()}>
              {reasoning.supporting_evidence ||
                strategyReason ||
                uiText("模型未提供進一步理由。")}
            </p>
          </div>
        </article>
        <article className="change-of-view">
          <div>
            <h3>{uiText("什麼情況要改看法")}</h3>
            <p lang={reasoning.counter_evidence ? outputLocale : uiLocale()}>{reasoning.counter_evidence || uiText("模型未提供改看法條件。")}</p>
          </div>
        </article>
      </div>
      {reasoning.macro_outlook && (
        <details className="report-disclosure macro-disclosure">
          <summary>
            <span>{uiText("宏觀市場判斷")}</span>
            <span className="disclosure-hint">
              {reasoning.macro_outlook.stance === "bullish"
                ? uiText("偏多")
                : reasoning.macro_outlook.stance === "bearish"
                  ? uiText("偏空")
                  : uiText("中性／證據不足")}
            </span>
          </summary>
          <p lang={outputLocale}>{reasoning.macro_outlook.reason}</p>
          {savedMacro ? (
            <p className="muted">{uiText("引用經濟事件頁共用的 AI 解讀 ·")}{" "}{new Date(savedMacro.generated_at).toLocaleString(uiLocale(), { hour12: false })}{" "}{uiText("生成 · 資料版本")}{" "}{savedMacro.dataset_version.slice(0, 8)}{uiText("。這是本次分析保存的版本。")}</p>
          ) : macroContext && (
            <p className="muted">{uiText("近期宏觀證據")}{" "}{macroContext.directional_evidence.length}{" "}{uiText("筆。")}{" "}{macroContext.missing_actuals.length > 0 &&
                uiText("尚缺：{{p0}}。", { p0: macroContext.missing_actuals.join("、") })}
            </p>
          )}
          {macroEvidence
            .filter((item) =>
              reasoning.macro_outlook?.evidence_ids.includes(item.id),
            )
            .map((item) => (
              <p key={item.id}>
                <a href={item.source_url} target="_blank" rel="noreferrer">
                  {item.label ? economicMetricLabel(item.label, item.metric) : uiText("官方 FOMC 聲明")}
                </a>
                ：
                {item.value != null
                  ? `${item.period} ${item.value}${item.unit === "thousand_jobs" ? " " + uiText("千人") : "%"}`
                  : item.lower_pct != null && item.upper_pct != null
                    ? `${item.decision}，${item.lower_pct}–${item.upper_pct}%`
                    : uiText("官方發布資料")}
              </p>
            ))}
        </details>
      )}
      {macroInterpretation && !reasoning.macro_outlook && (
        <details className="report-disclosure macro-disclosure">
          <summary><span>{uiText("宏觀市場判斷")}</span><span className="disclosure-hint">{uiText("本次未採用")}</span></summary>
          <p>{macroInterpretation.error?.message ?? (macroInterpretation.status === "insufficient"
            ? uiText("官方指標不足，本次沒有可採用的 AI 宏觀解讀。")
            : uiText("本次尚未取得此資料版本的 AI 宏觀解讀，交易分析仍依行情證據判斷。"))}</p>
        </details>
      )}
      <details className="report-disclosure evidence-context">
        <summary>
          <span>{uiText("完整市場解讀")}</span>
          <span className="disclosure-hint">{uiText("方向、價位與盤中反應")}</span>
        </summary>
        <div className="full-reasoning">
          <h3>{uiText("市場方向")}</h3>
          <p lang={outputLocale}>{reasoning.market}</p>
          <h3>{uiText("支撐與壓力")}</h3>
          <p lang={outputLocale}>{reasoning.levels}</p>
          <h3>{uiText("策略取捨")}</h3>
          <p lang={outputLocale}>{strategyReason}</p>
        </div>
        {currentCandle && (
          <div className="current-candle-card">
            <h3>{uiText("分析時的本根 K 線 · 尚未收盤")}</h3>
            <p>{uiText("現價")}{" "}{planPrice(currentCandle.quote_price)}{uiText("；上一根收盤")}{" "}
              {planPrice(currentCandle.last_closed_close)}。
            </p>
            {currentCandle.candle && (
              <p>{uiText("本根開盤")}{" "}{planPrice(currentCandle.candle.open)}{uiText("；盤中低點")}{" "}
                {planPrice(currentCandle.candle.low)}{uiText("，高點")}{" "}
                {planPrice(currentCandle.candle.high)}。
              </p>
            )}
            <small>{uiText("盤中反應不等於收盤確認，盤中成交量也不直接與完整 K 線相比。")}</small>
          </div>
        )}
      </details>
      <details className="report-disclosure calculation-details">
        <summary>
          <span>{uiText("指標與計算依據")}</span>
          <span className="disclosure-hint">{runs.length}{" " + uiText("組計算依據")}</span>
        </summary>
        {technical && (
          <TechnicalIndicators snapshot={technical} execution={execution} />
        )}
        <div className="tool-heading">
          <div>
            <strong>{uiText("計算紀錄")}</strong>
            <small>{uiText("預先計算")}{" "}{precomputedCount}{" " + uiText("組；追加")}{" "}
              {runs.length - precomputedCount}{" "}{uiText("次。")}</small>
          </div>
        </div>
        <ol className="tool-list">
          {runs.map((run, i) => (
            <li key={run.tool + "-" + i}>
              <details>
                <summary>
                  <span className="tool-step">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="tool-main">
                    <strong>
                      {names[run.tool] ?? run.tool}
                      {typeof run.parameters.timeframe === "string" &&
                        ` · ${run.parameters.timeframe.toUpperCase()}`}
                    </strong>
                    <code>{run.tool}</code>
                    <small>{run.execution_source === "precomputed" || !run.execution_source ? pythonReferenceText(run.reason, { tool: run.tool, origin: "python" }) : run.reason}</small>
                    <em>{resultSummary(run)}</em>
                  </span>
                  <span className="tool-side">
                    {cited.has(run.tool) && <mark>{uiText("策略引用")}</mark>}
                    <b>{uiText("查看 ＋")}</b>
                  </span>
                </summary>
                <pre>
                  {JSON.stringify(
                    { parameters: run.parameters, result: run.result },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </li>
          ))}
        </ol>
      </details>
    </section>
  );
}
