import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeTheme, shell } from "electron";
import { randomBytes } from "node:crypto";
import { createWriteStream, mkdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { availablePort, backendCommand, backendEnvironment, developmentConfig, externalUrl,
  backupDatabaseFiles, spawnBackend, stopBackend, waitForBackend } from "./runtime.mjs";
import { NATIVE_STRINGS, readSavedLocale, validateLocale } from "./locales.mjs";
import { readSavedTheme, validateTheme } from "./themes.mjs";
import { readReleaseInfo } from "./release-info.mjs";
import { createDesktopUpdater, notesForLocale, updatesAllowed } from "./updater.mjs";
import { resolveUserData } from "./user-data.mjs";

const repoDir = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const token = randomBytes(32).toString("hex");
let window;
let backend;
let origin;
let closing = false;
let quitting = false;
let cleanupComplete = false;
let starting = false;
let startupFailed = false;
let backendLog;
let uiLocale = "zh-TW";
let updater;
let updateDialogOpen = false;
// Set once the app page has its own update window; a navigation or reload clears it.
let updatePromptsInWindow = false;
let manualUpdateChecks = 0;
let updateMenuKey;
const updateNotices = new Set();

app.setName("txinTrade");
// Must run before the single-instance lock, which creates the new profile directory.
if (!app.isPackaged) {
  // Builds run from source keep their own data: a schema from unreleased code must never
  // reach the installed app's database, which would then refuse to open it.
  app.setPath("userData", join(app.getPath("appData"), "txinTrade Dev"));
} else {
  const userData = resolveUserData({ appData: app.getPath("appData"), current: app.getPath("userData") });
  if (userData.path !== app.getPath("userData")) app.setPath("userData", userData.path);
}
if (!app.requestSingleInstanceLock()) app.quit();
app.on("second-instance", () => {
  if (window?.isMinimized()) window.restore();
  window?.focus();
});

function trustedSender(event) {
  if (!window || event.sender !== window.webContents
      || event.senderFrame !== window.webContents.mainFrame) throw new Error("Invalid window");
}

function runUpdateAction(action) {
  // Closing a native dialog during shutdown may reject its promise.
  void Promise.resolve().then(action).catch(() => {});
}

const LATEST_DOWNLOAD_URL = "https://txintrade.com/download";
// desktop_runtime.DATABASE_FROM_NEWER_VERSION_EXIT
const DATABASE_FROM_NEWER_VERSION_EXIT = 65;

// The startup page stays visible long enough for its one-time introduction to settle.
const STARTUP_MINIMUM_MS = 3000;
const STARTUP_EXIT_MS = 240;
const STARTUP_LETTERS = [..."txin"].map(letter => [letter, false])
  .concat([..."Trade"].map(letter => [letter, true]));

function startupWordmark() {
  const letters = STARTUP_LETTERS.map(([letter, strong], index) =>
    `<span class="${strong ? "strong" : ""}" style="--i:${index}">${letter}</span>`).join("");
  // The shine layer repeats the same glyph boxes so one gradient travels across the whole word.
  return `<div class="wordmark" role="img" aria-label="txinTrade"><div class="letters">${letters}</div>
    <div class="shine" aria-hidden="true">${letters}</div></div>`;
}

/** The startup page: starting, a failure, or a database only a newer version can open. */
function startupPage(failure = null) {
  const text = NATIVE_STRINGS[uiLocale];
  const failed = failure !== null;
  const title = failure === "database-newer" ? text.newerTitle : failed ? text.failed : text.starting;
  const body = failure === "database-newer"
    ? `<section class="failure"><h1>${title}</h1><p>${text.newerBody}</p><p class="hint">${text.newerHint}</p>
       <button id="check">${text.checkNow}</button> <button id="download" class="secondary">${text.downloadLatest}</button>
       <p id="error" role="alert"></p></section>`
    : failed
    ? `<section class="failure"><h1>${title}</h1><p>${text.failureBody}</p><p class="hint">${text.failureHint}</p>
       <button id="retry">${text.retry}</button><p id="error" role="alert"></p></section>`
    : `<p class="status" role="status"><span class="sr">${title}. </span>${text.preparing}</p>`;
  return `<!doctype html><html lang="${uiLocale}"><head><meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
  <title>txinTrade</title><style>
  :root{color-scheme:light dark;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue','Segoe UI','Microsoft JhengHei',system-ui,sans-serif;
    --canvas:light-dark(#f5f5f7,#151517);--text:light-dark(#1d1d1f,#f5f5f7);--muted:light-dark(#58585f,#a1a1ab);
    --accent:light-dark(#0066cc,#75b6ff);--danger:light-dark(#c13543,#ff98a3);--settle:cubic-bezier(.32,.72,0,1)}
  body{margin:0;display:grid;place-items:center;min-height:100vh;background:var(--canvas);color:var(--text);
    -webkit-font-smoothing:antialiased;transition:opacity ${STARTUP_EXIT_MS}ms ease}
  body.leaving{opacity:0}
  main{width:min(440px,calc(100vw - 64px));padding:32px;display:flex;flex-direction:column;align-items:center}
  .wordmark{position:relative;font-size:${failed ? "32px" : "52px"};line-height:1.15;font-weight:400;letter-spacing:-.035em;white-space:nowrap}
  .letters,.shine{display:flex}
  .letters span{display:inline-block;animation:letter .8s var(--settle) both;animation-delay:calc(var(--i) * 70ms)}
  .shine{position:absolute;inset:0;color:transparent;pointer-events:none;
    background:linear-gradient(100deg,transparent 40%,var(--accent) 50%,transparent 60%) 100% 0/300% 100% no-repeat;
    -webkit-background-clip:text;background-clip:text;opacity:0;
    animation:fade .4s 1.1s ease forwards,sweep 2.4s 1.1s cubic-bezier(.45,0,.25,1) infinite}
  .shine span{display:inline-block}
  .wordmark .strong{font-weight:700}
  .status{margin:22px 0 0;font-size:13px;line-height:1.5;color:var(--muted);text-align:center;animation:fade .5s .7s ease both}
  .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
  .failure{align-self:stretch;margin-top:32px}
  h1{font-size:22px;font-weight:600;letter-spacing:-.01em;margin:0 0 12px}
  .failure p{font-size:14px;line-height:1.6;color:var(--muted);margin:0 0 10px}
  button{font:inherit;font-weight:500;background:var(--accent);color:light-dark(#fff,#122033);border:0;border-radius:10px;
    min-height:44px;padding:0 20px;cursor:pointer;margin-top:12px}
  button:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:2px}
  button:disabled{opacity:.6}.hint{font-size:12px!important}#error{color:var(--danger)}
  button.secondary{background:transparent;color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
  @keyframes letter{from{opacity:0;filter:blur(10px);transform:translateY(12px)}}
  @keyframes sweep{0%{background-position:100% 0}65%,100%{background-position:0 0}}
  @keyframes fade{from{opacity:0}to{opacity:1}}
  ${failed ? ".letters span{animation:none}.shine{display:none}" : ""}
  @media(prefers-reduced-motion:reduce){.letters span{animation:fade .4s ease both}.shine{display:none}}
  </style></head><body><main>${startupWordmark()}
  ${body}</main>
  <script>document.querySelector('#check')?.addEventListener('click',()=>window.tradeHelper.showUpdate());
  document.querySelector('#download')?.addEventListener('click',()=>window.tradeHelper.openExternal(${JSON.stringify(LATEST_DOWNLOAD_URL)}));
  document.querySelector('#retry')?.addEventListener('click',async()=>{
    const button=document.querySelector('#retry');button.disabled=true;
    try{await window.tradeHelper.retryStartup()}
    catch{document.querySelector('#error').textContent=${JSON.stringify(text.retryFailed)};button.disabled=false}
  });</script></body></html>`;
}

// Hold the startup page for its minimum duration, then fade it before the workspace loads.
async function finishStartupPage(shownAt) {
  const remaining = STARTUP_MINIMUM_MS - (Date.now() - shownAt);
  if (remaining > 0) await delay(remaining);
  try {
    await window.webContents.executeJavaScript("document.body.classList.add('leaving')");
    await delay(STARTUP_EXIT_MS);
  } catch { /* The fade is cosmetic; loading the workspace continues without it. */ }
}

function updateNativeMenu() {
  const text = NATIVE_STRINGS[uiLocale];
  const update = updater?.state;
  const releaseInfo = () => readReleaseInfo({ packaged: app.isPackaged,
    resourcesDir: process.resourcesPath, repoDir, version: app.getVersion(), locale: uiLocale });
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform === "darwin" ? [{ role: "appMenu" }] : []),
    { label: text.settings, submenu: [
      { label: text.about, click: () => {
        const info = releaseInfo();
        void dialog.showMessageBox(window, { type: "info", title: text.about,
          message: `txinTrade ${info.version}`, buttons: [text.close],
          detail: `${text.releaseChannel}: ${info.channel === "beta" ? text.betaChannel : text.stableChannel}`
            + (info.prepared ? "" : `\n${text.unreleasedBuild}`) });
      } },
      { label: text.changelog, click: () => {
        const info = releaseInfo();
        void dialog.showMessageBox(window, { type: "info", title: text.changelog,
          message: `txinTrade ${info.version}`, buttons: [text.close],
          detail: (info.prepared ? "" : `${text.unreleasedBuild}\n\n`)
            + (info.notes || text.noReleaseNotes) });
      } },
      { type: "separator" },
      { label: text.checkUpdates, enabled: Boolean(update?.enabled)
          && !["checking", "downloading", "waiting-for-idle", "installing"].includes(update.status),
        click: () => { runUpdateAction(checkForUpdates); } },
      { label: update?.status === "downloading"
          ? `${text.updateDownloading} ${Math.floor(update.percent)}%` : text.updateStatus,
        click: () => { runUpdateAction(showUpdateDialog); } },
      ...(update?.canInstall && !["waiting-for-idle", "installing"].includes(update.status)
        ? [{ label: text.installUpdate, click: () => { installUpdate(); } }] : []),
      ...(update?.status === "waiting-for-idle"
        ? [{ label: text.cancelUpdateWait, click: () => { runUpdateAction(cancelUpdateWait); } }] : []),
      { type: "separator" },
      { label: text.openData, click: () => {
        void shell.openPath(join(app.getPath("userData"), "data"));
      } },
      { label: text.openLog, click: () => {
        void shell.openPath(join(app.getPath("userData"), "backend.log"));
      } },
    ] },
    { role: "editMenu", label: text.edit }, { role: "viewMenu", label: text.view },
    { role: "windowMenu", label: text.window },
  ]));
}

