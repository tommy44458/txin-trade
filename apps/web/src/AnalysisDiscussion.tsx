import { uiText, uiLocale, languageName, type UiLocale } from "./i18n/index.ts";
import { useId, useLayoutEffect, useRef } from "react";
import DiscussionContent from "./DiscussionContent";
import { AnalysisSpinner } from "./AnalysisProgress";
import useDiscussion, { type DiscussionSubjectType } from "./useDiscussion";
import { discussionLiveMarketView, type DiscussionLiveMarket } from "./discussionLiveMarket";
import "./AnalysisDiscussion.css";
import { localizeReportText } from "./zhPunctuation";

export type AnalysisDiscussionProps = {
  subjectType: DiscussionSubjectType;
  subjectId: string;
  contextLabel: string;
  resultAt: string;
  stale?: boolean;
  outputLocale?: UiLocale;
};

function stamp(value: string) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString(uiLocale(), {
    timeZone: "Asia/Taipei", hour12: false,
  }) : uiText("時間未提供");
}

function LiveMarketNote({ evidence, subjectMarketId }: {
  evidence?: DiscussionLiveMarket | null; subjectMarketId?: string;
}) {
  const view = discussionLiveMarketView(evidence, subjectMarketId);
  if (!view) return null;
  return <div className="discussion-live-market" data-status={view.status} role="note" aria-label={uiText("本次追問行情")}>
    <p className="discussion-live-quote">
      <strong>{view.pair}{view.price ? ` $${view.price}` : " · " + uiText("現價未取得")}</strong>
      {view.price && view.status === "partial" && <span className="discussion-live-status">{uiText("行情不完整")}</span>}
    </p>
    <p className="discussion-live-meta">
      <span>{uiText("本次追問行情")} · {view.source} · {view.timeframe}</span>
      <span>{view.timeBasis === "quote" ? uiText("報價時間") : uiText("讀取時間")} · {view.observedAt
        ? <time dateTime={view.observedAt}>{stamp(view.observedAt)}</time> : uiText("時間未提供")}</span>
    </p>
    {view.note && <p className="discussion-live-note">{view.note}</p>}
    {view.snapshot && <p className="discussion-live-note" data-snapshot={view.snapshot}>{view.snapshot === "available"
      ? uiText("已重新計算支撐壓力、指標、合約資料、清算與資金流向。")
      : view.snapshot === "partial" ? uiText("已重新計算盤勢，部分資料未取得。")
        : uiText("盤勢未能重新計算，這次只有現價與 K 線。")}</p>}
  </div>;
}

export default function AnalysisDiscussion(props: AnalysisDiscussionProps) {
  // A new result gets a new lifecycle, including refs, scroll state, and abort scope.
  return <DiscussionSession key={`${props.subjectType}:${props.subjectId}`} {...props} />;
}

