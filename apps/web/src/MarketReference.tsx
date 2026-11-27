import { uiText } from "./i18n/index.ts";
import { timeframeCode } from "./timeframes";

type ReferenceFrame = {
  status: string;
  reason?: string;
  trend?: string | null;
  structure?: string;
  change_pct?: string;
  change_candles?: number;
  correlation?: string | null;
  beta?: string | null;
  linkage?: string;
};

export type MarketReferenceData = {
  status: string;
  timeframes?: string[];
  correlation_window_candles?: number;
  references?: Record<string, { status: string; timeframes?: Record<string, ReferenceFrame> }>;
};

const direction = (value?: string | null) => value === "bullish" ? uiText("偏多")
  : value === "bearish" ? uiText("偏空") : value === "mixed" ? uiText("混合") : uiText("方向未明");

const structure = (value?: string) => value === "rising" ? uiText("高低點抬高")
  : value === "falling" ? uiText("高低點降低") : value === "mixed" ? uiText("高低點混合")
    : uiText("確認轉折不足");

const linkage = (value?: string) => value === "strong" ? uiText("連動強")
  : value === "moderate" ? uiText("連動中等") : value === "weak" ? uiText("連動弱") : uiText("連動未知");

const symbol = (marketId: string) => marketId.split(":").at(-1)?.replace(/USDT$/, "") ?? marketId;

function Frame({ frame, value }: { frame: string; value?: ReferenceFrame }) {
  if (value?.status !== "available") {
    return <p><b>{timeframeCode(frame)}</b>：{uiText("資料未取得")}</p>;
  }
  return (
    <p>
      <b>{timeframeCode(frame)}</b>：{direction(value.trend)} · {structure(value.structure)} ·{" "}
      {uiText("近 {{p0}} 根 {{p1}}%", { p0: value.change_candles ?? "—", p1: value.change_pct ?? "—" })} ·{" "}
      {value.correlation == null
        ? uiText("重疊 K 線不足，無法計算連動")
        : <>{uiText("相關係數 {{p0}}，beta {{p1}}", { p0: value.correlation, p1: value.beta ?? "—" })} · {linkage(value.linkage)}</>}
    </p>
  );
}

export default function MarketReference({ data, usEquity = false }: { data?: MarketReferenceData | null; usEquity?: boolean }) {
  if (!data) return null;
  if (!data.references || data.status === "unavailable" && !Object.keys(data.references).length) {
    return <p className="note">{uiText("資料未取得")}</p>;
  }
  return (
    <>
      <p className="note">{usEquity
        ? uiText("依已收盤 K 線計算本交易對與 SPY、QQQ 的連動；連動越強，美股大盤方向越值得作為風險參考。相關係數只描述最近 {{p0}} 根 K 線，不代表未來仍會同步。", { p0: data.correlation_window_candles ?? "—" })
        : uiText("依已收盤 K 線計算本交易對與 BTC、ETH 的連動；連動越強，大盤方向越值得作為風險參考。相關係數只描述最近 {{p0}} 根 K 線，不代表未來仍會同步。", { p0: data.correlation_window_candles ?? "—" })}</p>
      {Object.entries(data.references).map(([marketId, reference]) => (
        <div key={marketId}>
          <strong>{symbol(marketId)}</strong>
          {reference.status === "self"
            ? <p className="note">{uiText("本交易對即為參考標的")}</p>
            : (data.timeframes ?? Object.keys(reference.timeframes ?? {})).map((frame) => (
              <Frame key={frame} frame={frame} value={reference.timeframes?.[frame]} />
            ))}
        </div>
      ))}
    </>
  );
}
