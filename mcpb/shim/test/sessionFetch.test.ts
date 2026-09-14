import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { sessionSafeFetch } from "../src/sessionFetch.js";

function respond(status: number, sessionId?: string, body = "{}"): Response {
  const headers = new Headers({ "content-type": "application/json" });
  if (sessionId !== undefined) {
    headers.set("mcp-session-id", sessionId);
  }
  return new Response(body, { status, headers });
}

describe("sessionSafeFetch", () => {
  // The regression: /mcp admits a session, rejects a non-initialize opening
  // frame with 400, discards the session -- and sends the discarded id back
  // on the 400. The client adopts it, and every later request, `initialize`
  // included, is then answered 404 "Session not found". One rejected frame
  // killed the whole connection.
  it("drops the session id from a failed response", async () => {
    const guarded = sessionSafeFetch(async () => respond(400, "dead-session"));
    const response = await guarded("http://localhost/mcp");
    assert.equal(response.headers.get("mcp-session-id"), null);
  });

  it("keeps the session id on a successful response", async () => {
    const guarded = sessionSafeFetch(async () => respond(200, "live-session"));
    const response = await guarded("http://localhost/mcp");
    assert.equal(response.headers.get("mcp-session-id"), "live-session");
  });

  it("preserves status, statusText and body of a failed response", async () => {
    const body = '{"error":{"message":"Bad Request: Missing session ID"}}';
    const guarded = sessionSafeFetch(async () => respond(400, "dead-session", body));
    const response = await guarded("http://localhost/mcp");
    assert.equal(response.status, 400);
    assert.equal(await response.text(), body);
    assert.equal(response.headers.get("content-type"), "application/json");
  });

  it("passes a failed response with no session id through untouched", async () => {
    const original = respond(404);
    const guarded = sessionSafeFetch(async () => original);
    assert.equal(await guarded("http://localhost/mcp"), original);
  });

  it("forwards url and init to the wrapped fetch", async () => {
    const seen: Array<[string | URL, RequestInit | undefined]> = [];
    const guarded = sessionSafeFetch(async (url, init) => {
      seen.push([url, init]);
      return respond(200);
    });
    await guarded("http://localhost/mcp", { method: "POST" });
    assert.equal(seen.length, 1);
    assert.equal(seen[0]![0], "http://localhost/mcp");
    assert.equal(seen[0]![1]?.method, "POST");
  });
});
