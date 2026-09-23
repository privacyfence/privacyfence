/**
 * Tests for fileBridge.ts -- the shim's own half of the local file bridge
 * (ADR 0007 SS1.1/SS1.6). Covers the pure guards directly (G1's
 * pathIsDeclaredInArguments, the basename/no-overwrite-rename helpers) and
 * both wire-protocol flows through `createFileBridge`'s hooks, against a
 * fake `fetch` -- no real daemon or network involved, see
 * test/fakeMcpDaemon.ts / test_shim_mcp_contract.py for the cross-language
 * proof that the real wire format agrees with what's exercised here.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import {
  createFileBridge,
  expandHome,
  isSafeBasename,
  pathIsDeclaredInArguments,
  pickNonCollidingName,
  setAtJsonPointer,
} from "../src/fileBridge.js";

describe("expandHome", () => {
  it("expands a bare ~", () => {
    assert.equal(expandHome("~", "/home/alice"), "/home/alice");
  });
  it("expands ~/...", () => {
    assert.equal(expandHome("~/report.pdf", "/home/alice"), path.join("/home/alice", "report.pdf"));
  });
  it("leaves an already-absolute path alone", () => {
    assert.equal(expandHome("/tmp/x", "/home/alice"), "/tmp/x");
  });
  it("does not expand ~otheruser", () => {
    assert.equal(expandHome("~bob/x", "/home/alice"), "~bob/x");
  });
});

describe("pathIsDeclaredInArguments", () => {
  it("finds a top-level string argument", () => {
    assert.ok(pathIsDeclaredInArguments("~/report.pdf", { local_path: "~/report.pdf" }));
  });
  it("finds a nested string argument", () => {
    assert.ok(pathIsDeclaredInArguments("~/x", { a: { b: ["~/x"] } }));
  });
  it("finds a path inside a JSON-encoded array-of-strings argument (Gmail attachments)", () => {
    assert.ok(pathIsDeclaredInArguments("~/x.pdf", { attachments: JSON.stringify(["~/a.pdf", "~/x.pdf"]) }));
  });
  it("rejects a path the agent never named", () => {
    assert.equal(pathIsDeclaredInArguments("/etc/passwd", { local_path: "~/report.pdf" }), false);
  });
  it("trims the declared path before comparing", () => {
    assert.ok(pathIsDeclaredInArguments("  ~/x  ", { local_path: "~/x" }));
  });
});

describe("isSafeBasename", () => {
  it("accepts a plain name", () => assert.ok(isSafeBasename("report.pdf")));
  it("rejects a slash", () => assert.equal(isSafeBasename("a/b"), false));
  it("rejects a backslash", () => assert.equal(isSafeBasename("a\\b"), false));
  it("rejects .. ", () => assert.equal(isSafeBasename(".."), false));
  it("rejects a NUL byte", () => assert.equal(isSafeBasename("a\0b"), false));
});

describe("pickNonCollidingName", () => {
  it("returns the name unchanged when free", () => {
    assert.equal(pickNonCollidingName("/d", "report.pdf", () => false), "report.pdf");
  });
  it("splits the extension correctly on collision", () => {
    const taken = new Set(["/d/report.pdf", "/d/report (1).pdf"]);
    assert.equal(
      pickNonCollidingName("/d", "report.pdf", (p) => taken.has(p)),
      "report (2).pdf",
    );
  });
  it("handles a name with no extension", () => {
    const taken = new Set(["/d/README"]);
    assert.equal(
      pickNonCollidingName("/d", "README", (p) => taken.has(p)),
      "README (1)",
    );
  });
});

describe("setAtJsonPointer", () => {
  it("sets a top-level key", () => {
    const obj: Record<string, unknown> = { path: null, delivery: "client_bridge" };
    setAtJsonPointer(obj, "/path", "/real/path.pdf");
    assert.equal(obj.path, "/real/path.pdf");
  });
});

/** Drains a fake PUT's request body the way a real `fetch` implementation
 * would consume it -- without this, the `fs.ReadStream` fileBridge.ts hands
 * `fetch` as `body` never gets read, and its (deferred) `open()` fires after
 * the test has already torn down its temp directory, surfacing as an
 * uncaught ENOENT. */
async function drainBody(init: RequestInit | undefined): Promise<void> {
  const body = init?.body as AsyncIterable<Buffer> | undefined;
  if (body === undefined) return;
  for await (const _chunk of body) {
    // discarded -- only draining the stream matters here
  }
}

