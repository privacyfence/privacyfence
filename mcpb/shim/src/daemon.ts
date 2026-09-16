/**
 * Daemon auto-start + /mcp reachability check. Ported from bridge/src/
 * daemon.ts -- see that file's own module comment for the original
 * bridge_main.py provenance. The one thing that changes here is *what*
 * "connectable" means: the bridge discovered a bare TCP socket via
 * ipc_port; this discovers /mcp's host:port via the mcp_url file
 * (protocol.ts) written by web/server.py's WebServer.start(). The probe
 * itself stays a plain TCP connect, not an HTTP request -- the shim has no
 * HTTP/MCP protocol knowledge of its own before it hands off to
 * StreamableHTTPClientTransport in index.ts.
 */

import { spawn } from "node:child_process";
import fs, { constants as fsConstants } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { ShimExitError } from "./errors.js";
import { MCP_URL_FILE, privilegeSeparationRoot } from "./protocol.js";

const CONNECT_TIMEOUT_MS = 10_000; // time to wait for daemon startup
const CONNECT_INTERVAL_MS = 400;
const PATIENT_RETRY_INTERVAL_MS = 2_000; // polling interval once the initial window has elapsed

// Where the installer puts privacyfence-app on each platform (build_dmg.sh's
// Contents/MacOS/ on macOS). No Linux entry here -- a `.deb` install's
// /usr/bin/privacyfence-app is normally already on PATH (caught by the
// which() lookup below), and the one Linux location that isn't
// (~/.local/bin, a pipx install) is handled by its own fallback further
// down instead of a single fixed path, since it's relative to homeDir
// rather than a constant. Windows has no entry here either, for a similar
// reason -- see windowsDefaultAppPaths below, which checks two locations
// instead of one fixed path.
const DEFAULT_APP_PATH_BY_PLATFORM: Partial<Record<NodeJS.Platform, string>> = {
  darwin: "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app",
};
const DEFAULT_APP_PATH = DEFAULT_APP_PATH_BY_PLATFORM.darwin as string;

// Windows install locations for privacyfence-app.exe, checked in this
// order. installer/privacyfence.iss defaults to a non-admin, per-user
// install (`PrivilegesRequired=lowest`), which Inno Setup's {autopf}
// resolves to %LOCALAPPDATA%\Programs\PrivacyFence\ rather than
// %ProgramFiles%\PrivacyFence\ -- only an elevated ("for all users") install
// lands in the latter. A single hardcoded Program-Files-only path here used
// to leave the shim's own self-heal spawn unable to find the daemon on the
// common non-admin install whenever the Task Scheduler autostart task
// didn't fire for any reason, producing a silent hang instead of a clear
// error (privacyfence/privacyfence#410). ProgramFiles and LOCALAPPDATA are
// real env vars Windows always sets; both are still parameterized here
// (rather than read via process.env directly) so tests can exercise this on
// non-Windows CI hosts.
function windowsDefaultAppPaths(env: NodeJS.ProcessEnv): string[] {
  const candidates: string[] = [];
  if (env.ProgramFiles) {
    candidates.push(path.join(env.ProgramFiles, "PrivacyFence", "privacyfence-app.exe"));
  }
  if (env.LOCALAPPDATA) {
    candidates.push(
      path.join(env.LOCALAPPDATA, "Programs", "PrivacyFence", "privacyfence-app.exe")
    );
  }
  return candidates;
}

function isExecutable(candidate: string): boolean {
  try {
    fs.accessSync(candidate, fsConstants.X_OK);
    return fs.statSync(candidate).isFile();
  } catch {
    return false;
  }
}

function which(name: string, pathEnv: string): string | null {
  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, name);
    if (isExecutable(candidate)) return candidate;
  }
  return null;
}

