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
 * The one thing it does read out of a message is whether that message is a
 * *request* -- has both a ``method`` and an ``id``, so the far side is
 * blocked waiting for exactly one response carrying that id. That is
 * JSON-RPC envelope framing, the same layer both transports already parse;
 * it is not knowledge of any MCP method, manifest or tool schema, and
 * nothing here inspects or depends on what the request actually asks for.
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

/** Forwards ``from``'s messages to ``to``, answering ``from`` with a JSON-RPC
 * error if a forward fails and something is waiting on it. */
function forwardMessages(from: MessageTransport, to: MessageTransport): void {
  from.onmessage = (message) => {
    to.send(message).catch((exc: unknown) => {
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
      from.send(failure).catch((replyExc: unknown) => from.onerror?.(toError(replyExc)));
    });
  };
}

/** Installs ``onmessage`` on both sides. Must be called before either side's
 * ``start()`` -- see the ``Transport`` interface's own doc comment: "This
 * method should only be called after callbacks are installed, or else
 * messages may be lost." */
export function proxyTransports(desktopSide: MessageTransport, daemonSide: MessageTransport): void {
  forwardMessages(desktopSide, daemonSide);
  forwardMessages(daemonSide, desktopSide);
}