const REQUEST_ID = 42;

function toolCallRequest(args: Record<string, unknown>): JSONRPCMessage {
  return {
    jsonrpc: "2.0",
    id: REQUEST_ID,
    method: "tools/call",
    params: { name: "drive_upload_file", arguments: args },
  } as unknown as JSONRPCMessage;
}

describe("createFileBridge upload flow", () => {
  it("uploads the declared file, resends with the uploads map, and forwards the final response untouched", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-bridge-up-"));
    const filePath = path.join(dir, "report.pdf");
    fs.writeFileSync(filePath, "hello world");

    const puts: { url: string; auth: string | null }[] = [];
    const fakeFetch = (async (url: string | URL, init?: RequestInit) => {
      puts.push({ url: url.toString(), auth: (init?.headers as Record<string, string> | undefined)?.Authorization ?? null });
      await drainBody(init);
      return new Response(null, { status: 204 });
    }) as typeof fetch;

    const bridge = createFileBridge({ origin: "http://127.0.0.1:9", authHeader: "Bearer tok", fetch: fakeFetch });

    const original = toolCallRequest({ local_path: filePath });
    bridge.onDesktopRequest(original);

    const needUploads: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: {
        content: [{ type: "text", text: "needs upload" }],
        _meta: {
          "privacyfence.eu/file-bridge": {
            v: 1,
            op: "need_uploads",
            files: [{ path: filePath, slot: "slot123", upload_path: "/mcp-files/uploads/slot123", max_bytes: 1000 }],
          },
        },
      },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(needUploads);
    assert.equal(decision.forwardToDesktop, undefined);
    assert.ok(decision.resendToDaemon);
    assert.equal(puts.length, 1);
    assert.equal(puts[0]!.url, "http://127.0.0.1:9/mcp-files/uploads/slot123");
    assert.equal(puts[0]!.auth, "Bearer tok");

    const resendEnv = decision.resendToDaemon as unknown as {
      params: { _meta: Record<string, unknown> };
    };
    assert.deepEqual(resendEnv.params._meta["privacyfence.eu/file-bridge"], {
      v: 1,
      uploads: { [filePath]: "slot123" },
    });

    const finalResponse: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: { content: [{ type: "text", text: "ok" }] },
    } as unknown as JSONRPCMessage;
    const finalDecision = await bridge.onDaemonMessage(finalResponse);
    assert.deepEqual(finalDecision.forwardToDesktop, finalResponse);
    assert.equal(finalDecision.resendToDaemon, undefined);

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("rejects a path the agent never declared, without ever calling the daemon", async () => {
    let calls = 0;
    const fakeFetch = (async () => {
      calls++;
      return new Response(null, { status: 204 });
    }) as typeof fetch;
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t", fetch: fakeFetch });

    bridge.onDesktopRequest(toolCallRequest({ local_path: "/declared/only.pdf" }));
    const needUploads: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: {
        content: [],
        _meta: {
          "privacyfence.eu/file-bridge": {
            v: 1,
            op: "need_uploads",
            files: [{ path: "/etc/passwd", slot: "s", upload_path: "/mcp-files/uploads/s", max_bytes: 100 }],
          },
        },
      },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(needUploads);
    assert.equal(calls, 0);
    assert.ok(decision.forwardToDesktop);
    const result = (decision.forwardToDesktop as unknown as { result: { isError: boolean } }).result;
    assert.equal(result.isError, true);
  });

  it("refuses a second need_uploads round for the same call", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-bridge-loop-"));
    const filePath = path.join(dir, "a.pdf");
    fs.writeFileSync(filePath, "x");
    const fakeFetch = (async (_url: string | URL, init?: RequestInit) => {
      await drainBody(init);
      return new Response(null, { status: 204 });
    }) as typeof fetch;
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t", fetch: fakeFetch });

    bridge.onDesktopRequest(toolCallRequest({ local_path: filePath }));
    const needUploadsOf = (): JSONRPCMessage =>
      ({
        jsonrpc: "2.0",
        id: REQUEST_ID,
        result: {
          content: [],
          _meta: {
            "privacyfence.eu/file-bridge": {
              v: 1,
              op: "need_uploads",
              files: [{ path: filePath, slot: "s", upload_path: "/mcp-files/uploads/s", max_bytes: 100 }],
            },
          },
        },
      }) as unknown as JSONRPCMessage;

    const first = await bridge.onDaemonMessage(needUploadsOf());
    assert.ok(first.resendToDaemon);

    const second = await bridge.onDaemonMessage(needUploadsOf());
    assert.ok(second.forwardToDesktop);
    const result = (second.forwardToDesktop as unknown as { result: { isError: boolean; content: { text: string }[] } })
      .result;
    assert.equal(result.isError, true);
    assert.match(result.content[0]!.text, /second upload round/);

    fs.rmSync(dir, { recursive: true, force: true });
  });
});

