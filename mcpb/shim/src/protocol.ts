/**
 * Discovery-file constants, ported from what web/server.py and
 * web/mcp_auth.py write on the daemon side (docs/https-connector-refactor-
 * plan.md §12's "Gap found while implementing P2" / D11):
 *
 * - <data dir>/mcp_url   -- written by WebServer.start() once the
 *   embedded HTTP server is actually bound, cleared on stop(). The direct
 *   successor of ipc.py's PORT_FILE (see bridge/src/protocol.ts) for a
 *   client that talks to /mcp instead of the old IPC socket.
 * - <data dir>/mcp_token -- the bearer secret for /mcp
 *   (web/mcp_auth.py's load_or_create_mcp_token()), deliberately a
 *   *different* secret than the approval surface's own session cookie or
 *   the retired ipc_token (§10.3's audience separation) -- see that
 *   module's own docstring.
 *
 * ``<data dir>`` mirrors paths.py's ``data_dir()``: ``~/.privacyfence`` on
 * POSIX, ``%LOCALAPPDATA%\PrivacyFence`` on Windows (not the same dotfile
 * name reused under ``%USERPROFILE%`` -- see that function's own docstring
 * for why). This shim has no install-mode branch of its own (dev-checkout
 * vs. bundled) because it only ever runs from a built ``.mcpb`` -- Claude
 * Desktop never spawns it out of a source tree -- so it always resolves the
 * per-user data dir, matching paths.py's bundled/installed branch.
 *
 * #428 Phase 4 adds one more branch, and it is the reason these two paths go
 * through ``handoffDir()`` rather than ``dataDir()`` directly: on an install
 * that has opted into privilege separation -- any of the three platforms,
 * since B5c -- the daemon runs as its own account and its data directory
 * moves to a system location that account owns (``%ProgramData%\PrivacyFence``
 * on Windows, which is also why the ``%LOCALAPPDATA%`` branch above is not
 * the whole answer there). The two files this shim reads are exactly the two
 * that stay reachable from the user's session, in ``<system root>/handoff``. See
 * src/privacyfence/privilege_separation.py -- this is a port of its marker
 * discovery, deliberately a small and permissive one: anything unreadable,
 * unparseable or not version 1 falls back to the ordinary layout, because a
 * shim that guesses wrong here is a shim that reports "daemon not running"
 * for a daemon that is running fine.
 *
 * Both are read fresh on every launch (this process is spawned once per
 * Claude Desktop session and exits when it ends -- see index.ts), not
 * cached beyond that, since a relaunched daemon can bind a different port
 * and rotate neither of these unless the files themselves are deleted.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/** ``%LOCALAPPDATA%\PrivacyFence`` on Windows, falling back to
 * ``~\AppData\Local\PrivacyFence`` if the env var isn't set -- same
 * fallback reasoning as paths.py's ``windows_data_dir()``. Exported only
 * for tests, which can't otherwise force the ``LOCALAPPDATA``-unset branch
 * without mutating real process.env. */
export function windowsDataDir(): string {
  const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
  return path.join(localAppData, "PrivacyFence");
}

/** Exported only for tests -- see windowsDataDir()'s docstring. */
export function dataDir(): string {
  return process.platform === "win32" ? windowsDataDir() : path.join(os.homedir(), ".privacyfence");
}

/** Each platform's default separated root, keyed exactly like
 * privilege_separation.PLATFORM_LAYOUTS. A platform absent from here has no
 * #428 Phase 4 installer, so nothing can have written a marker for it and
 * this must not go looking for one; as of B5c all three are present.
 *
 * Windows' entry is spelled with forward slashes on purpose. It is only ever
 * consumed by path.join(), which normalizes separators, and writing it this
 * way keeps it comparable to the Python side's own Path("C:/ProgramData/...")
 * -- a backslash literal here would also have to be escaped in both this file
 * and the test that reads it back, for no gain. The real default is
 * %ProgramData%, which privilegeSeparationRoot() prefers when it is set; this
 * literal is the fallback for a process started without it.
 *
 * Exported for tests on both sides of that contract: this file's own, and
 * tests/unit/test_privilege_separation.py, which reads this literal back and
 * asserts it against the Python constants. A drift here does not fail
 * loudly -- it makes the shim look for mcp_url in a directory no installer
 * provisioned, which presents as "daemon not running" against a daemon that
 * is running perfectly well. */