function updateMessage(state, text) {
  if (!state?.enabled) return text.updateDisabled;
  if (state.errorCode === "UPDATE_BACKUP_FAILED") return text.updateBackupError;
  if (state.errorCode === "UPDATE_CANCEL_FAILED") return text.updateCancelError;
  return ({ idle: text.updateIdle, checking: text.updateChecking,
    available: text.updateAvailable, downloading: text.updateDownloading,
    downloaded: text.updateDownloaded, "waiting-for-idle": text.updateWaiting,
    installing: text.updateInstalling, error: text.updateError })[state.status] || text.updateError;
}

/** What the update window says and offers for the current state, in the app's language. */
function updatePrompt() {
  const state = updater?.state;
  const text = NATIVE_STRINGS[uiLocale];
  const details = [`${text.updateInstalledVersion}: ${app.getVersion()}`];
  if (state?.version) details.push(`${text.updateNewVersion}: ${state.version}`);
  if (state?.status === "downloading") details.push(`${Math.floor(state.percent)}%`);
  if (state?.status === "waiting-for-idle") {
    details.push(`${text.updateActiveTasks}: ${state.activeTasks}`, text.updateWaitingDetail);
  } else if (state?.canInstall) details.push(text.updateDownloadedDetail);
  else if (state?.enabled && state.status === "idle") details.push(text.updateIdleDetail);
  let action = null;
  if (state?.status === "waiting-for-idle") action = "cancel";
  else if (state?.canInstall && state.status !== "installing") action = "install";
  else if (state?.status === "available") action = "download";
  else if (state?.enabled && ["idle", "error"].includes(state.status)) action = "check";
  return {
    tone: state?.status === "error" ? "warning" : "info",
    title: text.updateStatus, message: updateMessage(state, text), details,
    notes: state?.releaseNotes ? notesForLocale(state.releaseNotes, uiLocale) : "",
    action, actionLabel: action ? { cancel: text.cancelUpdateWait, install: text.restartNow,
      download: text.downloadUpdate, check: text.checkUpdates }[action] : null,
    dismissLabel: action ? text.later : text.close,
  };
}

