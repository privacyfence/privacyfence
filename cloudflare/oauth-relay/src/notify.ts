/**
 * New-registration notification (maintainer alert): one plain-text mail to the maintainers through the
 * Gmail API, with `fetch` only -- no SDK, no dependency, no [[send_email]] binding.
 *
 * The OAuth client is an Internal Web client of the privacyfence.eu Workspace; its refresh token is
 * minted for the QA account with the single scope gmail.send, so it can send but never read mail.
 * The message goes out as `NOTIFY_FROM` (a "Send mail as" alias of that account) to the constant
 * NOTIFY_TO, never to an address taken from a request.
 */
export const NOTIFY_TO = "info@privacyfence.eu";

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send";
const TIMEOUT_MS = 10_000;
const EXPIRY_MARGIN_MS = 60_000;

export type NotifyResult =
  | { status: "sent" }
  | { status: "skipped" }
  | { status: "failed"; code: string };

export interface NotifyConfig {
  NOTIFY_FROM?: string;
  GMAIL_CLIENT_ID?: string;
  GMAIL_CLIENT_SECRET?: string;
  GMAIL_REFRESH_TOKEN?: string;
}

interface CachedToken {
  accessToken: string;
  expiresAtMs: number;
}

let cached: CachedToken | null = null;

/** Drops the cached access token. Tests only; the Worker never needs it. */
export function resetNotifyCache(): void {
  cached = null;
}

export function notifyConfigured(config: NotifyConfig): boolean {
  return Boolean(
    config.NOTIFY_FROM && config.GMAIL_CLIENT_ID && config.GMAIL_CLIENT_SECRET && config.GMAIL_REFRESH_TOKEN,
  );
}

export interface Message {
  from: string;
  to: string;
  subject: string;
  body: string;
  date: Date;
  messageId: string;
}

/** An RFC 5322 plain-text message. Refuses any header value containing CR or LF. */
export function buildMessage(message: Message): string {
  const headers: [string, string][] = [
    ["From", message.from],
    ["To", message.to],
    ["Subject", message.subject],
    ["Date", message.date.toUTCString()],
    ["Message-ID", message.messageId],
    ["MIME-Version", "1.0"],
    ["Content-Type", "text/plain; charset=utf-8"],
  ];
  for (const [name, value] of headers) {
    if (/[\r\n]/.test(value)) throw new Error(`header ${name} contains a line break`);
  }
  const head = headers.map(([name, value]) => `${name}: ${value}`).join("\r\n");
  return `${head}\r\n\r\n${message.body.replace(/\r?\n/g, "\r\n")}\r\n`;
}

function base64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function accessToken(config: NotifyConfig, nowMs: number): Promise<{ token: string } | { code: string }> {
  if (cached !== null && nowMs < cached.expiresAtMs - EXPIRY_MARGIN_MS) return { token: cached.accessToken };
  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: config.GMAIL_CLIENT_ID!,
      client_secret: config.GMAIL_CLIENT_SECRET!,
      refresh_token: config.GMAIL_REFRESH_TOKEN!,
      grant_type: "refresh_token",
    }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!response.ok) {
    let error = "";
    try {
      error = ((await response.json()) as { error?: string }).error ?? "";
    } catch {
      // not JSON: the status code alone is the error
    }
    return { code: error === "invalid_grant" ? "token_invalid_grant" : `token_http_${response.status}` };
  }
  const body = (await response.json()) as { access_token?: string; expires_in?: number };
  if (!body.access_token) return { code: "token_http_200" };
  cached = { accessToken: body.access_token, expiresAtMs: nowMs + (body.expires_in ?? 0) * 1000 };
  return { token: body.access_token };
}

/** Sends one mail. Never throws; a failure is a short code, never a secret or a response body. */
export async function sendNotification(
  config: NotifyConfig,
  subject: string,
  body: string,
  now: Date = new Date(),
): Promise<NotifyResult> {
  if (!notifyConfigured(config)) return { status: "skipped" };
  const from = config.NOTIFY_FROM!;
  let raw: string;
  try {
    raw = buildMessage({
      from,
      to: NOTIFY_TO,
      subject,
      body,
      date: now,
      messageId: `<${crypto.randomUUID()}@${from.slice(from.indexOf("@") + 1)}>`,
    });
  } catch {
    return { status: "failed", code: "bad_header" };
  }
  try {
    const token = await accessToken(config, now.getTime());
    if ("code" in token) return { status: "failed", code: token.code };
    const response = await fetch(SEND_URL, {
      method: "POST",
      headers: { authorization: `Bearer ${token.token}`, "content-type": "application/json" },
      body: JSON.stringify({ raw: base64Url(raw) }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!response.ok) {
      if (response.status === 401) cached = null;
      return { status: "failed", code: `send_http_${response.status}` };
    }
    return { status: "sent" };
  } catch {
    return { status: "failed", code: "network" };
  }
}
