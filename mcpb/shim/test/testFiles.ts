/**
 * A short-lived temp directory holding a fake mcp_url file and a token for the
 * fake daemon to expect, plus
 * cleanup -- the shim-side analogue of bridge/test/testDaemon.ts's
 * makeTempIpcFiles(). writeUrl() lets a test fill in the URL once it knows
 * the real bound port (e.g. after a fake /mcp server's listen() resolves),
 * mirroring how the real daemon only writes mcp_url once it's actually
 * bound (web/server.py's WebServer.start()).
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export function makeTempMcpFiles(token = "test-mcp-token"): {
  mcpUrlFile: string;
  token: string;
  writeUrl: (url: string) => void;
  cleanup: () => void;
} {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-"));
  const mcpUrlFile = path.join(dir, "mcp_url");
  return {
    mcpUrlFile,
    token,
    writeUrl: (url: string) => fs.writeFileSync(mcpUrlFile, url),
    cleanup: () => {
      try {
        fs.unlinkSync(mcpUrlFile);
      } catch {
        // already gone
      }
      try {
        fs.rmdirSync(dir);
      } catch {
        // not empty / already gone
      }
    },
  };
}

/** Bind an ephemeral TCP port on 127.0.0.1, immediately free it, and return
 * the number -- same "probably still free" trick as bridge/test/
 * testDaemon.ts's getFreePort(). */
export async function getFreePort(): Promise<number> {
  const net = await import("node:net");
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const address = srv.address();
      const port = typeof address === "object" && address ? address.port : 0;
      srv.close(() => resolve(port));
    });
  });
}
