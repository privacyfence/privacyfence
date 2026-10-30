/**
 * The host rules of the relay contract (see README.md), the secret, state and sign-in-domain
 * parsing, the sign-in domain record value and the rate-limit key. The Python side
 * (relay_client.valid_relay_host, public_host, dns_record_value) implements the same rules and
 * tests the same cases.
 */
import { PSL_ICANN, PSL_ICANN_WILDCARD_BASES } from "./psl_icann";
import { PSL_PRIVATE, PSL_PRIVATE_WILDCARD_BASES } from "./psl_private";

const LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const TLD = /^[a-z]{2,63}$/;
const PORT = /^[1-9][0-9]{0,4}$/;
const LOOPBACK = /^(localhost|127\.0\.0\.1)(:([1-9][0-9]{0,4}))?$/;
const SECRET = /^[A-Za-z0-9_-]{43}$/;
const STATE_NONCE = /^[A-Za-z0-9_-]{16,128}$/;
const NOT_PUBLIC_SUFFIXES = [".localhost", ".local", ".internal", ".home.arpa", ".lan"];
// Mail providers every account of which shares one domain: no organization owns them, so a server
// cannot prove "its" gmail.com. They are not public suffixes, hence the explicit list.
const FREE_MAIL = new Set(["gmail.com", "googlemail.com"]);

export const MAX_SIGNIN_DOMAINS = 20;

/** `name` and optional port of an already shape-checked host. */
function splitHost(host: string): { name: string; port: string | null } {
  const colon = host.lastIndexOf(":");
  if (colon === -1) return { name: host, port: null };
  return { name: host.slice(0, colon), port: host.slice(colon + 1) };
}

function validName(name: string): boolean {
  const labels = name.split(".");
  if (labels.length < 2) return false;
  if (!labels.every((label) => LABEL.test(label))) return false;
  return TLD.test(labels[labels.length - 1]!);
}

/** A host is valid when it satisfies every rule of the host list. Case matters: hosts are lower-case. */
export function validRelayHost(host: unknown): host is string {
  if (typeof host !== "string" || host.length === 0 || host.length > 253) return false;
  const { name, port } = splitHost(host);
  if (port !== null) {
    if (!PORT.test(port) || Number(port) > 65535) return false;
  }
  if (!validName(name)) return false;
  try {
    return new URL(`https://${host}`).host === host;
  } catch {
    return false;
  }
}

/** `localhost` or `127.0.0.1`, with an optional port of at most 65535). */
export function loopbackHost(host: string): boolean {
  const match = LOOPBACK.exec(host);
  if (!match) return false;
  return match[3] === undefined || Number(match[3]) <= 65535;
}

/** Names that can never be a public server. Call on a host that passed validRelayHost. */
export function publicHost(host: string): boolean {
  const { name } = splitHost(host);
  if (name === "localhost") return false;
  return !NOT_PUBLIC_SUFFIXES.some((suffix) => name.endsWith(suffix));
}

function labelSuffixes(name: string): string[] {
  const labels = name.split(".");
  return labels.map((_, index) => labels.slice(index).join("."));
}

/**
 * True when the name is, or sits below, a public suffix of the Public Suffix List's private
 * section (`x.github.io`, `a.pages.dev`, ngrok domains...). Anyone with a free account there could otherwise register.
 */
export function inPrivateSuffix(hostOrDomain: string): boolean {
  const { name } = splitHost(hostOrDomain);
  for (const suffix of labelSuffixes(name)) {
    if (PSL_PRIVATE.has(suffix)) return true;
    if (suffix !== name && PSL_PRIVATE_WILDCARD_BASES.has(suffix)) return true;
  }
  return false;
}

/** The shape of a sign-in domain: a host name with no port. A malformed one refuses the whole request. */
export function signinDomainSyntax(domain: unknown): domain is string {
  if (typeof domain !== "string" || domain.length === 0 || domain.length > 253) return false;
  return !domain.includes(":") && validName(domain);
}

/**
 * Whether a well-formed name may be a sign-in domain: not a shared mail domain and not a public
 * suffix. A name that fails this is refused on its own, never the whole registration.
 */
export function signinDomainAllowed(domain: string): boolean {
  if (FREE_MAIL.has(domain) || PSL_ICANN.has(domain) || inPrivateSuffix(domain)) return false;
  const parent = domain.slice(domain.indexOf(".") + 1);
  return !PSL_ICANN_WILDCARD_BASES.has(parent);
}

/** A sign-in domain must be a plain registrable-looking name (sign-in domain proof): no port, no public suffix. */
export function validSigninDomain(domain: unknown): domain is string {
  return signinDomainSyntax(domain) && signinDomainAllowed(domain);
}

