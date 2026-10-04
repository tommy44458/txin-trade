import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import nodeTest from "node:test";

// These run main.mjs in a simulated macOS process with POSIX paths throughout;
// its logic is platform-neutral and the macOS job covers it.
const test = process.platform === "win32" ? nodeTest.skip : nodeTest;
import { createDesktopUpdater, notesForLocale, updatesAllowed } from "../updater.mjs";

const signedPolicy = { enabled: true, signed: true, platform: "darwin", arch: "arm64", channel: "stable" };
const update = { version: "0.3.0", releaseNotes: "An update for the isolated test" };
const mainSource = readFileSync(new URL("../main.mjs", import.meta.url), "utf8");
const stripImports = source => source.replace(/^import\s[\s\S]*?;\r?\n/gm, "");
const stringsContext = {};
runInNewContext(stripImports(readFileSync(new URL("../locales.mjs", import.meta.url), "utf8"))
  .replace(/^export /gm, "") + "\nglobalThis.strings = NATIVE_STRINGS;", stringsContext);
const strings = stringsContext.strings;

function deferred() {
  let resolvePromise;
  let reject;
  const promise = new Promise((resolveValue, rejectValue) => { resolvePromise = resolveValue; reject = rejectValue; });
  return { promise, resolve: resolvePromise, reject };
}

async function flush() {
  for (let count = 0; count < 30; count += 1) await Promise.resolve();
  // Give Node's unhandled-rejection detector a turn. A missing terminal catch
  // fails the test runner, even when the rejection originated inside the VM.
  await new Promise(resolveValue => setImmediate(resolveValue));
}

/** Executes the real main module with every external effect replaced. The only
 * real file reads in this test are the main and locale sources above. */
