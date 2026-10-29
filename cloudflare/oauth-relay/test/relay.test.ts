/**
 * The relay contract (README.md) against the Worker's own
 * fetch handler in the workers runtime. Storage is isolated per test FILE, not per test, so every
 * test uses fresh hosts and client addresses and never assumes an empty registry.
 */
import { env } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  NOT_REGISTERED,
  REVOKE_TOKEN,
  TERMS,
  TOKEN,
  bearer,
  call,
  expectSecurityHeaders,
  freshHost,
  freshIp,
  postJson,
  proofServes,
  proofUrl,
  register,
  registerOk,
  registry,
  stateFor,
  stubFetch,
} from "./helpers";

afterEach(() => {
  vi.restoreAllMocks();
});

async function jsonOf(response: Response): Promise<Record<string, unknown>> {
  return (await response.json()) as Record<string, unknown>;
}

describe("GET and HEAD /health", () => {
  it("answers ok on GET", async () => {
    const response = await call("/health");
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("ok");
    expectSecurityHeaders(response);
  });

  it("answers HEAD without a body", async () => {
    const response = await call("/health", { method: "HEAD" });
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("");
    expectSecurityHeaders(response);
  });

  it("refuses other methods", async () => {
    const response = await call("/health", { method: "POST" });
    expect(response.status).toBe(405);
    expect(response.headers.get("Allow")).toBe("GET, HEAD");
  });
});

describe("terms links", () => {
  it.each(["/", "/terms"])("%s redirects to the relay terms on GET and HEAD", async (path) => {
    for (const method of ["GET", "HEAD"]) {
      const response = await call(path, { method });
      expect(response.status).toBe(302);
      expect(response.headers.get("Location")).toBe("https://privacyfence.eu/relay-terms/");
      expectSecurityHeaders(response);
    }
    expect((await call(path, { method: "POST" })).status).toBe(405);
  });
});

describe("unknown routes", () => {
  it.each(["/nope", "/callback", "/callback/", "/callback/nope", "/callback/slack/x", "/callback/Slack", "/admin"])(
    "%s is 404",
    async (path) => {
      const response = await call(path);
      expect(response.status).toBe(404);
      expectSecurityHeaders(response);
    },
  );
});

