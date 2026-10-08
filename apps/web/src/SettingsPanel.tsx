import { uiText, uiLocale, useUiLocale, setUiLocale, isUiLocale, type UiLocale } from "./i18n/index.ts";
import CloudAccountSection from "./CloudAccountSection";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { AnalysisSpinner } from "./AnalysisProgress";
import SelectControl from "./SelectControl";
import BinanceSettingsPanel from "./BinanceSettingsPanel";
import CliSetupDialog, { type CliSetupReason } from "./CliSetupDialog";
import OutcomeSharingSection from "./OutcomeSharingSection";
import WebSearchSection from "./WebSearchSection";
import { openAuthorization } from "./desktop";
import { applyUiTheme, currentUiTheme, isUiTheme, normalizeUiTheme, type UiTheme } from "./uiTheme";
import { isRemoteMode } from "./transport.ts";
import { rememberBrowserPreference } from "./browserPreferences.ts";
import {
  initialIndicatorCatalog,
  initialIndicatorCopy,
  initialIndicatorParameterBadges,
  normalizeInitialIndicators,
  sameInitialIndicators,
} from "./initialIndicators";
import {
  settingsRequest,
  type ModelProvider,
  type LocalSettings,
  type ModelAuthStatus,
  type IntegrationStatus,
} from "./localSettings";
import "./SettingsPanel.css";
type Model = { id: string; name: string };
const needsReentry = (state?: IntegrationStatus) => Boolean(state?.needs_reentry || state?.needs_migration);
type ModelCatalog =
  | Model[]
  | { data: { id: string; model?: string; displayName?: string }[] };

function normalizeModels(value: ModelCatalog): Model[] {
  return Array.isArray(value)
    ? value
    : value.data.map((item) => ({
        id: item.model || item.id,
        name: item.displayName || item.model || item.id,
      }));
}
type SettingsUpdate = {
  model_provider?: ModelProvider;
  ui_locale?: UiLocale;
  ui_theme?: UiTheme;
  model?: string;
  bingx_api_key?: string;
  bingx_api_secret?: string;
  jev_api_key?: string;
  jblanked_api_key?: string;
  openai_api_key?: string;
  anthropic_api_key?: string;
  clear_bingx?: boolean;
  clear_jev?: boolean;
  clear_jblanked?: boolean;
  clear_openai?: boolean;
  clear_anthropic?: boolean;
};

/** On the remote page, show this browser's own appearance and language, not the computer's. */
function shownSettings(value: LocalSettings): LocalSettings {
  return isRemoteMode() ? { ...value, ui_theme: currentUiTheme(), ui_locale: uiLocale() } : value;
}