async function harness(options = {}) {
  const calls = { imports: 0, fetches: [], stops: [], spawns: [], dialogs: [], quits: [],
    reads: [], logEnds: 0, menu: null, boot: null, window: null, updater: null };
  const timers = new Map();
  let nextTimer = 0;
  let nextPort = 41000;
  const ipcHandlers = new Map();
  const app = Object.assign(new EventEmitter(), {
    isPackaged: options.packaged ?? true,
    setName: () => {}, requestSingleInstanceLock: () => true,
    getVersion: () => options.version ?? "0.2.0",
    getPath: name => {
      assert.ok(["userData", "appData"].includes(name));
      return name === "appData" ? "/isolated/app-data" : "/isolated/user-data";
    },
    setPath: (name, path) => {
      // Only a build run from source moves, to its own folder, never the installed app's.
      assert.equal(app.isPackaged, false, "an installed app keeps its user data where it is");
      assert.deepEqual([name, path], ["userData", "/isolated/app-data/txinTrade Dev"]);
      calls.devUserData = path;
    },
    whenReady: () => ({ then(callback) { calls.boot = Promise.resolve().then(callback); return calls.boot; } }),
    quit() {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      app.emit("before-quit", event);
      calls.quits.push(event);
      return event;
    },
  });
  const engine = new EventEmitter();
  engine.checkForUpdates = async () => { engine.emit("update-available", update); return { updateInfo: update }; };
  engine.downloadUpdate = async () => { engine.emit("update-downloaded", update); return []; };
  engine.quitAndInstall = () => { calls.nativeRequested = true; };
  class FakeWindow extends EventEmitter {
    constructor() {
      super();
      calls.window = this;
      this.urls = [];
      this.webContents = Object.assign(new EventEmitter(), { id: 1, mainFrame: {},
        executeJavaScript: async code => { calls.startupScripts ??= []; calls.startupScripts.push(code); },
        send: (channel, state) => { calls.sent ??= []; calls.sent.push([channel, state]); },
        setWindowOpenHandler: () => {}, session: {
          setPermissionRequestHandler: () => {}, setPermissionCheckHandler: () => {},
          webRequest: { onBeforeSendHeaders: () => {}, onHeadersReceived: () => {} },
        } });
    }
    isDestroyed() { return false; }
    setProgressBar(value) { calls.progress ??= []; calls.progress.push(value); }
    isMinimized() { return false; }
    focus() {}
    restore() {}
    show() {}
    setBackgroundColor() {}
    async loadURL(url) { this.urls.push(url); }
  }
  const fakeProcess = Object.assign(new EventEmitter(), { platform: options.platform ?? "darwin",
    arch: options.arch ?? "arm64", resourcesPath: "/isolated/Resources", env: {} });
  const bindings = {
    app, BrowserWindow: FakeWindow,
    dialog: { async showMessageBox(_window, value) {
      calls.dialogs.push(value);
      if (options.dialog) return options.dialog(value, calls.dialogs.length);
      return { response: 1 };
    } },
    ipcMain: { handle: (name, fn) => ipcHandlers.set(name, fn) },
    Menu: { buildFromTemplate: value => value, setApplicationMenu: value => { calls.menu = value; } },
    nativeTheme: Object.assign(new EventEmitter(), { shouldUseDarkColors: false }),
    shell: { openPath: async () => {}, openExternal: async () => {} },
    randomBytes: () => ({ toString: () => "isolated-bearer-token" }),
    createWriteStream: () => ({ on() {}, end() { calls.logEnds += 1; } }),
    mkdirSync: path => assert.equal(path, "/isolated/user-data/data"),
    readFileSync: path => {
      calls.reads.push(path);
      assert.equal(path, "/isolated/Resources/update-policy.json");
      if (options.markerError) throw new Error("missing isolated marker");
      return options.markerText ?? JSON.stringify(options.policy === undefined ? signedPolicy : options.policy);
    },
    dirname, join, resolve, fileURLToPath,
    availablePort: async () => nextPort++, backendCommand: () => ({ command: "mock-only" }),
    backendEnvironment: value => value, developmentConfig: () => ({}), externalUrl: value => value,
    spawnBackend: () => {
      const child = Object.assign(new EventEmitter(), { exitCode: null, signalCode: null,
        stdout: { pipe() {}, unpipe() {} }, stderr: { pipe() {}, unpipe() {} } });
      calls.spawns.push(child);
      return child;
    },
    stopBackend: async child => {
      if (!child) return;
      calls.stops.push(child);
      if (options.stop) await options.stop(child, calls.stops.length);
      child.exitCode = 0;
      child.emit("exit", 0);
    },
    waitForBackend: async () => {
      // A backend that exits during startup: with code 65 its database is from a newer version.
      if (options.backendExit !== undefined) {
        calls.spawns.at(-1).exitCode = options.backendExit;
        throw new Error("isolated backend exited");
      }
    },
    backupDatabaseFiles: (dataDir, version) => {
      calls.backups ??= [];
      calls.backups.push([dataDir, version]);
      return "/isolated/user-data/data/backups/offline.sqlite3";
    },
    resolveUserData: ({ current }) => ({ path: current, migrated: false }),
    delay: async ms => { calls.startupDelays ??= []; calls.startupDelays.push(ms); },
    NATIVE_STRINGS: strings, readSavedLocale: () => options.locale ?? "zh-TW",
    validateLocale: value => { assert.ok(["zh-TW", "en-US"].includes(value)); return value; },
    readSavedTheme: () => "system", validateTheme: value => value,
    readReleaseInfo: () => ({ version: app.getVersion(), channel: "stable", prepared: true, notes: "Isolated notes" }),
    updatesAllowed,
    notesForLocale,
    createDesktopUpdater: value => {
      calls.updater = createDesktopUpdater({ ...value, platform: fakeProcess.platform, arch: fakeProcess.arch,
        timers: { setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
          clearTimeout(id) { timers.delete(id); } }, random: () => 0.5 });
      if (options.dispose) calls.updater.dispose = options.dispose;
      return calls.updater;
    },
    loadUpdaterEngine: async () => {
      calls.imports += 1;
      if (options.importError) throw new Error("isolated missing dependency");
      return { default: { autoUpdater: engine } };
    },
    process: fakeProcess, Promise, URL,
    AbortSignal: { timeout: () => ({ isolated: true }) },
    fetch: async (url, request) => {
      calls.fetches.push({ url, request });
      assert.equal(new URL(url).hostname, "127.0.0.1");
      assert.equal(request.headers.Authorization, "Bearer isolated-bearer-token");
      if (options.fetch) return options.fetch(url, request);
      return { ok: true, json: async () => ({ ready: true, active_tasks: 0,
        backup_path: "/isolated/backups/update.sqlite3" }) };
    },
  };
  assert.equal((mainSource.match(/import\("electron-updater"\)/g) || []).length, 1);
  const source = stripImports(mainSource)
    .replaceAll("import.meta.url", '"file:///isolated/apps/desktop/main.mjs"')
    .replace('import("electron-updater")', "loadUpdaterEngine()")
    + "\nglobalThis.main = { showUpdateDialog, checkForUpdates, installUpdate, updateNativeMenu,"
    + " updateStateChanged, startBackend, get flags() { return { closing, quitting, cleanupComplete, updateDialogOpen }; } };";
  const context = { ...bindings };
  runInNewContext(source, context, { filename: "isolated-main.mjs" });
  await calls.boot;
  await flush();
  return { calls, app, engine, timers, main: context.main,
    menuItem(label) {
      const settings = calls.menu.find(item => item.submenu);
      return settings.submenu.find(item => item.label === label);
    },
    changeLocale(locale) {
      ipcHandlers.get("desktop:update-locale")({ sender: calls.window.webContents,
        senderFrame: calls.window.webContents.mainFrame }, locale);
    },
    /** Call a renderer-facing handler as the app's own window would. */
    invoke(name, ...args) {
      return ipcHandlers.get(name)({ sender: calls.window.webContents,
        senderFrame: calls.window.webContents.mainFrame }, ...args);
    },
    invokeFrom(sender, name, ...args) {
      return ipcHandlers.get(name)(sender, ...args);
    },
  };
}

