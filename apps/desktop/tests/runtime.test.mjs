import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { EventEmitter, once } from "node:events";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { availablePort, backendCommand, backendEnvironment, developmentConfig, externalUrl,
  createUpdateLog, spawnBackend, stopBackend } from "../runtime.mjs";

test("desktop fixes SQLite to its private data directory and ignores legacy database credentials", () => {
  const env = backendEnvironment({
    inherited: { PATH: "/bin", DATABASE_URL: "postgresql://user:secret@host/db",
      MIGRATION_DATABASE_URL: "postgresql://user:migration-secret@host/db",
      APP_DB_SCHEMA: "public", APP_DB_PATH: "/other.sqlite3", APP_DATA_DIR: "/other",
      APP_DESKTOP: "0", APP_DESKTOP_TOKEN: "unsafe", APP_API_PORT: "8000",
      OPENAI_API_KEY: "secret", BINGX_API_KEY: "secret", TYPESAFE_API_KEY: "secret",
      JBLANKED_API_KEY: "secret",
      VITE_SECRET: "unsafe" },
    config: { DATABASE_URL: "postgresql:///test", MIGRATION_DATABASE_URL: "postgresql:///test",
      APP_DB_PATH: "/config.sqlite3",
      TRADE_DERIVATIVES_CONTEXT_ENABLED: "1" },
    dataDir: "/private/app/data", port: 51234, token: "private-session", platform: "darwin",
  });
  assert.deepEqual(env, {
    PATH: "/bin", TRADE_DERIVATIVES_CONTEXT_ENABLED: "1", APP_MODE: "local",
    APP_DESKTOP: "1", APP_DESKTOP_TOKEN: "private-session", APP_API_PORT: "51234",
    APP_DATA_DIR: "/private/app/data", APP_DB_PATH: join("/private/app/data", "trade_helper.sqlite3"),
    PYTHONUNBUFFERED: "1",
  });
});

test("desktop development never imports optional service credentials from .env", () => {
  const repo = mkdtempSync(join(tmpdir(), "trade-helper-config-"));
  try {
    writeFileSync(join(repo, ".env"), [
      "DATABASE_URL=postgresql:///test", "MIGRATION_DATABASE_URL=postgresql:///test",
      "OPENAI_API_KEY=dummy", "OPENAI_MODEL=legacy",
      "BINGX_API_KEY=dummy", "BINGX_API_SECRET=dummy", "TYPESAFE_API_KEY=dummy",
      "JBLANKED_API_KEY=dummy",
      "APP_DESKTOP_TOKEN=unsafe", "APP_DATA_DIR=/other", "APP_DB_PATH=/other.sqlite3",
      "APP_DB_SCHEMA=private", "VITE_SECRET=unsafe",
      "TRADE_DERIVATIVES_CONTEXT_ENABLED=1",
    ].join("\n"));
    assert.deepEqual(developmentConfig(repo), {
      TRADE_DERIVATIVES_CONTEXT_ENABLED: "1",
    });
  } finally { rmSync(repo, { recursive: true, force: true }); }
});

test("packaged app never falls back to an arbitrary system Python", () => {
  assert.throws(() => backendCommand({ packaged: true, resourcesDir: "/missing", repoDir: "/missing" }), /執行檔/);
  const resources = mkdtempSync(join(tmpdir(), "trade-helper-bundle-"));
  try {
    mkdirSync(join(resources, "backend"));
    const binary = join(resources, "backend", process.platform === "win32"
      ? "trade-helper-backend.exe" : "trade-helper-backend");
    writeFileSync(binary, "");
    assert.deepEqual(backendCommand({ packaged: true, resourcesDir: resources }), {
      command: binary, args: [], cwd: resources,
    });
  } finally { rmSync(resources, { recursive: true, force: true }); }
});

