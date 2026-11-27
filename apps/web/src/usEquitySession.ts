import { uiLocale, uiText } from "./i18n/index.ts";

/** Where the US stock market's day stood, for Binance perpetuals that track US stocks. */
export type UsEquitySession = {
  status: "available" | "calendar_unavailable";
  state?: "regular" | "pre_market" | "after_hours" | "overnight" | "weekend" | "holiday";
  early_close_today?: boolean;
  next_regular_open?: string | null;
  last_regular_close?: string | null;
  recent_candles?: { timeframe: string; count: number; regular_session_share_pct: number | null };
};

const STATE_LABELS = {
  get regular() { return uiText("美股盤中"); },
  get pre_market() { return uiText("美股盤前"); },
  get after_hours() { return uiText("美股盤後"); },
  get overnight() { return uiText("美股夜間休市"); },
  get weekend() { return uiText("美股週末休市"); },
  get holiday() { return uiText("美股假日休市"); },
};

/** One line: the session state, an early close, and the next regular open in local time. */
export function usEquitySessionText(session: UsEquitySession): string {
  if (session.status !== "available" || !session.state) return uiText("美股交易時段未知");
  const parts = [STATE_LABELS[session.state]];
  if (session.early_close_today) parts.push(uiText("今天提早在美東 13:00 收盤"));
  if (session.state !== "regular" && session.next_regular_open) {
    parts.push(uiText("下次開盤 {{p0}}", {
      p0: new Date(session.next_regular_open).toLocaleString(uiLocale(), {
        month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
      }),
    }));
  }
  const share = session.recent_candles?.regular_session_share_pct;
  if (share != null) {
    parts.push(uiText("最近 {{p0}} 根 K 線有 {{p1}}% 落在正規交易時段", { p0: session.recent_candles!.count, p1: share }));
  }
  return parts.join(" · ");
}