async function downloaded(h) {
  await h.calls.updater.check();
  await h.calls.updater.download();
  await flush();
  assert.equal(h.calls.updater.state.status, "downloaded");
}

test("unverified markers, unsupported builds and a missing engine keep main usable without update imports or requests", async () => {
  const scenarios = [
    { markerError: true }, { markerText: "broken-json" }, { policy: null }, { policy: [] },
    { policy: { ...signedPolicy, enabled: false } }, { policy: { ...signedPolicy, signed: false } },
    { policy: { enabled: true, signed: true } }, { policy: { ...signedPolicy, platform: "win32" } },
    { policy: { ...signedPolicy, arch: "x64" } }, { policy: { ...signedPolicy, channel: "beta" } },
    { packaged: false }, { platform: "win32" }, { arch: "x64" }, { version: "0.2.0-rc.1" },
    { importError: true },
  ];
  for (const options of scenarios) {
    const h = await harness(options);
    assert.equal(h.calls.imports, options.importError ? 1 : 0, JSON.stringify(options));
    assert.equal(h.calls.updater.state.status, "disabled");
    assert.equal(h.calls.spawns.length, 1, "only the mock backend starts");
    assert.equal(h.calls.fetches.length, 0);
    assert.equal(h.timers.size, 0);
    assert.equal(h.menuItem(strings["zh-TW"].checkUpdates).enabled, false);
    h.menuItem(strings["zh-TW"].updateStatus).click();
    await flush();
    assert.equal(h.calls.dialogs.at(-1).message, strings["zh-TW"].updateDisabled);
    assert.equal(h.calls.dialogs.at(-1).buttons.length, 1);
  }
});

