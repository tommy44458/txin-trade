import { uiText } from "./i18n/index.ts";
import { applyUiTheme, normalizeUiTheme, type UiTheme } from "./uiTheme.ts";
import { apiFetch, isRemoteMode } from "./transport.ts";
export type ModelProvider = "codex" | "claude_code" | "openai" | "anthropic" | "chatgpt_plan";
export type InitialIndicatorCatalogItem = {
  name: string;
  tool: string;
  parameters: Record<string, number | string>;
};
export type IntegrationStatus = {
  configured: boolean;
  needs_verification?: boolean;
  needs_reentry?: boolean;
  needs_migration?: boolean;
};
export class SettingsRequestError extends Error {
  readonly code: string | null;
  readonly exchangeCode: number | null;
  readonly retryAfter: number | null;
  constructor(message: string, detail?: { code?: unknown; exchange_code?: unknown; retry_after?: unknown }) {
    super(message);
    this.name = "SettingsRequestError";
    this.code = typeof detail?.code === "string" && /^[A-Z0-9_]{1,80}$/.test(detail.code) ? detail.code : null;
    this.exchangeCode = typeof detail?.exchange_code === "number" && Number.isInteger(detail.exchange_code) ? detail.exchange_code : null;
    this.retryAfter = typeof detail?.retry_after === "number" && Number.isInteger(detail.retry_after) && detail.retry_after >= 0 ? detail.retry_after : null;
  }
}
export type TradingPreferences = {
  directional_bias: "bullish" | "bearish" | null;
  risk_tolerance: "high" | "medium" | "low" | null;
  trading_style: "left" | "right" | null;
  timeframe: AnalysisTimeframe;
  leverage: number;
  market_id: string | null;
};
export type LocalSettings = {
  ui_locale?: import("./i18n/index.ts").UiLocale;
  ui_theme?: UiTheme;
  model_provider: ModelProvider;
  model: string;
  desktop: boolean;
  favorite_market_ids?: string[];
  initial_indicators?: string[];
  initial_indicator_catalog?: InitialIndicatorCatalogItem[];
  trading_preferences?: TradingPreferences;
  // Opt-in sharing of reconciled outcomes with the txinTrade cloud.
  share_outcomes?: boolean;
  integrations: {
    bingx: IntegrationStatus;
    binance?: IntegrationStatus;
    jev: IntegrationStatus;
    jblanked?: IntegrationStatus;
    openai?: IntegrationStatus;
    anthropic?: IntegrationStatus;
  };
};
export type ModelAuthStatus = {
  authenticated: boolean;
  needs_reentry?: boolean;
  status_known?: boolean;
  email?: string | null;
  plan?: string | null;
  cli_installed?: boolean;
  error?: string | null;
  login_pending?: boolean;
};

export async function settingsRequest<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await apiFetch(`/api/v1${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw new SettingsRequestError(
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : uiText("設定請求失敗（{{p0}}）", { p0: response.status }),
      typeof detail === "object" && detail ? detail : undefined,
    );
  }
  // The remote page keeps its own appearance; only the computer follows its saved theme.
  if (path === "/settings" && body && typeof body === "object" && !isRemoteMode()) {
    applyUiTheme(normalizeUiTheme(body.ui_theme));
  }
  return body as T;
}

// Providers added in 1.0.9. A computer on 1.0.11 or earlier refuses the remote
// sign-in check for them, so the remote page lets the analysis itself report it.
const PROVIDERS_NEWER_THAN_RELAY = new Set(["chatgpt_plan", "anthropic", "openai"]);

export async function readModelConnection() {
  const settings = await settingsRequest<LocalSettings>("/settings");
  let status: ModelAuthStatus;
  try {
    status = await settingsRequest<ModelAuthStatus>(
      `/auth/${settings.model_provider}/check`,
      { method: "POST" },
    );
  } catch (reason) {
    if (!isRemoteMode() || !PROVIDERS_NEWER_THAN_RELAY.has(settings.model_provider)) throw reason;
    // The computer checks the AI connection again when the analysis starts.
    return { settings, error: null, ready: true };
  }
  return {
    settings,
    error: status.error ?? null,
    ready: status.authenticated,
  };
}
import type { AnalysisTimeframe } from "./timeframes";