test("external links reject executable protocols, embedded passwords and loopback", () => {
  for (const value of ["file:///etc/passwd", "javascript:alert(1)", "http://openai.com",
    "https://user:password@openai.com", "https://localhost/private", "https://127.0.0.1/private",
    "https://[::1]/private"]) assert.throws(() => externalUrl(value));
  assert.equal(externalUrl("https://auth.openai.com/authorize?state=x"),
    "https://auth.openai.com/authorize?state=x");
});

test("only the configured development cloud origin may open over loopback HTTP", () => {
  const developmentOrigin = "http://localhost:8787";
  assert.equal(externalUrl("http://localhost:8787/auth/google/start?client=desktop", { developmentOrigin }),
    "http://localhost:8787/auth/google/start?client=desktop");
  for (const value of ["http://localhost:9999/x", "http://127.0.0.1:8787/x", "http://user:pw@localhost:8787/x",
    "file:///etc/passwd", "http://evil.test/x"]) assert.throws(() => externalUrl(value, { developmentOrigin }));
  // A non-loopback development origin never weakens the HTTPS rule.
  assert.throws(() => externalUrl("http://cloud.test/x", { developmentOrigin: "http://cloud.test" }));
  assert.throws(() => externalUrl("http://localhost:8787/x"));
});

test("desktop chooses a free loopback port instead of fixed development ports", async () => {
  const port = await availablePort();
  assert.ok(port > 0 && port <= 65535);
});

