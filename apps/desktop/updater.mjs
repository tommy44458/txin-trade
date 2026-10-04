const CHECK_INTERVAL = 6 * 60 * 60 * 1000;
const RETRY_INTERVAL = 15 * 60 * 1000;
const INSTALL_POLL_INTERVAL = 2000;

function parseVersion(value) {
  if (typeof value !== "string") return null;
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(0|[1-9]\d*))?$/.exec(value);
  if (!match) return null;
  const parts = match.slice(1, 4).map(Number);
  const beta = match[4] === undefined ? null : Number(match[4]);
  if (![...parts, ...(beta === null ? [] : [beta])].every(Number.isSafeInteger)) return null;
  return { parts, beta, channel: beta === null ? "stable" : "beta" };
}

/**
 * In-app updates run only in a released build for its own platform and channel.
 * macOS also needs the signed distribution marker (Squirrel requires a signature);
 * Windows releases are unsigned until a certificate exists, so there the updater
 * rests on the SHA-512 in the release's latest.yml.
 */
export function updatesAllowed({ packaged, platform, arch, policy, channel }) {
  if (packaged !== true || policy?.enabled !== true || policy.platform !== platform
      || policy.arch !== arch || policy.channel !== channel) return false;
  if (platform === "darwin" && arch === "arm64") return policy.signed === true;
  return platform === "win32" && arch === "x64";
}

function newerVersion(candidate, current) {
  for (let index = 0; index < 3; index += 1) {
    if (candidate.parts[index] !== current.parts[index]) {
      return candidate.parts[index] > current.parts[index];
    }
  }
  if (candidate.beta === null) return current.beta !== null;
  if (current.beta === null) return false;
  return candidate.beta > current.beta;
}

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: "\"", "#39": "'", apos: "'", nbsp: " " };