export const SYSTEM_ROOTS: Record<string, string> = {
  darwin: "/Library/Application Support/PrivacyFence",
  linux: "/var/lib/privacyfence",
  win32: "C:/ProgramData/PrivacyFence",
};

/** This platform's default separated root before any marker is read --
 * SYSTEM_ROOTS, except on Windows, where %ProgramData% is consulted first
 * for exactly the reason privilege_separation.system_root() consults it:
 * that folder can be redirected to another volume, the installer's icacls
 * runs against wherever it really is, and a hardcoded C: would then send
 * this shim looking somewhere nothing was ever provisioned.
 *
 * Exported only for tests, which need to drive the win32 branch from a
 * non-Windows CI host. */
export function defaultSystemRoot(
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): string | null {
  if (platform === "win32" && env.ProgramData) {
    return path.join(env.ProgramData, "PrivacyFence");
  }
  return SYSTEM_ROOTS[platform] ?? null;
}

/** Reads and parses the marker at `root`, or null if it's absent or doesn't
 * parse as JSON -- the same "absent on every unseparated install, not an
 * error" case privilegeSeparationRoot() below has always treated quietly. */
function readMarker(root: string): { version?: unknown; platform?: unknown } | null {
  try {
    return JSON.parse(fs.readFileSync(path.join(root, "privilege-separation.json"), "utf8"));
  } catch {
    return null;
  }
}

function isRealMarker(marker: { version?: unknown; platform?: unknown } | null): boolean {
  return marker?.version === 1 && marker?.platform === process.platform;
}

/** The marker file each platform's privilege-separation installer writes
 * (scripts/{macos,linux}_privilege_separation.sh,
 * scripts/windows_privilege_separation.ps1), or
 * null on an install (or a platform) that has no privilege separation.
 * Mirrors privilege_separation.system_root()/separation(): same default
 * roots, same version and platform checks, and -- B11 -- the same guard on
 * PRIVACYFENCE_SYSTEM_ROOT: this shim's environment is whatever the
 * logged-in user's session set, so once a real install is provisioned at
 * the platform's actual root, that variable is refused rather than letting
 * a user-session process redirect the shim onto a root it controls. It is
 * still honoured on the common dev/CI machine, which has no real marker at
 * that literal system root to begin with.
 * Exported only for tests, which need to point it at a temp directory. */
export function privilegeSeparationRoot(env: NodeJS.ProcessEnv = process.env): string | null {
  const override = env.PRIVACYFENCE_SYSTEM_ROOT;
  const defaultRoot = defaultSystemRoot(env);
  const overrideRefused = defaultRoot !== null && isRealMarker(readMarker(defaultRoot));
  const root = override && path.isAbsolute(override) && !overrideRefused ? override : defaultRoot;
  if (!root) return null;
  const marker = readMarker(root);
  if (!isRealMarker(marker)) return null;
  return root;
}

/** Where mcp_url/mcp_token live: ``dataDir()`` normally, and the
 * user-reachable handoff subdirectory of the service-owned root on a
 * privilege-separated install. Exported only for tests. */
export function handoffDir(env: NodeJS.ProcessEnv = process.env): string {
  const root = privilegeSeparationRoot(env);
  return root === null ? dataDir() : path.join(root, "handoff");
}

export const MCP_URL_FILE = path.join(handoffDir(), "mcp_url");
export const MCP_TOKEN_FILE = path.join(handoffDir(), "mcp_token");

