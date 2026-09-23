/**
 * The shim's actual job: a message pump between two MCP ``Transport``
 * objects. Whatever ``desktopSide`` (Claude Desktop's stdio connection to
 * this process) receives is forwarded to ``daemonSide`` (the outbound
 * Streamable HTTP connection to /mcp), and vice versa.
 *
 * Deliberately *not* built on the SDK's ``Client``/``Server`` classes: those
 * re-run the initialize handshake and cache tool schemas on this process,
 * which is exactly the "protocol/manifest/tool-schema knowledge" the shim
 * must not carry -- it is what keeps the class of bug bridge/test/manifest.test.ts and
 * tests/integration/test_bridge_daemon_contract.py exist to catch (one
 * side's wire format drifting from the other's) structurally impossible for
 * this transport, rather than something the shim also has to get right.
 * Both ``Transport`` implementations already do their own JSON-RPC framing
 * (line-delimited stdio / Streamable HTTP's SSE-or-JSON) -- this only moves
 * the decoded ``JSONRPCMessage`` values between them.
 *
 * The one thing it does read out of a message on its own account is whether
 * that message is a *request* -- has both a ``method`` and an ``id``, so the
 * far side is blocked waiting for exactly one response carrying that id.
 * That is JSON-RPC envelope framing, the same layer both transports already
 * parse; it is not knowledge of any MCP method, manifest or tool schema, and
 * nothing in this file inspects or depends on what a request actually asks
 * for.
 *
 * It is needed because forwarding can fail, and a failed forward used to be
 * reported only through ``onerror`` -- which writes a line to this process's
 * stderr and nothing else. The request itself was simply dropped: no
 * response, no error, no end of stream. A host that sent a request got
 * silence and sat there until its own timeout expired, while the daemon's
 * log showed only a session opened and closed with no request in it. Both
 * ends looked healthy and neither said why.
 *
 * The case that surfaced this: an MCP host may open a connection with
 * something other than ``initialize`` (a capability probe, or a pooled
 * startup path bringing a server up for background sessions). Streamable
 * HTTP requires the session-opening request to be ``initialize``, so /mcp
 * answers anything else with 400 -- correctly. That 400 is a perfectly good
 * answer, and the host is entitled to receive it. Swallowing it turns a
 * clean, immediate protocol error into an indefinite hang.
 *
 * So a forward that fails now sends a JSON-RPC error response back to
 * whichever side asked, carrying the id it is waiting on. Notifications and
 * responses have no id and nothing waiting on them, so for those a failure
 * stays log-only, as before.
 *
 * **One bounded exception to "no protocol knowledge": the local file
 * bridge (ADR 0007, ``docs/adr/0007-local-file-bridge.md``).** ``proxyTransports``
 * takes an optional ``interceptor`` that gets a look at the ``tools/call``
 * traffic flowing each way -- not to understand it, but because privilege
 * separation (ADR 0003) leaves the daemon unable to read or write the real
 * user's files, so the shim (which runs as that user) has to do certain
 * reads/writes on its behalf, keyed off a fixed ``_meta`` vendor key the
 * daemon attaches to specific responses. That knowledge lives entirely in
 * fileBridge.ts's ``createFileBridge`` -- this module still does not parse
 * tool names, arguments or schemas itself. What changes here is mechanical:
 * ``onDesktopRequest`` gets a look at a request before it is forwarded (it
 * cannot change or drop it), and ``onDaemonMessage`` gets to decide, for a
 * message that would otherwise go straight to the desktop, whether to
 * forward it (as given, or rewritten), resend a request back to the daemon
 * instead (the upload handshake's second round), or both. With no
 * interceptor passed, ``proxyTransports`` behaves exactly as it always has
 * -- every existing caller and test is unaffected.
 */
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";

export interface MessageTransport {
  send(message: JSONRPCMessage): Promise<void>;
  onmessage?: (message: JSONRPCMessage) => void;
  onerror?: (error: Error) => void;
}

/** JSON-RPC's own "Internal error" code. The failure being reported is this
 * proxy's inability to deliver the request at all, which is neither a
 * malformed request nor an unknown method -- the far side never got far
 * enough to judge either. */
const INTERNAL_ERROR = -32603;

function toError(exc: unknown): Error {
  return exc instanceof Error ? exc : new Error(String(exc));
}

/**
 * The id a response must carry to satisfy ``message``, or ``undefined`` when
 * nothing is waiting on it. A JSON-RPC request has both a ``method`` and an
 * ``id``; a notification has a ``method`` and no ``id``; a response has an
 * ``id`` and no ``method`` (answering it again would be inventing traffic).
 */
export function pendingRequestId(message: JSONRPCMessage): string | number | undefined {
  if (typeof message !== "object" || message === null) {
    return undefined;
  }
  const { id, method } = message as { id?: unknown; method?: unknown };
  if (typeof method !== "string") {
    return undefined;
  }
  return typeof id === "string" || typeof id === "number" ? id : undefined;
}

