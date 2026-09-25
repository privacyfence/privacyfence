import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import {
  describeTarget,
  ensureDaemonRunning,
  findDaemonCmd,
  socketConnectable,
  waitForDaemonPatiently,
} from "../src/daemon.js";
import { ShimExitError } from "../src/errors.js";
import { getFreePort, makeTempMcpFiles } from "./testFiles.js";

describe("findDaemonCmd", () => {
  it("prefers a sibling binary next to the shim script", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-"));
    const sibling = path.join(dir, "privacyfence-app");
    fs.writeFileSync(sibling, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({ scriptPath: path.join(dir, "shim.js") });
    assert.deepEqual(cmd, [sibling]);
  });

  it("falls back to a PATH lookup when no sibling binary exists", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty-"));
    const pathDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-path-"));
    const onPath = path.join(pathDir, "privacyfence-app");
    fs.writeFileSync(onPath, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: pathDir,
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
    });
    assert.deepEqual(cmd, [onPath]);
  });

  it("falls back to python3 -m privacyfence.daemon_main when nothing is found", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty2-"));
    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir, // nothing named privacyfence-app here
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
      platform: "linux",
      homeDir: emptyDir, // no ~/.local/bin/privacyfence-app here either
    });
    assert.deepEqual(cmd, ["python3", "-m", "privacyfence.daemon_main"]);
  });

  it("on Linux, falls back to the pipx default (~/.local/bin/privacyfence-app) before python3 -m", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty3-"));
    const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-home-"));
    const localBin = path.join(homeDir, ".local", "bin");
    fs.mkdirSync(localBin, { recursive: true });
    const pipxDefault = path.join(localBin, "privacyfence-app");
    fs.writeFileSync(pipxDefault, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir, // ~/.local/bin deliberately not on PATH here
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
      platform: "linux",
      homeDir,
    });
    assert.deepEqual(cmd, [pipxDefault]);
  });

  it("does not check the Linux pipx default on other platforms", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty4-"));
    const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-home2-"));
    const localBin = path.join(homeDir, ".local", "bin");
    fs.mkdirSync(localBin, { recursive: true });
    fs.writeFileSync(path.join(localBin, "privacyfence-app"), "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir,
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
      platform: "darwin",
      homeDir,
    });
    assert.deepEqual(cmd, ["python3", "-m", "privacyfence.daemon_main"]);
  });

  // Windows branch -- `platform` is injectable specifically
  // so these can run on any CI host, not just a real Windows one.
  it("falls back to python -m privacyfence.daemon_main (not python3) on win32", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-"));
    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir, // nothing named privacyfence-app here
      defaultAppPath: "C:\\definitely\\does\\not\\exist\\privacyfence-app.exe",
      platform: "win32",
    });
    assert.deepEqual(cmd, ["python", "-m", "privacyfence.daemon_main"]);
  });

  it("falls through to python when the Windows default location doesn't exist, with no defaultAppPath override given", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-default-"));
    // No defaultAppPath override, and a windowsEnv naming directories that
    // don't exist: exercises the platform-conditional default falling
    // through to the python fallback -- confirming the win32 branch is
    // reached at all, not just that an override is honored.
    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir,
      platform: "win32",
      windowsEnv: {
        ProgramFiles: path.join(emptyDir, "no-such-program-files"),
        LOCALAPPDATA: path.join(emptyDir, "no-such-localappdata"),
      },
    });
    assert.deepEqual(cmd, ["python", "-m", "privacyfence.daemon_main"]);
  });

  it("finds privacyfence-app.exe under the elevated Program Files install location", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-pf-"));
    const programFiles = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-pf-root-"));
    const appDir = path.join(programFiles, "PrivacyFence");
    fs.mkdirSync(appDir, { recursive: true });
    const exe = path.join(appDir, "privacyfence-app.exe");
    fs.writeFileSync(exe, "", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir,
      platform: "win32",
      windowsEnv: {
        ProgramFiles: programFiles,
        LOCALAPPDATA: path.join(emptyDir, "no-such-localappdata"),
      },
    });
    assert.deepEqual(cmd, [exe]);
  });

  it("does not look under LOCALAPPDATA\\Programs, which no current installer writes to", () => {
    // ADR 0041: only the current install layout is supported. The installer
    // is PrivilegesRequired=admin, so an exe under %LOCALAPPDATA%\Programs
    // is not one this shim should ever launch.
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-lad-"));
    const localAppData = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-lad-root-"));
    const appDir = path.join(localAppData, "Programs", "PrivacyFence");
    fs.mkdirSync(appDir, { recursive: true });
    fs.writeFileSync(path.join(appDir, "privacyfence-app.exe"), "", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir,
      platform: "win32",
      windowsEnv: {
        ProgramFiles: path.join(emptyDir, "no-such-program-files"),
        LOCALAPPDATA: localAppData,
      },
    });
    assert.deepEqual(cmd, ["python", "-m", "privacyfence.daemon_main"]);
  });

  it("ignores windowsEnv on non-Windows platforms", () => {
    // Confirms the win32 branch (and therefore windowsEnv) is genuinely
    // platform-gated, not just usually irrelevant because the paths don't
    // exist on a POSIX host.
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-notwin-"));
    const programFiles = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-notwin-pf-"));
    const appDir = path.join(programFiles, "PrivacyFence");
    fs.mkdirSync(appDir, { recursive: true });
    fs.writeFileSync(path.join(appDir, "privacyfence-app.exe"), "", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir,
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
      platform: "linux",
      homeDir: emptyDir,
      windowsEnv: { ProgramFiles: programFiles },
    });
    assert.deepEqual(cmd, ["python3", "-m", "privacyfence.daemon_main"]);
  });
});