/** GitHub sends release notes as HTML; keep their shape (headings, bullets) as plain Markdown-like text. */
export function releaseNotes(info) {
  const notes = Array.isArray(info?.releaseNotes)
    ? info.releaseNotes.map(item => typeof item?.note === "string" ? item.note : "").join("\n\n")
    : typeof info?.releaseNotes === "string" ? info.releaseNotes : "";
  return notes
    .replace(/\r\n/g, "\n")
    .replace(/<h([1-6])[^>]*>/gi, (_, level) => `\n${"#".repeat(Number(level))} `)
    .replace(/<li[^>]*>/gi, "\n- ")
    .replace(/<br\s*\/?>|<\/(?:p|li|h[1-6]|ul|ol|div)>/gi, "\n")
    .replace(/<[^>]*>/g, "")
    .replace(/&(amp|lt|gt|quot|#39|apos|nbsp);/g, (_, name) => ENTITIES[name])
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .slice(0, 24000).trim();
}

/**
 * Release notes carry both languages ("## 繁體中文" then "## English"); show only the reader's,
 * without the release title. Notes in another shape are returned as they are.
 */
export function notesForLocale(notes, locale) {
  const text = String(notes ?? "");
  const sections = [...text.matchAll(/^## (繁體中文|English)\s*$/gm)];
  if (sections.length < 2) return text.replace(/^# .*\n+/, "").trim();
  const wanted = locale === "zh-TW" ? "繁體中文" : "English";
  const index = sections.findIndex(match => match[1] === wanted);
  const start = sections[index].index + sections[index][0].length;
  const end = sections[index + 1]?.index ?? text.length;
  return text.slice(start, end).trim();
}

function updateError(code) {
  // Native UI translates these codes; network errors, paths and credentials are
  // deliberately not copied into user-visible state or thrown error messages.
  const error = new Error(code);
  error.code = code;
  return error;
}

function failureCode(error, fallback) {
  if (/SIGNATURE|CODE_SIGNATURE|INVALID_PUBLISHER|UNSIGNED/.test(String(error?.code || ""))
      || /code\s*signature|codesign|not\s+signed|unsigned|signature\s+(?:invalid|verification|check)/i
        .test(String(error?.message || ""))) {
    return "UPDATE_SIGNATURE_INVALID";
  }
  if (["UPDATE_BACKUP_FAILED", "UPDATE_PREPARE_FAILED", "UPDATE_BACKEND_UNAVAILABLE",
    "UPDATE_SIGNATURE_INVALID", "UPDATE_VERSION_REJECTED", "UPDATE_CANCEL_FAILED",
    "UPDATE_INSTALL_FAILED", "UPDATE_NOT_DOWNLOADED"].includes(error?.code)) return error.code;
  return fallback;
}

/**
 * Coordinates electron-updater without importing Electron or accepting a feed
 * URL. The updater reads electron-builder's bundled app-update.yml itself.
 * The signed distribution marker is produced only by the official release job.
 */
export function createDesktopUpdater({ app, autoUpdater, prepareUpdate, cancelUpdate, stopBackend,
  onState = () => {}, distributionPolicy = null, platform = process.platform, arch = process.arch,
  logger = null,
  currentVersion = app?.getVersion?.() ?? "", timers = globalThis, now = Date.now,
  random = Math.random, startupDelayMs, checkIntervalMs = CHECK_INTERVAL,
  installPollMs = INSTALL_POLL_INTERVAL } = {}) {
  const current = parseVersion(currentVersion);
  const enabled = current !== null && updatesAllowed({ packaged: app?.isPackaged, platform, arch,
    policy: distributionPolicy, channel: current.channel })
    && autoUpdater !== undefined && autoUpdater !== null;
  let state = Object.freeze({ status: enabled ? "idle" : "disabled", enabled, currentVersion,
    channel: current?.channel ?? "stable", version: null, percent: 0, releaseNotes: "",
    errorCode: null, activeTasks: 0, canInstall: false, lastCheckedAt: null });
  let started = false;
  let disposed = false;
  let checkTimer = null;
  let checkTask = null;
  let downloadTask = null;
  let installTask = null;
  let checkContext = null;
  let downloadContext = null;
  let installContext = null;
  let availableInfo = null;
  let downloadedInfo = null;
  let gateNeedsRelease = false;
  let releaseTask = null;
  let failures = 0;
  const listeners = [];

  function publish(patch) {
    if (disposed) return state;
    const next = { ...state, ...patch };
    if (next.status !== state.status || next.errorCode !== state.errorCode) {
      logger?.info?.(`state ${next.status}${next.version ? ` ${next.version}` : ""}`
        + `${next.errorCode ? ` ${next.errorCode}` : ""}`);
    }
    state = Object.freeze({ ...next,
      canInstall: downloadedInfo !== null && next.status !== "installing" });
    try { onState(state); } catch { /* UI failures cannot bypass update safety. */ }
    return state;
  }

  function clearCheckTimer() {
    if (checkTimer !== null) timers.clearTimeout(checkTimer);
    checkTimer = null;
  }

  function nextCheckDelay() {
    const interval = failures === 0 ? checkIntervalMs
      : Math.min(checkIntervalMs, RETRY_INTERVAL * (2 ** Math.min(failures - 1, 6)));
    // Spread recurring requests across releases rather than synchronizing every
    // installation on the same six-hour boundary. Backoff is spread as well.
    return Math.round(interval * (0.9 + random() * 0.2));
  }

  function scheduleCheck(delay = nextCheckDelay()) {
    clearCheckTimer();
    if (!started || disposed || !enabled) return;
    checkTimer = timers.setTimeout(() => {
      checkTimer = null;
      void check().finally(() => {
        if (checkTimer === null) scheduleCheck();
      });
    }, delay);
    checkTimer?.unref?.();
  }

  function candidateAccepted(info) {
    const candidate = parseVersion(info?.version);
    return candidate !== null && newerVersion(candidate, current)
      && (current.channel === "beta" || candidate.channel === "stable");
  }

  function acceptAvailable(info) {
    if (!candidateAccepted(info)) {
      if (checkContext) checkContext.failure = "UPDATE_VERSION_REJECTED";
      availableInfo = null;
      publish({ status: "error", errorCode: "UPDATE_VERSION_REJECTED" });
      return false;
    }
    availableInfo = info;
    publish({ status: "available", version: info.version, releaseNotes: releaseNotes(info),
      errorCode: null, percent: 0 });
    return true;
  }

  function wakeInstall(context) {
    context?.wake?.();
  }

  function pauseInstall(context) {
    return new Promise(resolve => {
      let timer;
      const finish = () => {
        timers.clearTimeout(timer);
        context.wake = null;
        resolve();
      };
      timer = timers.setTimeout(finish, installPollMs);
      timer?.unref?.();
      context.wake = finish;
      if (context.cancelled || context.failure || disposed) finish();
    });
  }

  async function releaseGate() {
    if (!gateNeedsRelease) return;
    if (releaseTask) return releaseTask;
    releaseTask = (async () => {
      if (typeof cancelUpdate !== "function") throw updateError("UPDATE_CANCEL_FAILED");
      try {
        await cancelUpdate();
        gateNeedsRelease = false;
      } catch {
        throw updateError("UPDATE_CANCEL_FAILED");
      }
    })();
    try { await releaseTask; } finally { releaseTask = null; }
  }

  if (enabled) {
    autoUpdater.logger = logger;
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;
    autoUpdater.forceDevUpdateConfig = false;
    // Setting channel enables downgrades in electron-updater. Restore both
    // flags afterwards; stable metadata is latest-mac.yml (macOS) or latest.yml
    // (Windows), not stable-*.yml.
    autoUpdater.channel = current.channel === "beta" ? "beta" : "latest";
    autoUpdater.allowPrerelease = current.channel === "beta";
    autoUpdater.allowDowngrade = false;

    const on = (name, handler) => {
      autoUpdater.on(name, handler);
      listeners.push([name, handler]);
    };
    on("checking-for-update", () => {
      if (!disposed && checkContext && !installContext && !downloadContext) {
        publish({ status: "checking", errorCode: null });
      }
    });
    on("update-available", info => {
      if (!disposed && checkContext && !checkContext.failure && !installContext && !downloadContext) {
        acceptAvailable(info);
      }
    });
    on("update-not-available", () => {
      if (!disposed && checkContext && !checkContext.failure && !installContext && !downloadContext) {
        availableInfo = null;
        publish({ status: "idle", version: null, releaseNotes: "", errorCode: null, percent: 0 });
      }
    });
    on("download-progress", progress => {
      if (disposed || !downloadContext || downloadContext.failure || installContext || downloadedInfo) return;
      const percent = Number(progress?.percent);
      publish({ status: "downloading", percent: Number.isFinite(percent)
        ? Math.max(0, Math.min(100, percent)) : 0 });
    });
    on("update-downloaded", info => {
      if (disposed || !downloadContext || downloadContext.failure || installContext) return;
      if (!candidateAccepted(info) || info.version !== availableInfo?.version) {
        downloadContext.failure = "UPDATE_VERSION_REJECTED";
        publish({ status: "error", errorCode: "UPDATE_VERSION_REJECTED" });
        return;
      }
      downloadedInfo = info;
      publish({ status: "downloaded", version: info.version, releaseNotes: releaseNotes(info)
        || state.releaseNotes, percent: 100, errorCode: null });
    });
    on("error", error => {
      if (disposed) return;
      if (installContext) {
        installContext.failure = failureCode(error, "UPDATE_INSTALL_FAILED");
        if (installContext.failure === "UPDATE_SIGNATURE_INVALID") downloadedInfo = null;
        wakeInstall(installContext);
        return;
      }
      const context = downloadContext || checkContext;
      if (!context) return;
      context.failure = failureCode(error, downloadContext ? "UPDATE_DOWNLOAD_FAILED" : "UPDATE_CHECK_FAILED");
      if (context.failure === "UPDATE_SIGNATURE_INVALID") downloadedInfo = null;
      publish({ status: "error", errorCode: context.failure });
    });
  }

  async function start() {
    if (disposed || !enabled || started) return state;
    started = true;
    // Soon after launch, so an available update is offered each time the app opens.
    const delay = startupDelayMs ?? (5000 + Math.floor(random() * 10001));
    scheduleCheck(delay);
    return state;
  }

  function check() {
    if (disposed || !enabled) return Promise.resolve(state);
    if (checkTask) return checkTask;
    if (downloadContext || installContext || downloadedInfo) return Promise.resolve(state);
    const context = { failure: null };
    checkContext = context;
    publish({ status: "checking", errorCode: null });
    checkTask = Promise.resolve().then(async () => {
      try {
        const result = await autoUpdater.checkForUpdates();
        if (disposed || checkContext !== context) return state;
        if (context.failure) throw updateError(context.failure);
        if (state.status === "checking") {
          if (candidateAccepted(result?.updateInfo)) acceptAvailable(result.updateInfo);
          else {
            availableInfo = null;
            publish({ status: "idle", version: null, releaseNotes: "", percent: 0 });
          }
        }
        failures = 0;
        publish({ lastCheckedAt: now() });
      } catch (error) {
        if (!disposed) {
          failures += 1;
          publish({ status: "error", errorCode: context.failure
            || failureCode(error, "UPDATE_CHECK_FAILED") });
        }
      } finally {
        checkContext = null;
        checkTask = null;
        scheduleCheck();
      }
      return state;
    });
    return checkTask;
  }

  function download() {
    if (disposed || !enabled || downloadedInfo || installContext) return Promise.resolve(state);
    if (downloadTask) return downloadTask;
    if (checkContext || !availableInfo || !candidateAccepted(availableInfo)) return Promise.resolve(state);
    const context = { failure: null };
    downloadContext = context;
    publish({ status: "downloading", percent: 0, errorCode: null });
    downloadTask = Promise.resolve().then(async () => {
      try {
        await autoUpdater.downloadUpdate();
        if (disposed || downloadContext !== context) return state;
        if (context.failure) throw updateError(context.failure);
        if (!downloadedInfo) throw updateError("UPDATE_DOWNLOAD_INCOMPLETE");
      } catch (error) {
        if (!disposed && !installContext) {
          downloadedInfo = null;
          publish({ status: "error", errorCode: context.failure
            || failureCode(error, "UPDATE_DOWNLOAD_FAILED") });
        }
      } finally {
        downloadContext = null;
        downloadTask = null;
      }
      return state;
    });
    return downloadTask;
  }

  function install() {
    if (disposed || !enabled) return Promise.resolve(state);
    if (installTask) return installTask;
    if (!downloadedInfo) return Promise.reject(updateError("UPDATE_NOT_DOWNLOADED"));
    const context = { cancelled: false, failure: null, wake: null, committed: false,
      nativeRequested: false, handedOff: false };
    installContext = context;
    publish({ status: "waiting-for-idle", activeTasks: 0, errorCode: null });
    installTask = Promise.resolve().then(async () => {
      let error;
      try {
        if (typeof prepareUpdate !== "function" || typeof stopBackend !== "function"
            || typeof cancelUpdate !== "function" || typeof app?.once !== "function"
            || typeof app?.removeListener !== "function") throw updateError("UPDATE_BACKEND_UNAVAILABLE");
        while (!context.cancelled && !disposed) {
          // A failed request may still have closed the backend's job gate.
          gateNeedsRelease = true;
          let prepared;
          try { prepared = await prepareUpdate(); }
          catch (cause) { throw updateError(failureCode(cause, "UPDATE_PREPARE_FAILED")); }
          if (context.failure) throw updateError(context.failure);
          if (context.cancelled || disposed) break;
          if (!prepared || typeof prepared.ready !== "boolean"
              || !Number.isSafeInteger(prepared.active_tasks) || prepared.active_tasks < 0) {
            throw updateError("UPDATE_PREPARE_FAILED");
          }
          if (!prepared.ready) {
            if (prepared.active_tasks === 0) throw updateError("UPDATE_PREPARE_FAILED");
            publish({ status: "waiting-for-idle", activeTasks: prepared.active_tasks });
            await pauseInstall(context);
            if (context.failure) throw updateError(context.failure);
            continue;
          }
          if (prepared.active_tasks !== 0) throw updateError("UPDATE_PREPARE_FAILED");
          if (typeof prepared.backup_path !== "string" || !prepared.backup_path.trim()) {
            throw updateError("UPDATE_BACKUP_FAILED");
          }
          context.committed = true;
          publish({ status: "installing", activeTasks: 0, errorCode: null });
          await stopBackend();
          // A stopped backend cannot retain a gate. Its fresh process starts
          // with an open gate, including after a failed native installation.
          gateNeedsRelease = false;
          if (context.failure) throw updateError(context.failure);
          if (disposed) break;
          // On macOS this call first lets Squirrel fetch and verify the ZIP
          // from electron-updater's private local server. It returns before
          // native verification finishes, so retain the failure listener until
          // the app actually begins quitting; late errors reach install.catch.
          await new Promise((resolve, reject) => {
            const cleanup = () => {
              app.removeListener("before-quit", beforeQuit);
              context.wake = null;
            };
            const finish = () => { cleanup(); resolve(); };
            const beforeQuit = () => { context.handedOff = true; finish(); };
            context.wake = finish;
            app.once("before-quit", beforeQuit);
            try {
              context.nativeRequested = true;
              // Windows: run the NSIS installer silently, then relaunch, so the
              // update looks like the macOS one instead of an installer wizard.
              autoUpdater.quitAndInstall(platform === "win32", true);
            }
            catch (cause) { cleanup(); reject(cause); }
          });
          if (context.failure) throw updateError(context.failure);
          break;
        }
      } catch (cause) {
        error = updateError(failureCode(cause, "UPDATE_INSTALL_FAILED"));
      } finally {
        if (!context.handedOff) {
          try { await releaseGate(); }
          catch (cause) { error = updateError(failureCode(cause, "UPDATE_CANCEL_FAILED")); }
        }
        installContext = null;
        installTask = null;
        if (!disposed && !context.handedOff) {
          publish({ status: error ? "error" : "downloaded", errorCode: error?.code ?? null,
            activeTasks: 0 });
        }
      }
      if (error) throw error;
      return state;
    });
    return installTask;
  }

  async function cancel() {
    const context = installContext;
    // Once the verified backup has allowed shutdown, installation is committed.
    if (context?.committed && !disposed) return state;
    if (context) {
      context.cancelled = true;
      wakeInstall(context);
      // Wait for any pending prepare request before releasing its gate. Otherwise
      // a late prepare response could close the gate again after cancellation.
      await installTask?.catch(() => {});
    }
    try { await releaseGate(); }
    catch (error) {
      publish({ status: "error", errorCode: "UPDATE_CANCEL_FAILED" });
      throw error;
    }
    if (!disposed && downloadedInfo && state.status !== "installing") {
      publish({ status: "downloaded", errorCode: null, activeTasks: 0 });
    }
    return state;
  }

  async function dispose() {
    if (disposed) return state;
    disposed = true;
    started = false;
    clearCheckTimer();
    // Retain the error sink until already-started network promises settle.
    // EventEmitter must not turn an expected late network failure into a crash.
    for (const [name, handler] of listeners) {
      if (name !== "error") autoUpdater.removeListener(name, handler);
    }
    const network = [checkTask, downloadTask].filter(Boolean);
    const nativeRequested = installContext?.nativeRequested === true;
    const removeErrorSink = () => {
      for (const [name, handler] of listeners) {
        if (name === "error") autoUpdater.removeListener(name, handler);
      }
    };
    // Squirrel's native request cannot be cancelled once handed off. During
    // application shutdown it may still forward an error after install() has
    // unwound, so its inert sink lives until the process exits.
    if (nativeRequested) { /* retain the disposed error handler */ }
    else if (network.length) void Promise.allSettled(network).then(removeErrorSink);
    else removeErrorSink();
    await cancel();
    state = Object.freeze({ ...state, status: "disabled", enabled: false, canInstall: false });
    return state;
  }

  return { start, check, download, install, cancel, dispose, get state() { return state; } };
}
