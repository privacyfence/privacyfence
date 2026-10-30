/** The registration email through the Gmail API, with fetch stubbed for both endpoints. */
import { env } from "cloudflare:test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildMessage, notifyConfigured, resetNotifyCache, sendNotification } from "../src/notify";
import {
  PROOF,
  SECRET,
  bearer,
  call,
  decodeRaw,
  freshHost,
  freshIp,
  mailEnv,
  proofServes,
  proofUrl,
  register,
  registry,
  stubFetch,
  txtValue,
} from "./helpers";

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send";

beforeEach(() => {
  resetNotifyCache();
});

afterEach(() => {
  vi.restoreAllMocks();
});

interface Gmail {
  sent: string[];
  tokenCalls: number;
}

/** A fetch handler where `host` proves itself and both Gmail endpoints answer, recording what was sent. */
function gmailStub(host: string, overrides: { token?: () => Response; send?: () => Response } = {}) {
  const gmail: Gmail = { sent: [], tokenCalls: 0 };
  const spy = stubFetch((url, init) => {
    if (url === proofUrl(host)) return new Response(PROOF);
    if (url === TOKEN_URL) {
      gmail.tokenCalls += 1;
      return overrides.token?.() ?? new Response(JSON.stringify({ access_token: "at-1", expires_in: 3600 }));
    }
    if (url === SEND_URL) {
      const { raw } = JSON.parse(String(init?.body)) as { raw: string };
      gmail.sent.push(decodeRaw(raw));
      return overrides.send?.() ?? new Response("{}");
    }
    return new Response("unexpected " + url, { status: 500 });
  });
  return { gmail, spy };
}

async function jsonOf(response: Response): Promise<Record<string, unknown>> {
  return (await response.json()) as Record<string, unknown>;
}

async function stats(): Promise<Record<string, unknown>> {
  return (await registry().notifyStats()) as unknown as Record<string, unknown>;
}

