/**
 * The OAuth redirect relay behind https://oauth.privacyfence.eu. The contract with the PrivacyFence
 * server (routes, codes, texts, headers) is described in README.md and docs/oauth-relay.md: registration
 * by well-known proof, loopback forwarding, rate limits, the terms redirect, restore, and the sign-in
 * grants with their DNS domain proof.
 *
 * The Worker writes no logs and never stores or logs an authorization `code`.
 */
import { sendNotification } from "./notify";
import type { Registration } from "./registry";
import {
  MAX_SIGNIN_DOMAINS,
  dnsRecordValue,
  forwardPath,
  inPrivateSuffix,
  isSigninGrant,
  loopbackHost,
  parseState,
  publicHost,
  rateLimitKey,
  sha256Hex,
  signinDomainAllowed,
  signinDomainSyntax,
  validRelayHost,
  validSecret,
} from "./validate";

export { Registry } from "./registry";

const TERMS_URL = "https://privacyfence.eu/relay-terms/";
const TERMS_SENTENCE = ` Terms: ${TERMS_URL}`;
const NOT_REGISTERED =
  "This sign-in cannot be completed: the PrivacyFence server it belongs to is not registered with the " +
  "PrivacyFence relay. Ask your administrator.";
const BAD_REDIRECT =
  "This sign-in cannot be completed: the redirect is not a valid PrivacyFence sign-in redirect.";
const HD_REFUSED = "This Google account's organization is not allowed to sign in to this PrivacyFence server.";
const MAX_VERIFIED_AGE_MS = 35 * 24 * 3600 * 1000;
const MAX_BODY_BYTES = 4096;
const PROOF_PATH = "/.well-known/privacyfence-relay";
const DNS_NAME_PREFIX = "_privacyfence-relay.";

const SECURITY_HEADERS: Record<string, string> = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

function reply(status: number, body: string | null, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { ...SECURITY_HEADERS, ...headers } });
}

function json(status: number, value: unknown, headers: Record<string, string> = {}): Response {
  return reply(status, JSON.stringify(value), { "Content-Type": "application/json", ...headers });
}

function text(status: number, body: string, headers: Record<string, string> = {}): Response {
  return reply(status, body, { "Content-Type": "text/plain; charset=utf-8", ...headers });
}

function redirect(location: string): Response {
  return reply(302, null, { Location: location });
}

function error(status: number, code: string): Response {
  return json(status, { error: code });
}

function notFound(): Response {
  return error(404, "not_found");
}

function methodNotAllowed(allow: string[]): Response {
  return json(405, { error: "method_not_allowed" }, { Allow: allow.join(", ") });
}

function registryOf(env: Env): DurableObjectStub<import("./registry").Registry> {
  return env.REGISTRY.get(env.REGISTRY.idFromName("global"));
}

/** Constant-time comparison of two strings through their SHA-256 digests. */
async function safeEqual(a: string, b: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(a)),
    crypto.subtle.digest("SHA-256", encoder.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(da, db);
}

async function authorized(request: Request, env: Env): Promise<boolean> {
  const secret = env.REVOKE_TOKEN;
  if (!secret) return false;
  const header = request.headers.get("Authorization") ?? "";
  const match = /^Bearer (.+)$/.exec(header);
  if (!match) return false;
  return safeEqual(match[1]!, secret);
}

