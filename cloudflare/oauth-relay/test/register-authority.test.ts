/**
 * Who may change a registration, and what a sign-in domain record vouches for: only the holder of a
 * host's secret can register it, a DNS record proves a domain for one host only, a stored domain
 * leaves only when DNS says so, and requests from browsers or one IPv6 /64 cannot get around that.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { dnsRecordValue } from "../src/validate";
import {
  PROOF,
  SECRET,
  call,
  freshHost,
  freshIp,
  postJson,
  proofUrl,
  register,
  registry,
  sha256Hex,
  stateFor,
  stubFetch,
  txtValue,
} from "./helpers";

afterEach(() => {
  vi.restoreAllMocks();
});

async function jsonOf(response: Response): Promise<Record<string, unknown>> {
  return (await response.json()) as Record<string, unknown>;
}

const DAY_MS = 24 * 3600 * 1000;
const ACME = "acme-signin.example";
const OTHER_SECRET = "o".repeat(43);

/**
 * Two servers: `acme` (secret SECRET) has published the record for ACME, and `evil` (its own secret)
 * serves whatever it likes. DNS answers ACME's record for every lookup of ACME.
 */
async function world(acme: string, evil: string, evilServes: string) {
  const acmeRecord = await txtValue(acme);
  stubFetch((url) => {
    if (url === proofUrl(acme)) return new Response(PROOF);
    if (url === proofUrl(evil)) return new Response(evilServes);
    if (url.startsWith("https://cloudflare-dns.com/") && url.includes(`_privacyfence-relay.${ACME}&`)) {
      return new Response(JSON.stringify({ Status: 0, Answer: [{ type: 16, data: `"${acmeRecord}"` }] }));
    }
    return new Response(JSON.stringify({ Status: 3 }));
  });
}

describe("the sign-in domain record is bound to one host", () => {
  // The same vectors are in tests/unit/test_relay_client.py: the server and the relay must agree.
  it.each([
    ["pf.acme.example.com", "pf-relay=v1:084fdbae3551700b442490b19ce08f8e00fa3c9844c1ade07d8155790a0112d0"],
    ["pf.acme.example.com:8443", "pf-relay=v1:e36de5e96920cee82fc23148240b3970ac70511442062d99d3c98b8d03864119"],
  ])("matches the server's vector for %s", async (host, value) => {
    expect(SECRET).toBe("t".repeat(43));
    expect(await dnsRecordValue(host, PROOF)).toBe(value);
    expect(await txtValue(host)).toBe(value);
  });

  it("is pf-relay=v1: and the SHA-256 of v1|host|proof", async () => {
    const host = freshHost();
    await world(host, freshHost(), PROOF);
    const body = await jsonOf(await register(host, { domains: ["nobody-published.example"] }));
    const expected = `pf-relay=v1:${await sha256Hex(`v1|${host}|${PROOF}`)}`;
    expect(body.signin_domains_missing).toEqual([
      { domain: "nobody-published.example", txt_name: "_privacyfence-relay.nobody-published.example", txt_value: expected },
    ]);
  });

  it("includes the port of a host that has one", async () => {
    const host = `${freshHost()}:8443`;
    expect(await txtValue(host)).not.toBe(await txtValue(host.replace(":8443", "")));
    await world(host, freshHost(), PROOF);
    const body = await jsonOf(await register(host, { domains: ["port-corp.example"] }));
    expect((body.signin_domains_missing as { txt_value: string }[])[0]!.txt_value).toBe(await txtValue(host));
  });

  it("refuses another server that copies the public proof and claims the domain", async () => {
    const acme = freshHost();
    const evil = freshHost();
    await world(acme, evil, PROOF);
    expect((await jsonOf(await register(acme, { domains: [ACME] }))).signin_domains_verified).toEqual([ACME]);

    // Evil serves acme's public proof on its own host. The relay hashes the secret the request
    // carries, and evil does not know the one behind acme's proof.
    const copied = await register(evil, { secret: OTHER_SECRET, domains: [ACME] });
    expect(copied.status).toBe(403);
    expect(await jsonOf(copied)).toEqual({ error: "proof_failed" });
    const asSecret = await call("/register", postJson({ host: evil, secret: PROOF, signin_domains: [ACME] }));
    expect(asSecret.status).toBe(400);
    expect(await registry().getRegistration(evil)).toBeNull();

    // Evil registers properly with its own secret: acme's record does not vouch for evil's host.
    await world(acme, evil, await sha256Hex(OTHER_SECRET));
    const own = await jsonOf(await register(evil, { secret: OTHER_SECRET, domains: [ACME] }));
    expect(own.signin_domains_verified).toEqual([]);
    expect(own.signin_domains_missing).toEqual([
      { domain: ACME, txt_name: `_privacyfence-relay.${ACME}`, txt_value: await txtValue(evil, await sha256Hex(OTHER_SECRET)) },
    ]);
    expect((await registry().getRegistration(evil))?.signin_domains).toEqual([]);

    // So a sign-in from acme's organization never reaches evil's host.
    const response = await call(`/callback/idp?state=${encodeURIComponent(stateFor(evil))}&code=abc&hd=${ACME}`);
    expect(response.status).toBe(403);
    expect(response.headers.get("Location")).toBeNull();
  });

  it("refuses a server that serves the proof under the old unbound record format", async () => {
    const host = freshHost();
    const unbound = `pf-relay=${await sha256Hex(SECRET)}`;
    stubFetch((url) =>
      url === proofUrl(host)
        ? new Response(PROOF)
        : new Response(JSON.stringify({ Status: 0, Answer: [{ type: 16, data: `"${unbound}"` }] })),
    );
    expect((await jsonOf(await register(host, { domains: [ACME] }))).signin_domains_verified).toEqual([]);
  });
});

