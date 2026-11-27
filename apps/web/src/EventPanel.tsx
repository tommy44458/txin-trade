import { economicMetricLabel } from "./economicLabels";
import { uiText, uiLocale } from "./i18n/index.ts";
import "./EventPanel.css";
import MacroInterpretationPanel, {
  type MacroInterpretationPanelProps,
} from "./MacroInterpretationPanel";

export type EventContext = {
  status: "available" | "no_events" | "partial" | "offline";
  source_status: Record<string, "ok" | "offline">;
  risk: "none" | "high_impact_window" | "unknown_major_event";
  cutoff: string;
  events: {
    id: string;
    version: number;
    source: string;
    kind: string;
    title: string;
    scheduled_date: string;
    scheduled_at: string | null;
    time_precision: string;
    source_url: string;
    ingested_at: string;
    actual_value: string | null;
    official_result?: {
      decision: "raise" | "lower" | "maintain";
      lower_pct: string;
      upper_pct: string;
      source_url: string;
      published_at: string;
      ingested_at: string;
      parser_version: string;
    };
  }[];
  official_actuals?: {
    id: string;
    version: number;
    source: "bls" | "bea";
    kind: string;
    metric: string;
    label: string;
    period: string;
    value: string;
    unit: "percent" | "thousand_jobs";
    previous_value: string | null;
    source_url: string;
    published_at: string | null;
    ingested_at: string;
    method: string;
  }[];
  actual_source_status?: Record<string, "ok" | "partial" | "offline">;
  /** Annual changes the agency did not publish with the latest monthly release. */
  official_actual_gaps?: {
    source: "bls" | "bea";
    kind: string;
    metric: string;
    label: string;
    period: string;
    comparison_period: string;
    reason: "comparison_period_not_published";
    previous: { period: string; value: string } | null;
    source_url: string;
  }[];
  actual_values_status: string;
  news_status: string;
};

const decisionText: Record<string, string> = {
  get raise() { return uiText("調升"); },
  get lower() { return uiText("調降"); },
  get maintain() { return uiText("維持"); },
};
const sources: Record<string, string> = { bls: "BLS", bea: "BEA", fed: "Fed" };
const stamp = (value: string) =>
  new Date(value).toLocaleString(uiLocale(), {
    timeZone: "Asia/Taipei",
    hour12: false,
  });

