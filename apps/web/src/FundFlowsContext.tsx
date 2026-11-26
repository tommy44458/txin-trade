import { uiText, useUiLocale } from "./i18n/index.ts";
import { formatAmount, formatUsd } from "./smartMoney";

// Reports made before exchange reserves (fund_flows_context v1 and v2) counted single transfers;
// whale_count is null in v2 for a coin whose whale transfers were not tracked.
type Totals = { inflow: number; outflow: number; net: number; inflow_usd: number; outflow_usd: number; net_usd: number; whale_count: number | null };
type AssetFlows = { coverage?: string; "24h"?: Totals; "7d"?: Totals };

/** A change in exchange holdings; positive means coins moved onto exchanges. */
type Net = { amount: number; usd: number };
type Reserves = { held: number; held_usd: number; net_1d: Net; net_7d: Net; net_30d: Net };

export type FundFlowsContextData = {
  version?: string;
  status: "available" | "stale" | "unavailable";
  as_of?: string;
  assets?: Record<string, AssetFlows | Reserves>;
  pair_asset?: (AssetFlows | Partial<Reserves>) & { asset: string; status: "tracked" | "not_tracked" | "same_as_reference" | "unavailable" };
  stablecoins?: {
    // Exchange reserves (v3).
    exchange_reserves?: Record<string, Reserves>;
    // Transfer counting (v1, v2).
    exchange_net_24h_usd?: number;
    exchange_net_7d_usd?: number;
    total_supply_usd?: number;
    supply_change_1d_usd?: number;
    supply_change_7d_usd?: number;
  };
};

const isReserves = (flows: AssetFlows | Partial<Reserves>): flows is Reserves => "net_1d" in flows;

function ReserveRow({ asset, flows, locale }: { asset: string; flows: Reserves; locale: string }) {
  return (
    <p>
      <b>{asset}</b>：{uiText("交易所持有 {{p0}}（{{p1}}）", {
        p0: formatAmount(flows.held, locale), p1: formatUsd(flows.held_usd, locale),
      })} · {uiText("1 天 {{p0}} {{p1}}（{{p2}}）", {
        p0: formatAmount(flows.net_1d.amount, locale, true), p1: asset, p2: formatUsd(flows.net_1d.usd, locale, true),
      })} · {uiText("7 天 {{p0}}", { p0: formatUsd(flows.net_7d.usd, locale, true) })} ·{" "}
      {uiText("30 天 {{p0}}", { p0: formatUsd(flows.net_30d.usd, locale, true) })}
    </p>
  );
}

function TransferRow({ asset, flows, locale }: { asset: string; flows: AssetFlows; locale: string }) {
  const day = flows["24h"];
  const week = flows["7d"];
  if (!day || !week) return null;
  return (
    <p>
      <b>{asset}</b>：{uiText("24 小時交易所淨流入 {{p0}} {{p1}}（{{p2}}）", {
        p0: formatAmount(day.net, locale), p1: asset, p2: formatUsd(day.net_usd, locale, true),
      })} · {uiText("7 天 {{p0}}", { p0: formatUsd(week.net_usd, locale, true) })}
      {day.whale_count !== null && <> ·{" "}{uiText("巨鯨轉帳 {{p0}} 筆", { p0: day.whale_count })}</>}
    </p>
  );
}

function Row({ asset, flows, locale }: { asset: string; flows: AssetFlows | Partial<Reserves>; locale: string }) {
  return isReserves(flows)
    ? <ReserveRow asset={asset} flows={flows} locale={locale} />
    : <TransferRow asset={asset} flows={flows as AssetFlows} locale={locale} />;
}

/** The fund flows an analysis saw, as context; the report's own reasoning says how they were weighed. */
export default function FundFlowsContext({ data }: { data?: FundFlowsContextData | null }) {
  const locale = useUiLocale();
  if (!data) return null;
  if (data.status === "unavailable" || !data.assets) return <p className="note">{uiText("資料未取得")}</p>;
  const reserves = data.version === "fund_flows_context_v3";
  const pair = data.pair_asset;
  const stable = data.stablecoins;
  const stableReserves = Object.values(stable?.exchange_reserves ?? {});
  return (
    <>
      <p className="note">
        {reserves
          ? uiText("交易所儲備只作為供給與參與程度的背景：交易所持有量增加可能是準備賣出，減少可能是提領持有，但原因無法確定；資料截至 {{p0}} 00:00 UTC。", {
            p0: data.as_of ? new Date(data.as_of).toLocaleDateString(locale, { month: "numeric", day: "numeric", timeZone: "UTC" }) : "—",
          })
          : uiText("鏈上資金只作為供給與參與程度的背景：流入交易所可能是準備賣出，流出可能是提領持有，但單筆用途無法確定。")}
        {data.status === "stale" && <> {uiText("資料延遲，以下數字可能不是最新。")}</>}
      </p>
      {Object.entries(data.assets).map(([asset, flows]) => <Row key={asset} asset={asset} flows={flows} locale={locale} />)}
      {pair && pair.status === "tracked" && <Row asset={pair.asset} flows={pair} locale={locale} />}
      {pair && pair.status === "not_tracked" && (
        <p className="note">{reserves
          ? uiText("{{p0}} 沒有交易所儲備資料。", { p0: pair.asset })
          : uiText("{{p0}} 不在以太坊上或尚未追蹤，沒有鏈上資料。", { p0: pair.asset })}</p>
      )}
      {stable && reserves && stableReserves.length > 0 && (
        <p>
          <b>{uiText("穩定幣")}</b>：{uiText("交易所持有 USDT＋USDC {{p0}}，7 天 {{p1}}", {
            p0: formatUsd(stableReserves.reduce((n, r) => n + r.held_usd, 0), locale),
            p1: formatUsd(stableReserves.reduce((n, r) => n + r.net_7d.usd, 0), locale, true),
          })}
          {stable.total_supply_usd != null && <> · {uiText("總供給 {{p0}}，7 天 {{p1}}", {
            p0: formatUsd(stable.total_supply_usd, locale), p1: formatUsd(stable.supply_change_7d_usd ?? 0, locale, true),
          })}</>}
        </p>
      )}
      {stable && !reserves && stable.total_supply_usd != null && (
        <p>
          <b>{uiText("穩定幣")}</b>：{uiText("總供給 {{p0}}，24 小時 {{p1}}", {
            p0: formatUsd(stable.total_supply_usd, locale), p1: formatUsd(stable.supply_change_1d_usd ?? 0, locale, true),
          })} · {uiText("24 小時交易所淨流入 {{p0}}", { p0: formatUsd(stable.exchange_net_24h_usd ?? 0, locale, true) })}
        </p>
      )}
    </>
  );
}