describe("socketConnectable", () => {
  it("is false when the mcp_url file doesn't exist yet", async () => {
    const { mcpUrlFile, cleanup } = makeTempMcpFiles();
    try {
      // mcpUrlFile was created by makeTempMcpFiles only as a path, not written to.
      assert.equal(await socketConnectable(mcpUrlFile), false);
    } finally {
      cleanup();
    }
  });

  it("is false when the mcp_url file doesn't parse as a URL", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    writeUrl("not a url");
    try {
      assert.equal(await socketConnectable(mcpUrlFile), false);
    } finally {
      cleanup();
    }
  });

  it("is false when the URL names a port nothing is listening on", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const port = await getFreePort(); // freed immediately -- nothing listens on it
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    try {
      assert.equal(await socketConnectable(mcpUrlFile), false);
    } finally {
      cleanup();
    }
  });

  it("is true when a real listener is present", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    try {
      assert.equal(await socketConnectable(mcpUrlFile), true);
    } finally {
      server.close();
      cleanup();
    }
  });
});

describe("ensureDaemonRunning", () => {
  it("returns immediately when already connectable, without spawning anything", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    let findCmdCalled = false;
    try {
      await ensureDaemonRunning({
        mcpUrlFile,
        findCmd: () => {
          findCmdCalled = true;
          return ["should-not-run"];
        },
      });
      assert.equal(findCmdCalled, false);
    } finally {
      server.close();
      cleanup();
    }
  });

  it("launches the daemon and waits until connectable", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const port = await getFreePort();
    // Simulate the daemon coming up shortly after being "launched": start
    // listening for real (and only then write mcp_url), but only after
    // ensureDaemonRunning's first connectability check has already failed.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(port, "127.0.0.1", () => writeUrl(`http://127.0.0.1:${port}/mcp`));
    }, 50);

    try {
      await ensureDaemonRunning({
        mcpUrlFile,
        findCmd: () => ["true"], // a real no-op command; spawn() must succeed
        connectIntervalMs: 20,
        connectTimeoutMs: 2000,
      });
    } finally {
      clearTimeout(timer);
      lateServer?.close();
      cleanup();
    }
  });

  it("throws ShimExitError after the timeout elapses", async () => {
    const { mcpUrlFile, cleanup } = makeTempMcpFiles();
    try {
      await assert.rejects(
        ensureDaemonRunning({
          mcpUrlFile,
          findCmd: () => ["true"],
          connectTimeoutMs: 100,
          connectIntervalMs: 20,
        }),
        (err: unknown) => {
          assert.ok(err instanceof ShimExitError);
          assert.equal(err.code, 1);
          assert.match(err.message, /did not start/);
          return true;
        }
      );
    } finally {
      cleanup();
    }
  });

  it("survives a spawn failure (missing/corrupted binary) instead of crashing the process", async () => {
    // A Finder upgrade interrupted mid-drag (or a Gatekeeper-quarantined
    // bundle) can leave findDaemonCmd() pointing at a binary that no longer
    // runs. Left unhandled, spawn()'s async ENOENT surfaces as an 'error'
    // event on the child process, which Node rethrows as an uncaught
    // exception -- killing this whole shim process well before the
    // ShimExitError timeout below ever has a chance to fire. If that
    // regressed, this test would itself crash the test runner rather than
    // observing a rejection.
    const { mcpUrlFile, cleanup } = makeTempMcpFiles();
    try {
      await assert.rejects(
        ensureDaemonRunning({
          mcpUrlFile,
          findCmd: () => ["/definitely/does/not/exist/privacyfence-app"],
          connectTimeoutMs: 150,
          connectIntervalMs: 20,
        }),
        (err: unknown) => {
          assert.ok(err instanceof ShimExitError);
          assert.equal(err.code, 1);
          assert.match(err.message, /did not start/);
          return true;
        }
      );
    } finally {
      cleanup();
    }
  });
});

