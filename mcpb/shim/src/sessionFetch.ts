/**
 * A ``fetch`` wrapper that stops a dead Streamable HTTP session id from
 * killing this connection. It handles the two ways that happens, in the two
 * directions they travel.
 *
 * **Inbound: an id the client was never really given.**
 *
 * Streamable HTTP requires the request that opens a session to be
 * ``initialize``. When a client opens with anything else, the daemon's
 * session manager nonetheless admits a session (it allocates the id before it
 * has parsed the body far enough to know better), answers 400, and then
 * discards that session again -- correct, except that the 400 goes out
 * carrying the discarded session's ``Mcp-Session-Id``:
 *
 *     HTTP/1.1 400 Bad Request
 *     mcp-session-id: a0a7c16026ec4cafb3ccbe9e67a3dcd5
 *     {"error":{"code":-32600,"message":"Bad Request: Missing session ID"}}
 *
 * The MCP client transport reads that header off *every* response, before it
 * checks whether the response succeeded, so it adopts the id of a session that
 * no longer exists. From then on it stamps that dead id on everything it
 * sends, and the daemon answers every one of them -- ``initialize`` very much
 * included -- with 404 "Session not found":
 *
 *     POST /mcp  Mcp-Session-Id: a0a7c16...  {"method":"initialize",...}
 *     HTTP/1.1 404 Not Found   {"error":{"message":"Session not found"}}
 *
 * That is the part that turns one rejected frame into a dead connection. A
 * host that opens with a probe and then initializes properly -- which is a
 * reasonable thing to do, and what a rejected opening frame invites -- can
 * never recover, for the whole life of this process: its correct
 * ``initialize`` is refused because of the failure that preceded it.
 *
 * Dropping the header from any non-2xx response is enough. A session id is
 * only ever meaningful on a response that established or used a session; on a
 * failure it names, at best, something already gone. Successful responses are
 * passed through untouched, so normal session handling is unaffected.
 *
 * **Outbound: an id that was real and has since died.**
 *
 * The above prevents adopting a bad id; it cannot help a transport that holds
 * a perfectly legitimate one whose session is gone -- the daemon restarted, or
 * it evicted the session. Every request then earns a 404, and the transport
 * does not recover on its own: ``StreamableHTTPClientTransport`` throws
 * ``StreamableHTTPError`` on a 404 (it special-cases 401 and 403 only), and it
 * clears its ``_sessionId`` nowhere except in ``terminateSession()`` -- which
 * throws on the 404 that a dead session's own DELETE earns, before reaching
 * the line that would have cleared it. The id is stuck, and so is everything
 * after it.
 *
 * So a 404 to a POST that carried a session id is retried once with that
 * header removed. The daemon treats a session-less POST as a new session, so
 * the retried ``initialize`` succeeds, and the transport adopts the fresh id
 * off that 2xx by the very same read-every-response behavior that caused the
 * inbound problem above. The connection re-homes itself with nothing asked of
 * the host.
 *
 * Deliberately narrow, on three counts:
 *
 * - **Only ``initialize`` is retried.** ``web/routes_mcp.py``'s own
 *   ``_RehomeStaleInitialize`` re-homes exactly the same frame and nothing
 *   else, for the reason given there: a server session that never saw
 *   ``initialize`` refuses any other frame anyway, so retrying one would swap
 *   a clear 404 for a confusing 400. Reading ``method`` off the envelope is
 *   the same JSON-RPC framing ``proxy.ts`` already reads ``id`` and ``method``
 *   from -- not knowledge of any MCP method or tool schema.
 * - **Only a string body is retried**, because only that can be re-sent
 *   safely. The SDK always sends ``JSON.stringify(message)``; a stream would
 *   already be consumed.
 * - **Only a successful retry replaces the original**, so a daemon that
 *   refuses the retry for some other reason still reports its own answer to
 *   the original request rather than this wrapper's second guess.
 *
 * A daemon carrying ``_RehomeStaleInitialize`` already strips the stale id
 * server-side, which makes the retry redundant there. It is here for the
 * shim-newer-than-daemon case -- the same compatibility reason the inbound
 * half above exists on this side at all.
 */
import type { FetchLike } from "@modelcontextprotocol/sdk/shared/transport.js";

const MCP_SESSION_ID_HEADER = "mcp-session-id";

/** Whether this request body is (or, for a batch, contains) an
 * ``initialize`` request -- the only frame worth retrying without a session
 * id. Never throws: an unparseable body is simply not retried. */
function isInitialize(body: unknown): boolean {
  if (typeof body !== "string" || body.length === 0) {
    return false;
  }
  let frame: unknown;
  try {
    frame = JSON.parse(body);
  } catch {
    return false;
  }
  const namesInitialize = (value: unknown): boolean =>
    typeof value === "object" && value !== null && (value as { method?: unknown }).method === "initialize";
  return Array.isArray(frame) ? frame.some(namesInitialize) : namesInitialize(frame);
}

/** Discards a response body we are not going to hand back, so the underlying
 * connection is released rather than left dangling. Failure here is nothing
 * the caller can act on. */
async function discard(response: Response): Promise<void> {
  await response.body?.cancel().catch(() => undefined);
}

/**
 * The same request with ``Mcp-Session-Id`` removed, or ``undefined`` when
 * this request is not one to retry (no session id, not a POST, not a string
 * body, or not an ``initialize``).
 */
async function retryWithoutSessionId(
  inner: FetchLike,
  url: string | URL,
  init: RequestInit | undefined,
): Promise<Response | undefined> {
  const headers = new Headers(init?.headers);
  if (!headers.has(MCP_SESSION_ID_HEADER)) {
    return undefined;
  }
  if ((init?.method ?? "GET").toUpperCase() !== "POST" || !isInitialize(init?.body)) {
    return undefined;
  }
  headers.delete(MCP_SESSION_ID_HEADER);
  return await inner(url, { ...init, headers });
}

export function sessionSafeFetch(inner: FetchLike = fetch): FetchLike {
  return async (url, init) => {
    let response = await inner(url, init);

    if (response.status === 404) {
      const retried = await retryWithoutSessionId(inner, url, init);
      if (retried !== undefined) {
        if (retried.ok) {
          await discard(response);
          response = retried;
        } else {
          await discard(retried);
        }
      }
    }

    if (response.ok || !response.headers.has(MCP_SESSION_ID_HEADER)) {
      return response;
    }
    const headers = new Headers(response.headers);
    headers.delete(MCP_SESSION_ID_HEADER);
    // Rebuilt rather than mutated: a Response's own headers are immutable.
    // The body is passed through as the original stream, not read here --
    // whatever consumes this response still gets the daemon's own error text.
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  };
}
