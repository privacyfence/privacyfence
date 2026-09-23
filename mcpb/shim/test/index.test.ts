import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { main, parseArgs } from "../src/index.js";
import { FakeMcpDaemon } from "./fakeMcpDaemon.js";
import { makeTempMcpFiles } from "./testFiles.js";

describe("parseArgs", () => {
  it("accepts no arguments", () => {
    assert.doesNotThrow(() => parseArgs([]));
  });

  it("accepts --config <path> (daemon-side only, ignored here)", () => {
    assert.doesNotThrow(() => parseArgs(["--config", "/tmp/x.yaml"]));
  });

  it("accepts --config=<path>", () => {
    assert.doesNotThrow(() => parseArgs(["--config=/tmp/x.yaml"]));
  });

  // Regression: this used to throw, which exits main() before stdio is
  // ever read -- the host sees a server that starts and then never answers
  // initialize, and the daemon logs nothing at all, because no /mcp
  // connection was ever opened. A host may add flags of its own to the
  // servers it spawns; an argument this proxy has no use for must not cost
  // the connection. See parseArgs' own doc comment.
  it("ignores an unrecognized flag instead of refusing to start", () => {
    assert.doesNotThrow(() => parseArgs(["--bogus"]));
  });

  it("ignores unrecognized flags alongside a --config it understands", () => {
    assert.doesNotThrow(() => parseArgs(["--config", "/tmp/x.yaml", "--pool", "--session-id=abc"]));
  });
});

describe("main() end-to-end orchestration", () => {
  it("proxies a real MCP session between stdio and a real /mcp endpoint, bearer header attached", async () => {
    const { mcpUrlFile, mcpTokenFile, writeUrl, token, cleanup } = makeTempMcpFiles();
    const daemon = new FakeMcpDaemon(token);
    const url = await daemon.start();
    writeUrl(url);

    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let resolveDisconnect: () => void = () => {};
    const waitForDisconnect = () => new Promise<void>((resolve) => (resolveDisconnect = resolve));

    const mainPromise = main([], {
      mcpUrlFile,
      mcpTokenFile,
      transport: serverTransport,
      waitForDisconnect,
    });

    // A real Client, standing in for Claude Desktop, drives the shim over
    // the in-memory stdio replacement -- everything past this point crosses
    // the shim's real proxy and a real Streamable HTTP round trip to
    // FakeMcpDaemon.
    const client = new Client({ name: "index-test-client", version: "1.0.0" });
    await client.connect(clientTransport);

    const { tools } = await client.listTools();
    assert.deepEqual(
      tools.map((t) => t.name),
      ["shim_test_echo"]
    );

    const result = await client.callTool({ name: "shim_test_echo", arguments: {} });
    assert.equal(result.isError, undefined);
    assert.deepEqual(result.structuredContent, { echoed: true });

    // §10.3's audience separation, restated on the shim's own side: every
    // request that reached the fake daemon carried exactly the token from
    // mcp_token, never anything else (no unauthenticated request slipped
    // through, e.g. from a stray SSE probe).
    assert.ok(daemon.receivedAuthHeaders.length > 0);
    for (const header of daemon.receivedAuthHeaders) {
      assert.equal(header, `Bearer ${token}`);
    }

    await client.close();
    resolveDisconnect();
    await mainPromise;

    await daemon.stop();
    cleanup();
  });

  // The "mcp_url isn't there yet, launch the daemon" path (findDaemonCmd +
  // spawn) is covered by daemon.test.ts's ensureDaemonRunning/
  // waitForDaemonPatiently suites directly, with a fake findCmd injected --
  // main() has no seam to inject one (matching bridge/src/index.ts, which
  // has the same gap for the same reason: real production startup should
  // never be racing a fake findCmd), so exercising the launch path here
  // would spawn a real process instead of a fake one.

  // ADR 0008 D3: getMcpToken()'s two paths, exercised end to end through a
  // real proxied session rather than by calling that helper directly, so a
  // regression in main()'s own wiring (e.g. reading mcpTokenFile before
  // even trying the mint) would show up here the same way it would in
  // production.
  it("uses a token from a successful mint, and never reads the token file at all", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const mintedToken = "minted-over-the-control-channel";
    const daemon = new FakeMcpDaemon(mintedToken);
    const url = await daemon.start();
    writeUrl(url);

    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let resolveDisconnect: () => void = () => {};
    const waitForDisconnect = () => new Promise<void>((resolve) => (resolveDisconnect = resolve));

    const mainPromise = main([], {
      mcpUrlFile,
      // A path nothing will ever write to -- readMcpToken() throws ENOENT
      // if main() reads it, which is exactly the regression this test
      // exists to catch.
      mcpTokenFile: "/definitely/does/not/exist/mcp_token",
      mintMcpToken: async () => mintedToken,
      transport: serverTransport,
      waitForDisconnect,
    });

    const client = new Client({ name: "index-test-client-mint", version: "1.0.0" });
    await client.connect(clientTransport);
    const { tools } = await client.listTools();
    assert.deepEqual(tools.map((t) => t.name), ["shim_test_echo"]);

    assert.ok(daemon.receivedAuthHeaders.length > 0);
    for (const header of daemon.receivedAuthHeaders) {
      assert.equal(header, `Bearer ${mintedToken}`);
    }

    await client.close();
    resolveDisconnect();
    await mainPromise;

    await daemon.stop();
    cleanup();
  });

  it("falls back to the legacy token file when the mint fails, exactly like today's behavior", async () => {
    const { mcpUrlFile, mcpTokenFile, writeUrl, token, cleanup } = makeTempMcpFiles();
    const daemon = new FakeMcpDaemon(token);
    const url = await daemon.start();
    writeUrl(url);

    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let resolveDisconnect: () => void = () => {};
    const waitForDisconnect = () => new Promise<void>((resolve) => (resolveDisconnect = resolve));

    const mainPromise = main([], {
      mcpUrlFile,
      mcpTokenFile,
      // Stands in for both failure shapes controlChannel.ts can raise (a
      // plain connection error and a ControlChannelError) -- getMcpToken()
      // treats them identically, so one rejection is enough to exercise the
      // fallback branch; the real mintMcpToken()'s own two-shape distinction
      // is controlChannel.test.ts's job, not this one's.
      mintMcpToken: async () => {
        throw new Error("no daemon control channel in this test sandbox");
      },
      transport: serverTransport,
      waitForDisconnect,
    });

    const client = new Client({ name: "index-test-client-fallback", version: "1.0.0" });
    await client.connect(clientTransport);
    const { tools } = await client.listTools();
    assert.deepEqual(tools.map((t) => t.name), ["shim_test_echo"]);

    assert.ok(daemon.receivedAuthHeaders.length > 0);
    for (const header of daemon.receivedAuthHeaders) {
      assert.equal(header, `Bearer ${token}`);
    }

    await client.close();
    resolveDisconnect();
    await mainPromise;

    await daemon.stop();
    cleanup();
  });
});
