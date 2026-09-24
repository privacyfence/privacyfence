/**
 * PrivacyFence .mcpb shim: a thin stdio-to-Streamable-HTTP transport proxy,
 * spawned by Claude Desktop exactly the way it used to spawn
 * privacyfence-bridge (mcpb/manifest.json.tmpl's server.mcp_config -- same
 * shape, only the staged file changed, see scripts/build_mcpb.sh). This is
 * what replaces the bridge for Desktop once tool calls go over /mcp (P2)
 * instead of the IPC socket.
 *
 * Unlike the bridge (bridge/src/index.ts), this process has no knowledge of
 * ToolSpec, no manifest fetch, no tool registration, and no JSON-RPC framing
 * of its own -- see proxy.ts's module docstring for why that's a deliberate
 * design constraint, not an oversight. Its whole job:
 *
 * 1. Wait for the daemon's /mcp endpoint to be reachable, launching
 *    privacyfence-app first if it isn't running yet (daemon.ts -- the one
 *    piece of the bridge whose job survives verbatim, per D11).
 * 2. Get a bearer token for /mcp: mint one over the daemon's control
 *    channel (controlChannel.ts's ``mintMcpToken()``, ``MINT MCP\n``), which
 *    resolves to *this OS account's own* token (ADR 0008, ``docs/adr/
 *    0008-one-principal-per-os-user.md``, D3) -- and fall back to reading
 *    the legacy shared ``mcp_token`` file (protocol.ts's ``readMcpToken()``,
 *    today's only path) when that mint fails for any reason. The only two
 *    installs that fall back are an unseparated one (which has exactly one
 *    principal anyway, so the shared file and this OS account's own token
 *    are the same value) and one running a daemon too old to answer ``MINT
 *    MCP`` at all -- ADR 0007's own stale-shim posture, just facing the
 *    other direction: an old daemon talking to a shim new enough to ask it
 *    something it has never heard of, degrading to old behavior rather than
 *    failing outright. Read the daemon's current /mcp URL from its own
 *    discovery file the same way as before (web/server.py's ``mcp_url``).
 * 3. Open a Streamable HTTP client connection to /mcp, authenticated by
 *    that bearer token, advertising the local file bridge via the
 *    ``X-PrivacyFence-File-Bridge`` header.
 * 4. Proxy MCP frames between that connection and this process's own stdio
 *    transport (proxy.ts) -- Claude Desktop can now call tools, exactly as
 *    it could through the bridge.
 *
 * One exception to "no protocol knowledge" (ADR 0007, ``docs/adr/
 * 0007-local-file-bridge.md``): fileBridge.ts's local file bridge, which
 * reads and writes local files on the daemon's behalf now that privilege
 * separation (ADR 0003) leaves the daemon unable to reach the real user's
 * home directory itself. It is wired into step 4's proxy as an
 * ``interceptor`` -- see proxy.ts's module docstring for exactly how
 * bounded that exception is.
 *
 * Logs go to stderr only (stdout is the MCP protocol channel) -- see
 * setupLogging(), same reasoning as bridge_main.py's original stderr-only
 * logging.StreamHandler setup.
 */

import { pathToFileURL } from "node:url";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { mintMcpToken as mintMcpTokenReal } from "./controlChannel.js";
import { waitForDaemonPatiently } from "./daemon.js";
import { ShimExitError } from "./errors.js";
import { createFileBridge, FILE_BRIDGE_HEADER } from "./fileBridge.js";
import { MCP_TOKEN_FILE, MCP_URL_FILE, readMcpToken, readMcpUrl } from "./protocol.js";
import { proxyTransports } from "./proxy.js";
import { sessionSafeFetch } from "./sessionFetch.js";

/**
 * Redirect console.log/info/debug/warn to stderr. stdout is the MCP wire
 * channel (StdioServerTransport owns it); a stray console.log from this
 * code or a dependency would corrupt the protocol stream, so every logging
 * path is forced through stderr instead -- identical to bridge/src/
 * index.ts's setupLogging().
 */
function setupLogging(): void {
  console.log = console.error;
  console.info = console.error;
  console.debug = console.error;
  console.warn = console.error;
}

