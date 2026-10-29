import { createExecutionContext, env, waitOnExecutionContext } from "cloudflare:test";
import { vi } from "vitest";
import worker from "../src/index";
import type { Registry } from "../src/registry";

export const ORIGIN = "https://oauth.privacyfence.eu";
export const TOKEN = "t".repeat(43);
export const REVOKE_TOKEN = "test-revoke-token";
export const TERMS = " Terms: https://privacyfence.eu/relay-terms/";
export const NOT_REGISTERED =
  "This sign-in cannot be completed: the PrivacyFence server it belongs to is not registered with the " +
  "PrivacyFence relay. Ask your administrator." +
  TERMS;

/** Calls the Worker's fetch handler. `testEnv` overrides bindings, for the Gmail secrets. */
export async function call(path: string, init?: RequestInit, testEnv: Env = env): Promise<Response> {
  const ctx = createExecutionContext();
  const request = new Request(`${ORIGIN}${path}`, init) as Request<unknown, IncomingRequestCfProperties>;
  const response = await worker.fetch(request, testEnv, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

export function registry(): DurableObjectStub<Registry> {
  return env.REGISTRY.get(env.REGISTRY.idFromName("global"));
}

let ipCounter = 0;
/** A fresh client address, so the per-address limits of one test never touch another's. */
export function freshIp(): string {
  ipCounter += 1;
  return `10.${Math.floor(ipCounter / 250)}.${ipCounter % 250}.7`;
}

let hostCounter = 0;
export function freshHost(): string {
  hostCounter += 1;
  return `pf${hostCounter}.example.org`;
}

export function postJson(body: unknown, headers: Record<string, string> = {}): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  };
}

export const bearer = (token = REVOKE_TOKEN): Record<string, string> => ({ authorization: `Bearer ${token}` });

export type FetchHandler = (url: string, init?: RequestInit) => Response | Promise<Response>;

/** Replaces the Worker's outbound fetch. An unexpected URL fails the test loudly. */
export function stubFetch(handler: FetchHandler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    return handler(url, init as RequestInit | undefined);
  });
}

export const proofUrl = (host: string): string => `https://${host}/.well-known/privacyfence-relay`;

/** A fetch handler where `host` serves `token` at its well-known path. */
export function proofServes(host: string, token = TOKEN): FetchHandler {
  return (url) => (url === proofUrl(host) ? new Response(`${token}\n`) : new Response("unexpected", { status: 500 }));
}

export async function register(
  host: string,
  options: { token?: string; ip?: string; domains?: unknown } = {},
  testEnv: Env = env,
): Promise<Response> {
  const body: Record<string, unknown> = { host, token: options.token ?? TOKEN };
  if (options.domains !== undefined) body.signin_domains = options.domains;
  return call("/register", postJson(body, { "CF-Connecting-IP": options.ip ?? freshIp(), "CF-IPCountry": "DE" }), testEnv);
}

/** Registers `host` for real (proof stubbed), leaving the stub in place. */
export async function registerOk(host: string, domains?: unknown): Promise<void> {
  stubFetch(proofServes(host));
  const response = await register(host, domains === undefined ? {} : { domains });
  if (response.status !== 200) throw new Error(`register ${host}: ${response.status} ${await response.text()}`);
}

export function stateFor(host: string): string {
  return `${host}~${"n".repeat(43)}`;
}

export function expectSecurityHeaders(response: Response): void {
  if (response.headers.get("Cache-Control") !== "no-store") throw new Error("missing Cache-Control: no-store");
  if (response.headers.get("Referrer-Policy") !== "no-referrer") throw new Error("missing Referrer-Policy");
  if (response.headers.get("X-Content-Type-Options") !== "nosniff") throw new Error("missing nosniff");
}

export function decodeRaw(raw: string): string {
  const base64 = raw.replace(/-/g, "+").replace(/_/g, "/");
  return new TextDecoder().decode(Uint8Array.from(atob(base64), (c) => c.charCodeAt(0)));
}

/** `env` with the Gmail secrets set, for the notification tests. */
export function mailEnv(): Env {
  return {
    ...env,
    GMAIL_CLIENT_ID: "gmail-client-id",
    GMAIL_CLIENT_SECRET: "gmail-client-secret",
    GMAIL_REFRESH_TOKEN: "gmail-refresh-token",
  };
}
