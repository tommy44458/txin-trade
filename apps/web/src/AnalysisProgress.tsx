import { uiText, type UiLocale } from "./i18n/index.ts";
import { localizeReportText } from "./zhPunctuation.ts";
import "./AnalysisProgress.css";

export type AnalysisKind = "market" | "positions";

type Zone = { low: string; high: string };
type DraftSection = { text: string; complete: boolean };
/** What a running analysis can already show; the finished report replaces it. */
export type AnalysisProgressData = {
  market?: { market_id: string; timeframe: string; price: string; observed_at?: string | null };
  evidence?: {
    trend?: string | null; ma20?: string | null; ma50?: string | null; atr14?: string | null;
    support?: Zone[]; resistance?: Zone[];
  };
  draft?: Partial<Record<"market" | "levels" | "strategy" | "supporting_evidence" | "counter_evidence", DraftSection>>;
};

export function AnalysisSpinner() {
  return <span className="analysis-spinner" aria-hidden="true" />;
}

function phaseMessage(kind: AnalysisKind, phase: string) {
  switch (phase) {
    case "submitting":
      return uiText("正在送出分析請求…");
    case "queued":
      return uiText("分析已排隊，等待開始…");
    case "retrying":
      return uiText("正在重新取得行情資料…");
    case "macro":
      return uiText("正在取得共用的 AI 宏觀解讀；指標更新時會先重新分析…");
    case "fetching":
      return uiText("正在取得行情與市場背景…");
    case "calculating":
      return uiText("正在計算指標與支撐壓力…");
    case "model":
      return kind === "positions"
        ? uiText("AI 正在評估續抱或平倉，並整理理由…")
        : uiText("AI 正在評估交易策略，並整理理由…");
    default:
      return uiText("分析進行中，請稍候…");
  }
}

const PHASE_ORDER = ["submitting", "queued", "retrying", "macro", "fetching", "calculating", "model"];

const price = (value?: string | null) => {
  if (value == null) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return value;
  const digits = Math.abs(number) >= 1000 ? 2 : Math.abs(number) >= 1 ? 4 : 6;
  return number.toLocaleString("en-US", { maximumFractionDigits: digits });
};

const trendLabel = (trend?: string | null) =>
  trend === "bullish" ? uiText("偏多") : trend === "bearish" ? uiText("偏空") : trend === "mixed" ? uiText("方向不一") : "—";

function Step({ done, active, label }: { done: boolean; active: boolean; label: string }) {
  return (
    <li className={done ? "is-done" : active ? "is-active" : undefined}>
      {active && !done ? <AnalysisSpinner /> : <span className="analysis-step-mark" aria-hidden="true" />}
      <span>{label}</span>
    </li>
  );
}

function Zones({ title, zones }: { title: string; zones?: Zone[] }) {
  if (!zones?.length) return null;
  return (
    <div className="analysis-live-zones">
      <dt>{title}</dt>
      <dd>{zones.map((zone) => <span key={`${zone.low}-${zone.high}`}>{price(zone.low)}–{price(zone.high)}</span>)}</dd>
    </div>
  );
}

export default function AnalysisProgress({
  kind,
  phase,
  progress,
  outputLocale = "zh-TW",
}: {
  kind: AnalysisKind;
  phase: string;
  progress?: AnalysisProgressData | null;
  outputLocale?: UiLocale;
}) {
  const reached = PHASE_ORDER.indexOf(phase);
  const quoteDone = !!progress?.market || reached > PHASE_ORDER.indexOf("fetching");
  const evidenceDone = !!progress?.evidence || reached > PHASE_ORDER.indexOf("calculating");
  const evidence = progress?.evidence;
  const draft = progress?.draft ?? {};
  const sections = ([
    ["market", uiText("市場方向")],
    ["levels", uiText("支撐與壓力")],
    ["strategy", uiText("策略取捨")],
    ["supporting_evidence", uiText("建議理由")],
    ["counter_evidence", uiText("什麼情況要改看法")],
  ] as const).filter(([key]) => draft[key]?.text);
  const writing = sections.length > 0;
  return (
    <div className="analysis-progress" role="status" aria-live="polite" aria-busy="true">
      <div className="analysis-progress-head">
        <AnalysisSpinner />
        <div>
          <strong>
            {kind === "positions" ? uiText("正在分析持倉") : uiText("正在分析市場")}
          </strong>
          <p>{writing ? uiText("AI 正在撰寫報告，已完成的段落會先顯示…") : phaseMessage(kind, phase)}</p>
        </div>
      </div>
      <ol className="analysis-steps">
        <Step done={quoteDone} active={!quoteDone} label={uiText("取得行情")} />
        <Step done={evidenceDone} active={quoteDone && !evidenceDone} label={uiText("計算指標與支撐壓力")} />
        <Step done={false} active={evidenceDone} label={kind === "positions" ? uiText("AI 評估持倉") : uiText("AI 評估策略")} />
      </ol>
      {(progress?.market || evidence) && (
        <dl className="analysis-live-facts">
          {progress?.market && (
            <div>
              <dt>{uiText("分析時現價")}</dt>
              <dd className="analysis-live-price">{price(progress.market.price)}</dd>
            </div>
          )}
          {evidence?.trend && (
            <div>
              <dt>{uiText("主週期趨勢")}</dt>
              <dd>{trendLabel(evidence.trend)}</dd>
            </div>
          )}
          {evidence?.atr14 && (
            <div>
              <dt>ATR 14</dt>
              <dd>{price(evidence.atr14)}</dd>
            </div>
          )}
          <Zones title={uiText("附近壓力")} zones={evidence?.resistance} />
          <Zones title={uiText("附近支撐")} zones={evidence?.support} />
        </dl>
      )}
      {writing && (
        <div className="analysis-live-draft">
          {sections.map(([key, title]) => {
            const section = draft[key]!;
            return (
              <section key={key}>
                <h3>{title}</h3>
                <p lang={outputLocale} className={section.complete ? undefined : "is-writing"}>
                  {localizeReportText(section.text, outputLocale)}
                </p>
              </section>
            );
          })}
          <small>{uiText("AI 仍在撰寫，完成後會換成完整報告與價位方案。")}</small>
        </div>
      )}
    </div>
  );
}
