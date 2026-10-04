import { pythonReferenceText } from "./pythonReferenceText";
import { uiText, uiLocale, useUiLocale, setUiLocale, isUiLocale, type UiLocale } from "./i18n/index.ts";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import CandlestickChart from "./CandlestickChart";
import "./App.css";
import "./Workspace.css";
import AnalysisEvidence, {
  type Reasoning,
  type CurrentCandle,
  type MacroContext,
  type EntryDecision,
  type EntryRiskReference,
} from "./AnalysisEvidence";
import { type AnalysisExecution } from "./TechnicalIndicators";
import { type BookEvidence } from "./OrderBookEvidence";
import StrategyCard, { type Strategy } from "./StrategyCard";
import { type TimeframeContextData } from "./TimeframeContext";
import EditPositionForm from "./EditPositionForm";
import SelectControl from "./SelectControl";
import MarketPicker, { type Market } from "./MarketPicker";
import useMarketFavorites from "./useMarketFavorites";
import useTradingPreferences from "./useTradingPreferences";
import useLatestAnalysis from "./useLatestAnalysis";
import { applyPendingAnalysisUpdate } from "./latestPositionAnalysis";
import PositionOptionalFields, {
  type OptionalPositionFields,
} from "./PositionOptionalFields";
import EventPanel, { type EventContext } from "./EventPanel";
import NewsPanel, { type NewsContext } from "./NewsPanel";
import PositionReviewCard, { type PositionReview } from "./PositionReviewCard";
import PositionLevels, { type PositionChartSnapshot } from "./PositionLevels";
import useMacroInterpretation from "./useMacroInterpretation";
import type { MacroInterpretationStatus } from "./MacroInterpretationPanel";
import { levelLocationLabel, type LevelLocation } from "./levelLocation";
import AnalysisFailure from "./AnalysisFailure";
import DiscussionSidebar from "./DiscussionSidebar";
import type { AnalysisDiscussionProps } from "./AnalysisDiscussion";
import LeverageControl from "./LeverageControl";
import { sessionAccount, type SessionAccount, type SessionInfo } from "./sessionIdentity";
import { reportIndicators, type IndicatorReport } from "./chartIndicators";
import { ANALYSIS_TIMEFRAMES, isAnalysisTimeframe, timeframeCode, timeframeLabel, type AnalysisTimeframe } from "./timeframes";
import AnalysisProgress, {
  AnalysisSpinner,
  type AnalysisKind,
  type AnalysisProgressData,
} from "./AnalysisProgress";
import ExchangeSyncPanel from "./ExchangeSyncPanel";
import { levelLadder } from "./levelLadder";
import { localizeReportText } from "./zhPunctuation";
import { importedPositionFacts, isManualPosition, liquidationSourceLabel, positionSourceLabel, type PositionSource } from "./positionSources";
import Icon, { type IconName } from "./Icon";
import AccountMenu from "./AccountMenu";
import { CLOUD_ACCOUNT_CHANGED } from "./cloudAccountEvents";
import SmartMoneyPanel from "./SmartMoneyPanel";
import TrackRecord, { OutcomeLine } from "./TrackRecord";
import { loadOutcomes, type Outcome } from "./outcomes";
import type { FlowSnapshot, FlowWindow } from "./smartMoney";
import UpdateDialog from "./UpdateDialog";
import CloudSignInNotice from "./CloudSignInNotice";
import UpdateNotice from "./UpdateNotice";
import LocalCloudMenu from "./LocalCloudMenu";
import DerivativesContext, { type DerivativesData } from "./DerivativesContext";
import MarketReference, { type MarketReferenceData } from "./MarketReference";
import FundFlowsContext, { type FundFlowsContextData } from "./FundFlowsContext";
import DirectionAssessment from "./DirectionAssessment";
import SettingsPanel from "./SettingsPanel";
import {
  readModelConnection,
  settingsRequest,
  type LocalSettings,
  type TradingPreferences,
} from "./localSettings";
import { apiFetch, isRemoteMode } from "./transport.ts";

