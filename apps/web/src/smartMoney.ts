// Fund flows data from the cloud: exchange reserves read daily from DefiLlama's CEX
// transparency data, and the small pure helpers the page uses.

export type FlowWindow = "7d" | "30d" | "90d";
export const FLOW_WINDOWS: FlowWindow[] = ["7d", "30d", "90d"];

/** A change in exchange holdings: positive means coins moved onto exchanges. */
export type ReserveChange = { net: number; net_usd: number };

export type ReserveAssetSummary = {
  amount: number;
  usd: number;
  /** How many followed exchanges hold the coin at the latest close. */
  exchanges: number;
  "1d": ReserveChange;
  "7d": ReserveChange;
  "30d": ReserveChange;
  movers_7d: (ReserveChange & { exchange: string })[];
  excluded_30d: (ReserveChange & { exchange: string; day: number })[];
};

type ReserveFreshness = {
  generated_at: number;
  source: string;
  /** 00:00 UTC of the latest close every followed exchange has reached; null before the first read. */
  through: number | null;
  lagging: { exchange: string; through: number }[];
  /** The cloud's data is old, or the computer could not reach the cloud and shows its last answer. */
  stale?: boolean;
};

export type ReserveSummary = ReserveFreshness & { assets: Record<string, ReserveAssetSummary> };

export type ReservePoint = ReserveChange & { t: number; amount: number; usd: number };

export type ReserveDetail = ReserveFreshness & {
  asset: string;
  window: FlowWindow;
  amount: number;
  usd: number;
  totals: ReserveChange;
  series: ReservePoint[];
  exchanges: (ReserveChange & { exchange: string; amount: number; usd: number; share: number })[];
  excluded: (ReserveChange & { exchange: string; day: number })[];
};

/** Total stablecoin supply, from the cloud's overview. */
export type StablecoinSupply = {
  total_usd: number | null;
  change_1d: number | null;
  change_7d: number | null;
};

/** The coin behind a market: binance:perp:1000PEPEUSDT trades PEPE. */
export function assetForMarket(marketId: string): string {
  const symbol = marketId.split(":").pop() ?? "";
  return symbol.replace(/(USDT|USDC)$/, "").replace(/^1000+(?=[A-Z])/, "");
}

const LEADING = ["BTC", "ETH", "USDT", "USDC"];

/** BTC, ETH and the stablecoins first, then the other followed coins alphabetically. */
export function reserveOrder(assets: string[]): string[] {
  const rank = (asset: string) => (LEADING.includes(asset) ? LEADING.indexOf(asset) : LEADING.length);
  return [...assets].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

/** Two coins' changes as one, for USDT and USDC together; missing coins count as nothing. */
export function combine(changes: (ReserveChange | undefined)[]): ReserveChange {
  return changes.reduce<ReserveChange>(
    (sum, change) => ({ net: sum.net + (change?.net ?? 0), net_usd: sum.net_usd + (change?.net_usd ?? 0) }),
    { net: 0, net_usd: 0 },
  );
}

/** The largest daily change in a series, so bars are drawn against one scale; at least 1. */
export function netScale(points: ReserveChange[]): number {
  return Math.max(1, ...points.map((p) => Math.abs(p.net_usd)));
}

export function formatUsd(value: number, locale: string, signed = false): string {
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    notation: Math.abs(value) >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: Math.abs(value) >= 10_000 ? 1 : 0,
    signDisplay: signed ? "exceptZero" : "auto",
  }).format(value);
}

export function formatAmount(value: number, locale: string, signed = false): string {
  const size = Math.abs(value);
  return new Intl.NumberFormat(locale, {
    notation: size >= 100_000 ? "compact" : "standard",
    maximumFractionDigits: size >= 100_000 ? 1 : size >= 100 ? 0 : size >= 1 ? 2 : 4,
    signDisplay: signed ? "exceptZero" : "auto",
  }).format(value);
}

/** A UTC close as a short date: reserves are counted at 00:00 UTC, whatever the viewer's zone. */
export function closeDate(ms: number, locale: string): string {
  return new Date(ms).toLocaleDateString(locale, { month: "numeric", day: "numeric", timeZone: "UTC" });
}

/** The frozen fund flows a follow-up conversation is about. */
export type FlowSnapshot = { id: string; asset: string; window: string; as_of: string; created_at: string };