/**
 * ADR 0008 (docs/adr/0008-one-principal-per-os-user.md, D3): the address of
 * the daemon's control channel -- the same one companion.py already speaks
 * ``MINT``/``MINT COMPANION``/``STATUS``/``QUIT`` over (web/control_channel.
 * py) -- ported to TypeScript for the first time, because Phase 3 gives this
 * shim its own reason to open a connection there: minting its own ``MINT
 * MCP`` token instead of reading the shared ``mcp_token`` file (see
 * controlChannel.ts). Every function below is a line-for-line port of that
 * module's own address-resolution functions, kept in this file rather than
 * controlChannel.ts because it is pure path arithmetic with the exact same
 * shape as MCP_URL_FILE/MCP_TOKEN_FILE just above -- controlChannel.ts is
 * only the socket/pipe I/O that talks to the address this section computes.
 *
 * These addresses have to match the daemon's own computation byte for byte,
 * or the shim connects to nothing and MINT MCP always falls through to the
 * legacy token file (index.ts's fallback) even on an install new enough to
 * answer it -- a silent "wrong version" symptom worth avoiding by porting
 * the arithmetic exactly rather than approximating it.
 */

/** ``control_channel.py``'s ``SOCKET_FILE_NAME`` -- the file this shim looks
 * for under controlSocketDir(), same name the daemon binds. */
export const CONTROL_SOCKET_FILE_NAME = "control.sock";

/** ``control_channel.py``'s ``_MAX_SUN_PATH_BYTES`` -- see that module's own
 * comment for why 100 rather than the platform's real ``sun_path`` limit
 * (108 on Linux, 104 on macOS, both including the trailing NUL): staying
 * comfortably under the smaller of the two, with margin for the NUL and any
 * encoding quirks, rather than hair-splitting the exact platform limit. This
 * shim never binds a socket itself -- it only has to agree with the daemon
 * on which of the two candidate paths *it* chose, so the comfortable margin
 * matters here exactly as much as it does there. */
export const MAX_SUN_PATH_BYTES = 100;

/**
 * Where the daemon's own ``MINT MCP``/etc. control channel lives, on
 * macOS/Linux -- the directory ``CONTROL_SOCKET_FILE_NAME`` sits under.
 * ``control_channel.py``'s ``posix_socket_path()`` calls this
 * ``paths.control_socket_dir()``, and that function's own docstring gives
 * the branch this ports: ``handoff_dir()`` (companion-reachable) on a
 * privilege-separated install, so the process running as the logged-in
 * human -- exactly what this shim is -- can still reach a socket that now
 * lives under a service account's own root; ``authority_dir()`` (here,
 * ``dataDir()/"authority"``, since this shim's *local principal* is always
 * the one this ADR keeps at the unseparated layout -- see this shim's own
 * module docstring on why it never needs any other principal's address)
 * otherwise, since an unseparated install has no service/human account
 * split for ``handoffDir()`` to exist for in the first place. Deliberately
 * NOT ``handoffDir()`` unconditionally: on a separated install ``authority``
 * and ``handoff`` are two different subdirectories of the same system root,
 * and only the control *socket*'s own address moves to the companion-
 * reachable one -- everything else this shim reads (``mcp_url``,
 * ``mcp_token``) already lives under ``handoffDir()`` for an unrelated
 * reason (the #428 Phase 4 doc comment at the top of this file), so the two
 * "when do I use handoffDir()" answers happen to coincide for those files
 * without being the same question. */
export function controlSocketDir(env: NodeJS.ProcessEnv = process.env): string {
  return privilegeSeparationRoot(env) !== null ? handoffDir(env) : path.join(dataDir(), "authority");
}

