/**
 * The relay's only state: one SQLite-backed Durable Object, reached as
 * `env.REGISTRY.get(env.REGISTRY.idFromName("global"))`.
 *
 * Tables:
 *  - registrations: a row exists only after a successful proof (D15 SEC-03). An empty
 *    `verified_at` means "restored, must prove again".
 *  - attempts: register requests and failures, for rate limits. Pruned of rows older than an hour
 *    on every write; never a registrations row.
 *  - new_hosts: first-proof counts per address per UTC day (SEC-25).
 *  - counters: notification bookkeeping (SEC-23), shown to the maintainer on /registrations.
 *
 * Every method is synchronous SQL inside a single-threaded object, so each is atomic.
 */
import { DurableObject } from "cloudflare:workers";

export interface Registration {
  host: string;
  registered_at: string;
  verified_at: string;
  last_attempt_at: string;
  revoked: boolean;
  signin_domains: string[];
  notified: boolean;
}

export type AttemptVerdict = "ok" | "too_soon" | "too_many_new_hosts";
export type NotifyDecision = "send" | "limit" | "none";

export interface UpsertResult {
  isNew: boolean;
  domainsChanged: boolean;
  registered_at: string;
  verified_at: string;
}

export interface NotifyStats {
  notify_failures: number;
  notify_last_error: string;
  notify_skipped: number;
  last_notify_failure_at: string;
}

const PER_HOST_IP_WINDOW_MS = 60_000;
const PER_IP_WINDOW_MS = 10 * 60_000;
const PER_IP_LIMIT = 30;
const NEW_HOSTS_PER_IP_PER_DAY = 5;
const ATTEMPT_RETENTION_MS = 60 * 60_000;
export const NOTIFY_DAILY_CAP = 50;

type Row = Record<string, string | number | null>;

function toRegistration(row: Row): Registration {
  return {
    host: String(row.host),
    registered_at: String(row.registered_at),
    verified_at: String(row.verified_at),
    last_attempt_at: String(row.last_attempt_at),
    revoked: row.revoked === 1,
    signin_domains: JSON.parse(String(row.signin_domains)) as string[],
    notified: row.notified === 1,
  };
}