test("startup holds the loading page for its minimum duration and fades before the workspace loads", async () => {
  const h = await harness({ policy: { ...signedPolicy, enabled: false } });
  const [hold, fade] = h.calls.startupDelays;
  assert.ok(hold > 2900 && hold <= 3000, `hold ${hold}`);
  assert.equal(fade, 240);
  assert.deepEqual(h.calls.startupScripts, ["document.body.classList.add('leaving')"]);
  assert.match(h.calls.window.urls[0], /^data:text\/html/);
  assert.equal(h.calls.window.urls.at(-1), "http://127.0.0.1:" + new URL(h.calls.window.urls.at(-1)).port);
});

test("native menus and update dialogs follow the saved language and locale changes", async () => {
  const h = await harness({ policy: { ...signedPolicy, enabled: false }, locale: "en-US" });
  assert.equal(h.calls.menu.find(item => item.submenu).label, "Settings");
  assert.equal(h.menuItem("Check for Updates").enabled, false);
  h.menuItem("Update Status").click();
  await flush();
  assert.equal(h.calls.dialogs.at(-1).message, strings["en-US"].updateDisabled);
  h.changeLocale("zh-TW");
  assert.equal(h.calls.menu.find(item => item.submenu).label, "設定");
  assert.equal(h.menuItem("檢查更新").enabled, false);
  h.menuItem("更新狀態").click();
  await flush();
  assert.equal(h.calls.dialogs.at(-1).message, strings["zh-TW"].updateDisabled);
});

test("notices offer an explicit download and restart, and a rejected native install restores the backend", async () => {
  const h = await harness({ locale: "en-US" });
  assert.equal(h.calls.imports, 1);
  await h.calls.updater.check();
  await flush();
  assert.equal(h.calls.dialogs.at(-1).buttons[0], "Download Update");
  assert.equal(h.calls.updater.state.status, "available");
  assert.equal(h.calls.nativeRequested, undefined);
  // The window's persistent notice hears every state, with only what it shows.
  const [channel, notice] = h.calls.sent.at(-1);
  assert.equal(channel, "desktop:update-state");
  assert.deepEqual(Object.keys(notice).sort(), ["canInstall", "percent", "status", "version"]);
  assert.equal(notice.status, "available");
  await h.calls.updater.download();
  await flush();
  assert.equal(h.calls.dialogs.at(-1).buttons[0], "Restart and Update");
  assert.equal(h.calls.sent.at(-1)[1].canInstall, true);
  // The Dock showed the download as soon as it began, and cleared once it finished.
  assert.ok(h.calls.progress.includes(0.01));
  assert.equal(h.calls.progress.at(-1), -1);
  assert.equal(h.calls.fetches.length, 0);
  assert.equal(h.calls.nativeRequested, undefined);
  h.menuItem("Install Update").click();
  await flush();
  assert.equal(h.calls.updater.state.status, "installing");
  assert.equal(h.calls.nativeRequested, true);
  assert.equal(h.calls.spawns.length, 1);
  assert.equal(h.main.flags.closing, true);
  assert.ok(h.calls.fetches[0].url.endsWith("/desktop-updates/prepare"));
  h.engine.emit("error", new Error("native signature verification failed"));
  await flush();
  assert.equal(h.calls.updater.state.status, "error");
  assert.equal(h.calls.updater.state.errorCode, "UPDATE_SIGNATURE_INVALID");
  assert.equal(h.calls.spawns.length, 2, "late failure starts one new mock backend");
  assert.equal(h.main.flags.closing, false);
  assert.equal(h.main.flags.quitting, false);
  assert.equal(h.calls.dialogs.at(-1).message, strings["en-US"].updateError);
});