describe("POST /register", () => {
  it("registers a host that serves its token", async () => {
    const host = freshHost();
    const spy = stubFetch(proofServes(host));
    const response = await register(host);
    expect(response.status).toBe(200);
    expectSecurityHeaders(response);
    const body = await jsonOf(response);
    expect(body.host).toBe(host);
    expect(typeof body.verified_at).toBe("string");
    expect(body.terms).toBe("https://privacyfence.eu/relay-terms/");
    expect(body.signin_domains_verified).toEqual([]);
    expect(body.signin_domains_missing).toEqual([]);
    const call0 = spy.mock.calls[0]!;
    expect(call0[0]).toBe(proofUrl(host));
    expect((call0[1] as RequestInit).redirect).toBe("manual");
    const row = await registry().getRegistration(host);
    expect(row?.verified_at).toBe(body.verified_at);
    expect(row?.registered_at).toBe(body.verified_at);
    expect(row?.revoked).toBe(false);
  });

  it("accepts a host with a port and fetches the proof from that port", async () => {
    const host = `${freshHost()}:8443`;
    stubFetch(proofServes(host));
    expect((await register(host)).status).toBe(200);
  });

  it("keeps registered_at and refreshes verified_at on a re-registration", async () => {
    const host = freshHost();
    await registerOk(host);
    const first = await registry().getRegistration(host);
    await registry().upsertVerified(host, "2030-01-01T00:00:00.000Z", "", []);
    const again = await registry().getRegistration(host);
    expect(again?.registered_at).toBe(first?.registered_at);
    expect(again?.verified_at).toBe("2030-01-01T00:00:00.000Z");
  });

  it.each([
    ["a..b"],
    ["-x.com"],
    ["x.com:0"],
    ["x.com:99999"],
    ["foo.localhost."],
    ["127.1"],
    ["2130706433"],
    ["0x7f000001"],
    ["UPPER.example.org"],
    ["x"],
    ["user@pf.example.org"],
    ["pf.example.org:443"],
    [5],
    [null],
  ])("rejects the host %j as invalid_request", async (host) => {
    const spy = stubFetch(() => new Response("never", { status: 500 }));
    const response = await call("/register", postJson({ host, token: TOKEN }, { "CF-Connecting-IP": freshIp() }));
    expect(response.status).toBe(400);
    expect(await jsonOf(response)).toEqual({ error: "invalid_request" });
    expect(spy).not.toHaveBeenCalled();
  });

  it.each([
    ["localhost"],
    ["127.0.0.1"],
    ["localhost:8765"],
    ["127.0.0.1:8765"],
    ["foo.localhost"],
    ["a.local"],
    ["a.internal"],
    ["a.home.arpa"],
    ["a.lan"],
    ["x.github.io"],
    ["a.pages.dev"],
    ["a.workers.dev"],
    ["a.netlify.app"],
  ])("refuses %s as host_not_public without calling anyone", async (host) => {
    const spy = stubFetch(() => new Response("never", { status: 500 }));
    const response = await call("/register", postJson({ host, token: TOKEN }, { "CF-Connecting-IP": freshIp() }));
    expect(response.status).toBe(400);
    expect(await jsonOf(response)).toEqual({ error: "host_not_public" });
    expect(spy).not.toHaveBeenCalled();
    expect(await registry().getRegistration(host)).toBeNull();
  });

  it.each([
    ["not json", "{"],
    ["an array", "[]"],
    ["a string", '"x"'],
    ["null", "null"],
    ["no token", JSON.stringify({ host: "pf.example.org" })],
    ["a short token", JSON.stringify({ host: "pf.example.org", token: "short" })],
    ["a token with a bad character", JSON.stringify({ host: "pf.example.org", token: "+".repeat(43) })],
    ["an oversized body", JSON.stringify({ host: "pf.example.org", token: TOKEN, pad: "x".repeat(5000) })],
    ["signin_domains that is no array", JSON.stringify({ host: "pf.example.org", token: TOKEN, signin_domains: "acme.com" })],
    [
      "more than 20 sign-in domains",
      JSON.stringify({
        host: "pf.example.org",
        token: TOKEN,
        signin_domains: Array.from({ length: 21 }, (_, i) => `d${i}.example.org`),
      }),
    ],
    ["a public-suffix sign-in domain", JSON.stringify({ host: "pf.example.org", token: TOKEN, signin_domains: ["co.uk"] })],
    ["a free-mail sign-in domain", JSON.stringify({ host: "pf.example.org", token: TOKEN, signin_domains: ["gmail.com"] })],
    ["a malformed sign-in domain", JSON.stringify({ host: "pf.example.org", token: TOKEN, signin_domains: ["a..b"] })],
  ])("rejects %s as invalid_request", async (_name, body) => {
    const response = await call("/register", postJson(body, { "CF-Connecting-IP": freshIp() }));
    expect(response.status).toBe(400);
    expect(await jsonOf(response)).toEqual({ error: "invalid_request" });
  });

  it("rejects a declared content length over the limit", async () => {
    const response = await call("/register", postJson("{}", { "Content-Length": "999999" }));
    expect(response.status).toBe(400);
  });

  it("answers 405 to GET", async () => {
    expect((await call("/register")).status).toBe(405);
  });

  describe("the proof", () => {
    it("refuses a token mismatch", async () => {
      const host = freshHost();
      stubFetch(proofServes(host, "x".repeat(43)));
      const response = await register(host);
      expect(response.status).toBe(403);
      expect(await jsonOf(response)).toEqual({ error: "proof_failed" });
      expectSecurityHeaders(response);
    });

    it.each([
      ["a 404", () => new Response("no", { status: 404 })],
      ["a 302", () => new Response(null, { status: 302, headers: { Location: "https://elsewhere.example/" } })],
      ["a 500", () => new Response("no", { status: 500 })],
      ["a body over 256 bytes", () => new Response(TOKEN + " ".repeat(300))],
      [
        "a body that errors",
        () =>
          new Response(
            new ReadableStream({
              pull(controller) {
                controller.error(new Error("boom"));
              },
            }),
          ),
      ],
    ])("refuses %s", async (_name, make) => {
      const host = freshHost();
      stubFetch(() => make());
      const response = await register(host);
      expect(response.status).toBe(403);
      expect(await jsonOf(response)).toEqual({ error: "proof_failed" });
    });

    it("refuses a fetch that throws, and a timeout", async () => {
      stubFetch(() => {
        throw new TypeError("network down");
      });
      expect((await register(freshHost())).status).toBe(403);
      stubFetch(() => {
        throw new DOMException("timed out", "TimeoutError");
      });
      expect((await register(freshHost())).status).toBe(403);
    });

    it("accepts an empty-bodied success only when the token matches", async () => {
      const host = freshHost();
      stubFetch(() => new Response(null, { status: 200 }));
      expect((await register(host)).status).toBe(403);
    });

    it("creates no registration row for a failed proof", async () => {
      const host = freshHost();
      stubFetch(() => new Response("no", { status: 404 }));
      await register(host);
      expect(await registry().getRegistration(host)).toBeNull();
      expect(await registry().listActive()).not.toContainEqual(expect.objectContaining({ host }));
    });
  });

  describe("revocation", () => {
    it("refuses a revoked host before any proof fetch", async () => {
      const host = freshHost();
      await registry().revoke(host);
      const spy = stubFetch(proofServes(host));
      const response = await register(host);
      expect(response.status).toBe(403);
      expect(await jsonOf(response)).toEqual({ error: "revoked" });
      expect(spy).not.toHaveBeenCalled();
    });
  });

  describe("rate limits", () => {
    it("throttles a repeated failure from the same address, never a valid proof from another", async () => {
      const host = freshHost();
      const attacker = freshIp();
      const owner = freshIp();
      const spy = stubFetch(() => new Response("no", { status: 404 }));
      expect((await register(host, { ip: attacker })).status).toBe(403);
      const again = await register(host, { ip: attacker });
      expect(again.status).toBe(429);
      expect(await jsonOf(again)).toEqual({ error: "too_soon" });
      expect(spy).toHaveBeenCalledTimes(1);

      stubFetch(proofServes(host));
      const ok = await register(host, { ip: owner });
      expect(ok.status).toBe(200);
    });

    it("lets the same address try again once the 60 seconds have passed", async () => {
      const host = freshHost();
      const ip = freshIp();
      const old = Date.now() - 61_000;
      await registry().noteFailure(host, ip, old);
      stubFetch(proofServes(host));
      expect((await register(host, { ip })).status).toBe(200);
    });

    it("a failure for one host does not throttle the same address for another host", async () => {
      const ip = freshIp();
      stubFetch(() => new Response("no", { status: 404 }));
      expect((await register(freshHost(), { ip })).status).toBe(403);
      const other = freshHost();
      stubFetch(proofServes(other));
      expect((await register(other, { ip })).status).toBe(200);
    });

    it("limits one address to 30 requests in 10 minutes", async () => {
      const ip = freshIp();
      stubFetch(() => new Response("no", { status: 404 }));
      for (let i = 0; i < 30; i += 1) {
        expect((await register(freshHost(), { ip })).status).toBe(403);
      }
      const over = await register(freshHost(), { ip });
      expect(over.status).toBe(429);
      expect(await jsonOf(over)).toEqual({ error: "too_soon" });
      // Another address is unaffected.
      expect((await register(freshHost())).status).toBe(403);
    });

    it("allows 5 new hosts per address a day and refuses the sixth without a row", async () => {
      const ip = freshIp();
      for (let i = 0; i < 5; i += 1) {
        const host = freshHost();
        stubFetch(proofServes(host));
        expect((await register(host, { ip })).status).toBe(200);
      }
      const sixth = freshHost();
      const spy = stubFetch(proofServes(sixth));
      spy.mockClear();
      const refused = await register(sixth, { ip });
      expect(refused.status).toBe(429);
      expect(await jsonOf(refused)).toEqual({ error: "too_many_new_hosts" });
      expect(spy).not.toHaveBeenCalled();
      expect(await registry().getRegistration(sixth)).toBeNull();

      // Another address is unaffected.
      stubFetch(proofServes(sixth));
      expect((await register(sixth, { ip: freshIp() })).status).toBe(200);
    });

    it("does not count a re-verification of a known host", async () => {
      const ip = freshIp();
      const known = freshHost();
      stubFetch(proofServes(known));
      expect((await register(known, { ip })).status).toBe(200);
      for (let i = 0; i < 4; i += 1) {
        const host = freshHost();
        stubFetch(proofServes(host));
        expect((await register(host, { ip })).status).toBe(200);
      }
      stubFetch(proofServes(known));
      expect((await register(known, { ip })).status).toBe(200);
      expect((await register(known, { ip })).status).toBe(200);
    });

    it("prunes attempts older than an hour on the next write", async () => {
      const ip = freshIp();
      const host = freshHost();
      await registry().noteFailure(host, ip, Date.now() - 2 * 3600_000);
      const before = await registry().attemptRowCount();
      await registry().noteFailure(freshHost(), ip, Date.now());
      expect(await registry().attemptRowCount()).toBeLessThan(before + 1);
    });
  });
});

