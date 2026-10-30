import { describe, expect, it } from "vitest";
import {
  forwardPath,
  inPrivateSuffix,
  isSigninGrant,
  loopbackHost,
  parseState,
  publicHost,
  validRelayHost,
  validSigninDomain,
  rateLimitKey,
  validSecret,
} from "../src/validate";

describe("validRelayHost", () => {
  it.each([
    "pf.acme.example.com",
    "pf.acme.example.com:8443",
    "a.co",
    "x-y.example.org",
    "a1.b2.example.org:65535",
    "a".repeat(63) + ".example.org",
  ])("accepts %s", (host) => {
    expect(validRelayHost(host)).toBe(true);
  });

  it.each([
    "a..b",
    "-x.com",
    "x-.com",
    "x.com:0",
    "x.com:99999",
    "x.com:65536",
    "x.com:08080",
    "x.com:",
    "x.com:80a",
    "foo.localhost.",
    "x.com.",
    "localhost",
    "x",
    "127.1",
    "2130706433",
    "0x7f000001",
    "127.0.0.1",
    "10.0.0.1",
    "a.b.c.1",
    "x.c",
    "x.com1",
    "X.com",
    "pf.Acme.com",
    "a_b.example.org",
    "user@host.example.org",
    "host.example.org/path",
    "[::1]",
    "x.com:80:90",
    "x.com:443",
    "a".repeat(64) + ".example.org",
    `${"a".repeat(60)}.`.repeat(5) + "com",
    "",
  ])("rejects %j", (host) => {
    expect(validRelayHost(host)).toBe(false);
  });

  it.each([null, undefined, 5, {}, ["x.com"]])("rejects the non-string %j", (value) => {
    expect(validRelayHost(value)).toBe(false);
  });
});

describe("publicHost", () => {
  it.each(["foo.localhost", "a.b.localhost", "x.local", "x.internal", "x.home.arpa", "x.lan", "x.local:8080"])(
    "%s is not public",
    (host) => {
      expect(publicHost(host)).toBe(false);
    },
  );

  it.each(["pf.acme.example.com", "localhost.example.com", "lan.example.com", "pf.acme.example.com:8443"])(
    "%s is public",
    (host) => {
      expect(publicHost(host)).toBe(true);
    },
  );

  it("treats a bare localhost as not public", () => {
    expect(publicHost("localhost")).toBe(false);
  });
});

describe("loopbackHost", () => {
  it.each(["localhost", "127.0.0.1", "localhost:8765", "127.0.0.1:1", "localhost:65535"])("accepts %s", (host) => {
    expect(loopbackHost(host)).toBe(true);
  });

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
    "localhost:",
    "127.0.0.2",
    "",
  ])("rejects %j", (host) => {
    expect(loopbackHost(host)).toBe(false);
  });
});

describe("inPrivateSuffix", () => {
  it.each([
    "x.github.io",
    "github.io",
    "a.pages.dev",
    "a.workers.dev",
    "a.netlify.app",
    "deep.sub.x.github.io",
    "pf.example.com.s3.amazonaws.com",
    "a.b.compute.amazonaws.com",
    "x.github.io:8443",
  ])("%s is under a private suffix", (host) => {
    expect(inPrivateSuffix(host)).toBe(true);
  });

  it.each(["pf.acme.example.com", "github.com", "acme.dev", "pages.example.org"])("%s is not", (host) => {
    expect(inPrivateSuffix(host)).toBe(false);
  });
});

describe("validSigninDomain", () => {
  it.each(["acme.com", "sub.acme.co.uk", "acme.de"])("accepts %s", (domain) => {
    expect(validSigninDomain(domain)).toBe(true);
  });

  it.each([
    "gmail.com",
    "googlemail.com",
    "co.uk",
    "com",
    "acme",
    "Acme.com",
    "acme.com:443",
    "x.github.io",
    "a..b",
    "foo.ck",
    "",
    "a".repeat(254),
    5,
    null,
  ])("rejects %j", (domain) => {
    expect(validSigninDomain(domain)).toBe(false);
  });
});

describe("validSecret", () => {
  it("wants exactly 43 url-safe characters", () => {
    expect(validSecret("A".repeat(43))).toBe(true);
    expect(validSecret("a-_".repeat(14) + "a")).toBe(true);
    expect(validSecret("A".repeat(42))).toBe(false);
    expect(validSecret("A".repeat(44))).toBe(false);
    expect(validSecret("A".repeat(42) + "+")).toBe(false);
    expect(validSecret(null)).toBe(false);
  });
});

describe("parseState", () => {
  it("splits host and nonce at the first tilde", () => {
    const nonce = "n".repeat(43);
    expect(parseState(`pf.acme.com~${nonce}`)).toEqual({ host: "pf.acme.com", nonce });
    expect(parseState(`pf.acme.com:8443~${nonce}`)).toEqual({ host: "pf.acme.com:8443", nonce });
  });

  it.each(["", "nohost", "~" + "n".repeat(43), "pf.acme.com~", "pf.acme.com~short", "pf.acme.com~" + "n".repeat(43) + "!"])(
    "refuses %j",
    (state) => {
      expect(parseState(state)).toBeNull();
    },
  );
});

describe("grants", () => {
  it("maps connector and sign-in grants to their forward paths", () => {
    expect(forwardPath("gmail")).toBe("/oauth/callback/gmail");
    expect(forwardPath("apps_script")).toBe("/oauth/callback/apps_script");
    expect(forwardPath("idp")).toBe("/oauth/idp/callback");
    expect(forwardPath("idp-login")).toBe("/oauth/idp/login-callback");
    expect(forwardPath("stepup")).toBe("/oauth/stepup/callback");
    expect(forwardPath("nope")).toBeNull();
    expect(forwardPath("constructor")).toBeNull();
  });

  it("knows the sign-in grants", () => {
    expect(isSigninGrant("idp")).toBe(true);
    expect(isSigninGrant("slack")).toBe(false);
    expect(isSigninGrant("toString")).toBe(false);
  });
});

describe("rateLimitKey", () => {
  it.each([
    ["203.0.113.9", "203.0.113.9"],
    ["2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"],
    ["2001:DB8:1:2::1", "2001:db8:1:2::/64"],
    ["2001:db8::1", "2001:db8:0:0::/64"],
    ["::1", "0:0:0:0::/64"],
    ["fe80::1%eth0", "fe80:0:0:0::/64"],
    ["::ffff:198.51.100.7", "198.51.100.7"],
    ["64:ff9b::198.51.100.7", "64:ff9b:0:0::/64"],
    ["1:2:3:4:5:6:198.51.100.7", "1:2:3:4::/64"],
  ])("keys %s as %s", (ip, key) => {
    expect(rateLimitKey(ip)).toBe(key);
  });

  it.each([["unknown"], ["1:2:3"], ["1::2::3"], ["1:2:3:4:5:6:7:8:9"], ["1::2:3:4:5:6:7:8"], ["g::1"], ["::1.2.3.999"]])(
    "uses %s as is when it is no address",
    (ip) => {
      expect(rateLimitKey(ip)).toBe(ip);
    },
  );
});