test("normal quit with a failed drain release never runs install recovery or restarts the backend", async () => {
  const prepare = deferred();
  const h = await harness({ fetch: async url => {
    if (url.endsWith("/prepare")) return prepare.promise;
    return { ok: false, json: async () => ({ detail: { code: "UPDATE_CANCEL_FAILED" } }) };
  } });
  await downloaded(h);
  h.main.installUpdate();
  await flush();
  assert.equal(h.calls.updater.state.status, "waiting-for-idle");
  const quit = h.app.quit();
  assert.equal(quit.prevented, true);
  assert.equal(h.main.flags.quitting, true);
  prepare.resolve({ ok: true, json: async () => ({ ready: true, active_tasks: 0, backup_path: "/isolated/backup" }) });
  await flush();
  assert.equal(h.calls.spawns.length, 1);
  assert.equal(h.calls.nativeRequested, undefined);
  assert.equal(h.calls.stops.length, 1);
  assert.equal(h.main.flags.cleanupComplete, true);
  assert.equal(h.calls.quits.filter(event => !event.prevented).length, 1);
});

test("repeated quit waits for backend cleanup even when disposal itself rejects", async () => {
  const stopped = deferred();
  const h = await harness({ dispose: async () => { throw new Error("isolated disposal failure"); },
    stop: () => stopped.promise });
  const first = h.app.quit();
  await flush();
  const second = h.app.quit();
  assert.equal(first.prevented, true);
  assert.equal(second.prevented, true);
  assert.equal(h.calls.stops.length, 1);
  assert.equal(h.main.flags.cleanupComplete, false);
  assert.equal(h.calls.quits.filter(event => !event.prevented).length, 0);
  stopped.resolve();
  await flush();
  assert.equal(h.main.flags.cleanupComplete, true);
  assert.equal(h.calls.quits.filter(event => !event.prevented).length, 1);
  assert.equal(h.calls.spawns.length, 1);
});

test("closing a native update dialog rejects safely and clears its open flag", async () => {
  const h = await harness({ dialog: async () => { throw new Error("isolated dialog was destroyed"); } });
  h.menuItem(strings["zh-TW"].updateStatus).click();
  await flush();
  assert.equal(h.main.flags.updateDialogOpen, false);
  h.menuItem(strings["zh-TW"].checkUpdates).click();
  await flush();
  assert.equal(h.main.flags.updateDialogOpen, false);
  assert.ok(h.calls.dialogs.length >= 2);
  await h.calls.updater.download();
  await flush();
  assert.equal(h.main.flags.updateDialogOpen, false);
  assert.equal(h.calls.nativeRequested, undefined);
});

test("explicit download actions and background errors cannot restart the app without user confirmation", async () => {
  let chooseDownload = true;
  const h = await harness({ dialog: async value => {
    if (chooseDownload && value.buttons[0] === strings["zh-TW"].downloadUpdate) {
      chooseDownload = false;
      return { response: 0 };
    }
    return { response: 1 };
  } });
  await h.calls.updater.check();
  await flush();
  assert.equal(h.calls.updater.state.status, "downloaded");
  assert.equal(h.calls.fetches.length, 0);
  assert.equal(h.calls.nativeRequested, undefined);
  assert.equal(h.calls.spawns.length, 1);
  const failure = await harness();
  failure.engine.checkForUpdates = async () => { throw new Error("isolated connection failed"); };
  await failure.calls.updater.check();
  await flush();
  assert.equal(failure.calls.updater.state.status, "error");
  assert.equal(failure.calls.dialogs.length, 0, "background check failures remain quiet");
});

test("the window's update notice reads the state and opens the update dialog, for the app's own window only", async () => {
  const h = await harness({ locale: "en-US" });
  assert.equal(h.invoke("desktop:update-state").status, "idle");
  await h.calls.updater.check();
  await flush();
  // Only what the notice shows crosses into the page.
  const state = h.invoke("desktop:update-state");
  assert.deepEqual(Object.keys(state).sort(), ["canInstall", "percent", "status", "version"]);
  assert.equal(state.status, "available");
  assert.equal(state.canInstall, false);
  const shown = h.calls.dialogs.length;
  h.invoke("desktop:show-update");
  await flush();
  assert.equal(h.calls.dialogs.length, shown + 1);
  assert.equal(h.calls.dialogs.at(-1).buttons[0], "Download Update");
  // Another page or frame cannot read the state or raise the dialog.
  const stranger = { sender: { id: 99 }, senderFrame: {} };
  assert.throws(() => h.invokeFrom(stranger, "desktop:update-state"), /Invalid window/);
  assert.throws(() => h.invokeFrom(stranger, "desktop:show-update"), /Invalid window/);
  assert.equal(h.calls.dialogs.length, shown + 1);
});