function DiscussionSession({
  subjectType, subjectId, contextLabel, resultAt, stale = false, outputLocale = "zh-TW",
}: AnalysisDiscussionProps) {
  const discussion = useDiscussion(subjectType, subjectId, outputLocale);
  const headingId = useId();
  const inputId = useId();
  const hintId = useId();
  const countId = useId();
  const viewport = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const anchor = useRef<{ oldestId?: string; height: number; top: number } | null>(null);
  const composing = useRef(false);
  const messages = discussion.data?.messages ?? [];
  const signature = messages.map((message) => `${message.id}:${message.status}:${message.content}:${message.error?.message ?? ""}:${JSON.stringify(message.live_market ?? null)}`).join("\n");
  const oldestId = messages[0]?.id;
  const replyLocale = discussion.data?.session?.response_locale ?? discussion.data?.session?.output_locale ?? discussion.data?.subject.response_locale ?? discussion.data?.subject.output_locale ?? outputLocale;
  const busy = discussion.data?.busy ?? false;
  const canSend = !discussion.loading && !discussion.posting && !discussion.loadingOlder &&
    !discussion.readError && !busy && !discussion.uncertain &&
    !!discussion.draft.trim() && discussion.draft.length <= 6_000;

  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    if (anchor.current && anchor.current.oldestId !== oldestId) {
      element.scrollTop = anchor.current.top + element.scrollHeight - anchor.current.height;
      anchor.current = null;
    } else if (!anchor.current && nearBottom.current) {
      // Only move the chat's viewport. Never scroll the document or focus an element.
      element.scrollTop = element.scrollHeight;
    }
  }, [signature, oldestId]);

  async function loadOlder() {
    const element = viewport.current;
    if (element) anchor.current = { oldestId, height: element.scrollHeight, top: element.scrollTop };
    const loaded = await discussion.loadOlder();
    if (!loaded) anchor.current = null;
  }

  const status = discussion.posting ? uiText("正在送出…") : busy ? discussion.streamFallback ? uiText("正在同步回覆…") : uiText("AI 正在回覆…")
    : discussion.readError ? uiText("讀取中斷") : discussion.loading ? uiText("正在讀取討論…") : "";

  return (
    <section className="analysis-discussion" aria-labelledby={headingId} data-subject-key={`${subjectType}:${subjectId}`}>
      <div className="discussion-heading">
        <h3 id={headingId} className="discussion-visually-hidden">{subjectType === "macro" ? uiText("討論這份解讀") : uiText("向 AI 追問")}</h3>
        <span className="discussion-status" role="status" aria-live="polite">{(discussion.loading || discussion.posting || busy) && <AnalysisSpinner />}{status}</span>
      </div>
      <p className="discussion-context"><span className="discussion-visually-hidden">{contextLabel} · </span>{(subjectType === "fund_flows" ? uiText("資料時間 ·") : uiText("分析快照 ·")) + " "}<time dateTime={resultAt}>{stamp(resultAt)}</time></p>
      <p className="discussion-language">{subjectType === "fund_flows"
        ? uiText("回覆語言：{{p0}}。", { p0: languageName(replyLocale) })
        : uiText("回覆跟隨本次分析的語言：{{p0}}。", { p0: languageName(replyLocale) })}</p>
      {subjectType === "analysis" && <p className="discussion-live-hint">{uiText("每次追問會讀取這個交易對的現價；支撐、壓力與指標沿用原分析。")}</p>}
      {subjectType === "fund_flows" && <p className="discussion-live-hint">{uiText("追問依據開始對話時的資金流向資料；要用最新資料，請在頁面上開新的追問。")}</p>}
      {stale && <p className="discussion-stale" role="note">{uiText("正在討論前一版資料的解讀；更新解讀後會使用另一段對話。")}</p>}

      <div className="discussion-history-tools">
        <span className="discussion-history-label">{uiText("對話紀錄")}</span>
        {discussion.data?.has_more && <button type="button" className="secondary-button" disabled={discussion.loadingOlder || discussion.posting} onClick={() => void loadOlder()}>
          {discussion.loadingOlder ? uiText("讀取更早訊息…") : uiText("載入更早訊息")}
        </button>}
        {discussion.olderError && <span role="alert">{discussion.olderError}{" " + uiText("請再次載入更早訊息。")}</span>}
      </div>
      <div
        className="discussion-messages"
        ref={viewport}
        role="region"
        aria-label={uiText("追問對話訊息")}
        tabIndex={0}
        onScroll={(event) => {
          const element = event.currentTarget;
          nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 72;
        }}
      >
        {!messages.length && <p className="discussion-empty">
          {discussion.loading ? uiText("正在讀取已儲存的對話…") : discussion.readError ? uiText("暫時無法讀取對話。") : uiText("可針對這份結果詢問判斷依據、風險或不同情境。")}
        </p>}
        <ol className="discussion-message-list">
          {messages.map((message) => <li key={message.id} className={`discussion-message discussion-${message.role}`} data-message-id={message.id}>
            <div className="discussion-message-meta">
              <strong>{message.role === "user" ? uiText("你") : uiText("交易員 AI")}</strong>
              <time dateTime={message.created_at}>{stamp(message.created_at)}</time>
            </div>
            {message.role === "assistant" && subjectType === "analysis" && <LiveMarketNote evidence={message.live_market} subjectMarketId={discussion.data?.subject.market_id} />}
            {message.role === "assistant" && message.status === "failed" && message.content &&
              <p className="discussion-incomplete-note" role="note">{uiText("回覆未完成；以下僅為已收到的部分內容。")}</p>}
            {message.content && (message.role === "assistant"
              ? <div lang={replyLocale}><DiscussionContent content={localizeReportText(message.content, replyLocale)} /></div>
              : <p className="discussion-user-content">{message.content}</p>)}
            {message.role === "assistant" && ["queued", "running"].includes(message.status) &&
              <p className={`discussion-pending ${message.content ? "discussion-receiving" : ""}`}>
                {!message.content && <AnalysisSpinner />}
                {message.content ? uiText("正在回覆…") : message.status === "queued" ? uiText("等待 AI 回覆…") : uiText("AI 正在整理回覆…")}
                {!message.content && " " + uiText("切換頁面後仍會繼續。")}
              </p>}
            {message.role === "assistant" && message.status === "failed" && <div className="discussion-failed">
              <p role="alert">{message.error?.message ?? uiText("這次回覆未完成。")}</p>
              {message.error?.retryable && <button type="button" className="secondary-button" disabled={discussion.posting || discussion.loadingOlder || busy || !!discussion.readError} onClick={() => void discussion.retry(message.id)}>{uiText("重試回覆")}</button>}
            </div>}
          </li>)}
        </ol>
      </div>

      <div className="discussion-errors">
        {discussion.readError && <div className="discussion-error" role="alert"><p>{discussion.readError}</p><button type="button" className="secondary-button" disabled={discussion.posting || discussion.loading} onClick={discussion.reload}>{uiText("重新讀取")}</button></div>}{" "}
        {discussion.actionError && <div className="discussion-error" role="alert"><p>{discussion.actionError}</p>
          {!discussion.uncertain && !discussion.readError && <button type="button" className="secondary-button" disabled={discussion.loading || discussion.posting || discussion.loadingOlder} onClick={discussion.reload}>{uiText("重新讀取")}</button>}
        </div>}{" "}
        {discussion.uncertain && !discussion.posting && !busy && <div className="discussion-uncertain">
          <p>{uiText("上一則提問尚未確認送達。重送會沿用同一編號，避免重複建立訊息。")}</p>
          <div><button type="button" className="secondary-button" disabled={discussion.loading || !!discussion.readError || discussion.loadingOlder} onClick={() => void discussion.submit(true)}>{uiText("重送同一則提問")}</button>
          {!discussion.readError && <button type="button" className="secondary-button" disabled={discussion.loading || discussion.loadingOlder} onClick={discussion.reload}>{uiText("重新讀取")}</button>}</div>
        </div>}
      </div>
      <form className="discussion-composer" onSubmit={(event) => { event.preventDefault(); if (canSend) void discussion.submit(); }}>
        <label className="discussion-visually-hidden" htmlFor={inputId}>{uiText("追問內容")}</label>
        <textarea
          id={inputId}
          value={discussion.draft}
          onChange={(event) => discussion.setDraft(event.target.value)}
          disabled={discussion.loading || !!discussion.readError}
          maxLength={6_000}
          rows={3}
          placeholder={subjectType === "macro" ? uiText("討論這份宏觀解讀…")
            : subjectType === "fund_flows" ? uiText("追問這些資金流向…") : uiText("追問這份分析…")}
          aria-describedby={`${hintId} ${countId}`}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.shiftKey || composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
            event.preventDefault();
            if (canSend) void discussion.submit();
          }}
        />
        <div className="discussion-composer-footer">
          <div><p id={hintId}>{uiText("Enter 送出 · Shift + Enter 換行")}</p><span id={countId}>{discussion.draft.length.toLocaleString(uiLocale())} / 6,000</span></div>
          <button type="submit" className="discussion-send" disabled={!canSend} aria-busy={discussion.posting} aria-label={uiText("送出追問")} title={uiText("送出追問")}><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6" /></svg></button>
        </div>
      </form>
    </section>
  );
}