describe("the first registration email", () => {
  it("sends exactly one plain-text message with the host, IP, country and the revoke command", async () => {
    const host = freshHost();
    const { gmail, spy } = gmailStub(host);
    const ip = freshIp();
    const response = await register(host, { ip }, mailEnv());
    expect(response.status).toBe(200);
    expect(gmail.sent).toHaveLength(1);
    const message = gmail.sent[0]!;
    expect(message).toContain("From: relay-notify@privacyfence.eu\r\n");
    expect(message).toContain("To: info@privacyfence.eu\r\n");
    expect(message).toContain(`Subject: New PrivacyFence relay registration: ${host}\r\n`);
    expect(message).toContain("Content-Type: text/plain; charset=utf-8\r\n");
    expect(message).toContain("MIME-Version: 1.0\r\n");
    expect(message).toMatch(/Message-ID: <[0-9a-f-]{36}@privacyfence\.eu>\r\n/);
    expect(message).toMatch(/Date: \w{3}, \d{2} \w{3} \d{4}/);
    expect(message).toContain(`Host: ${host}`);
    expect(message).toContain(`IP: ${ip}`);
    expect(message).toContain("Country: DE");
    expect(message).toMatch(/Active registrations: \d+/);
    expect(message).toContain("Built-in Google sign-in: no");
    expect(message).toContain(
      `curl -X POST -H "Authorization: Bearer $REVOKE_TOKEN" -H "Content-Type: application/json" ` +
        `-d '{"host":"${host}"}' https://oauth.privacyfence.eu/admin/revoke`,
    );
    const sendCall = spy.mock.calls.find(([url]) => url === SEND_URL)!;
    expect((sendCall[1] as RequestInit).headers).toMatchObject({ authorization: "Bearer at-1" });
    expect((await registry().getRegistration(host))?.notified).toBe(true);
  });

  it("sends nothing for a re-verification", async () => {
    const host = freshHost();
    const { gmail } = gmailStub(host);
    await register(host, {}, mailEnv());
    await register(host, {}, mailEnv());
    expect(gmail.sent).toHaveLength(1);
  });

  it("mentions the verified sign-in domains, and mails once more only for a domain no mail named", async () => {
    const host = freshHost();
    const domain = "mail-corp.example";
    const second = "mail-corp2.example";
    const value = await txtValue(host);
    const gmail: Gmail = { sent: [], tokenCalls: 0 };
    let published = new Set([domain]);
    stubFetch((url, init) => {
      if (url === proofUrl(host)) return new Response(PROOF);
      if (url.startsWith("https://cloudflare-dns.com/")) {
        const name = new URL(url).searchParams.get("name")!.replace("_privacyfence-relay.", "");
        return new Response(
          JSON.stringify(published.has(name) ? { Status: 0, Answer: [{ type: 16, data: `"${value}"` }] } : { Status: 3 }),
        );
      }
      if (url === TOKEN_URL) return new Response(JSON.stringify({ access_token: "at", expires_in: 3600 }));
      gmail.sent.push(decodeRaw((JSON.parse(String(init?.body)) as { raw: string }).raw));
      return new Response("{}");
    });
    await register(host, { domains: [domain] }, mailEnv());
    expect(gmail.sent).toHaveLength(1);
    expect(gmail.sent[0]).toContain(`Built-in Google sign-in: yes (${domain})`);
    await register(host, { domains: [domain] }, mailEnv());
    expect(gmail.sent).toHaveLength(1);
    // A removed record takes the domain away without a mail, and putting it back sends none either.
    published = new Set();
    expect((await jsonOf(await register(host, { domains: [domain] }, mailEnv()))).signin_domains_verified).toEqual([]);
    published = new Set([domain]);
    await register(host, { domains: [domain] }, mailEnv());
    expect(gmail.sent).toHaveLength(1);
    // A domain no mail named yet is mailed.
    published = new Set([domain, second]);
    await register(host, { domains: [domain, second] }, mailEnv());
    expect(gmail.sent).toHaveLength(2);
    expect(gmail.sent[1]).toContain(`Subject: PrivacyFence relay sign-in domains added: ${host}\r\n`);
    expect(gmail.sent[1]).toContain(`Built-in Google sign-in: yes (${domain}, ${second})`);
    expect((await registry().getRegistration(host))?.notified).toBe(true);
  });

  it("mails about one host at most three times a day, and leaves the rest unnotified", async () => {
    const host = freshHost();
    const value = await txtValue(host);
    const gmail: Gmail = { sent: [], tokenCalls: 0 };
    // Wildcard DNS: every name proves this host, so each request can bring new domains.
    stubFetch((url, init) => {
      if (url === proofUrl(host)) return new Response(PROOF);
      if (url.startsWith("https://cloudflare-dns.com/")) {
        return new Response(JSON.stringify({ Status: 0, Answer: [{ type: 16, data: `"${value}"` }] }));
      }
      if (url === TOKEN_URL) return new Response(JSON.stringify({ access_token: "at", expires_in: 3600 }));
      gmail.sent.push(decodeRaw((JSON.parse(String(init?.body)) as { raw: string }).raw));
      return new Response("{}");
    });
    const ip = freshIp();
    for (let i = 0; i < 8; i += 1) {
      const domains = Array.from({ length: 20 }, (_, j) => `w${i}-${j}.wild.example`);
      expect((await register(host, { ip, domains }, mailEnv())).status).toBe(200);
    }
    // The first registration and two changes; the five later changes wait for the weekly review.
    expect(gmail.sent).toHaveLength(3);
    expect((await registry().getRegistration(host))?.notified).toBe(false);
    expect((await registry().getRegistration(host))?.signin_domains).toHaveLength(20);
  });

  it("reuses the access token within its lifetime and fetches a new one after it", async () => {
    const first = freshHost();
    const second = freshHost();
    const { gmail } = gmailStub(first);
    await register(first, {}, mailEnv());
    stubFetch((url, init) => {
      if (url === proofUrl(second)) return new Response(PROOF);
      if (url === TOKEN_URL) {
        gmail.tokenCalls += 1;
        return new Response(JSON.stringify({ access_token: "at-2", expires_in: 3600 }));
      }
      gmail.sent.push(decodeRaw((JSON.parse(String(init?.body)) as { raw: string }).raw));
      return new Response("{}");
    });
    await register(second, {}, mailEnv());
    expect(gmail.tokenCalls).toBe(1);

    // After the token's lifetime the next mail asks for a new one.
    const future = new Date(Date.now() + 2 * 3600_000);
    const result = await sendNotification(mailEnv(), "s", "b", future);
    expect(result).toEqual({ status: "sent" });
    expect(gmail.tokenCalls).toBe(2);
  });
});

