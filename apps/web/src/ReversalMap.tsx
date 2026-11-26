import type { EntryDecision } from "./AnalysisEvidence";
import { uiText } from "./i18n/index.ts";
import "./ReversalMap.css";

type Side = "long" | "short";
type Window = "0_6h" | "6_24h" | "1_3d";
export type ReversalZone = {
  side: Side;
  window: Window;
  tier: "first" | "second";
  low: string;
  high: string;
  basis: string;
  confidence: "high" | "medium" | "low" | null;
  confirmation: string | null;
  status: "candidate" | "triggered" | "invalidated";
  stop_loss: string | null;
  // R is measured from reference_entry, the middle of the zone.
  targets: { price: string; r_multiple: string | null }[];
  reference_entry: string;
  distance_pct: string;
  quote_in_zone: boolean;
};
/** The extreme zones the strategy projects ahead of time; checked against the snapshot by the app. */
export type ReversalMapData = {
  version: string;
  grade: "A" | "B" | "C" | null;
  grade_reason: string | null;
  zones: ReversalZone[];
  dropped_zones: number;
  paths: { up_first: string | null; down_first: string | null };
  cancel_condition: string | null;
  data_limits: string | null;
  reference_price: string;
  plan_adjusted?: "grade_c_plan" | "open_now_needs_grade_a";
} | null;

const WINDOWS: Window[] = ["0_6h", "6_24h", "1_3d"];
// The strategy prefers at least 1:3 reward to risk; targets meeting it are marked.
const PREFERRED_R = 3;

const windowLabel = (window: Window) => window === "0_6h" ? uiText("0–6 小時")
  : window === "6_24h" ? uiText("6–24 小時") : uiText("1–3 天");

const gradeLabel = (grade: "A" | "B" | "C") => grade === "A" ? uiText("可評估左側試單")
  : grade === "B" ? uiText("候選觀察") : uiText("無優勢／不交易");

const statusLabel = (status: ReversalZone["status"]) => status === "triggered" ? uiText("已觸發")
  : status === "invalidated" ? uiText("已失效") : uiText("預測候選，尚未觸發");

const confidenceLabel = (value: NonNullable<ReversalZone["confidence"]>) => value === "high" ? uiText("依據：高")
  : value === "medium" ? uiText("依據：中") : uiText("依據：低");

const decimalsOf = (value: string) => value.split(".")[1]?.length ?? 0;

