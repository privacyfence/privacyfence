/**
 * Worker backing https://downloads.privacyfence.eu -- the only public path to the private
 * `privacyfence-releases` R2 bucket (docs/downloads-and-release-kpi.md). The browser
 * never receives R2 credentials or a direct bucket URL: every installer download streams
 * through this Worker, which also records a privacy-preserving download count in D1 (no IP,
 * cookie, fingerprint, or User-Agent -- see migrations/0001_download_counts.sql).
 *
 * Routes:
 *   GET/HEAD /download/<channel>/<artifact-id>            -- e.g. /download/stable/macos-arm64
 *   GET/HEAD /download/version/<version>/<artifact-id>    -- pin to an exact, possibly older, version
 *   GET      /api/releases                                -- latest manifest per channel
 *   GET      /api/releases/<channel>                       -- latest manifest for one channel
 *   GET      /api/releases/history                         -- every published version, newest first
 *   GET      /api/stats/downloads                          -- aggregated D1 counters
 *   GET      /health                                       -- liveness probe, touches no binding
 *   GET/HEAD /robots.txt                                   -- keeps crawlers off /download/, touches no binding
 *
 * `<artifact-id>` matches a manifest artifact's own `id` field (e.g. "macos-arm64",
 * "windows-x64", "linux-x64") -- see manifest.ts's Manifest/ManifestArtifact types, which match
 * the manifest.json scripts/r2_release.py writes exactly.
 */
import { CHANNELS, isChannel, type Channel } from "./channel.js";
import {
  findArtifact,
  publicManifest,
  resolveLatestManifest,
  resolveVersionManifest,
  type Manifest,
} from "./manifest.js";
import { recordDownload, queryStats } from "./counters.js";
import { artifactHeaders, isDownloadStart, parseRangeHeader } from "./artifacts.js";
import { corsPreflight, jsonResponse, methodNotAllowed, notFound, withCors } from "./http.js";
import { listReleaseHistory } from "./history.js";

// `Env` (RELEASES/DB bindings) is declared globally in ../worker-configuration.d.ts, matching
// wrangler.toml -- no import needed, same as any other Workers project's generated types.

function hasBody(object: R2Object | R2ObjectBody): object is R2ObjectBody {
  return "body" in object;
}

async function serveArtifact(
  request: Request,
  env: Env,
  ctx: ExecutionContext,
  channel: Channel,
  manifest: Manifest,
  artifactId: string,
): Promise<Response> {
  const artifact = findArtifact(manifest, artifactId);
  if (!artifact) return notFound(`no such artifact: ${artifactId}`);

  if (request.method === "HEAD") {
    const head = await env.RELEASES.head(artifact.key);
    if (!head) return notFound("artifact is missing from storage");
    return new Response(null, { status: 200, headers: artifactHeaders(artifact, undefined, head.httpEtag) });
  }

  // This Worker parses `Range` itself (see parseRangeHeader's docstring for why) and only passes
  // R2 the resolved range, rather than handing it the raw request Headers to parse internally.
  // `onlyIf` still gets the request's own Headers directly -- R2's conditional-request parsing
  // (If-Match/If-None-Match/...) has no equivalent ambiguity to work around.
  const requestedRange = parseRangeHeader(request.headers.get("Range"));
  const getOptions: R2GetOptions & { onlyIf: Headers } = { onlyIf: request.headers };
  if (requestedRange) getOptions.range = requestedRange;
  const object = await env.RELEASES.get(artifact.key, getOptions);
  if (!object) return notFound("artifact is missing from storage");
  if (!hasBody(object)) {
    // A conditional request's precondition failed (e.g. If-None-Match matched) -- R2 returns
    // metadata with no body in that case. Never counts: nothing was actually downloaded.
    return new Response(null, { status: 304 });
  }

  if (isDownloadStart(requestedRange)) {
    ctx.waitUntil(
      recordDownload(env.DB, {
        channel,
        version: manifest.version,
        platform: artifact.platform,
        architecture: artifact.architecture,
        artifactKind: artifact.kind,
      }),
    );
  }

  return new Response(object.body, {
    status: requestedRange ? 206 : 200,
    headers: artifactHeaders(artifact, requestedRange, object.httpEtag),
  });
}

async function routeDownload(path: string, request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const versionMatch = /^\/download\/version\/([^/]+)\/([^/]+)$/.exec(path);
  if (versionMatch) {
    const version = decodeURIComponent(versionMatch[1]!);
    const rawArtifactId = versionMatch[2]!;
    let resolved;
    try {
      resolved = await resolveVersionManifest(env.RELEASES, version);
    } catch (err) {
      return notFound(err instanceof Error ? err.message : `invalid version: ${version}`);
    }
    if (!resolved) return notFound(`no such release version: ${version}`);
    return serveArtifact(request, env, ctx, resolved.channel, resolved.manifest, decodeURIComponent(rawArtifactId));
  }

  const channelMatch = /^\/download\/([^/]+)\/([^/]+)$/.exec(path);
  if (channelMatch) {
    const channel = channelMatch[1]!;
    const rawArtifactId = channelMatch[2]!;
    if (!isChannel(channel)) return notFound(`unknown channel: ${channel}`);
    const manifest = await resolveLatestManifest(env.RELEASES, channel);
    if (!manifest) return notFound(`no published release for channel: ${channel}`);
    return serveArtifact(request, env, ctx, channel, manifest, decodeURIComponent(rawArtifactId));
  }

  return notFound("no such download route");
}

