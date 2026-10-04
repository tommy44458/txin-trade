import { useEffect, useState } from "react";
import Icon from "./Icon";
import { uiText, uiLocale } from "./i18n/index.ts";
import {
  MIN_SAMPLE, loadOutcomes, outcomeFigure, outcomeLabel, outcomeSubject, signedPct, signedR,
  type Outcome, type OutcomeGroup, type OutcomePeriod, type OutcomeStats, type OutcomeSummary,
} from "./outcomes";
import "./TrackRecord.css";

const PAGE = 20;

const periodLabel = (period: OutcomePeriod) =>
  ({ "30d": uiText("近 30 天"), "90d": uiText("近 90 天"), all: uiText("全部") })[period];

const groupLabel = (group: OutcomeGroup | "") => ({
  "": uiText("不分組"), market: uiText("交易對"), timeframe: uiText("週期"), risk: uiText("風險承擔"),
  style: uiText("交易風格"), model: uiText("AI 模型"), prompt: uiText("提示詞版本"),
})[group];

function groupKey(group: OutcomeGroup, key: string): string {
  if (key === "unknown") return uiText("未記錄");
  if (group === "market") return key.split(":").at(-1) ?? key;
  if (group === "risk") return ({ high: uiText("高"), medium: uiText("中"), low: uiText("低") } as Record<string, string>)[key] ?? key;
  if (group === "style") return ({ left: uiText("左側"), right: uiText("右側") } as Record<string, string>)[key] ?? key;
  if (group === "timeframe") return key.toUpperCase();
  return key;
}

const when = (value: string) => new Date(value).toLocaleString(uiLocale(), {
  hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}{note && <small>{note}</small>}</dd>
    </div>
  );
}

function Overview({ stats }: { stats: OutcomeStats }) {
  const { entries, holds, closes, stand_aside: aside } = stats;
  const settled = entries.wins + entries.losses;
  return (
    <div className="track-overview">
      <section className="panel track-card">
        <h2>{uiText("進場建議")}</h2>
        <dl>
          <Figure label={uiText("勝率")} value={entries.win_rate ? `${entries.win_rate}%` : "—"}
            note={uiText("{{p0}} 勝 {{p1}} 負", { p0: entries.wins, p1: entries.losses })} />
          <Figure label={uiText("平均每筆")} value={signedR(entries.average_r)} />
          <Figure label={uiText("累計")} value={signedR(entries.total_r)} />
          <Figure label={uiText("到期／未觸發")} value={`${entries.expired} / ${entries.not_triggered}`} />
        </dl>
        {settled > 0 && settled < MIN_SAMPLE && (
          <p className="track-sample">{uiText("已分勝負的只有 {{p0}} 筆，樣本不足，僅供參考。", { p0: settled })}</p>
        )}
      </section>
      <section className="panel track-card">
        <h2>{uiText("持倉建議")}</h2>
        <dl>
          <Figure label={uiText("續抱達標／失效")} value={`${holds.target_hit} / ${holds.invalidated}`} />
          <Figure label={uiText("續抱平均")} value={signedR(holds.average_r)} />
          <Figure label={uiText("平倉：平得好")} value={closes.good_rate ? `${closes.good_rate}%` : "—"}
            note={uiText("{{p0}} 好 {{p1}} 早", { p0: closes.good, p1: closes.early })} />
        </dl>
      </section>
      <section className="panel track-card">
        <h2>{uiText("觀望之後")}</h2>
        <dl>
          <Figure label={uiText("筆數")} value={String(aside.count)} />
          <Figure label={uiText("平均最大漲幅")} value={signedPct(aside.average_max_up_pct)} />
          <Figure label={uiText("平均最大跌幅")} value={signedPct(aside.average_max_down_pct)} />
        </dl>
        <p className="track-sample">{uiText("觀望很難論對錯，只作參考。")}</p>
      </section>
    </div>
  );
}

