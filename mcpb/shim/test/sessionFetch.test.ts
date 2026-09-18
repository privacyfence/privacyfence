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

// The other direction: an id that *was* real, whose session has since gone
// (the daemon restarted, or evicted it). The transport never clears it on a
// 404 -- see sessionFetch.ts's docstring -- so without a retry the connection
// is stuck for the life of the process.
describe("sessionSafeFetch re-homing a stale session id", () => {
  const INIT = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  const PING = JSON.stringify({ jsonrpc: "2.0", id: 0, method: "ping" });

  // `null`, not `undefined`, for "send no session id" -- passing `undefined`
  // to an optional parameter selects its default rather than overriding it.
  function initFor(body: string, sessionId: string | null = "dead-session"): RequestInit {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (sessionId !== null) {
      headers["mcp-session-id"] = sessionId;
    }
    return { method: "POST", headers, body };
  }

  /** A daemon that 404s anything naming a session and serves anything that
   * doesn't -- exactly what a restarted daemon does. */
  function restartedDaemon(attempts: RequestInit[] = []): ReturnType<typeof sessionSafeFetch> {
    return sessionSafeFetch(async (_url, init) => {
      attempts.push(init!);
      return new Headers(init?.headers).has("mcp-session-id")
        ? respond(404, undefined, '{"error":{"message":"Session not found"}}')
        : respond(200, "fresh-session");
    });
  }

  it("retries an initialize without the session id and returns the fresh session", async () => {
    const attempts: RequestInit[] = [];
    const response = await restartedDaemon(attempts)("http://localhost/mcp", initFor(INIT));
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("mcp-session-id"), "fresh-session");
    assert.equal(attempts.length, 2);
    assert.equal(new Headers(attempts[1]!.headers).has("mcp-session-id"), false);
  });

  it("keeps the rest of the request intact on the retry", async () => {
    const attempts: RequestInit[] = [];
    await restartedDaemon(attempts)("http://localhost/mcp", initFor(INIT));
    assert.equal(attempts[1]!.method, "POST");
    assert.equal(attempts[1]!.body, INIT);
    assert.equal(new Headers(attempts[1]!.headers).get("content-type"), "application/json");
  });

  it("retries a batch that contains an initialize", async () => {
    const attempts: RequestInit[] = [];
    const batch = `[${PING},${INIT}]`;
    const response = await restartedDaemon(attempts)("http://localhost/mcp", initFor(batch));
    assert.equal(response.status, 200);
    assert.equal(attempts.length, 2);
  });

  it("does not retry anything but an initialize", async () => {
    const attempts: RequestInit[] = [];
    const response = await restartedDaemon(attempts)("http://localhost/mcp", initFor(PING));
    assert.equal(response.status, 404);
    assert.equal(attempts.length, 1);
  });

  it("does not retry when the request carried no session id", async () => {
    const attempts: RequestInit[] = [];
    const guarded = sessionSafeFetch(async (_url, init) => {
      attempts.push(init!);
      return respond(404);
    });
    await guarded("http://localhost/mcp", initFor(INIT, null));
    assert.equal(attempts.length, 1);
  });

  it("does not retry a non-POST", async () => {
    const attempts: RequestInit[] = [];
    const guarded = sessionSafeFetch(async (_url, init) => {
      attempts.push(init!);
      return respond(404);
    });
    await guarded("http://localhost/mcp", { ...initFor(INIT), method: "DELETE" });
    assert.equal(attempts.length, 1);
  });

  it("does not retry a request with no method set", async () => {
    const attempts: RequestInit[] = [];
    const guarded = sessionSafeFetch(async (_url, init) => {
      attempts.push(init!);
      return respond(404);
    });
    await guarded("http://localhost/mcp", { headers: { "mcp-session-id": "dead" }, body: INIT });
    assert.equal(attempts.length, 1);
  });

  it("does not retry a body it cannot re-send", async () => {
    // Only a string body can be sent twice; a stream is already consumed.
    const attempts: RequestInit[] = [];
    const guarded = sessionSafeFetch(async (_url, init) => {
      attempts.push(init!);
      return respond(404);
    });
    await guarded("http://localhost/mcp", { ...initFor(INIT), body: new Uint8Array([1, 2, 3]) });
    assert.equal(attempts.length, 1);
  });

  it("does not retry an unparseable or empty body", async () => {
    for (const body of ["not json", ""]) {
      const attempts: RequestInit[] = [];
      const guarded = sessionSafeFetch(async (_url, init) => {
        attempts.push(init!);
        return respond(404);
      });
      await guarded("http://localhost/mcp", initFor(body));
      assert.equal(attempts.length, 1);
    }
  });

  it("does not retry anything that is not a 404", async () => {
    const attempts: RequestInit[] = [];
    const guarded = sessionSafeFetch(async (_url, init) => {
      attempts.push(init!);
      return respond(400, "dead-session");
    });
    await guarded("http://localhost/mcp", initFor(INIT));
    assert.equal(attempts.length, 1);
  });

  it("reports the original 404 when the retry fails too", async () => {
    // The daemon's own answer to what was actually asked beats this
    // wrapper's second guess.
    const original = '{"error":{"message":"Session not found"}}';
    const guarded = sessionSafeFetch(async (_url, init) =>
      new Headers(init?.headers).has("mcp-session-id")
        ? respond(404, undefined, original)
        : respond(503, undefined, '{"error":{"message":"Too many open sessions"}}'),
    );
    const response = await guarded("http://localhost/mcp", initFor(INIT));
    assert.equal(response.status, 404);
    assert.equal(await response.text(), original);
  });
});
