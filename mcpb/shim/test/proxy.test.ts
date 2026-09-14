import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { pendingRequestId, proxyTransports } from "../src/proxy.js";

/** A minimal hand-rolled Transport double -- proxy.ts's only real dependency
 * is the shared Transport shape (send/onmessage/onerror), so a fake this
 * small is enough to prove the wiring without any real I/O. */
class FakeTransport implements Transport {
  sent: JSONRPCMessage[] = [];
  onmessage?: (message: JSONRPCMessage) => void;
  onerror?: (error: Error) => void;
  onclose?: () => void;
  sendShouldReject = false;

  async start(): Promise<void> {}

  async send(message: JSONRPCMessage): Promise<void> {
    if (this.sendShouldReject) {
      throw new Error("send failed");
    }
    this.sent.push(message);
  }

  async close(): Promise<void> {
    this.onclose?.();
  }

  receive(message: JSONRPCMessage): void {
    this.onmessage?.(message);
  }
}

const REQUEST: JSONRPCMessage = { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} };
const RESPONSE: JSONRPCMessage = { jsonrpc: "2.0", id: 1, result: { tools: [] } };

describe("proxyTransports", () => {
  it("forwards a message from the desktop side to the daemon side", () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);

    desktop.receive(REQUEST);

    assert.deepEqual(daemon.sent, [REQUEST]);
    assert.deepEqual(desktop.sent, []);
  });

  it("forwards a message from the daemon side to the desktop side", () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);

    daemon.receive(RESPONSE);

    assert.deepEqual(desktop.sent, [RESPONSE]);
    assert.deepEqual(daemon.sent, []);
  });

  it("does not cross-wire a side to itself", () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);

    desktop.receive(REQUEST);
    daemon.receive(RESPONSE);

    assert.deepEqual(daemon.sent, [REQUEST]);
    assert.deepEqual(desktop.sent, [RESPONSE]);
  });

  it("routes a forwarding failure to the source side's onerror, not a thrown/unhandled rejection", async () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    daemon.sendShouldReject = true;
    proxyTransports(desktop, daemon);

    let captured: Error | undefined;
    desktop.onerror = (err) => {
      captured = err;
    };

    desktop.receive(REQUEST);
    // send() rejects asynchronously; let the microtask queue drain.
    await Promise.resolve();
    await Promise.resolve();

    assert.ok(captured);
    assert.match(captured!.message, /send failed/);
  });
});

const NOTIFICATION: JSONRPCMessage = { jsonrpc: "2.0", method: "notifications/initialized" };

describe("pendingRequestId", () => {
  it("returns the id of a request, which something is waiting on", () => {
    assert.equal(pendingRequestId(REQUEST), 1);
  });

  it("returns undefined for a notification -- no id, nothing waiting", () => {
    assert.equal(pendingRequestId(NOTIFICATION), undefined);
  });

  it("returns undefined for a response -- answering it would invent traffic", () => {
    assert.equal(pendingRequestId(RESPONSE), undefined);
  });

  it("accepts a string id, which JSON-RPC allows as well as a number", () => {
    assert.equal(pendingRequestId({ jsonrpc: "2.0", id: "abc", method: "tools/list" }), "abc");
  });
});

describe("a forward that fails", () => {
  // The regression this exists for: /mcp answers a session-opening request
  // that is not `initialize` with 400 -- correctly -- and that 400 used to
  // be written to stderr and otherwise dropped. The host was left waiting on
  // a response that would never come until its own timeout fired, with the
  // daemon's log showing a session opened and closed and no request in it.
  it("answers the asking side with a JSON-RPC error carrying the pending id", async () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);
    daemon.sendShouldReject = true;

    desktop.receive(REQUEST);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(desktop.sent.length, 1);
    const reply = desktop.sent[0] as { id: unknown; error: { code: number; message: string } };
    assert.equal(reply.id, 1);
    assert.equal(reply.error.code, -32603);
    assert.match(reply.error.message, /could not forward this request/);
    assert.match(reply.error.message, /send failed/);
  });

  it("still reports the failure through onerror", async () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    const seen: Error[] = [];
    proxyTransports(desktop, daemon);
    desktop.onerror = (err) => seen.push(err);
    daemon.sendShouldReject = true;

    desktop.receive(REQUEST);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(seen.length, 1);
    assert.match(seen[0]!.message, /send failed/);
  });

  it("does not answer a notification -- nothing is waiting on one", async () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);
    daemon.sendShouldReject = true;

    desktop.receive(NOTIFICATION);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(desktop.sent.length, 0);
  });

  it("answers a daemon-side request the same way, in the other direction", async () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);
    desktop.sendShouldReject = true;

    daemon.receive(REQUEST);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(daemon.sent.length, 1);
    assert.equal((daemon.sent[0] as { id: unknown }).id, 1);
  });
});