describe("GET /callback/:grant", () => {
  const query = (state: string, extra = "code=abc") => `state=${encodeURIComponent(state)}&${extra}`;

  it("forwards the exact original query to the registered host", async () => {
    const host = freshHost();
    await registerOk(host);
    const search = `?state=${encodeURIComponent(stateFor(host))}&code=4%2F0AX%20y&scope=a%20b&authuser=0&z=%C3%A9`;
    const response = await call(`/callback/gmail${search}`);
    expect(response.status).toBe(302);
    expect(response.headers.get("Location")).toBe(`https://${host}/oauth/callback/gmail${search}`);
    expectSecurityHeaders(response);
    expect(await response.text()).toBe("");
  });

  it.each(["gmail", "drive", "calendar", "contacts", "tasks", "apps_script", "slack", "salesforce", "atlassian", "miro"])(
    "forwards the %s grant to /oauth/callback/%s",
    async (grant) => {
      const host = freshHost();
      await registerOk(host);
      const response = await call(`/callback/${grant}?${query(stateFor(host))}`);
      expect(response.status).toBe(302);
      expect(new URL(response.headers.get("Location")!).pathname).toBe(`/oauth/callback/${grant}`);
    },
  );

  it("forwards to a host on a non-default port", async () => {
    const host = `${freshHost()}:8443`;
    await registerOk(host);
    const response = await call(`/callback/slack?${query(stateFor(host))}`);
    expect(response.headers.get("Location")!.startsWith(`https://${host}/oauth/callback/slack?`)).toBe(true);
  });

  it("forwards an error as well as a code", async () => {
    const host = freshHost();
    await registerOk(host);
    const response = await call(`/callback/slack?${query(stateFor(host), "error=access_denied")}`);
    expect(response.status).toBe(302);
  });

  it("answers HEAD like GET and refuses POST", async () => {
    const host = freshHost();
    await registerOk(host);
    expect((await call(`/callback/slack?${query(stateFor(host))}`, { method: "HEAD" })).status).toBe(302);
    expect((await call(`/callback/slack?${query(stateFor(host))}`, { method: "POST" })).status).toBe(405);
  });

  it("answers an unknown grant with 404", async () => {
    const host = freshHost();
    await registerOk(host);
    expect((await call(`/callback/unknown?${query(stateFor(host))}`)).status).toBe(404);
    expect((await call(`/callback/slack/x?${query(stateFor(host))}`)).status).toBe(404);
    expect((await call(`/callback/constructor?${query(stateFor(host))}`)).status).toBe(404);
  });

  it("answers an unregistered host with the exact text", async () => {
    const response = await call(`/callback/slack?${query(stateFor(freshHost()))}`);
    expect(response.status).toBe(400);
    expect(response.headers.get("Content-Type")).toBe("text/plain; charset=utf-8");
    expect(response.headers.get("Location")).toBeNull();
    expect(await response.text()).toBe(NOT_REGISTERED);
    expectSecurityHeaders(response);
  });

  it("answers a revoked host with the same text", async () => {
    const host = freshHost();
    await registerOk(host);
    await registry().revoke(host);
    const response = await call(`/callback/slack?${query(stateFor(host))}`);
    expect(response.status).toBe(400);
    expect(await response.text()).toBe(NOT_REGISTERED);
  });

  it("answers a host last verified 36 days ago with the same text, and one 34 days ago with a redirect", async () => {
    const stale = freshHost();
    const fresh = freshHost();
    const day = 24 * 3600 * 1000;
    await registry().upsertVerified(stale, new Date(Date.now() - 36 * day).toISOString(), "", []);
    await registry().upsertVerified(fresh, new Date(Date.now() - 34 * day).toISOString(), "", []);
    const refused = await call(`/callback/slack?${query(stateFor(stale))}`);
    expect(refused.status).toBe(400);
    expect(await refused.text()).toBe(NOT_REGISTERED);
    expect((await call(`/callback/slack?${query(stateFor(fresh))}`)).status).toBe(302);
  });

  it("answers an unparseable verified_at as not registered", async () => {
    const host = freshHost();
    await registry().upsertVerified(host, "not a date", "", []);
    expect((await call(`/callback/slack?${query(stateFor(host))}`)).status).toBe(400);
  });

  it("refuses a state whose host part is invalid", async () => {
    for (const bad of ["UPPER.example.org", "a..b", "x", "pf.example.org:443", "user@pf.example.org", "foo.local"]) {
      const response = await call(`/callback/slack?${query(`${bad}~${"n".repeat(43)}`)}`);
      expect(response.status).toBe(400);
      expect(response.headers.get("Location")).toBeNull();
    }
  });

  it("refuses a host under a private suffix", async () => {
    const response = await call(`/callback/slack?${query(`x.github.io~${"n".repeat(43)}`)}`);
    expect(response.status).toBe(400);
    expect(await response.text()).toBe(NOT_REGISTERED);
  });

  describe("strict parameters", () => {
    const state = encodeURIComponent(stateFor("pf.example.org"));
    it.each([
      ["no query", ""],
      ["only a state", `?state=${state}`],
      ["state and an access_token", `?state=${state}&access_token=x`],
      ["a code and an error together", `?state=${state}&code=a&error=b`],
      ["no state", "?code=a"],
      ["a duplicate state", `?state=${state}&state=${state}&code=a`],
      ["a duplicate code", `?state=${state}&code=a&code=b`],
      ["a duplicate error", `?state=${state}&error=a&error=b`],
      ["an empty code", `?state=${state}&code=`],
      ["a state with no nonce", `?state=${encodeURIComponent("pf.example.org~")}&code=a`],
      ["a state with no tilde", `?state=pf.example.org&code=a`],
    ])("answers %s with 400 and no redirect", async (_name, search) => {
      const response = await call(`/callback/slack${search}`);
      expect(response.status).toBe(400);
      expect(response.headers.get("Location")).toBeNull();
      expect((await response.text()).endsWith(TERMS)).toBe(true);
    });
  });

  describe("on localhost", () => {
    it.each(["localhost", "localhost:8765", "127.0.0.1", "127.0.0.1:8765"])(
      "forwards %s to http without consulting the registry",
      async (host) => {
        const search = `?state=${encodeURIComponent(stateFor(host))}&code=abc%2Fdef`;
        const response = await call(`/callback/gmail${search}`);
        expect(response.status).toBe(302);
        expect(response.headers.get("Location")).toBe(`http://${host}/oauth/callback/gmail${search}`);
        expect(await registry().getRegistration(host)).toBeNull();
      },
    );

    it.each([
      "localhost.evil.com",
      "localhost:80@evil.com",
      "127.0.0.1.nip.io",
      "LOCALHOST",
      "[::1]",
      "localhost:0",
      "localhost:099",
      "localhost:65536",
      "localhost%2F@evil.com",
      "localhost:80",
    ])("never forwards %s", async (host) => {
      const response = await call(`/callback/slack?${query(`${host}~${"n".repeat(43)}`)}`);
      expect(response.status).toBe(400);
      expect(response.headers.get("Location")).toBeNull();
    });

    it("still refuses to register a loopback host", async () => {
      const response = await register("localhost:8765");
      expect(response.status).toBe(400);
      expect(await jsonOf(response)).toEqual({ error: "host_not_public" });
    });
  });
});

