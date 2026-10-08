import { uiText } from "./i18n/index.ts";
import "./DirectionAssessment.css";

type Side = "long" | "short";
type Verdict = { verdict: "reasonable" | "conditional" | "unsuitable"; reason: string };
export type DirectionAssessmentData = Partial<Record<Side, Verdict>> | null;
/** The AI's answer to the direction the trader asked about. */
export type BiasAnswer = {
  direction: Side;
  verdict: Verdict["verdict"];
  recommendation: "enter" | "wait_for_entry" | "stand_aside" | "reverse";
  reason: string;
  derived?: boolean;
  adjusted?: "plan_mismatch" | "reverse_not_supported";
} | null;

const verdictLabel = (value: Verdict["verdict"]) => value === "reasonable" ? uiText("現在合理")
  : value === "conditional" ? uiText("有條件") : uiText("現在不適合");

const sideLabel = (side: Side) => side === "long" ? uiText("做多") : uiText("做空");

const recommendationLabel = (value: NonNullable<BiasAnswer>["recommendation"], side: Side) =>
  value === "enter" ? uiText("建議進場")
    : value === "wait_for_entry" ? uiText("等待進場條件")
      : value === "reverse" ? uiText("建議反手{{p0}}", { p0: sideLabel(side === "long" ? "short" : "long") })
        : uiText("建議觀望");

/** Both directions judged on the same evidence; a chosen direction is answered first, as a question. */
export default function DirectionAssessment({ assessment, bias, answer }: {
  assessment?: DirectionAssessmentData;
  bias?: string | null;
  answer?: BiasAnswer;
}) {
  if (!assessment) return null;
  const mine: Side | null = bias === "bullish" ? "long" : bias === "bearish" ? "short" : null;
  // The user's own direction comes first so a mismatch is the first thing read.
  const sides = (["long", "short"] as const).filter((side) => side === mine || assessment[side])
    .sort((a, b) => Number(b === mine) - Number(a === mine));
  return (
    <section className="panel direction-assessment" aria-label={uiText("多空方向評估")}>
      <header className="ai-report-header">
        <div><span>{uiText("多空方向評估")}</span></div>
        <small className="direction-assessment-note">{mine
          ? uiText("你的方向是提問，AI 依同一套證據回答，不會因此改變判斷")
          : uiText("未指定方向，AI 自行評估多空")}</small>
      </header>
      {answer && (
        <div className={`bias-answer ${answer.verdict}`}>
          <p className="bias-answer-head">
            <strong>{uiText("你想{{p0}}", { p0: sideLabel(answer.direction) })}</strong>
            <span className={`verdict-pill ${answer.verdict}`}>{verdictLabel(answer.verdict)}</span>
            <b>{recommendationLabel(answer.recommendation, answer.direction)}</b>
          </p>
          <p>{answer.reason}</p>
          {answer.adjusted === "reverse_not_supported" && (
            <small>{uiText("AI 建議反手，但反方向沒有被評為現在合理，因此改為觀望。")}</small>
          )}
          {answer.adjusted === "plan_mismatch" && (
            <small>{uiText("AI 給出的開單方向與回答不一致，這份方案不列為建議。")}</small>
          )}
        </div>
      )}
      <ul className="direction-list">
        {sides.map((side) => {
          const item = assessment[side];
          return (
            <li key={side} className={`direction-row${side === mine && item?.verdict === "unsuitable" ? " flagged" : ""}`}>
              <div className="direction-row-head">
                <span className={`stance-pill ${side}`}>{sideLabel(side)}</span>
                {item && <span className={`verdict-pill ${item.verdict}`}>{verdictLabel(item.verdict)}</span>}
                {side === mine && <span className="tag">{uiText("你的提問")}</span>}
              </div>
              <p>{item ? item.reason : uiText("AI 本次未回傳此方向的評估。")}</p>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
