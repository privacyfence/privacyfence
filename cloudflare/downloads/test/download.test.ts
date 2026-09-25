/**
 * Integration tests against the Worker's own `fetch()` handler, running inside the real Workers
 * runtime (workerd via Miniflare) with the R2/D1 fixtures test/setup.ts seeds. Covers channel
 * resolution, artifact classification and every counting rule in
 * docs/downloads-and-release-kpi.md "Counting semantics".
 */
import { createExecutionContext, env, waitOnExecutionContext } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";
import { CHANNELS } from "../src/channel";
import worker from "../src/index";
import stableOldManifest from "./fixtures/stable/old-manifest.json";

const ORIGIN = "https://downloads.privacyfence.eu";

async function call(path: string, init?: RequestInit): Promise<Response> {
  const ctx = createExecutionContext();
  // `cf`-properties generics differ between a plain `new Request()` and the incoming-request type
  // `ExportedHandler#fetch` expects; workerd fills in real `cf` properties for us at the HTTP
  // layer, so this cast is just bridging two TS-only shapes of the same runtime `Request`.
  const request = new Request(`${ORIGIN}${path}`, init) as Request<unknown, IncomingRequestCfProperties>;
  const response = await worker.fetch(request, env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

// The Worker edge-caches these routes, and the Cache API (like R2 here) outlives each test: every
// test starts without the responses an earlier one left behind.
const CACHED_PATHS = [
  "/api/releases",
  "/api/releases/history",
  ...CHANNELS.map((channel) => `/api/releases/${channel}`),
];
beforeEach(async () => {
  await Promise.all(CACHED_PATHS.map((path) => caches.default.delete(new Request(`${ORIGIN}${path}`))));
});

/** Runs `check` with `key` removed from R2, then puts it back whatever the outcome. */
async function withoutObject(key: string, check: () => Promise<void>): Promise<void> {
  const original = await env.RELEASES.get(key);
  if (!original) throw new Error(`fixture missing: ${key}`);
  const body = await original.text();
  await env.RELEASES.delete(key);
  try {
    await check();
  } finally {
    await env.RELEASES.put(key, body);
  }
}

async function statsTotal(): Promise<number> {
  const response = await call("/api/stats/downloads");
  const body = (await response.json()) as { total: number };
  return body.total;
}

describe("GET /health", () => {
  it("returns 200 without touching R2/D1", async () => {
    const response = await call("/health");
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });
});

describe("channel resolution", () => {
  it.each([
    ["stable", "PrivacyFence-4.3.0.dmg"],
    ["beta", "PrivacyFence-4.4.0b1.dmg"],
    ["alpha", "PrivacyFence-4.4.0a1.dmg"],
    ["rc", "PrivacyFence-4.4.0rc1.dmg"],
  ])("resolves the %s channel's latest macos-arm64 installer", async (channel, filename) => {
    const response = await call(`/download/${channel}/macos-arm64`);
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe(`attachment; filename="${filename}"`);
  });

  it("404s for an unknown channel", async () => {
    const response = await call("/download/nightly/macos-arm64");
    expect(response.status).toBe(404);
  });
});

describe("artifact classification", () => {
  it.each([
    ["macos-arm64", "PrivacyFence-4.3.0.dmg", "application/x-apple-diskimage"],
    ["windows-x64", "PrivacyFence-4.3.0-setup.exe", "application/vnd.microsoft.portable-executable"],
    ["linux-x64", "privacyfence_4.3.0_amd64.deb", "application/vnd.debian.binary-package"],
  ])("serves the %s artifact with the right filename and content type", async (id, filename, contentType) => {
    const response = await call(`/download/stable/${id}`);
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe(`attachment; filename="${filename}"`);
    expect(response.headers.get("Content-Type")).toBe(contentType);
    expect(await response.text()).toMatch(new RegExp(`^FAKE-.+-stable-4\\.3\\.0-`));
  });

  it("404s for an artifact id the manifest doesn't have", async () => {
    const response = await call("/download/stable/linux-arm64");
    expect(response.status).toBe(404);
  });
});

describe("version-pinned downloads", () => {
  it("serves an older version directly, independent of what's current on the channel", async () => {
    const response = await call("/download/version/4.2.0/macos-arm64");
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe('attachment; filename="PrivacyFence-4.2.0.dmg"');
    expect(await response.text()).toBe("FAKE-DMG-BYTES-stable-4.2.0-macos-arm64");
  });

  it("still serves the latest version by its exact version number too", async () => {
    const response = await call("/download/version/4.3.0/macos-arm64");
    expect(response.status).toBe(200);
  });

  it("404s -- safely, not a crash -- for a version that was never published", async () => {
    const response = await call("/download/version/9.9.9/macos-arm64");
    expect(response.status).toBe(404);
  });

  it("404s -- safely -- for a string that isn't a real version", async () => {
    const response = await call("/download/version/not-a-version/macos-arm64");
    expect(response.status).toBe(404);
  });
});

describe("HEAD requests", () => {
  it("returns metadata with no body and the expected filename", async () => {
    const response = await call("/download/stable/macos-arm64", { method: "HEAD" });
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe('attachment; filename="PrivacyFence-4.3.0.dmg"');
    expect(response.headers.get("Content-Length")).toBe("39");
    expect(await response.text()).toBe("");
  });
});

describe("download counting", () => {
  it("a full GET increments the counter; a HEAD does not", async () => {
    const before = await statsTotal();

    await call("/download/stable/macos-arm64");
    expect(await statsTotal()).toBe(before + 1);

    await call("/download/stable/macos-arm64", { method: "HEAD" });
    expect(await statsTotal()).toBe(before + 1);
  });

  it("Range: bytes=0-... counts; Range: bytes=<N>- (N>0) does not", async () => {
    const before = await statsTotal();

    await call("/download/stable/macos-arm64", { headers: { Range: "bytes=0-9" } });
    expect(await statsTotal()).toBe(before + 1);

    await call("/download/stable/macos-arm64", { headers: { Range: "bytes=10-" } });
    expect(await statsTotal()).toBe(before + 1);
  });

  it("a 404 (missing artifact) never increments the counter", async () => {
    const before = await statsTotal();
    const response = await call("/download/stable/does-not-exist");
    expect(response.status).toBe(404);
    expect(await statsTotal()).toBe(before);
  });

  it("the metadata and stats APIs never increment the counter", async () => {
    const before = await statsTotal();
    await call("/api/releases");
    await call("/api/releases/stable");
    await call("/api/releases/history");
    await call("/api/stats/downloads");
    expect(await statsTotal()).toBe(before);
  });
});

describe("range responses", () => {
  it("returns 206 with a matching Content-Range for a partial request", async () => {
    const response = await call("/download/stable/macos-arm64", { headers: { Range: "bytes=0-9" } });
    expect(response.status).toBe(206);
    expect(response.headers.get("Content-Range")).toBe("bytes 0-9/39");
    expect(response.headers.get("Content-Length")).toBe("10");
    expect(await response.text()).toBe("FAKE-DMG-B");
  });
});

describe("GET /api/releases", () => {
  it("reports the latest manifest for every channel", async () => {
    const response = await call("/api/releases");
    expect(response.status).toBe(200);
    const body = (await response.json()) as { channels: Record<string, { version: string } | null> };
    expect(body.channels.stable?.version).toBe("4.3.0");
    expect(body.channels.beta?.version).toBe("4.4.0b1");
    expect(body.channels.alpha?.version).toBe("4.4.0a1");
    expect(body.channels.rc?.version).toBe("4.4.0rc1");
  });
});

describe("GET /api/releases/:channel", () => {
  it("returns that channel's manifest", async () => {
    const response = await call("/api/releases/beta");
    expect(response.status).toBe(200);
    expect((await response.json()) as { version: string }).toMatchObject({ version: "4.4.0b1", channel: "beta" });
  });

  it("404s for an unknown channel", async () => {
    expect((await call("/api/releases/nightly")).status).toBe(404);
  });
});

interface HistoryBody {
  releases: { version: string; channel: string; artifacts: { id: string; kind: string }[] }[];
}

// R2 writes here outlive the test (the Cache API does too, see beforeEach below), so an object a
// test adds is removed again whatever the outcome.
async function withTemporaryObject(key: string, body: string, check: () => Promise<void>): Promise<void> {
  await env.RELEASES.put(key, body);
  try {
    await check();
  } finally {
    await env.RELEASES.delete(key);
  }
}

// A stable version between 4.2.0 and the current 4.3.0, published after the history was cached.
const NEW_MANIFEST_KEY = "releases/stable/4.2.1/manifest.json";
const NEW_MANIFEST = JSON.stringify({ ...stableOldManifest, version: "4.2.1", published_at: "2026-07-01T12:00:00Z" });

async function history(init?: RequestInit): Promise<HistoryBody> {
  const response = await call("/api/releases/history", init);
  expect(response.status).toBe(200);
  return (await response.json()) as HistoryBody;
}

describe("GET /api/releases/history", () => {
  it("lists every published version on every channel, newest first", async () => {
    const body = await history();
    expect(body.releases.map((release) => [release.version, release.channel])).toEqual([
      ["4.4.0rc1", "rc"],
      ["4.4.0b1", "beta"],
      ["4.4.0a1", "alpha"],
      ["4.3.0", "stable"],
      ["4.3.0rc1", "rc"],
      ["4.2.0", "stable"],
    ]);
  });

  it("leaves out a manifest newer than the channel's latest.json, and a directory that is not a version", async () => {
    const versions = (await history()).releases.map((release) => release.version);
    expect(versions).not.toContain("4.4.0");
    expect(versions).not.toContain("not-a-version");
  });

  it("passes on installers only", async () => {
    const rc = (await history()).releases.find((release) => release.version === "4.3.0rc1");
    expect(rc?.artifacts.map((artifact) => [artifact.id, artifact.kind])).toEqual([["macos-arm64", "installer"]]);
  });

  it("is cacheable and served from the cache, so page views cannot drive R2 reads", async () => {
    const first = await call("/api/releases/history");
    expect(first.headers.get("Cache-Control")).toBe("public, max-age=300");
    await first.text();

    await withTemporaryObject(NEW_MANIFEST_KEY, NEW_MANIFEST, async () => {
      const versions = (await history()).releases.map((release) => release.version);
      expect(versions).not.toContain("4.2.1");
    });
  });

  it("shares one cache entry whatever the query string", async () => {
    await (await call("/api/releases/history?nocache=1")).text();
    await withTemporaryObject(NEW_MANIFEST_KEY, NEW_MANIFEST, async () => {
      const versions = (await history()).releases.map((release) => release.version);
      expect(versions).not.toContain("4.2.1");
    });
  });

  it("lists a newly published older version once the cache entry is gone", async () => {
    await withTemporaryObject(NEW_MANIFEST_KEY, NEW_MANIFEST, async () => {
      const versions = (await history()).releases.map((release) => release.version);
      expect(versions).toContain("4.2.1");
    });
  });

  it("skips an unreadable manifest instead of failing the whole list", async () => {
    await withTemporaryObject("releases/beta/4.3.0b1/manifest.json", "{not json", async () => {
      const versions = (await history()).releases.map((release) => release.version);
      expect(versions).not.toContain("4.3.0b1");
      expect(versions).toContain("4.4.0b1");
    });
  });

  it("returns an uncached 503 when R2 itself is broken", async () => {
    const realReleases = env.RELEASES;
    // @ts-expect-error -- intentionally swapping in a broken stand-in for this one test
    env.RELEASES = {
      get() {
        throw new Error("R2 is down");
      },
      list() {
        throw new Error("R2 is down");
      },
    };
    try {
      const response = await call("/api/releases/history");
      expect(response.status).toBe(503);
      expect(response.headers.get("Cache-Control")).toBe("no-store");
    } finally {
      env.RELEASES = realReleases;
    }
    expect((await history()).releases.length).toBeGreaterThan(0);
  });

  it("allows privacyfence.eu through CORS", async () => {
    const response = await call("/api/releases/history", { headers: { Origin: "https://privacyfence.eu" } });
    expect(response.headers.get("Access-Control-Allow-Origin")).toBe("https://privacyfence.eu");
  });

  it("rejects POST", async () => {
    expect((await call("/api/releases/history", { method: "POST" })).status).toBe(405);
  });
});

describe("metadata routes", () => {
  it.each(["/api/releases", "/api/releases/stable", "/api/releases/rc", "/api/releases/history"])(
    "%s never exposes an R2 key or path",
    async (path) => {
      const response = await call(path);
      expect(response.status).toBe(200);
      const text = await response.text();
      expect(text).toContain('"sha256"'); // the artifacts are there, just without their keys
      expect(text).not.toMatch(/"key"/);
      expect(text).not.toMatch(/releases\//);
      expect(text).not.toMatch(/r2\.|cloudflarestorage/);
    },
  );

  it.each(["/api/releases", "/api/releases/stable", "/api/releases/history"])(
    "%s may be cached for five minutes",
    async (path) => {
      const response = await call(path);
      expect(response.status).toBe(200);
      expect(response.headers.get("Cache-Control")).toBe("public, max-age=300");
    },
  );

  it.each(["/api/releases/nightly", "/api/stats/downloads"])("%s carries no Cache-Control", async (path) => {
    // A 404 must not stick once the channel is published, and the stats are live counts.
    expect((await call(path)).headers.get("Cache-Control")).toBeNull();
  });

  it.each(["/api/releases", "/api/releases/beta"])("%s is served from the edge cache", async (path) => {
    const first = await call(path);
    expect(first.status).toBe(200);
    const body = await first.text();
    await withoutObject("releases/beta/latest.json", async () => {
      const again = await call(`${path}?nocache=1`);
      expect(again.status).toBe(200);
      expect(await again.text()).toBe(body);
    });
  });

  it("never caches a 404, so a channel's first release is not hidden behind one", async () => {
    await withoutObject("releases/beta/latest.json", async () => {
      expect((await call("/api/releases/beta")).status).toBe(404);
    });
    expect((await call("/api/releases/beta")).status).toBe(200);
  });

  it("caches each route separately", async () => {
    await (await call("/api/releases/stable")).text();
    const body = (await (await call("/api/releases/beta")).json()) as { version: string };
    expect(body.version).toBe("4.4.0b1");
  });

  it("the latest-per-channel routes keep every other artifact field", async () => {
    const body = (await (await call("/api/releases/stable")).json()) as { artifacts: object[] };
    expect(body.artifacts[0]).toEqual({
      id: "macos-arm64",
      kind: "installer",
      platform: "macos",
      architecture: "arm64",
      filename: "PrivacyFence-4.3.0.dmg",
      size: 39,
      sha256: "938a4c3a3ae6a02a3214a6182582efc906fdf0d06b8aed6de8de21d6c04876ff",
    });
  });

  it("stripping the key from the API leaves downloads resolving it", async () => {
    const response = await call("/download/stable/macos-arm64");
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("FAKE-DMG-BYTES-stable-4.3.0-macos-arm64");
  });
});

describe("GET /api/stats/downloads", () => {
  it("returns an aggregate shape even with no downloads yet in this test's isolated storage", async () => {
    const response = await call("/api/stats/downloads");
    expect(response.status).toBe(200);
    const body = (await response.json()) as { total: number; by_channel: object; by_platform: unknown[] };
    expect(typeof body.total).toBe("number");
    expect(typeof body.by_channel).toBe("object");
    expect(Array.isArray(body.by_platform)).toBe(true);
  });

  it("returns 503, never a fake zero, when D1 itself is broken", async () => {
    const realDb = env.DB;
    // @ts-expect-error -- intentionally swapping in a broken stand-in for this one test
    env.DB = {
      prepare() {
        throw new Error("D1 is down");
      },
    };
    try {
      const response = await call("/api/stats/downloads");
      expect(response.status).toBe(503);
    } finally {
      env.DB = realDb;
    }
  });
});

describe("CORS", () => {
  it("allows privacyfence.eu on /api/* routes", async () => {
    const response = await call("/api/releases", { headers: { Origin: "https://privacyfence.eu" } });
    expect(response.headers.get("Access-Control-Allow-Origin")).toBe("https://privacyfence.eu");
  });

  it("does not reflect an unrecognized origin", async () => {
    const response = await call("/api/releases", { headers: { Origin: "https://evil.example" } });
    expect(response.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });

  it("never sets CORS headers on /download/* routes", async () => {
    const response = await call("/download/stable/macos-arm64", { headers: { Origin: "https://privacyfence.eu" } });
    expect(response.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });
});

describe("method handling", () => {
  it("rejects POST to a download route", async () => {
    const response = await call("/download/stable/macos-arm64", { method: "POST" });
    expect(response.status).toBe(405);
  });

  it("rejects POST to an API route", async () => {
    const response = await call("/api/releases", { method: "POST" });
    expect(response.status).toBe(405);
  });

  it("404s an unknown top-level route", async () => {
    expect((await call("/nonsense")).status).toBe(404);
  });
});