/** Logs the flags this process was spawned with. ``--config`` is
 * daemon-side only, accepted here for CLI compatibility with how the bridge
 * was invoked; anything else is *ignored*, not rejected.
 *
 * Rejecting used to mean throwing out of main(), which exits before
 * desktopSide.start() has read a single byte of stdin. The failure that
 * produces is invisible from both sides at once: the host sees a server
 * process that started and then died without ever answering ``initialize``
 * (it reports a timeout, not a crash, since the spawn itself succeeded),
 * and because the shim never got as far as opening its ``/mcp`` connection,
 * the daemon's own log records nothing whatsoever -- no session, no
 * request, no error. A user reading privacyfence.log to find out why Claude
 * "cannot connect" finds an idle, healthy daemon and no trace of the
 * attempt.
 *
 * An MCP host owns the argv of the servers it spawns and may add flags of
 * its own at any time -- the same ``.mcpb`` launched a second way by a
 * newer client is the case this was found on. Refusing to start over an
 * argument this transport proxy has no use for anyway trades a working
 * connection for exactly that silent failure, so unrecognized flags are
 * logged to stderr (which the host captures into its own server log) and
 * otherwise ignored. */
export function parseArgs(argv: string[]): void {
  const ignored: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--config") {
      i++; // consume the value
      continue;
    }
    if (arg?.startsWith("--config=")) {
      continue;
    }
    if (arg !== undefined) {
      ignored.push(arg);
    }
  }
  if (ignored.length > 0) {
    console.error(`Ignoring unrecognized argument(s): ${ignored.join(" ")}`);
  }
}

export interface MainOptions {
  /** Overridable for tests; defaults to the real <data dir>/mcp_url (see protocol.ts). */
  mcpUrlFile?: string;
  /** Overridable for tests; defaults to the real <data dir>/mcp_token (see protocol.ts). */
  mcpTokenFile?: string;
  /** Overridable for tests (e.g. a fake resolve/reject) so index.test.ts can
   * exercise both the "mint succeeds, the token file is never read" and
   * "mint fails, fall back to the token file exactly as before" paths
   * without touching a real socket/pipe. Defaults to the real
   * controlChannel.ts ``mintMcpToken()`` with its own default timeout (see
   * the note above ``getMcpToken()`` for why this call site no longer
   * shortens it). */
  mintMcpToken?: () => Promise<string>;
  /** Overridable for tests (e.g. a fake Transport); defaults to a real
   * StreamableHTTPClientTransport pointed at the discovered mcp_url. */
  daemonTransport?: Transport;
  /** Overridable for tests (e.g. InMemoryTransport); defaults to real stdio. */
  transport?: Transport;
  /** Overridable for tests; defaults to waiting on stdin closing (real Claude Desktop disconnect). */
  waitForDisconnect?: () => Promise<void>;
}

// The mint deliberately runs with controlChannel.ts's own default timeout.
// This call site used to cut it to 1s, reasoning that only a daemon too old
// to know MINT MCP, or one that isn't running, could be slow here, and that
// both should fail fast. Neither is ever slow: an old daemon answers
// ``ERROR unknown command`` at once, and a missing one refuses the
// connection at once. The only thing a timeout ever catches is a daemon
// that is up but busy -- the control channel serves one connection at a
// time, and resolves each peer's account via getpwuid(), which on macOS is
// a directory-service round trip -- and the 1s cut turned exactly that case
// into a fallback onto a legacy token file that a separated install no
// longer has, so the shim exited instead of waiting. That is what failed
// v4.3.0's packaged macOS smoke test on its first attempt. The daemon's own
// Python client (control_channel.mint_mcp_token()) already waits 5s for
// the same call.

function defaultWaitForDisconnect(): Promise<void> {
  return new Promise<void>((resolve) => {
    process.stdin.once("close", resolve);
    process.stdin.once("end", resolve);
  });
}

/**
 * ADR 0008 D3's fallback posture, as one function: mint this OS account's
 * own MCP token over the control channel, and only fall back to reading the
 * legacy shared ``mcp_token`` file when the mint itself fails, for any
 * reason -- a plain connection error (no daemon listening yet, or one old
 * enough to have never heard of ``MINT MCP``) and a ``ControlChannelError``
 * (the daemon answered but refused) are treated identically here, because
 * both mean "this call bought nothing" and the file is the only fallback
 * either way. The one thing that differs between them is the stderr line
 * logged before falling back -- ``err.message`` already carries that
 * distinction (controlChannel.ts's own ``ControlChannelError`` vs. a plain
 * ``Error``/``NodeJS.ErrnoException``), so this function does not need a
 * second branch to say something different for each; it needs one line
 * that makes *why* legible to whoever reads privacyfence.log next, which a
 * silent fallback would not.
 */
