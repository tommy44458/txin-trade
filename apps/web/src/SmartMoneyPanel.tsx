import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import Icon from "./Icon";
import { uiText, type UiLocale } from "./i18n/index.ts";
import { apiFetch } from "./transport";
import {
  assetForMarket,
  closeDate,
  combine,
  FLOW_WINDOWS,
  formatAmount,
  formatUsd,
  netScale,
  reserveOrder,
  type FlowSnapshot,
  type FlowWindow,
  type ReserveChange,
  type ReserveDetail,
  type ReservePoint,
  type ReserveSummary,
  type StablecoinSupply,
} from "./smartMoney";
import "./SmartMoneyPanel.css";

// Reserves change once a day; a refresh every few minutes only notices a new close.
const REFRESH_MS = 5 * 60_000;
const CARD_ASSETS = ["BTC", "ETH"];

async function load<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch(`/api/v1/smart-money${path}`, { signal });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

const windowLabel = (w: string) =>
  ({ "1d": uiText("24 小時"), "7d": uiText("7 天"), "30d": uiText("30 天"), "90d": uiText("90 天") })[w] ?? w;

/** A change as words, never as a buy or sell signal: which way the coins went. */
function NetChange({ change, asset, locale, usdOnly = false }: {
  change: ReserveChange;
  asset: string;
  locale: UiLocale;
  /** Stablecoins together are counted in dollars only. */
  usdOnly?: boolean;
}) {
  if (!change.net) return <p className="sm-net sm-net-none">{uiText("交易所持有量沒有變化")}</p>;
  const into = change.net > 0;
  return (
    <p className={into ? "sm-net sm-net-in" : "sm-net sm-net-out"}>
      <span>{into ? uiText("淨流入交易所") : uiText("淨流出交易所")}</span>
      <strong>
        {usdOnly ? formatUsd(Math.abs(change.net_usd), locale) : `${formatAmount(Math.abs(change.net), locale)} ${asset}`}
      </strong>
      {!usdOnly && <small>{formatUsd(Math.abs(change.net_usd), locale)}</small>}
    </p>
  );
}

/** Each day's change in exchange holdings: up for coins arriving, down for coins leaving. */
function NetBars({ points, locale, label, legend = true, asset }: {
  points: ReservePoint[];
  locale: UiLocale;
  label: string;
  /** The overview cards are narrow; the detail chart below carries the legend. */
  legend?: boolean;
  asset: string;
}) {
  const [active, setActive] = useState<number | null>(null);
  const scale = netScale(points);
  const n = points.length;
  if (!n) return null;
  // The column under the pointer or finger: hover on a desktop, press and drag on a phone.
  const pick = (event: ReactPointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    setActive(Math.min(n - 1, Math.max(0, Math.floor(((event.clientX - box.left) / box.width) * n))));
  };
  const point = active === null ? null : points[active];
  const position = active === null ? 0 : ((active + 0.5) / n) * 100;
  return (
    <figure className="sm-bars">
      <div className={legend ? "sm-plot sm-plot-scrub" : "sm-plot"} tabIndex={0} aria-label={label}
        onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setActive(null)}
        onBlur={() => setActive(null)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
            event.preventDefault();
            const step = event.key === "ArrowLeft" ? -1 : 1;
            setActive((current) => Math.min(n - 1, Math.max(0, (current ?? (step > 0 ? -1 : n)) + step)));
          } else if (event.key === "Escape") setActive(null);
        }}>
        <svg viewBox={`0 0 ${n} 100`} preserveAspectRatio="none" aria-hidden="true">
          {active !== null && <rect className="sm-column" x={active} width={1} y={0} height={100} />}
          <line x1="0" x2={n} y1="50" y2="50" className="sm-axis" />
          {points.map((p, i) => {
            const height = (Math.abs(p.net_usd) / scale) * 48;
            return (
              <g key={p.t} className={active !== null && active !== i ? "sm-dim" : undefined}>
                {p.net_usd > 0 && <rect className="sm-bar-in" x={i + 0.15} width={0.7} y={50 - height} height={height} />}
                {p.net_usd < 0 && <rect className="sm-bar-out" x={i + 0.15} width={0.7} y={50} height={height} />}
              </g>
            );
          })}
        </svg>
        {point && (
          <div className={`sm-tip${position > 66 ? " sm-tip-left" : position < 34 ? " sm-tip-right" : ""}`}
            style={{ left: `${position}%` }} role="status">
            <strong>{uiText("{{p0}} 00:00 UTC", { p0: closeDate(point.t, locale) })}</strong>
            <span>
              <i className={point.net >= 0 ? "sm-bar-in" : "sm-bar-out"} />
              {uiText("前一天變化")} {formatAmount(point.net, locale, true)} {asset} · {formatUsd(point.net_usd, locale, true)}
            </span>
            <span>{uiText("交易所持有")} {formatAmount(point.amount, locale)} {asset}</span>
          </div>
        )}
      </div>
      <figcaption>
        <span>{closeDate(points[0].t, locale)}</span>
        {legend && (
          <span className="sm-legend">
            <i className="sm-bar-in" /> {uiText("流入交易所")}
            <i className="sm-bar-out" /> {uiText("流出交易所")}
          </span>
        )}
        <span>{closeDate(points[n - 1].t, locale)}</span>
      </figcaption>
    </figure>
  );
}

