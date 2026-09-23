/**
 * Exercises mintMcpToken() against a fake control-channel listener -- the
 * shim-side counterpart of tests/unit/web/test_control_channel.py's own
 * MINT coverage, minus everything Python-side (peer identity, persistence)
 * this file has no way to fake and no business asserting on: all this file
 * owns is "did this client send the right line and parse the reply
 * correctly", the same scope fakeMcpDaemon.ts keeps for the /mcp side.
 *
 * POSIX-only (AF_UNIX), matching ADR 0008's own "Windows peer identity is
 * written, not exercised" note (docs/adr/0008-one-principal-per-os-user.md)
 * -- nothing in this repository can run a real Windows named pipe outside a
 * Windows CI runner, and controlChannel.ts's own sendControlChannelLine()
 * routes both platforms through the identical net.createConnection() call,
 * so a POSIX socket exercises the same code path a Windows pipe would.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, it } from "node:test";
import { ControlChannelError, mintMcpToken } from "../src/controlChannel.js";

/** Points dataDir()'s HOME lookup -- and therefore posixControlSocketPath(),
 * which mintMcpToken() calls with no override of its own -- at a fresh temp
 * directory for the duration of one test, restoring the real value
 * afterwards. Same env-mutation-then-restore discipline protocol.test.ts's
 * "dataDir" suite already uses for LOCALAPPDATA; this repo has no marker
 * file in this temp HOME, so privilegeSeparationRoot() is null and the
 * resolved socket lands at ``<home>/.privacyfence/authority/control.sock``
 * (controlSocketDir()'s unseparated branch). */
let originalHome: string | undefined;
let tempHome: string | undefined;

afterEach(() => {
  if (tempHome !== undefined) {
    fs.rmSync(tempHome, { recursive: true, force: true });
    tempHome = undefined;
  }
  if (originalHome === undefined) {
    delete process.env.HOME;
  } else {
    process.env.HOME = originalHome;
  }
});

function fakeAuthorityDir(): string {
  originalHome = process.env.HOME;
  tempHome = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-cc-home-"));
  process.env.HOME = tempHome;
  const authorityDir = path.join(tempHome, ".privacyfence", "authority");
  fs.mkdirSync(authorityDir, { recursive: true });
  return authorityDir;
}

/** A fake control channel that answers every request the same fixed way --
 * enough to test mintMcpToken()'s parsing without pulling in
 * control_channel.py's own dispatch. ``reply`` is written back verbatim
 * once a full request line has been read; "hang" never writes anything
 * (the connection stays open, standing in for a daemon that accepted the
 * connection but is stuck), and "close" ends the connection with nothing
 * written at all (standing in for one that hung up mid-request). */
function startFakeControlChannel(
  socketPath: string,
  reply: string | "hang" | "close"
): Promise<net.Server> {
  return new Promise((resolve) => {
    const server = net.createServer((conn) => {
      conn.on("data", () => {
        if (reply === "hang") return;
        if (reply === "close") {
          conn.end();
          return;
        }
        conn.end(reply);
      });
    });
    server.listen(socketPath, () => resolve(server));
  });
}

function closeServer(server: net.Server): Promise<void> {
  return new Promise((resolve) => server.close(() => resolve()));
}

describe("mintMcpToken", () => {
  it("resolves with the token from a well-formed OK reply", async () => {
    const authorityDir = fakeAuthorityDir();
    const server = await startFakeControlChannel(path.join(authorityDir, "control.sock"), "OK sometoken\n");
    try {
      assert.equal(await mintMcpToken({ timeoutMs: 2000 }), "sometoken");
    } finally {
      await closeServer(server);
    }
  });

  it("rejects with ControlChannelError on an ERROR reply", async () => {
    const authorityDir = fakeAuthorityDir();
    const server = await startFakeControlChannel(path.join(authorityDir, "control.sock"), "ERROR nope\n");
    try {
      await assert.rejects(mintMcpToken({ timeoutMs: 2000 }), (err: unknown) => {
        assert.ok(err instanceof ControlChannelError);
        assert.match((err as Error).message, /nope/);
        return true;
      });
    } finally {
      await closeServer(server);
    }
  });

  it("rejects with ControlChannelError on a reply that is neither OK nor ERROR", async () => {
    const authorityDir = fakeAuthorityDir();
    const server = await startFakeControlChannel(path.join(authorityDir, "control.sock"), "garbage\n");
    try {
      await assert.rejects(mintMcpToken({ timeoutMs: 2000 }), ControlChannelError);
    } finally {
      await closeServer(server);
    }
  });

  it("rejects quickly when nothing is listening at all", async () => {
    fakeAuthorityDir(); // creates the directory, but binds no socket in it
    const startedAt = Date.now();
    await assert.rejects(mintMcpToken({ timeoutMs: 2000 }));
    // ENOENT/ECONNREFUSED-shaped failures are near-instant; asserting well
    // under the 2s override (rather than under a fixed small number) is what
    // proves this test isn't accidentally the one below it, waiting out the
    // whole timeout for a case that should never need to.
    assert.ok(Date.now() - startedAt < 1000, "connecting to a nonexistent socket should fail fast");
  });

  it("rejects on timeout when the listener never replies, without hanging the test", async () => {
    const authorityDir = fakeAuthorityDir();
    const server = await startFakeControlChannel(path.join(authorityDir, "control.sock"), "hang");
    try {
      await assert.rejects(mintMcpToken({ timeoutMs: 200 }), /timed out/);
    } finally {
      await closeServer(server);
    }
  });

  it("rejects when the listener closes the connection without answering", async () => {
    const authorityDir = fakeAuthorityDir();
    const server = await startFakeControlChannel(path.join(authorityDir, "control.sock"), "close");
    try {
      await assert.rejects(mintMcpToken({ timeoutMs: 2000 }), /closed before answering/);
    } finally {
      await closeServer(server);
    }
  });
});
