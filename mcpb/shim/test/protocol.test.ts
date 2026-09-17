import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, it } from "node:test";
import {
  dataDir,
  handoffDir,
  privilegeSeparationRoot,
  readMcpToken,
  readMcpUrl,
  SYSTEM_ROOTS,
  defaultSystemRoot,
  windowsDataDir,
} from "../src/protocol.js";

/** process.platform is a getter Node defines as configurable but not
 * writable -- redefine it per test and always restore, so a failure here
 * can't leak a fake platform into an unrelated test. */
function withPlatform(value: NodeJS.Platform, fn: () => void): void {
  const original = Object.getOwnPropertyDescriptor(process, "platform")!;
  Object.defineProperty(process, "platform", { value, configurable: true });
  try {
    fn();
  } finally {
    Object.defineProperty(process, "platform", original);
  }
}

describe("dataDir", () => {
  const originalLocalAppData = process.env.LOCALAPPDATA;

  afterEach(() => {
    if (originalLocalAppData === undefined) {
      delete process.env.LOCALAPPDATA;
    } else {
      process.env.LOCALAPPDATA = originalLocalAppData;
    }
  });

  it("uses ~/.privacyfence on non-Windows platforms", () => {
    withPlatform("linux", () => {
      assert.equal(dataDir(), path.join(os.homedir(), ".privacyfence"));
    });
  });

  it("uses %LOCALAPPDATA%\\PrivacyFence on Windows", () => {
    process.env.LOCALAPPDATA = "C:\\Users\\alice\\AppData\\Local";
    withPlatform("win32", () => {
      assert.equal(dataDir(), path.join("C:\\Users\\alice\\AppData\\Local", "PrivacyFence"));
    });
  });

  it("falls back to ~\\AppData\\Local\\PrivacyFence when LOCALAPPDATA is unset", () => {
    delete process.env.LOCALAPPDATA;
    assert.equal(windowsDataDir(), path.join(os.homedir(), "AppData", "Local", "PrivacyFence"));
  });
});

describe("readMcpUrl", () => {
  it("returns the trimmed URL text", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_url");
    fs.writeFileSync(file, "http://localhost:8765/mcp\n");
    assert.equal(readMcpUrl(file), "http://localhost:8765/mcp");
  });

  it("throws on an empty file", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_url");
    fs.writeFileSync(file, "\n");
    assert.throws(() => readMcpUrl(file), /Empty MCP URL/);
  });

  it("throws on a malformed URL", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_url");
    fs.writeFileSync(file, "not a url");
    assert.throws(() => readMcpUrl(file));
  });

  it("throws when the file doesn't exist", () => {
    assert.throws(() => readMcpUrl("/definitely/does/not/exist/mcp_url"));
  });
});

describe("readMcpToken", () => {
  it("returns the trimmed token text", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_token");
    fs.writeFileSync(file, "abc123\n");
    assert.equal(readMcpToken(file), "abc123");
  });
});

