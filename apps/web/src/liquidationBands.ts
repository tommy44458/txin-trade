// Pure helpers for the estimated liquidation map, kept apart from the component for tests.

export type Cluster = { low: string; high: string; notional_usd: string; distance_pct: string; share_of_side_pct: string };

/** Binance liquidation bands estimated from open interest changes (estimated_liquidation_map_v1). */
export type LiquidationMapData = {
  status: "available" | "partial" | "unavailable";
  reason?: string;
  hours?: number;
  reference_price?: string;
  near_pct?: number;
  near_long_clusters?: Cluster[];
  near_short_clusters?: Cluster[];
  cumulative_within?: Record<string, { long_usd: string; short_usd: string }>;
  modelled_share_of_open_interest_pct?: string;
  /** [low, high, long liquidations USD, short liquidations USD] per band. */
  histogram?: [string, string, string, string][];
};

export const CHART_PCT = 5;
export const ROW_PCT = 0.5;

export type Row = { index: number; from: number; to: number; value: number };

/** Half-percent rows from five percent above price down to five percent below it. */
export function liquidationRows(data: LiquidationMapData): Row[] {
  const reference = Number(data.reference_price);
  const totals = new Map<number, number>();
  for (const [low, high, long, short] of data.histogram ?? []) {
    const distance = ((Number(low) + Number(high)) / 2 / reference - 1) * 100;
    if (Math.abs(distance) > CHART_PCT) continue;
    const index = Math.floor(distance / ROW_PCT);
    totals.set(index, (totals.get(index) ?? 0) + Number(index >= 0 ? short : long));
  }
  const rows: Row[] = [];
  for (let index = CHART_PCT / ROW_PCT - 1; index >= -CHART_PCT / ROW_PCT; index -= 1) {
    rows.push({
      index, value: totals.get(index) ?? 0,
      from: reference * (1 + (index * ROW_PCT) / 100), to: reference * (1 + ((index + 1) * ROW_PCT) / 100),
    });
  }
  return rows;
}