/** Run what the user chose in the update window, if it still applies to the current state. */
function performUpdateAction(action) {
  if (closing) return;
  if (action === "check") runUpdateAction(checkForUpdates);
  else if (action === "download" && updater?.state.status === "available") {
    runUpdateAction(async () => {
      const state = await updater.download();
      if (state.status === "error") await showUpdateDialog();
    });
  } else if (action === "install" && updater?.state.canInstall) installUpdate();
  else if (action === "cancel" && updater?.state.status === "waiting-for-idle") runUpdateAction(cancelUpdateWait);
}

/**
 * Inside the app the update window scrolls, so long notes never cover the screen. Before the app
 * has loaded (startup, a failed backend) the native dialog is the fallback.
 */
async function showUpdateDialog() {
  if (closing || !window || window.isDestroyed()) return;
  const prompt = updatePrompt();
  if (updatePromptsInWindow) {
    try {
      window.webContents.send("desktop:update-prompt", prompt);
      return;
    } catch { updatePromptsInWindow = false; }
  }
  if (updateDialogOpen) return;
  updateDialogOpen = true;
  const details = prompt.notes ? [...prompt.details, prompt.notes] : prompt.details;
  let response;
  try {
    ({ response } = await dialog.showMessageBox(window, { type: prompt.tone,
      title: prompt.title, message: prompt.message, detail: details.join("\n\n"),
      buttons: prompt.action ? [prompt.actionLabel, prompt.dismissLabel] : [prompt.dismissLabel],
      defaultId: 0, cancelId: prompt.action ? 1 : 0, noLink: true }));
  } finally { updateDialogOpen = false; }
  if (response === 0 && prompt.action) performUpdateAction(prompt.action);
}

