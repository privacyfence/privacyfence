# ADR 0106: Gemini CLI, Antigravity and the Gemini app are not supported AI clients; Gemini support means Gemini Enterprise

## Status

Accepted — 2026-09-29. Implemented: the Gemini CLI contract test and canary entry are removed.
Closes [issue 392](https://github.com/privacyfence/privacyfence/issues/392) and
[issue 394](https://github.com/privacyfence/privacyfence/issues/394) as not planned. Gemini
Enterprise stays open as [issue 393](https://github.com/privacyfence/privacyfence/issues/393).

## Context

Three Google products could connect to PrivacyFence's `/mcp`: Gemini CLI (issue 392), Gemini
Enterprise (issue 393) and the Gemini app's custom apps, "Spark" (issue 394). Work on Gemini CLI
had started: a T3 contract test and a weekly canary entry
([PR 790](https://github.com/privacyfence/privacyfence/pull/790)), a seeded replay fixture
([PR 791](https://github.com/privacyfence/privacyfence/pull/791)), and a draft setup page
([PR 792](https://github.com/privacyfence/privacyfence/pull/792)).

The maintainer's manual check could not sign in to Gemini CLI. The reason, from Gemini CLI's own
announcement ([google-gemini/gemini-cli discussion 28017](https://github.com/google-gemini/gemini-cli/discussions/28017)):

- Since 2026-06-18 Gemini CLI no longer serves free-tier, Google AI Pro or Google AI Ultra
  individual accounts; "Login with Google" fails for them. Users with an API key or an enterprise
  Gemini Code Assist licence are unaffected, and the npm package is still published.
- Google's replacement is Antigravity: the `agy` CLI and the Antigravity 2.0 desktop app. Its
  installer copies Gemini CLI's MCP servers over, but it configures a remote server with
  `serverUrl`, not Gemini CLI's `httpUrl`.
- Antigravity sends its first request to an OAuth-protected HTTP MCP server without the bearer
  token, so the server answers 401
  ([google-antigravity/antigravity-cli issue 25](https://github.com/google-antigravity/antigravity-cli/issues/25),
  open since 2026-05-20). Organization mode authenticates every client with OAuth, so Antigravity
  cannot use it until that is fixed.

PrivacyFence targets organizations. Individual Gemini users are not an audience it plans for. An
organization that standardises on Gemini instead of Claude or ChatGPT gives its people Gemini
Enterprise, not a developer CLI.

## Decision

1. **Gemini CLI, Antigravity (the `agy` CLI and the desktop app) and the Gemini app's Spark are
   not supported AI clients.** None gets a `connect-<slug>.md` setup page, a website page, a row
   in `ai-client-qa.md`'s results, or a T3 contract test or canary entry.
2. **The registry keeps recognising Gemini CLI.** `agent_identity.REGISTRY`'s `gemini-cli` entry,
   its icon, and the seeded replay fixture `tests/fixtures/ai_clients/gemini-cli/` stay, so an
   organization member who connects Gemini CLI anyway is named correctly on the card and in the
   Audit Log. Recognising a client is not supporting it.
3. **"Gemini support" means Gemini Enterprise.** Until it is verified, "Gemini" stays on the
   website guardrail's not-yet-supported list (`tests/unit/test_website_clients.py`), unchanged.

## Alternatives considered

- **Support Gemini CLI for API-key and Code Assist-licensed users.** It still works for them, and
  the contract test already passed. Rejected: Google now treats it as the legacy tool, and its
  successor is Antigravity. A public "works with Gemini CLI" claim, a setup page and a pinned
  weekly test would all be spent on a client its vendor is winding down, for users who are not
  PrivacyFence's audience.
- **Support Antigravity instead.** Rejected for now. Organization mode can't work until its OAuth
  bug is fixed. It is also not on npm, so the pinned, lockfile-installed T3 test
  ([ADR 0085](0085-ci-installs-ai-client-clis-from-a-committed-lockfile.md)) doesn't fit it, and
  it needs a Google sign-in, which T3 tests must never need.
- **Research Spark before deciding.** Rejected: search results say Spark's custom apps are for
  personal Google accounts only. That rules it out whatever else the research found.
- **Remove the `gemini-cli` registry entry as well.** Rejected: it costs nothing, and dropping it
  would turn a recognisable client into `unknown:gemini-cli-mcp-client` in the Audit Log.

## Consequences

- Nothing on the website or in the docs names Gemini CLI, Antigravity or Spark as supported. The
  finer not-yet list that listing Gemini CLI would have needed isn't built.
- CI no longer installs `@google/gemini-cli`, and the canary no longer tracks it. A Gemini CLI
  release that breaks `/mcp` goes unnoticed. That risk is accepted.
- An organization that asks for Antigravity is a new decision: a new ADR superseding this one,
  once Antigravity's OAuth bug is fixed.

## Verification

- `tests/integration/ai_clients/package.json` pins no `@google/gemini-cli`, and
  `.github/workflows/ai-client-canary.yml` has no `gemini-cli` matrix entry.
- `docs/` has no `connect-gemini-cli.md`, and `website/_data/clients.json` lists no Gemini client.
- `tests/unit/test_website_clients.py`'s not-yet-supported list still contains `"Gemini"`.
- `tests/unit/web/test_ai_client_replay.py` still replays `tests/fixtures/ai_clients/gemini-cli/`.

## Related

- Issues [392](https://github.com/privacyfence/privacyfence/issues/392),
  [393](https://github.com/privacyfence/privacyfence/issues/393),
  [394](https://github.com/privacyfence/privacyfence/issues/394).
- PRs [790](https://github.com/privacyfence/privacyfence/pull/790) (reverted here),
  [791](https://github.com/privacyfence/privacyfence/pull/791) (kept),
  [792](https://github.com/privacyfence/privacyfence/pull/792) (closed unmerged).
- [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md),
  [ADR 0094](0094-claude-clients-are-matched-by-their-observed-names.md): the registry.
- [ADR 0100](0100-unverified-client-setup-instructions-never-merge-to-main.md): why PR 792 never
  merged.
