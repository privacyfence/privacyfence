/**
 * The host rules of the relay contract (docs/builtin-oauth-apps-plan.md D5, D9, D15 SEC-02/SEC-10)
 * and the token, state and sign-in-domain parsing. The Python side (relay_client.valid_relay_host,
 * public_host) implements the same rules and tests the same cases.
 */
import { PSL_ICANN, PSL_ICANN_WILDCARD_BASES } from "./psl_icann";
import { PSL_PRIVATE, PSL_PRIVATE_WILDCARD_BASES } from "./psl_private";

const LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const TLD = /^[a-z]{2,63}$/;
const PORT = /^[1-9][0-9]{0,4}$/;
const LOOPBACK = /^(localhost|127\.0\.0\.1)(:([1-9][0-9]{0,4}))?$/;
const TOKEN = /^[A-Za-z0-9_-]{43}$/;
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

/** A host is valid when it satisfies every rule of D5's host list. Case matters: hosts are lower-case. */
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

/** `localhost` or `127.0.0.1`, with an optional port of at most 65535 (SEC-10). */
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
 * section (`x.github.io`, `a.pages.dev`, ngrok domains...): D15 SEC-02.
 */
export function inPrivateSuffix(hostOrDomain: string): boolean {
  const { name } = splitHost(hostOrDomain);
  for (const suffix of labelSuffixes(name)) {
    if (PSL_PRIVATE.has(suffix)) return true;
    if (suffix !== name && PSL_PRIVATE_WILDCARD_BASES.has(suffix)) return true;
  }
  return false;
}

/** A sign-in domain must be a plain registrable-looking name (D20): no port, no public suffix. */
export function validSigninDomain(domain: unknown): domain is string {
  if (typeof domain !== "string" || domain.length === 0 || domain.length > 253) return false;
  if (domain.includes(":") || !validName(domain)) return false;
  if (FREE_MAIL.has(domain) || PSL_ICANN.has(domain) || inPrivateSuffix(domain)) return false;
  const parent = domain.slice(domain.indexOf(".") + 1);
  return !PSL_ICANN_WILDCARD_BASES.has(parent);
}

export function validToken(token: unknown): token is string {
  return typeof token === "string" && TOKEN.test(token);
}

/** `state` is `host~nonce`; the host part is validated separately by the caller. */
export function parseState(state: string): { host: string; nonce: string } | null {
  const tilde = state.indexOf("~");
  if (tilde <= 0) return null;
  const nonce = state.slice(tilde + 1);
  if (!STATE_NONCE.test(nonce)) return null;
  return { host: state.slice(0, tilde), nonce };
}

/** The `grant` -> forward path map: connector grants (D5) and the sign-in grants (D20). */
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