async function checkForUpdates() {
  if (!updater || closing) return;
  manualUpdateChecks += 1;
  try {
    await updater.check({ userInitiated: true });
    await showUpdateDialog();
  } finally { manualUpdateChecks -= 1; }
}

function installUpdate() {
  if (!updater?.state.canInstall || closing) return;
  runUpdateAction(async () => {
    try { await updater.install(); }
    catch {
      if (quitting || !updater.state.enabled) return;
      // If handing control to the native installer failed after stopping Python,
      // restore a usable local service before showing the retry option.
      if (closing) {
        closing = false;
        cleanupComplete = false;
        await startBackend();
      }
      await showUpdateDialog();
    }
  });
}

async function cancelUpdateWait() {
  try { await updater?.cancel(); }
  catch { await showUpdateDialog(); }
}

/** What the window shows of the update state: enough for its notice, nothing more. */
function publicUpdateState(state) {
  if (!state?.enabled) return null;
  return { status: state.status, version: state.version ?? null, percent: Math.floor(state.percent ?? 0),
    canInstall: Boolean(state.canInstall) };
}

function updateStateChanged(state) {
  // The notice is a convenience: a page that is still loading must never hold up the update itself.
  try {
    if (window && !window.isDestroyed()) {
      window.webContents.send("desktop:update-state", publicUpdateState(state));
      // The Dock icon shows the download too, so it is visible with the window hidden; -1 removes it.
      window.setProgressBar(state.status === "downloading" ? Math.max(0.01, state.percent / 100) : -1);
    }
  } catch { /* The native dialog below still offers the update. */ }
  // Keep download progress visible without rebuilding the entire menu for each byte event.
  const key = `${state.status}:${state.canInstall}:${Math.floor(state.percent / 5)}`;
  if (key !== updateMenuKey) { updateMenuKey = key; updateNativeMenu(); }
  if (closing || manualUpdateChecks || updateDialogOpen
      || !["available", "downloaded"].includes(state.status)) return;
  const notice = `${state.status}:${state.version}`;
  if (updateNotices.has(notice)) return;
  updateNotices.add(notice);
  runUpdateAction(showUpdateDialog);
}