/** The server's registration secret; the server publishes only its SHA-256 (see README.md). */
// The sign-in domain record format. A new format gets a new version tag; never change this one.
const DNS_RECORD_VERSION = "v1";

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * The value of the `_privacyfence-relay.<domain>` TXT record that lets `host` sign in people from
 * `<domain>`: `pf-relay=v1:` + hex SHA-256 of the UTF-8 string `v1|<host>|<proof>`, where `host` is
 * the registered host (lower-case, with its port if it has one) and `proof` is what the host serves
 * at its well-known path. The host is part of the hash, so the record vouches for that one host and
 * no other server can claim the domain with it.
 */
export async function dnsRecordValue(host: string, proof: string): Promise<string> {
  return `pf-relay=${DNS_RECORD_VERSION}:${await sha256Hex(`${DNS_RECORD_VERSION}|${host}|${proof}`)}`;
}

export function validSecret(secret: unknown): secret is string {
  return typeof secret === "string" && SECRET.test(secret);
}

/**
 * The client address the rate limits count against: an IPv4 address as is, an IPv6 address by its
 * /64, since one subscriber is usually given a whole /64. Anything unparsable is used as is.
 */
export function rateLimitKey(ip: string): string {
  if (!ip.includes(":")) return ip;
  const groups = ipv6Groups(ip);
  if (groups === null) return ip;
  if (groups.slice(0, 6).every((g, i) => g === (i === 5 ? 0xffff : 0))) {
    // An IPv4-mapped address is the IPv4 address it carries.
    return [groups[6]! >> 8, groups[6]! & 0xff, groups[7]! >> 8, groups[7]! & 0xff].join(".");
  }
  return `${groups.slice(0, 4).map((g) => g.toString(16)).join(":")}::/64`;
}

/** The eight 16-bit groups of an IPv6 address (with `::` and a trailing dotted IPv4), or null. */
function ipv6Groups(ip: string): number[] | null {
  let text = ip.toLowerCase();
  const zone = text.indexOf("%");
  if (zone !== -1) text = text.slice(0, zone);
  const tail: number[] = [];
  const dotted = /^(.*:)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(text);
  if (dotted) {
    const octets = dotted.slice(2, 6).map(Number);
    if (octets.some((o) => o > 255)) return null;
    tail.push((octets[0]! << 8) | octets[1]!, (octets[2]! << 8) | octets[3]!);
    text = dotted[1]!.endsWith("::") ? dotted[1]! : dotted[1]!.slice(0, -1);
  }
  const halves = text.split("::");
  if (halves.length > 2) return null;
  const parse = (part: string): number[] | null => {
    if (part === "") return [];
    const out: number[] = [];
    for (const group of part.split(":")) {
      if (!/^[0-9a-f]{1,4}$/.test(group)) return null;
      out.push(parseInt(group, 16));
    }
    return out;
  };
  const head = parse(halves[0]!);
  const rest = halves.length === 2 ? parse(halves[1]!) : [];
  if (head === null || rest === null) return null;
  const known = head.length + rest.length + tail.length;
  if (halves.length === 2 ? known > 7 : known !== 8) return null;
  const zeros = new Array<number>(8 - known).fill(0);
  return [...head, ...zeros, ...rest, ...tail];
}

/** `state` is `host~nonce`; the host part is validated separately by the caller. */
export function parseState(state: string): { host: string; nonce: string } | null {
  const tilde = state.indexOf("~");
  if (tilde <= 0) return null;
  const nonce = state.slice(tilde + 1);
  if (!STATE_NONCE.test(nonce)) return null;
  return { host: state.slice(0, tilde), nonce };
}

/** The `grant` -> forward path map: connector grants and the sign-in grants. */
export const CONNECTOR_GRANTS = [
  "gmail",
  "drive",
  "calendar",
  "contacts",
  "tasks",
  "apps_script",
  "slack",
  "salesforce",
  "atlassian",
  "miro",
] as const;

export const SIGNIN_FORWARD_PATHS: Readonly<Record<string, string>> = {
  idp: "/oauth/idp/callback",
  "idp-login": "/oauth/idp/login-callback",
  stepup: "/oauth/stepup/callback",
};

export function forwardPath(grant: string): string | null {
  if ((CONNECTOR_GRANTS as readonly string[]).includes(grant)) return `/oauth/callback/${grant}`;
  return Object.hasOwn(SIGNIN_FORWARD_PATHS, grant) ? SIGNIN_FORWARD_PATHS[grant]! : null;
}

export function isSigninGrant(grant: string): boolean {
  return Object.hasOwn(SIGNIN_FORWARD_PATHS, grant);
}
