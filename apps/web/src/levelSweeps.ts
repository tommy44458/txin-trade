import { uiText } from "./i18n/index.ts";

/** What a v3 zone records about price trading beyond its outer edge (context v3). */
export type LevelSweepFields = {
  sweep_count?: number;
  last_sweep_at?: string | null;
  liquidity_beyond?: {
    side: "below" | "above";
    basis: "equal_pivots" | "single_pivot";
    status: "untaken" | "swept" | "closed_beyond";
    taken_at: string | null;
  };
  forming_candle_swept?: boolean | null;
};

/** Short notes for a zone row; empty for zones from older reports or with nothing to say. */
export function levelSweepNotes(level: LevelSweepFields, when: (value: string) => string): string[] {
  const notes: string[] = [];
  if (level.forming_candle_swept) notes.push(uiText("這根未收盤 K 線掃過外緣後已收回"));
  const pool = level.liquidity_beyond;
  if (pool?.status === "untaken" && pool.basis === "equal_pivots") {
    notes.push(pool.side === "below" ? uiText("下方等低的停損尚未被掃") : uiText("上方等高的停損尚未被掃"));
  } else if (pool?.status === "swept" && pool.taken_at) {
    notes.push(uiText("{{p0}} 掃過外緣後收回", { p0: when(pool.taken_at) }));
  }
  if (level.sweep_count) notes.push(uiText("累計被掃 {{p0}} 次", { p0: level.sweep_count }));
  return notes;
}
