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

const windowLabel = (window: Window) => window === "0_6h" ? uiText("0–6 小時")
  : window === "6_24h" ? uiText("6–24 小時") : uiText("1–3 天");

const gradeLabel = (grade: "A" | "B" | "C") => grade === "A" ? uiText("可評估左側試單")
  : grade === "B" ? uiText("候選觀察") : uiText("無優勢／不交易");

const statusLabel = (status: ReversalZone["status"]) => status === "triggered" ? uiText("已觸發")
  : status === "invalidated" ? uiText("已失效") : uiText("預測候選，尚未觸發");

const confidenceLabel = (value: NonNullable<ReversalZone["confidence"]>) => value === "high" ? uiText("依據：高")
  : value === "medium" ? uiText("依據：中") : uiText("依據：低");

/** Keep the exchange's tick precision; only add thousands separators. */
function price(value: string): string {
  const decimals = value.split(".")[1]?.length ?? 0;
  return Number(value).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

const signedPct = (value: string) => `${Number(value) > 0 ? "+" : Number(value) < 0 ? "−" : ""}${Math.abs(Number(value)).toFixed(2)}%`;

function ZoneRow({ zone }: { zone: ReversalZone }) {
  return (
    <li className={`reversal-zone ${zone.side} ${zone.status}`}>
      <div className="reversal-zone-head">
        <span className={`stance-pill ${zone.side}`}>{zone.side === "long" ? uiText("做多區") : uiText("做空區")}</span>
        <span className="tag">{zone.tier === "first" ? uiText("第一極端區") : uiText("第二極端區")}</span>
        <span className={`reversal-status ${zone.status}`}>{statusLabel(zone.status)}</span>
        {zone.confidence && <span className="reversal-confidence">{confidenceLabel(zone.confidence)}</span>}
      </div>
      <p className="reversal-zone-range">
        <strong>{price(zone.low)} – {price(zone.high)}</strong>
        <span>{zone.quote_in_zone ? uiText("現價在區內") : uiText("距現價 {{p0}}", { p0: signedPct(zone.distance_pct) })}</span>
      </p>
      <dl className="reversal-zone-plan">
        <div>
          <dt>{uiText("失效")}</dt>
          <dd>{zone.stop_loss ? price(zone.stop_loss) : uiText("未提供")}</dd>
        </div>
        <div>
          <dt>{uiText("目標")}</dt>
          <dd>{zone.targets.length ? zone.targets.map((target, index) => (
            <span key={target.price}>{index ? " · " : ""}{price(target.price)}
              {target.r_multiple && <small>{uiText("（{{p0}}R）", { p0: Number(target.r_multiple).toFixed(1) })}</small>}
            </span>
          )) : uiText("未提供")}</dd>
        </div>
      </dl>
      <p className="reversal-zone-text"><b>{uiText("依據")}</b>{zone.basis}</p>
      {zone.confirmation && <p className="reversal-zone-text"><b>{uiText("到價要看")}</b>{zone.confirmation}</p>}
    </li>
  );
}

/** Per window, short zones above the snapshot price and long zones below it, highest price first. */
function WindowLadder({ window, zones, quote }: { window: Window; zones: ReversalZone[]; quote: string }) {
  const byPrice = (a: ReversalZone, b: ReversalZone) => Number(b.high) - Number(a.high);
  const shorts = zones.filter((zone) => zone.side === "short").sort(byPrice);
  const longs = zones.filter((zone) => zone.side === "long").sort(byPrice);
  return (
    <section className="reversal-window" aria-label={windowLabel(window)}>
      <h3>{windowLabel(window)}</h3>
      {zones.length ? (
        <ul className="reversal-ladder">
          {shorts.map((zone) => <ZoneRow key={`${zone.side}-${zone.tier}`} zone={zone} />)}
          <li className="reversal-quote" aria-label={uiText("快照價格")}>
            <span>{uiText("快照價格 {{p0}}", { p0: price(quote) })}</span>
          </li>
          {longs.map((zone) => <ZoneRow key={`${zone.side}-${zone.tier}`} zone={zone} />)}
        </ul>
      ) : <p className="reversal-empty">{uiText("暫無有依據的極端區")}</p>}
    </section>
  );
}

export default function ReversalMap({ data }: { data?: ReversalMapData }) {
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
      {!data ? <p className="reversal-empty reversal-missing">{uiText("AI 本次未回傳極端反轉地圖。")}</p> : (
        <>
          {(data.grade_reason || data.plan_adjusted) && (
            <div className="reversal-summary">
              {data.grade_reason && <p>{data.grade_reason}</p>}
              {data.plan_adjusted === "grade_c_plan" && (
                <small>{uiText("評級為 C，AI 給出的開單方案與評級不一致，因此改為觀望。")}</small>
              )}
              {data.plan_adjusted === "open_now_needs_grade_a" && (
                <small>{uiText("立即進場需要 A 級，這份方案只到 B 級，因此改為觀望。")}</small>
              )}
            </div>
          )}
          <div className="reversal-windows">
            {WINDOWS.map((window) => (
              <WindowLadder key={window} window={window} quote={data.reference_price}
                zones={data.zones.filter((zone) => zone.window === window)} />
            ))}
          </div>
          {(data.paths.up_first || data.paths.down_first) && (
            <div className="reversal-paths">
              {data.paths.up_first && (
                <div><h3>{uiText("先上衝再反轉")}</h3><p>{data.paths.up_first}</p></div>
              )}
              {data.paths.down_first && (
                <div><h3>{uiText("先下殺再反轉")}</h3><p>{data.paths.down_first}</p></div>
              )}
            </div>
          )}
          {(data.cancel_condition || data.data_limits) && (
            <dl className="reversal-footnotes">
              {data.cancel_condition && <div><dt>{uiText("取消伏擊")}</dt><dd>{data.cancel_condition}</dd></div>}
              {data.data_limits && <div><dt>{uiText("資料限制")}</dt><dd>{data.data_limits}</dd></div>}
            </dl>
          )}
          <p className="reversal-disclaimer">
            {uiText("候選區是預先劃定值得伏擊的價位，不是必然頂底；「依據」是候選區依據的充足程度，不是到價或反轉的機率。R 以區間中點估算，未計手續費與滑價。")}
            {data.dropped_zones > 0 && uiText("另有 {{p0}} 個區因方向、價格或距離無法核對而未顯示。", { p0: data.dropped_zones })}
          </p>
        </>
      )}
    </section>
  );
}