/** Reads at most `limit` bytes of text; null when the body is longer or cannot be read. */
async function readLimited(response: Response, limit: number): Promise<string | null> {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

/** Whether `host` serves `proof` (the SHA-256 of the secret in the request) at its well-known path. */
async function proofMatches(host: string, proof: string): Promise<boolean> {
  try {
    const response = await fetch(`https://${host}${PROOF_PATH}`, {
      redirect: "manual",
      signal: AbortSignal.timeout(5000),
      headers: { Accept: "text/plain" },
    });
    if (response.status !== 200) return false;
    const body = await readLimited(response, 256);
    return body !== null && body.trim() === proof;
  } catch {
    return false;
  }
}

type DnsAnswer = "yes" | "no" | "error";

/** One DNS-over-HTTPS TXT lookup for `_privacyfence-relay.<domain>` (sign-in domain proof). */
async function dnsProof(domain: string, expected: string): Promise<DnsAnswer> {
  try {
    const url = `https://cloudflare-dns.com/dns-query?name=${DNS_NAME_PREFIX}${domain}&type=TXT`;
    const response = await fetch(url, {
      headers: { Accept: "application/dns-json" },
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) return "error";
    const body = (await response.json()) as { Status?: number; Answer?: { type?: number; data?: string }[] };
    if (body.Status !== 0 && body.Status !== 3) return "error";
    for (const answer of body.Answer ?? []) {
      if (answer.type !== 16 || typeof answer.data !== "string") continue;
      const strings = [...answer.data.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]!);
      const value = strings.length > 0 ? strings.join("") : answer.data;
      if (value === expected) return "yes";
    }
    return "no";
  } catch {
    return "error";
  }
}

interface RegisterBody {
  host: string;
  secret: string;
  signinDomains: string[];
}

function parseRegisterBody(raw: string): RegisterBody | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const body = parsed as Record<string, unknown>;
  if (!validRelayHost(body.host) && !(typeof body.host === "string" && loopbackHost(body.host))) return null;
  if (!validSecret(body.secret)) return null;
  let signinDomains: string[] = [];
  if (body.signin_domains !== undefined) {
    const domains = body.signin_domains;
    if (!Array.isArray(domains) || domains.length > MAX_SIGNIN_DOMAINS) return null;
    if (!domains.every(signinDomainSyntax)) return null;
    signinDomains = [...new Set(domains as string[])];
  }
  return { host: body.host as string, secret: body.secret, signinDomains };
}

/** `application/json`, with or without parameters: a cross-site form or `text/plain` post is refused. */
function jsonContentType(request: Request): boolean {
  const value = request.headers.get("Content-Type") ?? "";
  return value.split(";")[0]!.trim().toLowerCase() === "application/json";
}

async function readBody(request: Request): Promise<string | null> {
  const declared = Number(request.headers.get("Content-Length") ?? "0");
  if (declared > MAX_BODY_BYTES) return null;
  const raw = await request.text();
  return raw.length > MAX_BODY_BYTES ? null : raw;
}

function utcDay(now: Date): string {
  return now.toISOString().slice(0, 10);
}

interface NotifyInput {
  host: string;
  ip: string;
  country: string;
  now: Date;
  domains: string[];
  isNew: boolean;
}

/** Sends the maintainers' mail for a first registration, or for sign-in domains a host gained. */
async function notifyMaintainers(env: Env, input: NotifyInput): Promise<void> {
  const registry = registryOf(env);
  const nowIso = input.now.toISOString();
  if (!env.NOTIFY_FROM || !env.GMAIL_CLIENT_ID || !env.GMAIL_CLIENT_SECRET || !env.GMAIL_REFRESH_TOKEN) {
    await registry.recordNotifySkipped();
    return;
  }
  const decision = await registry.reserveNotification(utcDay(input.now));
  if (decision === "none") return;
  let subject: string;
  let body: string;
  if (decision === "limit") {
    subject = "PrivacyFence relay: notification limit reached";
    body = "More new registrations today than the notification limit; see /registrations.";
  } else {
    subject = input.isNew
      ? `New PrivacyFence relay registration: ${input.host}`
      : `PrivacyFence relay sign-in domains added: ${input.host}`;
    const active = await registry.countActive(input.now.getTime(), MAX_VERIFIED_AGE_MS);
    const signin = input.domains.length > 0 ? `yes (${input.domains.join(", ")})` : "no";
    body = [
      `Host: ${input.host}`,
      `Time (UTC): ${nowIso}`,
      `IP: ${input.ip}`,
      `Country: ${input.country}`,
      `Active registrations: ${active}`,
      `Built-in Google sign-in: ${signin}`,
      "",
      "Revoke with:",
      `curl -X POST -H "Authorization: Bearer $REVOKE_TOKEN" -H "Content-Type: application/json" ` +
        `-d '{"host":"${input.host}"}' https://oauth.privacyfence.eu/admin/revoke`,
    ].join("\n");
  }
  const result = await sendNotification(env, subject, body, input.now);
  if (result.status === "sent") {
    if (decision === "send") await registry.markNotified(input.host, input.domains);
  } else if (result.status === "failed") {
    await registry.recordNotifyFailure(result.code, nowIso);
  } else {
    await registry.recordNotifySkipped();
  }
}