describe("failures never fail the registration", () => {
  async function failureCase(
    overrides: Parameters<typeof gmailStub>[1],
    expectedCode: string,
    options: { reject?: boolean } = {},
  ): Promise<void> {
    const host = freshHost();
    const before = Number((await stats()).notify_failures);
    if (options.reject) {
      stubFetch((url) => {
        if (url === proofUrl(host)) return new Response(PROOF);
        throw new DOMException("timed out", "TimeoutError");
      });
    } else {
      gmailStub(host, overrides);
    }
    const response = await register(host, {}, mailEnv());
    expect(response.status).toBe(200);
    const after = await stats();
    expect(after.notify_failures).toBe(before + 1);
    expect(after.notify_last_error).toBe(expectedCode);
    expect(typeof after.last_notify_failure_at).toBe("string");
    expect((await registry().getRegistration(host))?.notified).toBe(false);

    const listing = await call("/registrations", { headers: bearer() });
    const text = JSON.stringify([...listing.headers.entries()]) + (await listing.text()) + (await response.clone().text());
    for (const secret of ["gmail-client-secret", "gmail-refresh-token", "gmail-client-id", "at-1"]) {
      expect(text).not.toContain(secret);
    }
    expect(listing.headers.get("X-Notify-Last-Error")).toBe(expectedCode);
  }

  it("records token_invalid_grant", async () => {
    await failureCase({ token: () => new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 }) }, "token_invalid_grant");
  });

  it("records token_http_<status> for a 500 from the token endpoint, with or without a JSON body", async () => {
    await failureCase({ token: () => new Response("oops", { status: 500 }) }, "token_http_500");
    resetNotifyCache();
    await failureCase({ token: () => new Response(JSON.stringify({ error: "other" }), { status: 401 }) }, "token_http_401");
  });

  it("records token_http_200 for a token response without an access token", async () => {
    await failureCase({ token: () => new Response("{}") }, "token_http_200");
  });

  it("records send_http_<status> for a 500 from the send endpoint, and drops the token on 401", async () => {
    await failureCase({ send: () => new Response("oops", { status: 500 }) }, "send_http_500");
    resetNotifyCache();
    await failureCase({ send: () => new Response("no", { status: 401 }) }, "send_http_401");
  });

  it("records network for a timeout and for a thrown fetch", async () => {
    await failureCase({}, "network", { reject: true });
  });
});

describe("missing configuration", () => {
  it.each(["NOTIFY_FROM", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"] as const)(
    "counts notify_skipped and makes no outbound call when %s is empty",
    async (name) => {
      const host = freshHost();
      const spy = stubFetch(proofServes(host));
      const before = Number((await stats()).notify_skipped);
      const response = await register(host, {}, { ...mailEnv(), [name]: "" });
      expect(response.status).toBe(200);
      expect(spy.mock.calls.map(([url]) => String(url))).toEqual([proofUrl(host)]);
      expect((await stats()).notify_skipped).toBe(before + 1);
    },
  );

  it("sendNotification reports skipped on its own", async () => {
    expect(await sendNotification({ ...env, GMAIL_REFRESH_TOKEN: "" }, "s", "b")).toEqual({ status: "skipped" });
    expect(notifyConfigured(mailEnv())).toBe(true);
    expect(notifyConfigured({})).toBe(false);
  });
});

describe("buildMessage", () => {
  const base = {
    from: "relay-notify@privacyfence.eu",
    to: "info@privacyfence.eu",
    subject: "s",
    body: "line one\nline two",
    date: new Date("2026-10-29T12:00:00Z"),
    messageId: "<id@privacyfence.eu>",
  };

  it("builds a CRLF message", () => {
    const message = buildMessage(base);
    expect(message).toContain("Date: Thu, 29 Oct 2026 12:00:00 GMT\r\n");
    expect(message.endsWith("\r\n\r\nline one\r\nline two\r\n")).toBe(true);
  });

  it.each(["from", "to", "subject", "messageId"] as const)("refuses a line break in %s", (field) => {
    expect(() => buildMessage({ ...base, [field]: "a\nb" })).toThrow();
    expect(() => buildMessage({ ...base, [field]: "a\rb" })).toThrow();
  });

  it("makes sendNotification fail with bad_header instead of sending", async () => {
    const spy = stubFetch(() => new Response("{}"));
    const result = await sendNotification({ ...mailEnv(), NOTIFY_FROM: "a@b.example\nBcc: x@y.example" }, "s", "b");
    expect(result).toEqual({ status: "failed", code: "bad_header" });
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("a send that reports success without a Gmail body", () => {
  it("is sent", async () => {
    stubFetch((url) =>
      url === TOKEN_URL ? new Response(JSON.stringify({ access_token: "x" })) : new Response(null, { status: 200 }),
    );
    expect(await sendNotification(mailEnv(), "s", "b")).toEqual({ status: "sent" });
  });
});
