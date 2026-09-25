import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ControlChannelError } from "../src/controlChannel.js";
import { ShimExitError } from "../src/errors.js";
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
    const { mcpUrlFile, writeUrl, token, cleanup } = makeTempMcpFiles();
    const daemon = new FakeMcpDaemon(token);
    const url = await daemon.start();
    writeUrl(url);

    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let resolveDisconnect: () => void = () => {};
    const waitForDisconnect = () => new Promise<void>((resolve) => (resolveDisconnect = resolve));

    const mainPromise = main([], {
      mcpUrlFile,
      mintMcpToken: async () => token,
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

    // Token audience separation (ADR 0061), restated on the shim's own
    // side: every request that reached the fake daemon carried exactly the
    // minted token, never anything else (no unauthenticated request slipped
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

  // ADR 0008 D3: getMcpToken()'s two outcomes, exercised through main()
  // itself so a regression in main()'s own wiring shows up here the same
  // way it would in production.
  it("exits with a user-facing error when the mint fails, without reading any token file", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const daemon = new FakeMcpDaemon("unused");
    const url = await daemon.start();
    writeUrl(url);

    await assert.rejects(
      main([], {
        mcpUrlFile,
        // Stands in for both failure shapes controlChannel.ts can raise (a
        // plain connection error and a ControlChannelError) -- getMcpToken()
        // treats them identically; the real mintMcpToken()'s own two-shape
        // distinction is controlChannel.test.ts's job, not this one's.
        mintMcpToken: async () => {
          throw new Error("no daemon control channel in this test sandbox");
        },
        waitForDisconnect: async () => {},
      }),
      (err: unknown) => {
        assert.ok(err instanceof ShimExitError);
        assert.equal(err.code, 1);
        assert.match(err.message, /Could not get an MCP token/);
        assert.match(err.message, /no daemon control channel in this test sandbox/);
        return true;
      }
    );
    // Nothing reached /mcp: a failed mint never degrades to an
    // unauthenticated (or file-sourced) request.
    assert.equal(daemon.receivedAuthHeaders.length, 0);

    await daemon.stop();
    cleanup();
  });
  // A mint nobody answered is retried for a bounded window before the shim
  // gives up (getMcpToken()'s MINT_RETRY_WINDOW_MS); one the daemon answered,
  // or one this account may not make at all, is not. v4.3.0's first packaged
  // macOS smoke run is the case being ridden out: the control channel was up
  // but still serving the companion's own attested mint when the shim asked.
  function timeoutError(): Error {
    return new Error("control channel request timed out after 5000ms (target: /fake/control.sock)");
  }

  function errnoError(code: string): NodeJS.ErrnoException {
    const err: NodeJS.ErrnoException = new Error(`${code}: fake`);
    err.code = code;
    return err;
  }

  it("retries a mint nobody answered, then proxies with the token it eventually gets", async () => {
    const { mcpUrlFile, writeUrl, token, cleanup } = makeTempMcpFiles();
    const daemon = new FakeMcpDaemon(token);
    const url = await daemon.start();
    writeUrl(url);

    const failures = [timeoutError(), errnoError("ECONNREFUSED"), errnoError("ENOENT")];
    let calls = 0;
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let resolveDisconnect: () => void = () => {};
    const mainPromise = main([], {
      mcpUrlFile,
      mintMcpToken: async () => {
        calls += 1;
        const failure = failures.shift();
        if (failure) throw failure;
        return token;
      },
      mintRetryWindowMs: 5_000,
      mintRetryIntervalMs: 10,
      transport: serverTransport,
      waitForDisconnect: () => new Promise<void>((resolve) => (resolveDisconnect = resolve)),
    });

    // main() rejecting before it ever proxies (a mint that is not retried)
    // must fail this test, not leave client.connect() waiting forever.
    const mainGaveUp = mainPromise.then(
      () => assert.fail("main() returned before the client connected"),
      (err: unknown) => assert.fail(`main() gave up instead of retrying: ${String(err)}`)
    );
    const client = new Client({ name: "index-test-client", version: "1.0.0" });
    try {
      await Promise.race([client.connect(clientTransport), mainGaveUp]);
      const { tools } = await client.listTools();
      assert.deepEqual(
        tools.map((t) => t.name),
        ["shim_test_echo"]
      );
      assert.equal(calls, 4);
      for (const header of daemon.receivedAuthHeaders) {
        assert.equal(header, `Bearer ${token}`);
      }
    } finally {
      await client.close();
      resolveDisconnect();
      await mainPromise.catch(() => {});
      await daemon.stop();
      cleanup();
    }
  });

  it("gives up once the retry window is spent, naming the attempts and the last error", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const daemon = new FakeMcpDaemon("unused");
    const url = await daemon.start();
    writeUrl(url);

    try {
      let calls = 0;
      const startedAt = Date.now();
      await assert.rejects(
        main([], {
          mcpUrlFile,
          mintMcpToken: async () => {
            calls += 1;
            throw timeoutError();
          },
          mintRetryWindowMs: 200,
          mintRetryIntervalMs: 20,
          waitForDisconnect: async () => {},
        }),
        (err: unknown) => {
          assert.ok(err instanceof ShimExitError);
          assert.equal(err.code, 1);
          assert.match(err.message, new RegExp(`after ${calls} attempts`));
          assert.match(err.message, /timed out after 5000ms/);
          return true;
        }
      );
      assert.ok(calls > 1, `expected more than one attempt, got ${calls}`);
      assert.ok(Date.now() - startedAt < 2_000, "the retry window must bound how long the shim waits");
      assert.equal(daemon.receivedAuthHeaders.length, 0);
    } finally {
      await daemon.stop();
      cleanup();
    }
  });

  for (const [what, failure] of [
    ["a refusal the daemon answered (ControlChannelError)", () => new ControlChannelError("nope")],
    ["EACCES (this account may not connect)", () => errnoError("EACCES")],
    ["EPERM", () => errnoError("EPERM")],
  ] as const) {
    it(`does not retry ${what}`, async () => {
      const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
      const daemon = new FakeMcpDaemon("unused");
      const url = await daemon.start();
      writeUrl(url);

      let calls = 0;
      await assert.rejects(
        main([], {
          mcpUrlFile,
          mintMcpToken: async () => {
            calls += 1;
            throw failure();
          },
          mintRetryWindowMs: 5_000,
          mintRetryIntervalMs: 10,
          waitForDisconnect: async () => {},
        }),
        (err: unknown) => {
          assert.ok(err instanceof ShimExitError);
          assert.doesNotMatch(err.message, /attempts/);
          return true;
        }
      );
      assert.equal(calls, 1);

      await daemon.stop();
      cleanup();
    });
  }
});
