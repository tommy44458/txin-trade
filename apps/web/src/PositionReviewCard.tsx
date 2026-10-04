import { pythonReferenceText } from "./pythonReferenceText";
import { uiText, uiLocale, type UiLocale } from "./i18n/index.ts";
import { timeframeCode } from "./timeframes";
import './PositionReviewCard.css'

export type PositionAction = {
  id: string
  kind: 'verify_execution' | 'review_protection' | 'maintain' | 'tighten_stop_review' | 'reduce_exposure_review'
  reason: string
  current_stop: string | null
  proposed_stop: string | null
  risk_change_usdt: string | null
  level_id: string | null
  selection_source?: 'openai_assisted' | 'rules_only' | 'python_reference'
}

export type PositionReview = {
  position_id: string; version: number; side: string; leverage: number
  valuation_price_type: string; unrealized_pnl_usdt: string
  theoretical_initial_margin_usdt: string; return_on_theoretical_margin_pct: string
  original_risk_reward?: string | null; remaining_risk_reward: string | null
  profitable_side_stop?: boolean; manual_stop_reached?: boolean
  previous_stop_loss?: string | null; stop_risk_delta_usdt?: string | null
  entry_time?: string | null; user_note?: string | null
  exchange_liquidation_price?: string | null
  distance_to_exchange_liquidation_pct?: string | null
  stop_price_exposure_usdt?: string | null; stop_price_exposure_pct_of_equity?: string | null
  messages: string[]; note: string
  advice?: PositionAction; available_actions?: PositionAction[]
  agent_decision?: { decision: 'hold' | 'close_now'; reason: string; exit_plan?: ExitPlan | null }
}

/** When a held position stops being worth holding, as checked against the analysis snapshot. */
export type ExitPlan = {
  version: string
  invalidation: { price: string; confirmation: 'close' | 'touch'; condition: string | null }
  protective_stop: string | null
  take_profits: { price: string; portion_pct: number | null }[]
  basis_level_ids: string[]
}

const labels: Record<PositionAction['kind'], string> = {
  get verify_execution() { return uiText("先核對實際成交"); },
  get review_protection() { return uiText("檢查保護條件"); },
  get maintain() { return uiText("維持原條件並觀察"); },
  get tighten_stop_review() { return uiText("評估收緊止損"); },
  get reduce_exposure_review() { return uiText("評估降低曝險"); },
}
const fmt = (value: string | number | null | undefined) => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 8 })

function ExitPlanSection({ plan, side, timeframe, currentStop, outputLocale, riskTolerance }: {
  plan: ExitPlan; side: string; timeframe: string; currentStop: string | null | undefined; outputLocale: UiLocale
  riskTolerance?: string | null
}) {
  const long = side === 'long'
  const exitPrice = fmt(plan.invalidation.price)
  const rule = plan.invalidation.confirmation === 'close'
    ? uiText(long ? "{{p0}} 收盤低於 ${{p1}}" : "{{p0}} 收盤高於 ${{p1}}", { p0: timeframeCode(timeframe), p1: exitPrice })
    : uiText(long ? "盤中跌破 ${{p0}}" : "盤中突破 ${{p0}}", { p0: exitPrice })
  return <section className="exit-plan" aria-label={uiText("離場計畫")}>
    <h4>{uiText("離場計畫")}</h4>
    <dl>
      <div className="exit-plan-rule">
        <dt>{uiText("失效時離場")}</dt>
        <dd><b>{rule}</b>{plan.invalidation.condition && <span lang={outputLocale}>{plan.invalidation.condition}</span>}</dd>
      </div>
      {plan.protective_stop && <div>
        <dt>{uiText("建議止損")}</dt>
        <dd><b>${fmt(plan.protective_stop)}</b><span>{currentStop ? uiText("目前 ${{p0}}", { p0: fmt(currentStop) }) : uiText("目前未設定")}</span></dd>
      </div>}
      {plan.take_profits.length > 0 && <div>
        <dt>{uiText("分批止盈")}</dt>
        <dd><ol>{plan.take_profits.map((target) => <li key={target.price}>
          <b>${fmt(target.price)}</b>{target.portion_pct != null && <span>{uiText("減碼 {{p0}}%", { p0: target.portion_pct })}</span>}
        </li>)}</ol></dd>
      </div>}
    </dl>
    {/* A high tolerance asks for room: say what that costs when the stop is hit. */}
    {riskTolerance === 'high' && <small className="exit-plan-wide">{uiText("依你選擇的高風險承擔，止損放得較寬、止盈看得較遠；止損觸發時虧損也較大，請確認倉位大小承受得起。")}</small>}
    <small>{uiText("AI 建議，不會更改你的交易所訂單。")}</small>
  </section>
}