async function getMcpToken(opts: MainOptions, mcpTokenFile: string): Promise<string> {
  const mint = opts.mintMcpToken ?? (() => mintMcpTokenReal());
  try {
    return await mint();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(
      `Could not mint an MCP token over the control channel (${message}); falling back to the legacy token file`
    );
    return readMcpToken(mcpTokenFile);
  }
}

export async function main(argv = process.argv.slice(2), opts: MainOptions = {}): Promise<void> {
  setupLogging();
  parseArgs(argv);

  const mcpUrlFile = opts.mcpUrlFile ?? MCP_URL_FILE;
  const mcpTokenFile = opts.mcpTokenFile ?? MCP_TOKEN_FILE;
  await waitForDaemonPatiently({ mcpUrlFile });

  const mcpUrl = readMcpUrl(mcpUrlFile);
  const mcpToken = await getMcpToken(opts, mcpTokenFile);
  console.error(`Proxying stdio <-> ${mcpUrl}`);

  const authHeader = `Bearer ${mcpToken}`;
  const daemonSide =
    opts.daemonTransport ??
    new StreamableHTTPClientTransport(new URL(mcpUrl), {
      requestInit: {
        headers: {
          Authorization: authHeader,
          // Tells the daemon this shim can carry out the local file bridge
          // handshake (ADR 0007) -- without it, local_files.py never offers
          // need_uploads/deliver and instead falls back to the no-bridge
          // messaging (a plain client, or an old .mcpb, gets that fallback
          // instead of a silently-never-arriving upload prompt).
          [FILE_BRIDGE_HEADER]: "1",
        },
      },
      // Keeps a rejected request from leaving this connection pinned to a
      // session the daemon has already discarded -- see sessionFetch.ts.
      fetch: sessionSafeFetch(),
    });
  const desktopSide = opts.transport ?? new StdioServerTransport();

  // Same origin /mcp itself is on, and the same bearer token already used
  // for it -- the file bridge's uploads/downloads endpoints sit behind the
  // same auth as /mcp (routes_file_bridge.py), and Phase 1 has no second
  // credential of its own to read (fileBridge.ts's own docstring).
  const fileBridge = createFileBridge({ origin: new URL(mcpUrl).origin, authHeader });

  // Must run before either side's start() -- see proxy.ts's own doc comment.
  proxyTransports(desktopSide, daemonSide, fileBridge);

  let closed = false;
  const closeBoth = async (): Promise<void> => {
    if (closed) return;
    closed = true;
    await Promise.allSettled([desktopSide.close(), daemonSide.close()]);
  };
  // Either side dropping (Claude Desktop closing stdin, or /mcp becoming
  // unreachable mid-session) tears down the other -- there is no partial
  // proxy state worth keeping alive.
  desktopSide.onclose = () => {
    void closeBoth();
  };
  daemonSide.onclose = () => {
    void closeBoth();
  };
  desktopSide.onerror = (err) => console.error("stdio side error:", err);
  daemonSide.onerror = (err) => console.error("/mcp side error:", err);

  await daemonSide.start();
  await desktopSide.start();

  const waitForDisconnect = opts.waitForDisconnect ?? defaultWaitForDisconnect;
  try {
    await waitForDisconnect();
  } finally {
    await closeBoth();
  }
}

// Only auto-run when this module is the actual entry point (the bundled
// dist/shim.js Claude Desktop spawns, or `node`/`tsx src/index.ts` in dev)
// -- not when index.test.ts imports main() directly to drive it in an
// in-process integration test.
const isEntryPoint = process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;

if (isEntryPoint) {
  main().catch((exc: unknown) => {
    // ShimExitError carries its own fully-formatted, user-facing message
    // (see daemon.ts) -- print it plainly, no "Error:" prefix/stack trace,
    // matching the bridge's equivalent BridgeExitError handling.
    if (exc instanceof ShimExitError) {
      console.error(exc.message);
      process.exit(exc.code);
    }
    console.error(exc instanceof Error ? (exc.stack ?? exc.message) : String(exc));
    process.exit(1);
  });
}
