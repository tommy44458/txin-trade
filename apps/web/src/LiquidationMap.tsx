import { Fragment } from "react";
import { uiLocale, uiText } from "./i18n/index.ts";
import "./LiquidationMap.css";
import { liquidationRows, type Cluster, type LiquidationMapData } from "./liquidationBands";

export type { LiquidationMapData } from "./liquidationBands";

const DISTANCES = ["1pct", "2pct", "3pct", "5pct"];

const usd = (value: number) => new Intl.NumberFormat(uiLocale(), {
  style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1,
}).format(value);
const price = (value: number) => new Intl.NumberFormat(uiLocale(), { maximumSignificantDigits: 6 }).format(value);

function Clusters({ label, clusters }: { label: string; clusters?: Cluster[] }) {
  return (
    <p>
      <b>{label}</b>：{clusters?.length ? clusters.map((cluster, index) => (
        <span key={cluster.low}>{index ? " · " : ""}{uiText("${{p0}}–${{p1}}（{{p2}}%，占該側 {{p3}}%）", {
          p0: price(Number(cluster.low)), p1: price(Number(cluster.high)),
          p2: cluster.distance_pct, p3: cluster.share_of_side_pct,
        })}</span>
      )) : uiText("附近沒有估計的清算價帶")}
    </p>
  );
}

export default function LiquidationMap({ data }: { data: LiquidationMapData }) {
  if (data.status === "unavailable" || !data.histogram || !data.reference_price) {
    return <p>{uiText("資料未取得")}</p>;
  }
  const rows = liquidationRows(data);
  const largest = Math.max(1, ...rows.map((row) => row.value));
  return (
    <>
      <div className="liq-map" role="img" aria-label={uiText("現價上下 5% 的估計清算金額，上方是空單、下方是多單")}>
        {rows.map((row) => (
          <Fragment key={row.index}>
            {row.index === -1 && (
              <div className="liq-map-now"><span>{uiText("現價")}</span><b>${price(Number(data.reference_price))}</b></div>
            )}
            <div className="liq-map-row" data-side={row.index >= 0 ? "short" : "long"}
              title={`$${price(row.from)}–$${price(row.to)}`}>
              <span className="liq-map-price">${price(row.index >= 0 ? row.to : row.from)}</span>
              <span className="liq-map-bar"><i style={{ width: `${(row.value / largest) * 100}%` }} /></span>
              <span className="liq-map-value">{row.value > 0 ? usd(row.value) : ""}</span>
            </div>
          </Fragment>
        ))}
      </div>
      <Clusters label={uiText("上方 {{p0}}% 內最大的空單清算", { p0: data.near_pct ?? 3 })} clusters={data.near_short_clusters} />
      <Clusters label={uiText("下方 {{p0}}% 內最大的多單清算", { p0: data.near_pct ?? 3 })} clusters={data.near_long_clusters} />
      {data.cumulative_within && (
        <>
          <p>
            <b>{uiText("上漲時被迫買進")}</b>：{DISTANCES.map((key, index) => (
              <span key={key}>{index ? " · " : ""}{uiText("{{p0}}% 約 {{p1}}", {
                p0: key.replace("pct", ""), p1: usd(Number(data.cumulative_within![key]?.short_usd ?? 0)),
              })}</span>
            ))}
          </p>
          <p>
            <b>{uiText("下跌時被迫賣出")}</b>：{DISTANCES.map((key, index) => (
              <span key={key}>{index ? " · " : ""}{uiText("{{p0}}% 約 {{p1}}", {
                p0: key.replace("pct", ""), p1: usd(Number(data.cumulative_within![key]?.long_usd ?? 0)),
              })}</span>
            ))}
          </p>
        </>
      )}
      <p className="note">{uiText("依 Binance 最近 {{p0}} 小時的未平倉量變化與假設槓桿（10、25、50、100 倍）推估，不是實際倉位或停損，也不含其他交易所；約涵蓋目前未平倉量的 {{p1}}%。價格常會掃過這些位置再反轉，也可能根本不到。", {
        p0: data.hours ?? 0, p1: data.modelled_share_of_open_interest_pct ?? "—",
      })}</p>
    </>
  );
}