test("cleanup kills a private worker after its supervisor has already exited", {
  skip: process.platform === "win32",
}, async () => {
  const leader = spawn(process.execPath, ["-e", `
    const { spawn } = require('node:child_process');
    const worker = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
    worker.unref();
    process.stdout.write(String(worker.pid), () => process.exit(0));
  `], { detached: true, stdio: ["ignore", "pipe", "ignore"] });
  const exited = once(leader, "exit");
  let output = "";
  leader.stdout.on("data", chunk => { output += chunk.toString(); });
  try {
    await exited;
    const workerPid = Number(output);
    assert.ok(Number.isSafeInteger(workerPid) && workerPid > 0);
    assert.equal(leader.exitCode, 0);
    assert.doesNotThrow(() => process.kill(workerPid, 0));
    await stopBackend(leader);
    const deadline = Date.now() + 3000;
    let workerStopped = false;
    while (Date.now() < deadline) {
      try { process.kill(workerPid, 0); }
      catch (error) {
        if (error.code === "ESRCH") { workerStopped = true; break; }
        throw error;
      }
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    assert.ok(workerStopped, "the exited supervisor's worker must not remain running");
  } finally {
    try { process.kill(-leader.pid, "SIGKILL"); } catch { /* Test cleanup already finished. */ }
  }
});

test("on Windows the backend gets UTF-8 and a stdin lifeline instead of POSIX signals", () => {
  const env = backendEnvironment({ inherited: {}, dataDir: "C:\\data", port: 1, token: "t", platform: "win32" });
  assert.equal(env.PYTHONUTF8, "1");
  assert.equal(env.APP_DESKTOP_LIFELINE, "stdin");
  const mac = backendEnvironment({ inherited: {}, dataDir: "/data", port: 1, token: "t", platform: "darwin" });
  assert.equal(mac.APP_DESKTOP_LIFELINE, undefined);
});

function fakeBackend({ exitsOnStdinEnd }) {
  const child = new EventEmitter();
  Object.assign(child, { pid: 4321, exitCode: null, signalCode: null, ended: false });
  child.kill = () => { throw new Error("kill() is a hard stop on Windows"); };
  child.stdin = { end: () => {
    child.ended = true;
    if (exitsOnStdinEnd) setTimeout(() => { child.exitCode = 0; child.emit("exit", 0); }, 10);
  } };
  return child;
}

function fakeRun(calls) {
  return (command, args) => {
    calls.push([command, ...args]);
    const killer = new EventEmitter();
    setTimeout(() => killer.emit("exit", 0), 5);
    return killer;
  };
}

test("on Windows stopping closes the lifeline and forces nothing when the backend exits", async () => {
  const child = fakeBackend({ exitsOnStdinEnd: true });
  const calls = [];
  await stopBackend(child, { platform: "win32", run: fakeRun(calls), graceMs: 1000 });
  assert.equal(child.ended, true);
  assert.deepEqual(calls, []);
});

test("on Windows a backend that ignores its lifeline has its process tree ended", async () => {
  const child = fakeBackend({ exitsOnStdinEnd: false });
  const calls = [];
  await stopBackend(child, { platform: "win32", run: fakeRun(calls), graceMs: 50 });
  assert.equal(child.ended, true);
  assert.deepEqual(calls, [["taskkill", "/pid", "4321", "/T", "/F"]]);
});

test("an update without the local service backs up the database with its WAL, privately", async () => {
  const { mkdtempSync, writeFileSync, readFileSync, statSync, readdirSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join: joinPath } = await import("node:path");
  const { backupDatabaseFiles } = await import("../runtime.mjs");
  const dataDir = mkdtempSync(joinPath(tmpdir(), "txintrade-backup-"));
  writeFileSync(joinPath(dataDir, "trade_helper.sqlite3"), "main");
  writeFileSync(joinPath(dataDir, "trade_helper.sqlite3-wal"), "recent writes");
  const target = backupDatabaseFiles(dataDir, "1.0.6", { now: () => Date.UTC(2026, 9, 3, 9, 30, 15) });
  assert.equal(target, joinPath(dataDir, "backups", "trade_helper-before-update-1.0.6-20261003T093015Z-offline.sqlite3"));
  assert.equal(readFileSync(target, "utf8"), "main");
  assert.equal(readFileSync(`${target}-wal`, "utf8"), "recent writes");
  assert.deepEqual(readdirSync(joinPath(dataDir, "backups")).length, 2);
  // Windows has no POSIX modes; the backup stays private inside the user's profile.
  if (process.platform !== "win32") {
    assert.equal(statSync(target).mode & 0o777, 0o600);
    assert.equal(statSync(joinPath(dataDir, "backups")).mode & 0o777, 0o700);
  }
  assert.throws(() => backupDatabaseFiles(joinPath(dataDir, "missing"), "1.0.6"), /UPDATE_BACKUP_FAILED/);
});

test("a backend pipe that fails while the backend exits never becomes an uncaught error", async () => {
  // Like Windows: stdin is the lifeline pipe, and closing it can fail with "write EOF".
  const child = spawnBackend({ command: process.execPath, args: ["-e", "setTimeout(() => {}, 200)"], cwd: tmpdir() },
    { port: 1, webDir: tmpdir(), env: process.env, platform: "win32" });
  for (const stream of [child.stdin, child.stdout, child.stderr]) {
    assert.doesNotThrow(() => stream.emit("error", Object.assign(new Error("write EOF"), { code: "EOF" })));
  }
  child.stdin.end();
  await once(child, "exit");
});

test("the update log appends lines, starts over past its limit, and never throws", () => {
  const files = new Map();
  const fs = {
    statSync: path => { if (!files.has(path)) throw new Error("ENOENT"); return { size: files.get(path).length }; },
    appendFileSync: (path, text) => files.set(path, (files.get(path) ?? "") + text),
    writeFileSync: (path, text) => files.set(path, text),
  };
  const log = createUpdateLog("/u/updater.log", { fs, now: () => new Date("2026-10-05T00:00:00Z"), maxBytes: 120 });
  log.info("Checking for update");
  log.error(new Error("net::ERR_CONNECTION_RESET"));
  assert.match(files.get("/u/updater.log"), /^2026-10-05T00:00:00.000Z info Checking for update\n.*error Error: net::ERR_CONNECTION_RESET/s);
  log.info("x".repeat(100));
  assert.equal(files.get("/u/updater.log").split("\n").length, 2, "past the limit the log starts over");
  const broken = createUpdateLog("/u/updater.log", { fs: { statSync() { throw new Error(); }, appendFileSync() { throw new Error("EACCES"); } } });
  assert.doesNotThrow(() => broken.warn("still fine"));
});