test("once the app page registers, update prompts open its scrollable window instead of a native dialog", async () => {
  const h = await harness({ locale: "zh-TW" });
  h.invoke("desktop:update-prompt-ready");
  await h.calls.updater.check();
  await flush();
  const dialogs = h.calls.dialogs.length;
  await h.main.showUpdateDialog();
  assert.equal(h.calls.dialogs.length, dialogs, "no native dialog while the page shows prompts");
  const [channel, prompt] = h.calls.sent.filter(([name]) => name === "desktop:update-prompt").at(-1);
  assert.equal(channel, "desktop:update-prompt");
  assert.equal(prompt.action, "download");
  assert.equal(prompt.actionLabel, strings["zh-TW"].downloadUpdate);
  assert.equal(prompt.dismissLabel, strings["zh-TW"].later);
  assert.equal(typeof prompt.notes, "string");
  // Choosing Download in the page downloads, exactly as the native button did.
  h.invoke("desktop:update-respond", "download");
  await flush();
  assert.equal(h.calls.updater.state.status, "downloaded");
  assert.throws(() => h.invoke("desktop:update-respond", "rm -rf"), /Unknown update action/);
  // A reloaded page has not registered yet: the native dialog is used again.
  h.calls.window.webContents.emit("did-start-loading");
  await h.main.showUpdateDialog();
  await flush();
  assert.equal(h.calls.dialogs.length, dialogs + 1);
});

test("a database from a newer version gets its own screen, and the update is offered at once", async () => {
  const h = await harness({ backendExit: 65, locale: "en-US" });
  const page = decodeURIComponent(h.calls.window.urls.at(-1));
  assert.ok(page.includes(strings["en-US"].newerTitle));
  assert.ok(page.includes("https://txintrade.com/download"));
  assert.ok(!page.includes(strings["en-US"].failureBody));
  await flush();
  // No waiting for the scheduled check: the update window is already offered.
  assert.equal(h.calls.dialogs.at(-1).buttons[0], "Download Update");
  // Installing works without the local service: the app backs the database up itself.
  await h.calls.updater.download();
  await flush();
  h.menuItem("Install Update").click();
  await flush();
  assert.equal(h.calls.updater.state.status, "installing");
  assert.deepEqual(h.calls.backups, [["/isolated/user-data/data", "0.2.0"]]);
  assert.equal(h.calls.fetches.filter(f => f.url.endsWith("/desktop-updates/prepare")).length, 0);
  assert.equal(h.calls.nativeRequested, true);
});

test("any other startup failure keeps the usual screen and still checks for updates", async () => {
  const h = await harness({ backendExit: 1, locale: "en-US" });
  const page = decodeURIComponent(h.calls.window.urls.at(-1));
  assert.ok(page.includes(strings["en-US"].failureBody));
  assert.ok(!page.includes(strings["en-US"].newerTitle));
  // The updater started before the backend, so its first check is scheduled regardless.
  assert.equal(h.calls.imports, 1);
  assert.ok([...h.timers.values()].some(timer => timer.delay >= 5000 && timer.delay <= 15000));
});

test("the update window checks first when nothing has been found yet", async () => {
  const h = await harness({ locale: "en-US" });
  assert.equal(h.calls.updater.state.status, "idle");
  await h.invoke("desktop:show-update");
  await flush();
  assert.equal(h.calls.updater.state.status, "available");
  assert.equal(h.calls.dialogs.at(-1).buttons[0], "Download Update");
});