type Candle = {
  open_time: string;
  close_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume?: string;
  closed?: boolean;
};
type Position = {
  id: string;
  market_id: string;
  version: number;
  side: "long" | "short";
  leverage: number;
  margin_mode: "isolated" | "cross";
  entry_price: string;
  quantity: string;
  stop_loss: string | null;
  take_profit: string | null;
  previous_stop_loss?: string | null;
  entry_time?: string | null;
  exchange_liquidation_price?: string | null;
  notes?: string | null;
  source?: PositionSource;
  contract_type?: "perpetual" | "standard" | null;
  synced_at?: string | null;
};
type Level = LevelLocation & {
  id?: string;
  kind: string;
  low: string;
  high: string;
  center: string;
  confirmed_at: string;
  algorithm_version?: string;
  touch_count?: number;
  relative_pivot_volume?: string;
  pivot_count?: number;
  independent_touch_count?: number;
  evidence_score?: string;
  order_book_snapshot_overlap?: boolean;
  zone_state?: string;
};
type MarketContext = {
  market_id: string;
  timeframe: AnalysisTimeframe;
  as_of: string;
  candles: Candle[];
  chart_candles?: Candle[];
  forming_candle: Candle | null;
  levels: Level[];
  recently_invalidated_levels?: Level[];
  quote: {
    price: string;
    mark_price?: string;
    index_price?: string;
    last_funding_rate?: string;
    observed_at: string;
  };
  history: {
    requested_candles: number;
    closed_candle_count: number;
    start_at: string;
    end_at: string;
    status: string;
  };
  level_metadata: {
    algorithm_version: string;
    reference_price: string;
    reference_time: string;
    source_time: string;
  };
};
const EMPTY_CANDLES: Candle[] = [];
const EMPTY_LEVELS: Level[] = [];
type CostAssumptions = {
  fee_bps_per_fill: string;
  slippage_bps_per_fill: string;
  source: string;
};
type Report = {
  output_locale?: UiLocale;
  response_locale?: UiLocale;
  derivatives_context?: DerivativesData | null;
  market_reference?: MarketReferenceData | null;
  fund_flows_context?: FundFlowsContextData | null;
  market_id: string;
  timeframe: string;
  generated_at: string;
  data_source: string;
  analysis_mode: string;
  market_snapshot_sha256?: string;
  strategy_level_algorithm_version?: string;
  report_schema_version?: string;
  cost_assumptions?: CostAssumptions;
  quote: {
    price: string;
    mark_price?: string;
    index_price?: string;
    last_funding_rate?: string;
    observed_at: string;
  };
  analysis_execution?: AnalysisExecution | null;
  analysis_leverage: number;
  reasoning?: Reasoning;
  macro_context?: MacroContext;
  macro_interpretation?: MacroInterpretationStatus | null;
  entry_decision?: EntryDecision | null;
  entry_risk_reference?: EntryRiskReference | null;
  agent_stance?: "long" | "short" | "wait" | null;
  current_candle?: CurrentCandle;
  tool_trace?: {
    tool: string;
    reason: string;
    parameters: Record<string, unknown>;
    result: Record<string, unknown>;
  }[];
  fallback_reason?: string | null;
  metrics: {
    last_close: string;
    ma20: string;
    ma50: string;
    atr14: string;
    trend: string;
    market_state?: string;
    last_candle_at: string;
    levels: Level[];
    level_algorithm_version?: string;
    order_book?: BookEvidence;
    level_reference_price?: string;
    level_reference_time?: string;
  };
  timeframe_context?: TimeframeContextData;
  strategies: Strategy[];
  preference_assessment: {
    directional_bias: string | null;
    risk_tolerance: string | null;
    trading_style?: "left" | "right" | null;
    consistency: string;
  };
  position_reviews: PositionReview[];
  account_equity_usdt?: string | null;
  model_note: string | null;
  limitations: string[];
  events_status: string;
  event_context?: EventContext | null;
  news_context?: NewsContext | null;
};
type Job = {
  id: string;
  status: string;
  phase: string;
  created_at: string;
  freshness?: "fresh" | "stale";
  stale_reasons?: string[];
  submitted_input: {
    kind?: string;
    market_id: string;
    timeframe: string;
    directional_bias: string | null;
    risk_tolerance: string | null;
    trading_style?: "left" | "right" | null;
    leverage?: number;
    output_locale?: UiLocale;
    account_equity_usdt?: string | null;
  };
  report: Report | null;
  error: { code?: string; message: string } | null;
  // What a running analysis can already show; absent for finished ones and older computers.
  progress?: AnalysisProgressData | null;
};

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await apiFetch(`/api/v1${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : typeof body?.detail?.message === "string"
          ? body.detail.message
        : uiText("請求失敗 ({{p0}})", { p0: res.status }),
    );
  }
  return res.json();
}
const fmt = (v?: string | number | null, digits?: number) => {
  if (v == null) return "—";
  const value = Number(v);
  const precision =
    digits ??
    (Math.abs(value) >= 1000
      ? 2
      : Math.abs(value) >= 100
        ? 3
        : Math.abs(value) >= 1
          ? 4
          : 6);
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits ?? 2,
    maximumFractionDigits: precision,
  });
};
const when = (v?: string | null) =>
  v
    ? new Date(v).toLocaleString(uiLocale(), {
        hour12: false,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const localDateTimeInput = (value?: string | null) => {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
};
const optionalPositionPayload = (value: OptionalPositionFields) => ({
  entry_time: value.entry_time
    ? new Date(value.entry_time).toISOString()
    : null,
  exchange_liquidation_price: value.exchange_liquidation_price || null,
  notes: value.notes.trim() || null,
});

function TradingStyleSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <>
      <span className="field-label" id="style-label">{uiText("進場風格")}</span>
      <div
        className="choices style-choices"
        role="group"
        aria-labelledby="style-label"
      >
        <button
          type="button"
          className={value === "left" ? "choice chosen" : "choice"}
          aria-pressed={value === "left"}
          onClick={() => onChange(value === "left" ? "" : "left")}
        >
          <strong>{uiText("左側交易")}</strong>
          <small>{uiText("靠近關鍵價位時提前布局")}</small>
        </button>
        <button
          type="button"
          className={value === "right" ? "choice chosen" : "choice"}
          aria-pressed={value === "right"}
          onClick={() => onChange(value === "right" ? "" : "right")}
        >
          <strong>{uiText("右側交易")}</strong>
          <small>{uiText("確認價格反應後參與")}</small>
        </button>
      </div>
    </>
  );
}

function RiskSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <>
      <span className="field-label" id="risk-label">{uiText("風險傾向")}</span>
      <div className="choices three" role="group" aria-labelledby="risk-label">
        {(
          [
            ["low", uiText("低")],
            ["medium", uiText("中")],
            ["high", uiText("高")],
          ] as const
        ).map(([risk, label]) => (
          <button
            type="button"
            key={risk}
            className={value === risk ? "choice chosen" : "choice"}
            aria-pressed={value === risk}
            onClick={() => onChange(value === risk ? "" : risk)}
          >
            {label}
          </button>
        ))}
      </div>
    </>
  );
}

function App({ remoteSection, remoteIdentity, remoteMenu, remoteStatus }: {
  remoteSection?: ReactNode;
  /** On the remote page: the signed-in cloud account shown in the sidebar. */
  remoteIdentity?: SessionAccount | null;
  /** On the remote page: account menu items (computers, sign-out) in place of the local ones. */
  remoteMenu?: ReactNode;
  /** On the remote page: which computer the screens are reaching, in place of "local mode". */
  remoteStatus?: ReactNode;
} = {}) {
  const locale = useUiLocale();
  const [view, setView] = useState<
    "market" | "positions" | "smartMoney" | "events" | "history" | "settings"
  >("market");
  const [localSettings, setLocalSettings] = useState<LocalSettings | null>(
    null,
  );
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const [markets, setMarkets] = useState<Market[]>([]);
  const marketFavorites = useMarketFavorites();
  const [chosenMarketId, setMarketId] = useState("binance:perp:BTCUSDT");
  const [viewingHistoricalPosition, setViewingHistoricalPosition] = useState(false);
  const [newPositionMarketId, setNewPositionMarketId] = useState("");
  const [positions, setPositions] = useState<Position[]>([]);
  const positionMarkets = useMemo(() => [...new Set(positions.map((position) => position.market_id))]
    .map((id) => markets.find((item) => item.id === id) ?? {
      id, symbol: id.split(":").at(-1)?.replace(/USDT$/, "/USDT") ?? id,
      exchange: "Binance", quote_asset: "USDT", contract_type: "PERPETUAL",
    }), [positions, markets]);
  const marketId = view === "positions" && !viewingHistoricalPosition
    ? positionMarkets.find((item) => item.id === chosenMarketId)?.id ?? positionMarkets[0]?.id ?? ""
    : chosenMarketId;
  const manualMarketId = newPositionMarketId || marketId || chosenMarketId;
  const [timeframe, setTimeframe] = useState<AnalysisTimeframe>("1h");
  const [bias, setBias] = useState("");
  const [risk, setRisk] = useState("");
  const [tradingStyle, setTradingStyle] = useState("");
  const [leverage, setLeverage] = useState(5);
  const [analysisLeverageValid, setAnalysisLeverageValid] = useState(true);
  const [accountEquity, setAccountEquity] = useState("");
  const tradingPreferences = useTradingPreferences((settings, touched) => {
    setLocalSettings((current) => current ?? settings);
    if (!isRemoteMode() && isUiLocale(settings.ui_locale)) void setUiLocale(settings.ui_locale).catch(() => {});
    const saved = settings.trading_preferences;
    if (!saved) return;
    if (!touched.has("market_id") && saved.market_id) setMarketId(saved.market_id);
    if (!touched.has("timeframe") && isAnalysisTimeframe(saved.timeframe)) setTimeframe(saved.timeframe);
    if (!touched.has("directional_bias")) setBias(saved.directional_bias ?? "");
    if (!touched.has("risk_tolerance")) setRisk(saved.risk_tolerance ?? "");
    if (!touched.has("trading_style")) setTradingStyle(saved.trading_style ?? "");
    if (!touched.has("leverage")) setLeverage(saved.leverage);
  });
  const [marketData, setMarketData] = useState<MarketContext | null>(null);
  const [marketRefresh, setMarketRefresh] = useState(0);
  const [marketRequest, setMarketRequest] = useState<{
    marketId: string; timeframe: AnalysisTimeframe; refresh: number; error: string;
  } | null>(null);
  const currentMarketData =
    marketData?.market_id === marketId && marketData.timeframe === timeframe
      ? marketData
      : null;
  const currentMarketRequest = marketRequest?.marketId === marketId && marketRequest.timeframe === timeframe && marketRequest.refresh === marketRefresh
    ? marketRequest : null;
  const marketLoading = !!marketId && !currentMarketRequest;
  const marketError = currentMarketRequest?.error ?? "";
  const candles = currentMarketData?.candles ?? EMPTY_CANDLES;
  const formingCandle = currentMarketData?.forming_candle;
  const chartCandles = useMemo(() => currentMarketData?.chart_candles ??
    (formingCandle ? [...candles, { ...formingCandle, closed: false }] : candles),
    [currentMarketData?.chart_candles, candles, formingCandle]);
  const quote = currentMarketData?.quote ?? null;
  const [analysisJob, setJob] = useState<Job | null>(null);
  // Polling belongs to the submitted task, independently of the report being viewed.
  const [pendingJob, setPendingJob] = useState<Job | null>(null);
  const [manualPositionAnalysisId, setManualPositionAnalysisId] = useState<string | null>(null);
  const synchronizeLatestTimeframe = useRef(true);
  const manualPositionRef = useRef<HTMLDetailsElement>(null);
  /** The field to bring into view once the position editor opens, e.g. a stop loss the exchange does not report. */
  const [editFocus, setEditFocus] = useState<"stop_loss" | null>(null);
  /** Open the manual position form below the list, bring it into view, and start at its first field. */
  function openManualPosition() {
    const details = manualPositionRef.current;
    if (!details) return;
    details.open = true;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    details.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
    details.querySelector<HTMLElement>("form input, form button, form select")?.focus({ preventScroll: true });
  }
  useEffect(() => {
    // Closing or synchronizing positions may select a different pair without using the picker.
    synchronizeLatestTimeframe.current = true;
  }, [marketId]);
  const [history, setHistory] = useState<Job[]>([]);
  // The record page opens on the AI track record; past analyses are one tap away.
  const [historyTab, setHistoryTab] = useState<"record" | "analyses">("record");
  const [liveEvents, setLiveEvents] = useState<EventContext | null>(null);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [eventsError, setEventsError] = useState("");
  const macroInterpretation = useMacroInterpretation(view === "events", locale);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const account = remoteIdentity !== undefined ? remoteIdentity : sessionAccount(session);
  // The sidebar account follows the cloud account on its own: a sign-in can finish in the browser
  // after Settings was left, or a sign-out can happen from the account menu.
  useEffect(() => {
    if (remoteIdentity !== undefined) return;
    const reload = () => {
      if (document.visibilityState === "visible") api<SessionInfo>("/session").then(setSession).catch(() => {});
    };
    const timer = window.setInterval(reload, 15_000);
    window.addEventListener(CLOUD_ACCOUNT_CHANGED, reload);
    window.addEventListener("focus", reload);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener(CLOUD_ACCOUNT_CHANGED, reload);
      window.removeEventListener("focus", reload);
    };
  }, [remoteIdentity]);
  const [selected, setSelected] = useState<string[]>([]);
  const marketPositions = useMemo(() => positions.filter((position) => position.market_id === marketId),
    [positions, marketId]);
  const selectedPositionIds = selected.filter((id) => marketPositions.some((position) => position.id === id));
  const allPositionsSelected = marketPositions.length > 0 && selectedPositionIds.length === marketPositions.length;
  const [form, setForm] = useState({
    side: "long" as "long" | "short",
    leverage: 5,
    margin_mode: "isolated" as "isolated" | "cross",
    entry_price: "",
    quantity: "",
    stop_loss: "",
    take_profit: "",
    entry_time: "",
    exchange_liquidation_price: "",
    notes: "",
  });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({
    side: "long" as "long" | "short",
    leverage: 5,
    margin_mode: "isolated" as "isolated" | "cross",
    entry_price: "",
    quantity: "",
    stop_loss: "",
    take_profit: "",
    entry_time: "",
    exchange_liquidation_price: "",
    notes: "",
  });
  const [error, setError] = useState("");
  const [submittingKind, setSubmittingKind] = useState<AnalysisKind | null>(
    null,
  );
  const manualPositionJob = manualPositionAnalysisId === analysisJob?.id &&
    analysisJob?.submitted_input.kind === "positions" && analysisJob.submitted_input.market_id === marketId
    ? analysisJob : null;
  const pendingPositionJob = pendingJob?.submitted_input.kind === "positions" &&
    pendingJob.submitted_input.market_id === marketId ? pendingJob : null;
  const showLatestTimeframe = (latest: Job) => {
    if (synchronizeLatestTimeframe.current && isAnalysisTimeframe(latest.submitted_input.timeframe)) {
      // This selects the saved report's visible timeframe, without changing saved defaults.
      tradingPreferences.touched.current.add("timeframe");
      setTimeframe(latest.submitted_input.timeframe);
    }
  };
  const latestPositionAnalysis = useLatestAnalysis<Job>(
    "positions",
    view === "positions" && !viewingHistoricalPosition && !manualPositionJob &&
      !pendingPositionJob && submittingKind !== "positions",
    marketId,
    showLatestTimeframe,
  );
  // A report just requested or opened from history wins; otherwise each pair shows its latest one.
  const viewedMarketJob = analysisJob?.submitted_input.kind !== "positions" &&
    analysisJob?.submitted_input.market_id === marketId ? analysisJob : null;
  const latestMarketAnalysis = useLatestAnalysis<Job>(
    "market",
    view === "market" && !viewedMarketJob && submittingKind !== "market",
    marketId,
    showLatestTimeframe,
  );
  const job = view === "positions" && !viewingHistoricalPosition
    ? pendingPositionJob ?? manualPositionJob ?? latestPositionAnalysis.data
    : view === "market"
      ? viewedMarketJob ?? latestMarketAnalysis.data
      : analysisJob;
  const shownReport =
    submittingKind !== "positions" && job?.report?.market_id === marketId && job.report.timeframe === timeframe
      ? job.report
      : null;
  // Traditional Chinese reports read with full-width commas; the saved report stays as written.
  const report = useMemo(() => localizeReportText(shownReport,
    shownReport?.response_locale ?? shownReport?.output_locale ?? job?.submitted_input.output_locale ?? "zh-TW"),
  [shownReport, job?.submitted_input.output_locale]);
  // The reconciled result of the report on screen, once the background check has it.
  const outcomeJobId = job?.status === "completed" && report ? job.id : null;
  const [reportOutcomes, setReportOutcomes] = useState<{ id: string; items: Outcome[] } | null>(null);
  useEffect(() => {
    if (!outcomeJobId) return;
    const controller = new AbortController();
    loadOutcomes<{ items: Outcome[] }>(`/analyses/${outcomeJobId}/outcomes`, controller.signal)
      .then((result) => setReportOutcomes({ id: outcomeJobId, items: result.items }))
      .catch(() => { /* An older computer has no reconciliation; the report stands alone. */ });
    return () => controller.abort();
  }, [outcomeJobId]);
  const shownOutcomes = reportOutcomes?.id === outcomeJobId ? reportOutcomes.items : [];
  const entryOutcome = shownOutcomes.find((item) => item.item_key === "entry");
  // Indicators the report used, drawn only on the chart of that same timeframe.
  const chartIndicators = useMemo(() => reportIndicators(report as IndicatorReport | null), [report]);
  const jobId = pendingJob?.id;
  const jobStatus = pendingJob?.status;
  const positionChartId = view === "positions" && job?.submitted_input.kind === "positions" &&
    job.status === "completed" && report ? job.id : null;
  const [positionChartRefresh, setPositionChartRefresh] = useState(0);
  const [positionChartResult, setPositionChartResult] = useState<{
    analysisId: string; refresh: number; data: PositionChartSnapshot | null; error: string;
  } | null>(null);
  const currentPositionChart = positionChartResult?.analysisId === positionChartId &&
    positionChartResult.refresh === positionChartRefresh ? positionChartResult : null;
  const market = markets.find((m) => m.id === marketId) ?? positionMarkets.find((m) => m.id === marketId);
  const changed =
    !!job &&
    (job.submitted_input.market_id !== marketId ||
      job.submitted_input.timeframe !== timeframe ||
      (job.submitted_input.directional_bias ?? "") !== bias ||
      (job.submitted_input.risk_tolerance ?? "") !== risk ||
      (job.submitted_input.trading_style ?? "") !== tradingStyle ||
      (job.submitted_input.leverage ?? 5) !== leverage ||
      (job.submitted_input.kind === "positions" &&
        (job.submitted_input.account_equity_usdt ?? "") !== accountEquity));
  const visibleLevels = currentMarketData?.levels ?? EMPTY_LEVELS;
  const chartLevels = useMemo(() => [...visibleLevels, ...(currentMarketData?.recently_invalidated_levels ?? [])],
    [visibleLevels, currentMarketData?.recently_invalidated_levels]);
  useEffect(() => {
    if (!editingId || !editFocus) return;
    const input = document.querySelector<HTMLInputElement>(`.position-editor input[name="${editFocus}"]`);
    if (!input) return;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    input.closest("form")?.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "center" });
    input.focus({ preventScroll: true });
    setEditFocus(null);
  }, [editingId, editFocus]);

  async function refreshLocalSettings() {
    try {
      setLocalSettings(await settingsRequest<LocalSettings>("/settings"));
      setModelReady(null);
    } catch {
      // Existing API deployments may not expose the desktop settings routes.
      if (window.tradeHelper) setError(uiText("無法更新分析設定，請重新開啟設定再試一次。"));
    }
  }

  useEffect(() => {
    let active = true;
    function refreshMarkets() {
      api<Market[]>("/markets")
        .then((items) => { if (active) setMarkets(items); })
        .catch((e) => { if (active) setError(e.message); });
    }
    refreshMarkets();
    const marketRefreshTimer = window.setInterval(refreshMarkets, 15 * 60 * 1000);
    api<Job[]>("/analyses")
      .then((items) =>
        setHistory(
          items.filter((item) =>
            item.submitted_input.market_id.startsWith("binance:perp:"),
          ),
        ),
      )
      .catch(() => {});
    api<Position[]>("/positions")
      .then((items) => {
        setPositions(items);
      })
      .catch(() => {});
    api<EventContext>("/events")
      .then(setLiveEvents)
      .catch((e) => setEventsError(e.message))
      .finally(() => setEventsLoading(false));
    api<SessionInfo>("/session")
      .then(setSession)
      .catch(() => {});
    return () => {
      active = false;
      window.clearInterval(marketRefreshTimer);
    };
  }, []);
  function chooseRisk(value: string) {
    setRisk(value);
    tradingPreferences.save({ risk_tolerance: (value || null) as TradingPreferences["risk_tolerance"] });
  }
  function chooseTradingStyle(value: string) {
    setTradingStyle(value);
    tradingPreferences.save({ trading_style: (value || null) as TradingPreferences["trading_style"] });
  }
  function chooseBias(value: "bullish" | "bearish") {
    const next = bias === value ? null : value;
    setBias(next ?? "");
    tradingPreferences.save({ directional_bias: next });
  }
  useEffect(() => {
    if (!marketId) return;
    let active = true;
    const controller = new AbortController();
    api<MarketContext>(
      `/market-context?market_id=${encodeURIComponent(marketId)}&timeframe=${timeframe}&refresh=${marketRefresh ? 1 : 0}`,
      { signal: controller.signal },
    ).then((context) => {
      if (!active) return;
      setMarketData(context);
      setMarketRequest({ marketId, timeframe, refresh: marketRefresh, error: "" });
    }).catch((reason: Error) => {
      if (!active) return;
      setMarketRequest({ marketId, timeframe, refresh: marketRefresh, error: reason.message });
    });
    return () => { active = false; controller.abort(); };
  }, [marketId, timeframe, marketRefresh]);
  useEffect(() => {
    if (!jobId || !jobStatus || !["queued", "running"].includes(jobStatus))
      return;
    let active = true;
    const controller = new AbortController();
    const timer = window.setInterval(
      () =>
        api<Job>(`/analyses/${jobId}`, { signal: controller.signal })
          .then((updated) => {
            if (!active) return;
            setPendingJob((current) => current?.id === updated.id
              ? ["queued", "running"].includes(updated.status) ? updated : null
              : current);
            setJob((current) => applyPendingAnalysisUpdate(current, updated));
          })
          .catch((e) => { if (active && !controller.signal.aborted) setError(e.message); }),
      1500,
    );
    return () => { active = false; controller.abort(); clearInterval(timer); };
  }, [jobId, jobStatus]);

  useEffect(() => {
    if (!positionChartId) return;
    let active = true;
    const controller = new AbortController();
    api<PositionChartSnapshot>(`/analyses/${positionChartId}/chart-snapshot`, { signal: controller.signal })
      .then((data) => { if (active) setPositionChartResult({
        analysisId: positionChartId, refresh: positionChartRefresh, data, error: "",
      }); })
      .catch((reason: Error) => { if (active) setPositionChartResult({
        analysisId: positionChartId, refresh: positionChartRefresh, data: null, error: reason.message,
      }); });
    return () => { active = false; controller.abort(); };
  }, [positionChartId, positionChartRefresh]);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [view]);

  useEffect(() => {
    if (view !== "events") return;
    const controller = new AbortController();
    api<EventContext>("/events", { signal: controller.signal })
      .then((context) => { setLiveEvents(context); setEventsError(""); })
      .catch((reason: Error) => { if (!controller.signal.aborted) setEventsError(reason.message); });
    return () => controller.abort();
  }, [view, macroInterpretation.data?.dataset_version]);

  async function analyze(kind: "market" | "positions") {
    if (!analysisLeverageValid) return;
    if (kind === "positions" && (!marketId || !selectedPositionIds.length)) return;
    latestPositionAnalysis.invalidate();
    latestMarketAnalysis.invalidate();
    setSubmittingKind(kind);
    setError("");
    try {
      const connection = await readModelConnection();
      setLocalSettings(connection.settings);
      setModelReady(connection.ready);
      if (!connection.ready) {
        setError(
          connection.error ||
          uiText("尚未連線 AI 分析帳號，請在設定完成 Codex 或 Claude Code 連線後再分析。"),
        );
        setView("settings");
        return;
      }
      const result = await api<Job>("/analyses", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          kind,
          output_locale: locale,
          market_id: marketId,
          timeframe,
          leverage,
          directional_bias: bias || null,
          risk_tolerance: risk || null,
          trading_style: tradingStyle || null,
          position_ids: kind === "positions" ? selectedPositionIds : [],
          account_equity_usdt:
            kind === "positions" ? accountEquity || null : null,
        }),
      });
      setJob(result);
      setPendingJob(["queued", "running"].includes(result.status) ? result : null);
      setManualPositionAnalysisId(kind === "positions" ? result.id : null);
      setView(kind === "positions" ? "positions" : "market");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmittingKind(null);
    }
  }
  async function refreshPositions() {
    const latest = await api<Position[]>("/positions");
    setPositions(latest);
    setSelected((items) => items.filter((id) => latest.some((position) => position.id === id)));
    latestPositionAnalysis.refresh();
    if (analysisJob) {
      const updated = await api<Job>(`/analyses/${analysisJob.id}`);
      setJob((current) => current?.id === updated.id ? updated : current);
    }
  }
  async function addPosition(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const position = await api<Position>("/positions", {
        method: "POST",
        body: JSON.stringify({
          market_id: manualMarketId,
          ...form,
          stop_loss: form.stop_loss || null,
          take_profit: form.take_profit || null,
          ...optionalPositionPayload(form),
        }),
      });
      setPositions((items) => [position, ...items]);
      setMarketId(position.market_id);
      setViewingHistoricalPosition(false);
      setManualPositionAnalysisId(null);
      synchronizeLatestTimeframe.current = true;
      setSelected([]);
      tradingPreferences.save({ market_id: position.market_id });
      setForm({
        ...form,
        entry_price: "",
        quantity: "",
        stop_loss: "",
        take_profit: "",
        entry_time: "",
        exchange_liquidation_price: "",
        notes: "",
      });
    } catch (err) {
      setError((err as Error).message);
    }
  }
  async function closePosition(id: string) {
    const position = positions.find((item) => item.id === id);
    if (!position || !isManualPosition(position.source)) return;
    try {
      await api(`/positions/${id}/close`, { method: "POST" });
      setPositions((items) => items.filter((p) => p.id !== id));
      setSelected((items) => items.filter((x) => x !== id));
      latestPositionAnalysis.refresh();
      if (analysisJob) {
        const updated = await api<Job>(`/analyses/${analysisJob.id}`);
        setJob((current) => current?.id === updated.id ? updated : current);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function deletePosition(id: string) {
    if (!window.confirm(uiText("確定刪除這筆持倉紀錄？包含它的個人分析紀錄也會刪除。")))
      return;
    try {
      await api(`/positions/${id}`, { method: "DELETE" });
      setPositions((items) => items.filter((p) => p.id !== id));
      setSelected((items) => items.filter((x) => x !== id));
      setJob(null);
      setPendingJob(null);
      setManualPositionAnalysisId(null);
      latestPositionAnalysis.refresh();
      setHistory(await api<Job[]>("/analyses"));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function savePosition(e: React.FormEvent) {
    e.preventDefault();
    const current = positions.find((p) => p.id === editingId);
    if (!current) return;
    try {
      const updated = await api<Position>(`/positions/${current.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          ...(isManualPosition(current.source) ? { ...editForm, ...optionalPositionPayload(editForm) }
            : { ...importedPositionFacts(current), notes: editForm.notes || null }),
          market_id: current.market_id,
          expected_version: current.version,
          stop_loss: editForm.stop_loss || null,
          take_profit: editForm.take_profit || null,
        }),
      });
      setPositions((items) =>
        items.map((p) => (p.id === updated.id ? updated : p)),
      );
      setEditingId(null);
      latestPositionAnalysis.refresh();
      if (analysisJob) {
        const updated = await api<Job>(`/analyses/${analysisJob.id}`);
        setJob((current) => current?.id === updated.id ? updated : current);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function refreshEvents() {
    setEventsLoading(true);
    setEventsError("");
    try {
      setLiveEvents(await api<EventContext>("/events"));
      macroInterpretation.refresh();
    } catch (e) {
      setEventsError((e as Error).message);
    } finally {
      setEventsLoading(false);
    }
  }
  const pages: { id: typeof view; label: string; icon: IconName }[] = [
    { id: "market", label: uiText("市場分析"), icon: "market" },
    { id: "positions", label: uiText("我的持倉"), icon: "positions" },
    { id: "smartMoney", label: uiText("資金流向"), icon: "flows" },
    { id: "events", label: uiText("經濟事件"), icon: "events" },
    { id: "history", label: uiText("紀錄與表現"), icon: "history" },
    { id: "settings", label: uiText("設定"), icon: "settings" },
  ];
  const lastMarketsPage = useRef<"events" | "smartMoney">("events");
  // The fund-flows follow-up: undefined while loading, null when none was started yet.
  const [flowSnapshot, setFlowSnapshot] = useState<FlowSnapshot | null | undefined>(undefined);
  const startFlowConversation = async (asset: string, window: FlowWindow) => {
    setFlowSnapshot(await api<FlowSnapshot>("/smart-money/snapshots", {
      method: "POST", body: JSON.stringify({ asset, window }),
    }));
  };
  useEffect(() => {
    if (view !== "smartMoney" || flowSnapshot !== undefined) return;
    api<{ snapshot: FlowSnapshot | null }>("/smart-money/snapshots/latest")
      .then((latest) => setFlowSnapshot(latest.snapshot))
      .catch(() => setFlowSnapshot(null));
  }, [view, flowSnapshot]);
  const openPage = (id: typeof view) => {
    if (id === "events" || id === "smartMoney") lastMarketsPage.current = id;
    if (id !== view || viewingHistoricalPosition) latestPositionAnalysis.invalidate();
    if (id === "positions" && view === "positions" && !viewingHistoricalPosition)
      latestPositionAnalysis.refresh();
    if (id !== view) latestMarketAnalysis.invalidate();
    if (id === "market" && view === "market") latestMarketAnalysis.refresh();
    setManualPositionAnalysisId(null);
    synchronizeLatestTimeframe.current = true;
    setView(id);
    setViewingHistoricalPosition(false);
    if (id === "positions") setSelected([]);
    if (id === "history")
      api<Job[]>("/analyses")
        .then((items) =>
          setHistory(
            items.filter((item) =>
              item.submitted_input.market_id.startsWith(
                "binance:perp:",
              ),
            ),
          ),
        )
        .catch((e) => setError(e.message));
  };
  /** Show a past analysis on its own page, with the settings it was made with. */
  const openAnalysis = (item: Job) => {
    latestPositionAnalysis.invalidate();
    latestMarketAnalysis.invalidate();
    setManualPositionAnalysisId(null);
    // A delayed preference load must not replace a report being viewed.
    for (const key of ["market_id", "timeframe", "directional_bias", "risk_tolerance", "trading_style", "leverage"] as const)
      tradingPreferences.touched.current.add(key);
    setMarketId(item.submitted_input.market_id);
    if (isAnalysisTimeframe(item.submitted_input.timeframe))
      setTimeframe(item.submitted_input.timeframe);
    setBias(item.submitted_input.directional_bias ?? "");
    setRisk(item.submitted_input.risk_tolerance ?? "");
    setTradingStyle(item.submitted_input.trading_style ?? "");
    setLeverage(item.submitted_input.leverage ?? 5);
    setAccountEquity(item.submitted_input.account_equity_usdt ?? "");
    setJob(item);
    if (["queued", "running"].includes(item.status))
      setPendingJob((current) => current ?? item);
    setViewingHistoricalPosition(item.submitted_input.kind === "positions");
    setView(item.submitted_input.kind === "positions" ? "positions" : "market");
  };
  // Phones show four tabs: economic events and fund flows share one "Markets" tab,
  // which reopens whichever of the two was seen last.
  const mergedMarkets = pages.some((page) => page.id === "smartMoney");
  const isMarketsPage = view === "events" || view === "smartMoney";
  const navButton = (page: (typeof pages)[number]) => (
    <button
      key={page.id}
      className={[
        "nav",
        view === page.id ? "active" : "",
        mergedMarkets && (page.id === "events" || page.id === "smartMoney") ? "nav-wide-only" : "",
      ].filter(Boolean).join(" ")}
      aria-current={view === page.id ? "page" : undefined}
      onClick={() => openPage(page.id)}
    >
      <Icon name={page.icon} />
      <span className="nav-text">{page.label}</span>
      {page.id === "positions" && positions.length > 0 && (
        <small>{positions.length}</small>
      )}
    </button>
  );
  const running = pendingJob?.status === "queued" || pendingJob?.status === "running";
  const busy = submittingKind !== null || running;
  const activeAnalysisKind =
    submittingKind ?? (running ? pendingJob.submitted_input.kind : null);
  const marketBusy = busy && activeAnalysisKind === "market";
  const positionsBusy = busy && activeAnalysisKind === "positions";
  const analysisPhase = submittingKind
    ? "submitting"
    : pendingJob?.phase === "retrying"
      ? "retrying"
      : pendingJob?.status === "queued"
        ? "queued"
        : (pendingJob?.phase ?? "");
  const busyLabel = positionsBusy ? uiText("正在分析持倉…") : uiText("正在分析中…");
  const macroResult = macroInterpretation.data?.interpretation;
  const flowWindowLabel = (w: FlowWindow) =>
    ({ "1d": uiText("24 小時"), "7d": uiText("7 天"), "30d": uiText("30 天"), "90d": uiText("90 天") })[w];
  const analysisMatchesView = (view === "positions" && job?.submitted_input.kind === "positions") ||
    (view === "market" && job?.submitted_input.kind !== "positions");
  const discussionSubject: AnalysisDiscussionProps | null = view === "smartMoney" && flowSnapshot
    ? {
      subjectType: "fund_flows", subjectId: flowSnapshot.id,
      contextLabel: `${uiText("資金流向")} · ${flowSnapshot.asset} · ${flowWindowLabel(flowSnapshot.window)}`,
      resultAt: flowSnapshot.as_of, outputLocale: locale,
    }
    : view === "events" && macroResult
    ? {
      subjectType: "macro", subjectId: macroResult.id,
      contextLabel: uiText("宏觀解讀"), resultAt: macroResult.generated_at,
      stale: macroInterpretation.data?.stale,
      outputLocale: macroResult.response_locale ?? macroResult.output_locale ?? "zh-TW",
    }
    : analysisMatchesView && report && job?.status === "completed"
      ? {
        subjectType: "analysis", subjectId: job.id,
        contextLabel: `${market?.symbol ?? report.market_id} · ${report.timeframe.toUpperCase()} · ${view === "positions" ? uiText("持倉分析") : uiText("市場分析")}`,
        resultAt: report.generated_at,
        outputLocale: report.response_locale ?? report.output_locale ?? job.submitted_input.output_locale ?? "zh-TW",
      }
      : null;
  const marketControls = (
    <div className="controls">
      <MarketPicker
        markets={view === "positions" ? positionMarkets : markets}
        value={marketId}
        label={view === "positions" ? uiText("持倉交易對") : uiText("交易對")}
        emptyLabel={uiText("尚無持倉交易對")}
        onChange={(id) => {
          if (id !== marketId || viewingHistoricalPosition) latestPositionAnalysis.invalidate();
          if (id !== marketId) latestMarketAnalysis.invalidate();
          setMarketId(id);
          setViewingHistoricalPosition(false);
          setManualPositionAnalysisId(null);
          synchronizeLatestTimeframe.current = true;
          setBias("");
          tradingPreferences.save({ market_id: id, directional_bias: null });
          setSelected([]);
        }}
        favoriteIds={marketFavorites.favoriteIds}
        onToggleFavorite={marketFavorites.toggleFavorite}
        favoritesReady={marketFavorites.ready}
        favoritesSaving={marketFavorites.saving}
        favoritesError={marketFavorites.error}
      />
      <div className="timeframe-control">
        <span className="control-label" id="timeframe-label">{uiText("分析週期")}</span>
        <div
          className="segments timeframe-segments"
          role="group"
          aria-labelledby="timeframe-label"
        >
          {ANALYSIS_TIMEFRAMES.map((value) => (
            <button
              key={value}
              type="button"
              aria-label={timeframeLabel(value)}
              title={timeframeLabel(value)}
              aria-pressed={timeframe === value}
              className={timeframe === value ? "chosen" : ""}
              onClick={() => {
                synchronizeLatestTimeframe.current = false;
                setTimeframe(value);
                setBias("");
                tradingPreferences.save({ timeframe: value, directional_bias: null });
              }}
            >
              {timeframeCode(value)}
            </button>
          ))}
        </div>
      </div>
      <LeverageControl
        label={uiText("分析槓桿")}
        value={leverage}
        onChange={(next) => {
          setLeverage(next);
          tradingPreferences.save({ leverage: next });
        }}
        onValidityChange={setAnalysisLeverageValid}
      />
      <div className="quote">
        <div className="quote-heading">
          <small>{uiText("最新成交價 · USDT")}</small>
          <button type="button" className="quote-refresh" aria-label={uiText("刷新行情")}
            title={uiText("更新現價、K 線與支撐壓力")} disabled={!marketId || marketLoading} aria-busy={marketLoading}
            onClick={() => setMarketRefresh((value) => value + 1)}>
            {marketLoading ? <AnalysisSpinner /> : <Icon name="refresh" />}
          </button>
        </div>
        <strong>{quote ? fmt(quote.price) : "—"}</strong>
        <small>{marketError && quote ? uiText("上次取得") : uiText("更新於")}{" "} {when(quote?.observed_at)}</small>
      </div>
    </div>
  );

  return (
    <div className="shell">
      {remoteIdentity === undefined && <UpdateDialog />}
      <a className="skip-link" href="#main-content">{uiText("跳至主要內容")}</a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            latestPositionAnalysis.invalidate();
            if (view !== "market") latestMarketAnalysis.invalidate();
            setManualPositionAnalysisId(null);
            setView("market");
          }}
          aria-label={`txinTrade · ${uiText("市場分析")}`}
        >
          <span className="brand-wordmark">txin<strong>Trade</strong></span>
        </a>
        <div className="nav-label">{uiText("工作空間")}</div>
        <nav aria-label={uiText("主要導覽")}>
          {pages.filter((page) => page.id !== "settings").flatMap((page) => [
            navButton(page),
            // On phones the merged tab takes the place right after My positions.
            ...(mergedMarkets && page.id === "positions" ? [
              <button key="markets"
                className={isMarketsPage ? "nav nav-phone-only active" : "nav nav-phone-only"}
                aria-current={isMarketsPage ? "page" : undefined}
                onClick={() => openPage(lastMarketsPage.current)}
              >
                <Icon name="pulse" />
                <span className="nav-text">{uiText("市場動態")}</span>
              </button>,
            ] : []),
          ])}
        </nav>
        <UpdateNotice />
        <CloudSignInNotice onOpen={() => {
          openPage("settings");
          // Bring the cloud account section into view once Settings has rendered.
          window.setTimeout(() => document.getElementById("settings-cloud-title")?.closest("section")
            ?.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
        }} />
        <AccountMenu account={account} settingsActive={view === "settings"} onOpenSettings={() => openPage("settings")}>
          {remoteMenu ?? <LocalCloudMenu onOpenSettings={() => openPage("settings")}
            onChanged={() => { api<SessionInfo>("/session").then(setSession).catch(() => {}); }} />}
        </AccountMenu>
      </aside>
      <main id="main-content" tabIndex={-1}>
        <header className="topbar">
          <span className="topbar-crumb">{uiText("工作台")}<b>/</b> {pages.find((page) => page.id === view)?.label}
          </span>
          <span className="workspace-status">
            {tradingPreferences.saving ? <><AnalysisSpinner />{" "}{uiText("儲存偏好中…")}</>
              : remoteStatus ?? <><i />{" "}{uiText("本地模式")}</>}
          </span>
        </header>
        <div className="content">
          {(view === "market" || view === "positions") && tradingPreferences.error && (
            <div className="alert preference-error" role="alert">
              <span>{tradingPreferences.error}</span>
              <button type="button" onClick={tradingPreferences.retry}>
                {tradingPreferences.initialSettings ? uiText("重新儲存") : uiText("重新載入")}
              </button>
            </div>
          )}{" "}
          {error && (
            <div className="alert" role="alert">
              <span>{error}</span>
              <button onClick={() => setError("")} aria-label={uiText("關閉錯誤訊息")}>
                <Icon name="close" />
              </button>
            </div>
          )}{" "}
          {(view === "market" || view === "positions") &&
            modelReady === false &&
            (localSettings?.desktop || window.tradeHelper) && (
              <div className="model-connection-prompt" role="status">
                <p>{uiText("連線 Codex 或 Claude Code 後，即可開始 AI 分析。")}</p>
                <button type="button" onClick={() => setView("settings")}>{uiText("前往設定")}</button>
              </div>
            )}{" "}
          {view === "settings" && (
            <SettingsPanel
              initialSettings={localSettings}
              onChanged={refreshLocalSettings}
              onCloudAccountChanged={() => {
                api<SessionInfo>("/session").then(setSession).catch(() => {});
              }}
              remoteSection={remoteSection}
            />
          )}
          {view === "market" && (
            <>
              {marketControls}
              <div className="analysis-workspace">
                <aside className="analysis-config" aria-label={uiText("分析設定")}>
                  <details className="analysis-config-options" open={!report}>
                    <summary>{uiText("調整交易偏好")}{" "}
                      <span>
                        {risk === "high"
                          ? uiText("高風險")
                          : risk === "medium"
                            ? uiText("中風險")
                            : risk === "low"
                              ? uiText("低風險")
                              : uiText("未設定風險")}{" "}
                        ·{" "}
                        {tradingStyle === "left"
                          ? uiText("左側")
                          : tradingStyle === "right"
                            ? uiText("右側")
                            : uiText("未指定風格")}
                      </span>
                    </summary>
                    <section className="panel">
                      <span className="field-label" id="bias-label">{uiText("個人方向判斷")}</span>
                      <div
                        className="choices"
                        role="group"
                        aria-labelledby="bias-label"
                      >
                        <button
                          aria-pressed={bias === "bullish"}
                          className={
                            bias === "bullish" ? "choice chosen" : "choice"
                          }
                          onClick={() => chooseBias("bullish")}
                        >{uiText("偏多")}</button>
                        <button
                          aria-pressed={bias === "bearish"}
                          className={
                            bias === "bearish" ? "choice chosen" : "choice"
                          }
                          onClick={() => chooseBias("bearish")}
                        >{uiText("偏空")}</button>
                      </div>
                      <RiskSelector value={risk} onChange={chooseRisk} />
                      <TradingStyleSelector
                        value={tradingStyle}
                        onChange={chooseTradingStyle}
                      />
                    </section>
                  </details>
                  <button
                    className="action"
                    disabled={busy || !analysisLeverageValid}
                    aria-busy={marketBusy}
                    onClick={() => analyze("market")}
                  >
                    {busy ? busyLabel : uiText("開始市場分析")}{" "}
                    {!busy && <Icon name="arrow" />}
                  </button>
                </aside>
                <div className="analysis-main">
                  {marketBusy && (
                    <AnalysisProgress kind="market" phase={analysisPhase} progress={pendingJob?.progress}
                      outputLocale={pendingJob?.submitted_input.output_locale} />
                  )}
                  {!marketBusy && latestMarketAnalysis.status === "loading" && (
                    <div className="market-context-status" role="status" aria-busy="true">
                      <AnalysisSpinner />
                      <span>{uiText("正在載入此交易對的最新市場分析…")}</span>
                    </div>
                  )}
                  {!marketBusy && latestMarketAnalysis.status === "empty" && (
                    <p className="note" role="status">{uiText("此交易對尚無已完成的市場分析。調整交易偏好後開始分析。")}</p>
                  )}
                  {!marketBusy && latestMarketAnalysis.status === "error" && (
                    <div className="market-context-status" role="alert" data-error="true">
                      <span>{latestMarketAnalysis.error}</span>
                      <button type="button" className="settings-link" onClick={latestMarketAnalysis.refresh}>{uiText("重試")}</button>
                    </div>
                  )}
                  {!marketBusy &&
                    job?.submitted_input.kind === "market" &&
                    job.status === "failed" && (
                      <AnalysisFailure
                        error={job.error}
                        jobId={job.id}
                        market={market?.symbol ?? marketId}
                        timeframe={job.submitted_input.timeframe}
                        kind="market"
                      />
                    )}{" "}
                  {changed && report && (
                    <div className="warning" role="status">{uiText("設定已變更。下方是上次分析，重新分析後更新。")}</div>
                  )}{" "}
                  {report && (
                    <DirectionAssessment assessment={report.reasoning?.direction_assessment}
                      bias={report.preference_assessment?.directional_bias} />
                  )}{" "}
                  {report && entryOutcome && <OutcomeLine outcome={entryOutcome} />}
                  {report && (
                    <AnalysisEvidence
                        outputLocale={report.response_locale ?? report.output_locale ?? job?.submitted_input.output_locale ?? "zh-TW"}
                      mode={report.analysis_mode}
                      reasoning={report.reasoning}
                      runs={report.tool_trace}
                      fallbackReason={report.fallback_reason}
                      entryDecision={report.entry_decision}
                      riskTolerance={report.preference_assessment?.risk_tolerance}
                      entryRiskReference={report.entry_risk_reference}
                      analysisLeverage={report.analysis_leverage}
                      macroContext={report.macro_context}
                      macroInterpretation={report.macro_interpretation}
                      currentCandle={report.current_candle}
                      snapshotObservedAt={report.quote.observed_at}
                      generatedAt={report.generated_at}
                      execution={report.analysis_execution}
                    />
                  )}

                  <details className="market-reference" open>
                    <summary>
                      <span>
                        <Icon name="market" />{uiText("行情圖表與支撐壓力")}</span>
                      <small>
                        {market?.symbol} · {timeframe.toUpperCase()}
                      </small>
                    </summary>
                    <div className="reference-panels">
                      <section className="panel">
                        {/* The summary above already names the pair and timeframe. */}
                        <div className="market-context-status" data-error={!!marketError}>
                          <span role="status">
                            {marketError ? <>
                              {marketError}{" "}{currentMarketData ? uiText("；保留上次行情，尚未更新。") : uiText("；尚未取得行情與區間。")}
                            </> : marketLoading ? uiText("正在更新行情與支撐壓力…") : uiText("現價所在區間以本次行情更新判斷")}
                          </span>
                          {marketError && <button type="button" className="settings-link" disabled={marketLoading}
                            onClick={() => setMarketRefresh((value) => value + 1)}>{uiText("重試")}</button>}
                          <div className="legend">{uiText("● 上漲")}<span>{uiText("● 下跌")}</span>{uiText("◼ 支撐／壓力")}</div>
                        </div>
                        <CandlestickChart candles={chartCandles} levels={chartLevels} quotePrice={quote?.price}
                          marketId={marketId} timeframe={timeframe} loading={marketLoading && !currentMarketData}
                          levelsLoading={marketLoading} indicators={chartIndicators} />
                        <div className="chart-foot">
                          <span>{formingCandle ? uiText("含當前未收盤 K 線；區間只依已收盤資料計算") : uiText("圖表顯示已收盤 K 線")}</span>
                          <span>
                            {currentMarketData?.history.closed_candle_count ?? 0}{" " + uiText("根作為計算依據 · 最後收盤")}{" "}
                            {when(candles.at(-1)?.close_time)}
                          </span>
                        </div>
                      </section>
                      <section className="panel">
                        <div className="panel-head">
                          <div>
                            <h2>{uiText("支撐與壓力")}</h2>
                          </div>
                          <span className="tag">{uiText("v3 · 即時區間")}</span>
                        </div>
                        {quote && (
                          <p className="level-reference">{uiText("以現價 $")}{fmt(quote.price)}{" " + uiText("判斷區間位置 ·") + " "}{when(quote.observed_at)}{" "}{uiText("更新。 區間包含上下邊界；盤中穿越不代表已確認突破。")}</p>
                        )}
                        {visibleLevels.length ? (
                          levelLadder(visibleLevels, quote?.price).map((row, i) => row.kind === "now" ? (
                            <div className="level-now" key="now"><span>{uiText("現價")}</span><b>${fmt(row.price)}</b></div>
                          ) : (
                            <div className={`level ladder${row.level.price_relation === "inside" ? " level-inside" : ""}`}
                              data-relation={row.level.price_relation} key={row.level.id ?? `${row.level.kind}-${row.level.low}-${i}`}>
                              <i className={row.level.kind} />
                              <strong>
                                {row.level.kind === "support"
                                  ? uiText("支撐區")
                                  : uiText("壓力區")}
                              </strong>
                              <b>
                                ${fmt(row.level.low)} – ${fmt(row.level.high)}
                              </b>
                              <small>
                                {levelLocationLabel(row.level) &&
                                  `${levelLocationLabel(row.level)} · `}
                                {when(row.level.confirmed_at)}{" "}{uiText("確認")}{row.level.algorithm_version ===
                                "confirmed_pivot_lifecycle_v3"
                                  ? " " + uiText("· {{p0}} 次確認轉折 · {{p1}} 次獨立觸及", { p0: row.level.pivot_count, p1: row.level.independent_touch_count })
                                  : row.level.touch_count != null
                                    ? " " + uiText("· {{p0}} 次轉折 · 轉折 K 線相對量 {{p1}}×{{p2}}", { p0: row.level.touch_count, p1: row.level.relative_pivot_volume, p2: row.level.order_book_snapshot_overlap ? " " + uiText("· 與掛單快照重疊") : "" })
                                    : " " + uiText("· 版本未標示")}
                              </small>
                            </div>
                          ))
                        ) : (
                          <div className="placeholder">
                            {marketLoading ? uiText("正在計算支撐與壓力區…") : marketError ? uiText("行情更新失敗，無法確認目前區間。") : uiText("目前資料沒有符合 v3 條件的有效區間。")}
                          </div>
                        )}
                        {currentMarketData && (
                          <p className="level-reference">{uiText("須連續兩根已收盤 K 線越過原區間外緣才標示失效。支撐／壓力保留原角色，不因現價穿越就換名或隱藏。")}{report && " " + uiText("此處為目前行情；上方 AI 報告保留分析當時的資料。")}
                          </p>
                        )}
                        {!!currentMarketData?.recently_invalidated_levels?.length && (
                          <details className="compact-details invalidated-levels">
                            <summary>{uiText("已收盤穿越的原區間（")}{" "}{currentMarketData.recently_invalidated_levels.length}）</summary>
                            <p>{uiText("以下已由兩根收盤穿越確認失效，僅保留作行情背景，不當作有效支撐或壓力。")}</p>
                            {currentMarketData.recently_invalidated_levels.map((level) => (
                              <div className="level" key={level.id ?? `${level.kind}-${level.low}`}>
                                <div><strong>{uiText("原")}{" "}{level.kind === "support" ? uiText("支撐") : uiText("壓力")}</strong><small>{levelLocationLabel(level)}{" " + uiText("· 已收盤穿越")}</small></div>
                                <b>${fmt(level.low)} – ${fmt(level.high)}</b>
                              </div>
                            ))}
                          </details>
                        )}
                      </section>
                    </div>
                  </details>
                </div>
              </div>
              <div className="analysis-secondary">
                {report && (
                  <details className="secondary-evidence">
                    <summary>{uiText("查看本次分析的新聞來源覆蓋")}</summary>
                    <NewsPanel context={report.news_context} />
                  </details>
                )}{" "}
                {report?.market_reference && (
                  <details className="secondary-evidence">
                    <summary>{uiText("BTC／ETH 大盤參考")}</summary>
                    <MarketReference data={report.market_reference} />
                  </details>
                )}{" "}
                {report?.fund_flows_context && (
                  <details className="secondary-evidence">
                    <summary>{uiText("資金流向")}</summary>
                    <FundFlowsContext data={report.fund_flows_context} />
                  </details>
                )}{" "}
                {report && (
                  <details className="secondary-evidence">
                    <summary>{uiText("衍生品市場背景")}</summary>
                    <DerivativesContext data={report.derivatives_context} />
                  </details>
                )}{" "}
                {report && (
                  <details className="panel scenarios">
                    <summary>{uiText("查看策略候選與限制（僅供參考，AI 判斷以上方分析為準）")}</summary>
                    <div className="strategy-grid">
                      {report.strategies.map((s, i) => (
                        <StrategyCard strategy={s} key={s.id ?? i} />
                      ))}
                    </div>
                    <div className="limits">
                      {report.limitations.map((x, i) => (
                        <span key={i}>· {pythonReferenceText(x, { origin: "python" })}</span>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            </>
          )}
          {view === "positions" && (
            <>
              <div className="page-title">
                <div>
                  <h1>{uiText("我的持倉")}</h1>
                  <p>{uiText("同步或記錄部位，選擇持倉，取得續抱或平倉的建議。")}</p>
                </div>
              </div>
              {marketControls}
              <ExchangeSyncPanel
                onSynced={refreshPositions}
                onOpenSettings={() => setView("settings")}
              />
              <div className="positions-grid">
                <section className="panel current-positions">
                  <div className="panel-head">
                    <div>
                      <h2>{uiText("目前持倉")}</h2>
                    </div>
                    <div className="current-positions-actions">
                      <span className="tag">
                        {uiText("positionCount", { count: marketPositions.length })}</span>
                      <button type="button" className="secondary-button add-position-button" onClick={openManualPosition}>
                        <Icon name="plus" />{uiText("新增持倉")}
                      </button>
                    </div>
                  </div>
                  {!!marketPositions.length && <div className="position-selection">
                    <div className="position-selection-actions">
                      <button type="button" className="secondary-button"
                        disabled={busy || !marketPositions.length || allPositionsSelected}
                        onClick={() => setSelected(marketPositions.map((position) => position.id))}>
                        {uiText("全選持倉")}
                      </button>
                      <button type="button" className="secondary-button"
                        disabled={busy || !selectedPositionIds.length}
                        onClick={() => setSelected([])}>
                        {uiText("清除選取")}
                      </button>
                    </div>
                    <span className="position-selection-count" role="status">
                      {uiText("已選 {{p0}}／{{p1}} 筆", { p0: selectedPositionIds.length, p1: marketPositions.length })}
                    </span>
                  </div>}
                  {marketPositions.map((p) => (
                      <div className="position" key={p.id}>
                        <label>
                          <input
                            type="checkbox"
                            checked={selected.includes(p.id)}
                            disabled={busy}
                            onChange={(e) => {
                              const checked = e.target.checked;
                              setSelected((current) => checked
                                ? [...new Set([...current, p.id])]
                                : current.filter((id) => id !== p.id));
                            }}
                          />
                          <span className="position-body">
                            <span className="position-title">
                              <b>{market?.symbol}</b>
                              <em className="position-side" data-side={p.side}>{p.side === "long" ? uiText("多單") : uiText("空單")}</em>
                              <em className="position-chip">{p.leverage}× {p.margin_mode === "isolated" ? uiText("逐倉") : uiText("全倉")}</em>
                              <small>{positionSourceLabel(p.source, p.contract_type)}</small>
                            </span>
                            <dl className="position-facts">
                              <div><dt>{uiText("進場")}</dt><dd>${fmt(p.entry_price)}</dd></div>
                              <div><dt>{uiText("數量")}</dt><dd>{p.quantity}</dd></div>
                              {/* Some exchange APIs (BingX standard contracts) never report stops, so an
                                  imported position without one says so instead of claiming none was set. */}
                              <div><dt>{uiText("止損")}</dt><dd>{p.stop_loss ? "$" + fmt(p.stop_loss) : <span>{isManualPosition(p.source) ? uiText("未設定") : uiText("交易所未提供")}</span>}</dd></div>
                              <div><dt>{uiText("止盈")}</dt><dd>{p.take_profit ? "$" + fmt(p.take_profit) : <span>{isManualPosition(p.source) ? uiText("未設定") : uiText("交易所未提供")}</span>}</dd></div>
                              {p.exchange_liquidation_price && (
                                <div><dt>{liquidationSourceLabel(p.source)}</dt><dd>${fmt(p.exchange_liquidation_price)}</dd></div>
                              )}
                            </dl>
                            {(p.entry_time || p.synced_at) && (
                              <small className="position-meta">
                                {p.entry_time && uiText("進場") + " " + when(p.entry_time)}
                                {p.entry_time && p.synced_at && " · "}
                                {p.synced_at && uiText("交易所同步") + " " + when(p.synced_at)}
                              </small>
                            )}
                            {p.notes && <small className="position-meta">{uiText("備註")}{" · "}{p.notes}</small>}
                          </span>
                        </label>
                        <div className="editor-actions">
                          <button
                            onClick={() => {
                              setEditFocus(!isManualPosition(p.source) && !p.stop_loss ? "stop_loss" : null);
                              setEditingId(p.id);
                              setEditForm({
                                side: p.side,
                                leverage: p.leverage,
                                margin_mode: p.margin_mode,
                                entry_price: p.entry_price,
                                quantity: p.quantity,
                                stop_loss: p.stop_loss ?? "",
                                take_profit: p.take_profit ?? "",
                                entry_time: localDateTimeInput(p.entry_time),
                                exchange_liquidation_price:
                                  p.exchange_liquidation_price ?? "",
                                notes: p.notes ?? "",
                              });
                            }}
                          >{!isManualPosition(p.source) && !p.stop_loss ? uiText("補登止損") : uiText("修改")}</button>
                          {isManualPosition(p.source) && (
                            <button onClick={() => closePosition(p.id)}>{uiText("標記平倉")}</button>
                          )}
                          <button onClick={() => deletePosition(p.id)}>{uiText("刪除紀錄")}</button>
                        </div>
                      </div>
                    ))}
                  {editingId && (
                    <EditPositionForm
                      exchangeSynced={!isManualPosition(positions.find((p) => p.id === editingId)?.source)}
                      value={editForm}
                      onChange={setEditForm}
                      onSubmit={savePosition}
                      onCancel={() => setEditingId(null)}
                    />
                  )}{" "}
                  {!marketPositions.length && (
                    <div className="positions-empty">
                      <p>{positions.length ? uiText("此歷史報告的持倉已不在目前持倉清單。") : uiText("尚無持倉。可同步帳戶或手動新增持倉。")}</p>
                      <button type="button" className="action" onClick={openManualPosition}>
                        <Icon name="plus" />{uiText("手動新增持倉")}
                      </button>
                    </div>
                  )}
                  <RiskSelector value={risk} onChange={chooseRisk} />
                  <TradingStyleSelector
                    value={tradingStyle}
                    onChange={chooseTradingStyle}
                  />
                  <label className="equity-field">{uiText("本次帳戶權益（選填，USDT）")}<input
                      inputMode="decimal"
                      value={accountEquity}
                      onChange={(e) => setAccountEquity(e.target.value)}
                      placeholder={uiText("用於估算止損價格曝險比例")}
                    />
                  </label>
                  <button
                    className="action"
                    disabled={!selectedPositionIds.length || busy || !analysisLeverageValid}
                    aria-busy={positionsBusy}
                    onClick={() => analyze("positions")}
                  >
                    {busy ? busyLabel : uiText("分析已選持倉（{{p0}}）", { p0: selectedPositionIds.length })}
                  </button>
                  {report && job?.submitted_input.kind === "positions" && (
                    <a
                      className="position-report-link"
                      href="#position-analysis-report"
                    >{uiText("查看本次分析報告")}</a>
                  )}{" "}
                  {positionsBusy && (
                    <div className="position-analysis-progress">
                      <AnalysisProgress
                        kind="positions"
                        phase={analysisPhase}
                        progress={pendingJob?.progress}
                        outputLocale={pendingJob?.submitted_input.output_locale}
                      />
                    </div>
                  )}
                  {!positionsBusy &&
                    job?.submitted_input.kind === "positions" &&
                    job.status === "failed" && (
                      <AnalysisFailure
                        error={job.error}
                        jobId={job.id}
                        market={market?.symbol ?? marketId}
                        timeframe={job.submitted_input.timeframe}
                        kind="positions"
                      />
                    )}{" "}
                  {job?.freshness === "stale" &&
                    job.submitted_input.kind === "positions" && (
                      <div className="warning">{uiText("持倉已變更，舊報告反映修改前版本，請重新分析。")}</div>
                    )}{" "}
                  {report?.market_id === marketId &&
                    job?.submitted_input.kind === "positions" && (
                      <div className="note">{uiText("本次風險傾向：")}{" "}{report.preference_assessment.risk_tolerance === "high"
                          ? uiText("高")
                          : report.preference_assessment.risk_tolerance ===
                              "medium"
                            ? uiText("中")
                            : report.preference_assessment.risk_tolerance ===
                                "low"
                              ? uiText("低")
                              : uiText("未設定")}{uiText("；進場風格：")}{" "}{report.preference_assessment.trading_style === "left"
                          ? uiText("左側")
                          : report.preference_assessment.trading_style ===
                              "right"
                            ? uiText("右側")
                            : uiText("未設定")}{uiText("。AI 依市場證據判斷是否續抱；風險估算僅供參考。")}</div>
                    )}{" "}
                  {report?.market_id === marketId &&
                    job?.submitted_input.kind === "positions" &&
                    report.account_equity_usdt && (
                      <div className="note">{uiText("本次手動填寫帳戶權益 $")}{fmt(report.account_equity_usdt)}{" "}{uiText("USDT；比例只計算參考價到登記止損的價格距離，未含費用、滑價與其他持倉。")}</div>
                    )}
                </section>
                <details className="panel manual-position" ref={manualPositionRef}>
                  <summary>{uiText("手動新增持倉")}</summary>
                  <form className="position-form" onSubmit={addPosition}>
                    <MarketPicker
                      label={uiText("新增持倉交易對")}
                      markets={markets}
                      value={manualMarketId}
                      onChange={setNewPositionMarketId}
                      favoriteIds={marketFavorites.favoriteIds}
                      onToggleFavorite={marketFavorites.toggleFavorite}
                      favoritesReady={marketFavorites.ready}
                      favoritesSaving={marketFavorites.saving}
                      favoritesError={marketFavorites.error}
                    />
                    <div className="position-fields">
                      <label>{uiText("方向")}<SelectControl
                          value={form.side}
                          onChange={(e) =>
                            setForm({
                              ...form,
                              side: e.target.value as "long" | "short",
                            })
                          }
                        >
                          <option value="long">{uiText("做多")}</option>
                          <option value="short">{uiText("做空")}</option>
                        </SelectControl>
                      </label>
                      <LeverageControl
                        label={uiText("槓桿")}
                        value={form.leverage}
                        onChange={(next) => setForm({ ...form, leverage: next })}
                      />
                      <label>{uiText("保證金模式")}<SelectControl
                          value={form.margin_mode}
                          onChange={(e) =>
                            setForm({
                              ...form,
                              margin_mode: e.target.value as
                                "isolated" | "cross",
                            })
                          }
                        >
                          <option value="isolated">{uiText("逐倉")}</option>
                          <option value="cross">{uiText("全倉")}</option>
                        </SelectControl>
                      </label>
                    </div>
                    {(
                      [
                        "entry_price",
                        "quantity",
                        "stop_loss",
                        "take_profit",
                      ] as const
                    ).map((key, i) => (
                      <label key={key}>
                        {
                          [
                            uiText("平均進場價（USDT）"),
                            uiText("持有數量（幣）"),
                            uiText("止損價（選填）"),
                            uiText("止盈價（選填）"),
                          ][i]
                        }
                        <input
                          required={i < 2}
                          inputMode="decimal"
                          value={form[key]}
                          onChange={(e) =>
                            setForm({ ...form, [key]: e.target.value })
                          }
                        />
                      </label>
                    ))}
                    <details className="form-details">
                      <summary>{uiText("更多資料（進場時間、強平價、備註）")}</summary>
                      <PositionOptionalFields
                        value={form}
                        onChange={(patch) => setForm({ ...form, ...patch })}
                      />
                    </details>
                    <button className="action" disabled={!manualMarketId || !markets.length}>{uiText("新增持倉")}</button>
                  </form>
                  <div className="note">{uiText("手動登記，未連接交易帳戶。保證金與報酬率僅為理論估算，不等於強平價。")}</div>
                </details>
              </div>
              <div
                className="position-analysis-results"
                id="position-analysis-report"
              >
                {!viewingHistoricalPosition && latestPositionAnalysis.status === "loading" && (
                  <div className="market-context-status" role="status" aria-busy="true">
                    <AnalysisSpinner />
                    <span>{uiText("正在載入此交易對的最新持倉分析…")}</span>
                  </div>
                )}
                {!viewingHistoricalPosition && latestPositionAnalysis.status === "empty" && (
                  <p className="note" role="status">{uiText("此交易對尚無已完成的持倉分析。選擇持倉後開始分析。")}</p>
                )}
                {!viewingHistoricalPosition && latestPositionAnalysis.status === "error" && (
                  <div className="market-context-status" role="alert" data-error="true">
                    <span>{latestPositionAnalysis.error}</span>
                    <button type="button" className="settings-link" onClick={latestPositionAnalysis.refresh}>{uiText("重試")}</button>
                  </div>
                )}
                {report && job?.submitted_input.kind === "positions" && (
                  <div className="position-result-heading">
                    <h2>{uiText("AI 持倉建議")}</h2>
                    <time dateTime={report.generated_at}>
                      {when(report.generated_at)}{" "}{uiText("· 分析快照")}</time>
                  </div>
                )}{" "}
                {report &&
                  job?.submitted_input.kind === "positions" &&
                  (changed || job.freshness === "stale") && (
                    <div className="warning" role="status">{uiText("設定或持倉已變更，下方為上次分析，請重新分析。")}</div>
                  )}{" "}
                {report?.market_id === marketId &&
                  job?.submitted_input.kind === "positions" &&
                  report.position_reviews.map((r) => (
                    <PositionReviewCard key={r.position_id} review={r} timeframe={report.timeframe}
                      outcome={shownOutcomes.find((item) => item.item_key === `position:${r.position_id}`)} riskTolerance={report.preference_assessment?.risk_tolerance} outputLocale={report.response_locale ?? report.output_locale ?? job?.submitted_input.output_locale ?? "zh-TW"} />
                  ))}
                {report?.market_id === marketId &&
                  job?.submitted_input.kind === "positions" && (
                    <PositionLevels
                      levels={report.metrics.levels}
                      quotePrice={report.quote.price}
                      marketId={report.market_id}
                      analysisId={job.id}
                      snapshotHash={report.market_snapshot_sha256}
                      chartSnapshot={currentPositionChart?.data}
                      chartLoading={!!positionChartId && !currentPositionChart}
                      chartError={currentPositionChart?.error}
                      onRetryChart={() => setPositionChartRefresh((value) => value + 1)}
                      timeframe={report.timeframe}
                      observedAt={report.quote.observed_at}
                      algorithmVersion={report.metrics.level_algorithm_version}
                      indicators={chartIndicators}
                    />
                  )}{" "}
                {report && job?.submitted_input.kind === "positions" && (
                  <>
                    <DirectionAssessment assessment={report.reasoning?.direction_assessment}
                      bias={report.preference_assessment?.directional_bias} />
                    <AnalysisEvidence
                        outputLocale={report.response_locale ?? report.output_locale ?? job?.submitted_input.output_locale ?? "zh-TW"}
                      positionAnalysis
                      mode={report.analysis_mode}
                      reasoning={report.reasoning}
                      runs={report.tool_trace}
                      fallbackReason={report.fallback_reason}
                      entryDecision={report.entry_decision}
                      riskTolerance={report.preference_assessment?.risk_tolerance}
                      entryRiskReference={report.entry_risk_reference}
                      analysisLeverage={report.analysis_leverage}
                      macroContext={report.macro_context}
                      macroInterpretation={report.macro_interpretation}
                      currentCandle={report.current_candle}
                      snapshotObservedAt={report.quote.observed_at}
                      generatedAt={report.generated_at}
                      execution={report.analysis_execution}
                    />
                    <details className="secondary-evidence">
                      <summary>{uiText("查看本次分析的新聞來源覆蓋")}</summary>
                      <NewsPanel context={report.news_context} />
                    </details>
                    {report.market_reference && (
                      <details className="secondary-evidence">
                        <summary>{uiText("BTC／ETH 大盤參考")}</summary>
                        <MarketReference data={report.market_reference} />
                      </details>
                    )}
                    {report.fund_flows_context && (
                      <details className="secondary-evidence">
                        <summary>{uiText("資金流向")}</summary>
                        <FundFlowsContext data={report.fund_flows_context} />
                      </details>
                    )}
                    <details className="secondary-evidence">
                      <summary>{uiText("衍生品市場背景")}</summary>
                      <DerivativesContext data={report.derivatives_context} />
                    </details>
                  </>
                )}
              </div>
            </>
          )}
          {mergedMarkets && isMarketsPage && (
            <div className="segments markets-switch" role="group" aria-label={uiText("市場動態")}>
              {(["events", "smartMoney"] as const).map((id) => (
                <button key={id} type="button" aria-pressed={view === id}
                  className={view === id ? "chosen" : ""} onClick={() => openPage(id)}>
                  {pages.find((page) => page.id === id)?.label}
                </button>
              ))}
            </div>
          )}
          {view === "smartMoney" && <SmartMoneyPanel marketId={marketId} locale={locale}
            conversation={flowSnapshot} onStartConversation={startFlowConversation} />}
          {view === "events" && (
            <>
              <div className="page-title">
                <div>
                  <h1>{uiText("美國經濟事件")}</h1>
                  <p>{uiText("追蹤官方實際值與公布日程，也是 Agent 判斷宏觀方向的依據。")}</p>
                </div>
                <button
                  className="secondary-button"
                  disabled={eventsLoading}
                  onClick={refreshEvents}
                >
                  <Icon name="refresh" />
                  {eventsLoading ? uiText("讀取中…") : uiText("重新整理")}
                </button>
              </div>
              {eventsError && (
                <div className="alert" role="alert">{uiText("經濟資料讀取失敗：")}{" "}{eventsError}{uiText("。請重新整理。")}</div>
              )}{" "}
              {(liveEvents || !eventsError) && (
                <EventPanel context={liveEvents}
                  macroInterpretation={macroInterpretation.data}
                  macroEnsuring={macroInterpretation.ensuring}
                  macroError={macroInterpretation.error}
                  onEnsureMacro={macroInterpretation.retry} onTranslateMacro={macroInterpretation.translate} macroTranslating={macroInterpretation.translating} />
              )}
            </>
          )}{" "}
          {view === "history" && (
            <>
              <div className="page-title">
                <div>
                  <h1>{historyTab === "record" ? uiText("AI 表現") : uiText("分析紀錄")}</h1>
                  <p>{historyTab === "record"
                    ? uiText("App 會在背景用之後的行情，自動判斷每份報告的結果。")
                    : uiText("回看當時的行情、交易設定與決策理由。")}</p>
                </div>
              </div>
              <div className="segments history-switch" role="group" aria-label={uiText("紀錄與表現")}>
                {(["record", "analyses"] as const).map((tab) => (
                  <button key={tab} type="button" aria-pressed={historyTab === tab}
                    className={historyTab === tab ? "chosen" : ""} onClick={() => setHistoryTab(tab)}>
                    {tab === "record" ? uiText("AI 表現") : uiText("分析紀錄")}
                  </button>
                ))}
              </div>
              {historyTab === "record" && (
                <TrackRecord onOpenAnalysis={(id) => {
                  api<Job>(`/analyses/${id}`).then(openAnalysis).catch((e) => setError(e.message));
                }} />
              )}
              {historyTab === "analyses" && <section className="panel">
                <div className="panel-head">
                  <h2>{uiText("近期分析")}</h2>
                  <span className="tag">{uiText("最近 50 筆")}</span>
                </div>
                {history.map((item) => (
                  <button
                    className="history-row"
                    key={item.id}
                    onClick={() => openAnalysis(item)}
                  >
                    <span>
                      <b>
                        {markets.find(
                          (m) => m.id === item.submitted_input.market_id,
                        )?.symbol ??
                          item.submitted_input.market_id.split(":").at(-1)}{" "}
                        · {item.submitted_input.timeframe}
                      </b>
                      <small>{when(item.created_at)}</small>
                    </span>
                    <span>
                      {item.status === "completed"
                        ? uiText("已完成")
                        : item.status === "failed"
                          ? uiText("失敗")
                          : uiText("進行中")}
                    </span>
                    <Icon name="arrow" />
                  </button>
                ))}{" "}
                {!history.length && (
                  <div className="placeholder big">{uiText("還沒有分析紀錄。")}</div>
                )}
              </section>}
            </>
          )}
        </div>
      </main>
      <DiscussionSidebar
        subject={discussionSubject}
        available={view === "market" || view === "positions" || view === "events" || view === "smartMoney"}
      />
    </div>
  );
}

export default App;