describe("sign-in grants", () => {
  const DOMAIN = "acme-corp.example";
  const hash = async (token: string) => {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  };
  const dnsAnswer = (value: string) =>
    new Response(JSON.stringify({ Status: 0, Answer: [{ name: "x", type: 16, data: `"${value}"` }] }));

  async function registerWithDomain(host: string): Promise<void> {
    const expected = `pf-relay=${await hash(TOKEN)}`;
    stubFetch((url) => (url === proofUrl(host) ? new Response(TOKEN) : dnsAnswer(expected)));
    const response = await register(host, { domains: [DOMAIN] });
    expect(response.status).toBe(200);
    expect((await jsonOf(response)).signin_domains_verified).toEqual([DOMAIN]);
  }

  const qs = (host: string, extra: string) => `?state=${encodeURIComponent(stateFor(host))}&${extra}`;

  it.each([
    ["idp", "/oauth/idp/callback"],
    ["idp-login", "/oauth/idp/login-callback"],
    ["stepup", "/oauth/stepup/callback"],
  ])("forwards %s to %s when hd is a verified domain", async (grant, path) => {
    const host = freshHost();
    await registerWithDomain(host);
    const search = qs(host, `code=abc&hd=${DOMAIN}`);
    const response = await call(`/callback/${grant}${search}`);
    expect(response.status).toBe(302);
    expect(response.headers.get("Location")).toBe(`https://${host}${path}${search}`);
  });

  it("refuses a sign-in without hd", async () => {
    const host = freshHost();
    await registerWithDomain(host);
    const response = await call(`/callback/idp${qs(host, "code=abc")}`);
    expect(response.status).toBe(403);
    expect(await response.text()).toBe(
      "This Google account's organization is not allowed to sign in to this PrivacyFence server.",
    );
    expect(response.headers.get("Location")).toBeNull();
    expectSecurityHeaders(response);
  });

  it("refuses a foreign hd and a repeated hd", async () => {
    const host = freshHost();
    await registerWithDomain(host);
    expect((await call(`/callback/idp${qs(host, "code=abc&hd=other.example")}`)).status).toBe(403);
    expect((await call(`/callback/idp${qs(host, `code=abc&hd=${DOMAIN}&hd=${DOMAIN}`)}`)).status).toBe(400);
  });

  it("compares hd case-insensitively", async () => {
    const host = freshHost();
    await registerWithDomain(host);
    expect((await call(`/callback/idp${qs(host, `code=abc&hd=${DOMAIN.toUpperCase()}`)}`)).status).toBe(302);
  });

  it("refuses a host with no verified domain", async () => {
    const host = freshHost();
    await registerOk(host);
    expect((await call(`/callback/idp${qs(host, `code=abc&hd=${DOMAIN}`)}`)).status).toBe(403);
  });

  it("forwards an error without the hd check", async () => {
    const host = freshHost();
    await registerWithDomain(host);
    expect((await call(`/callback/idp${qs(host, "error=access_denied")}`)).status).toBe(302);
  });

  it("does not check hd on a connector grant", async () => {
    const host = freshHost();
    await registerWithDomain(host);
    expect((await call(`/callback/gmail${qs(host, "code=abc")}`)).status).toBe(302);
  });

  it("skips the hd check when SIGNIN_REQUIRE_HD is false", async () => {
    const host = freshHost();
    await registerWithDomain(host);
    const off = { ...env, SIGNIN_REQUIRE_HD: "false" };
    expect((await call(`/callback/idp${qs(host, "code=abc")}`, undefined, off)).status).toBe(302);
    const on = { ...env, SIGNIN_REQUIRE_HD: "true" };
    expect((await call(`/callback/idp${qs(host, "code=abc")}`, undefined, on)).status).toBe(403);
  });

  it("refuses an unregistered host before the hd check", async () => {
    const response = await call(`/callback/idp${qs(freshHost(), `code=abc&hd=${DOMAIN}`)}`);
    expect(response.status).toBe(400);
    expect(await response.text()).toBe(NOT_REGISTERED);
  });

  it.each(["idp", "idp-login", "stepup"])("refuses a loopback host for %s", async (grant) => {
    const response = await call(`/callback/${grant}${qs("localhost:8765", "code=abc&hd=x.example")}`);
    expect(response.status).toBe(400);
    expect(await jsonOf(response)).toEqual({ error: "invalid_request" });
    expect(response.headers.get("Location")).toBeNull();
  });

  describe("domain proof at /register", () => {
    it("reports a missing record with the TXT name and value to publish", async () => {
      const host = freshHost();
      stubFetch((url) =>
        url === proofUrl(host) ? new Response(TOKEN) : new Response(JSON.stringify({ Status: 3 })),
      );
      const response = await register(host, { domains: [DOMAIN] });
      expect(response.status).toBe(200);
      const body = await jsonOf(response);
      expect(body.signin_domains_verified).toEqual([]);
      expect(body.signin_domains_missing).toEqual([
        { domain: DOMAIN, txt_name: `_privacyfence-relay.${DOMAIN}`, txt_value: `pf-relay=${await hash(TOKEN)}` },
      ]);
      expect((await registry().getRegistration(host))?.signin_domains).toEqual([]);
    });

    it("treats a wrong TXT value as missing", async () => {
      const host = freshHost();
      stubFetch((url) => (url === proofUrl(host) ? new Response(TOKEN) : dnsAnswer("pf-relay=deadbeef")));
      const body = await jsonOf(await register(host, { domains: [DOMAIN] }));
      expect(body.signin_domains_verified).toEqual([]);
      expect(body.signin_domains_missing).toHaveLength(1);
    });

    it("reads a TXT value split into several strings and ignores other record types", async () => {
      const host = freshHost();
      const expected = `pf-relay=${await hash(TOKEN)}`;
      const half = Math.floor(expected.length / 2);
      stubFetch((url) =>
        url === proofUrl(host)
          ? new Response(TOKEN)
          : new Response(
              JSON.stringify({
                Status: 0,
                Answer: [
                  { type: 5, data: "cname.example." },
                  { type: 16 },
                  { type: 16, data: `"${expected.slice(0, half)}" "${expected.slice(half)}"` },
                ],
              }),
            ),
      );
      expect((await jsonOf(await register(host, { domains: [DOMAIN] }))).signin_domains_verified).toEqual([DOMAIN]);
    });

    it("reads an unquoted TXT value and an empty answer", async () => {
      const host = freshHost();
      const expected = `pf-relay=${await hash(TOKEN)}`;
      stubFetch((url) =>
        url === proofUrl(host)
          ? new Response(TOKEN)
          : new Response(JSON.stringify({ Status: 0, Answer: [{ type: 16, data: expected }] })),
      );
      expect((await jsonOf(await register(host, { domains: [DOMAIN] }))).signin_domains_verified).toEqual([DOMAIN]);
      const other = freshHost();
      stubFetch((url) => (url === proofUrl(other) ? new Response(TOKEN) : new Response(JSON.stringify({ Status: 0 }))));
      expect((await jsonOf(await register(other, { domains: [DOMAIN] }))).signin_domains_missing).toHaveLength(1);
    });

    it("queries DNS over HTTPS for the _privacyfence-relay name", async () => {
      const host = freshHost();
      const spy = stubFetch((url) =>
        url === proofUrl(host) ? new Response(TOKEN) : new Response(JSON.stringify({ Status: 3 })),
      );
      await register(host, { domains: [DOMAIN, DOMAIN] });
      const dns = spy.mock.calls.filter(([url]) => String(url).startsWith("https://cloudflare-dns.com/"));
      expect(dns).toHaveLength(1);
      expect(String(dns[0]![0])).toBe(
        `https://cloudflare-dns.com/dns-query?name=_privacyfence-relay.${DOMAIN}&type=TXT`,
      );
      expect(((dns[0]![1] as RequestInit).headers as Record<string, string>).Accept).toBe("application/dns-json");
    });

    it("keeps a domain verified earlier when the DNS lookup itself fails, and drops it when the record is gone", async () => {
      const host = freshHost();
      await registerWithDomain(host);
      stubFetch((url) => {
        if (url === proofUrl(host)) return new Response(TOKEN);
        throw new TypeError("dns down");
      });
      expect((await jsonOf(await register(host, { domains: [DOMAIN] }))).signin_domains_verified).toEqual([DOMAIN]);
      stubFetch((url) => (url === proofUrl(host) ? new Response(TOKEN) : new Response("bad", { status: 502 })));
      expect((await jsonOf(await register(host, { domains: [DOMAIN] }))).signin_domains_verified).toEqual([DOMAIN]);
      stubFetch((url) => (url === proofUrl(host) ? new Response(TOKEN) : new Response("{}", { status: 200 })));
      expect((await jsonOf(await register(host, { domains: [DOMAIN] }))).signin_domains_verified).toEqual([DOMAIN]);
      stubFetch((url) => (url === proofUrl(host) ? new Response(TOKEN) : new Response(JSON.stringify({ Status: 3 }))));
      const dropped = await jsonOf(await register(host, { domains: [DOMAIN] }));
      expect(dropped.signin_domains_verified).toEqual([]);
      expect((await registry().getRegistration(host))?.signin_domains).toEqual([]);
    });

    it("does not keep a never-verified domain when DNS fails", async () => {
      const host = freshHost();
      stubFetch((url) => {
        if (url === proofUrl(host)) return new Response(TOKEN);
        throw new TypeError("dns down");
      });
      const body = await jsonOf(await register(host, { domains: [DOMAIN] }));
      expect(body.signin_domains_verified).toEqual([]);
      expect(body.signin_domains_missing).toHaveLength(1);
    });
  });
});