export default function EventPanel({
  context,
  macroInterpretation,
  macroEnsuring,
  macroError,
  onEnsureMacro,
  onTranslateMacro,
  macroTranslating,
}: {
  context: EventContext | null | undefined;
  macroInterpretation?: MacroInterpretationPanelProps["interpretation"];
  macroEnsuring?: boolean;
  macroError?: string | null;
  onEnsureMacro?: () => void;
  onTranslateMacro?: () => void;
  macroTranslating?: boolean;
}) {
  const macroPanel = (
    <MacroInterpretationPanel
      interpretation={macroInterpretation}
      ensuring={macroEnsuring}
      error={macroError}
      onEnsure={onEnsureMacro}
      onTranslate={onTranslateMacro}
      translating={macroTranslating}
    />
  );
  if (!context)
    return (
      <div className="economics-workspace">
        <section className="panel event-panel">
          <div className="panel-head">
            <h2>{uiText("官方經濟資料")}</h2>
          </div>
          <p className="event-note">{uiText("正在讀取已同步的官方日程與實際值…")}</p>
        </section>
        {macroPanel}
      </div>
    );
  return (
    <div className="economics-workspace">
      <section className="panel event-panel" data-section="economic-indicators">
        <div className="panel-head">
          <div>
            <h2>{uiText("最新公布數據")}</h2>
          </div>
          <span className="tag">BLS · BEA</span>
        </div>
        <p className="panel-copy">{uiText("消費者物價、就業、個人消費支出與經濟成長。數值代表各指標的資料期間。")}</p>
        <div className="event-sources">
          {Object.entries(context.actual_source_status ?? {}).map(
            ([source, status]) => (
              <span
                className={status === "ok" ? "source-ok" : status === "partial" ? "source-partial" : "source-offline"}
                key={source}
              >
                <i />
                {sources[source] ?? source}{" "}{uiText("數值：")}{" "}{status === "ok" ? uiText("已同步")
                  : status === "partial" ? uiText("部分取得") : uiText("離線／過期")}
              </span>
            ),
          )}
        </div>
        {context.official_actuals?.length || context.official_actual_gaps?.length ? (
          <div className="economic-metrics">
            {context.official_actual_gaps?.map((gap) => (
              <article className="economic-metric metric-unpublished" key={`gap:${gap.metric}:${gap.period}`}>
                <div className="metric-label">
                  <h3>{economicMetricLabel(gap.label, gap.metric)}</h3>
                  <span>{sources[gap.source]}</span>
                </div>
                <div className="metric-value">{uiText("未公布")}</div>
                <p className="metric-period">{uiText("資料期間") + " "}{gap.period}</p>
                <div className="metric-comparison">
                  <span>{uiText("前一期")}</span>
                  <strong>{gap.previous ? `${gap.previous.value}%（${gap.previous.period}）` : uiText("未提供")}</strong>
                </div>
                <p className="metric-note">{uiText("官方沒有 {{p0}} 的資料可比較，所以沒有公布這期的年增率。", { p0: gap.comparison_period })}</p>
              </article>
            ))}
            {context.official_actuals?.filter((actual) => !context.official_actual_gaps?.some(
              (gap) => gap.metric === actual.metric)).map((actual) => {
              const unit = actual.unit === "percent" ? "%" : uiText("千人");
              const age = Math.max(
                0,
                Math.floor(
                  (Date.parse(context.cutoff) -
                    Date.parse(actual.published_at ?? actual.ingested_at)) /
                    86_400_000,
                ),
              );
              return (
                <article
                  className="economic-metric"
                  key={`${actual.id}:${actual.version}`}
                >
                  <div className="metric-label">
                    <h3>{economicMetricLabel(actual.label, actual.metric)}</h3>
                    <span>{sources[actual.source]}</span>
                  </div>
                  <div className="metric-value">
                    {actual.value}
                    <span>{unit}</span>
                  </div>
                  <p className="metric-period">{uiText("資料期間") + " "}{actual.period}</p>
                  <div className="metric-comparison">
                    <span>{uiText("前一期")}</span>
                    <strong>
                      {actual.previous_value != null
                        ? `${actual.previous_value}${unit === "%" ? "%" : " " + uiText("千人")}`
                        : uiText("未提供")}
                    </strong>
                  </div>
                  <details className="metric-provenance">
                    <summary>{uiText("來源與發布時間")}</summary>
                    <small>
                      {actual.published_at
                        ? uiText("官方發布 {{p0}}", { p0: stamp(actual.published_at) })
                        : uiText("BLS API 未提供精確發布時間")}
                    </small>
                    <small>{uiText("本機取得")}{" "}{stamp(actual.ingested_at)}{" "}{uiText("（台灣時間）")}</small>
                    <small>{uiText("距資料快照約")}{" "}{age}{" " + uiText("天 · 資料版本") + " "}{actual.version}
                    </small>
                    {actual.method === "derived_from_bls_v1_series" && (
                      <small>{uiText("由 BLS 公布數列計算，四捨五入至一位小數；可能與新聞稿顯示值略有差異。")}</small>
                    )}
                    <a
                      href={actual.source_url}
                      target="_blank"
                      rel="noreferrer"
                    >{uiText("查看官方數據 ↗")}</a>
                  </details>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="placeholder">{uiText("尚無可用實際值，請查看來源狀態或重新整理。")}</div>
        )}
      </section>
      {macroPanel}
      <section className="panel event-panel calendar-panel">
        <div className="panel-head">
          <div>
            <h2>{uiText("公布日程")}</h2>
          </div>
          <span className="tag">{uiText("台灣時間")}</span>
        </div>
        <div className="event-sources">
          {Object.entries(context.source_status).map(([source, status]) => (
            <span
              className={status === "ok" ? "source-ok" : "source-offline"}
              key={source}
            >
              <i />
              {sources[source] ?? source}：
              {status === "ok" ? uiText("已同步") : uiText("離線／過期")}
            </span>
          ))}
        </div>
        {context.status === "offline" && (
          <div className="warning">{uiText("官方日程來源目前離線；舊資料不能當作完整事件清單。")}</div>
        )}{" "}
        {context.status === "partial" && (
          <div className="warning">{uiText("部分官方日程來源離線，事件資料不完整。")}</div>
        )}{" "}
        {context.risk !== "none" && (
          <div className="warning">
            {context.risk === "high_impact_window"
              ? uiText("目前接近重大指標公布時間（公布前 30 分鐘至後 15 分鐘）。")
              : uiText("今日有 FOMC 會議，但官方日曆未提供精確公布時間。")}
          </div>
        )}{" "}
        {context.events.length ? (
          <div className="event-list">
            {context.events.map((event) => {
              const date = event.scheduled_at
                ? new Date(event.scheduled_at).toLocaleDateString(uiLocale(), {
                    timeZone: "Asia/Taipei",
                    month: "2-digit",
                    day: "2-digit",
                  })
                : event.scheduled_date.slice(5).replace("-", "/");
              return (
                <article
                  className="event-item"
                  key={`${event.id}:${event.version}`}
                >
                  <div className="event-date">
                    <strong>{date}</strong>
                    <small>
                      {event.scheduled_at
                        ? new Date(event.scheduled_at).toLocaleTimeString(
                            uiLocale(),
                            {
                              timeZone: "Asia/Taipei",
                              hour12: false,
                              hour: "2-digit",
                              minute: "2-digit",
                            },
                          )
                        : uiText("美東日期")}
                    </small>
                  </div>
                  <div className="event-description">
                    <h3>{event.title}</h3>
                    <small>
                      {sources[event.source] ?? event.source} ·{" "}
                      {event.scheduled_at
                        ? uiText("{{p0}}（台灣時間）", { p0: stamp(event.scheduled_at) })
                        : uiText("{{p0}}（美東日期，時間未公布）", { p0: event.scheduled_date })}
                    </small>
                    {event.official_result && (
                      <div className="event-result">
                        <strong>{uiText("官方聲明：")}{" "}{decisionText[event.official_result.decision]}{" "}{uiText("目標區間至")}{" "}{event.actual_value}
                        </strong>
                        <small>{uiText("發布")}{" "}{stamp(event.official_result.published_at)}{" "}{uiText("· 取得")}{" "}{stamp(event.official_result.ingested_at)}{" "}{uiText("（台灣時間）")}</small>
                        <a
                          href={event.official_result.source_url}
                          target="_blank"
                          rel="noreferrer"
                        >{uiText("查看 Fed 聲明 ↗")}</a>
                      </div>
                    )}
                  </div>
                  <a
                    className="event-source-link"
                    href={event.source_url}
                    target="_blank"
                    rel="noreferrer"
                  >{uiText("官方日程 ↗")}</a>
                </article>
              );
            })}
          </div>
        ) : (
          <p className="event-note">
            {context.status === "no_events"
              ? uiText("查詢區間內沒有相關官方日程。")
              : uiText("目前沒有可顯示的事件，請查看來源狀態。")}
          </p>
        )}
        <p className="event-note">{uiText("資料快照：")}{" "}{stamp(context.cutoff)}{" "}{uiText("（台灣時間）。日程不代表結果已公布；市場預期值尚未接入。")}</p>
      </section>
    </div>
  );
}
