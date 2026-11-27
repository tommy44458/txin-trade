import { uiText, uiLocale } from "./i18n/index.ts";
import { orderedTimeframes, timeframeCode } from "./timeframes";
import LiquidationMap, { type LiquidationMapData } from "./LiquidationMap";

type Unavailable = { status: "unavailable"; reason?: string };
type Ratio = { status: "available"; observed_at: string; long_share_pct: string; long_share_24h_ago_pct: string | null };
type Taker = {
  status: "available";
  complete_through: string;
  buy_share_24h_pct: string | null;
  buy_share_prior_24h_pct: string | null;
};
/** Binance long/short shares and taker volume for the primary period (derivatives_context v3). */
type Positioning = {
  period: string;
  status: string;
  global_accounts: Ratio | Unavailable;
  top_trader_accounts: Ratio | Unavailable;
  top_trader_positions: Ratio | Unavailable;
  taker_volume: Taker | Unavailable;
};
export type DerivativesData = {
  status: string;
  source_url: string;
  timeframes?: Record<
    string,
    {
      status: string;
      observed_at?: string;
      open_interest_base_units?: string;
      change_24h_pct?: string | null;
    }
  >;
  positioning?: Positioning;
};

type FlowWindow = {
  status: string;
  cvd_quote?: string;
  buy_share_pct?: string | null;
  price_change_pct?: string;
  price_vs_flow?: "same_direction" | "opposite" | "flat";
};
/** The cumulative volume delta of recent closed candles, computed from the snapshot's taker volume. */
export type OrderFlowData = {
  primary: { timeframe: string; windows: Record<string, FlowWindow> };
  context: { timeframe: string; windows: Record<string, FlowWindow> } | null;
};

type LiquidationTotals = { long_usd: number; short_usd: number; largest_usd: number };
type LiquidationWindow = {
  symbol: Record<"bybit" | "okx", LiquidationTotals>;
  market: Record<"bybit" | "okx", LiquidationTotals>;
  coverage_pct: Record<"bybit" | "okx", number>;
};
/** Positions closed by force on Bybit and OKX, collected by the cloud (liquidation_context v1). */
export type LiquidationData = {
  status: "available" | "partial" | "unavailable";
  symbol: string;
  windows?: Record<"1h" | "4h" | "24h", LiquidationWindow>;
};

const LIQUIDATION_WINDOWS = ["1h", "4h", "24h"] as const;
const both = (side: Record<"bybit" | "okx", LiquidationTotals>, key: "long_usd" | "short_usd") =>
  side.bybit[key] + side.okx[key];

function Liquidations({ data }: { data: LiquidationData }) {
  const windows = data.windows;
  if (data.status === "unavailable" || !windows) return <p>{uiText("資料未取得")}</p>;
  const day = windows["24h"];
  return (
    <>
      {LIQUIDATION_WINDOWS.map((name) => {
        const window = windows[name];
        const partial = window.coverage_pct.bybit < 90 || window.coverage_pct.okx < 90;
        return (
          <p key={name}>
            <b>{uiText("{{p0}} 小時", { p0: name.replace("h", "") })}</b>：{uiText("多單被清算 {{p0}}、空單被清算 {{p1}}", {
              p0: usd(String(both(window.symbol, "long_usd"))).replace("+", ""),
              p1: usd(String(both(window.symbol, "short_usd"))).replace("+", ""),
            })}
            {partial && <small> · {uiText("連線涵蓋 Bybit {{p0}}%、OKX {{p1}}%", {
              p0: window.coverage_pct.bybit, p1: window.coverage_pct.okx,
            })}</small>}
          </p>
        );
      })}
      <p>
        <b>{uiText("全市場 24 小時")}</b>：{uiText("多單被清算 {{p0}}、空單被清算 {{p1}}", {
          p0: usd(String(both(day.market, "long_usd"))).replace("+", ""),
          p1: usd(String(both(day.market, "short_usd"))).replace("+", ""),
        })}
      </p>
      <p className="note">{uiText("多單被清算是被迫賣出，空單被清算是被迫買進。OKX 只公開部分清算，Binance 的清算不在其中；連線中斷的時段沒有資料，不代表沒有清算。")}</p>
    </>
  );
}

const usd = (value: string) => new Intl.NumberFormat(uiLocale(), {
  style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1, signDisplay: "exceptZero",
}).format(Number(value));

function RatioLine({ label, value }: { label: string; value: Ratio | Unavailable }) {
  return (
    <p>
      <b>{label}</b>：{value.status === "available"
        ? <>{uiText("多方占 {{p0}}%", { p0: value.long_share_pct })}{value.long_share_24h_ago_pct != null &&
          <>{uiText("（一天前 {{p0}}%）", { p0: value.long_share_24h_ago_pct })}</>}</>
        : uiText("資料未取得")}
    </p>
  );
}