export default function PositionReviewCard({ review, timeframe, outputLocale = "zh-TW", riskTolerance }: {
  review: PositionReview; timeframe: string; outputLocale?: UiLocale; riskTolerance?: string | null
}) {
  const advice = review.advice
  const decision = review.agent_decision
  return <div className="review position-review">
    <div className="position-review-head"><b>{review.side === 'long' ? uiText("多單") : uiText("空單")} · {review.leverage}×</b>
      {review.agent_decision && <span>{uiText("Agent 決定")}</span>}
    </div>
    {review.agent_decision && <div className="position-review-advice"><strong>{review.agent_decision.decision === 'hold' ? uiText("Agent 建議：繼續持倉") : uiText("Agent 建議：現在平倉")}</strong><p lang={outputLocale}>{review.agent_decision.reason}</p></div>}{" "}
    {decision?.decision === 'hold' && (decision.exit_plan
      ? <ExitPlanSection plan={decision.exit_plan} side={review.side} timeframe={timeframe} currentStop={advice?.current_stop} outputLocale={outputLocale} riskTolerance={riskTolerance} />
      // Reports from before exit plans have no exit_plan field at all; only say it is missing when it was checked.
      : decision.exit_plan === null && <small className="exit-plan-missing">{uiText("這筆續抱建議沒有附上可核對的離場計畫。")}</small>)}
    {!review.agent_decision && <small>{uiText("這筆持倉尚未有 AI 的續抱／平倉建議，請重新分析。")}</small>}
    {advice && <details><summary>{uiText("查看風險估算參考")}</summary><p>{labels[advice.kind]}：{advice.selection_source === "python_reference" || advice.selection_source === "rules_only" ? pythonReferenceText(advice.reason, { kind: advice.kind, origin: "python" }) : advice.reason}</p>
      {advice.proposed_stop && <p>{uiText("規則估算止損 $")}{fmt(advice.current_stop)} → ${fmt(advice.proposed_stop)}{uiText("；風險變化") + " "}{fmt(advice.risk_change_usdt)}{" " + uiText("USDT。這不是 Agent 的最終決定，也不會更改訂單。")}</p>}
    </details>}
    <details><summary>{uiText("查看持倉數值與風險估算")}</summary>
    <small>{review.valuation_price_type === 'mark' ? uiText("標記價") : uiText("最新成交價")}{" "}{uiText("估算損益 $")}{fmt(review.unrealized_pnl_usdt)}{" " + uiText("USDT · 理論保證金 $")}{fmt(review.theoretical_initial_margin_usdt)}{" " + uiText("· 理論報酬率") + " "}{fmt(review.return_on_theoretical_margin_pct)}{uiText("% · 原始風報比") + " "}{review.original_risk_reward ?? '—'}{" " + uiText("· 剩餘風報比") + " "}{review.remaining_risk_reward ?? '—'}</small>
    {review.entry_time && <small>{uiText("進場時間：")}{" "}{new Date(review.entry_time).toLocaleString(uiLocale())}</small>}{" "}
    {review.user_note && <small>{uiText("分析當時的備註：")}{" "}{review.user_note}</small>}
    {review.exchange_liquidation_price && <small>{uiText("紀錄的強平價 $")}{fmt(review.exchange_liquidation_price)}{" " + uiText("· 參考價距離") + " "}{fmt(review.distance_to_exchange_liquidation_pct)}{uiText("%（非即時保證）")}</small>}
    {review.stop_price_exposure_pct_of_equity != null && <small>{uiText("由目前參考價至登記止損的價格曝險約 $")}{fmt(review.stop_price_exposure_usdt)}{" " + uiText("USDT，占當次填寫帳戶權益") + " "}{fmt(review.stop_price_exposure_pct_of_equity)}{uiText("%；未計費用或滑價，不代表最大損失。")}</small>}
    {review.stop_risk_delta_usdt != null && <small>{uiText("上次手動調整止損後，至止損的價格風險變化") + " "}{fmt(review.stop_risk_delta_usdt)}{" " + uiText("USDT（負值代表降低）")}</small>}
    {review.messages.map((message, index) => <small key={index}>{pythonReferenceText(message, { origin: "python" })}</small>)}
    </details>
  </div>
}