/** The local service failed to start or has stopped: nothing can be drained or back itself up. */
function backendDown() {
  return !starting && (startupFailed || !backend || backend.exitCode !== null || backend.signalCode !== null);
}

async function prepareUpdate() {
  if (!backendDown()) return updateBackendRequest("prepare");
  // No work can be running without the service; back the database files up directly.
  try {
    const backup = backupDatabaseFiles(join(app.getPath("userData"), "data"), app.getVersion());
    return { ready: true, active_tasks: 0, backup_path: backup };
  } catch {
    throw Object.assign(new Error("UPDATE_BACKUP_FAILED"), { code: "UPDATE_BACKUP_FAILED" });
  }
}

async function cancelUpdatePreparation() {
  // Without a running service there is no update gate to release.
  if (!backendDown()) await updateBackendRequest("cancel");
}

async function updateBackendRequest(action) {
  if (!origin || startupFailed || starting || !backend || backend.exitCode !== null
      || backend.signalCode !== null) throw new Error("UPDATE_PREPARE_FAILED");
  const response = await fetch(`${origin}/api/v1/desktop-updates/${action}`, {
    method: "POST", headers: { Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(30_000),
  });
  const result = await response.json();
  if (!response.ok) {
    const code = result?.detail?.code === "UPDATE_BACKUP_FAILED"
      ? "UPDATE_BACKUP_FAILED" : "UPDATE_PREPARE_FAILED";
    throw Object.assign(new Error(code), { code });
  }
  return result;
}

async function initializeUpdater() {
  let distributionPolicy = {};
  if (app.isPackaged) {
    try {
      const policy = JSON.parse(readFileSync(join(process.resourcesPath, "update-policy.json"), "utf8"));
      if (policy && typeof policy === "object" && !Array.isArray(policy)) distributionPolicy = policy;
    } catch { /* Packages without a verified-release policy keep updates disabled. */ }
  }
  let autoUpdater;
  const version = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(0|[1-9]\d*))?$/.exec(app.getVersion());
  const validVersion = version && version.slice(1).filter(part => part !== undefined)
    .every(part => Number.isSafeInteger(Number(part)));
  const channel = version?.[4] === undefined ? "stable" : "beta";
  if (validVersion && updatesAllowed({ packaged: app.isPackaged, platform: process.platform,
    arch: process.arch, policy: distributionPolicy, channel })) {
    try {
      const engine = await import("electron-updater");
      autoUpdater = engine.default.autoUpdater;
    } catch { /* A missing update engine keeps this App usable with updates disabled. */ }
  }
  updater = createDesktopUpdater({ app, autoUpdater, distributionPolicy,
    prepareUpdate,
    cancelUpdate: cancelUpdatePreparation,
    stopBackend: async () => {
      closing = true;
      await stopBackend(backend);
      closeBackendLog();
      cleanupComplete = true;
    },
    onState: updateStateChanged,
  });
  updateNativeMenu();
  await updater.start();
}

/** Stop copying backend output into the log, then close it; output after quitting is dropped. */
function closeBackendLog() {
  if (!backendLog) return;
  for (const stream of [backend?.stdout, backend?.stderr]) {
    try { stream?.unpipe(backendLog); } catch { /* Already detached. */ }
  }
  backendLog.end();
  backendLog = undefined;
}

async function showStartup(failure = null) {
  startupFailed = failure !== null;
  // Closing the window while the backend starts or fails leaves nothing to show.
  if (!window || window.isDestroyed()) return;
  await window.loadURL(`data:text/html;charset=UTF-8,${encodeURIComponent(startupPage(failure))}`);
  window.show();
}