describe("only the secret changes a registration", () => {
  it("does not let a third party with the public proof wipe the verified domains or refresh the row", async () => {
    const acme = freshHost();
    await world(acme, freshHost(), PROOF);
    expect((await jsonOf(await register(acme, { domains: [ACME] }))).signin_domains_verified).toEqual([ACME]);
    await registry().upsertVerified(acme, "2030-01-01T00:00:00.000Z", "", [ACME]);
    const before = await registry().getRegistration(acme);

    // Everything the attacker can learn: the host name and what it serves publicly.
    for (const body of [
      { host: acme, secret: PROOF },
      { host: acme, secret: PROOF.slice(0, 43) },
      { host: acme, token: PROOF, signin_domains: [] },
      { host: acme, secret: OTHER_SECRET, signin_domains: [] },
    ]) {
      const response = await call("/register", postJson(body, { "CF-Connecting-IP": freshIp() }));
      expect([400, 403]).toContain(response.status);
    }
    const after = await registry().getRegistration(acme);
    expect(after?.signin_domains).toEqual([ACME]);
    expect(after?.verified_at).toBe(before?.verified_at);

    const signin = await call(`/callback/idp?state=${encodeURIComponent(stateFor(acme))}&code=abc&hd=${ACME}`);
    expect(signin.status).toBe(302);
  });

  it("keeps a verified domain the server itself leaves out, until DNS no longer has it", async () => {
    const host = freshHost();
    const record = await txtValue(host);
    let published = true;
    stubFetch((url) => {
      if (url === proofUrl(host)) return new Response(PROOF);
      return new Response(
        JSON.stringify(published ? { Status: 0, Answer: [{ type: 16, data: `"${record}"` }] } : { Status: 3 }),
      );
    });
    expect((await jsonOf(await register(host, { domains: [ACME] }))).signin_domains_verified).toEqual([ACME]);
    // Left out of the request: checked again and kept.
    expect((await jsonOf(await register(host, { domains: [] }))).signin_domains_verified).toEqual([ACME]);
    expect((await jsonOf(await register(host))).signin_domains_verified).toEqual([ACME]);
    expect((await registry().getRegistration(host))?.signin_domains).toEqual([ACME]);
    // The record is gone: dropped, and not reported as missing since nobody asked for it.
    published = false;
    const dropped = await jsonOf(await register(host));
    expect(dropped.signin_domains_verified).toEqual([]);
    expect(dropped.signin_domains_missing).toEqual([]);
    expect((await registry().getRegistration(host))?.signin_domains).toEqual([]);
  });

  it("checks at most 20 domains, the requested ones first", async () => {
    const host = freshHost();
    const record = await txtValue(host);
    const spy = stubFetch((url) =>
      url === proofUrl(host)
        ? new Response(PROOF)
        : new Response(JSON.stringify({ Status: 0, Answer: [{ type: 16, data: `"${record}"` }] })),
    );
    const first = Array.from({ length: 20 }, (_, i) => `a${i}.cap.example`);
    const second = Array.from({ length: 20 }, (_, i) => `b${i}.cap.example`);
    await register(host, { domains: first });
    spy.mockClear();
    const body = await jsonOf(await register(host, { domains: second }));
    expect(body.signin_domains_verified).toEqual([...second].sort());
    expect(spy.mock.calls.filter(([url]) => String(url).startsWith("https://cloudflare-dns.com/"))).toHaveLength(20);
  });
});

describe("a domain that can never be a sign-in domain", () => {
  it.each([["co.uk"], ["github.io"], ["gmail.com"], ["pages.dev"]])(
    "refuses only %s and still registers the host",
    async (bad) => {
      const host = freshHost();
      const record = await txtValue(host);
      stubFetch((url) =>
        url === proofUrl(host)
          ? new Response(PROOF)
          : new Response(JSON.stringify({ Status: 0, Answer: [{ type: 16, data: `"${record}"` }] })),
      );
      const response = await register(host, { domains: [bad, ACME] });
      expect(response.status).toBe(200);
      const body = await jsonOf(response);
      expect(body.signin_domains_verified).toEqual([ACME]);
      expect(body.signin_domains_missing).toEqual([]);
      expect(body.signin_domains_refused).toEqual([bad]);
      expect((await registry().getRegistration(host))?.signin_domains).toEqual([ACME]);
    },
  );
});