describe("GET /registrations", () => {
  it("needs the maintainer token", async () => {
    for (const headers of [{}, bearer("wrong"), { authorization: "Basic abc" }, { authorization: REVOKE_TOKEN }]) {
      const response = await call("/registrations", { headers });
      expect(response.status).toBe(401);
      expect(await jsonOf(response)).toEqual({ error: "unauthorized" });
      expectSecurityHeaders(response);
    }
  });

  it("is 401 when the Worker has no REVOKE_TOKEN configured", async () => {
    const response = await call("/registrations", { headers: bearer("") }, { ...env, REVOKE_TOKEN: "" });
    expect(response.status).toBe(401);
    const empty = await call("/registrations", { headers: { authorization: "Bearer x" } }, { ...env, REVOKE_TOKEN: "" });
    expect(empty.status).toBe(401);
  });

  it("lists non-revoked, verified hosts, sorted, with the notification state", async () => {
    const [b, a, revoked, restored] = [freshHost(), freshHost(), freshHost(), freshHost()];
    await registerOk(b);
    await registerOk(a);
    await registerOk(revoked);
    await registerOk(restored);
    await registry().revoke(revoked);
    await registry().restore(restored);
    const response = await call("/registrations", { headers: bearer() });
    expect(response.status).toBe(200);
    expect(response.headers.get("X-Notify-Skipped")).not.toBeNull();
    expect(response.headers.get("X-Notify-Failures")).not.toBeNull();
    expect(response.headers.get("X-Notify-Last-Error")).not.toBeNull();
    const list = (await response.json()) as { host: string; registered_at: string; notified: boolean }[];
    const hosts = list.map((row) => row.host);
    expect(hosts).toEqual([...hosts].sort());
    expect(hosts).toContain(a);
    expect(hosts).toContain(b);
    expect(hosts).not.toContain(revoked);
    expect(hosts).not.toContain(restored);
    const row = list.find((entry) => entry.host === a)!;
    expect(row.notified).toBe(false);
    expect(typeof row.registered_at).toBe("string");
  });

  it("answers 405 to POST", async () => {
    expect((await call("/registrations", { method: "POST" })).status).toBe(405);
  });
});