async function handleStats(env: Env): Promise<Response> {
  try {
    return jsonResponse(await queryStats(env.DB));
  } catch (err) {
    console.error("privacyfence-downloads: stats query failed", err);
    // A stats failure must never look like "zero downloads" -- surface it so the website
    // download page can hide the stats section instead of showing a wrong number.
    return jsonResponse({ error: "stats temporarily unavailable" }, 503);
  }
}

// Crawlers that honor robots.txt stay off the counted download routes, so they never inflate the
// download KPI (docs/downloads-and-release-kpi.md). /api/* and /health stay allowed: metadata
// only, never counted. A host with no robots.txt (a 404) reads to a crawler as "allow everything".
const ROBOTS_TXT = "User-agent: *\nDisallow: /download/\n";
const ROBOTS_MAX_AGE_SECONDS = 86400;

function robotsTxt(request: Request): Response {
  return new Response(request.method === "HEAD" ? null : ROBOTS_TXT, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": `public, max-age=${ROBOTS_MAX_AGE_SECONDS}`,
    },
  });
}

// How long release metadata may be reused, by browsers and other HTTP caches and by this
// Worker's edge cache. Short, so a new release shows up within minutes; long enough that page
// views cannot drive the R2 reads (and, for the history, list operations) behind every response.
const RELEASES_MAX_AGE_SECONDS = 300;
const RELEASES_CACHE_CONTROL = { "Cache-Control": `public, max-age=${RELEASES_MAX_AGE_SECONDS}` };

/**
 * Serves a release-metadata route from the edge cache, or builds it with `produce` and caches it.
 * One entry per route path: the key has no query string and no Origin (CORS is added by the
 * caller, after the cache), so neither can be used to bypass it. Only a 200 is cached -- a 404
 * for a channel with nothing published yet must not outlive its first release, and a 503 must
 * not outlive the outage.
 */
async function cachedRelease(
  path: string,
  request: Request,
  ctx: ExecutionContext,
  produce: () => Promise<Response>,
): Promise<Response> {
  const cacheKey = new Request(new URL(path, request.url).toString());
  const cached = await caches.default.match(cacheKey);
  if (cached) return cached;
  const response = await produce();
  if (response.status === 200) ctx.waitUntil(caches.default.put(cacheKey, response.clone()));
  return response;
}

async function handleHistory(env: Env): Promise<Response> {
  let releases;
  try {
    releases = await listReleaseHistory(env.RELEASES);
  } catch (err) {
    console.error("privacyfence-downloads: release history failed", err);
    // Not cached: the website falls back to /api/releases, and the next request retries.
    return jsonResponse({ error: "release history temporarily unavailable" }, 503, { "Cache-Control": "no-store" });
  }
  return jsonResponse({ releases }, 200, RELEASES_CACHE_CONTROL);
}

async function handleLatest(env: Env): Promise<Response> {
  const entries = await Promise.all(
    CHANNELS.map(async (channel) => {
      const manifest = await resolveLatestManifest(env.RELEASES, channel);
      return [channel, manifest && publicManifest(manifest)] as const;
    }),
  );
  return jsonResponse({ channels: Object.fromEntries(entries) }, 200, RELEASES_CACHE_CONTROL);
}

async function handleChannel(env: Env, channel: Channel): Promise<Response> {
  const manifest = await resolveLatestManifest(env.RELEASES, channel);
  if (!manifest) return notFound(`no published release for channel: ${channel}`);
  return jsonResponse(publicManifest(manifest), 200, RELEASES_CACHE_CONTROL);
}

async function routeApi(path: string, request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  if (path === "/api/releases") return cachedRelease(path, request, ctx, () => handleLatest(env));

  // Before the channel route, which would otherwise read "history" as an unknown channel.
  if (path === "/api/releases/history") return cachedRelease(path, request, ctx, () => handleHistory(env));

  const channelMatch = /^\/api\/releases\/([^/]+)$/.exec(path);
  if (channelMatch) {
    const channel = channelMatch[1]!;
    if (!isChannel(channel)) return notFound(`unknown channel: ${channel}`);
    return cachedRelease(path, request, ctx, () => handleChannel(env, channel));
  }

  if (path === "/api/stats/downloads") return handleStats(env);

  return notFound("no such API route");
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);

    if (pathname === "/health") {
      if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
      // Deliberately touches neither binding: a real outage in R2/D1 shouldn't also take down
      // the liveness probe used to check the Worker itself deployed correctly.
      return jsonResponse({ status: "ok" });
    }

    if (pathname === "/robots.txt") {
      if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
      // Touches neither binding and records nothing, same as /health.
      return robotsTxt(request);
    }

    if (pathname.startsWith("/api/")) {
      if (request.method === "OPTIONS") return corsPreflight(request);
      if (request.method !== "GET") return withCors(request, methodNotAllowed(["GET", "OPTIONS"]));
      return withCors(request, await routeApi(pathname, request, env, ctx));
    }

    if (pathname.startsWith("/download/")) {
      if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
      return routeDownload(pathname, request, env, ctx);
    }

    return notFound("no such route");
  },
} satisfies ExportedHandler<Env>;
