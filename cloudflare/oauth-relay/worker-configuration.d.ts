// Ambient binding types for this Worker, matching wrangler.toml exactly. Committed by hand: the
// bindings are fixed and documented in docs/downloads-and-release-kpi.md's "Cloudflare resources"
// table; regenerate with `npx wrangler types` instead if a binding is ever added or renamed.

interface Env {
  REGISTRY: DurableObjectNamespace<import("./src/registry").Registry>;
  // Worker secret: the maintainer token for /registrations, /admin/revoke and /admin/restore.
  REVOKE_TOKEN: string;
  // [vars] in wrangler.toml.
  NOTIFY_FROM: string;
  SIGNIN_REQUIRE_HD: string;
  // Worker secrets for the Gmail notification (src/notify.ts); any of them empty disables sending.
  GMAIL_CLIENT_ID: string;
  GMAIL_CLIENT_SECRET: string;
  GMAIL_REFRESH_TOKEN: string;
}

// `cloudflare:test`'s `env` export and the `DurableObject` base class default their `Env` type
// parameter to this -- see @cloudflare/workers-types' `declare namespace Cloudflare`.
declare namespace Cloudflare {
  interface Env extends globalThis.Env {}
}