describe("requests a browser can send", () => {
  it.each([
    ["text/plain", "text/plain"],
    ["a form", "application/x-www-form-urlencoded"],
    ["multipart", "multipart/form-data; boundary=x"],
    ["no content type", ""],
    ["json in a suffix", "application/json-patch+json"],
  ])("refuses %s with 415 and fetches nothing", async (_name, type) => {
    const spy = stubFetch(() => new Response("never", { status: 500 }));
    const headers: Record<string, string> = { "CF-Connecting-IP": freshIp() };
    if (type !== "") headers["content-type"] = type;
    const response = await call("/register", {
      method: "POST",
      headers,
      body: new Blob([JSON.stringify({ host: freshHost(), secret: SECRET })]),
    });
    expect(response.status).toBe(415);
    expect(await jsonOf(response)).toEqual({ error: "unsupported_media_type" });
    expect(spy).not.toHaveBeenCalled();
  });

  it("accepts application/json with a charset, in any case", async () => {
    const host = freshHost();
    stubFetch((url) => (url === proofUrl(host) ? new Response(PROOF) : new Response("", { status: 500 })));
    const response = await call(
      "/register",
      postJson({ host, secret: SECRET }, { "content-type": "Application/JSON; charset=utf-8", "CF-Connecting-IP": freshIp() }),
    );
    expect(response.status).toBe(200);
  });
});

describe("rate limits key IPv6 on the /64", () => {
  let prefix = 0;
  const fresh64 = () => {
    prefix += 1;
    return `2001:db8:${prefix.toString(16)}:1`;
  };

  it("counts every address of one /64 together for the new-host cap", async () => {
    const net = fresh64();
    for (let i = 0; i < 5; i += 1) {
      const host = freshHost();
      stubFetch((url) => (url === proofUrl(host) ? new Response(PROOF) : new Response("", { status: 500 })));
      expect((await register(host, { ip: `${net}::${i + 1}` })).status).toBe(200);
    }
    const sixth = freshHost();
    stubFetch((url) => (url === proofUrl(sixth) ? new Response(PROOF) : new Response("", { status: 500 })));
    const refused = await register(sixth, { ip: `${net}:abcd:ef01:2345:6789` });
    expect(refused.status).toBe(429);
    expect(await jsonOf(refused)).toEqual({ error: "too_many_new_hosts" });
    // The next /64 is another subscriber.
    expect((await register(sixth, { ip: `${fresh64()}::1` })).status).toBe(200);
  });

  it("throttles a repeated failure from another address of the same /64", async () => {
    const net = fresh64();
    const host = freshHost();
    stubFetch(() => new Response("no", { status: 404 }));
    expect((await register(host, { ip: `${net}::1` })).status).toBe(403);
    expect((await register(host, { ip: `${net}::2` })).status).toBe(429);
  });
});

describe("GET /registrations", () => {
  it("lists only hosts verified within the last 35 days", async () => {
    const [fresh, stale] = [freshHost(), freshHost()];
    stubFetch((url) => (url === proofUrl(fresh) || url === proofUrl(stale) ? new Response(PROOF) : new Response("", { status: 500 })));
    expect((await register(fresh)).status).toBe(200);
    expect((await register(stale)).status).toBe(200);
    await registry().upsertVerified(stale, new Date(Date.now() - 36 * DAY_MS).toISOString(), "", []);
    const response = await call("/registrations", { headers: { authorization: "Bearer test-revoke-token" } });
    const hosts = ((await response.json()) as { host: string }[]).map((row) => row.host);
    expect(hosts).toContain(fresh);
    expect(hosts).not.toContain(stale);
    const count = await registry().countActive(Date.now(), 35 * DAY_MS);
    expect(count).toBe(hosts.length);
  });
});

describe("a stored domain the suffix list now refuses", () => {
  it("is not kept when the request leaves it out", async () => {
    const host = freshHost();
    const record = await txtValue(host);
    stubFetch((url) =>
      url === proofUrl(host)
        ? new Response(PROOF)
        : new Response(JSON.stringify({ Status: 0, Answer: [{ type: 16, data: `"${record}"` }] })),
    );
    await registry().upsertVerified(host, new Date().toISOString(), "", ["github.io", ACME]);
    const body = await jsonOf(await register(host));
    expect(body.signin_domains_verified).toEqual([ACME]);
    expect((await registry().getRegistration(host))?.signin_domains).toEqual([ACME]);
  });
});