export interface FindDaemonCmdOptions {
  /** Defaults to process.argv[1] — the path shim.js was invoked with. */
  scriptPath?: string;
  /** Defaults to process.env.PATH. */
  pathEnv?: string;
  /** Defaults to the real per-platform default app path (macOS only --
   * win32 has no single default, see windowsDefaultAppPaths); overridable
   * for tests on any platform, including win32. */
  defaultAppPath?: string;
  /** win32 only: environment consulted for ProgramFiles/LOCALAPPDATA when
   * defaultAppPath isn't overridden. Defaults to process.env; overridable
   * for tests since these are real Windows-only env vars that a non-Windows
   * CI host won't have set. */
  windowsEnv?: NodeJS.ProcessEnv;
  /** Defaults to os.homedir(); overridable for tests. */
  homeDir?: string;
  /** Defaults to process.platform; overridable for tests. */
  platform?: NodeJS.Platform;
}

/**
 * Return the command to launch privacyfence-app. Identical reasoning to
 * bridge/src/daemon.ts's findDaemonCmd: the shim ships inside the .mcpb,
 * never as a sibling of privacyfence-app on disk, so this normally only
 * matters as a fallback -- the daemon should already be running via its
 * LaunchAgent (macOS), Task Scheduler task (Windows), or systemd --user unit / XDG autostart
 * entry (Linux) by the time Claude Desktop spawns the shim.
 */
export function findDaemonCmd(opts: FindDaemonCmdOptions = {}): string[] {
  const scriptPath = opts.scriptPath ?? process.argv[1] ?? process.execPath;
  const pathEnv = opts.pathEnv ?? process.env.PATH ?? "";
  const platform = opts.platform ?? process.platform;
  const homeDir = opts.homeDir ?? os.homedir();

  const here = path.dirname(path.resolve(scriptPath));
  const sibling = path.join(here, "privacyfence-app");
  if (isExecutable(sibling)) return [sibling];

  const found = which("privacyfence-app", pathEnv);
  if (found) return [found];

  if (opts.defaultAppPath !== undefined) {
    // An explicit override (real callers never pass one for win32; tests
    // do, to exercise the fallthrough below without touching a real
    // filesystem path) takes priority over the platform defaults.
    if (isExecutable(opts.defaultAppPath)) return [opts.defaultAppPath];
  } else if (platform === "win32") {
    const windowsEnv = opts.windowsEnv ?? process.env;
    for (const candidate of windowsDefaultAppPaths(windowsEnv)) {
      if (isExecutable(candidate)) return [candidate];
    }
  } else {
    const defaultAppPath = DEFAULT_APP_PATH_BY_PLATFORM[platform] ?? DEFAULT_APP_PATH;
    if (isExecutable(defaultAppPath)) return [defaultAppPath];
  }

  // Linux fallback: a `.deb` install puts a wrapper at /usr/bin/privacyfence-app
  // (normally already on PATH, so the which() lookup above would have found
  // it), but a `pipx install privacyfence` drops the console script at
  // ~/.local/bin/privacyfence-app instead -- a location that's on a user's
  // interactive shell PATH but not necessarily on the trimmed-down PATH a
  // graphical session (and therefore Claude Desktop, and this spawned shim)
  // inherits. Check it explicitly before giving up, same spirit as
  // DEFAULT_APP_PATH above for the macOS .app case.
  if (platform === "linux") {
    const linuxPipxDefault = path.join(homeDir, ".local", "bin", "privacyfence-app");
    if (isExecutable(linuxPipxDefault)) return [linuxPipxDefault];
  }

  // Development fallback: run the daemon as a Python module. Relies on a
  // Python interpreter already on PATH with privacyfence installed (e.g.
  // an activated venv) -- see bridge/src/daemon.ts's identical fallback
  // for why this can't reuse an interpreter path the way the old Python
  // bridge did. Windows Python installs commonly expose only `python`, not
  // a `python3` alias (the reverse of most POSIX distros), so try that
  // name first there.
  const pythonCmd = platform === "win32" ? "python" : "python3";
  return [pythonCmd, "-m", "privacyfence.daemon_main"];
}