/** Keep the exchange's tick precision; only add thousands separators. */
function price(value: string): string {
  const decimals = decimalsOf(value);
  return Number(value).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** A short price for the chart, without trailing zeros. */
const shortPrice = (value: string) => Number(value).toLocaleString("en-US", { maximumFractionDigits: decimalsOf(value) });

const signedPct = (value: string) => `${Number(value) > 0 ? "+" : Number(value) < 0 ? "−" : ""}${Math.abs(Number(value)).toFixed(2)}%`;

const inPlan = (zone: ReversalZone, entry?: EntryDecision | null) =>
  !!entry && entry.action !== "stand_aside" && entry.side === zone.side && entry.entry_price !== null &&
  Number(entry.entry_price) >= Number(zone.low) && Number(entry.entry_price) <= Number(zone.high);

/** Round axis steps of 1, 2, 2.5 or 5 at the price's own scale. */
function axisTicks(low: number, high: number): { value: number; label: string }[] {
  const raw = (high - low) / 4;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((n) => n * power).find((candidate) => candidate >= raw) ?? 10 * power;
  const digits = Math.max(0, -Math.floor(Math.log10(step)) + (step / power === 2.5 ? 1 : 0));
  const ticks = [];
  for (let value = Math.ceil(low / step) * step; value <= high; value += step)
    ticks.push({ value, label: value.toLocaleString("en-US", { maximumFractionDigits: digits }) });
  return ticks;
}

/** All windows share one price axis, so a zone's height and gap to the snapshot price compare across time. */
function PriceMap({ zones, quote, entry }: { zones: ReversalZone[]; quote: string; entry?: EntryDecision | null }) {
  const prices = [Number(quote), ...zones.flatMap((zone) => [zone.low, zone.high, zone.stop_loss ?? zone.low].map(Number))];
  const spread = Math.max(...prices) - Math.min(...prices) || Number(quote) * 0.02;
  const top = Math.max(...prices) + spread * 0.08;
  const bottom = Math.min(...prices) - spread * 0.08;
  const y = (value: string | number) => ((top - Number(value)) / (top - bottom)) * 100;
  const ticks = axisTicks(bottom, top);
  const axisWidth = Math.max(...ticks.map((tick) => tick.label.length), 4);
  return (
    <figure className="rmap-chart" style={{ gridTemplateColumns: `calc(${axisWidth}ch + 8px) minmax(0, 1fr)` }}>
      <div />
      <div className="rmap-chart-heads">
        {WINDOWS.map((window) => (
          <span key={window}>{windowLabel(window)}</span>
        ))}
      </div>
      <div className="rmap-axis" aria-hidden="true">
        {ticks.map((tick) => <span key={tick.value} style={{ top: `${y(tick.value)}%` }}>{tick.label}</span>)}
      </div>
      <div className="rmap-plot" role="img"
        aria-label={uiText("各時間窗的極端區與快照價格 {{p0}} 的相對位置，價位明細見下方清單。", { p0: price(quote) })}>
        {ticks.map((tick) => <i key={tick.value} className="rmap-grid" style={{ top: `${y(tick.value)}%` }} />)}
        {WINDOWS.map((window) => (
          <div key={window} className="rmap-col">
            {zones.filter((zone) => zone.window === window).map((zone) => {
              // The stop lies beyond the zone's far edge: above a short zone, below a long one.
              const edge = zone.side === "short" ? zone.high : zone.low;
              return (
                <div key={`${zone.side}-${zone.tier}`}>
                  {zone.stop_loss && (
                    <i className={`rmap-wick ${zone.side}`} style={zone.side === "short"
                      ? { top: `${y(zone.stop_loss)}%`, height: `${y(edge) - y(zone.stop_loss)}%` }
                      : { top: `${y(edge)}%`, height: `${y(zone.stop_loss) - y(edge)}%` }} />
                  )}
                  <span
                    className={`rmap-band ${zone.side} ${zone.tier} ${zone.status}${inPlan(zone, entry) ? " plan" : ""}`}
                    style={{ top: `${(y(zone.high) + y(zone.low)) / 2}%`, height: `${y(zone.low) - y(zone.high)}%` }}
                    title={`${zone.side === "long" ? uiText("做多區") : uiText("做空區")} ${price(zone.low)} – ${price(zone.high)}`}
                  >
                    <span className="rmap-band-full">{shortPrice(zone.low)}–{shortPrice(zone.high)}</span>
                    {/* A narrow column shows only the edge that price reaches first. */}
                    <span className="rmap-band-edge">{shortPrice(zone.side === "short" ? zone.low : zone.high)}</span>
                  </span>
                </div>
              );
            })}
          </div>
        ))}
        <div className="rmap-quote-line" style={{ top: `${y(quote)}%` }}>
          <span>{uiText("快照 {{p0}}", { p0: price(quote) })}</span>
        </div>
      </div>
      <figcaption className="rmap-legend">
        <span><i className="rmap-key short" />{uiText("做空區")}</span>
        <span><i className="rmap-key long" />{uiText("做多區")}</span>
        <span><i className="rmap-key second" />{uiText("第二極端區")}</span>
        <span><i className="rmap-key stop" />{uiText("失效價")}</span>
        {entry && entry.action !== "stand_aside" && <span><i className="rmap-key plan" />{uiText("本次方案")}</span>}
      </figcaption>
    </figure>
  );
}

/** One zone shown once, with every window that projected the same prices and plan. */
type ZoneGroup = ReversalZone & { windows: Window[]; notes: { window: Window; basis: string; confirmation: string | null }[] };

function groupZones(zones: ReversalZone[]): ZoneGroup[] {
  const groups = new Map<string, ZoneGroup>();
  for (const zone of zones) {
    const key = [zone.side, zone.tier, zone.low, zone.high, zone.stop_loss, zone.status, zone.confidence,
      zone.targets.map((target) => target.price).join("/")].join("|");
    const note = { window: zone.window, basis: zone.basis, confirmation: zone.confirmation };
    const group = groups.get(key);
    if (group) {
      group.windows.push(zone.window);
      group.notes.push(note);
    } else groups.set(key, { ...zone, windows: [zone.window], notes: [note] });
  }
  return [...groups.values()];
}

/** Repeated text is shown once; differing text is labelled with its window. */
function ZoneNotes({ label, notes }: { label: string; notes: { window: Window; text: string | null }[] }) {
  const distinct = notes.filter((note, index) => note.text &&
    notes.findIndex((other) => other.text === note.text) === index);
  return distinct.map((note) => (
    <p key={note.window} className="rmap-row-note">
      <b>{label}</b>
      {distinct.length > 1 && <span className="rmap-row-window">{windowLabel(note.window)}</span>}
      {note.text}
    </p>
  ));
}

function ZoneRow({ zone, entry }: { zone: ZoneGroup; entry?: EntryDecision | null }) {
  const plan = inPlan(zone, entry);
  return (
    <li className={`rmap-row ${zone.side} ${zone.tier} ${zone.status}${plan ? " plan" : ""}`}>
      <div className="rmap-row-top">
        <div className="rmap-row-tags">
          <span className={`stance-pill ${zone.side}`}>{zone.side === "long" ? uiText("做多區") : uiText("做空區")}</span>
          <span className="rmap-row-tier">{zone.tier === "first" ? uiText("第一極端區") : uiText("第二極端區")}</span>
          {zone.windows.map((window) => <span key={window} className="tag">{windowLabel(window)}</span>)}
          {plan && <span className="rmap-plan-tag">{uiText("本次方案")}</span>}
        </div>
        <span className={`rmap-row-status ${zone.status}`}>
          {statusLabel(zone.status)}{zone.confidence && ` · ${confidenceLabel(zone.confidence)}`}
        </span>
      </div>
      <div className="rmap-row-prices">
        <p className="rmap-row-range">
          <strong>{price(zone.low)} – {price(zone.high)}</strong>
          <span>{zone.quote_in_zone ? uiText("現價在區內") : uiText("距現價 {{p0}}", { p0: signedPct(zone.distance_pct) })}</span>
        </p>
        <dl className="rmap-row-plan">
          <div>
            <dt>{uiText("失效")}</dt>
            <dd>{zone.stop_loss ? price(zone.stop_loss) : uiText("未提供")}</dd>
          </div>
          <div>
            <dt>{uiText("目標")}</dt>
            <dd>{zone.targets.length ? zone.targets.map((target) => (
              <span key={target.price} className="rmap-target">{price(target.price)}
                {target.r_multiple && (
                  <small className={Number(target.r_multiple) >= PREFERRED_R ? "rmap-r preferred" : "rmap-r"}>
                    {Number(target.r_multiple).toFixed(1)}R
                  </small>
                )}
              </span>
            )) : uiText("未提供")}</dd>
          </div>
        </dl>
      </div>
      <ZoneNotes label={uiText("依據")} notes={zone.notes.map((note) => ({ window: note.window, text: note.basis }))} />
      <ZoneNotes label={uiText("到價要看")}
        notes={zone.notes.map((note) => ({ window: note.window, text: note.confirmation }))} />
    </li>
  );
}

/** Short zones above the snapshot price and long zones below it, highest price first, like the chart. */
function ZoneList({ zones, quote, entry }: { zones: ReversalZone[]; quote: string; entry?: EntryDecision | null }) {
  const groups = groupZones(zones);
  const byPrice = (a: ZoneGroup, b: ZoneGroup) => Number(b.high) - Number(a.high);
  const shorts = groups.filter((zone) => zone.side === "short").sort(byPrice);
  const longs = groups.filter((zone) => zone.side === "long").sort(byPrice);
  const key = (zone: ZoneGroup) => `${zone.side}-${zone.tier}-${zone.windows.join("-")}`;
  return (
    <ol className="rmap-list" aria-label={uiText("極端區明細")}>
      {shorts.map((zone) => <ZoneRow key={key(zone)} zone={zone} entry={entry} />)}
      <li className="rmap-quote" aria-label={uiText("快照價格")}>
        <span>{uiText("快照價格 {{p0}}", { p0: price(quote) })}</span>
      </li>
      {longs.map((zone) => <ZoneRow key={key(zone)} zone={zone} entry={entry} />)}
    </ol>
  );
}

export default function ReversalMap({ data, entry }: { data?: ReversalMapData; entry?: EntryDecision | null }) {
  return (
    <section className="panel reversal-map" aria-label={uiText("極端反轉地圖")}>
      <header className="ai-report-header">
        <div>
          <span>{uiText("極端反轉地圖")}</span>
          {data?.grade && (
            <span className={`reversal-grade grade-${data.grade}`}>{data.grade} · {gradeLabel(data.grade)}</span>
          )}
        </div>
        <small className="reversal-note">{uiText("策略：極端左側反轉")}</small>
      </header>
      {!data ? <p className="rmap-empty">{uiText("AI 本次未回傳極端反轉地圖。")}</p> : (
        <>
          {(data.grade_reason || data.plan_adjusted) && (
            <div className="rmap-summary">
              {data.grade_reason && <p>{data.grade_reason}</p>}
              {data.plan_adjusted === "grade_c_plan" && (
                <small>{uiText("評級為 C，AI 給出的開單方案與評級不一致，因此改為觀望。")}</small>
              )}
              {data.plan_adjusted === "open_now_needs_grade_a" && (
                <small>{uiText("立即進場需要 A 級，這份方案只到 B 級，因此改為觀望。")}</small>
              )}
            </div>
          )}
          {data.zones.length ? (
            <>
              <PriceMap zones={data.zones} quote={data.reference_price} entry={entry} />
              <ZoneList zones={data.zones} quote={data.reference_price} entry={entry} />
            </>
          ) : <p className="rmap-empty">{uiText("暫無有依據的極端區")}</p>}
          {(data.paths.up_first || data.paths.down_first) && (
            <div className="rmap-paths">
              {data.paths.up_first && (
                <div className="rmap-path short"><h3>{uiText("先上衝再反轉")}</h3><p>{data.paths.up_first}</p></div>
              )}
              {data.paths.down_first && (
                <div className="rmap-path long"><h3>{uiText("先下殺再反轉")}</h3><p>{data.paths.down_first}</p></div>
              )}
            </div>
          )}
          {(data.cancel_condition || data.data_limits) && (
            <dl className="rmap-footnotes">
              {data.cancel_condition && <div><dt>{uiText("取消伏擊")}</dt><dd>{data.cancel_condition}</dd></div>}
              {data.data_limits && <div><dt>{uiText("資料限制")}</dt><dd>{data.data_limits}</dd></div>}
            </dl>
          )}
          <p className="rmap-disclaimer">
            {uiText("候選區是預先劃定值得伏擊的價位，不是必然頂底；「依據」是候選區依據的充足程度，不是到價或反轉的機率。R 以區間中點估算，未計手續費與滑價。")}
            {data.dropped_zones > 0 && uiText("另有 {{p0}} 個區因方向、價格或距離無法核對而未顯示。", { p0: data.dropped_zones })}
          </p>
        </>
      )}
    </section>
  );
}
