"""Generate the standalone integration-test process supervisor."""

from __future__ import annotations


def integration_runner_source() -> str:
    return r'''import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const testsRoot = fileURLToPath(new URL("../", import.meta.url));
const projectRoot = resolve(testsRoot, "..");
const active = new Set();
const stopping = new WeakMap();
let interrupted = false;

function pause(milliseconds) {
  return new Promise((done) => setTimeout(done, milliseconds));
}

function start(args, options = {}) {
  const child = spawn(process.execPath, args, {
    cwd: projectRoot,
    stdio: "inherit",
    detached: true,
    windowsHide: true,
    ...options,
  });
  active.add(child);
  child.once("close", () => active.delete(child));
  return child;
}

function waitFor(child) {
  return new Promise((done, fail) => {
    child.once("error", fail);
    child.once("close", (code) => done(code ?? 1));
  });
}

function stop(child) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve();
  }
  if (stopping.has(child)) return stopping.get(child);
  const task = (async () => {
    if (process.platform === "win32") {
      await new Promise((done) => {
        const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          stdio: "ignore",
          windowsHide: true,
        });
        killer.once("error", done);
        killer.once("close", done);
      });
    } else {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        child.kill("SIGTERM");
      }
    }
    if (child.exitCode === null && child.signalCode === null) {
      await Promise.race([new Promise((done) => child.once("close", done)), pause(2000)]);
    }
    if (child.exitCode === null && child.signalCode === null) {
      try {
        if (process.platform === "win32") child.kill("SIGKILL");
        else process.kill(-child.pid, "SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    }
  })();
  stopping.set(child, task);
  return task;
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    interrupted = true;
    process.exitCode = signal === "SIGINT" ? 130 : 143;
    for (const child of active) void stop(child);
  });
}

async function freePort() {
  const listener = createServer();
  try {
    await new Promise((done, fail) => {
      listener.once("error", fail);
      listener.listen(0, "127.0.0.1", done);
    });
    const address = listener.address();
    if (!address || typeof address === "string") throw new Error("No TCP port allocated");
    return address.port;
  } finally {
    await new Promise((done) => listener.close(done));
  }
}

async function waitUntilReady(server, baseUrl) {
  const deadline = Date.now() + 20000;
  while (!interrupted && Date.now() < deadline) {
    if (server.exitCode !== null || server.signalCode !== null) {
      throw new Error(`Backend exited before readiness (${server.exitCode ?? server.signalCode})`);
    }
    try {
      const response = await fetch(`${baseUrl}/__arc/health`, {
        signal: AbortSignal.timeout(1000),
      });
      if (response.ok && (await response.json()).service === "arc-backend") return;
    } catch {}
    await pause(200);
  }
  throw new Error(interrupted ? "Integration run interrupted" : "Backend health check timed out");
}

async function main() {
  try {
    const npmCli = process.env.npm_execpath;
    if (!npmCli) throw new Error("Run integration tests via npm run test:integration");
    const build = start([npmCli, "run", "build", "-w", "@arc/backend"]);
    const buildStatus = await waitFor(build);
    if (buildStatus !== 0 || interrupted) {
      process.exitCode ||= buildStatus || 1;
      return;
    }

    const port = await freePort();
    const baseUrl = `http://127.0.0.1:${port}`;
    const server = start([resolve(testsRoot, "support/server.mjs")], {
      env: { ...process.env, NODE_ENV: "test", DATABASE_URL: ":memory:", PORT: String(port) },
    });
    let serverError;
    server.once("error", (error) => { serverError = error; });
    await waitUntilReady(server, baseUrl);
    if (serverError) throw serverError;

    const filters = process.argv.slice(2);
    if (!filters.some((value) => /(?:^|[/\\])integration(?:[/\\]|$)/.test(value))) {
      filters.unshift("integration");
    }
    const vitest = fileURLToPath(import.meta.resolve("vitest/vitest.mjs"));
    const testProcess = start([vitest, "run", "--config", "vitest.config.ts", ...filters], {
      cwd: testsRoot,
      env: { ...process.env, ARC_TEST_LAYER: "INTEGRATION", ARC_TEST_BASE_URL: baseUrl },
    });
    const testStatus = await waitFor(testProcess);
    process.exitCode ||= testStatus;
  } catch (error) {
    console.error("Integration runner failed:", error);
    process.exitCode ||= 1;
  } finally {
    await Promise.all([...active].map(stop));
  }
}

await main();
'''