export class Registry extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const sql = ctx.storage.sql;
    sql.exec(`CREATE TABLE IF NOT EXISTS registrations (
      host TEXT PRIMARY KEY,
      registered_at TEXT NOT NULL DEFAULT '',
      verified_at TEXT NOT NULL DEFAULT '',
      last_attempt_at TEXT NOT NULL DEFAULT '',
      revoked INTEGER NOT NULL DEFAULT 0,
      signin_domains TEXT NOT NULL DEFAULT '[]',
      notified INTEGER NOT NULL DEFAULT 0
    )`);
    sql.exec(`CREATE TABLE IF NOT EXISTS attempts (
      host TEXT NOT NULL,
      ip TEXT NOT NULL,
      at_ms INTEGER NOT NULL,
      failed INTEGER NOT NULL DEFAULT 0
    )`);
    sql.exec("CREATE INDEX IF NOT EXISTS attempts_ip ON attempts (ip, at_ms)");
    sql.exec(`CREATE TABLE IF NOT EXISTS new_hosts (
      day TEXT NOT NULL,
      ip TEXT NOT NULL,
      count INTEGER NOT NULL,
      PRIMARY KEY (day, ip)
    )`);
    sql.exec("CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
  }

  private rows(query: string, ...bindings: (string | number)[]): Row[] {
    return this.ctx.storage.sql.exec(query, ...bindings).toArray() as Row[];
  }

  private counter(key: string): string {
    const rows = this.rows("SELECT value FROM counters WHERE key = ?", key);
    return rows.length === 0 ? "" : String(rows[0]!.value);
  }

  private setCounter(key: string, value: string | number): void {
    this.ctx.storage.sql.exec(
      "INSERT INTO counters (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      key,
      String(value),
    );
  }

  private prune(nowMs: number): void {
    this.ctx.storage.sql.exec("DELETE FROM attempts WHERE at_ms < ?", nowMs - ATTEMPT_RETENTION_MS);
  }

  getRegistration(host: string): Registration | null {
    const rows = this.rows("SELECT * FROM registrations WHERE host = ?", host);
    return rows.length === 0 ? null : toRegistration(rows[0]!);
  }

  /**
   * Records one register request and answers whether it may proceed: a failed attempt for the same
   * host from the same address within 60 s, more than 30 requests from one address in 10 minutes,
   * or a sixth new host from one address today are refused. Nothing here creates a registration.
   */
  noteAttempt(host: string, ip: string, nowMs: number, nowIso: string): AttemptVerdict {
    this.prune(nowMs);
    const recentFailure = this.rows(
      "SELECT 1 FROM attempts WHERE host = ? AND ip = ? AND failed = 1 AND at_ms > ?",
      host,
      ip,
      nowMs - PER_HOST_IP_WINDOW_MS,
    );
    if (recentFailure.length > 0) return "too_soon";
    const perIp = this.rows(
      "SELECT COUNT(*) AS n FROM attempts WHERE ip = ? AND failed = 0 AND at_ms > ?",
      ip,
      nowMs - PER_IP_WINDOW_MS,
    );
    if (Number(perIp[0]!.n) >= PER_IP_LIMIT) return "too_soon";
    if (this.getRegistration(host) === null) {
      const day = nowIso.slice(0, 10);
      const today = this.rows("SELECT count FROM new_hosts WHERE day = ? AND ip = ?", day, ip);
      if (today.length > 0 && Number(today[0]!.count) >= NEW_HOSTS_PER_IP_PER_DAY) return "too_many_new_hosts";
    }
    this.ctx.storage.sql.exec("INSERT INTO attempts (host, ip, at_ms, failed) VALUES (?, ?, ?, 0)", host, ip, nowMs);
    this.ctx.storage.sql.exec("UPDATE registrations SET last_attempt_at = ? WHERE host = ?", nowIso, host);
    return "ok";
  }

  /** A proof that did not check out. Only repeated failures from the same address are throttled. */
  noteFailure(host: string, ip: string, nowMs: number): void {
    this.prune(nowMs);
    this.ctx.storage.sql.exec("INSERT INTO attempts (host, ip, at_ms, failed) VALUES (?, ?, ?, 1)", host, ip, nowMs);
  }

  /** The successful proof: the only way a row is created. `domains` are the DNS-verified sign-in domains. */
  upsertVerified(host: string, nowIso: string, ip = "", domains: string[] = []): UpsertResult {
    const existing = this.getRegistration(host);
    const sorted = [...domains].sort();
    const json = JSON.stringify(sorted);
    if (existing === null) {
      this.ctx.storage.sql.exec(
        `INSERT INTO registrations (host, registered_at, verified_at, last_attempt_at, signin_domains)
         VALUES (?, ?, ?, ?, ?)`,
        host,
        nowIso,
        nowIso,
        nowIso,
        json,
      );
      if (ip !== "") {
        const day = nowIso.slice(0, 10);
        this.ctx.storage.sql.exec(
          `INSERT INTO new_hosts (day, ip, count) VALUES (?, ?, 1)
           ON CONFLICT(day, ip) DO UPDATE SET count = count + 1`,
          day,
          ip,
        );
        this.ctx.storage.sql.exec("DELETE FROM new_hosts WHERE day < ?", day);
      }
      return { isNew: true, domainsChanged: sorted.length > 0, registered_at: nowIso, verified_at: nowIso };
    }
    const registeredAt = existing.registered_at === "" ? nowIso : existing.registered_at;
    this.ctx.storage.sql.exec(
      `UPDATE registrations SET registered_at = ?, verified_at = ?, last_attempt_at = ?, signin_domains = ?
       WHERE host = ?`,
      registeredAt,
      nowIso,
      nowIso,
      json,
      host,
    );
    const changed = JSON.stringify([...existing.signin_domains].sort()) !== json;
    return { isNew: false, domainsChanged: changed, registered_at: registeredAt, verified_at: nowIso };
  }

  /** Revokes a host, inserting a revoked row when the host is absent. */
  revoke(host: string): void {
    this.ctx.storage.sql.exec(
      `INSERT INTO registrations (host, revoked) VALUES (?, 1) ON CONFLICT(host) DO UPDATE SET revoked = 1`,
      host,
    );
  }

  /** Lifts a revocation and clears `verified_at`: the host must prove control again (D17). */
  restore(host: string): void {
    this.ctx.storage.sql.exec("UPDATE registrations SET revoked = 0, verified_at = '' WHERE host = ?", host);
  }

  /** Non-revoked, verified hosts, sorted by host. */
  listActive(): Registration[] {
    return this.rows("SELECT * FROM registrations WHERE revoked = 0 AND verified_at != '' ORDER BY host").map(
      toRegistration,
    );
  }

  /** Whether this registration may forward: not revoked, verified within `maxAgeMs` of `nowMs`. */
  isActive(host: string, nowMs: number, maxAgeMs: number): Registration | null {
    const registration = this.getRegistration(host);
    if (registration === null || registration.revoked || registration.verified_at === "") return null;
    const verifiedMs = Date.parse(registration.verified_at);
    if (Number.isNaN(verifiedMs) || nowMs - verifiedMs > maxAgeMs) return null;
    return registration;
  }

  /** Decides whether a notification email may go out today (SEC-23's hard cap of 50 a UTC day). */
  reserveNotification(day: string): NotifyDecision {
    const stored = this.counter("notify_day");
    if (stored !== day) {
      this.setCounter("notify_day", day);
      this.setCounter("notify_day_count", 0);
    }
    const count = Number(this.counter("notify_day_count"));
    if (count < NOTIFY_DAILY_CAP) {
      this.setCounter("notify_day_count", count + 1);
      return "send";
    }
    this.setCounter("notify_day_count", count + 1);
    return count === NOTIFY_DAILY_CAP ? "limit" : "none";
  }

  markNotified(host: string): void {
    this.ctx.storage.sql.exec("UPDATE registrations SET notified = 1 WHERE host = ?", host);
  }

  recordNotifySkipped(): void {
    this.setCounter("notify_skipped", Number(this.counter("notify_skipped")) + 1);
  }

  /** `code` is a short code, never a secret or a response body. */
  recordNotifyFailure(code: string, nowIso: string): void {
    this.setCounter("notify_failures", Number(this.counter("notify_failures")) + 1);
    this.setCounter("notify_last_error", code);
    this.setCounter("last_notify_failure_at", nowIso);
  }

  notifyStats(): NotifyStats {
    return {
      notify_failures: Number(this.counter("notify_failures")),
      notify_last_error: this.counter("notify_last_error"),
      notify_skipped: Number(this.counter("notify_skipped")),
      last_notify_failure_at: this.counter("last_notify_failure_at"),
    };
  }

  countActive(): number {
    return Number(this.rows("SELECT COUNT(*) AS n FROM registrations WHERE revoked = 0 AND verified_at != ''")[0]!.n);
  }

  attemptRowCount(): number {
    return Number(this.rows("SELECT COUNT(*) AS n FROM attempts")[0]!.n);
  }
}
