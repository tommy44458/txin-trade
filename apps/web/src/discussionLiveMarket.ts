import { uiText } from "./i18n/index.ts";
import { timeframeCode } from "./timeframes.ts";
import type { ChartCandle } from "./candlestickData.ts";

/** v2 adds a fresh snapshot: zones, indicators, derivatives, liquidations and fund flows. */
type DiscussionSnapshot = {
  status: "available" | "partial" | "unavailable";
  as_of?: string;
  reason?: string;
  errors?: Record<string, string>;
};

export type DiscussionLiveMarket = {
  version: "discussion_live_market_v1" | "discussion_live_market_v2";
  status: "available" | "partial" | "unavailable";
  market_id: string;
  timeframe: string;
  source: "binance_usdt_perpetual";
  requested_at: string;
  observed_at: string | null;
  quote: {
    price: string;
    observed_at: string;
    exchange_at?: string | null;
    tick_size?: string | null;
  } | null;
  forming_candle: ChartCandle | null;
  recent_closed_candles: ChartCandle[];
  comparison: { analysis_price: string | null; change_since_analysis_pct: string | null } | null;
  errors: Record<string, string>;
  not_refreshed: string[];
  snapshot?: DiscussionSnapshot;
};

function decimalParts(value: unknown): { integer: string; fraction: string } | null {
  if (typeof value !== "string" || value.length > 128 || !Number.isFinite(Number(value)) || Number(value) <= 0) return null;
  const match = value.trim().match(/^(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);
  if (!match) return null;
  const exponent = Number(match[3] ?? 0);
  if (!Number.isInteger(exponent) || Math.abs(exponent) > 64) return null;
  const digits = match[1] + (match[2] ?? "");
  const point = match[1].length + exponent;
  const expanded = point <= 0 ? `0.${"0".repeat(-point)}${digits}`
    : point >= digits.length ? digits + "0".repeat(point - digits.length)
      : digits.slice(0, point) + "." + digits.slice(point);
  const [integer, fraction = ""] = expanded.split(".");
  return { integer: integer.replace(/^0+(?=\d)/, ""), fraction };
}

/** Keep exact decimal digits, including small-contract precision, without rounding through Number. */
export function formatDiscussionLivePrice(price: unknown, tickSize?: unknown): string | null {
  const value = decimalParts(price);
  if (!value) return null;
  const tick = decimalParts(tickSize);
  const precision = tick?.fraction.replace(/0+$/, "").length ?? 0;
  const fraction = value.fraction.replace(/0+$/, "").padEnd(precision, "0");
  return value.integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction ? "." + fraction : "");
}

function validTime(value: unknown): string | null {
  return typeof value === "string" && value.trim() && Number.isFinite(Date.parse(value)) ? value : null;
}

export type DiscussionLiveMarketView = {
  status: "available" | "partial" | "unavailable";
  pair: string;
  timeframe: string;
  source: string;
  price: string | null;
  observedAt: string | null;
  timeBasis: "quote" | "request";
  note: string | null;
  /** Whether this turn recalculated the market; null for replies from before snapshots. */
  snapshot: "available" | "partial" | "unavailable" | null;
};

/** A historical reply keeps its own quote; the selected UI market and today's clock never replace it. */
export function discussionLiveMarketView(value: DiscussionLiveMarket | null | undefined,
  subjectMarketId?: string): DiscussionLiveMarketView | null {
  if (!value || (value.version !== "discussion_live_market_v1" && value.version !== "discussion_live_market_v2")) return null;
  const marketId = subjectMarketId ?? value.market_id;
  const symbol = marketId.match(/^binance:perp:([\p{L}\p{N}_]+)USDT$/u);
  const matches = marketId === value.market_id && value.source === "binance_usdt_perpetual";
  const quoteTime = validTime(value.quote?.observed_at);
  const quotePrice = matches && value.status !== "unavailable" && quoteTime
    ? formatDiscussionLivePrice(value.quote?.price, value.quote?.tick_size) : null;
  const status = quotePrice ? value.status === "partial" ? "partial" : "available"
    : value.status === "partial" && matches ? "partial" : "unavailable";
  return {
    status, pair: symbol ? `${symbol[1]}/USDT` : marketId,
    timeframe: timeframeCode(value.timeframe),
    source: value.source === "binance_usdt_perpetual" ? uiText("Binance USDT 永續") : uiText("行情來源未確認"),
    price: quotePrice,
    observedAt: quotePrice ? quoteTime : validTime(value.requested_at),
    timeBasis: quotePrice ? "quote" : "request",
    note: !quotePrice ? uiText("本次未取得現價；原分析價格不是即時報價。")
      : status === "partial" ? uiText("部分行情未取得。") : null,
    snapshot: value.version === "discussion_live_market_v2" && matches
      ? value.snapshot?.status === "available" || value.snapshot?.status === "partial" ? value.snapshot.status : "unavailable"
      : null,
  };
}