describe("waitForDaemonPatiently", () => {
  it("returns immediately when already connectable, without spawning anything", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    let findCmdCalled = false;
    try {
      await waitForDaemonPatiently({
        mcpUrlFile,
        findCmd: () => {
          findCmdCalled = true;
          return ["should-not-run"];
        },
      });
      assert.equal(findCmdCalled, false);
    } finally {
      server.close();
      cleanup();
    }
  });

  it("keeps retrying past the initial timeout instead of throwing, and succeeds once the socket comes up", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const port = await getFreePort();
    // The initial launch-and-wait window (connectTimeoutMs) elapses with
    // nothing listening -- ensureDaemonRunning would normally throw here.
    // Only after that do we start listening (and write mcp_url), simulating
    // an app cold start slower than the initial window.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(port, "127.0.0.1", () => writeUrl(`http://127.0.0.1:${port}/mcp`));
    }, 150);

    let findCmdCalls = 0;
    try {
      await waitForDaemonPatiently({
        mcpUrlFile,
        findCmd: () => {
          findCmdCalls++;
          return ["true"]; // a real no-op command; spawn() must succeed
        },
        connectIntervalMs: 20,
        connectTimeoutMs: 60, // deliberately shorter than the 150ms the socket takes to appear
        retryIntervalMs: 20,
      });
      // findDaemonCmd (and therefore spawn) must only ever run once -- a
      // slow app start must not launch a second instance on every retry.
      assert.equal(findCmdCalls, 1);
    } finally {
      clearTimeout(timer);
      lateServer?.close();
      cleanup();
    }
  });
});