async function startBackend() {
  if (starting) return;
  if (updater?.state.status === "waiting-for-idle") {
    try { await updater.cancel(); }
    catch { await showUpdateDialog(); return; }
  }
  if (closing) return;
  starting = true;
  try {
    await stopBackend(backend);
    await showStartup();
    const startupShownAt = Date.now();
    const port = await availablePort();
    origin = `http://127.0.0.1:${port}`;
    const resourcesDir = process.resourcesPath;
    const webDir = app.isPackaged ? join(resourcesDir, "web") : join(repoDir, "apps/web/dist");
    const userData = app.getPath("userData");
    mkdirSync(join(userData, "data"), { recursive: true, mode: 0o700 });
    const legacyConfig = !app.isPackaged ? developmentConfig(repoDir) : {};
    // Existing encrypted PostgreSQL configuration files remain untouched. Native
    // startup always uses its own embedded SQLite file, without a vault read.
    const env = backendEnvironment({ config: legacyConfig, dataDir: join(userData, "data"),
      port, token, pythonPath: !app.isPackaged ? join(repoDir, "apps/api/src") : undefined });
    const spec = backendCommand({ packaged: app.isPackaged, resourcesDir, repoDir,
      pythonPath: process.env.TRADE_HELPER_PYTHON });
    backend = spawnBackend(spec, { port, webDir, env });
    if (!backendLog) {
      backendLog = createWriteStream(join(userData, "backend.log"), { flags: "a", mode: 0o600 });
      // A log that cannot be written must never interrupt the app or its quit.
      backendLog.on("error", () => {});
    }
    backend.stdout.pipe(backendLog, { end: false });
    backend.stderr.pipe(backendLog, { end: false });
    let spawnError;
    backend.once("error", error => { spawnError = error; });
    await waitForBackend(backend, origin, token);
    if (spawnError) throw spawnError;
    const activeBackend = backend;
    backend.once("exit", () => {
      if (!closing && !starting && activeBackend === backend) {
        stopBackend(activeBackend).then(() => showStartup("failed")).catch(() => {});
      }
    });
    await finishStartupPage(startupShownAt);
    if (closing) return;
    await window.loadURL(origin);
    startupFailed = false;
    window.show();
  } catch {
    // The backend says, by its exit code, when the database comes from a newer version.
    const newer = backend?.exitCode === DATABASE_FROM_NEWER_VERSION_EXIT;
    await stopBackend(backend);
    await showStartup(newer ? "database-newer" : "failed");
    // Offer the update at once; this version can do nothing else with that data.
    if (newer) runUpdateAction(checkForUpdates);
  } finally { starting = false; }
}