/**
 * Return true if the daemon's /mcp endpoint is reachable right now --
 * meaning the mcp_url discovery file exists and names a host:port something
 * is actually listening on. A file that hasn't been written yet (daemon not
 * started), doesn't parse as a URL, or still names an earlier launch's now-
 * dead port (daemon mid-restart) all read as false here, same as the
 * bridge's socketConnectable() did for ipc_port.
 */
export function socketConnectable(mcpUrlFile = MCP_URL_FILE): Promise<boolean> {
  return new Promise((resolve) => {
    let url: URL;
    try {
      const text = fs.readFileSync(mcpUrlFile, "utf8").trim();
      if (!text) {
        resolve(false);
        return;
      }
      url = new URL(text);
    } catch {
      resolve(false);
      return;
    }
    const port = url.port ? Number(url.port) : url.protocol === "https:" ? 443 : 80;
    const sock = net.createConnection({ host: url.hostname, port });
    const done = (ok: boolean) => {
      sock.removeAllListeners();
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(1000);
    sock.once("connect", () => done(true));
    sock.once("timeout", () => done(false));
    sock.once("error", () => done(false));
  });
}

/** What socketConnectable() is currently pointed at, for the retry log
 * below: the URL the discovery file names, or why there is no usable URL at
 * all. A shim parked in waitForDaemonPatiently answers nothing on stdio, so
 * the host's readiness check times out and reports a mute server -- while
 * the daemon's own log stays completely silent, since this process has yet
 * to open a single /mcp connection for it to record. These stderr lines are
 * the only evidence the attempt ever happened, which makes the distinction
 * between "no discovery file" (daemon never started, or shut down -- stop()
 * clears it) and "URL unreachable" (daemon still starting, or bound
 * somewhere else) worth spelling out rather than logging a bare "waiting".
 */
export function describeTarget(mcpUrlFile = MCP_URL_FILE): string {
  let text: string;
  try {
    text = fs.readFileSync(mcpUrlFile, "utf8").trim();
  } catch {
    return `no ${mcpUrlFile} yet -- daemon not running`;
  }
  if (!text) {
    return `${mcpUrlFile} is empty`;
  }
  try {
    new URL(text);
  } catch {
    return `${mcpUrlFile} does not contain a URL: ${text}`;
  }
  return `${text} not accepting connections`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface EnsureDaemonRunningOptions {
  mcpUrlFile?: string;
  /** Overridable for tests; defaults to the real findDaemonCmd(). */
  findCmd?: () => string[];
  connectTimeoutMs?: number;
  connectIntervalMs?: number;
  /** Overridable for tests; defaults to the real privilegeSeparationRoot(). */
  separationRoot?: () => string | null;
}

/** Connect to the daemon's /mcp endpoint, launching it first if needed.
 * Resolves once reachable. */
export async function ensureDaemonRunning(opts: EnsureDaemonRunningOptions = {}): Promise<void> {
  const mcpUrlFile = opts.mcpUrlFile ?? MCP_URL_FILE;
  const findCmd = opts.findCmd ?? findDaemonCmd;
  const connectTimeoutMs = opts.connectTimeoutMs ?? CONNECT_TIMEOUT_MS;
  const connectIntervalMs = opts.connectIntervalMs ?? CONNECT_INTERVAL_MS;
  const separationRoot = opts.separationRoot ?? privilegeSeparationRoot;

  if (await socketConnectable(mcpUrlFile)) {
    console.error("Daemon already running");
    return;
  }

  // #428 Phase 4: on a privilege-separated install the daemon belongs to
  // launchd and to the _privacyfence account, and this process is neither.
  // Spawning it here would start it as the logged-in user, where
  // privilege_separation.check_runtime_identity() refuses to run it rather
  // than seed a default policy over the real one -- so the spawn cannot
  // succeed, and trying it once per shim launch would just bury the real
  // reason (launchd hasn't started it, or it crashed) under a second
  // failure. Wait for launchd instead, and say what to look at.
  if (separationRoot() !== null) {
    console.error(
      `Daemon not running (${describeTarget(mcpUrlFile)}) — this install runs it as a ` +
        "LaunchDaemon under its own account (#428 Phase 4), so waiting for launchd to " +
        "start it rather than launching it here. If it never arrives: " +
        "sudo launchctl print system/com.privacyfence.daemon",
    );
    await waitForConnectable(mcpUrlFile, connectTimeoutMs, connectIntervalMs);
    return;
  }

  console.error(`Daemon not running (${describeTarget(mcpUrlFile)}) — launching it now`);
  const [cmd, ...args] = findCmd();
  if (!cmd) {
    throw new Error("findDaemonCmd() returned an empty command");
  }
  const child = spawn(cmd, args, {
    stdio: "ignore",
    detached: true, // detach from our process group
  });
  child.unref();

  await waitForConnectable(mcpUrlFile, connectTimeoutMs, connectIntervalMs);
}

/** Poll until the daemon's /mcp endpoint answers, or the window elapses.
 * Shared by the ordinary spawn-then-wait path and the #428 Phase 4
 * wait-for-launchd one above, so both raise the same ShimExitError and
 * therefore get waitForDaemonPatiently()'s same never-give-up retry. */
async function waitForConnectable(
  mcpUrlFile: string,
  connectTimeoutMs: number,
  connectIntervalMs: number,
): Promise<void> {
  const deadline = Date.now() + connectTimeoutMs;
  while (Date.now() < deadline) {
    if (await socketConnectable(mcpUrlFile)) {
      console.error("Daemon is ready");
      return;
    }
    await sleep(connectIntervalMs);
  }

  throw new ShimExitError(
    "ERROR: PrivacyFence daemon did not start within " +
      `${connectTimeoutMs / 1000} seconds.\n` +
      "Try running 'privacyfence-app' manually and check the logs.",
    1
  );
}

export interface WaitForDaemonPatientlyOptions extends EnsureDaemonRunningOptions {
  /** Polling interval used once the initial launch-and-wait window has elapsed. */
  retryIntervalMs?: number;
}

/**
 * Like ensureDaemonRunning, but never gives up -- same reasoning as bridge/
 * src/daemon.ts's waitForDaemonPatiently: a privacyfence-app cold start
 * (GUI launch, licensing checks, etc.) can outlast the initial window, and
 * since this process is an ephemeral MCP server Claude Desktop spawns once
 * per session, giving up early would force the user to restart their Claude
 * conversation instead of just waiting a few more seconds.
 *
 * findDaemonCmd/spawn only happens once, inside the initial
 * ensureDaemonRunning call — every retry after that just re-checks
 * reachability, so a slow app start never launches a second instance.
 */
export async function waitForDaemonPatiently(opts: WaitForDaemonPatientlyOptions = {}): Promise<void> {
  const mcpUrlFile = opts.mcpUrlFile ?? MCP_URL_FILE;
  const retryIntervalMs = opts.retryIntervalMs ?? PATIENT_RETRY_INTERVAL_MS;

  try {
    await ensureDaemonRunning(opts);
    return;
  } catch (exc) {
    if (!(exc instanceof ShimExitError)) throw exc;
    console.error(`${exc.message}\nWill keep retrying instead of giving up.`);
  }

  const waitingSince = Date.now();
  for (;;) {
    await sleep(retryIntervalMs);
    if (await socketConnectable(mcpUrlFile)) {
      console.error("Daemon is ready");
      return;
    }
    const seconds = Math.round((Date.now() - waitingSince) / 1000);
    console.error(
      `Still waiting for the PrivacyFence daemon after ${seconds}s (${describeTarget(mcpUrlFile)})`,
    );
  }
}
