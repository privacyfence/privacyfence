/**
 * A ``fetch`` wrapper that stops a *failed* /mcp response from re-homing this
 * connection's Streamable HTTP session.
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
 */
import type { FetchLike } from "@modelcontextprotocol/sdk/shared/transport.js";

const MCP_SESSION_ID_HEADER = "mcp-session-id";

export function sessionSafeFetch(inner: FetchLike = fetch): FetchLike {
  return async (url, init) => {
    const response = await inner(url, init);
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
