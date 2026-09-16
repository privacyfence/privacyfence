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
 * through ``handoffDir()`` rather than ``dataDir()`` directly: on a macOS
 * install that has opted into privilege separation, the daemon runs as its
 * own account and its data directory moves to a system location that account
 * owns. The two files this shim reads are exactly the two that stay
 * reachable from the user's session, in ``<system root>/handoff``. See
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

/** The marker file scripts/macos_privilege_separation.sh writes, or null on
 * an install (or a platform) that has no privilege separation. Mirrors
 * privilege_separation.separation(): same default root, same
 * PRIVACYFENCE_SYSTEM_ROOT override, same version and platform checks.
 * Exported only for tests, which need to point it at a temp directory. */
export function privilegeSeparationRoot(env: NodeJS.ProcessEnv = process.env): string | null {
  const override = env.PRIVACYFENCE_SYSTEM_ROOT;
  const root =
    override && path.isAbsolute(override)
      ? override
      : process.platform === "darwin"
        ? "/Library/Application Support/PrivacyFence"
        : null;
  if (!root) return null;
  let marker: { version?: unknown; platform?: unknown };
  try {
    marker = JSON.parse(fs.readFileSync(path.join(root, "privilege-separation.json"), "utf8"));
  } catch {
    // Absent on every unseparated install, which is the common case -- not
    // an error, and deliberately not logged.
    return null;
  }
  if (marker?.version !== 1) return null;
  if (marker?.platform !== process.platform) return null;
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