const relationLabel = (value?: FlowWindow["price_vs_flow"]) => value === "same_direction" ? uiText("與價格同向")
  : value === "opposite" ? uiText("與價格反向") : uiText("無明顯方向");

function FlowLine({ timeframe, windows }: { timeframe: string; windows: Record<string, FlowWindow> }) {
  const shown = Object.entries(windows).filter(([, w]) => w.status === "available");
  return (
    <p>
      <b>{timeframeCode(timeframe)}</b>：{shown.length ? shown.map(([name, w], index) => (
        <span key={name}>{index ? " · " : ""}{uiText("最近 {{p0}} 根 {{p1}}（{{p2}}，價格 {{p3}}%）", {
          p0: name.replace("last_", ""), p1: usd(w.cvd_quote!), p2: relationLabel(w.price_vs_flow),
          p3: w.price_change_pct,
        })}</span>
      )) : uiText("資料未取得")}
    </p>
  );
}

export default function DerivativesContext({
  data,
  orderFlow,
  liquidations,
  liquidationMap,
}: {
  data?: DerivativesData | null;
  orderFlow?: OrderFlowData | null;
  liquidations?: LiquidationData | null;
  liquidationMap?: LiquidationMapData | null;
}) {
  if (!data) return null;
  const positioning = data.positioning;
  const taker = positioning?.taker_volume;
  return (
    <details className="secondary-evidence">
      <summary>{liquidations ? uiText("合約背景：未平倉量、多空比、主動買賣與清算")
        : positioning || orderFlow ? uiText("合約背景：未平倉量、多空比與主動買賣") : uiText("合約背景：未平倉量")}</summary>
      <p className="note">{uiText("未平倉量代表仍持有的合約總量，每筆都有多空雙方；不能單靠增加或減少判斷方向，也不是強平熱圖。")}</p>
      {orderedTimeframes("", Object.keys(data.timeframes ?? {})).map((frame) => {
        const value = data.timeframes?.[frame];
        return (
          <p key={frame}>
            <b>{timeframeCode(frame)}</b>：
            {value?.status === "available" ? (
              <>
                {Number(value.open_interest_base_units).toLocaleString(
                  uiLocale(),
                  { maximumFractionDigits: 2 },
                )}{" "}{uiText("基礎資產單位；")}{" "}{value.change_24h_pct == null
                  ? uiText("24 小時樣本不足")
                  : uiText("24 小時變化 {{p0}}%", { p0: value.change_24h_pct })}
                <small>
                  {" "}
                  · {new Date(value.observed_at!).toLocaleString(uiLocale())}
                </small>
              </>
            ) : (
              uiText("資料未取得")
            )}
          </p>
        );
      })}
      {positioning && (
        <>
          <h4 className="evidence-subtitle">{uiText("Binance 多空比（{{p0}}）", { p0: timeframeCode(positioning.period) })}</h4>
          <RatioLine label={uiText("全體帳戶")} value={positioning.global_accounts} />
          <RatioLine label={uiText("大戶帳戶")} value={positioning.top_trader_accounts} />
          <RatioLine label={uiText("大戶持倉量")} value={positioning.top_trader_positions} />
          <p>
            <b>{uiText("主動買入占比")}</b>：{taker?.status === "available" && taker.buy_share_24h_pct != null
              ? <>{uiText("最近一天 {{p0}}%", { p0: taker.buy_share_24h_pct })}{taker.buy_share_prior_24h_pct != null &&
                <>{uiText("（前一天 {{p0}}%）", { p0: taker.buy_share_prior_24h_pct })}</>}</>
              : uiText("資料未取得")}
          </p>
          <p className="note">{uiText("多空比是 Binance 帳戶或大戶的持倉占比，不代表整體市場；過度擁擠的一方可能被軋，但本身不是買賣訊號。")}</p>
        </>
      )}
      {orderFlow && (
        <>
          <h4 className="evidence-subtitle">{uiText("累積成交量差（CVD）")}</h4>
          <FlowLine timeframe={orderFlow.primary.timeframe} windows={orderFlow.primary.windows} />
          {orderFlow.context && <FlowLine timeframe={orderFlow.context.timeframe} windows={orderFlow.context.windows} />}
          <p className="note">{uiText("主動買入減主動賣出，以 USDT 計，取已收盤 K 線。與價格反向可能是掛單吸收，也可能是避險或套利。")}</p>
        </>
      )}
      {liquidations && (
        <>
          <h4 className="evidence-subtitle">{uiText("{{p0}} 清算（Bybit、OKX）", { p0: liquidations.symbol })}</h4>
          <Liquidations data={liquidations} />
        </>
      )}
      {liquidationMap && (
        <>
          <h4 className="evidence-subtitle">{uiText("估算清算價位（Binance 模型）")}</h4>
          <LiquidationMap data={liquidationMap} />
        </>
      )}
      <a href={data.source_url} target="_blank" rel="noreferrer">{uiText("Binance 公開統計資料說明")}</a>
    </details>
  );
}