async function handleRegister(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed(["POST"]);
  if (!jsonContentType(request)) return error(415, "unsupported_media_type");
  const raw = await readBody(request);
  const body = raw === null ? null : parseRegisterBody(raw);
  if (body === null) return error(400, "invalid_request");
  const { host, secret, signinDomains } = body;
  if (loopbackHost(host) || !publicHost(host) || inPrivateSuffix(host)) return error(400, "host_not_public");

  const registry = registryOf(env);
  const existing = await registry.getRegistration(host);
  if (existing?.revoked) return error(403, "revoked");

  const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
  const limitKey = rateLimitKey(ip);
  const country = request.headers.get("CF-IPCountry") ?? "unknown";
  const now = new Date();
  const verdict = await registry.noteAttempt(host, limitKey, now.getTime(), now.toISOString());
  if (verdict !== "ok") return error(429, verdict);

  // Only the holder of the secret can register or change a host: the host publishes its hash, so
  // the public value alone proves nothing.
  const proof = await sha256Hex(secret);
  if (!(await proofMatches(host, proof))) {
    await registry.noteFailure(host, limitKey, now.getTime());
    return error(403, "proof_failed");
  }

  const expected = await dnsRecordValue(host, proof);
  const requested = signinDomains.filter(signinDomainAllowed);
  const refused = signinDomains.filter((domain) => !signinDomainAllowed(domain)).sort();
  // A domain verified earlier is checked again even when this request leaves it out: only DNS
  // takes it away. The requested domains come first within the cap of MAX_SIGNIN_DOMAINS.
  const stored = new Set(existing?.signin_domains ?? []);
  const extra = [...stored].filter((domain) => !requested.includes(domain) && signinDomainAllowed(domain)).sort();
  const checked = [...requested, ...extra].slice(0, MAX_SIGNIN_DOMAINS);
  const answers = await Promise.all(checked.map((domain) => dnsProof(domain, expected)));
  const verified: string[] = [];
  const missing: { domain: string; txt_name: string; txt_value: string }[] = [];
  checked.forEach((domain, index) => {
    // A lookup that failed (as opposed to finding no record) keeps a domain verified earlier.
    if (answers[index] === "yes" || (answers[index] === "error" && stored.has(domain))) verified.push(domain);
    else if (requested.includes(domain)) {
      missing.push({ domain, txt_name: `${DNS_NAME_PREFIX}${domain}`, txt_value: expected });
    }
  });

  const result = await registry.upsertVerified(host, now.toISOString(), limitKey, verified);
  if (result.notify) {
    ctx.waitUntil(notifyMaintainers(env, { host, ip, country, now, domains: verified, isNew: result.isNew }));
  }
  return json(200, {
    host,
    verified_at: result.verified_at,
    terms: TERMS_URL,
    signin_domains_verified: [...verified].sort(),
    signin_domains_missing: missing,
    signin_domains_refused: refused,
  });
}

function badRedirect(message: string): Response {
  return text(400, message + TERMS_SENTENCE);
}