app.whenReady().then(async () => {
  // Development runs inside the generic Electron.app; packaged builds carry icon.icns.
  if (!app.isPackaged && process.platform === "darwin") {
    app.dock?.setIcon(join(dirname(fileURLToPath(import.meta.url)), "resources/icon.png"));
  }
  uiLocale = readSavedLocale(join(app.getPath("userData"), "data"),
                            process.env.APP_LOCAL_USER_ID || "local-demo");
  nativeTheme.themeSource = readSavedTheme(join(app.getPath("userData"), "data"),
                                         process.env.APP_LOCAL_USER_ID || "local-demo");
  window = new BrowserWindow({ width: 1280, height: 900, minWidth: 840, minHeight: 640,
    title: "txinTrade", show: false,
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#151517" : "#f5f5f7",
    webPreferences: { preload: join(dirname(fileURLToPath(import.meta.url)), "preload.cjs"),
      additionalArguments: [`--trade-helper-version=${app.getVersion()}`],
      nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true,
      // Kept from the pre-rename build so existing renderer storage stays readable.
      partition: "ai-trade-helper" },
  });
  const localSession = window.webContents.session;
  localSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  localSession.setPermissionCheckHandler(() => false);
  localSession.webRequest.onBeforeSendHeaders((details, callback) => {
    if (origin && new URL(details.url).origin === origin
      && details.webContentsId === window.webContents.id) {
      details.requestHeaders.Authorization = `Bearer ${token}`;
    }
    callback({ requestHeaders: details.requestHeaders });
  });
  localSession.webRequest.onHeadersReceived((details, callback) => {
    const responseHeaders = { ...details.responseHeaders };
    if (origin && new URL(details.url).origin === origin) {
      responseHeaders["Content-Security-Policy"] = [
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'",
      ];
    }
    callback({ responseHeaders });
  });
  // A new page has not registered its update window yet: use the native dialog until it does.
  window.webContents.on("did-start-loading", () => { updatePromptsInWindow = false; });
  window.webContents.on("will-navigate", (event, url) => {
    if (!origin || new URL(url).origin !== origin) event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    try { shell.openExternal(externalUrl(url)); } catch { /* Unsupported protocols are blocked. */ }
    return { action: "deny" };
  });
  ipcMain.handle("desktop:open-external", async (event, url) => {
    trustedSender(event);
    await shell.openExternal(externalUrl(url, {
      developmentOrigin: app.isPackaged ? undefined : process.env.TXINTRADE_CLOUD_ORIGIN }));
  });
  // The page has no clipboard permission (every web permission is denied), so the
  // shell copies plain text for it, such as the CLI setup commands.
  ipcMain.handle("desktop:copy-text", (event, text) => {
    trustedSender(event);
    if (typeof text !== "string" || !text || text.length > 4000) throw new Error("Invalid text");
    clipboard.writeText(text);
  });
  ipcMain.handle("desktop:focus-window", event => {
    trustedSender(event);
    // Return to the app after a sign-in finished in the system browser.
    if (window.isMinimized()) window.restore();
    window.show();
    app.focus({ steal: true });
  });
  ipcMain.handle("desktop:retry-startup", async event => {
    trustedSender(event);
    if (!startupFailed) throw new Error("Startup is already running");
    void startBackend();
  });
  ipcMain.handle("desktop:update-state", event => {
    trustedSender(event);
    return publicUpdateState(updater?.state);
  });
  ipcMain.handle("desktop:show-update", event => {
    trustedSender(event);
    // With nothing found yet, asking for the update window checks first.
    const status = updater?.state.status;
    runUpdateAction(updater?.state.enabled && ["idle", "error"].includes(status) ? checkForUpdates : showUpdateDialog);
  });
  ipcMain.handle("desktop:update-prompt-ready", event => {
    trustedSender(event);
    updatePromptsInWindow = true;
  });
  ipcMain.handle("desktop:update-respond", (event, action) => {
    trustedSender(event);
    if (!["check", "download", "install", "cancel"].includes(action)) throw new Error("Unknown update action");
    performUpdateAction(action);
  });
  ipcMain.handle("desktop:update-locale", (event, locale) => {
    trustedSender(event);
    uiLocale = validateLocale(locale);
    updateNativeMenu();
  });
  ipcMain.handle("desktop:update-theme", (event, theme) => {
    trustedSender(event);
    nativeTheme.themeSource = validateTheme(theme);
    if (window && !window.isDestroyed()) window.setBackgroundColor(nativeTheme.shouldUseDarkColors ? "#151517" : "#f5f5f7");
  });
  nativeTheme.on("updated", () => {
    if (window && !window.isDestroyed()) {
      window.setBackgroundColor(nativeTheme.shouldUseDarkColors ? "#151517" : "#f5f5f7");
    }
  });
  updateNativeMenu();
  // Updates must not depend on the local service: a version that cannot open its data
  // (a newer database, a broken install) still finds and installs its fix.
  await initializeUpdater();
  await startBackend();
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", event => {
  if (closing) {
    if (!cleanupComplete) event.preventDefault();
    else quitting = true;
    return;
  }
  event.preventDefault();
  quitting = true;
  closing = true;
  // Release a pending update drain before stopping the private backend on normal quit.
  Promise.resolve(updater?.dispose()).catch(() => {}).then(() => stopBackend(backend)).finally(() => {
    closeBackendLog();
    cleanupComplete = true;
    app.quit();
  });
});

// Terminal termination uses the same cleanup path as closing the application.
process.on("SIGTERM", () => app.quit());
process.on("SIGINT", () => app.quit());