export default function TrackRecord({ onOpenAnalysis }: { onOpenAnalysis: (analysisId: string) => void }) {
  const [period, setPeriod] = useState<OutcomePeriod>("all");
  const [group, setGroup] = useState<OutcomeGroup | "">("");
  const [summary, setSummary] = useState<OutcomeSummary | null>(null);
  const [items, setItems] = useState<Outcome[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({ period, ...(group ? { group } : {}) });
    Promise.all([
      loadOutcomes<OutcomeSummary>(`/outcomes/summary?${query}`, controller.signal),
      loadOutcomes<{ items: Outcome[]; total: number }>(
        `/outcomes?period=${period}&limit=${PAGE}&offset=${page * PAGE}`, controller.signal),
    ]).then(([nextSummary, list]) => {
      setSummary(nextSummary);
      setItems(list.items);
      setTotal(list.total);
      setError("");
    }).catch((reason: Error) => {
      if (reason.name !== "AbortError") setError(uiText("無法載入 AI 戰績，請稍後重試。"));
    });
    return () => controller.abort();
  }, [period, group, page]);

  const pages = Math.max(1, Math.ceil(total / PAGE));
  return (
    <div className="track-record">
      <div className="track-controls">
        <div className="segments" role="group" aria-label={uiText("期間")}>
          {(["30d", "90d", "all"] as const).map((value) => (
            <button key={value} type="button" aria-pressed={period === value}
              className={period === value ? "chosen" : ""}
              onClick={() => { setPeriod(value); setPage(0); }}>{periodLabel(value)}</button>
          ))}
        </div>
        <label className="track-group">{uiText("分組")}
          <select value={group} onChange={(event) => setGroup(event.target.value as OutcomeGroup | "")}>
            {(["", "market", "timeframe", "risk", "style", "model", "prompt"] as const).map((value) => (
              <option key={value} value={value}>{groupLabel(value)}</option>
            ))}
          </select>
        </label>
      </div>
      {error && <div className="alert" role="alert">{error}</div>}
      {summary && summary.overall.total === 0 && (
        <div className="placeholder big">{uiText("還沒有可對帳的報告。完成分析後，App 會在背景用之後的行情自動判斷結果。")}</div>
      )}
      {summary && summary.overall.total > 0 && (
        <>
          <Overview stats={summary.overall} />
          {summary.overall.pending > 0 && (
            <p className="note">{uiText("另有 {{p0}} 筆仍在追蹤，結果出來後會自動更新。", { p0: summary.overall.pending })}</p>
          )}
          {group && summary.groups && (
            <section className="panel track-groups">
              <div className="panel-head"><h2>{uiText("依{{p0}}比較", { p0: groupLabel(group) })}</h2></div>
              <div className="track-table" role="table">
                <div role="row" className="track-table-head">
                  <span role="columnheader">{groupLabel(group)}</span>
                  <span role="columnheader">{uiText("勝／負")}</span>
                  <span role="columnheader">{uiText("勝率")}</span>
                  <span role="columnheader">{uiText("平均 R")}</span>
                  <span role="columnheader">{uiText("續抱達標／失效")}</span>
                </div>
                {summary.groups.map((row) => (
                  <div role="row" key={row.key}>
                    <span role="cell">{groupKey(group, row.key)}</span>
                    <span role="cell">{row.entries.wins} / {row.entries.losses}</span>
                    <span role="cell">{row.entries.win_rate ? `${row.entries.win_rate}%` : "—"}</span>
                    <span role="cell">{signedR(row.entries.average_r)}</span>
                    <span role="cell">{row.holds.target_hit} / {row.holds.invalidated}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
          <section className="panel">
            <div className="panel-head">
              <h2>{uiText("逐筆結果")}</h2>
              <span className="tag">{uiText("{{p0}} 筆", { p0: total })}</span>
            </div>
            {items.map((item) => {
              const label = outcomeLabel(item);
              return (
                <button key={item.id} type="button" className="history-row track-row"
                  onClick={() => onOpenAnalysis(item.analysis_id)}>
                  <span>
                    <b>{item.market_id?.split(":").at(-1)} · {item.timeframe?.toUpperCase()} · {outcomeSubject(item)}</b>
                    <small>{when(item.created_at)}</small>
                  </span>
                  <span className="track-result">
                    <i className={`track-badge track-${label.tone}`}>{label.text}</i>
                    <small>{outcomeFigure(item)}</small>
                  </span>
                  <Icon name="arrow" />
                </button>
              );
            })}
            {pages > 1 && (
              <nav className="track-pages" aria-label={uiText("分頁")}>
                <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>{uiText("上一頁")}</button>
                <span>{page + 1} / {pages}</span>
                <button type="button" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>{uiText("下一頁")}</button>
              </nav>
            )}
          </section>
          <p className="track-assumptions">{uiText("以 5 分 K 判斷先碰到止盈還是止損；同一根 K 線同時碰到兩者一律算輸。未計手續費與滑價。R 是止損的那段距離，+2R 代表賺到止損距離的兩倍。原始報告不會被修改。")}</p>
        </>
      )}
    </div>
  );
}

/** The reconciled result of one judged item, shown on its report. */
export function OutcomeLine({ outcome }: { outcome: Outcome }) {
  const label = outcomeLabel(outcome);
  const figure = outcomeFigure(outcome);
  return (
    <p className="outcome-line" role="status">
      <span>{uiText("對帳結果")}</span>
      <i className={`track-badge track-${label.tone}`}>{label.text}</i>
      {figure && <b>{figure}</b>}
      <small>{outcome.status === "pending"
        ? uiText("以之後的 5 分 K 持續判斷，最多 30 天。")
        : uiText("以 5 分 K 判斷，未計手續費與滑價。")}</small>
    </p>
  );
}