describe("describeTarget", () => {
  // What a shim parked in waitForDaemonPatiently reports about itself. It
  // is the only evidence such a shim leaves anywhere: it answers nothing on
  // stdio, and the daemon logs nothing either, having never been connected
  // to. Telling the two reasons apart is the whole point.
  it("reports a missing discovery file as the daemon not running", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-target-"));
    const missing = path.join(dir, "mcp_url");
    assert.match(describeTarget(missing), /daemon not running/);
  });

  it("reports an empty discovery file distinctly", () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    try {
      writeUrl("");
      assert.match(describeTarget(mcpUrlFile), /is empty/);
    } finally {
      cleanup();
    }
  });

  it("reports a file that does not hold a URL", () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    try {
      writeUrl("not-a-url");
      assert.match(describeTarget(mcpUrlFile), /does not contain a URL/);
    } finally {
      cleanup();
    }
  });

  it("names the URL itself when one is present but nothing is listening", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    try {
      const port = await getFreePort();
      writeUrl(`http://127.0.0.1:${port}/mcp`);
      const described = describeTarget(mcpUrlFile);
      assert.match(described, /not accepting connections/);
      assert.match(described, new RegExp(String(port)));
    } finally {
      cleanup();
    }
  });
});

describe("ensureDaemonRunning on a privilege-separated install", () => {
  it("never spawns the daemon -- the service manager owns it, and this process is the wrong account", async () => {
    // Spawning here would start the daemon as the logged-in user, where
    // privilege_separation.check_runtime_identity() refuses to run rather
    // than seed a default policy over the real one. So the spawn cannot
    // succeed; attempting it once per shim launch would only bury the real
    // reason under a second failure.
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const port = await getFreePort();
    const server = net.createServer();
    setTimeout(() => {
      server.listen(port, "127.0.0.1", () => writeUrl(`http://127.0.0.1:${port}/mcp`));
    }, 150);
    let findCmdCalled = false;
    try {
      await ensureDaemonRunning({
        mcpUrlFile,
        separationRoot: () => "/Library/Application Support/PrivacyFence",
        findCmd: () => {
          findCmdCalled = true;
          return ["should-not-run"];
        },
        connectTimeoutMs: 3000,
        connectIntervalMs: 50,
      });
      assert.equal(findCmdCalled, false);
    } finally {
      server.close();
      cleanup();
    }
  });

  it("names each platform's own service manager in the wait message", async () => {
    // Diagnostics, but load-bearing diagnostics: this message is the only
    // thing a user sees when a separated daemon has not come up, and it is
    // what sends them to `sc.exe query` rather than to launchctl on a
    // machine that has no launchd. Windows has a separated install too, so
    // a two-way branch (linux, else macOS) would be wrong, not merely
    // incomplete.
    const original = Object.getOwnPropertyDescriptor(process, "platform")!;
    const originalError = console.error;
    const lines: string[] = [];
    console.error = (...args: unknown[]) => {
      lines.push(args.join(" "));
    };
    const { mcpUrlFile, cleanup } = makeTempMcpFiles();
    try {
      Object.defineProperty(process, "platform", { value: "win32", configurable: true });
      await assert.rejects(
        ensureDaemonRunning({
          mcpUrlFile,
          separationRoot: () => "C:\\ProgramData\\PrivacyFence",
          findCmd: () => ["should-not-run"],
          connectTimeoutMs: 120,
          connectIntervalMs: 20,
        }),
        ShimExitError,
      );
    } finally {
      Object.defineProperty(process, "platform", original);
      console.error = originalError;
      cleanup();
    }
    const waiting = lines.find((line) => line.includes("under its own account"));
    assert.ok(waiting, `no wait message was logged; got ${JSON.stringify(lines)}`);
    assert.match(waiting, /a Windows service/);
    assert.match(waiting, /sc\.exe query PrivacyFence/);
  });

  it("still gives up after the connect window, so waitForDaemonPatiently keeps retrying", async () => {
    const { mcpUrlFile, cleanup } = makeTempMcpFiles();
    try {
      await assert.rejects(
        ensureDaemonRunning({
          mcpUrlFile,
          separationRoot: () => "/Library/Application Support/PrivacyFence",
          findCmd: () => ["should-not-run"],
          connectTimeoutMs: 120,
          connectIntervalMs: 20,
        }),
        ShimExitError,
      );
    } finally {
      cleanup();
    }
  });
});
