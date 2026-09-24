/**
 * A minimal client for the daemon's own control channel -- the Unix domain
 * socket (macOS/Linux) or named pipe (Windows) that ``web/control_channel.
 * py`` listens on, and that ``companion.py`` already speaks
 * ``MINT``/``MINT COMPANION``/``STATUS``/``QUIT`` over. ADR 0008 (``docs/
 * adr/0008-one-principal-per-os-user.md``, D3) is why the shim is a client
 * of it at all: its /mcp bearer token comes from here, not from a file.
 *
 * ``MINT MCP`` is the one command this file speaks. It is the exact same
 * request/response shape every other control-channel client in this
 * codebase already uses (one line out, one line back, ``OK <value>\n`` or
 * ``ERROR <reason>\n`` -- see ``control_channel.py``'s module docstring and
 * ``mint_bootstrap_code()``/``request_status()`` for the Python side of the
 * same idiom), ported to TypeScript for the first time because this is the
 * first TypeScript process in this codebase that has ever needed to be a
 * *client* of this channel rather than its *server* (the daemon) or its
 * *other* server (the companion's own ``CompanionChannelServer``, which the
 * daemon calls into, never the shim).
 *
 * What the daemon does in response to ``MINT MCP`` is the Python side of
 * this ADR, not this file's concern: it resolves the connecting peer's OS
 * account from the kernel's own peer-credential info on this same socket
 * (``SO_PEERCRED``/``LOCAL_PEERCRED``/the named-pipe client process's
 * token) and returns that account's own MCP bearer token, minted once and
 * persisted so a reconnect gets the same value back. This file has no way
 * to know or influence which principal it will be handed a token for --
 * that is exactly the point; the peer credential is read by the kernel, not
 * asserted on the wire the way every other field in this protocol is.
 *
 * A failed mint throws and lets the caller decide what "no mint" means --
 * index.ts exits with a user-facing error, since there is no other source
 * for the token (ADR 0041: only the current install layout is supported).
 */
import net from "node:net";
import { posixControlSocketPath, windowsControlPipeName } from "./protocol.js";

/** How long ``mintMcpToken()`` waits for a connection and a reply before
 * giving up, when the caller doesn't override it. Generous relative to the
 * ordinary case (a local socket round trip on a running daemon answers in
 * well under a second) because the alternative -- giving up too early on a
 * daemon that is merely busy -- makes the shim exit instead of connecting.
 * A daemon that isn't running, or one that refuses the mint, never waits
 * this out: the first refuses the connection and the second answers
 * ``ERROR`` immediately. */
const DEFAULT_TIMEOUT_MS = 5000;

export interface MintMcpTokenOptions {
  /** Overridable for tests; defaults to DEFAULT_TIMEOUT_MS. */
  timeoutMs?: number;
}

/**
 * Raised when the control channel answered but refused or malformed the
 * request -- an ``ERROR <reason>`` reply, or a reply that isn't a
 * well-formed ``OK <token>`` line at all. Mirrors ``control_channel.py``'s
 * own ``ControlChannelError``: the same distinction that module's client
 * functions (``mint_bootstrap_code()`` et al.) draw between "the daemon
 * answered and said no" (this class, there and here) and "nothing was
 * listening at all" (a plain connection error -- ``ENOENT``/``ECONNREFUSED``
 * on POSIX, the pipe-not-found case on Windows -- surfaced as an ordinary
 * ``Error``/``NodeJS.ErrnoException``, never this one). index.ts treats
 * both the same way (the shim exits), but the message differs, since
 * "nothing is listening" and "this install refused this mint for a reason"
 * call for different next steps from whoever reads the log. */
export class ControlChannelError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ControlChannelError";
  }
}

/**
 * Sends ``MINT MCP\n`` over the control channel and returns the token from
 * a well-formed ``OK <token>\n`` reply. Throws ``ControlChannelError`` on an
 * ``ERROR <reason>\n`` reply or any other malformed one; throws a plain
 * connection error (whatever ``net`` raised -- ``ENOENT``, ``ECONNREFUSED``,
 * or this call's own timeout) when nothing answered at all. The caller
 * (index.ts) turns either into a ``ShimExitError``.
 */