/** Which exchanges hold the coin, and which of them moved it over the window. */
function ExchangeTable({ detail, locale }: { detail: ReserveDetail; locale: UiLocale }) {
  if (!detail.exchanges.length) return <p className="sm-empty">{uiText("這段期間沒有交易所的持有資料。")}</p>;
  return (
    <table className="sm-exchanges">
      <thead>
        <tr>
          <th scope="col">{uiText("交易所")}</th>
          <th scope="col">{uiText("持有")}</th>
          <th scope="col">{uiText("占比")}</th>
          <th scope="col">{uiText("{{p0}}變化", { p0: windowLabel(detail.window) })}</th>
        </tr>
      </thead>
      <tbody>
        {detail.exchanges.map((e) => (
          <tr key={e.exchange}>
            <th scope="row">{e.exchange}</th>
            <td>
              {formatAmount(e.amount, locale)} <small>{formatUsd(e.usd, locale)}</small>
            </td>
            <td>
              <span className="sm-share" style={{ "--share": `${Math.round(e.share * 100)}%` } as React.CSSProperties}>
                {Math.round(e.share * 100)}%
              </span>
            </td>
            <td className={e.net > 0 ? "sm-change-in" : e.net < 0 ? "sm-change-out" : undefined}>
              {formatAmount(e.net, locale, true)} <small>{formatUsd(e.net_usd, locale, true)}</small>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Freshness({ summary, locale }: { summary: ReserveSummary; locale: UiLocale }) {
  if (summary.through === null)
    return <p className="sm-fresh">{uiText("交易所儲備剛開始讀取，資料會在一小時內出現。")}</p>;
  const date = closeDate(summary.through, locale);
  const lagging = summary.lagging.map((l) => l.exchange).join("、");
  return (
    <p className={summary.stale ? "sm-fresh sm-fresh-late" : "sm-fresh"}>
      {summary.stale
        ? uiText("交易所儲備資料延遲，目前只到 {{p0}} 00:00 UTC。", { p0: date })
        : uiText("每天 00:00 UTC 結算一次，目前資料截至 {{p0}}。", { p0: date })}
      {lagging && <> {uiText("{{p0}} 暫停更新，未計入合計。", { p0: lagging })}</>}
    </p>
  );
}

export default function SmartMoneyPanel({ marketId, locale, conversation, onStartConversation }: {
  marketId: string;
  locale: UiLocale;
  /** The follow-up conversation's frozen data: undefined while loading, null when none exists. */
  conversation?: FlowSnapshot | null;
  onStartConversation?: (asset: string, window: FlowWindow) => Promise<void>;
}) {
  const [summary, setSummary] = useState<ReserveSummary | null>(null);
  const [supply, setSupply] = useState<StablecoinSupply | null>(null);
  const [cards, setCards] = useState<Record<string, ReserveDetail>>({});
  const [detail, setDetail] = useState<ReserveDetail | null>(null);
  const [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const marketAsset = assetForMarket(marketId);
  const [asset, setAsset] = useState<string | null>(null);
  const [span, setSpan] = useState<FlowWindow>("30d");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    load<ReserveSummary>("/reserves", controller.signal)
      .then((data) => {
        setSummary(data);
        setError(false);
      })
      .catch(() => !controller.signal.aborted && setError(true))
      .finally(() => !controller.signal.aborted && setLoading(false));
    // The cards' month of daily bars, and total stablecoin supply; each may fail on its own.
    for (const coin of CARD_ASSETS)
      load<ReserveDetail>(`/reserves/${coin}?window=30d`, controller.signal)
        .then((data) => setCards((current) => ({ ...current, [coin]: data })))
        .catch(() => undefined);
    load<{ stablecoins?: StablecoinSupply }>("/overview", controller.signal)
      .then((data) => setSupply(data.stablecoins ?? null))
      .catch(() => undefined);
    const timer = setInterval(() => setRefresh((n) => n + 1), REFRESH_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [refresh]);

  const options = useMemo(() => reserveOrder(Object.keys(summary?.assets ?? {})), [summary]);
  const isTracked = (symbol: string) => options.includes(symbol);
  // Follow the market chosen elsewhere in the app, until a coin is picked here.
  const shown = asset ?? (isTracked(marketAsset) ? marketAsset : "BTC");

  useEffect(() => {
    if (!summary?.through) return;
    const controller = new AbortController();
    load<ReserveDetail>(`/reserves/${shown}?window=${span}`, controller.signal)
      .then(setDetail)
      .catch(() => !controller.signal.aborted && setDetail(null));
    return () => controller.abort();
  }, [shown, span, summary, refresh]);

  const [starting, setStarting] = useState(false);
  const startConversation = () => {
    if (!onStartConversation || starting) return;
    setStarting(true);
    onStartConversation(shown, span).catch(() => undefined).finally(() => setStarting(false));
  };
  const ready = !!summary?.through;
  // The first visit starts a conversation about what is on screen, so the AI panel is ready.
  useEffect(() => {
    if (conversation === null && ready) startConversation();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation, ready]);
  const stable = summary && ["USDT", "USDC"].some((s) => summary.assets[s])
    ? {
      usd: (summary.assets.USDT?.usd ?? 0) + (summary.assets.USDC?.usd ?? 0),
      day: combine([summary.assets.USDT?.["1d"], summary.assets.USDC?.["1d"]]),
      week: combine([summary.assets.USDT?.["7d"], summary.assets.USDC?.["7d"]]),
    }
    : null;
  const cardAssets = CARD_ASSETS.filter((coin) => summary?.assets[coin]);
  const cardCount = cardAssets.length + (stable ? 1 : 0);
  // Phones swipe through the overview cards; the dots follow and jump.
  const overviewRef = useRef<HTMLElement>(null);
  const [card, setCard] = useState(0);
  const cardStep = () => {
    const strip = overviewRef.current;
    const children = strip ? (Array.from(strip.children) as HTMLElement[]) : [];
    return children.length > 1 ? children[1].offsetLeft - children[0].offsetLeft : 1;
  };
  const showCard = (index: number) =>
    overviewRef.current?.scrollTo({ left: index * cardStep(), behavior: "smooth" });

  return (
    <div className="smart-money">
      <div className="page-title">
        <div>
          <h1>{uiText("資金流向")}</h1>
          <p>{uiText("追蹤各大交易所公布錢包裡的幣量變化。幣流入交易所常是準備賣出，流出多是提領持有，但只是線索，不是買賣訊號。")}</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => setRefresh((n) => n + 1)}>
          <Icon name="refresh" />
          {loading ? uiText("讀取中…") : uiText("重新整理")}
        </button>
      </div>
      {error && !summary && (
        <div className="alert" role="alert">{uiText("無法讀取交易所儲備資料，請檢查網路後重新整理。")}</div>
      )}
      {summary && (
        <>
          <div className="sm-meta">
            <Freshness summary={summary} locale={locale} />
            {onStartConversation && ready && (
              <button type="button" className="sm-ask" disabled={starting} onClick={startConversation}
                title={uiText("用目前畫面上的幣種與期間，以最新資料開始新的 AI 追問")}>
                {starting ? uiText("準備中…") : uiText("用最新資料開新的 AI 追問")}
              </button>
            )}
          </div>
          {conversation && (
            <p className="sm-conversation">{uiText("AI 追問依據：{{p0}} · {{p1}} · {{p2}}", {
              p0: conversation.asset, p1: windowLabel(conversation.window),
              p2: new Date(conversation.as_of).toLocaleString(locale, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }),
            })}</p>
          )}
          {ready && (
            <>
              <section className="sm-overview" ref={overviewRef} aria-label={uiText("BTC、ETH 與穩定幣，左右滑動切換")}
                onScroll={(event) => setCard(Math.round(event.currentTarget.scrollLeft / cardStep()))}>
                {cardAssets.map((coin) => {
                  const a = summary.assets[coin];
                  return (
                    <article key={coin} className="panel sm-card">
                      <header>
                        <h2>{coin}</h2>
                        <span>{uiText("最近一天")}</span>
                      </header>
                      <NetChange change={a["1d"]} asset={coin} locale={locale} />
                      <dl className="sm-figures">
                        <div><dt>{uiText("交易所持有")}</dt><dd>{formatAmount(a.amount, locale)} <small>{formatUsd(a.usd, locale)}</small></dd></div>
                        <div><dt>{uiText("計入交易所")}</dt><dd>{a.exchanges}</dd></div>
                        <div><dt>{uiText("7 天變化")}</dt><dd>{formatUsd(a["7d"].net_usd, locale, true)}</dd></div>
                        <div><dt>{uiText("30 天變化")}</dt><dd>{formatUsd(a["30d"].net_usd, locale, true)}</dd></div>
                      </dl>
                      {cards[coin] && (
                        <NetBars points={cards[coin].series} locale={locale} legend={false} asset={coin}
                          label={uiText("{{p0}} 過去 30 天每天交易所持有量的變化", { p0: coin })} />
                      )}
                    </article>
                  );
                })}
                {stable && (
                  <article className="panel sm-card sm-stable">
                    <header>
                      <h2>{uiText("穩定幣")}</h2>
                      <span>USDT · USDC</span>
                    </header>
                    <NetChange change={stable.day} asset="" locale={locale} usdOnly />
                    <dl className="sm-figures">
                      <div><dt>{uiText("交易所持有")}</dt><dd>{formatUsd(stable.usd, locale)}</dd></div>
                      <div><dt>{uiText("7 天變化")}</dt><dd>{formatUsd(stable.week.net_usd, locale, true)}</dd></div>
                      <div><dt>{uiText("總供給")}</dt><dd>{supply?.total_usd ? formatUsd(supply.total_usd, locale) : "—"}</dd></div>
                      <div><dt>{uiText("7 天供給變化")}</dt><dd>{supply?.change_7d == null ? "—" : formatUsd(supply.change_7d, locale, true)}</dd></div>
                    </dl>
                    <p className="sm-note">{uiText("供給增加代表新資金進場；穩定幣流入交易所，通常是準備買進的資金。")}</p>
                  </article>
                )}
              </section>
              <div className="sm-dots">
                {Array.from({ length: cardCount }, (_, i) => (
                  <button key={i} type="button" aria-current={card === i} onClick={() => showCard(i)}
                    aria-label={uiText("第 {{p0}} 張，共 {{p1}} 張", { p0: i + 1, p1: cardCount })} />
                ))}
              </div>

              <section className="panel sm-detail" aria-labelledby="sm-detail-title">
                <div className="sm-detail-head">
                  <div>
                    <h2 id="sm-detail-title">{uiText("{{p0}} 交易所儲備", { p0: shown })}</h2>
                    {detail && (
                      <p className="sm-note">{uiText("合計 {{p0}} 家公布錢包的交易所；每天的變化以當天價格換算美元，期間合計以最新收盤價換算。", { p0: detail.exchanges.length })}</p>
                    )}
                  </div>
                  <div className="sm-controls">
                    <label className="sm-select">
                      <span className="control-label">{uiText("幣種")}</span>
                      <select value={shown} onChange={(e) => setAsset(e.target.value)}>
                        {options.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    </label>
                    <div className="segments" role="group" aria-label={uiText("期間")}>
                      {FLOW_WINDOWS.map((w) => (
                        <button key={w} type="button" aria-pressed={span === w}
                          className={span === w ? "chosen" : ""} onClick={() => setSpan(w)}>
                          {windowLabel(w)}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
                {!isTracked(marketAsset) && asset === null && (
                  <p className="sm-untracked">
                    {uiText("{{p0}} 沒有交易所儲備資料，可能是 DefiLlama 尚未讀取它所在的鏈；先顯示 BTC。", { p0: marketAsset })}
                  </p>
                )}
                {detail && detail.asset === shown && (
                  <>
                    <NetChange change={detail.totals} asset={shown} locale={locale} />
                    <dl className="sm-figures sm-figures-wide">
                      <div><dt>{uiText("交易所持有")}</dt><dd>{formatAmount(detail.amount, locale)} <small>{formatUsd(detail.usd, locale)}</small></dd></div>
                      <div><dt>{uiText("{{p0}}變化", { p0: windowLabel(detail.window) })}</dt><dd>{formatAmount(detail.totals.net, locale, true)} <small>{formatUsd(detail.totals.net_usd, locale, true)}</small></dd></div>
                    </dl>
                    <NetBars points={detail.series} locale={locale} asset={shown}
                      label={uiText("{{p0}} 每天交易所持有量的變化", { p0: shown })} />
                    <h3 className="sm-section-title">{uiText("各交易所")}</h3>
                    <ExchangeTable detail={detail} locale={locale} />
                    {detail.excluded.length > 0 && (
                      <p className="sm-note">
                        {uiText("已排除 {{p0}} 天疑似交易所更新錢包清單造成的跳動：{{p1}}。", {
                          p0: detail.excluded.length,
                          p1: detail.excluded.map((c) => `${c.exchange} ${closeDate(c.day, locale)} ${formatAmount(c.net, locale, true)}`).join("、"),
                        })}
                      </p>
                    )}
                  </>
                )}
              </section>
            </>
          )}
          <p className="sm-sources">
            {uiText("資料來源：DefiLlama 交易所透明度資料（各交易所公布的錢包），由 txinTrade 雲端每天讀取，保留 90 天；穩定幣總供給同樣來自 DefiLlama。沒有公布錢包的交易所（例如 Coinbase）不在其中。")}
          </p>
        </>
      )}
    </div>
  );
}