describe("createFileBridge deliver flow", () => {
  it("downloads, verifies sha256, writes without overwrite, and rewrites the result", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-bridge-dl-"));
    const bytes = Buffer.from("downloaded bytes");
    const crypto = await import("node:crypto");
    const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");

    const fakeFetch = (async () => {
      return new Response(bytes, { status: 200 });
    }) as typeof fetch;

    const bridge = createFileBridge({ origin: "http://127.0.0.1:9", authHeader: "Bearer tok", fetch: fakeFetch });
    bridge.onDesktopRequest(
      toolCallRequest({ destination_dir: dir }) as unknown as JSONRPCMessage,
    );
    // toolCallRequest above stamps method "tools/call" with a fixed id --
    // reuse it for destination_dir instead of local_path.

    const deliver: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: {
        content: [{ type: "text", text: "placeholder" }],
        structuredContent: { path: null, name: "out.bin", size_bytes: bytes.length, delivery: "client_bridge" },
        _meta: {
          "privacyfence.eu/file-bridge": {
            v: 1,
            op: "deliver",
            files: [
              {
                dest_dir: dir,
                name: "out.bin",
                download_path: "/mcp-files/downloads/tok1",
                size_bytes: bytes.length,
                sha256,
                result_path_pointer: "/path",
              },
            ],
          },
        },
      },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(deliver);
    assert.ok(decision.forwardToDesktop);
    const result = (
      decision.forwardToDesktop as unknown as {
        result: { structuredContent: { path: string; delivery: string }; content: { text: string }[]; _meta?: unknown };
      }
    ).result;
    const realPath = result.structuredContent.path;
    assert.equal(realPath, path.join(dir, "out.bin"));
    assert.equal(result.structuredContent.delivery, "local_disk");
    assert.equal(fs.readFileSync(realPath, "utf8"), "downloaded bytes");
    assert.deepEqual(JSON.parse(result.content[0]!.text), result.structuredContent);
    assert.equal(result._meta, undefined);

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("does not overwrite an existing file, picking a (1) suffix instead", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-bridge-dl-collide-"));
    fs.writeFileSync(path.join(dir, "out.bin"), "existing");
    const bytes = Buffer.from("new bytes");
    const crypto = await import("node:crypto");
    const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
    const fakeFetch = (async () => new Response(bytes, { status: 200 })) as typeof fetch;
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t", fetch: fakeFetch });
    bridge.onDesktopRequest(toolCallRequest({ destination_dir: dir }));

    const deliver: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: {
        content: [{ type: "text", text: "placeholder" }],
        structuredContent: { path: null, delivery: "client_bridge" },
        _meta: {
          "privacyfence.eu/file-bridge": {
            v: 1,
            op: "deliver",
            files: [
              {
                dest_dir: dir,
                name: "out.bin",
                download_path: "/mcp-files/downloads/tok2",
                size_bytes: bytes.length,
                sha256,
                result_path_pointer: "/path",
              },
            ],
          },
        },
      },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(deliver);
    const result = (decision.forwardToDesktop as unknown as { result: { structuredContent: { path: string } } }).result;
    assert.equal(result.structuredContent.path, path.join(dir, "out (1).bin"));
    assert.equal(fs.readFileSync(path.join(dir, "out.bin"), "utf8"), "existing");
    assert.equal(fs.readFileSync(path.join(dir, "out (1).bin"), "utf8"), "new bytes");

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("a sha256 mismatch gives isError and leaves no partial file behind", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-bridge-dl-shamismatch-"));
    const bytes = Buffer.from("actual bytes on the wire");
    // Deliberately wrong -- the daemon claimed a different digest than
    // what the GET actually returns, as if the file changed underneath it
    // or the response was corrupted in transit.
    const wrongSha256 = "0".repeat(64);
    const fakeFetch = (async () => new Response(bytes, { status: 200 })) as typeof fetch;
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t", fetch: fakeFetch });
    bridge.onDesktopRequest(toolCallRequest({ destination_dir: dir }));

    const deliver: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: {
        content: [{ type: "text", text: "placeholder" }],
        structuredContent: { path: null, delivery: "client_bridge" },
        _meta: {
          "privacyfence.eu/file-bridge": {
            v: 1,
            op: "deliver",
            files: [
              {
                dest_dir: dir,
                name: "out.bin",
                download_path: "/mcp-files/downloads/tok3",
                size_bytes: bytes.length,
                sha256: wrongSha256,
                result_path_pointer: "/path",
              },
            ],
          },
        },
      },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(deliver);
    const result = (
      decision.forwardToDesktop as unknown as { result: { isError: boolean; content: { text: string }[] } }
    ).result;
    assert.equal(result.isError, true);
    assert.match(result.content[0]!.text, /SHA-256/);
    // No partial file, and no file at the final name either -- the
    // mismatch is caught before the rename ever happens.
    assert.deepEqual(fs.readdirSync(dir), []);

    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("gives a specific isError when the download destination can't actually be used", async () => {
    // A real OS-level failure distinct from "the file is simply missing":
    // dest_dir already exists as a *file*, so mkdir(dest_dir, {recursive})
    // fails with ENOTDIR -- the same describeError()/isError path an
    // EACCES from a real permission failure takes (not simulated directly
    // here, since chmod-based permission tests are unreliable when the
    // test runner itself has root/owner access to the file).
    const parent = fs.mkdtempSync(path.join(os.tmpdir(), "pf-bridge-dl-baddest-"));
    const destDir = path.join(parent, "not-a-directory");
    fs.writeFileSync(destDir, "this occupies the name destDir wants to be");
    const bytes = Buffer.from("data");
    const fakeFetch = (async () => new Response(bytes, { status: 200 })) as typeof fetch;
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t", fetch: fakeFetch });
    bridge.onDesktopRequest(toolCallRequest({ destination_dir: destDir }));

    const deliver: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: {
        content: [{ type: "text", text: "placeholder" }],
        structuredContent: { path: null, delivery: "client_bridge" },
        _meta: {
          "privacyfence.eu/file-bridge": {
            v: 1,
            op: "deliver",
            files: [
              {
                dest_dir: destDir,
                name: "out.bin",
                download_path: "/mcp-files/downloads/tok4",
                size_bytes: bytes.length,
                sha256: "irrelevant",
                result_path_pointer: "/path",
              },
            ],
          },
        },
      },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(deliver);
    const result = (
      decision.forwardToDesktop as unknown as { result: { isError: boolean; content: { text: string }[] } }
    ).result;
    assert.equal(result.isError, true);
    assert.match(result.content[0]!.text, /Could not save to/);
    assert.match(result.content[0]!.text, /different destination_dir/);

    fs.rmSync(parent, { recursive: true, force: true });
  });
});

describe("createFileBridge passthrough", () => {
  it("forwards a response with no file-bridge _meta key completely unchanged", async () => {
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t" });
    bridge.onDesktopRequest(toolCallRequest({ file_id: "f1" }));
    const ordinary: JSONRPCMessage = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      result: { content: [{ type: "text", text: "just an ordinary tool result" }] },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(ordinary);
    assert.deepEqual(decision.forwardToDesktop, ordinary);
    assert.equal(decision.resendToDaemon, undefined);
  });

  it("forwards a notification (no id) unchanged", async () => {
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t" });
    const notification = {
      jsonrpc: "2.0",
      method: "notifications/tools/list_changed",
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(notification);
    assert.deepEqual(decision.forwardToDesktop, notification);
  });

  it("forwards an error response unchanged", async () => {
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t" });
    bridge.onDesktopRequest(toolCallRequest({ file_id: "f1" }));
    const errorResponse = {
      jsonrpc: "2.0",
      id: REQUEST_ID,
      error: { code: -32000, message: "boom" },
    } as unknown as JSONRPCMessage;

    const decision = await bridge.onDaemonMessage(errorResponse);
    assert.deepEqual(decision.forwardToDesktop, errorResponse);
  });

  it("onDesktopRequest is a no-op for anything other than tools/call", () => {
    const bridge = createFileBridge({ origin: "http://x", authHeader: "Bearer t" });
    // Must not throw -- a non-tools/call desktop message (e.g. initialize,
    // or a plain notification) is simply not something the bridge ever
    // needs to remember.
    bridge.onDesktopRequest({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} } as unknown as JSONRPCMessage);
  });
});