/** Delivers ``message`` to ``to``, answering ``from`` with a JSON-RPC error
 * if the send fails and something is waiting on it (same "who's blocked on
 * this id" reasoning as the module docstring). Factored out of
 * ``forwardMessages`` so the interceptor path below can reuse the exact same
 * failure handling in both directions it sends on -- forwarding to the
 * desktop, and resending to the daemon. */
async function deliver(from: MessageTransport, to: MessageTransport, message: JSONRPCMessage): Promise<void> {
  try {
    await to.send(message);
  } catch (exc: unknown) {
    const error = toError(exc);
    from.onerror?.(error);
    const id = pendingRequestId(message);
    if (id === undefined) {
      return;
    }
    const failure: JSONRPCMessage = {
      jsonrpc: "2.0",
      id,
      error: {
        code: INTERNAL_ERROR,
        message: `PrivacyFence shim could not forward this request to the daemon: ${error.message}`,
      },
    };
    // A failure reporting the failure leaves nothing further to try: the
    // sender is unreachable too, so this can only be logged.
    await from.send(failure).catch((replyExc: unknown) => from.onerror?.(toError(replyExc)));
  }
}

/** Forwards ``from``'s messages to ``to`` unchanged. ``tap``, when given, is
 * called with every message before it is forwarded -- a read-only look, not
 * a filter: it cannot change or drop what gets sent. Used for
 * ``onDesktopRequest`` below, which only needs to remember a request, not
 * intervene in its delivery. */
function forwardMessages(from: MessageTransport, to: MessageTransport, tap?: (message: JSONRPCMessage) => void): void {
  from.onmessage = (message) => {
    tap?.(message);
    void deliver(from, to, message);
  };
}

/**
 * The file bridge's hook into this pump -- see the module docstring's "One
 * bounded exception" paragraph. ``fileBridge.ts``'s ``createFileBridge``
 * returns an object shaped like this.
 */
export interface ProxyInterceptor {
  /** Called with every message flowing desktop -> daemon, before it is
   * forwarded. Cannot change or drop it -- same "tap, not filter" contract
   * as ``forwardMessages``'s own ``tap`` parameter. */
  onDesktopRequest(message: JSONRPCMessage): void;
  /** Called with every message flowing daemon -> desktop, before it is
   * forwarded. Unlike ``onDesktopRequest``, this one *does* decide what
   * happens next: ``forwardToDesktop`` sends that message on to the desktop
   * side (the original message, unchanged, for anything the bridge doesn't
   * care about); ``resendToDaemon`` instead sends a request back to the
   * daemon (the upload handshake's second round) without the desktop side
   * ever seeing this intermediate message. Both may be set (a resend does
   * not preclude also answering the desktop directly, though nothing does
   * that today); neither set means the message is dropped -- also unused
   * today, but a decision this function is free to make. */
  onDaemonMessage(
    message: JSONRPCMessage,
  ): Promise<{ forwardToDesktop?: JSONRPCMessage; resendToDaemon?: JSONRPCMessage }>;
}

/** Like ``forwardMessages``, but every message is first run through
 * ``intercept`` and the pump does whatever it decides instead of a plain
 * forward. ``resendToDaemon`` is delivered with ``desktopSide`` as the
 * failure-reporting party (``deliver``'s first argument): the desktop is
 * the side actually blocked on that request's id, even though this exact
 * message never came from it -- so a failed resend is reported the same way
 * a failed ordinary forward would be, to the party left waiting. */
function forwardWithInterceptor(
  daemonSide: MessageTransport,
  desktopSide: MessageTransport,
  intercept: ProxyInterceptor["onDaemonMessage"],
): void {
  daemonSide.onmessage = (message) => {
    void intercept(message)
      .then(async (decision) => {
        if (decision.resendToDaemon !== undefined) {
          await deliver(desktopSide, daemonSide, decision.resendToDaemon);
        }
        if (decision.forwardToDesktop !== undefined) {
          await deliver(daemonSide, desktopSide, decision.forwardToDesktop);
        }
      })
      .catch((exc: unknown) => daemonSide.onerror?.(toError(exc)));
  };
}

/** Installs ``onmessage`` on both sides. Must be called before either side's
 * ``start()`` -- see the ``Transport`` interface's own doc comment: "This
 * method should only be called after callbacks are installed, or else
 * messages may be lost."
 *
 * With no ``interceptor``, this is unchanged from before the file bridge
 * existed: both directions are plain, unmediated forwards. Passing one adds
 * the file bridge's read on desktop -> daemon traffic and its decision-making
 * on daemon -> desktop traffic -- see ``ProxyInterceptor``'s own doc comment
 * and the module docstring. */
export function proxyTransports(
  desktopSide: MessageTransport,
  daemonSide: MessageTransport,
  interceptor?: ProxyInterceptor,
): void {
  forwardMessages(desktopSide, daemonSide, interceptor && ((message) => interceptor.onDesktopRequest(message)));
  if (interceptor) {
    forwardWithInterceptor(daemonSide, desktopSide, (message) => interceptor.onDaemonMessage(message));
  } else {
    forwardMessages(daemonSide, desktopSide);
  }
}
