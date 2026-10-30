/**
 * The hard cap of 50 notification mails a UTC day. Its own file: storage is isolated per
 * file, so the day's count starts at zero here.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetNotifyCache } from "../src/notify";
import { PROOF, decodeRaw, freshHost, freshIp, mailEnv, register, registry, stubFetch } from "./helpers";

afterEach(() => {
  vi.restoreAllMocks();
  resetNotifyCache();
});

describe("the daily notification cap", () => {
  it("sends one over-the-limit message for the 51st registration and none for the 52nd", async () => {
    const sent: string[] = [];
    stubFetch((url, init) => {
      if (url.endsWith("/.well-known/privacyfence-relay")) return new Response(PROOF);
      if (url.startsWith("https://oauth2.googleapis.com/")) {
        return new Response(JSON.stringify({ access_token: "at", expires_in: 3600 }));
      }
      sent.push(decodeRaw((JSON.parse(String(init?.body)) as { raw: string }).raw));
      return new Response("{}");
    });
    let ip = freshIp();
    const hosts: string[] = [];
    for (let i = 0; i < 52; i += 1) {
      if (i % 5 === 0) ip = freshIp();
      const host = freshHost();
      hosts.push(host);
      expect((await register(host, { ip }, mailEnv())).status).toBe(200);
    }
    expect(sent).toHaveLength(51);
    expect(sent.slice(0, 50).every((m) => m.includes("Subject: New PrivacyFence relay registration:"))).toBe(true);
    expect(sent[50]).toContain("More new registrations today than the notification limit; see /registrations.");
    expect((await registry().getRegistration(hosts[49]!))?.notified).toBe(true);
    // The hosts past the cap are listed as not notified, so the weekly review starts with them.
    expect((await registry().getRegistration(hosts[50]!))?.notified).toBe(false);
    expect((await registry().getRegistration(hosts[51]!))?.notified).toBe(false);
  });

  it("starts counting again on a new UTC day", async () => {
    expect(await registry().reserveNotification("2099-01-01")).toBe("send");
    for (let i = 1; i < 50; i += 1) await registry().reserveNotification("2099-01-01");
    expect(await registry().reserveNotification("2099-01-01")).toBe("limit");
    expect(await registry().reserveNotification("2099-01-01")).toBe("none");
    expect(await registry().reserveNotification("2099-01-02")).toBe("send");
  });
});