describe("privilegeSeparationRoot / handoffDir (#428 Phase 4)", () => {
  /** A temp directory standing in for the macOS system root
   * scripts/macos_privilege_separation.sh provisions, with whatever marker
   * the test wants inside it. */
  function withMarker(contents: string | null, fn: (root: string, env: NodeJS.ProcessEnv) => void): void {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "pf-sep-"));
    try {
      if (contents !== null) {
        fs.writeFileSync(path.join(root, "privilege-separation.json"), contents);
      }
      fn(root, { PRIVACYFENCE_SYSTEM_ROOT: root });
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }

  const validMarker = (platform: string = process.platform, version = 1) =>
    JSON.stringify({
      version,
      platform,
      service_account: "_privacyfence",
      service_group: "_privacyfence",
      owner_user: "alice",
    });

  it("finds a well-formed marker and resolves the handoff directory", () => {
    withMarker(validMarker(), (root, env) => {
      assert.equal(privilegeSeparationRoot(env), root);
      assert.equal(handoffDir(env), path.join(root, "handoff"));
    });
  });

  it("falls back to the ordinary data directory when there is no marker", () => {
    withMarker(null, (_root, env) => {
      assert.equal(privilegeSeparationRoot(env), null);
      assert.equal(handoffDir(env), dataDir());
    });
  });

  // Every rejection below has to land on dataDir(), not on a throw: a shim
  // that guesses wrong here reports "daemon not running" for a daemon that is
  // running fine, which is a far worse failure than ignoring a marker it
  // doesn't understand.
  it("ignores a marker from a newer PrivacyFence", () => {
    withMarker(validMarker(process.platform, 2), (_root, env) => {
      assert.equal(privilegeSeparationRoot(env), null);
    });
  });

  it("ignores another platform's marker", () => {
    withMarker(validMarker("win32"), (_root, env) => {
      withPlatform("darwin", () => {
        assert.equal(privilegeSeparationRoot(env), null);
      });
    });
  });

  it("ignores a malformed marker", () => {
    withMarker("{not json", (_root, env) => {
      assert.equal(privilegeSeparationRoot(env), null);
    });
  });

  it("ignores a relative PRIVACYFENCE_SYSTEM_ROOT on a platform with no default", () => {
    // freebsd, not win32: B5c gave Windows a default root of its own, so the
    // only platforms left with none are the ones #428 P4 has no installer
    // for at all.
    withPlatform("freebsd", () => {
      assert.equal(privilegeSeparationRoot({ PRIVACYFENCE_SYSTEM_ROOT: "relative/path" }), null);
    });
  });

  it("looks for no marker at all on a platform #428 P4 has not shipped for", () => {
    // All three desktop platforms have an installer as of B5c, so this is
    // now about the ones that never will: there is nothing that could have
    // written a marker on freebsd, and going looking for one would mean
    // reading a path this shim invented.
    withPlatform("freebsd", () => {
      assert.equal(privilegeSeparationRoot({}), null);
    });
  });

  it("prefers %ProgramData% to the hardcoded C: default on Windows", () => {
    // Mirrors privilege_separation.system_root()'s own Windows branch: that
    // folder can be redirected to another volume, the installer's icacls
    // runs against wherever it really is, and resolving the literal would
    // send this shim somewhere nothing was provisioned. The literal stays as
    // the fallback for a process started without the variable at all.
    withPlatform("win32", () => {
      assert.equal(defaultSystemRoot({ ProgramData: "D:\\ProgramData" }), path.join("D:\\ProgramData", "PrivacyFence"));
      assert.equal(defaultSystemRoot({}), SYSTEM_ROOTS.win32);
    });
  });

  it("knows each shipped platform's own default root (#428 P4 B5a/B5b/B5c)", () => {
    // The roots themselves, not just the marker logic: this is the shim's
    // half of the contract with privilege_separation.PLATFORM_LAYOUTS, whose
    // own test reads this same table back from source and asserts the two
    // agree. Getting one wrong means the shim looks for mcp_url in a
    // directory no installer provisioned, which presents as "daemon not
    // running" against a daemon that is running fine.
    assert.deepEqual(SYSTEM_ROOTS, {
      darwin: "/Library/Application Support/PrivacyFence",
      linux: "/var/lib/privacyfence",
      win32: "C:/ProgramData/PrivacyFence",
    });
  });

  it("resolves the handoff directory on every platform that has a root", () => {
    for (const platform of ["darwin", "linux", "win32"] as const) {
      withPlatform(platform, () => {
        withMarker(validMarker(platform), (root, env) => {
          assert.equal(privilegeSeparationRoot(env), root);
          assert.equal(handoffDir(env), path.join(root, "handoff"));
        });
      });
    }
  });

  describe("PRIVACYFENCE_SYSTEM_ROOT guard (B11)", () => {
    // This shim's environment is whatever the logged-in user's session set,
    // unlike the daemon's own (launchd/systemd-controlled) one. So once a
    // real install is provisioned at the platform's actual default root, the
    // override must be refused -- honouring it would let a user-session
    // process redirect the shim onto a root it controls instead of the one
    // the installer provisioned and locked down.

    /** Stands in for the platform's real, un-overridden default root by
     * pointing %ProgramData% at a tmp_path -- win32 is the one platform
     * whose default root (defaultSystemRoot()) is read from the environment
     * rather than hardcoded, so this exercises the guard without writing to
     * an actual system directory. defaultSystemRoot() appends "PrivacyFence"
     * to %ProgramData%, so that's where the marker has to live too. */
    function withDefaultRoot(fn: (programData: string, defaultRoot: string) => void): void {
      const programData = fs.mkdtempSync(path.join(os.tmpdir(), "pf-sep-programdata-"));
      const defaultRoot = path.join(programData, "PrivacyFence");
      fs.mkdirSync(defaultRoot);
      try {
        fn(programData, defaultRoot);
      } finally {
        fs.rmSync(programData, { recursive: true, force: true });
      }
    }

    it("refuses the override when the real default root already has a marker", () => {
      withPlatform("win32", () => {
        withDefaultRoot((programData, defaultRoot) => {
          fs.writeFileSync(path.join(defaultRoot, "privilege-separation.json"), validMarker("win32"));
          const attackerRoot = fs.mkdtempSync(path.join(os.tmpdir(), "pf-sep-attacker-"));
          try {
            const env = { ProgramData: programData, PRIVACYFENCE_SYSTEM_ROOT: attackerRoot };
            assert.equal(privilegeSeparationRoot(env), defaultRoot);
            assert.equal(handoffDir(env), path.join(defaultRoot, "handoff"));
          } finally {
            fs.rmSync(attackerRoot, { recursive: true, force: true });
          }
        });
      });
    });

    it("still honours the override when the real default root has no marker", () => {
      withPlatform("win32", () => {
        withDefaultRoot((programData) => {
          // defaultRoot exists but was never provisioned -- no marker in it.
          const testRoot = fs.mkdtempSync(path.join(os.tmpdir(), "pf-sep-test-"));
          try {
            fs.writeFileSync(path.join(testRoot, "privilege-separation.json"), validMarker("win32"));
            const env = { ProgramData: programData, PRIVACYFENCE_SYSTEM_ROOT: testRoot };
            assert.equal(privilegeSeparationRoot(env), testRoot);
          } finally {
            fs.rmSync(testRoot, { recursive: true, force: true });
          }
        });
      });
    });
  });
});