describe("POST /admin/revoke and /admin/restore", () => {
  it.each(["/admin/revoke", "/admin/restore"])("%s needs the maintainer token", async (path) => {
    const host = freshHost();
    for (const headers of [{}, bearer("wrong"), bearer("test-revoke-toke"), bearer(`${REVOKE_TOKEN}x`)]) {
      const response = await call(path, postJson({ host }, headers));
      expect(response.status).toBe(401);
      expect(await jsonOf(response)).toEqual({ error: "unauthorized" });
    }
    expect(await registry().getRegistration(host)).toBeNull();
  });

  it.each(["/admin/revoke", "/admin/restore"])("%s refuses a bad body and other methods", async (path) => {
    for (const body of ["{", JSON.stringify({}), JSON.stringify({ host: "a..b" }), JSON.stringify({ host: 5 })]) {
      const response = await call(path, postJson(body, bearer()));
      expect(response.status).toBe(400);
      expect(await jsonOf(response)).toEqual({ error: "invalid_request" });
    }
    const tooBig = await call(path, postJson("{}", { ...bearer(), "Content-Length": "999999" }));
    expect(tooBig.status).toBe(400);
    expect((await call(path, { headers: bearer() })).status).toBe(405);
  });

  it("revokes a registered host, which then cannot forward or register", async () => {
    const host = freshHost();
    await registerOk(host);
    const response = await call("/admin/revoke", postJson({ host }, bearer()));
    expect(response.status).toBe(200);
    expect(await jsonOf(response)).toEqual({ host, revoked: true });
    expectSecurityHeaders(response);
    const forward = await call(`/callback/slack?state=${encodeURIComponent(stateFor(host))}&code=x`);
    expect(forward.status).toBe(400);
    expect((await register(host)).status).toBe(403);
  });

  it("revokes an unknown host by inserting a revoked row", async () => {
    const host = freshHost();
    expect((await call("/admin/revoke", postJson({ host }, bearer()))).status).toBe(200);
    expect((await registry().getRegistration(host))?.revoked).toBe(true);
    // Revoking twice is fine.
    expect((await call("/admin/revoke", postJson({ host }, bearer()))).status).toBe(200);
  });

  it("restores a host, which must prove control again before forwarding resumes", async () => {
    const host = freshHost();
    await registerOk(host);
    await call("/admin/revoke", postJson({ host }, bearer()));
    const response = await call("/admin/restore", postJson({ host }, bearer()));
    expect(response.status).toBe(200);
    expect(await jsonOf(response)).toEqual({ host, revoked: false });
    const search = `?state=${encodeURIComponent(stateFor(host))}&code=x`;
    expect((await call(`/callback/slack${search}`)).status).toBe(400);
    stubFetch(proofServes(host));
    expect((await register(host)).status).toBe(200);
    expect((await call(`/callback/slack${search}`)).status).toBe(302);
  });

  it("restores a host the registry has never seen without creating a row", async () => {
    const host = freshHost();
    expect((await call("/admin/restore", postJson({ host }, bearer()))).status).toBe(200);
    expect(await registry().getRegistration(host)).toBeNull();
  });
});

describe("headers", () => {
  it("every response carries the three security headers", async () => {
    const host = freshHost();
    await registerOk(host);
    const responses = await Promise.all([
      call("/health"),
      call("/health", { method: "HEAD" }),
      call("/"),
      call("/nope"),
      call("/register"),
      call("/register", postJson("{")),
      call("/registrations"),
      call("/admin/revoke", postJson({ host })),
      call(`/callback/slack?state=${encodeURIComponent(stateFor(host))}&code=x`),
      call("/callback/slack"),
    ]);
    for (const response of responses) expectSecurityHeaders(response);
  });
});
