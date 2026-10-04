// Reconciled outcomes of finished reports: shared by the track record page and the report pages.
import { uiText } from "./i18n/index.ts";
import { apiFetch } from "./transport";

export type OutcomeKind = "entry" | "observe" | "hold" | "close";
export type Outcome = {
  id: string;
  analysis_id: string;
  item_key: string;
  kind: OutcomeKind;
  status: "pending" | "resolved" | "unavailable";
  result: string | null;
  r_multiple: string | null;
  resolved_at: string | null;
  created_at: string;
  report_kind: "market" | "positions" | null;
  market_id: string | null;
  timeframe: string | null;
  side: "long" | "short" | null;
  action: "open_now" | "wait_for_entry" | "stand_aside" | null;
  entered_at: string | null;
  exit_price: string | null;
  fill: string | null;
  trigger: { type: "touch" | "close_above" | "close_below"; price: string } | null;
  exit_reason?: "target" | "stop" | "invalidation" | null;
  max_up_pct: string | null;
  max_down_pct: string | null;
  end_pct: string | null;
};
export type OutcomeStats = {
  total: number;
  pending: number;
  entries: { wins: number; losses: number; expired: number; not_triggered: number; unverifiable: number;
    win_rate: string | null; average_r: string | null; total_r: string | null };
  holds: { target_hit: number; invalidated: number; expired: number; average_r: string | null };
  closes: { good: number; early: number; good_rate: string | null };
  stand_aside: { count: number; average_max_up_pct: string | null; average_max_down_pct: string | null };
};
export type OutcomeSummary = {
  period: OutcomePeriod;
  current_versions: string[];
  overall: OutcomeStats;
  groups?: ({ key: string } & OutcomeStats)[];
};
export type OutcomePeriod = "30d" | "90d" | "all";
export type OutcomeGroup = "market" | "timeframe" | "risk" | "style" | "model" | "prompt";
// Fewer settled trades than this read as noise, not a record.
export const MIN_SAMPLE = 20;

export async function loadOutcomes<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch(`/api/v1${path}`, { signal });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

export function signedR(value: string | null): string {
  if (value == null) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}R`;
}

export function signedPct(value: string | null): string {
  if (value == null) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

/** The result in a word, and whether it reads as good, bad or neither. */
export function outcomeLabel(outcome: Pick<Outcome, "status" | "result" | "kind" | "entered_at" | "exit_reason">):
  { text: string; tone: "good" | "bad" | "neutral" | "pending" } {
  if (outcome.status === "unavailable") return { text: uiText("無法對帳"), tone: "neutral" };
  if (outcome.status === "pending") {
    const entered = outcome.kind !== "entry" || outcome.entered_at;
    return { text: entered ? uiText("追蹤中") : uiText("等待觸發"), tone: "pending" };
  }
  switch (outcome.result) {
    case "win": return { text: outcome.exit_reason === "invalidation" ? uiText("贏（失效出場）") : uiText("贏"), tone: "good" };
    case "loss": return { text: outcome.exit_reason === "invalidation" ? uiText("輸（失效出場）") : uiText("輸"), tone: "bad" };
    case "expired": return { text: uiText("到期未分勝負"), tone: "neutral" };
    case "not_triggered": return { text: uiText("未觸發，已過期"), tone: "neutral" };
    case "unverifiable": return { text: uiText("條件無法判斷"), tone: "neutral" };
    case "target_hit": return { text: uiText("續抱達標"), tone: "good" };
    case "invalidated": return { text: uiText("續抱失效"), tone: "bad" };
    case "good_close": return { text: uiText("平得好"), tone: "good" };
    case "early_close": return { text: uiText("平早了"), tone: "bad" };
    case "observed": return { text: uiText("觀望紀錄"), tone: "neutral" };
    default: return { text: "—", tone: "neutral" };
  }
}

export function outcomeSubject(outcome: Pick<Outcome, "kind" | "action" | "side">): string {
  const side = outcome.side === "long" ? uiText("多") : outcome.side === "short" ? uiText("空") : "";
  switch (outcome.kind) {
    case "entry": return outcome.action === "wait_for_entry"
      ? uiText("等待進場（{{p0}}）", { p0: side }) : uiText("建議進場（{{p0}}）", { p0: side });
    case "observe": return uiText("觀望");
    case "hold": return uiText("持倉續抱（{{p0}}）", { p0: side });
    case "close": return uiText("持倉平倉（{{p0}}）", { p0: side });
  }
}

/** The figure that goes with the result: R for trades and holds, the later move otherwise. */
export function outcomeFigure(outcome: Outcome): string {
  if (outcome.kind === "entry" || outcome.kind === "hold") return outcome.r_multiple ? signedR(outcome.r_multiple) : "";
  if (outcome.status !== "resolved") return "";
  return uiText("最大 {{p0}} / {{p1}}", { p0: signedPct(outcome.max_up_pct), p1: signedPct(outcome.max_down_pct) });
}
