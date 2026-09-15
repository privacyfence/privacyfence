import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, it } from "node:test";
import { dataDir, readMcpToken, readMcpUrl, windowsDataDir } from "../src/protocol.js";

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