export async function mintMcpToken(opts: MintMcpTokenOptions = {}): Promise<string> {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const reply = await sendControlChannelLine("MINT MCP\n", timeoutMs);
  return parseMintReply(reply);
}

/** The pure half of ``mintMcpToken()`` -- parsing a reply line already read
 * off the wire, split out so a test can exercise every reply shape without
 * standing up a fake listener for each one. Mirrors ``control_channel.py``'s
 * own ``mint_bootstrap_code()``/``enrollment_state()``/``request_status()``,
 * which all parse the identical ``OK <value>\n`` / ``ERROR <reason>\n``
 * shape the same way. */
function parseMintReply(reply: string): string {
  const line = reply.replace(/\r?\n$/, "");
  if (line.startsWith("OK ")) {
    return line.slice("OK ".length).trim();
  }
  if (line.toUpperCase().startsWith("ERROR")) {
    const reason = line.slice("ERROR".length).trim();
    throw new ControlChannelError(reason || "the daemon refused to mint an MCP token");
  }
  throw new ControlChannelError(`malformed control channel reply: ${JSON.stringify(reply)}`);
}

/**
 * One request, one line-terminated reply, over whichever address this
 * platform's control channel listens on -- ``posixControlSocketPath()`` on
 * macOS/Linux, ``windowsControlPipeName()`` on Windows. Node's ``net``
 * module speaks both through the exact same API: a Windows named pipe path
 * (``\\.\pipe\...``) is not a TCP/Unix-socket address, but ``net.connect()``
 * recognizes and dials it directly, so unlike the Python side (which needs
 * a whole separate ``pywin32`` code path -- ``control_channel.py``'s
 * ``send_line_windows()``) this file needs exactly one connection routine
 * for every platform, not two.
 *
 * Timeout/cleanup shape follows ``daemon.ts``'s own ``socketConnectable()``:
 * a single ``net.Socket`` with ``setTimeout()`` armed, settling exactly
 * once through whichever of connect/data/timeout/error/close fires first,
 * and always destroying the socket before resolving or rejecting so a
 * caller's own timeout override (index.test.ts's fast ones) never leaves a
 * handle open past the test that set it. ``setTimeout()``'s countdown is
 * reset by any traffic, not just by the initial connect, which is what
 * lets one timeout value bound both "nothing is listening at all" (no
 * connect ever arrives) and "something is listening but never replies"
 * (connects, then goes silent) without this function needing to track
 * elapsed wall-clock time itself.
 */
function sendControlChannelLine(message: string, timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const target =
      process.platform === "win32" ? windowsControlPipeName() : posixControlSocketPath();
    const sock = net.createConnection({ path: target });
    let settled = false;
    let received = "";

    const finish = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      sock.removeAllListeners();
      sock.destroy();
      fn();
    };

    sock.setTimeout(timeoutMs);
    sock.once("connect", () => {
      sock.write(message);
    });
    sock.on("data", (chunk: Buffer) => {
      received += chunk.toString("utf8");
      if (received.includes("\n")) {
        finish(() => resolve(received));
      }
    });
    sock.once("timeout", () => {
      finish(() =>
        reject(new Error(`control channel request timed out after ${timeoutMs}ms (target: ${target})`))
      );
    });
    sock.once("error", (err: Error) => {
      finish(() => reject(err));
    });
    sock.once("close", () => {
      // A listener that accepts the connection and then hangs up without
      // ever sending a newline-terminated line -- distinct from "timeout"
      // (which fires when nothing happens at all) and worth its own message,
      // since a closed-without-answering socket usually means a daemon that
      // is shutting down mid-request rather than one that was never there.
      finish(() => reject(new Error(`control channel connection closed before answering (target: ${target})`)));
    });
  });
}