/**
 * The pure half of ``posixControlSocketPath()`` -- port of
 * ``control_channel.py``'s ``socket_path_under()``. Takes the already-
 * resolved authority directory rather than calling ``controlSocketDir()``
 * itself, mirroring why the Python function is split the same way (that
 * module's own docstring: a display-only caller can compute the same
 * fallback from a plain path join, without the side-effecting real
 * resolution) -- this file has no such display-only caller yet, but keeping
 * the same seam means a future one, or a test standing in for a specific
 * authority directory, doesn't have to duplicate the hashing logic.
 *
 * The byte-length check is the one place this port cannot just reach for
 * ``.length``: Python's ``len(str(preferred).encode("utf-8"))`` counts UTF-8
 * bytes, and a JS string's ``.length`` counts UTF-16 code units -- the same
 * number only for plain ASCII, which every real ``authority`` path is, but
 * not a distinction worth getting wrong for a check whose whole job is
 * staying under a *byte* buffer. ``Buffer.byteLength`` is the direct
 * equivalent of Python's ``.encode("utf-8")`` length. */
export function socketPathUnder(authorityDir: string): string {
  const preferred = path.join(authorityDir, CONTROL_SOCKET_FILE_NAME);
  if (Buffer.byteLength(preferred, "utf8") < MAX_SUN_PATH_BYTES) {
    return preferred;
  }
  const digest = crypto.createHash("sha256").update(path.dirname(preferred), "utf8").digest("hex").slice(0, 16);
  return path.join(os.tmpdir(), `privacyfence-control-${digest}.sock`);
}

/** Port of ``control_channel.py``'s ``posix_socket_path()`` -- the full
 * address, resolved from this process's own environment. */
export function posixControlSocketPath(env: NodeJS.ProcessEnv = process.env): string {
  return socketPathUnder(controlSocketDir(env));
}

/** Port of ``control_channel.py``'s ``pipe_name_for()`` -- takes a
 * ``dataDir()``-shaped path directly (rather than calling ``dataDir()``
 * itself) for the same reason the Python function does: a caller that
 * already knows a *different* process's data directory (a test standing up
 * a sandboxed daemon alongside a real one) can compute the exact pipe name
 * that process's own ``windowsControlPipeName()`` would, without touching
 * this process's own environment. */
export function pipeNameFor(dataDirPath: string): string {
  const digest = crypto.createHash("sha256").update(dataDirPath, "utf8").digest("hex").slice(0, 16);
  return `\\\\.\\pipe\\PrivacyFence-Control-${digest}`;
}

/** Port of ``control_channel.py``'s ``windows_pipe_name()``. NOTE: always
 * ``dataDir()`` -- NOT ``controlSocketDir()`` -- because a Windows named
 * pipe is a machine-global namespaced name, not a filesystem path under a
 * directory that might move; only the ACL on it changes with separation,
 * not the name (the Python docstring's own note, restated here verbatim
 * because it is exactly as easy to get backwards in this file as in that
 * one). ``env`` is accepted for symmetry with every other function in this
 * section even though ``dataDir()`` itself is not yet env-parameterized --
 * see that function's own definition above; this shim is a single process
 * per launch, so ``process.env`` there is never something a caller needs to
 * override independently of the real environment the way tests override it
 * elsewhere in this file. */
export function windowsControlPipeName(env: NodeJS.ProcessEnv = process.env): string {
  void env;
  return pipeNameFor(dataDir());
}

/** Reads and validates the daemon's current /mcp URL. Throws if the file is
 * missing, empty, or doesn't parse as an absolute URL -- callers only reach
 * this after daemon.ts's socketConnectable() has already confirmed the file
 * names a reachable host:port, so a throw here means the file's *contents*
 * are malformed, not merely that the daemon hasn't started yet. */
export function readMcpUrl(mcpUrlFile = MCP_URL_FILE): string {
  const text = fs.readFileSync(mcpUrlFile, "utf8").trim();
  if (!text) {
    throw new Error(`Empty MCP URL in ${mcpUrlFile}`);
  }
  new URL(text); // throws SyntaxError / TypeError if not a valid absolute URL
  return text;
}

export function readMcpToken(mcpTokenFile = MCP_TOKEN_FILE): string {
  return fs.readFileSync(mcpTokenFile, "utf8").trim();
}
