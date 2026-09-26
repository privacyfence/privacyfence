# Plan: ChatGPT and Gemini support, and an "AI agents" section on privacyfence.eu

> **Temporary plan document** (see [`adr/README.md`](adr/README.md)): delete it in the PR that
> finishes the last wave. Before deleting it, move every decision marked **→ ADR** below into an ADR.
> It is listed in `scripts/build_site.py`'s `CONTRIBUTOR_DOCS` and in the contributor half of
> [`README.md`](README.md) so the docs allowlist (guardrail 5) accepts it; the retiring PR removes
> both entries.

Covers these issues:

- [issue 390](https://github.com/privacyfence/privacyfence/issues/390): ChatGPT desktop, local mode
- [issue 391](https://github.com/privacyfence/privacyfence/issues/391): ChatGPT, org mode
- [issue 392](https://github.com/privacyfence/privacyfence/issues/392): Gemini CLI, both modes
- [issue 393](https://github.com/privacyfence/privacyfence/issues/393): Gemini Enterprise, needs code
- [issue 394](https://github.com/privacyfence/privacyfence/issues/394): Gemini app / Spark, research

It also covers the website's new **AI agents** menu (§5b). This plan writes the issues as
`I390`–`I394` from here on. Docs and test code must never use the `#NNN` form: see the
[§1 corrections](#1-corrections-what-the-issues-say-vs-what-main-does-today), item 10.

**The goal of the order below:** say "works with ChatGPT and Gemini" publicly after **Gate A**,
which needs no server-side code. Everything after Gate A widens that claim. The AI agents menu for
the clients that already work (Claude Desktop, Claude Code, claude.ai) ships in Wave 1. It does not
wait for any gate, and each gate then adds a page to it.

*Aligned to `main` at `ab07ab0d` (4.7.0) on 2026-09-26.*

---

## 0. How to use this plan

There are two kinds of step, and every step is labelled as one or the other:

- **🤖 WAVE n** means Claude sessions do the work. Each wave contains **work packages** (WP n.m).
  Packages in the same wave don't depend on each other. Each one gets its own session and its own
  PR, and they run at the same time.
- **🧑 M n.m** means **you** do it by hand, because it needs an account, a real app, or a
  decision. §7 has step-by-step instructions for each one.

This plan deliberately does **not** use `/implement`'s `## Implementation manifest`. `/implement`
builds one feature branch and opens one PR at the end. This plan's gates are human checks against
released builds, spread over several releases, so each package merges on its own (decision D10).

### Running a wave

Open a new Claude Code on the web session on `privacyfence/privacyfence` and paste:

```text
Implement WAVE <n> of docs/ai-client-support-plan.md (on main; if it isn't merged yet, read it from
branch claude/bold-fermat-xlsdy4).

You are the coordinator. Do not implement anything yourself. For each work package in that wave:
1. Create one child session with create_session. Give it the package's "Session prompt" word for
   word, plus the plan's §1 "Corrections" and §2 "Testing strategy" sections as context.
2. Tag every child session "ai-client-support:wave-<n>".
3. Track each child until its PR is open and CI is green. Use get_session, and
   check back with send_later about once an hour. Don't poll in a tight loop.
4. When every package's PR is green, post a checklist here: WP, PR link, CI state, and any
   manual (🧑) task that is now unblocked.
Only start a package whose "Depends on" items are all done (merged PRs, or 🧑 tasks I've reported
as done in this chat).
```

If a wave has only one package, you can skip the coordinator and paste that package's session
prompt straight into a new session.

Every child session follows these rules. They already reach each session through `CLAUDE.md` and
the `steward` skill, and are repeated here so you can check:

- Work on a new branch and open **one PR per package**. Put the `<type>` (`feature`, `fix`,
  `chore`, `tests`) in the PR title, because the `claude/*` branch name can't carry it.
- The PR is done when `/dod` (the §2.7 definition of done) passes and CI is green.
- User-visible changes get a line under `CHANGELOG.md`'s `## [Unreleased]`. Never add a version
  heading.
- Anything that needs a Mac, Windows or real credentials is **dispatched** as a workflow (see the
  `steward` skill's table). It is never skipped. A workflow can be dispatched only once it is on
  `main`.
- **ADR numbers:** take the next free number *at merge time*. `feature/org-mode-mobile` already
  claims 0078–0080, so the first ADR from this plan is most likely 0081. Renumber on conflict.

---

## 1. Corrections: what the issues say vs. what `main` does today

The issues were written 2026-09-14. These points were re-checked against `main` at `ab07ab0d` on
2026-09-26, and they change the work.

1. **Local-mode token.** The issues say `cat ~/.privacyfence/mcp_token`. Since
   [ADR 0008](adr/0008-one-principal-per-os-user.md), packaged installs are privilege-separated.
   The token comes from `--print-mcp-token`, and the URL is in the handoff directory's `mcp_url`
   file. The binary name differs per platform:
   - macOS: `/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp` (not
     `privacyfence-app`)
   - Linux: `privacyfence-app`
   - Windows: the installed exe. It has no console, so redirect its output to a file.

   Each platform's `install-*.md` → "Connect Claude Code" has the exact command.
2. **Where client setup docs live.** `getting-started.md` no longer has a "Connect Claude Code"
   section, and `TECHNICAL_REFERENCE.md` was deleted in the docs consolidation. Client setup is now
   in three places:
   - `install-macos.md`, `install-windows.md`, `install-linux.md` → "Connect Claude Desktop" /
     "Connect Claude Code"
   - `how-it-works.md` → "How an AI system connects"
   - `org-mode-setup-guide.md` §9 → "Add PrivacyFence to an AI client"

   Decision D1 gives these one home.
3. **Org-mode guide numbering.** "Point a client at `/mcp`" is now **§9 → "Add PrivacyFence to an
   AI client"**. The rest of the guide moved too:
   - §4 Identity provider (redirect URIs in its table)
   - §7 Reverse proxy and TLS (Caddy)
   - §8 Hardened systemd unit
   - **§11 AI systems** (admin pins)
   - §12 Approvals and step-up
   - §13 File delivery
4. **QA docs.** `manual-pre-release-test-plan.md` never existed, and `connector-qa-testing.md`
   was merged into [`connector-qa.md`](connector-qa.md). That page covers live *connector* QA, not
   clients. [`release-testing.md`](release-testing.md) → "What stays manual" admits only **one**
   real MCP client (Claude Desktop with the `.mcpb`). Adding ChatGPT/Gemini manual checks means
   amending that list on purpose (D2).
5. **The shim ships only inside `PrivacyFence.mcpb`.** No installer puts `shim.js` at a stable path
   on disk: the Windows installer copies the `.mcpb` into `{app}`, and the `.deb` ships no shim.
   Claude Desktop runs `.mcpb` servers with its own bundled Node. ChatGPT desktop would run `node`
   from a macOS GUI app's minimal `PATH`, where Homebrew's Node is not found. So I390 may need
   installer work (WP 3.1b).
6. **ChatGPT on the web can't reach a local-mode daemon.** Developer Mode connectors are called
   from OpenAI's cloud, and local mode listens on `127.0.0.1`. The fastest verified ChatGPT claim
   therefore goes through org mode (I391 Route A), on public HTTPS (🧑 M0.2).
7. **Agent names and attribution** (`src/privacyfence/agent_identity.py` `REGISTRY`,
   [ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)):
   - `gemini-cli-mcp-client` is already **verified** from Gemini CLI's source (ADR 0035). Only
     ChatGPT's `openai-mcp` is still a guess.
   - An unmatched name is not lost. It's recorded as `agent_id="unknown:<name>"`,
     `agent_source=client_info`, and shown as "Unrecognised AI system" plus the name. So the real
     name is readable from any audit entry.
   - **In org mode the DCR registration's `client_name` takes precedence over the handshake's
     `clientInfo.name`** (`web/routes_mcp.py`). The registry has to match the name each client
     *registers* with, as well as the name it sends in the handshake. T2 asserts both paths.
   - An admin **pin** on org **Settings → AI systems** (org guide §11) is the only attested source
     (`agent_source=oauth_client`). Every manual org test pins the new registration and records
     that the card shows it as verified.
   - Every `REGISTRY` entry needs an icon in `src/privacyfence/resources/agent_icons/` plus a
     license row in that directory's README (`test_agent_label.py`). `chatgpt.png` and
     `gemini-cli.png` exist. A new `gemini-enterprise` entry (WP 4.1) needs one.
8. **Traps for I393 in `web/oauth_provider.py`:**
   - The running daemon keeps `oauth_clients.json` **in memory** and rewrites the whole file on
     every `get_client` (`atomic_write_json`). A script that edits the file while the daemon runs
     is overwritten by the next `/authorize` or `/token`.
   - The daemon holds a `portalocker` single-instance lock (`daemon_main._acquire_instance_lock`).
     A command can use it to tell whether the daemon is running.
   - `_STALE_CLIENT_TTL_SECONDS` is 180 days, and `_prune_stale_clients_locked` runs on every
     `register_client`. A hand-registered client that nobody uses for 6 months is **pruned without
     warning**, which breaks the Gemini Enterprise connector. Its pin, if any, then shows under
     "Stale pins".
   - `_MAX_REGISTERED_CLIENTS` is 2000.
   - The secret is stored the way the SDK's `OAuthClientInformationFull` stores it, inside the
     model, in plaintext. A hand-registered client has to be stored the same way.
   - Records are `{"client", "last_used_at"}` (`_StoredClient`). Per
     [ADR 0041](adr/0041-only-the-current-install-layout-is-supported.md) there is **no older on-disk format to
     stay compatible with**. A new field only needs a default when it's absent.
9. **ADR 0076: every connector tool is advertised read-only.** `web/mcp_tools.py` gives every
   connector tool `readOnlyHint=true, destructiveHint=false, idempotentHint=true`, whatever it
   really does ([ADR 0076](adr/0076-every-connector-tool-is-advertised-read-only.md), a deliberate
   and temporary workaround tracked in
   [issue 46](https://github.com/privacyfence/privacyfence/issues/46)). For this plan that means:
   - ChatGPT's own write confirmation keys on non-read-only tools, so it probably **won't** appear.
     The earlier "double consent" expectation is likely wrong. Manual tests *record* whether any
     client-side confirmation appears instead of expecting one.
   - ChatGPT's "Scan Tools" and a workspace admin reviewing the connector will see every write
     labelled read-only. The ChatGPT docs must say this is deliberate, and that PrivacyFence's
     gate is the control (ADR 0076 → Consequences). See D5.
   - T1 can't just check that the hints are present. It asserts ADR 0076's invariant, written so it
     flips cleanly when issue 46 lands.
10. **No project history in docs or test code.**
    [ADR 0056](adr/0056-code-carries-no-project-history.md): `test_docs_no_history.py` rejects
    `#\d{3}` in every published and contributor doc, and `test_code_no_history.py` covers test
    files (`tests/fixtures/` is exempt). "Verification pending" notes therefore cite a **full issue
    URL** as a link, never `#392`. Any `docs/*.md` path named anywhere must exist
    (`test_docs_references_exist.py`), so a doc a WP names has to be created in that same WP. That is also why this plan writes
    docs that don't exist yet (`ai-client-qa.md`, `connecting-an-ai-client.md`) without the
    `docs/` prefix.
11. **Approvals.** In a privilege-separated local install, only a companion-attested browser
    session may approve
    ([ADR 0062](adr/0062-only-a-companion-attested-session-may-approve.md)). Local manual tests
    approve through the companion's **Open Approvals**, not a pasted URL. In org mode, `/approvals`
    after IdP sign-in still works.
12. **Moving files without the shim.** Direct HTTP clients (ChatGPT, Gemini) use
    [ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)'s capability URLs:
    `privacyfence_create_upload_slot` → `PUT /mcp-files/slots/<slot>`, and
    `/mcp-files/fetch/<token>`. Verification covers one upload and one download.
13. **Audit log location.** It's per principal: `users/<principal>/logs/audit/` (and
    `authority/logs/audit/`) under the data directory, not `logs/audit/`. Org data directory:
    `/var/lib/privacyfence-org/.privacyfence/`. Org service account: **`privacyfence-org`**.

---

## 2. Testing strategy

This builds on the four-tier design from the "AI agent protocol stability" session, which is
waiting on one decision (D2).

| Tier | What | Runs where | Catches | Built in |
|---|---|---|---|---|
| **T1** Static portability | A unit test over every advertised tool (connector and `privacyfence_*`). Checks: input schema root is `type: object`; no `$ref`/`$defs`/`definitions` and no tuple-form `items`; name matches `^[a-zA-Z0-9_-]{1,64}$` (OpenAI's limit); non-empty description with a length bound; every tool carries all three hints; and ADR 0076's uniform read-only triple on connector tools, in one clearly named assertion that issue 46 flips. | every PR (`tests.yml` → `test`) | A new tool that ChatGPT or Gemini would reject or rename | WP 1.1 |
| **T2** Recorded-handshake replay | Fixtures under `tests/fixtures/ai_clients/<client>/` hold each client's real DCR `/register` body, `clientInfo`, redirect URIs, `token_endpoint_auth_method` and request order. They are replayed through the in-process harness in `tests/unit/web/test_org_mcp_e2e.py`. Asserts the `agent_identity` result through the DCR `client_name` path **and** through the handshake path, unpinned (`client_info`) and pinned (`oauth_client`). | every PR | A server-side change that would break a client CI can't run (ChatGPT, Gemini Enterprise) | WP 1.1 (seeded), WP 2.1 (real captures) |
| **T3** Real client in CI | The pinned `@google/gemini-cli`, installed from a committed lockfile (D7), runs `gemini mcp list` against a real daemon. Local mode uses `httpUrl` plus a bearer header; org mode uses the mock IdP and DCR (WP 3.3). A separate **weekly canary** does the same with `@latest`. | PR (pinned), plus scheduled (`@latest`) | Gemini CLI changing on its side (drift) | WP 1.2, WP 3.3 |
| **T4** Manual pre-release check | One row per client in a new contributor doc `ai-client-qa.md`, plus a pointer from `release-testing.md` → "Human checks". Runs when `/mcp`, OAuth, `routes_mcp.py` or `agent_identity.py` change. | before a release, by a human | What the tiers above can't run (ChatGPT, Gemini Enterprise) | WP 1.3 (template), 🧑 M1/M3/M4 fill it |

The rule that ties the tiers together: **each manual verification (T4) ends by saving a T2 fixture**,
so a client verified once by hand stays covered by a test on every PR.

---

## 3. Decisions to confirm before Wave 1 (🧑 M0.1)

Answer these with "as recommended" or your changes. Everything below assumes the recommendation.

| # | Decision | Recommendation |
|---|---|---|
| D1 | One home for per-client setup instructions | A **new published doc, `connecting-an-ai-client.md`** ("Connecting an AI client"), the counterpart of `connecting-a-service.md`. It gets one `##` per client, with "Local mode" and "Organization mode" subsections and a per-platform table for the token command. List it in `docs/README.md` → "Install and first steps". The `install-*.md` "Connect …" sections keep their one platform-specific command and link to it. `how-it-works.md` and org guide §9 link to it instead of repeating steps. Every AI-agents page's "Set it up" button (§5b) points at its section. WP 1.5 creates it with the three Claude clients. |
| D2 | Where the AI-client checklist and evidence log go | A new **contributor** doc, `ai-client-qa.md`, shaped like `connector-qa.md`'s "Recording results". Registered in `CONTRIBUTOR_DOCS` and `docs/README.md`'s contributor half. One bullet from `release-testing.md` → "Human checks". Amend "What stays manual" from "one real MCP client" to "one real client per supported client family, run only when the client-facing surface changed". |
| D3 | Public claim wording before T4 has run against a release | "Works with ChatGPT (Developer Mode, with an organization deployment) and Gemini CLI". The site shows no dates or versions (they go stale); the evidence table in `ai-client-qa.md` holds them. ChatGPT desktop and Gemini Enterprise are listed only after their own gates. |
| D4 | How I393 writes a client while the daemon runs → **ADR** | A `privacyfence-app --register-oauth-client` subcommand that **refuses to run while the daemon holds its lock**. It writes through a new `OrgOAuthProvider.register_static_client()`, and the record carries `"source": "admin"` so it's never pruned as stale. It gets a `gemini-enterprise` registry entry with an icon. Later, maybe: the same action on org **Settings → AI systems** with step-up (ADR 0034). |
| D5 | ChatGPT and ADR 0076's uniform read-only annotations | Keep ADR 0076 as it is. Don't give ChatGPT truthful per-client annotations. The ChatGPT sections of the docs and of `/ai-agents/chatgpt/` say plainly that ChatGPT will list every PrivacyFence tool as read-only, that this is deliberate, and that PrivacyFence's approval card is the confirmation. M1.3 and M3.1 record ChatGPT's actual behaviour. If a ChatGPT workspace setting turns out to treat `readOnlyHint` as a *permission* (for example, "members may use read-only actions only"), that is a trust-boundary problem → stop and write a superseding ADR before Gate B. |
| D6 | Do unverified draft instructions land on `main`? | **No.** `/docs/` is built from the latest stable tag, so anything merged ships in the next release's docs. WP 1.3 merges only the contributor-side QA checklist. The ChatGPT/Gemini user-doc sections are written in WP 1.3 as a **draft PR** that WP 2.1 finishes and merges once M1 passes. The 🧑 M1 steps below are self-contained, so they don't need the drafts. |
| D7 | How CI gets Gemini CLI | A committed `tests/integration/ai_clients/package.json` + `package-lock.json` installed with `npm ci` (integrity-checked, the same posture as the SHA-pinned actions and `--require-hashes` pip). Not `npx --yes @google/gemini-cli@<v>`. The weekly canary alone uses `npx …@latest`, because drift is its job. |
| D8 | Website: menu name, URL, and which pages | Top-nav **"AI agents"** → `/ai-agents/`, one page per `website/_data/clients.json` entry at `/ai-agents/<slug>/`. **Now:** `claude-desktop`, `claude-code`, `claude-ai`. **Later:** each gate adds its clients' pages. The data file stays `clients.json` (it's already the one list). See §5b. |
| D9 | Website timing at each gate | A client's `clients.json` entry and `/ai-agents/<slug>/` page land **after the stable release whose docs carry its setup section**. Otherwise the page's "Set it up" link points into a `/docs/` page (built from the latest stable tag) that doesn't have that section yet, and the site's link check (guardrail 6) fails. So each gate is: docs/code PR → release → website PR. |
| D10 | Execution model | Keep one PR per package with the coordinator prompt in §0. Don't convert to `/implement`, which produces one PR at the end and can't span the releases the gates need. |
| D11 | Logos on the AI-agents pages | **No third-party logos** on the website: text only, like the connector pages. That avoids trademark-use questions. The product's attribution icons stay in the app. |
| D12 | Land this plan on `main` first | Yes, as a docs-only PR (this branch), before Wave 1. Then every session reads it from `main`. |

---

## 4. Roadmap at a glance

```text
Low effort ─────────────────────────────────────────────────────────────▶ High effort

🧑 M0  decisions + accounts + org deployment on public HTTPS
│
🤖 WAVE 1   WP1.1 T1+T2 tests │ WP1.2 Gemini CLI in CI │ WP1.3 QA checklist (+ draft docs PR)
│           WP1.4 re-source vendor docs │ WP1.5 AI agents menu + "Connecting an AI client" doc
│
🧑 M1  Gemini CLI local → Gemini CLI org → ChatGPT Dev Mode → ChatGPT desktop check  (~2 h)
│
🤖 WAVE 2   WP2.1 evidence → fixtures, registry names, finish + merge the user docs, CHANGELOG
│
🧑 M2  cut a stable release carrying WP2.1
│
🤖          WP2.2 website: clients.json + /ai-agents/chatgpt/ + /ai-agents/gemini-cli/
│
★ GATE A — public claim: "Works with ChatGPT and Gemini"      (I392 done, I391 Route A done)
│
🤖 WAVE 3   WP3.1 ChatGPT desktop (a/b/c from M1.4) │ WP3.2 ChatGPT workspace-admin guide │ WP3.3 Gemini CLI org-mode OAuth in CI
│
🧑 M3  ChatGPT workspace-admin publish (Business) + ChatGPT desktop retest, then release
│
★ GATE B — website PR: ChatGPT workspace connector, possibly ChatGPT desktop   (I391, I390 done)
│
🧑 M4.1 confirm Google's Gemini Enterprise requirements
🤖 WAVE 4   WP4.1 ADR + admin OAuth-client registration + tests + docs          (I393 code)
🧑 M4.2 register the client, configure Gemini Enterprise, verify, then release
│
★ GATE C — website PR: /ai-agents/gemini-enterprise/                  (I393 done)
│
🤖 WAVE 5   WP5.1 Spark research   → 🧑 M5 try it or close        (I394)
│
🤖 WAVE 6   WP6.1 retire this plan: extract ADRs, delete this file
```

| Order | Issue | Effort | Server code? | Unlocks |
|---|---|---|---|---|
| 0 | website: AI agents menu | one session | no | per-agent setup pages for today's clients |
| 1 | I392 Gemini CLI local | minutes | no | first Gemini claim |
| 2 | I392 Gemini CLI org | ~1 h (with an org deployment) | no | Gemini org claim |
| 3 | I391 Route A (ChatGPT Developer Mode) | ~1 h | no | **first ChatGPT claim** |
| 4 | I390 ChatGPT desktop | none, or installer work | maybe (packaging) | ChatGPT local mode |
| 5 | I391 Route B (workspace publish) | docs + Business workspace | no | ChatGPT for a whole org |
| 6 | I393 Gemini Enterprise | real feature + ADR | **yes** | Gemini for a whole org |
| 7 | I394 Spark | research | probably not | consumer Gemini (low priority) |

---

## 5. Waves (🤖 Claude)

### 🤖 WAVE 1: test scaffolding, QA checklist, AI agents menu (5 parallel sessions)

Depends on: 🧑 M0.1 (decisions). WP 1.4 has no dependencies.

#### WP 1.1: T1 portability lint and T2 replay harness · I390–I393 · `tests:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §1–§2. Implement WP 1.1, tests only, with no production code
change unless a test finds a real bug (if it does, stop and report it; don't fix it here).
Cite issues only as full URLs in test code (ADR 0056, test_code_no_history.py).
1. T1 — tests/unit/web/test_tool_schema_portability.py: over every tool McpDispatcher lists
   (build every connector the way test_mcp_tools.py does), plus the privacyfence_* meta-tools,
   assert: inputSchema root type=="object"; no "$ref"/"$defs"/"definitions"; no tuple-form
   "items"; name matches ^[a-zA-Z0-9_-]{1,64}$; description non-empty and ≤ 1024 chars; all
   three annotation hints present. Add one separately named test asserting ADR 0076's uniform
   triple (readOnly=True, destructive=False, idempotent=True) on connector tools, with a
   docstring saying it flips when https://github.com/privacyfence/privacyfence/issues/46 lands;
   don't duplicate test_mcp_tools.py's existing ADR 0076 test, reference it. Parametrize by tool
   name so a failure names the tool.
2. T2 — tests/fixtures/ai_clients/{chatgpt,gemini-cli}/register.json + initialize.json, and a
   README.md per client saying the fixture is SEEDED FROM VENDOR DOCS, NOT YET CAPTURED (WP 2.1
   replaces it). ChatGPT redirect_uri: https://chatgpt.com/connector_platform_oauth_redirect.
   Gemini CLI: http://localhost:7777/oauth/callback. Gemini CLI's clientInfo name is
   gemini-cli-mcp-client (verified, ADR 0035). Take the other fields from each vendor's public
   docs and cite them in the README.
   Factor test_org_mcp_e2e.py's private helpers (_register_client, _authorize_and_get_code,
   _exchange_for_tokens, _mcp_session) into tests/unit/web/conftest.py fixtures/helpers, and use
   them from both files. tests/unit/web/test_ai_client_replay.py, per fixture: POST /register with
   the recorded body → /authorize → faked IdP leg → /token → initialize with the recorded
   clientInfo → tools/list → one read tools/call. Assert success and the attribution: unpinned
   (agent_source client_info, the registry entry reached via the DCR client_name, which takes
   precedence over clientInfo in org mode) and pinned via the AI-systems pin store (agent_source
   oauth_client).
Run the /dod skill; open one PR titled "tests: AI-client schema portability (T1) and handshake
replay (T2)"; drive it to green.
```

**Done when:** both test files pass in CI, and the coverage floor holds.

#### WP 1.2: T3 Gemini CLI local-mode contract test in CI · I392 · `tests:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §1–§3 (decision D7). Implement WP 1.2:
1. tests/integration/ai_clients/package.json + package-lock.json pinning @google/gemini-cli
   (exact version); tests install it with `npm ci --prefix tests/integration/ai_clients`.
2. tests/integration/test_gemini_cli_contract.py, modelled on test_shim_mcp_contract.py and
   test_mcp_daemon_contract.py: start a real local-mode daemon on a free port, write a throwaway
   HOME with .gemini/settings.json containing
   {"mcpServers":{"privacyfence":{"httpUrl":"<url>","headers":{"Authorization":"Bearer <token>"}}}},
   run the installed `gemini mcp list` with that HOME, and assert privacyfence shows as connected
   with a non-zero tool count. It needs no Gemini/Google credential — if the pinned version needs
   one even for `mcp list`, stop and report that rather than adding a secret. Skip when node/npm
   is absent (same posture as the shim contract test). An env var GEMINI_CLI_BIN overrides the
   binary (the canary uses it). Module docstring: how to bump the pin.
3. Run it in tests.yml's existing `test` job (it already sets up Node 22), not a new job, unless
   the runtime cost is > 60 s — then a separate job.
4. .github/workflows/ai-client-canary.yml: weekly schedule + workflow_dispatch, runs the same test
   with `npx --yes @google/gemini-cli@latest` as GEMINI_CLI_BIN; on failure, open (or update) one
   issue titled "AI-client canary: Gemini CLI @latest broke /mcp". Pin all actions by SHA like the
   other workflows; add it to the testing-policy.md layer table.
Run /dod; one PR "tests: Gemini CLI contract test (T3) and weekly @latest canary"; drive to green.
```

**Done when:** the pinned test passes in the PR's CI. The canary can't be dispatched until it's on
`main` (see the steward skill), so dispatch it once after merge. 🧑 Merging the PR is your step.

#### WP 1.3: T4 checklist, plus the draft client docs as a draft PR · I390–I392 · `chore:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §1–§3 (D1, D2, D5, D6). Implement WP 1.3 as TWO PRs:

PR A (merge it) "chore: AI-client QA checklist":
1. New contributor doc ai-client-qa.md, in the shape of connector-qa.md's "Recording
   results": prerequisites, a per-client script (tools/list, one read, one gated write → approval
   card, whether the client showed its own confirmation (record, don't expect — ADR 0076), one
   capability-URL upload and one download per ADR 0028, the audit entry's agent_* fields, and in
   org mode: pin the registration on Settings → AI systems and confirm the card shows it verified),
   and an evidence table: Client | Version | OS | Mode | Date | Result | Registered client_name |
   clientInfo name | Client-side confirmation? | Fixture captured.
2. Register it: build_site.CONTRIBUTOR_DOCS, docs/README.md's contributor half.
3. release-testing.md: one bullet under "Human checks" → "Every platform" pointing at it, and amend
   "What stays manual" per D2.

PR B (open as DRAFT, do not merge; WP 2.1 finishes it) "feature: ChatGPT and Gemini CLI setup":
the ChatGPT and Gemini CLI sections of connecting-an-ai-client.md (created by WP 1.5 — base
this branch on WP 1.5's branch, or on main once it merged):
- Gemini CLI, local: settings.json httpUrl + headers, and the `gemini mcp add --transport http
  privacyfence <mcp_url> --header "Authorization: Bearer $(<platform binary> --print-mcp-token)"`
  one-liner, per platform; Gemini CLI does not use the .mcpb shim.
- Gemini CLI, org: httpUrl with no headers, `/mcp auth`.
- ChatGPT: web ChatGPT reaches only an organization deployment; Developer Mode steps (Settings →
  Apps → Advanced settings → Developer mode; Create; URL; OAuth); re-enable per chat; ChatGPT lists
  every PrivacyFence tool as read-only on purpose (ADR 0076) and PrivacyFence's card is the
  confirmation. ChatGPT desktop: pending.
- org-mode-setup-guide.md §9 "Add PrivacyFence to an AI client": one bullet each, linking the doc.
Each section carries a "Verification pending" note linking the full issue URL (never #39x).
No CHANGELOG line, no website change.
Run /dod on both; drive PR A to green.
```

#### WP 1.4: re-source the vendor claims · I390, I391, I393, I394 · research, no PR

**Session prompt:**

```text
Read docs/ai-client-support-plan.md. WP 1.4 is research only — no commits. Using WebFetch/
WebSearch, try to confirm from the vendor's OWN pages (not aggregators), and record the URL +
date for each:
(a) ChatGPT desktop MCP support: does it exist, config file path, stdio vs url, custom headers?
(b) ChatGPT Developer Mode menu path and OAuth redirect URI; the client_name ChatGPT registers
    with at /register; how ChatGPT treats readOnlyHint (confirmation prompts, and any workspace
    admin setting that permits only read-only actions — decision D5); workspace-admin publish flow
    and the "frozen snapshot / re-publish" behaviour (help.openai.com 12584461 and 11509118).
(c) Gemini Enterprise custom MCP server connector: DCR or not, fixed redirect URI, required fields
    (authorization URL, token URL, scopes, client auth method), console menu path.
(d) Gemini app "Spark" custom apps: DCR or pre-registration, TLS requirements.
Post one comment per issue (390, 391, 393, 394) with findings, clearly separating "confirmed on
vendor page <url>" from "could not reach". If egress blocks a page, say so and list it for the
human. End with a short summary in chat of anything that changes this plan — in particular any
answer to (b)'s readOnlyHint question that triggers D5's stop condition.
```

**Done when:** each of the four issues has a findings comment. Any page it couldn't reach becomes
part of 🧑 M1.4 or 🧑 M4.1.

#### WP 1.5: the AI agents menu and "Connecting an AI client" · website + docs · `feature:`

Depends on: M0.1 only (D1, D8, D11). This package doesn't touch ChatGPT or Gemini, so it ships
straight away. It's the frame that each gate adds a page to. See §5b for the design.

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §1, §3 (D1, D8, D9, D11) and §5b. Implement WP 1.5 for the
three clients website/_data/clients.json already lists (Claude Desktop, Claude Code, claude.ai).
Do not name ChatGPT, Gemini, Copilot or Cursor anywhere on the website (guardrail 10).

Docs (D1):
1. New published doc connecting-an-ai-client.md: intro (local vs organization, which clients
   can use which), then one "##" per client with "Local mode" / "Organization mode" subsections
   (claude.ai: organization only, and why), a per-platform table for the token command, a "Files"
   line (Claude Desktop via the extension; the others via capability URLs, ADR 0028), and "How
   the client is identified" (claimed vs pinned, how-it-works.md "Which AI system is asking").
   Consolidate from install-*.md, how-it-works.md "How an AI system connects" and org guide §9;
   leave in those pages only what is platform- or mode-specific, plus a link. List it in
   docs/README.md → "Install and first steps" right after connecting-a-service.md.

Website (§5b):
2. clients.json: give every entry a "slug" (claude-desktop, claude-code, claude-ai) and a "docs"
   anchor into /docs/connecting-an-ai-client/. build_site.render_clients() renders each strip item
   as a link to /ai-agents/<slug>/.
3. website/ai-agents/index.html and website/ai-agents/<slug>/index.html, in the connector pages'
   structure and classes (page-intro, section-heading, card-grid, steps, cta-section): kicker "AI
   agent · <name>"; how it connects; "Set it up" per deployment (local / organization) with the
   short steps and a "Set it up" button to its docs anchor; what happens with files; how
   PrivacyFence names it on cards (claimed vs verified); client-side settings worth knowing (e.g.
   Claude Desktop's own tool permission prompt; claude.ai owners add the connector for the org);
   CTA to /ai-agents/ and /connectors/. meta pf-content-group "ai-agent". The index has one card
   per client plus an "Other MCP clients" card (Streamable HTTP + OAuth 2.1 with DCR). No logos (D11).
4. build_site: add the pages to PAGES, llms.txt entries; website/_partials/header.html: "AI agents"
   after "Connectors" in BOTH .nav-links and .nav-menu-panel (tests/unit/test_build_site.py holds
   them equal); check the header still fits at the 900 px breakpoint in light and dark (the
   browser layout test, guardrail 12, and screenshots in the PR). FAQ "Which AI clients work?":
   link /ai-agents/.
5. New guardrail 14, tests/unit/test_website_agent_pages.py (modelled on
   test_website_connector_pages.py, guardrail 8): every clients.json entry has exactly one
   /ai-agents/<slug>/ page in PAGES and vice versa; each page has content group "ai-agent", a
   "Set it up" link to its docs anchor, and names its client; /ai-agents/ links every page; the
   header links /ai-agents/ in both lists. Update test_website_clients.py's hard-coded NAMES only
   if the data file's names change (they don't here).
6. CHANGELOG [Unreleased]: "Added" — the AI agents section on privacyfence.eu and the Connecting an
   AI client guide.
Note (D9): the pages link /docs/connecting-an-ai-client/#…, which exists on the site only once a
stable release carries that doc. Until then the link check will fail against the stable tag's
docs, so split the work: PR 1 = the doc (steps 1 and 6's docs half) → merges → cut a release (🧑);
PR 2 = the website (steps 2–5), linking to the new doc. If that ordering is unacceptable, point the
buttons at the existing install-*/how-it-works/org-guide anchors in PR 2 and switch them in a
follow-up. Say in the PR which you did.
Run /dod on each; drive to green.
```

**Done when:** `/ai-agents/` and its three pages are live on privacyfence.eu, the header shows
"AI agents", and guardrail 14 is green.

---

### 🤖 WAVE 2: fold in the evidence and claim support (1 session) → ★ GATE A

Depends on:
- WAVE 1 merged, including WP 1.5 PR 1 (the doc)
- WP 1.3's PR B still open as a draft
- 🧑 M1.1–M1.3 reported on the issues, with at least M1.1 and M1.3 passing

#### WP 2.1: turn verified results into tests and docs · I391, I392 · `feature:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md and the evidence comments the maintainer posted on issues 391
and 392 (M1.1–M1.3). Implement WP 2.1 on top of WP 1.3's draft PR B (take it over; mark it ready):
1. Replace the SEEDED T2 fixtures in tests/fixtures/ai_clients/ with the captured ones (scrub:
   no real hostnames, emails, client_ids, tokens — use pf.example.com and dummy ids), update the
   READMEs to "captured <date>, client version <v>". Add fixtures for any new client captured.
2. agent_identity.py REGISTRY: set ChatGPT's client_names to the names actually observed — both
   the DCR client_name and the handshake clientInfo name — and keep "openai-mcp" only if it was
   observed. Confirm gemini-cli-mcp-client and add Gemini CLI's registered name if it differs.
   Unit-test each. ADR 0035 lists the guesses: record the corrections in a new ADR that amends
   it (next free number, ≥ 0081) — never edit 0035's body.
3. Remove the "Verification pending" notes for every path that passed; keep them, with the
   observed failure, for any that didn't. Fill the ai-client-qa.md evidence table. Adjust the
   ChatGPT text to what M1.3 saw about client-side confirmation (D5).
4. CHANGELOG [Unreleased] "Added": ChatGPT (Developer Mode, organization deployments) and Gemini
   CLI (local and organization), per D3. README.md client mentions (not the canonical description
   — WP 2.2 changes that together with the website).
5. Tick the finished checkboxes in issues 391 (Route A) and 392 and close 392 if all of it passed.
No website change (WP 2.2, after the release — D9).
Run /dod; drive the PR "feature: verified ChatGPT (Developer Mode) and Gemini CLI support" to green.
```

Then **🧑 M2**: cut a stable release carrying WP 2.1.

#### WP 2.2: the website claim, and two AI-agent pages · `feature:`

Depends on: the stable release carrying WP 2.1 is published (its `/docs/` is live).

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §3 (D3, D5, D8, D9, D11) and §5b. The latest stable release
carries connecting-an-ai-client.md's ChatGPT and Gemini CLI sections. Implement WP 2.2:
1. website/_data/clients.json: add "ChatGPT" (deployments ["organization"], connects "straight to
   an organization deployment's /mcp over HTTPS, with OAuth sign-in (Developer Mode)", slug
   chatgpt) and "Gemini CLI" (["local","organization"], "straight to /mcp over HTTP", slug
   gemini-cli).
2. website/ai-agents/chatgpt/ and website/ai-agents/gemini-cli/ pages (§5b structure). The
   ChatGPT page says: organization deployment only (and why), re-enable per chat, every tool
   listed as read-only on purpose with PrivacyFence's card as the confirmation (ADR 0076), and
   files move through capability URLs. Add both to PAGES, llms.txt and the /ai-agents/ index.
3. tests/unit/test_website_clients.py: update NAMES; make the not-yet-supported check precise
   — replace the bare "Gemini" with the names still unsupported ("Gemini Enterprise", "Gemini app",
   "Copilot", "Cursor", "ChatGPT desktop"/"ChatGPT workspace" as applicable) so Gemini CLI can be
   named while Gemini Enterprise can't. Assert ChatGPT is organization-only like claude.ai.
4. Canonical description (guardrail 1): if the client examples change ("such as Claude Desktop and
   Claude Code" / "web clients such as claude.ai"), change website/canonical-description.md,
   README.md's opening and website/index.html in this one commit.
5. CHANGELOG [Unreleased]: the website now lists ChatGPT and Gemini CLI.
Run /dod; one PR "feature: privacyfence.eu lists ChatGPT and Gemini CLI"; drive to green.
```

**★ GATE A is passed when:** WP 2.2 is merged to `main` (`pages.yml` then deploys the website).
**This is the earliest point you can say "works with ChatGPT and Gemini".**

---

### 5a. Website items carried over from the website follow-up plan

These were T1 and T2 of the website follow-up plan (`website-followup-plan.md` on
`claude/gracious-cerf-4pnoha`, a branch that no longer exists). They're called WS1/WS2 here because
T1–T4 in this plan are test tiers.

**WS1: the website names a client only once its support has shipped.** The rule is enforced on
`main`:
- `website/_data/clients.json` is the one list.
- `scripts/build_site.py` renders the "Works with" strip on `/`, `/enterprise/` and `/faq/`, plus
  the JSON-LD `softwareRequirements` of `/` and `/download/`, from that list.
- Guardrail 10 (`tests/unit/test_website_clients.py`) fails the build if a hand-written page names
  ChatGPT, Gemini, Copilot or Cursor.

So:
- No website page names ChatGPT or Gemini before its gate. Guardrail 10 checks hand-written pages
  only, not `/docs/`, which is why D6 keeps unverified drafts off `main` altogether.
- At each gate the claim grows through the same file, together with that client's `/ai-agents/`
  page (§5b), after the release (D9):
  - Gate A: WP 2.2 adds ChatGPT and Gemini CLI.
  - Gate B: adds the ChatGPT workspace connector, and ChatGPT desktop if WP 3.1 took branch (a)
    or (b).
  - Gate C: adds Gemini Enterprise.
- Guardrail 10 hard-codes `NAMES` and matches `\bGemini\b`, so every gate's website PR edits that
  test too (WP 2.2 step 3).
- Web clients reach PrivacyFence only through an organization deployment, so their
  `deployments` is `["organization"]`, as it is for claude.ai.

**WS2: AI assistants describe PrivacyFence correctly.** A week or two after Gate A's deploy, and
then monthly alongside the website's measurement:

1. Ask ChatGPT, Copilot, Gemini and Claude "What is PrivacyFence?" and "Which AI clients does
   PrivacyFence work with?".
2. Note whether the answers match `website/canonical-description.md` and `clients.json`.
3. If ChatGPT or Copilot, which are fed by Bing's index, lag behind Google-backed answers:
   - re-add Bing Webmaster Tools (a ~5-minute import from Search Console) and submit the sitemap
     there;
   - that reverses part of [ADR 0050](adr/0050-website-analytics-is-ga4-behind-consent.md)
     ("Google is the only search tooling"), so it needs a superseding ADR in the same change.

### 5b. The AI agents menu (website)

The Connectors menu has an index at `/connectors/` and one page per connector at
`/connectors/<name>/`, held to the code by guardrail 8. This section adds the same thing for AI
clients: **AI agents** in the top navigation, `/ai-agents/`, and one page per client at
`/ai-agents/<slug>/`. It's built in WP 1.5 for today's clients. Each gate adds pages in its website
PR (WP 2.2, the Gate B and Gate C website PRs).

**Where the data comes from.** `website/_data/clients.json` stays the one list (guardrail 10).
Each entry grows two fields:
- `slug`: the page URL
- `docs`: the anchor in `/docs/connecting-an-ai-client/`

The "Works with" strip links each name to its page. Pages are hand-written, like connector pages.
Their setup text is short and specific to each client, and the exact commands live in the doc
(D1).

**Every per-agent page has the same sections:**

| Section | Content |
|---|---|
| Intro | kicker "AI agent · \<name\>", one-sentence summary, which deployments it works with |
| How it connects | extension (`.mcpb`) / direct HTTP with a bearer token / OAuth with dynamic registration; why web clients need an organization deployment |
| Set it up | "Local mode" and "Organization mode" step lists (3–4 steps each), each with a **Set it up** button to its docs anchor |
| Files | through the extension (Claude Desktop), or through capability URLs (ADR 0028) for everything else |
| How it's identified | the card says the client *says* it is \<name\> (**Not verified**), and an organization admin can pin it on **Settings → AI systems** to make it verified |
| Client settings worth knowing | per client, for example: Claude Desktop's own tool prompt; claude.ai Team/Enterprise owners add the connector for everyone; ChatGPT: re-enable in every chat, tools listed as read-only on purpose, workspace connectors must be re-published after connecting a new service |
| Next | buttons to `/ai-agents/` and `/connectors/` |

**The pages:**

| Page | Lands in | Deployments |
|---|---|---|
| `/ai-agents/` index (one card per client, plus "Other MCP clients") | WP 1.5 | — |
| `/ai-agents/claude-desktop/` | WP 1.5 | local, organization |
| `/ai-agents/claude-code/` | WP 1.5 | local, organization |
| `/ai-agents/claude-ai/` | WP 1.5 | organization |
| `/ai-agents/chatgpt/` | WP 2.2 (Gate A); Gate B extends it with workspace publishing and, if (a)/(b), desktop | organization (+ local if desktop) |
| `/ai-agents/gemini-cli/` | WP 2.2 (Gate A) | local, organization |
| `/ai-agents/gemini-enterprise/` | Gate C website PR | organization |

**Guardrail 14** (`tests/unit/test_website_agent_pages.py`, WP 1.5) holds `clients.json` and the
pages in both directions, the same way guardrail 8 does for connectors:
- every client has exactly one page, and every page has a client;
- each page is in `PAGES` with content group `ai-agent` and a "Set it up" link to its docs anchor;
- `/ai-agents/` links every page;
- the header links `/ai-agents/` in both of its nav lists.

A client that isn't in `clients.json` can't have a page, so WS1's rule also covers the menu.

**Also touched:**
- `website/_partials/header.html` (both lists)
- `llms.txt`
- the FAQ's "Which AI clients work?"
- GA4 content group `ai-agent`, set by the page's `<meta name="pf-content-group">`, which `site.js`
  reads

---

### 🤖 WAVE 3: finish ChatGPT (up to 3 parallel sessions) → ★ GATE B

Depends on: GATE A. WP 3.1 also needs M1.4's result.

#### WP 3.1: ChatGPT desktop (I390): run the branch that matches M1.4's result

- **(a) Desktop accepts a remote `url` with custom headers.** Docs only. Add a desktop subsection
  to `connecting-an-ai-client.md`'s ChatGPT section: `url` plus `Authorization: Bearer <token>`,
  the same shape as Gemini CLI. No shim involved.
- **(b) Desktop supports stdio only.** This is packaging work. Install `shim.js` at a stable path
  from all three installers:
  - macOS: `PrivacyFenceApp.app/Contents/Resources/shim/shim.js`, via `build_dmg.sh`/`build_pkg.sh`
  - `.deb`: `/usr/lib/privacyfence/shim/shim.js`
  - Windows: `{app}\shim\shim.js`

  Extend each packaged smoke test to assert the file exists and answers `initialize` over stdio.
  Document an **absolute `node` path** in the snippet (correction 5). Verify by dispatching
  `build.yml` against the branch (steward table). → **ADR** ("the shim is also shipped outside the
  `.mcpb`").
- **(c) Desktop has no MCP support, or it's unusable.** Post a findings comment on I390 and close
  it as "not planned — revisit when…". Change the docs note to point ChatGPT users at org mode.

**Session prompt:**

```text
Read docs/ai-client-support-plan.md and the maintainer's M1.4 result on issue 390. Implement WP
3.1, branch (a), (b) or (c) as the plan describes — only the branch matching that result. For (b):
dispatch build.yml against your branch and confirm build, build-windows and build-deb go green
before asking for review; write the ADR (next free number). No website change here (Gate B's
website PR, after the release). Run /dod; one PR; drive to green.
```

#### WP 3.2: ChatGPT workspace-admin publish guide (I391 Route B) · `chore:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md (D5) and WP 1.4's findings on issue 391. Implement WP 3.2 as
docs: a "ChatGPT Business/Enterprise/Edu (workspace-published connector)" subsection in
connecting-an-ai-client.md's ChatGPT section, linked from org-mode-setup-guide.md §9, containing:
the admin flow (enable custom MCP connectors → create → Scan Tools → test as draft → publish);
a callout that Scan Tools lists every PrivacyFence tool as read-only on purpose (ADR 0076) and
PrivacyFence's approval card is the confirmation; an explicit callout that enabling or disabling
a PrivacyFence connector org-wide requires the ChatGPT workspace admin to re-open and RE-PUBLISH
the connector (PrivacyFence's tool list is dynamic, ChatGPT's published copy is frozen); and
pinning the workspace's registration on Settings → AI systems (org guide §11). Mark it
"Verification pending" linking https://github.com/privacyfence/privacyfence/issues/391. Add a row
to ai-client-qa.md. CHANGELOG [Unreleased] line. Run /dod; one PR; drive to green.
```

#### WP 3.3: T3 Gemini CLI org-mode OAuth in CI (I392) · `tests:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §2. Implement WP 3.3: extend tests.yml's org-mode-smoke job
(tests/integration/test_org_ubuntu_release_smoke.py harness + tests/integration/mock_idp.py) with a
Gemini CLI run against the org daemon. That job has no Node today: add actions/setup-node (pinned
by SHA, Node 22 like `test`) and `npm ci` of tests/integration/ai_clients (WP 1.2). Settings.json
httpUrl with no headers; drive the OAuth browser leg headlessly (first try the BROWSER env var
pointing at a script that follows the redirect chain with a cookie jar; fall back to Playwright/
Chromium — the `test` job already installs it, reuse that step), then `gemini mcp list` must show
connected. Assert the DCR client landed in oauth_clients.json and its registered client_name maps
to the gemini-cli agent. Also add it to ai-client-canary.yml with @latest. Run /dod; one PR; drive
green.
```

**★ GATE B is passed when:**
- WP 3.1 and WP 3.2 are merged
- 🧑 M3.1 passed and is logged (and M3.2, if you took branch (b))
- a stable release carries them
- a small website PR has extended `/ai-agents/chatgpt/` (workspace publishing, and desktop if it
  applies) and `clients.json`'s ChatGPT entry, including `"local"` if desktop works (D9)

---

### 🤖 WAVE 4: Gemini Enterprise, the only code change (I393) → ★ GATE C

Depends on: 🧑 M4.1 (Google's requirements confirmed). If Google turns out to support DCR,
**stop**: I393 becomes docs only, the same shape as WP 3.2.

#### WP 4.1: ADR, admin OAuth-client registration, tests and docs · `feature:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md (esp. §1 points 7, 8 and decision D4) and the maintainer's
M4.1 findings on issue 393. Implement WP 4.1:
1. ADR (next free number at merge time, ≥ 0081): "Pre-registered OAuth clients for clients that
   cannot use DCR" — decision D4, the stale-prune exemption, secret storage matching the SDK, why
   the command refuses while the daemon holds its lock, and the rejected alternatives
   (hand-editing the JSON; a live Settings → AI systems action — deferred, not rejected).
2. OrgOAuthProvider.register_static_client(client_info, *, source="admin") and
   remove_static_client(client_id); _StoredClient grows `source` ("dcr" | "admin", default "dcr"
   when the key is absent — there is no older format to migrate, ADR 0041);
   _prune_stale_clients_locked skips source=="admin"; the 2000-client cap still applies. The
   Settings → AI systems list shows the source.
3. `privacyfence-app --register-oauth-client --name "Gemini Enterprise" --redirect-uri <uri>
   [--client-id ...] [--token-endpoint-auth-method client_secret_post|client_secret_basic]`
   plus `--list-oauth-clients` and `--remove-oauth-client <id>`: org mode only; refuse with a clear
   message while the single-instance lock is held; generate client_id/secret with `secrets` when not
   given; print them ONCE to stdout; never log the secret.
4. Unit tests in tests/unit/web/test_oauth_provider.py (+ daemon_main CLI tests): hand-registered
   and DCR clients are indistinguishable to get_client/authorize/token; admin clients survive
   pruning; records without `source` load as "dcr"; lock-held refusal; secret never in logs. Add a
   T2 replay fixture tests/fixtures/ai_clients/gemini-enterprise/ that drives /authorize → /token
   with the pre-registered client and client_secret auth.
5. agent_identity REGISTRY: add "gemini-enterprise" keyed on the client_name the command sets,
   with an icon in resources/agent_icons/ and its license row (test_agent_label.py).
6. connecting-an-ai-client.md: a "Gemini Enterprise" section; org-mode-setup-guide.md: a "Clients
   that can't register themselves" subsection under §9, with Gemini Enterprise as the worked
   example (authorization URL https://<host>/authorize, token URL https://<host>/token, redirect
   URI from M4.1), marked "Verification pending" linking the full issue URL. ai-client-qa.md row.
   CHANGELOG.
Run /dod; one PR "feature: pre-registered OAuth clients (Gemini Enterprise)"; drive to green.
```

**★ GATE C is passed when:**
1. WP 4.1 is merged.
2. A release carrying it is installed on your org deployment.
3. 🧑 M4.2 passed.
4. A small docs PR has removed the "Verification pending" note.
5. After the next stable release, the website PR has added "Gemini Enterprise" to `clients.json`,
   added `/ai-agents/gemini-enterprise/`, and taken "Gemini Enterprise" off guardrail 10's
   not-yet list.

---

### 🤖 WAVE 5: Gemini app / Spark (I394, low priority)

#### WP 5.1: research · no PR

```text
Read docs/ai-client-support-plan.md and issue 394. Re-source Spark's custom-app MCP requirements
from Google-owned pages (DCR vs pre-registration, TLS, transport, whether it can reach a
self-hosted host). Comment findings on issue 394 with a recommendation: (i) smoke-test (say which:
DCR path, or the WP 4.1 admin client) or (ii) close as not a target audience. Don't write code.
```

Then do 🧑 M5.

### 🤖 WAVE 6: retire this plan

```text
Read docs/ai-client-support-plan.md. Every issue 390–394 is closed. For every "→ ADR" marker and
every §3 decision that meets CLAUDE.md's bar (D1 docs home, D4, D5, D6, D7, D9 at least), confirm
an ADR exists (write any missing one, next free number); then delete this plan file, remove it
from build_site.CONTRIBUTOR_DOCS and docs/README.md in the same PR, and say in the PR description
which ADRs carry its decisions (CLAUDE.md "Retiring a plan").
```

---

## 6. Issue closure map

| Issue | Closed by | Evidence it needs |
|---|---|---|
| I392 | WP 2.1 (+ WP 3.3 for CI) | M1.1, M1.2 logged; T2 and T3 green |
| I391 | WP 3.2 docs PR, verified | M1.3 (Route A) and M3.1 (Route B) logged |
| I390 | WP 3.1 (a/b) or closed via (c) | M1.4 (+ M3.2 for branch b) |
| I393 | WP 4.1 and the Gate C docs PR | M4.1, M4.2 logged |
| I394 | WP 5.1 → M5 | comment with a decision |

---

## 7. 🧑 Your manual tasks: step by step

Vendor menu names below were current as of 2026-09. OpenAI and Google rename these often. If a
menu doesn't match, look for the nearest equivalent, and **write the real path in your evidence
comment** so WP 2.1 can put it in the docs.

**Evidence to post after every test**, as a comment on that test's issue (for example
"M1.1 result"):

- Client name and version, OS, date, and PrivacyFence version (About box, or `--version`).
- Pass or fail for each step, and screenshots of anything that failed.
- **Whether the client showed its own confirmation before a write** (ADR 0076, D5).
- **The agent PrivacyFence recorded:** the approval card's "AI system" line, and the audit entry's
  `agent_id`, `agent_name`, `agent_version` and `agent_source`. Audit entries are under the data
  directory's `users/<principal>/logs/audit/`.
- **Org mode only:**
  - The new client's entry from `org/oauth_clients.json`, under
    `/var/lib/privacyfence-org/.privacyfence/`. **Replace the `client_secret` value with
    `REDACTED` before pasting.** Keep `client_name`, `redirect_uris`, `grant_types` and
    `token_endpoint_auth_method`.
  - Then pin that registration on **Settings → AI systems** and confirm that the next card shows
    the client as verified.

### M0: before Wave 1

**M0.1 Confirm decisions D1–D12** (10 min)
1. Read §3.
2. Reply in the session with "D1–D12 as recommended", or give your changes.
3. Merge this plan's docs-only PR (D12).
4. Also answer the open question in the "AI agent protocol stability" session
   (<https://claude.ai/code/session_019R3xZ8EhKqmqmygckY8KEo>) with "docs location → D2 from
   docs/ai-client-support-plan.md", so the two plans don't diverge.

**M0.2 An org-mode deployment on public HTTPS** (needed for M1.2, M1.3, M3 and M4; 1–3 h if you
don't have one)

ChatGPT's and Gemini's cloud-side clients have to reach `/mcp` from the internet, over a certificate
from a public CA.
1. Get a small Linux VM from any provider (Ubuntu 24.04 is the reference), with 2 vCPU and 2 GB RAM.
2. DNS: at your registrar, open DNS → Records → Add record and create **A** `pf-test.<your-domain>`
   pointing at the VM's IP.
3. Follow [`org-mode-setup-guide.md`](org-mode-setup-guide.md) §2 → §9 in order:
   - **§4** is the identity provider. For Google: Google Cloud Console → **APIs & Services →
     Credentials → + Create credentials → OAuth client ID → Web application**, with the redirect
     URI exactly as §4's table says.
   - **§7** is Caddy + Let's Encrypt, which gives you the certificate ChatGPT and Gemini need.
   - **§8** is the systemd unit (`privacyfence-org.service`).
4. Check it from your laptop: `curl -s https://pf-test.<your-domain>/.well-known/oauth-authorization-server | head`
   must return JSON that lists `registration_endpoint`.
5. Sign in, and on `/connect` connect at least **Google Calendar**, so the tests have one read tool
   and one gated write (`calendar_create_event`). Connect Drive too for the file tests.

**M0.3 Accounts and installs** (15 min)

| Need | For | How |
|---|---|---|
| Node 20+ on your Mac | M1.1, M1.2 | `brew install node` |
| Gemini CLI | M1.1, M1.2 | `npm install -g @google/gemini-cli`, then run `gemini` once and choose **Login with Google** |
| ChatGPT **Plus/Pro** (personal) or a Business seat | M1.3 | <https://chatgpt.com/#pricing> |
| ChatGPT desktop for macOS | M1.4 | <https://openai.com/chatgpt/desktop/> → Download for macOS |
| ChatGPT **Business/Enterprise workspace, as admin** | M3.1 | <https://chatgpt.com/team> (Business trial is enough) |
| Google Cloud project with **Gemini Enterprise** | M4 | <https://console.cloud.google.com/gemini-enterprise> (trial is enough) |

### M1: after Wave 1 is merged (about 2 h in total)

Do these in order. Each one is enough on its own for a partial claim. The steps don't depend on
WP 1.3's draft docs.

**M1.1 Gemini CLI, local mode** (I392, 15 min)
1. Make sure PrivacyFence (packaged, local mode) is running on your Mac.
2. In Terminal:
   ```bash
   PF_HANDOFF="/Library/Application Support/PrivacyFence/handoff"
   PF_APP="/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp"
   gemini mcp add --transport http privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
     --header "Authorization: Bearer $("$PF_APP" --print-mcp-token)"
   ```
   If `gemini mcp add` doesn't exist in your version, edit `~/.gemini/settings.json` by hand:
   `{"mcpServers":{"privacyfence":{"httpUrl":"<mcp_url>","headers":{"Authorization":"Bearer <token>"}}}}`.
3. Run `gemini`, then type `/mcp`. **Expected:** `privacyfence` is 🟢 connected and its tools are
   listed.
4. Prompt: `Use privacyfence to list my calendar events for tomorrow.` **Expected:** the tool runs.
5. Prompt: `Use privacyfence to create a calendar event "PF test" tomorrow 10:00–10:15.`
   **Expected:**
   - Gemini CLI may ask you to allow the tool (note whether it did).
   - Then the **PrivacyFence approval card** appears. Open it from the companion's
     **Open Approvals**, approve, and confirm the event exists in Google Calendar.
6. Upload test (ADR 0028): `Use privacyfence to upload ~/Desktop/test.txt to my Drive root` (needs
   Drive connected). **Expected:** it uses `privacyfence_create_upload_slot` and a `PUT`.
   Download test: `Use privacyfence to download test.txt from my Drive root`. **Expected:** a
   `/mcp-files/fetch/…` URL.
7. Post evidence on **I392**, titled "M1.1 result".

**M1.2 Gemini CLI, org mode** (I392, 15 min; needs M0.2)
1. `gemini mcp add --transport http privacyfence-org https://pf-test.<your-domain>/mcp` (no header).
2. `gemini` → `/mcp auth privacyfence-org`. **Expected:** a browser opens at PrivacyFence `/login`,
   goes on to Google sign-in, and comes back to "authentication successful".
3. `/mcp`: **expected** 🟢 connected. Then repeat M1.1 steps 4–5 against this server, approving
   at `https://pf-test.<your-domain>/approvals`.
4. Pin the registration on **Settings → AI systems** and repeat step 4 of M1.1 once: the card now
   shows Gemini CLI as verified.
5. Post evidence on **I392**, titled "M1.2 result", with the `oauth_clients.json` entry (secret
   redacted).

**M1.3 ChatGPT Developer Mode, org mode** (I391 Route A, 30 min; needs M0.2)
1. <https://chatgpt.com> → your **profile icon** (bottom-left) → **Settings** → **Apps** (older UI:
   **Apps & Connectors**) → **Advanced settings** → turn **Developer mode** on.
   On a Business workspace, if the toggle is missing, the admin has to allow it first (M3.1 steps
   1–2).
2. **Settings** → **Apps** → **Create** (top right). Fill in:
   - Name: `PrivacyFence`
   - MCP Server URL: `https://pf-test.<your-domain>/mcp`
   - Authentication: **OAuth**
   - tick **I understand and want to continue** (the "trust this app" checkbox), then **Create**
3. **Expected:** ChatGPT redirects you to PrivacyFence `/login`, then Google, then back to ChatGPT
   showing the app as connected with its tool list.
   - Note how ChatGPT labels the tools. Expect every tool marked read-only (ADR 0076).
   - If OAuth finishes but the app doesn't appear, try once more before reporting it. This is a
     known ChatGPT flakiness (I391).
4. New chat → the **+** in the message box → **More** → **Developer mode** → enable **PrivacyFence**
   for this chat. (You have to do this in every new chat.)
5. Prompt: `List my calendar events for tomorrow.` **Expected:** the tool call succeeds.
6. Prompt: `Create a calendar event "PF test" tomorrow 10:00–10:15.` **Expected:** the PrivacyFence
   approval card appears at `https://pf-test.<your-domain>/approvals`. **Record whether ChatGPT
   showed its own Confirm box first** (probably not, because of ADR 0076). Screenshot what you
   see.
7. Pin the registration on **Settings → AI systems**, then repeat step 5: the card shows ChatGPT
   as verified.
8. Post evidence on **I391**, titled "M1.3 result (Route A)", with the `oauth_clients.json` entry
   (secret redacted) and the agent fields recorded, both before and after pinning.

**M1.4 ChatGPT desktop check** (I390, 15 min)
1. Open ChatGPT desktop → **ChatGPT** menu (menu bar) → **Settings…** Look for an **MCP servers**
   (or **Connectors / Apps → Advanced**) page with **Add server**.
2. Write down exactly what you find:
   - (a) you can add a **URL** and set **custom headers**
   - (b) you can add only a **command** (stdio)
   - (c) there's no MCP page at all
   Also check whether `~/Library/Application Support/ChatGPT/mcp_config.json` exists
   (`ls ~/Library/Application\ Support/ChatGPT/`).
3. If (a): add the URL from your `mcp_url` file (normally `http://127.0.0.1:8765/mcp`) with the
   header `Authorization: Bearer <output of PrivacyFenceApp --print-mcp-token>`, and repeat M1.1
   steps 4–5.
4. If (b): **don't** point it at Claude's extension folder. Post the result, and WP 3.1(b) ships a
   stable `shim.js`.
5. Post evidence on **I390**, titled "M1.4 result: (a)/(b)/(c)", with screenshots of the settings
   page.

→ Then run **WAVE 2**.

### M2: after Wave 2 → Gate A (about 20 min, plus the release)
1. Review and merge WP 2.1's PR: GitHub → **Pull requests** → the PR → **Files changed** →
   **Review changes** → **Approve** → **Merge pull request** (merge commit, not squash).
2. Cut a stable release (`/cut-release`), so the new setup sections reach `privacyfence.eu/docs/`.
3. Run **WP 2.2** (website), then review and merge it.
4. Check the website after `pages.yml` finishes: GitHub → **Actions** → **pages** → latest run is
   green. Then open <https://privacyfence.eu> and confirm three things: the "Works with" strip
   lists ChatGPT and Gemini CLI, **AI agents** shows their pages, and each page's **Set it up**
   button lands on the right docs section.
5. Start WS2's check (§5a) a week or two after the deploy, once crawlers have picked it up.

### M3: after Wave 3

**M3.1 ChatGPT workspace-published connector** (I391 Route B, 45 min; needs a Business/Enterprise
workspace where you're admin)
1. <https://chatgpt.com> → profile icon → **Workspace settings** (or <https://chatgpt.com/admin>)
   → **Permissions & roles** → turn on **Developer mode / Create custom MCP connectors** for
   admins.
2. Same place: under the **Apps** / **Connectors** permission, allow members to use custom apps,
   or scope it to one test group. **Note any setting that distinguishes read-only from write
   actions** (D5's stop condition) and screenshot it.
3. **Workspace settings** → **Apps** (or **Connectors**) → **Create** → MCP URL
   `https://pf-test.<your-domain>/mcp`, Authentication **OAuth** → **Scan tools**.
   **Expected:** the scan accepts all tools, listing each as read-only. Screenshot any warnings.
4. **Test as draft** in a chat, and run M1.3 steps 5–6.
5. **Publish** to the workspace. Then, signed in as a **non-admin member** with Developer Mode off,
   confirm PrivacyFence is available in a chat.
6. **Frozen-snapshot test:**
   1. On `/connect`, connect one *more* PrivacyFence connector (for example Google Tasks).
   2. In a member chat. **Expected:** its tools do **not** appear.
   3. As admin: **Apps** → PrivacyFence → **Update / Re-publish**. **Expected:** now they do.
7. Post evidence on **I391**, titled "M3.1 result (Route B)".
8. After the release that carries WP 3.1/3.2: run the Gate B website PR (see Gate B above).

**M3.2 ChatGPT desktop retest** (only if WP 3.1 took branch (b))
1. Install the release or pre-release that carries WP 3.1: `/cut-release` with an `aN` tag, then
   <https://privacyfence.eu/download/> → "Want to test the next version?".
2. In ChatGPT desktop → **Settings** → **MCP servers** → **Add server**. Set command to the
   absolute `node` path from the new docs snippet (`which node`), and args to the documented
   `shim.js` path.
3. Quit PrivacyFence first, so you test that the shim starts the daemon. Then repeat M1.1 steps 3–5.
4. Post evidence on **I390**, titled "M3.2 result".

### M4: Gemini Enterprise

**M4.1 Confirm Google's requirements before WAVE 4** (20 min)
1. Open <https://cloud.google.com/gemini/enterprise/docs> and search for **"custom MCP server"**,
   or use the URL from I393's source 7.
2. Write down:
   - does it support DCR? (If **yes**, tell the session; WAVE 4 becomes docs only.)
   - the exact **redirect URI**
   - which fields it asks for (authorization URL, token URL, scopes, client ID, client secret)
   - the client authentication method (`client_secret_post` or `_basic`)
   - the console menu path
3. Post it on **I393**, titled "M4.1 requirements", with links. Then run **WAVE 4**.

**M4.2 Register and verify** (45 min; after WP 4.1 ships in a release installed on your org VM)
1. On the VM: `sudo systemctl stop privacyfence-org` (the command refuses to run while the daemon
   is up).
2. `sudo -u privacyfence-org /opt/privacyfence/venv/bin/privacyfence-app --register-oauth-client --name "Gemini Enterprise" --redirect-uri https://vertexaisearch.cloud.google.com/oauth-redirect`
   Use the URI from M4.1 if it's different. Copy the printed `client_id` and `client_secret`
   **now**; they're shown only once. (Use the same `--config`/data-dir flags as the systemd unit
   in org guide §8, if WP 4.1's docs say so.)
3. `sudo systemctl start privacyfence-org`
4. <https://console.cloud.google.com/gemini-enterprise> → select your project → **Apps** → your
   app → **Connected data stores** (or **Data stores / Actions**) → **+ Add** → **Custom MCP
   server**. Fill in:
   - Server URL `https://pf-test.<your-domain>/mcp`
   - Authorization URL `https://pf-test.<your-domain>/authorize`
   - Token URL `https://pf-test.<your-domain>/token`
   - Client ID and Client secret from step 2
   - scopes as M4.1 found

   Then click **Connect/Authorize**. **Expected:** PrivacyFence `/login` → Google → back to the
   console showing it connected.
5. In the Gemini Enterprise web app, run M1.3 steps 5–6, and pin the registration (M1.3 step 7).
6. Post evidence on **I393**, titled "M4.2 result". → Gate C docs PR, then (after the next stable
   release) the Gate C website PR.

### M5: Spark (after WP 5.1)
1. If WP 5.1 recommends a smoke test: in the <https://gemini.google.com> app → **Settings** →
   **Apps / Connected apps** → **Add custom app** (menu per WP 5.1's findings), URL
   `https://pf-test.<your-domain>/mcp`, and repeat M1.3 steps 5–6.
2. Post the result on **I394**, or close the issue as "not a target audience" if WP 5.1
   recommended that.