async function handleCallback(request: Request, env: Env, grant: string): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
  const path = forwardPath(grant);
  if (path === null) return notFound();

  const url = new URL(request.url);
  const params = url.searchParams;
  const count = (name: string) => params.getAll(name).length;
  // Exactly one state and exactly one code or one error (strict parsing): a fragment-borne token
  // from a crafted response type is never carried on to a host.
  if (count("state") !== 1 || count("hd") > 1 || count("code") > 1 || count("error") > 1) {
    return badRedirect(BAD_REDIRECT);
  }
  if (count("code") + count("error") !== 1) return badRedirect(BAD_REDIRECT);
  const parsed = parseState(params.get("state")!);
  if (parsed === null || (count("code") === 1 && params.get("code") === "")) return badRedirect(BAD_REDIRECT);

  const { host } = parsed;
  const signin = isSigninGrant(grant);
  let target: URL;
  if (loopbackHost(host)) {
    // The browser performs this redirect, so the code reaches only the person's own machine.
    if (signin) return error(400, "invalid_request");
    target = new URL(`http://${host}${path}${url.search}`);
  } else {
    if (!validRelayHost(host) || !publicHost(host) || inPrivateSuffix(host)) return badRedirect(NOT_REGISTERED);
    const registration = await registryOf(env).isActive(host, Date.now(), MAX_VERIFIED_AGE_MS);
    if (registration === null) return badRedirect(NOT_REGISTERED);
    if (signin && count("code") === 1 && env.SIGNIN_REQUIRE_HD !== "false") {
      const hd = (params.get("hd") ?? "").toLowerCase();
      if (!hd || !registration.signin_domains.includes(hd)) return text(403, HD_REFUSED);
    }
    target = new URL(`https://${host}${path}${url.search}`);
  }
  if (target.host !== host) return badRedirect(BAD_REDIRECT);
  return redirect(target.href);
}

async function handleRegistrations(request: Request, env: Env): Promise<Response> {
  if (request.method !== "GET") return methodNotAllowed(["GET"]);
  if (!(await authorized(request, env))) return error(401, "unauthorized");
  const registry = registryOf(env);
  const [rows, stats] = await Promise.all([
    registry.listActive(Date.now(), MAX_VERIFIED_AGE_MS),
    registry.notifyStats(),
  ]);
  const list = rows.map((row: Registration) => ({
    host: row.host,
    registered_at: row.registered_at,
    notified: row.notified,
    signin_domains: row.signin_domains,
  }));
  // The notification counters ride in headers so the body stays the plain JSON list the contract promises.
  return json(200, list, {
    "X-Notify-Failures": String(stats.notify_failures),
    "X-Notify-Last-Error": stats.notify_last_error,
    "X-Notify-Skipped": String(stats.notify_skipped),
  });
}

async function handleAdmin(request: Request, env: Env, action: "revoke" | "restore"): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed(["POST"]);
  if (!(await authorized(request, env))) return error(401, "unauthorized");
  const raw = await readBody(request);
  let host: unknown;
  try {
    host = raw === null ? undefined : (JSON.parse(raw) as { host?: unknown }).host;
  } catch {
    host = undefined;
  }
  if (!validRelayHost(host)) return error(400, "invalid_request");
  const registry = registryOf(env);
  if (action === "revoke") {
    await registry.revoke(host);
    return json(200, { host, revoked: true });
  }
  await registry.restore(host);
  return json(200, { host, revoked: false });
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);

    if (pathname === "/health") {
      if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
      return request.method === "HEAD"
        ? reply(200, null, { "Content-Type": "text/plain; charset=utf-8" })
        : text(200, "ok");
    }

    if (pathname === "/" || pathname === "/terms") {
      if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
      return redirect(TERMS_URL);
    }

    if (pathname === "/register") return handleRegister(request, env, ctx);
    if (pathname === "/registrations") return handleRegistrations(request, env);
    if (pathname === "/admin/revoke") return handleAdmin(request, env, "revoke");
    if (pathname === "/admin/restore") return handleAdmin(request, env, "restore");

    const callback = /^\/callback\/([a-z_-]+)$/.exec(pathname);
    if (callback) return handleCallback(request, env, callback[1]!);

    return notFound();
  },
} satisfies ExportedHandler<Env>;
