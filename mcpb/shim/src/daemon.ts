/**
 * Daemon auto-start + /mcp reachability check. This discovers /mcp's
 * host:port via the mcp_url file
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
// rather than a constant. Windows has no entry here either, because its
// location comes from an env var -- see windowsDefaultAppPaths below.
const DEFAULT_APP_PATH_BY_PLATFORM: Partial<Record<NodeJS.Platform, string>> = {
  darwin: "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app",
};
const DEFAULT_APP_PATH = DEFAULT_APP_PATH_BY_PLATFORM.darwin as string;

// Windows install location for privacyfence-app.exe: installer/
// privacyfence.iss is PrivilegesRequired=admin, so it always lands in
// %ProgramFiles%\PrivacyFence\. Nothing else is searched -- only the current
// install layout is supported (ADR 0041). ProgramFiles is a real env var
// Windows always sets; it is still parameterized here (rather than read via
// process.env directly) so tests can exercise this on non-Windows CI hosts.
function windowsDefaultAppPaths(env: NodeJS.ProcessEnv): string[] {
  return env.ProgramFiles ? [path.join(env.ProgramFiles, "PrivacyFence", "privacyfence-app.exe")] : [];
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
  /** win32 only: environment consulted for ProgramFiles when
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
 * Return the command to launch privacyfence-app. The shim ships inside the
 * .mcpb, never as a sibling of privacyfence-app on disk, so this normally
 * only matters as a fallback for an unseparated development install -- a
 * shipped install runs the daemon as a system service (the LaunchDaemon
 * system/com.privacyfence.daemon on macOS, the system unit
 * privacyfence-daemon.service on Linux, the PrivacyFence service on
 * Windows), and ensureDaemonRunning below waits for that service rather
 * than calling this at all. The LaunchAgent and the Task Scheduler task
 * start only the companion, never the daemon.
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
  // an activated venv): unlike the old Python bridge, this Node process has
  // no interpreter path of its own to reuse. Windows Python installs commonly expose only `python`, not
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

  // On a privilege-separated install the daemon belongs to the
  // service manager and to its own account, and this process is neither.
  // Spawning it here would start it as the logged-in user, where
  // privilege_separation.check_runtime_identity() refuses to run it rather
  // than seed a default policy over the real one -- so the spawn cannot
  // succeed, and trying it once per shim launch would just bury the real
  // reason (the service manager hasn't started it, or it crashed) under a
  // second failure. Wait for it instead, and say what to look at.
  if (separationRoot() !== null) {
    // One entry per platform, since the thing a reader has to go look at is
    // different in each: the daemon is a LaunchDaemon, a system systemd
    // unit, or a Windows service running
    // as NT SERVICE\PrivacyFence. Keyed with a default rather than exhaustively,
    // because this message is diagnostics -- naming the wrong inspection
    // command would be unhelpful, but throwing here would turn a running
    // daemon into a failed shim launch.
    const managers: Partial<Record<NodeJS.Platform, [string, string]>> = {
      linux: ["a systemd unit", "systemctl status privacyfence-daemon.service"],
      win32: ["a Windows service", "sc.exe query PrivacyFence"],
    };
    const [manager, inspect] = managers[process.platform] ?? [
      "a LaunchDaemon",
      "sudo launchctl print system/com.privacyfence.daemon",
    ];
    console.error(
      `Daemon not running (${describeTarget(mcpUrlFile)}) — this install runs it as ` +
        `${manager} under its own account (#428 Phase 4), so waiting for the service ` +
        // The companion's own menu (macOS/Windows) or Applications-menu
        // entry (Linux) is what most people should
        // reach for first -- it can start/restart/stop the service itself,
        // with a real elevation prompt, where the inspect command below can
        // only say why it isn't running. Kept as the second sentence rather
        // than dropped: not every install runs the companion (a headless
        // server, a `--serve`-only Linux session with no Applications menu
        // in reach), and that reader still needs a command to run by hand.
        `manager to start it rather than launching it here. Open the PrivacyFence ` +
        `menu-bar/tray icon (or Applications menu entry, on Linux) and choose ` +
        `"Start PrivacyFence…". If it never arrives: ${inspect}`,
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
  // Without this, a spawn failure (ENOENT for a missing/relocated binary,
  // EACCES/ENOEXEC for a partially-copied .app left behind by a Finder
  // upgrade that was interrupted mid-drag, or a corrupted/quarantined
  // bundle) emits an unhandled 'error' on the
  // ChildProcess EventEmitter, which Node rethrows as an uncaught exception
  // and kills this whole shim process before waitForConnectable below (or
  // waitForDaemonPatiently's retry loop, which exists precisely for a slow
  // daemon start) ever gets a chance to run. Logging and falling through
  // instead turns that crash into the same "did not start in time" path a
  // merely-slow daemon already takes.
  child.on("error", (err) => {
    console.error(`Failed to launch the PrivacyFence daemon (${cmd}): ${err.message}`);
  });

  await waitForConnectable(mcpUrlFile, connectTimeoutMs, connectIntervalMs);
}

/** Poll until the daemon's /mcp endpoint answers, or the window elapses.
 * Shared by the ordinary spawn-then-wait path and the privilege-separated
 * wait-for-the-service-manager one above, so both raise the same ShimExitError and
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
 * Like ensureDaemonRunning, but never gives up: a privacyfence-app cold start
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