export default function SettingsPanel({
  initialSettings,
  onChanged,
  onCloudAccountChanged,
  remoteSection,
}: {
  initialSettings: LocalSettings | null;
  onChanged: () => Promise<void>;
  onCloudAccountChanged: () => void;
  /** On the remote page: the remote connection controls, shown first. */
  remoteSection?: ReactNode;
}) {
  const locale = useUiLocale();
  // Remote screens show only what is safe and meaningful away from the computer.
  const remote = isRemoteMode();
  const [settings, setSettings] = useState<LocalSettings | null>(
    initialSettings,
  );
  const [provider, setProvider] = useState<ModelProvider>(
    initialSettings?.model_provider ?? "codex",
  );
  const [model, setModel] = useState(initialSettings?.model ?? "");
  const [auth, setAuth] = useState<ModelAuthStatus | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [authUrl, setAuthUrl] = useState("");
  const [userCode, setUserCode] = useState("");
  const [cliSetup, setCliSetup] = useState<CliSetupReason | null>(null);
  const [cliChecking, setCliChecking] = useState(false);
  const [cliError, setCliError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [bingxKey, setBingxKey] = useState("");
  const [bingxSecret, setBingxSecret] = useState("");
  const [jevKey, setJevKey] = useState("");
  const [jblankedKey, setJblankedKey] = useState("");
  const [openaiKey, setOpenaiKey] = useState("");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [pendingTheme, setPendingTheme] = useState<UiTheme | null>(null);
  const [themeError, setThemeError] = useState("");
  const [themeNotice, setThemeNotice] = useState(false);
  const themeSavePending = useRef(false);
  const [initialIndicators, setInitialIndicators] = useState<string[]>(
    normalizeInitialIndicators(initialSettings?.initial_indicators),
  );
  const [savedInitialIndicators, setSavedInitialIndicators] = useState<string[]>(
    normalizeInitialIndicators(initialSettings?.initial_indicators),
  );
  const [indicatorsLoading, setIndicatorsLoading] = useState(true);
  const [indicatorsLoaded, setIndicatorsLoaded] = useState(false);
  const [indicatorsError, setIndicatorsError] = useState("");
  const [indicatorsNotice, setIndicatorsNotice] = useState(false);
  const indicatorSavePending = useRef(false);
  const mounted = useRef(true);
  const changedRef = useRef(onChanged);
  useEffect(() => {
    changedRef.current = onChanged;
  }, [onChanged]);
  useEffect(() => {
    let active = true;
    mounted.current = true;
    settingsRequest<LocalSettings>("/settings")
      .then((value) => {
        if (!active || !mounted.current) return;
        // The remote page keeps its own appearance and language; only the computer's apply here.
        setSettings(shownSettings(value));
        if (!isRemoteMode() && isUiLocale(value.ui_locale)) void setUiLocale(value.ui_locale).catch(() => {});
        setProvider(value.model_provider);
        setModel(value.model);
        const selection = normalizeInitialIndicators(value.initial_indicators);
        setInitialIndicators(selection);
        setSavedInitialIndicators(selection);
        setIndicatorsLoaded(true);
        setIndicatorsLoading(false);
      })
      .catch((reason: Error) => {
        if (active && mounted.current) {
          setError(reason.message);
          setIndicatorsError(reason.message);
          setIndicatorsLoading(false);
        }
      });
    return () => {
      active = false;
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    // The AI account is managed on the computer; the remote page never asks about it.
    if (isRemoteMode()) return;
    settingsRequest<ModelAuthStatus>(`/auth/${provider}/status`)
      .then((value) => {
        if (!active) return;
        setAuth(value);
        setWaiting(!!value.login_pending);
        // Selecting a provider whose CLI is not installed explains what to do.
        if (value.cli_installed === false) openCliSetup("install");
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      });
    return () => {
      active = false;
    };
  }, [provider]);

  useEffect(() => {
    if (!auth?.authenticated || isRemoteMode()) return;
    let active = true;
    settingsRequest<ModelCatalog>(`/auth/${provider}/models`)
      .then((value) => {
        if (!active) return;
        setModels(normalizeModels(value));
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      });
    return () => {
      active = false;
    };
  }, [provider, auth?.authenticated]);

  useEffect(() => {
    if (!waiting || provider === "openai") return;
    let active = true;
    let pending = false;
    const timer = window.setInterval(async () => {
      if (pending) return;
      pending = true;
      try {
        const value = await settingsRequest<ModelAuthStatus>(
          `/auth/${provider}/status`,
        );
        if (!active) return;
        setAuth(value);
        if (!value.login_pending && (value.authenticated || value.error)) {
          setWaiting(false);
          setUserCode("");
          if (value.authenticated) {
            setNotice(uiText("帳號已連線。確認模型後儲存設定即可開始分析。"));
            await changedRef.current();
          } else {
            setError(value.error || uiText("授權未完成，請重新連線"));
          }
        }
      } catch (reason) {
        if (active) {
          setError((reason as Error).message);
          setWaiting(false);
        }
      } finally {
        pending = false;
      }
    }, 1500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [waiting, provider]);

  async function perform(action: string, task: () => Promise<void>) {
    setBusy(action);
    setError("");
    setNotice("");
    try {
      await task();
    } catch (reason) {
      if (mounted.current) setError((reason as Error).message);
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function update(values: SettingsUpdate) {
    const next = await settingsRequest<LocalSettings>("/settings", {
      method: "PATCH",
      body: JSON.stringify(values),
    });
    setSettings(shownSettings(next));
    await changedRef.current();
  }

  async function saveTheme(selected: UiTheme) {
    if (themeSavePending.current || busy || !indicatorsLoaded
        || selected === normalizeUiTheme(settings?.ui_theme)) return;
    if (remote) {
      applyUiTheme(selected);
      rememberBrowserPreference("theme", selected);
      setSettings((current) => current && { ...current, ui_theme: selected });
      setThemeNotice(true);
      return;
    }
    themeSavePending.current = true;
    setPendingTheme(selected);
    setBusy("theme");
    setThemeError("");
    setThemeNotice(false);
    try {
      const next = await settingsRequest<LocalSettings>("/settings", {
        method: "PATCH", body: JSON.stringify({ ui_theme: selected }),
      });
      if (!mounted.current) return;
      setSettings(shownSettings(next));
      setThemeNotice(true);
      try {
        await changedRef.current();
      } catch {
        if (mounted.current) setThemeError(uiText("外觀已儲存，但畫面更新未完成。請重新開啟設定確認。"));
      }
    } catch (reason) {
      if (mounted.current) setThemeError(uiText("外觀未儲存：{{p0}}", { p0: (reason as Error).message }));
    } finally {
      themeSavePending.current = false;
      if (mounted.current) {
        setPendingTheme(null);
        setBusy(null);
      }
    }
  }

  async function reloadIndicators() {
    if (indicatorsLoading || busy) return;
    setIndicatorsLoading(true);
    setIndicatorsError("");
    setIndicatorsNotice(false);
    try {
      const next = await settingsRequest<LocalSettings>("/settings");
      if (!mounted.current) return;
      const selection = normalizeInitialIndicators(next.initial_indicators);
      setSettings(shownSettings(next));
      setInitialIndicators(selection);
      setSavedInitialIndicators(selection);
      setIndicatorsLoaded(true);
    } catch (reason) {
      if (mounted.current) setIndicatorsError((reason as Error).message);
    } finally {
      if (mounted.current) setIndicatorsLoading(false);
    }
  }

  async function saveIndicators() {
    if (indicatorSavePending.current || busy || !indicatorsLoaded || indicatorsLoading
        || sameInitialIndicators(initialIndicators, savedInitialIndicators)) return;
    indicatorSavePending.current = true;
    setBusy("indicators");
    setIndicatorsError("");
    setIndicatorsNotice(false);
    try {
      const next = await settingsRequest<LocalSettings>("/settings", {
        method: "PATCH",
        body: JSON.stringify({ initial_indicators: normalizeInitialIndicators(initialIndicators) }),
      });
      if (!mounted.current) return;
      const selection = normalizeInitialIndicators(next.initial_indicators);
      setSettings(shownSettings(next));
      setInitialIndicators(selection);
      setSavedInitialIndicators(selection);
      setIndicatorsNotice(true);
      try {
        await changedRef.current();
      } catch {
        if (mounted.current) setIndicatorsError(uiText("指標已儲存，但畫面更新未完成。請重新開啟設定確認。"));
      }
    } catch (reason) {
      if (mounted.current) setIndicatorsError((reason as Error).message);
    } finally {
      indicatorSavePending.current = false;
      if (mounted.current) setBusy(null);
    }
  }

  function changeIndicators(selection: string[]) {
    setInitialIndicators(normalizeInitialIndicators(selection));
    setIndicatorsError("");
    setIndicatorsNotice(false);
  }

  function openCliSetup(reason: CliSetupReason) {
    setCliError("");
    setCliSetup(reason);
  }

  async function recheckCli() {
    if (provider === "openai") return;
    setCliChecking(true);
    setCliError("");
    try {
      const value = await settingsRequest<ModelAuthStatus>(`/auth/${provider}/check`, { method: "POST" });
      if (!mounted.current) return;
      setAuth(value);
      if (value.cli_installed === false) {
        setCliSetup("install");
        setCliError(provider === "claude_code"
          ? uiText("仍未偵測到 Claude Code CLI。安裝完成後，若剛修改過 PATH，請重新開啟 txinTrade。")
          : uiText("仍未偵測到 Codex CLI。安裝完成後，若剛修改過 PATH，請重新開啟 txinTrade。"));
      } else if (value.authenticated) {
        setCliSetup(null);
        setNotice(uiText("帳號已連線。"));
        await changedRef.current();
      } else if (provider === "claude_code") {
        if (cliSetup === "signin") setCliError(uiText("Claude Code 仍未登入，請先完成 claude auth login。"));
        setCliSetup("signin");
      } else {
        setCliSetup(null);
        setNotice(provider === "chatgpt_plan" ? uiText("已偵測到 Codex CLI，請按「連線 ChatGPT」完成授權。")
          : uiText("已偵測到 Codex CLI，請按「連線 Codex」完成登入。"));
      }
    } catch (reason) {
      if (mounted.current) setCliError((reason as Error).message);
    } finally {
      if (mounted.current) setCliChecking(false);
    }
  }

  function login() {
    void perform("login", async () => {
      const result = await settingsRequest<{
        auth_url?: string;
        verification_url?: string;
        user_code?: string;
        status?: ModelAuthStatus | string;
      }>(`/auth/${provider}/login`, { method: "POST", body: "{}" });
      const url = result.auth_url || result.verification_url || "";
      setAuthUrl(url);
      setUserCode(result.user_code || "");
      if (typeof result.status === "object") setAuth(result.status);
      if (typeof result.status === "object" && result.status.cli_installed === false) {
        openCliSetup("install");
        return;
      }
      if (
        typeof result.status === "object" &&
        result.status.authenticated &&
        !result.status.login_pending
      ) {
        setNotice(uiText("帳號已連線。"));
        await changedRef.current();
        return;
      }
      if (provider === "anthropic" || provider === "openai") {
        // There is no sign-in: the saved key is checked against the API.
        if (typeof result.status === "object" && result.status.error) setError(result.status.error);
        return;
      }
      if (provider === "claude_code") {
        // Claude Code keeps its own sign-in; this app only binds to it.
        openCliSetup("signin");
        return;
      }
      setWaiting(true);
      if (url) await openAuthorization(url);
    });
  }

  function authAction(action: "cancel" | "logout") {
    void perform(action, async () => {
      await settingsRequest(`/auth/${provider}/${action}`, { method: "POST" });
      setWaiting(false);
      setAuthUrl("");
      setUserCode("");
      setAuth(
        await settingsRequest<ModelAuthStatus>(`/auth/${provider}/status`),
      );
      await changedRef.current();
      setNotice(action === "cancel" ? uiText("已取消這次授權。")
        : provider === "claude_code" ? uiText("已解除 Claude Code 連線；Claude Code 本身的登入不受影響。")
          : uiText("已登出此帳號。"));
    });
  }

  const modelOptions = models.some((item) => item.id === model)
    ? models
    : model
      ? [{ id: model, name: model }, ...models]
      : models;
  const connected = !!auth?.authenticated;
  // Providers used with the user's own API key, entered right in the AI account section.
  const keyProvider = provider === "anthropic" || provider === "openai" ? provider : null;
  const keyTitle = provider === "openai" ? "OpenAI API" : "Anthropic API";
  const providerKey = provider === "openai" ? openaiKey : anthropicKey;
  const setProviderKey = provider === "openai" ? setOpenaiKey : setAnthropicKey;
  const indicatorCatalog = initialIndicatorCatalog(settings?.initial_indicator_catalog);
  const indicatorsDirty = !sameInitialIndicators(initialIndicators, savedInitialIndicators);
  const indicatorsDisabled = !!busy || indicatorsLoading || !indicatorsLoaded;

  return (
    <div className="local-settings">
      {cliSetup && provider !== "openai" && provider !== "anthropic" && (
        <CliSetupDialog provider={provider} reason={cliSetup} checking={cliChecking} error={cliError}
          onRecheck={() => void recheckCli()} onClose={() => setCliSetup(null)} />
      )}
      <div className="page-title">
        <h1>{uiText("設定")}</h1>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="settings-notice" role="status">
          {notice}
        </p>
      )}{" "}
      {!settings && !error && (
        <p className="settings-loading" role="status">
          <AnalysisSpinner />{uiText("正在讀取設定…")}</p>
      )}{" "}
      {settings && (
        <>
          {remoteSection}
          <section className="panel settings-section settings-appearance" aria-labelledby="settings-appearance-title">
            <div className="settings-appearance-row">
              <h2 id="settings-appearance-title">{uiText("外觀")}</h2>
              <label className="settings-appearance-control" htmlFor="settings-theme">
              <span className="sr-only">{uiText("顯示模式")}</span>
              <SelectControl id="settings-theme" value={pendingTheme ?? normalizeUiTheme(settings.ui_theme)}
                disabled={!!busy || !indicatorsLoaded} aria-describedby="settings-theme-help settings-theme-feedback"
                aria-busy={busy === "theme"} onChange={event => {
                  const selected = event.target.value;
                  if (isUiTheme(selected)) void saveTheme(selected);
                }}>
                <option value="system">{uiText("跟隨系統")}</option>
                <option value="light">{uiText("淺色")}</option>
                <option value="dark">{uiText("深色")}</option>
              </SelectControl>
              </label>
            </div>
            <p className="settings-help" id="settings-theme-help">{uiText("選擇後自動儲存並套用。跟隨系統會配合電腦的外觀設定。")}</p>
            <div className="settings-appearance-feedback" id="settings-theme-feedback" aria-live="polite" aria-atomic="true">
              {themeError ? <p className="settings-inline-error" role="alert">{themeError}</p>
                : <p className="settings-appearance-state" role="status">
                  {!indicatorsLoaded ? uiText("正在讀取外觀設定…")
                    : busy === "theme" ? uiText("正在儲存外觀…")
                      : themeNotice ? uiText("外觀已儲存。") : ""}
                </p>}
            </div>
          </section>
          <section className="panel settings-section" aria-labelledby="settings-language-title">
            <div className="panel-head"><h2 id="settings-language-title">{uiText("語言")}</h2></div>
            <label className="settings-field">
              {uiText("語言")}
              <SelectControl value={locale} disabled={!!busy} onChange={(event) => {
                const selected = event.target.value;
                if (!isUiLocale(selected)) return;
                void perform("language", async () => {
                  if (remote) {
                    await setUiLocale(selected);
                    rememberBrowserPreference("locale", selected);
                    setNotice(uiText("語言已儲存。"));
                    return;
                  }
                  const next = await settingsRequest<LocalSettings>("/settings", {
                    method: "PATCH", body: JSON.stringify({ ui_locale: selected }),
                  });
                  await setUiLocale(next.ui_locale ?? selected);
                  setSettings(shownSettings(next));
                  await changedRef.current();
                  setNotice(uiText("語言已儲存。"));
                });
              }}>
                <option value="zh-TW">繁體中文</option>
                <option value="en-US">English</option>
              </SelectControl>
            </label>
            <p className="settings-help">{uiText("介面與新分析使用所選語言；既有報告及追問保留原語言。")}</p>
            {busy === "language" && <p role="status"><AnalysisSpinner /> {uiText("正在儲存語言…")}</p>}
          </section>
          {!remote && <CloudAccountSection onChanged={onCloudAccountChanged} />}
          {!remote && (<section
            className="panel settings-section"
            aria-labelledby="settings-model-title"
          >
            <div className="panel-head">
              <h2 id="settings-model-title">{uiText("AI 分析帳號")}</h2>
              <span
                className={`settings-status${connected ? " connected" : ""}`}
              >
                {connected
                  ? uiText("已連線")
                  : waiting
                    ? uiText("授權中")
                    : !auth || auth.status_known === false
                      ? uiText("尚未確認帳號")
                      : uiText("未連線")}
              </span>
            </div>
            <label className="settings-field">{uiText("分析來源")}<SelectControl
                value={provider}
                disabled={!!busy || waiting}
                onChange={(event) => {
                  setProvider(event.target.value as ModelProvider);
                  setCliSetup(null);
                  setAuth(null);
                  setModels([]);
                  setModel("");
                  setAuthUrl("");
                  setUserCode("");
                  setError("");
                  setNotice("");
                }}
              >
                <option value="chatgpt_plan">{uiText("ChatGPT 方案（官方授權）")}</option>
                <option value="codex">{uiText("Codex 授權")}</option>
                <option value="claude_code">{uiText("Claude Code")}</option>
                <option value="anthropic">{uiText("Claude API（API 金鑰）")}</option>
                <option value="openai">{uiText("OpenAI API（API 金鑰）")}</option>
              </SelectControl>
            </label>
            {(
              <div className="settings-account">
                {auth?.email && (
                  <p className="settings-account-email">{auth.email}</p>
                )}{" "}
                <p className="settings-help">
                  {provider === "claude_code"
                    ? uiText("使用本機 Claude Code 的登入狀態，本應用程式不會讀取或保存 Claude 憑證。尚未登入時，請先在終端機執行 claude auth login。")
                    : provider === "chatgpt_plan"
                      ? uiText("以 OpenAI 官方的 Sign in with ChatGPT 授權 txinTrade 使用你的 ChatGPT Plus 或 Pro 方案額度；每週可用的比例可在 chatgpt.com/settings/usage 設定。分析透過本機的 Codex CLI 執行，需先安裝。")
                    : provider === "openai"
                      ? uiText("使用你的 OpenAI API 金鑰，費用依用量計入你的 OpenAI 帳戶；這是 OpenAI 建議程式化使用的方式。")
                    : provider === "anthropic"
                      ? uiText("使用你在 Claude Console 建立的 Anthropic API 金鑰，費用依用量計入你的 Anthropic 帳戶。這是 Anthropic 條款允許第三方 App 使用 Claude 的方式。")
                      : uiText("使用本機 Codex 的 ChatGPT 授權；尚未登入時，可從這裡完成登入。")}
                </p>
                {/* Say plainly what the provider's terms mean for this connection before the user connects. */}
                {(provider === "codex" || provider === "claude_code") && <p className={provider === "claude_code" ? "settings-help settings-terms-warning" : "settings-help"}>
                  {provider === "claude_code"
                    ? uiText("Anthropic 的消費者條款將 Claude Free、Pro、Max 方案的登入限定於 Anthropic 自家的 App，並限制自動化存取。透過 txinTrade 使用 Claude Code 可能與條款衝突，帳號有被限制的風險；連線前請自行評估。")
                    : uiText("OpenAI 建議程式化的使用採用官方授權方式；建議改用「ChatGPT 方案（官方授權）」連線。")}
                </p>}
                {/* The key belongs with the provider it enables, not with the optional integrations further down. */}
                {keyProvider && (
                  <form className="settings-inline-key" onSubmit={(event) => {
                    event.preventDefault();
                    void perform(keyProvider, async () => {
                      if (!providerKey.trim()) throw new Error(uiText("請輸入 {{p0}} 金鑰", { p0: keyTitle }));
                      await update({ [`${keyProvider}_api_key`]: providerKey.trim() });
                      setProviderKey("");
                      const checked = await settingsRequest<ModelAuthStatus>(`/auth/${keyProvider}/check`, { method: "POST" });
                      setAuth(checked);
                      if (checked.authenticated) {
                        setModels(normalizeModels(await settingsRequest<ModelCatalog>(`/auth/${keyProvider}/models`, { method: "POST" })));
                      }
                      setNotice(checked.authenticated ? uiText("{{p0}} 金鑰已儲存並通過檢查。", { p0: keyTitle }) : uiText("{{p0}} 金鑰已加密儲存。", { p0: keyTitle }));
                    });
                  }}>
                    <label className="settings-field">API Key
                      <input type="password" autoComplete="off" autoCapitalize="none" spellCheck={false}
                        value={providerKey} onChange={(event) => setProviderKey(event.target.value)}
                        placeholder={settings.integrations[keyProvider]?.configured ? uiText("輸入新金鑰以更新") : uiText("輸入 API Key")} />
                    </label>
                    <div className="settings-actions">
                      <button type="submit" className="action" disabled={!!busy || !providerKey} aria-busy={busy === keyProvider}>
                        {busy === keyProvider && <AnalysisSpinner />}{uiText("儲存 {{p0}} 金鑰", { p0: keyTitle })}</button>
                      {settings.integrations[keyProvider]?.configured && (
                        <button type="button" className="settings-secondary" disabled={!!busy} onClick={() => {
                          void perform(`${keyProvider}-clear`, async () => {
                            await update({ [`clear_${keyProvider}`]: true });
                            setAuth(await settingsRequest<ModelAuthStatus>(`/auth/${keyProvider}/status`));
                            setModels([]);
                            setNotice(uiText("{{p0}} 金鑰已移除。", { p0: keyTitle }));
                          });
                        }}>{uiText("移除金鑰")}</button>
                      )}
                    </div>
                  </form>
                )}
                {/* Stays visible after the setup dialog is closed, until Codex or Claude Code is found. */}
                {auth?.cli_installed === false && !keyProvider && (
                  <div className="settings-cli-missing" role="status">
                    <p>{provider === "claude_code"
                      ? uiText("這台電腦還沒有安裝 Claude Code 命令列工具，安裝後才能分析。")
                      : provider === "chatgpt_plan"
                        ? uiText("這台電腦還沒有安裝 Codex。ChatGPT 方案透過 Codex 執行分析，請先安裝 Codex 再連線 ChatGPT。")
                        : uiText("這台電腦還沒有安裝 Codex 命令列工具，安裝後才能分析。")}</p>
                    <button type="button" className="settings-secondary" onClick={() => openCliSetup("install")}>
                      {uiText("查看安裝步驟")}</button>
                  </div>
                )}
                {auth?.error && (
                  <p className="settings-inline-error" role="status">
                    {auth.error}
                  </p>
                )}{" "}
                {waiting ? (
                  <>
                    <p className="settings-login-wait" role="status">
                      <AnalysisSpinner />{uiText("等待瀏覽器完成授權…")}</p>
                    {userCode && (
                      <p className="settings-device-code">{uiText("授權碼：")}<strong>{userCode}</strong>
                      </p>
                    )}
                    <div className="settings-actions">
                      {authUrl && (
                        <button
                          type="button"
                          className="settings-secondary"
                          onClick={() => {
                            void perform("browser", () =>
                              openAuthorization(authUrl),
                            );
                          }}
                          disabled={!!busy}
                        >{uiText("重新開啟授權頁面")}</button>
                      )}
                      <button
                        type="button"
                        className="settings-secondary"
                        disabled={!!busy}
                        onClick={() => authAction("cancel")}
                      >{uiText("取消授權")}</button>
                    </div>
                  </>
                ) : (
                  <div className="settings-actions">
                    <button
                      type="button"
                      className={keyProvider ? "settings-secondary" : "action"}
                      disabled={!!busy || (!!keyProvider && !settings.integrations[keyProvider]?.configured)}
                      aria-busy={busy === "login"}
                      onClick={() => login()}
                    >
                      {busy === "login" && <AnalysisSpinner />}{" "}
                      {provider === "claude_code"
                        ? connected ? uiText("重新檢查登入狀態") : uiText("連線 Claude Code")
                        : keyProvider
                          ? uiText("檢查 API 金鑰")
                          : connected ? uiText("重新授權")
                            : provider === "chatgpt_plan" ? uiText("連線 ChatGPT") : uiText("連線 Codex")}
                    </button>
                    {auth?.authenticated && !keyProvider && (
                      <button
                        type="button"
                        className="settings-secondary"
                        disabled={!!busy}
                        onClick={() => authAction("logout")}
                      >{provider === "claude_code" ? uiText("解除連線") : uiText("登出")}</button>
                    )}
                  </div>
                )}{" "}
                {auth?.needs_reentry && !waiting && (
                  <p className="settings-help">{uiText("請重新登入以啟用新的本機儲存方式。舊授權資料已保留。")}</p>
                )}
              </div>
            )}
            <label className="settings-field">{uiText("分析模型")}<SelectControl
                value={model}
                disabled={!!busy || waiting}
                onChange={(event) => setModel(event.target.value)}
              >
                <option value="">{uiText("帳號預設模型")}</option>
                {modelOptions.map((item) => (
                  <option value={item.id} key={item.id}>
                    {item.name}
                  </option>
                ))}
              </SelectControl>
            </label>
            {connected && (
              <button
                type="button"
                className="settings-secondary"
                disabled={!!busy || waiting}
                aria-busy={busy === "models"}
                onClick={() => {
                  void perform("models", async () => {
                    const value = await settingsRequest<ModelCatalog>(
                      `/auth/${provider}/models`,
                      { method: "POST" },
                    );
                    setModels(normalizeModels(value));
                    setNotice(uiText("模型清單已更新。"));
                  });
                }}
              >
                {busy === "models" && <AnalysisSpinner />}{" "}{uiText("重新整理模型清單")}</button>
            )}
            <div className="settings-actions">
              <button
                type="button"
                className="action"
                disabled={!!busy || waiting}
                aria-busy={busy === "model"}
                onClick={() => {
                  void perform("model", async () => {
                    await update({ model_provider: provider, model });
                    setNotice(uiText("AI 分析設定已儲存。"));
                  });
                }}
              >
                {busy === "model" && <AnalysisSpinner />}{" "}{uiText("儲存分析設定")}</button>
            </div>
          </section>)}
          <section className="panel settings-section settings-indicators" aria-labelledby="settings-indicators-title">
            <div className="panel-head">
              <h2 id="settings-indicators-title">{uiText("首次分析預先計算的指標")}</h2>
              <span className="settings-status">
                {uiText("已選 {{p0}}／{{p1}} 項", { p0: initialIndicators.filter(name => indicatorCatalog.some(item => item.name === name)).length, p1: indicatorCatalog.length })}
              </span>
            </div>
            <p className="settings-help" id="settings-indicators-help">
              {uiText("勾選的指標會先以主週期及三個輔助週期計算，一次交給 AI。未勾選的指標仍可由 Agent 按需呼叫。")}
            </p>
            <p className="settings-indicators-baseline" id="settings-indicators-baseline">
              {uiText("基本指標固定保留：EMA、RSI、MACD、ATR、成交量、VWAP、波段轉折。")}
            </p>
            <div className="settings-indicator-bulk" aria-label={uiText("指標勾選操作")}>
              <button type="button" className="settings-secondary" disabled={indicatorsDisabled || indicatorCatalog.every(item => initialIndicators.includes(item.name))}
                onClick={() => changeIndicators(indicatorCatalog.map(item => item.name))}>
                {uiText("全選指標")}
              </button>
              <button type="button" className="settings-secondary" disabled={indicatorsDisabled || !initialIndicators.length}
                onClick={() => changeIndicators([])}>{uiText("清除勾選")}</button>
            </div>
            <fieldset className="settings-indicator-options" aria-label={uiText("可預先計算的額外指標")}
              aria-describedby="settings-indicators-help settings-indicators-baseline" disabled={indicatorsDisabled}>
              {indicatorCatalog.map(item => {
                const copy = initialIndicatorCopy(item.name);
                const checked = initialIndicators.includes(item.name);
                const id = `initial-indicator-${item.name}`;
                return (
                  <label key={item.name} className="settings-indicator-option" htmlFor={id} data-selected={checked}>
                    <input id={id} type="checkbox" checked={checked} aria-labelledby={`${id}-name`} aria-describedby={`${id}-purpose ${id}-parameters`}
                      onChange={event => changeIndicators(event.target.checked ? [...initialIndicators, item.name] : initialIndicators.filter(name => name !== item.name))} />
                    <span className="settings-indicator-copy">
                      <span className="settings-indicator-name" id={`${id}-name`}>{copy.label}</span>
                      <span className="settings-indicator-purpose" id={`${id}-purpose`}>{copy.purpose}</span>
                      <span className="settings-indicator-parameters" id={`${id}-parameters`}>
                        {initialIndicatorParameterBadges(item.parameters).map((parameter, index) => <span key={index}>{parameter}</span>)}
                      </span>
                    </span>
                  </label>
                );
              })}
              {!indicatorCatalog.length && <p className="settings-help">{uiText("目前沒有可選的額外指標。")}</p>}
            </fieldset>
            <div className="settings-actions">
              <button type="button" className="action settings-indicator-save" disabled={indicatorsDisabled || !indicatorsDirty}
                aria-busy={busy === "indicators"} onClick={() => void saveIndicators()}>
                {busy === "indicators" && <span className="settings-indicator-save-spinner" aria-hidden="true"><AnalysisSpinner /></span>}
                <span className="settings-indicator-save-label">{uiText("儲存指標設定")}</span>
              </button>
              {!indicatorsLoaded && !indicatorsLoading && <button type="button" className="settings-secondary"
                disabled={!!busy} onClick={() => void reloadIndicators()}>{uiText("重新讀取指標設定")}</button>}
            </div>
            <div className="settings-indicator-feedback" aria-live="polite" aria-atomic="true">
              {indicatorsError
                ? <p className="settings-inline-error" role="alert">{indicatorsError}</p>
                : <p className="settings-indicator-state" role="status">
                  {indicatorsLoading ? uiText("正在讀取指標設定…")
                    : busy === "indicators" ? uiText("正在儲存指標…")
                      : indicatorsNotice ? uiText("指標設定已儲存，下次分析生效。")
                        : indicatorsDirty ? uiText("有尚未儲存的變更。") : uiText("變更只會套用到之後的新分析，既有報告保持原樣。")}
                </p>}
            </div>
          </section>
          {!remote && <WebSearchSection enabled={settings.allow_web_search !== false}
            onChanged={(next) => setSettings(shownSettings(next))} />}
          {!remote && <OutcomeSharingSection enabled={!!settings.share_outcomes}
            onChanged={(next) => setSettings(shownSettings(next))} />}
          {!remote && (<>
          <section
            className="panel settings-section"
            aria-labelledby="settings-bingx-title"
          >
            <div className="panel-head">
              <h2 id="settings-bingx-title">{uiText("BingX 持倉同步")}<small className="settings-optional">{uiText("選填")}</small>
              </h2>
              <span
                className={`settings-status${settings.integrations.bingx.configured && !settings.integrations.bingx.needs_verification ? " connected" : ""}`}
              >
                {needsReentry(settings.integrations.bingx)
                  ? uiText("需重新設定")
                  : settings.integrations.bingx.needs_verification
                  ? uiText("待確認")
                  : settings.integrations.bingx.configured ? uiText("已設定") : uiText("未設定")}
              </span>
            </div>
            <p className="settings-help">{uiText("讀取永續與標準合約持倉。未設定時，仍可手動新增持倉。")}</p>
            {needsReentry(settings.integrations.bingx) && (
              <p className="settings-help">{uiText("請重新輸入並儲存 BingX 金鑰以恢復同步。舊加密資料已保留；之後同步不再要求系統密碼。")}</p>
            )}{" "}
            {settings.integrations.bingx.needs_verification && (
              <p className="settings-help">{uiText("已記錄過金鑰設定；按下「同步持倉」時才確認已儲存金鑰，也可在此重新儲存。")}</p>
            )}
            <p className="settings-permission-note" id="settings-bingx-permissions">
              <strong>{uiText("金鑰權限")}</strong>
              <span>{uiText("僅需讀取持倉的唯讀權限。請勿開啟交易、轉帳或提幣權限。")}</span>
            </p>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void perform("bingx", async () => {
                  if (!bingxKey.trim() || !bingxSecret.trim())
                    throw new Error(uiText("請一併輸入 BingX API Key 與 Secret"));
                  await update({
                    bingx_api_key: bingxKey.trim(),
                    bingx_api_secret: bingxSecret.trim(),
                  });
                  setBingxKey("");
                  setBingxSecret("");
                  setNotice(uiText("BingX 金鑰已儲存。"));
                });
              }}
            >
              <div className="settings-fields">
                <label className="settings-field">
                  API Key
                  <input
                    type="password"
                    disabled={!!busy}
                    aria-describedby="settings-bingx-permissions"
                    value={bingxKey}
                    onChange={(event) => setBingxKey(event.target.value)}
                    autoComplete="off"
                    autoCapitalize="none"
                    spellCheck={false}
                    placeholder={
                      settings.integrations.bingx.configured
                        ? uiText("輸入新金鑰以更新")
                        : uiText("輸入 API Key")
                    }
                  />
                </label>
                <label className="settings-field">
                  API Secret
                  <input
                    type="password"
                    disabled={!!busy}
                    aria-describedby="settings-bingx-permissions"
                    value={bingxSecret}
                    onChange={(event) => setBingxSecret(event.target.value)}
                    autoComplete="off"
                    autoCapitalize="none"
                    spellCheck={false}
                    placeholder={uiText("輸入 API Secret")}
                  />
                </label>
              </div>
              <div className="settings-actions">
                <button
                  type="submit"
                  className="action"
                  disabled={!!busy || (!bingxKey && !bingxSecret)}
                  aria-busy={busy === "bingx"}
                >
                  {busy === "bingx" && <AnalysisSpinner />}{" "}{uiText("儲存 BingX 金鑰")}</button>
                {(settings.integrations.bingx.configured || needsReentry(settings.integrations.bingx)) && (
                  <button
                    type="button"
                    className="settings-secondary"
                    disabled={!!busy}
                    onClick={() => {
                      void perform("bingx-clear", async () => {
                        await update({ clear_bingx: true });
                        setBingxKey("");
                        setBingxSecret("");
                        setNotice(uiText("BingX 金鑰已移除。"));
                      });
                    }}
                  >{uiText("移除金鑰")}</button>
                )}
              </div>
            </form>
          </section>
          <BinanceSettingsPanel integration={settings.integrations.binance} busy={!!busy}
            onBusyChange={value => setBusy(value ? "binance" : null)}
            onUpdated={async next => { setSettings(shownSettings(next)); await changedRef.current(); }} />
          <section
            className="panel settings-section"
            aria-labelledby="settings-jev-title"
          >
            <div className="panel-head">
              <h2 id="settings-jev-title">{uiText("Jev 新聞分類")}<small className="settings-optional">{uiText("選填")}</small>
              </h2>
              <span
                className={`settings-status${settings.integrations.jev.configured && !settings.integrations.jev.needs_verification ? " connected" : ""}`}
              >
                {needsReentry(settings.integrations.jev)
                  ? uiText("需重新設定")
                  : settings.integrations.jev.needs_verification
                  ? uiText("待確認")
                  : settings.integrations.jev.configured ? uiText("已設定") : uiText("未分類")}
              </span>
            </div>
            <p className="settings-help">{uiText("提供 Typesafe／Jev 金鑰後啟用新聞分類。未設定時，不執行分類。")}</p>
            {needsReentry(settings.integrations.jev) && (
              <p className="settings-help">{uiText("請重新輸入並儲存 Jev 金鑰以恢復分類。舊加密資料已保留。")}</p>
            )}{" "}
            {settings.integrations.jev.needs_verification && (
              <p className="settings-help">{uiText("舊金鑰尚未確認，新聞分類暫停。請重新輸入並儲存金鑰後啟用。")}</p>
            )}
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void perform("jev", async () => {
                  if (!jevKey.trim()) throw new Error(uiText("請輸入 Jev API Key"));
                  await update({ jev_api_key: jevKey.trim() });
                  setJevKey("");
                  setNotice(uiText("Jev 金鑰已儲存，新聞分類已啟用。"));
                });
              }}
            >
              <label className="settings-field">
                API Key
                <input
                  type="password"
                  value={jevKey}
                  onChange={(event) => setJevKey(event.target.value)}
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder={
                    settings.integrations.jev.configured
                      ? uiText("輸入新金鑰以更新")
                      : uiText("輸入 Typesafe API Key")
                  }
                />
              </label>
              <div className="settings-actions">
                <button
                  type="submit"
                  className="action"
                  disabled={!!busy || !jevKey}
                  aria-busy={busy === "jev"}
                >
                  {busy === "jev" && <AnalysisSpinner />}{" "}{uiText("儲存 Jev 金鑰")}</button>
                {(settings.integrations.jev.configured || needsReentry(settings.integrations.jev)) && (
                  <button
                    type="button"
                    className="settings-secondary"
                    disabled={!!busy}
                    onClick={() => {
                      void perform("jev-clear", async () => {
                        await update({ clear_jev: true });
                        setJevKey("");
                        setNotice(uiText("Jev 金鑰已移除，新聞分類已停用。"));
                      });
                    }}
                  >{uiText("移除金鑰")}</button>
                )}
              </div>
            </form>
          </section>
          {([
            { name: "jblanked", title: uiText("JBlanked 經濟日曆"), key: jblankedKey, setKey: setJblankedKey,
              help: uiText("提供經濟日曆來源檢查使用。儲存金鑰不會立即呼叫供應商；檢查仍受既有請求額度限制。"), visible: true },
          ] as const).filter((item) => item.visible).map((item) => {
            const state = settings.integrations[item.name];
            return (
              <section key={item.name} className="panel settings-section" aria-labelledby={`settings-${item.name}-title`}>
                <div className="panel-head">
                  <h2 id={`settings-${item.name}-title`}>{item.title} <small className="settings-optional">{uiText("選填")}</small></h2>
                  <span className={`settings-status${state?.configured ? " connected" : ""}`}>
                    {needsReentry(state) ? uiText("需重新設定") : state?.configured ? uiText("已設定") : uiText("未設定")}
                  </span>
                </div>
                <p className="settings-help">{item.help}</p>
                {needsReentry(state) && (
                  <p className="settings-help">{uiText("請重新輸入並儲存金鑰以啟用此連線。舊加密資料已保留。")}</p>
                )}
                <form onSubmit={(event) => {
                  event.preventDefault();
                  void perform(item.name, async () => {
                    if (!item.key.trim()) throw new Error(uiText("請輸入 {{p0}} 金鑰", { p0: item.title }));
                    await update({ [`${item.name}_api_key`]: item.key.trim() });
                    item.setKey("");
                    setNotice(uiText("{{p0}} 金鑰已加密儲存。", { p0: item.title }));
                  });
                }}>
                  <label className="settings-field">API Key
                    <input type="password" autoComplete="off" autoCapitalize="none" spellCheck={false}
                      value={item.key} onChange={(event) => item.setKey(event.target.value)}
                      placeholder={state?.configured ? uiText("輸入新金鑰以更新") : uiText("輸入 API Key")} />
                  </label>
                  <div className="settings-actions">
                    <button type="submit" className="action" disabled={!!busy || !item.key}
                      aria-busy={busy === item.name}>
                      {busy === item.name && <AnalysisSpinner />}{uiText("儲存 {{p0}} 金鑰", { p0: item.title })}</button>
                    {(state?.configured || needsReentry(state)) && (
                      <button type="button" className="settings-secondary" disabled={!!busy} onClick={() => {
                        void perform(`${item.name}-clear`, async () => {
                          await update({ [`clear_${item.name}`]: true });
                          item.setKey("");
                          setNotice(uiText("{{p0}} 金鑰已移除。", { p0: item.title }));
                        });
                      }}>{uiText("移除金鑰")}</button>
                    )}
                  </div>
                </form>
              </section>
            );
          })}
          <p className="settings-storage-note">{uiText("交易偏好、持倉、最愛、金鑰與本應用程式的登入授權保存在本機 SQLite，不再使用系統金鑰圈。資料庫與備份包含可還原的連線資料，請勿分享。AI 分析與新聞分類仍需連網。")}</p>
          </>)}
        </>
      )}
    </div>
  );
}
