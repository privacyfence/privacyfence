# Plan: ChatGPT and Gemini support (#390–#394)

> **Temporary plan document** (see [`adr/README.md`](adr/README.md)): delete it in the PR that
> finishes the last wave. Before deleting it, move every decision marked **→ ADR** below into an ADR.

Covers [#390](https://github.com/privacyfence/privacyfence/issues/390) (ChatGPT desktop, local
mode), [#391](https://github.com/privacyfence/privacyfence/issues/391) (ChatGPT, org mode),
[#392](https://github.com/privacyfence/privacyfence/issues/392) (Gemini CLI, both modes),
[#393](https://github.com/privacyfence/privacyfence/issues/393) (Gemini Enterprise, needs code) and
[#394](https://github.com/privacyfence/privacyfence/issues/394) (Gemini app / Spark, research).

**The goal of the order below:** say "works with ChatGPT and Gemini" publicly after **Gate A**,
which needs no server-side code. Everything after Gate A widens that claim.

---

## 0. How to use this plan

There are two kinds of step, and every step is labelled as one or the other:

- **🤖 WAVE n** means Claude sessions do the work. Each wave contains **work packages** (WP n.m).
  Packages in the same wave do not depend on each other, so each one gets its own session and its
  own PR, and they run at the same time.
- **🧑 M n.m** means **you** do it by hand, because it needs an account, a real app, or a
  decision. §7 has step-by-step instructions for each one.

### Running a wave

Open a new Claude Code on the web session on `privacyfence/privacyfence` and paste:

```text
Implement WAVE <n> of docs/ai-client-support-plan.md. The plan is on the branch
claude/practical-thompson-lp6r1d if it's not on main yet.

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

Rules every child session follows (they are already in each prompt through `CLAUDE.md` and the
`steward` skill, and repeated here so you can check):

- Work on a new branch and open **one PR per package**. Put the `<type>` (`feature`, `fix`, `chore`,
  `tests`) in the PR title, because the `claude/*` branch name can't carry it.
- The PR is done when `/dod` (the §2.7 definition of done) passes and CI is green.
- User-visible changes get a line under `CHANGELOG.md`'s `## [Unreleased]`. Never add a version
  heading.
- Anything that needs a Mac, Windows or real credentials is **dispatched** as a workflow (see the
  `steward` skill's table). It is never skipped.

---

## 1. Corrections: what the issues say vs. what the code does today

The issues were written 2026-09-14. These points were checked against the code on 2026-09-24, and
they change the work:

1. **Local-mode token.** The issues say `cat ~/.privacyfence/mcp_token`. Since
   [ADR 0008](adr/0008-one-principal-per-os-user.md), packaged installs are privilege-separated,
   and the token comes from **`privacyfence-app --print-mcp-token`**. The URL is in the handoff
   directory's `mcp_url` file. See `getting-started.md` §"Connect Claude Code (or another HTTP MCP
   client)".
2. **Where setup snippets go.** The issues say "`TECHNICAL_REFERENCE.md`'s installation section".
   That section now only links to `platform-support.md`. The page users actually read is
   `getting-started.md`'s "Connect Claude Code (or another HTTP MCP client)", so client snippets go
   there, next to Claude Code. `TECHNICAL_REFERENCE.md` gets a one-line pointer. *(D1)*
3. **Org-mode guide reference.** The issues cite `org-mode-setup-guide.md` §9 for "point a client at
   `/mcp`". That text is now **§8 step 3**.
4. **`docs/manual-pre-release-test-plan.md` does not exist.** Log evidence in the places that do
   exist: `release-testing.md` (human checks) and `connector-qa-testing.md` (format).
5. **The shim ships only inside `PrivacyFence.mcpb`.** No installer puts `shim.js` at a stable path
   on disk, so ChatGPT desktop has nothing reliable to point `command`/`args` at. Claude Desktop also
   runs `.mcpb` servers with **its own bundled Node**. ChatGPT would run `node` from a macOS GUI
   app's minimal `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`), where Homebrew's Node is not found.
   → #390 may need installer work (WP 3.1b).
6. **ChatGPT on the web cannot reach a local-mode daemon.** Developer Mode connectors are called
   from OpenAI's cloud, and local mode listens on `127.0.0.1`. So the **fastest verified ChatGPT
   claim goes through org mode (#391 Route A)**, and that needs an org deployment on public HTTPS
   (🧑 M0.2). ChatGPT desktop (#390) is the only local-mode route, and it is the least certain one.
7. **Agent names are guesses.** `src/privacyfence/agent_identity.py`'s `REGISTRY` has `openai-mcp`
   (ChatGPT) and `gemini-cli-mcp-client` (Gemini CLI). ADR 0035 marks them as guesses to be
   "corrected from a real handshake". 🧑 M1 captures the real names, and WP 2.1 fixes them. Until
   then, audit entries from these clients show up as *unknown agent*.
8. **Traps for #393 in `web/oauth_provider.py`:**
   - The running daemon keeps `oauth_clients.json` **in memory** and rewrites the whole file on
     every `get_client` call. If a script edits the file while the daemon runs, the next
     `/authorize` or `/token` overwrites the edit.
   - The daemon holds a `portalocker` single-instance lock on the data directory. A script can use
     it to tell whether the daemon is running.
   - `_STALE_CLIENT_TTL_SECONDS` is 180 days. A client registered by hand that nobody uses for 6
     months gets **pruned without warning**, which breaks the Gemini Enterprise connector.
   - `_MAX_REGISTERED_CLIENTS` is 2000.
   - The client secret is stored the way the SDK's `OAuthClientInformationFull` stores it: inside
     the model, in plaintext. A hand-seeded client has to be stored the same way, or the SDK's own
     client authentication won't accept it.
9. **Moving files without the shim.** Direct HTTP clients (ChatGPT, Gemini) use
   [ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)'s capability URLs
   (`privacyfence_create_upload_slot`, `/mcp-files/fetch/<token>`). Verification has to cover one
   upload and one download.

---

## 2. Testing strategy

This builds on the four-tier design from the "AI agent protocol stability" session, which is
waiting on one decision (D2).

| Tier | What | Runs where | Catches | Built in |
|---|---|---|---|---|
| **T1** Static portability | A unit test over every `ToolSpec`. It checks that the input schema root is `type: object`; there are no `$ref`/`$defs`/remote refs and no tuple-form `items`; tool names match `^[a-zA-Z0-9_-]{1,64}$` (OpenAI's limit); descriptions have a length bound; and every tool has `readOnlyHint`/`destructiveHint`/`idempotentHint`, which ChatGPT's "Scan Tools" step reads. | every PR (`tests.yml` → `test`) | A new tool that ChatGPT or Gemini would reject or rename | WP 1.1 |
| **T2** Recorded-handshake replay | Fixtures under `tests/fixtures/ai_clients/<client>/` hold each client's real DCR `/register` body, `clientInfo`, redirect URIs, `token_endpoint_auth_method` and request order. They are replayed through the in-process harness `tests/unit/web/test_org_mcp_e2e.py` already uses. Also asserts the `agent_identity` result. | every PR | A server-side change that would break a client we can't run in CI (ChatGPT, Gemini Enterprise) | WP 1.1 (seeded), WP 2.1 (real captures) |
| **T3** Real client in CI | The pinned `@google/gemini-cli` runs `gemini mcp list` against a real daemon. Local mode uses `httpUrl` and a bearer header. Org mode uses the mock IdP and DCR (WP 3.3). A separate **weekly canary** does the same with `@latest`. | PR (pinned) plus scheduled (`@latest`) | Gemini CLI changing on its side (drift) | WP 1.2, WP 3.3 |
| **T4** Manual pre-release check | One row per client in a new `docs/ai-client-qa.md`, and a pointer bullet in `release-testing.md` → "Human checks". The check runs when `/mcp`, OAuth, `routes_mcp.py` or `agent_identity.py` change. | before a release, by a human | Everything the tiers above can't run (ChatGPT, Gemini Enterprise) | WP 1.3 (template), 🧑 M1/M3/M4 fill it |

The rule that ties the tiers together: **each manual verification (T4) ends by saving a T2 fixture**,
so a client verified once by hand stays covered by a test on every PR.

---

## 3. Decisions to confirm before Wave 1 (🧑 M0.1)

| # | Decision | Recommendation |
|---|---|---|
| D1 | Where the per-client setup snippets go | `getting-started.md`, in the section next to Claude Code (correction 2), plus a one-line pointer in `TECHNICAL_REFERENCE.md`. Org-mode clients go in `org-mode-setup-guide.md` §8 step 3. |
| D2 | Where the AI-client checklist and evidence log go (the open question from the testing session) | A new `docs/ai-client-qa.md`, shaped like `connector-qa-testing.md`, with one bullet pointing to it from `release-testing.md` → "Human checks". Don't put it in `connector-qa-testing.md`, which is about connectors (Gmail…), not clients. |
| D3 | The wording of the public claim before T4 has run against a release | "Works with ChatGPT (Developer Mode, org mode) and Gemini CLI — verified <date>". List ChatGPT desktop and Gemini Enterprise only after their own gates. |
| D4 | How #393 writes a client while the daemon runs → **ADR** | MVP: a `privacyfence-app --register-oauth-client` subcommand that **refuses to run while the daemon holds its lock**. It writes through a new `OrgOAuthProvider.register_static_client()`, and the client record carries `"source": "admin"` so it's never pruned as stale. Later, maybe: the same action in org `/settings` with step-up (ADR 0034). |

---

## 4. Roadmap at a glance

```text
Low effort ─────────────────────────────────────────────────────────────▶ High effort

🧑 M0  decisions + accounts + org deployment on public HTTPS
│
🤖 WAVE 1   WP1.1 T1+T2 tests │ WP1.2 Gemini CLI in CI │ WP1.3 docs (unverified) │ WP1.4 re-source vendor docs
│
🧑 M1  Gemini CLI local → Gemini CLI org → ChatGPT Dev Mode → ChatGPT desktop check  (~2 h)
│
🤖 WAVE 2   WP2.1 fold in evidence, fix agent names, flip docs to verified, README/website/CHANGELOG
│
★ GATE A — public claim: "Works with ChatGPT and Gemini"      (#392 done, #391 Route A done)
│
🤖 WAVE 3   WP3.1 ChatGPT desktop (a/b/c from M1.4) │ WP3.2 ChatGPT workspace-admin guide │ WP3.3 Gemini CLI org-mode OAuth in CI
│
🧑 M3  ChatGPT workspace-admin publish (Business) + ChatGPT desktop retest
│
★ GATE B — "ChatGPT Business/Enterprise workspace connector" and possibly "ChatGPT desktop"   (#391, #390 done)
│
🧑 M4.1 confirm Google's Gemini Enterprise requirements
🤖 WAVE 4   WP4.1 ADR + admin OAuth-client registration + tests + docs          (#393 code)
🧑 M4.2 register the client, configure Gemini Enterprise, verify
│
★ GATE C — "Gemini Enterprise"                                    (#393 done)
│
🤖 WAVE 5   WP5.1 Spark research   → 🧑 M5 try it or close        (#394)
│
🤖 WAVE 6   WP6.1 retire this plan: extract ADRs, delete this file
```

| Order | Issue | Effort | Server code? | Unlocks |
|---|---|---|---|---|
| 1 | #392 Gemini CLI local | minutes | no | first Gemini claim |
| 2 | #392 Gemini CLI org | ~1 h (with an org deployment) | no | Gemini org claim |
| 3 | #391 Route A (ChatGPT Developer Mode) | ~1 h | no | **first ChatGPT claim** |
| 4 | #390 ChatGPT desktop | none, or installer work | maybe (packaging) | ChatGPT local mode |
| 5 | #391 Route B (workspace publish) | docs + Business workspace | no | ChatGPT for a whole org |
| 6 | #393 Gemini Enterprise | real feature + ADR | **yes** | Gemini for a whole org |
| 7 | #394 Spark | research | probably not | consumer Gemini (low priority) |

---

## 5. Waves (🤖 Claude)

### 🤖 WAVE 1: test scaffolding and draft docs (4 parallel sessions)

Depends on: 🧑 M0.1 (decisions). WP 1.4 has no dependencies.

#### WP 1.1: T1 portability lint and T2 replay harness · #390–#393 · `tests:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §1–§2 (branch claude/practical-thompson-lp6r1d if not on main).
Implement WP 1.1, tests only, with no production code change unless a test finds a real bug (if it
does, stop and report it; don't fix it here):
1. T1 — tests/unit/web/test_tool_schema_portability.py: over every ToolSpec that McpDispatcher can
   list (build every connector the way test_mcp_tools.py does), assert: inputSchema root
   type=="object"; no "$ref"/"$defs"/"definitions"; no tuple-form "items"; tool name matches
   ^[a-zA-Z0-9_-]{1,64}$; description is non-empty and ≤ 1024 chars; annotations readOnlyHint,
   destructiveHint and idempotentHint are all present. Include meta-tools. Parametrize by tool name
   so a failure names the tool.
2. T2 — tests/fixtures/ai_clients/{chatgpt,gemini-cli}/register.json plus a README.md per client
   stating the fixture is SEEDED FROM VENDOR DOCS, NOT YET CAPTURED (WP 2.1 replaces it).
   ChatGPT redirect_uri: https://chatgpt.com/connector_platform_oauth_redirect. Gemini CLI:
   http://localhost:7777/oauth/callback. Take the other fields from each vendor's public docs and
   cite them in the README.
   tests/unit/web/test_ai_client_replay.py: reuse test_org_mcp_e2e.py's in-process harness
   (factor shared helpers into a conftest rather than copying) and for each fixture: POST /register
   with the recorded body → /authorize → faked IdP leg → /token → initialize with the recorded
   clientInfo → tools/list → one read tools/call; assert success and assert the audit/agent
   attribution result for that client.
Run the /dod skill; open one PR titled "tests: AI-client schema portability (T1) and handshake
replay (T2)"; drive it to green.
```

**Done when:** both test files pass in CI, and the coverage floor holds.

#### WP 1.2: T3 Gemini CLI local-mode contract test in CI · #392 · `tests:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §1–§2. Implement WP 1.2:
1. tests/integration/test_gemini_cli_contract.py, modelled on test_shim_mcp_contract.py and
   test_mcp_daemon_contract.py: start a real local-mode daemon on a free port, write a throwaway
   HOME with .gemini/settings.json containing
   {"mcpServers":{"privacyfence":{"httpUrl":"<url>","headers":{"Authorization":"Bearer <token>"}}}},
   run `npx --yes @google/gemini-cli@<PINNED> mcp list` with that HOME, and assert privacyfence
   shows as connected with a non-zero tool count. It needs no Gemini/Google credential — if the
   pinned version turns out to need one even for `mcp list`, stop and report that rather than
   adding a secret. Skip when npx is absent (same posture as the shim contract test).
   Pin the version in one constant; note in the module docstring how to bump it.
2. Run it in tests.yml's existing `test` job (it already sets up Node), not a new job, unless the
   runtime cost is > 60 s — then a separate job.
3. .github/workflows/ai-client-canary.yml: weekly schedule + workflow_dispatch, same test with
   GEMINI_CLI_VERSION=latest; on failure, open (or update) one issue titled
   "AI-client canary: Gemini CLI @latest broke /mcp". Pin all actions by SHA like the other
   workflows.
Run /dod; one PR "tests: Gemini CLI contract test (T3) and weekly @latest canary"; drive to green.
```

**Done when:** the pinned test passes in the PR's CI. The canary can't be dispatched until it's on
`main` (see the steward skill), so dispatch it once after merge. 🧑 Merging the PR is your step.

#### WP 1.3: draft client docs and the T4 checklist · #390–#392 · `chore:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §1–§3. Implement WP 1.3 (docs only; decisions D1 and D2 as
recommended unless the plan says otherwise):
1. getting-started.md: extend "Connect Claude Code (or another HTTP MCP client)" with
   "Gemini CLI" (settings.json httpUrl + headers, and the `gemini mcp add --transport http ...
   --header "Authorization: Bearer $(privacyfence-app --print-mcp-token)"` one-liner; state
   explicitly that Gemini CLI does not use the .mcpb shim and none should be packaged) and a
   "ChatGPT" note (web ChatGPT can't reach local mode; see org mode; desktop pending #390).
2. org-mode-setup-guide.md §8 step 3: add Gemini CLI (httpUrl, no headers) and ChatGPT Developer
   Mode (Settings → Apps → Advanced settings → Developer mode; create app with the /mcp URL,
   OAuth). Call out: re-enable per chat; ChatGPT's own confirmation stacks on PrivacyFence's
   approval dialog (double consent).
3. New docs/ai-client-qa.md in the shape of connector-qa-testing.md: prerequisites, a per-client
   script (tools/list, one read, one gated write → approval dialog, one capability-URL upload and
   one download per ADR 0028, audit entry shows the right agent), and an evidence table with
   columns Client | Version | OS | Mode | Date | Result | Agent name seen | Fixture captured.
   One bullet in release-testing.md "Human checks" pointing at it.
4. TECHNICAL_REFERENCE.md: one-line pointer from "Installation and packaging".
Mark every new ChatGPT/Gemini section with a visible "Verification pending (#39x)" admonition —
WP 2.1 removes them. No CHANGELOG line yet (nothing is claimed). No README/website change.
Run /dod; one PR "chore: draft ChatGPT and Gemini client setup + AI-client QA checklist".
```

#### WP 1.4: re-source the vendor claims · #390, #391, #393, #394 · research, no PR

**Session prompt:**

```text
Read docs/ai-client-support-plan.md. WP 1.4 is research only — no commits. Using WebFetch/
WebSearch, try to confirm from the vendor's OWN pages (not aggregators), and record the URL +
date for each:
(a) ChatGPT desktop MCP support: does it exist, config file path, stdio vs url, custom headers?
(b) ChatGPT Developer Mode menu path and OAuth redirect URI; workspace-admin publish flow and the
    "frozen snapshot / re-publish" behaviour (help.openai.com 12584461 and 11509118).
(c) Gemini Enterprise custom MCP server connector: DCR or not, fixed redirect URI, required fields
    (authorization URL, token URL, scopes, client auth method), console menu path.
(d) Gemini app "Spark" custom apps: DCR or pre-registration, TLS requirements.
Post one comment per issue (#390, #391, #393, #394) with findings, clearly separating "confirmed
on vendor page <url>" from "could not reach". If egress blocks a page, say so and list it for the
human. End with a short summary in chat of anything that changes this plan.
```

**Done when:** each of the four issues has a findings comment. Any page it couldn't reach becomes
part of 🧑 M1.4 or 🧑 M4.1.

---

### 🤖 WAVE 2: fold in the evidence and claim support (1 session) → ★ GATE A

Depends on: WAVE 1 merged, and 🧑 M1.1–M1.3 reported in the issues (at least M1.1 and M1.3 passing).

#### WP 2.1: turn verified results into tests, docs and the claim · #391, #392 · `feature:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md and the evidence comments the maintainer posted on #391 and
#392 (M1.1–M1.3). Implement WP 2.1:
1. Replace the SEEDED T2 fixtures in tests/fixtures/ai_clients/ with the captured ones (scrub:
   no real hostnames, emails, client_ids, tokens — use pf.example.com and dummy ids), update the
   READMEs to "captured <date>, client version <v>". Add fixtures for any new client captured.
2. agent_identity.py REGISTRY: correct the "chatgpt" and "gemini-cli" client_names to the names
   actually observed (keep old guesses only if also observed). Unit-test each. If ADR 0035 needs
   a follow-up note, add an ADR that amends it — never edit 0035's body.
3. Remove the "Verification pending" admonitions for every path that passed; keep them, with the
   observed failure, for any that didn't. Fill the ai-client-qa.md evidence table.
4. Claim: README.md (the "MCP-compatible AI assistant" intro + the architecture diagram's client
   list), website/index.html (client list / FAQ, wherever Claude is named as the client), and a
   CHANGELOG [Unreleased] "Added" line — wording per decision D3.
5. Tick the finished checkboxes in #391 (Route A) and #392 and close #392 if all of it passed.
Run /dod; one PR "feature: verified ChatGPT (Developer Mode) and Gemini CLI support"; drive green.
```

**★ GATE A is passed when:** WP 2.1 is merged to `main` (`pages.yml` then deploys the website).
**This is the earliest point you can say "works with ChatGPT and Gemini".**

---

### 🤖 WAVE 3: finish ChatGPT (up to 3 parallel sessions) → ★ GATE B

Depends on: GATE A. WP 3.1 also needs M1.4's result.

#### WP 3.1: ChatGPT desktop (#390): run the branch that matches M1.4's result

- **(a) Desktop accepts a remote `url` with custom headers.** Docs only. Add a `mcp_config.json`
  snippet using `url` plus `Authorization: Bearer <token>` to `getting-started.md`, the same shape
  as Gemini CLI. No shim involved.
- **(b) Desktop supports stdio only.** This is packaging work. Install `shim.js` at a stable path
  from all three installers:
  - macOS: `PrivacyFenceApp.app/Contents/Resources/shim/shim.js`, via `build_dmg.sh`/`build_pkg.sh`
  - `.deb`: `/usr/lib/privacyfence/shim/shim.js`
  - Windows: `{app}\shim\shim.js`

  Extend each packaged smoke test to assert the file exists and answers `initialize` over stdio.
  Document an **absolute `node` path** in the snippet (correction 5). Verify by dispatching
  `build.yml` against the branch (steward table). → **ADR** ("the shim is also shipped outside the
  `.mcpb`").
- **(c) Desktop has no MCP support, or it's unusable.** Post a findings comment on #390 and close
  it as "not planned — revisit when…". Change the docs note to point ChatGPT users at org mode.

**Session prompt:**

```text
Read docs/ai-client-support-plan.md and the maintainer's M1.4 result on #390. Implement WP 3.1,
branch (a), (b) or (c) as the plan describes — only the branch matching that result. For (b):
dispatch build.yml against your branch and confirm build, build-windows and build-deb go green
before asking for review; write the ADR. Run /dod; one PR; drive to green.
```

#### WP 3.2: ChatGPT workspace-admin publish guide (#391 Route B) · `chore:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md and WP 1.4's findings on #391. Implement WP 3.2 as docs:
a "ChatGPT Business/Enterprise/Edu (workspace-published connector)" subsection in
org-mode-setup-guide.md next to §8 step 3, containing: the admin flow (enable custom MCP
connectors → create → Scan Tools → test as draft → publish), an explicit callout box that
enabling or disabling a PrivacyFence connector org-wide requires the ChatGPT workspace admin to
re-open and RE-PUBLISH the connector (PrivacyFence's tool list is dynamic, ChatGPT's published copy
is frozen), and the double-consent note. Mark it "Verification pending (#391 Route B)". Add a row
to docs/ai-client-qa.md. CHANGELOG [Unreleased] line. Run /dod; one PR; drive to green.
```

#### WP 3.3: T3 Gemini CLI org-mode OAuth in CI (#392) · `tests:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md §2. Implement WP 3.3: extend tests.yml's org-mode-smoke job
(tests/integration/test_org_ubuntu_release_smoke.py harness + tests/integration/mock_idp.py) with a
Gemini CLI run against the org daemon: settings.json httpUrl with no headers, drive the OAuth
browser leg headlessly (first try the BROWSER env var pointing at a script that follows the
redirect chain with a cookie jar; fall back to Playwright/Chromium, already on the runner image
via PLAYWRIGHT_BROWSERS_PATH locally — install it in CI if needed), then `gemini mcp list` must
show connected. Assert the DCR client landed in oauth_clients.json and its client_name maps to the
gemini-cli agent. Also add it to ai-client-canary.yml with @latest. Run /dod; one PR; drive green.
```

**★ GATE B is passed when:** WP 3.1 and WP 3.2 are merged, and 🧑 M3.1 (and M3.2 if you took
branch (b)) passed and are logged. WP 2.1's claim can then add "ChatGPT Business/Enterprise
workspace connector" and, if it applies, "ChatGPT desktop". Do that as a small docs PR, or ask the
next wave's session to include it.

---

### 🤖 WAVE 4: Gemini Enterprise, the only code change (#393) → ★ GATE C

Depends on: 🧑 M4.1 (Google's requirements confirmed). If Google turns out to support DCR,
**stop**: #393 becomes docs only, the same shape as WP 3.2.

#### WP 4.1: ADR, admin OAuth-client registration, tests and docs · `feature:`

**Session prompt:**

```text
Read docs/ai-client-support-plan.md (esp. §1 point 8 and decision D4) and the maintainer's M4.1
findings on #393. Implement WP 4.1:
1. ADR (next free number): "Pre-registered OAuth clients for clients that cannot use DCR" —
   decision D4, the stale-prune exemption, secret storage matching the SDK, why the command
   refuses while the daemon holds its lock, and the rejected alternatives (hand-editing the JSON;
   a live /settings action — deferred, not rejected).
2. OrgOAuthProvider.register_static_client(client_info, *, source="admin") and
   remove_static_client(client_id); _StoredClient grows `source` ("dcr" | "admin", default "dcr",
   backward-compatible load of both existing on-disk formats); _prune_stale_clients_locked skips
   source=="admin"; the 2000-client cap still applies.
3. `privacyfence-app --register-oauth-client --name "Gemini Enterprise" --redirect-uri <uri>
   [--client-id ...] [--token-endpoint-auth-method client_secret_post|client_secret_basic]`
   plus `--list-oauth-clients` and `--remove-oauth-client <id>`: org mode only; refuse with a clear
   message while the single-instance lock is held; generate client_id/secret with `secrets` when not
   given; print them ONCE to stdout; never log the secret.
4. Unit tests in tests/unit/web/test_oauth_provider.py (+ daemon_main CLI tests): hand-seeded and
   DCR clients are indistinguishable to get_client/authorize/token; admin clients survive pruning;
   old-format files still load; lock-held refusal; secret never in logs. Add a T2 replay fixture
   tests/fixtures/ai_clients/gemini-enterprise/ that drives /authorize → /token with the
   pre-registered client and client_secret auth.
5. agent_identity REGISTRY: add "gemini-enterprise" keyed on the client_name the command sets.
6. org-mode-setup-guide.md: new "Clients that can't self-register" section, Gemini Enterprise as
   the worked example (authorization URL https://<host>/authorize, token URL https://<host>/token,
   redirect URI from M4.1), marked "Verification pending (#393)". ai-client-qa.md row. CHANGELOG.
Run /dod; one PR "feature: pre-registered OAuth clients (Gemini Enterprise)"; drive to green.
```

**★ GATE C is passed when:** WP 4.1 is merged, a release carrying it is installed on your org
deployment, and 🧑 M4.2 passed. Then send a small docs PR that removes the admonition and adds
"Gemini Enterprise" to the claim.

---

### 🤖 WAVE 5: Gemini app / Spark (#394, low priority)

#### WP 5.1: research · no PR

```text
Read docs/ai-client-support-plan.md and #394. Re-source Spark's custom-app MCP requirements from
Google-owned pages (DCR vs pre-registration, TLS, transport, whether it can reach a self-hosted
host). Comment findings on #394 with a recommendation: (i) smoke-test (say which: DCR path, or the
WP 4.1 admin client) or (ii) close as not a target audience. Don't write code.
```

Then do 🧑 M5.

### 🤖 WAVE 6: retire this plan

```text
Read docs/ai-client-support-plan.md. Every issue #390–#394 is closed. For every "→ ADR" marker in
the plan, confirm an ADR exists (write any missing one); then delete this plan file in the same PR
and say in the PR description which ADRs carry its decisions (CLAUDE.md "Retiring a plan").
```

---

## 6. Issue closure map

| Issue | Closed by | Evidence it needs |
|---|---|---|
| #392 | WP 2.1 (+ WP 3.3 for CI) | M1.1, M1.2 logged; T2 and T3 green |
| #391 | WP 3.2 docs PR, verified | M1.3 (Route A) and M3.1 (Route B) logged |
| #390 | WP 3.1 (a/b) or closed via (c) | M1.4 (+ M3.2 for branch b) |
| #393 | WP 4.1 and the Gate C docs PR | M4.1, M4.2 logged |
| #394 | WP 5.1 → M5 | comment with a decision |

---

## 7. 🧑 Your manual tasks: step by step

Vendor menu names below were current as of 2026-09. OpenAI and Google rename these often. If a
menu doesn't match, look for the nearest equivalent, and **write the real path in your evidence
comment** so WP 2.1 can put it in the docs.

**Evidence to post after every test** (as a comment on that test's issue, for example
"M1.1 result"):

- client name and version, OS, date, PrivacyFence version (`privacyfence-app --version`, or the
  About box)
- pass or fail for each step, and screenshots of anything that failed
- **the agent name PrivacyFence recorded:** open `/approvals` → the entry for your test call, or
  the audit log under the data directory's `logs/audit/`, and copy its `agent_*` / `client_info`
  fields
- **org mode only:** the new client's entry from `oauth_clients.json` in the org data directory
  (`org/oauth_clients.json`). **Replace the `client_secret` value with `REDACTED` before pasting.**
  Keep `client_name`, `redirect_uris`, `grant_types` and `token_endpoint_auth_method`.

### M0: before Wave 1

**M0.1 Confirm decisions D1–D4** (5 min)
1. Read §3.
2. Reply in the session with "D1–D4 as recommended", or give your changes.
3. Also answer the open question in the "AI agent protocol stability" session
   (<https://claude.ai/code/session_019R3xZ8EhKqmqmygckY8KEo>): "docs location → D2 from
   docs/ai-client-support-plan.md", so the two plans don't diverge.

**M0.2 An org-mode deployment on public HTTPS** (needed for M1.2, M1.3, M3 and M4; 1–3 h if you
don't have one)

ChatGPT's and Gemini's cloud-side clients have to reach `/mcp` from the internet, over a certificate
from a public CA.
1. Get a small Ubuntu 24.04 VM from any provider, with 2 vCPU and 2 GB RAM.
2. DNS: at your registrar, open DNS → Records → Add record and create **A** `pf-test.<your-domain>`
   pointing at the VM's IP.
3. Follow [`org-mode-setup-guide.md`](org-mode-setup-guide.md) §2 → §8 in order. §4.1 is the
   OIDC sign-in client: in Google Cloud Console go to **APIs & Services → Credentials → + Create
   credentials → OAuth client ID → Web application**, and set the redirect URI exactly as §4.1
   says. §6 is Caddy + Let's Encrypt, which gives you the certificate Spark and ChatGPT need.
4. Check it from your laptop: `curl -s https://pf-test.<your-domain>/.well-known/oauth-authorization-server | head`
   must return JSON that lists `registration_endpoint`.
5. On `/connect`, connect at least **Google Calendar**, so the tests have one read tool and one
   gated write (`calendar_create_event`).

**M0.3 Accounts and installs** (15 min)

| Need | For | How |
|---|---|---|
| Node 20+ on your Mac | M1.1, M1.2 | `brew install node` |
| Gemini CLI | M1.1, M1.2 | `npm install -g @google/gemini-cli`, then run `gemini` once and choose **Login with Google** |
| ChatGPT **Plus/Pro** (personal) or a Business seat | M1.3 | <https://chatgpt.com/#pricing> |
| ChatGPT desktop for macOS | M1.4 | <https://openai.com/chatgpt/desktop/> → Download for macOS |
| ChatGPT **Business/Enterprise workspace, as admin** | M3.1 | <https://chatgpt.com/team> (Business trial is enough) |
| Google Cloud project with **Gemini Enterprise** | M4 | <https://console.cloud.google.com/gemini-enterprise> (trial is enough) |

### M1: after Wave 1 is merged (about 2 h in total). Do them in this order; each one alone is enough for a partial claim.

**M1.1 Gemini CLI, local mode** (#392, 15 min)
1. Make sure PrivacyFence (packaged, local mode) is running on your Mac.
2. In Terminal:
   ```bash
   PF_HANDOFF="/Library/Application Support/PrivacyFence/handoff"
   PF_APP="/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app"
   gemini mcp add --transport http privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
     --header "Authorization: Bearer $("$PF_APP" --print-mcp-token)"
   ```
   If `gemini mcp add` doesn't exist in your version, edit `~/.gemini/settings.json` by hand, as
   shown in `getting-started.md` (WP 1.3).
3. Run `gemini`, then type `/mcp`. **Expected:** `privacyfence` is 🟢 connected and its tools are
   listed.
4. Prompt: `Use privacyfence to list my calendar events for tomorrow.` **Expected:** the tool runs,
   and it shows in `/approvals` if the gate requires review.
5. Prompt: `Use privacyfence to create a calendar event "PF test" tomorrow 10:00–10:15.`
   **Expected:** Gemini CLI asks you to allow the tool (its own prompt), then the **PrivacyFence
   approval dialog** appears. Approve it, and confirm the event exists in Google Calendar.
6. Upload test (ADR 0028): `Use privacyfence to upload ~/Desktop/test.txt to my Drive root` (needs
   Drive connected). **Expected:** it uses `privacyfence_create_upload_slot` and a `PUT`.
7. Post evidence on **#392**, titled "M1.1 result", and include the agent name recorded for these
   calls.

**M1.2 Gemini CLI, org mode** (#392, 15 min; needs M0.2)
1. `gemini mcp add --transport http privacyfence-org https://pf-test.<your-domain>/mcp` (no header).
2. `gemini` → `/mcp auth privacyfence-org`. **Expected:** a browser opens at PrivacyFence `/login`,
   goes on to Google sign-in, and comes back to "authentication successful".
3. `/mcp`: **expected** 🟢 connected. Then repeat M1.1 steps 4–5 against this server.
4. Post evidence on **#392**, titled "M1.2 result", with the `oauth_clients.json` entry (secret
   redacted).

**M1.3 ChatGPT Developer Mode, org mode** (#391 Route A, 30 min; needs M0.2)
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
   - If OAuth finishes but the app doesn't appear, try once more before reporting it. This is a
     known ChatGPT flakiness (#391).
4. New chat → the **+** in the message box → **More** → **Developer mode** → enable **PrivacyFence**
   for this chat. (You have to do this in every new chat.)
5. Prompt: `List my calendar events for tomorrow.` **Expected:** the tool call succeeds.
6. Prompt: `Create a calendar event "PF test" tomorrow 10:00–10:15.` **Expected:** ChatGPT's own
   **Confirm** box appears first, then the PrivacyFence approval (open `https://pf-test.<your-domain>/approvals`).
   Screenshot both; they're the "double consent" in the docs.
7. Post evidence on **#391**, titled "M1.3 result (Route A)", with the `oauth_clients.json` entry
   (secret redacted) and the agent name recorded.

**M1.4 ChatGPT desktop check** (#390, 15 min)
1. Open ChatGPT desktop → **ChatGPT** menu (menu bar) → **Settings…** Look for an **MCP servers**
   (or **Connectors / Apps → Advanced**) page with **Add server**.
2. Write down exactly what you find:
   - (a) you can add a **URL** and set **custom headers**
   - (b) you can add only a **command** (stdio)
   - (c) there's no MCP page at all
   Also check whether `~/Library/Application Support/ChatGPT/mcp_config.json` exists
   (`ls ~/Library/Application\ Support/ChatGPT/`).
3. If (a): add URL `http://127.0.0.1:8765/mcp` (use the value in your `mcp_url` file) with the header
   `Authorization: Bearer <output of --print-mcp-token>`, and repeat M1.1 steps 4–5.
4. If (b): **don't** point it at Claude's extension folder. Post the result, and WP 3.1(b) ships a
   stable `shim.js`.
5. Post evidence on **#390**, titled "M1.4 result: (a)/(b)/(c)", with screenshots of the settings
   page.

→ Then run **WAVE 2**.

### M2: after Wave 2 (Gate A, 10 min)
1. Review and merge WP 2.1's PR: GitHub → **Pull requests** → the PR → **Files changed** →
   **Review changes** → **Approve** → **Merge pull request** (merge commit, not squash).
2. Check the website after `pages.yml` finishes: GitHub → **Actions** → **pages** → latest run is
   green. Then open <https://privacyfence.eu> and confirm the new client list.
3. The claim is public. It ships in the next release's notes from `CHANGELOG.md` (`/cut-release`
   when you're ready).

### M3: after Wave 3

**M3.1 ChatGPT workspace-published connector** (#391 Route B, 45 min; needs a Business/Enterprise
workspace where you're admin)
1. <https://chatgpt.com> → profile icon → **Workspace settings** (or <https://chatgpt.com/admin>)
   → **Permissions & roles** → turn on **Developer mode / Create custom MCP connectors** for
   admins.
2. Same place: under the **Apps** / **Connectors** permission, allow members to use custom apps,
   or scope it to one test group.
3. **Workspace settings** → **Apps** (or **Connectors**) → **Create** → MCP URL
   `https://pf-test.<your-domain>/mcp`, Authentication **OAuth** → **Scan tools**.
   **Expected:** the scan accepts all tools. Screenshot any warnings.
4. **Test as draft** in a chat, and run M1.3 steps 5–6.
5. **Publish** to the workspace. Then, signed in as a **non-admin member** with Developer Mode off,
   confirm PrivacyFence is available in a chat.
6. **Frozen-snapshot test:** on `/connect`, connect one *more* PrivacyFence connector (for example
   Google Tasks). In a member chat, **expected:** its tools do **not** appear. As admin: **Apps** →
   PrivacyFence → **Update / Re-publish**. **Expected:** now they do.
7. Post evidence on **#391**, titled "M3.1 result (Route B)".

**M3.2 ChatGPT desktop retest** (only if WP 3.1 took branch (b))
1. Install the release or pre-release that carries WP 3.1: `/cut-release` with an `aN` tag, then
   <https://privacyfence.eu/download/> → "Want to test the next version?".
2. In ChatGPT desktop → **Settings** → **MCP servers** → **Add server** → command = the absolute
   `node` path from the new `getting-started.md` snippet (`which node`), args = the documented
   `shim.js` path.
3. Quit PrivacyFence first, so you test that the shim starts the daemon. Then repeat M1.1 steps 3–5.
4. Post evidence on **#390**, titled "M3.2 result".

### M4: Gemini Enterprise

**M4.1 Confirm Google's requirements before WAVE 4** (20 min)
1. Open <https://cloud.google.com/gemini/enterprise/docs> and search for **"custom MCP server"**,
   or use the URL from issue #393's source 7.
2. Write down:
   - does it support DCR? (If **yes**, tell the session; WAVE 4 becomes docs only.)
   - the exact **redirect URI**
   - which fields it asks for (authorization URL, token URL, scopes, client ID, client secret)
   - the client authentication method (`client_secret_post` or `_basic`)
   - the console menu path
3. Post it on **#393**, titled "M4.1 requirements", with links. Then run **WAVE 4**.

**M4.2 Register and verify** (45 min; after WP 4.1 ships in a release installed on your org VM)
1. On the VM: `sudo systemctl stop privacyfence-org` (the command refuses to run while the daemon
   is up).
2. `sudo -u privacyfence /opt/privacyfence/venv/bin/privacyfence-app --register-oauth-client --name "Gemini Enterprise" --redirect-uri https://vertexaisearch.cloud.google.com/oauth-redirect`
   Use the URI from M4.1 if it's different. Copy the printed `client_id` and `client_secret`
   **now**; they're shown only once.
3. `sudo systemctl start privacyfence-org`
4. <https://console.cloud.google.com/gemini-enterprise> → select your project → **Apps** → your
   app → **Connected data stores** (or **Data stores / Actions**) → **+ Add** → **Custom MCP
   server**. Fill in:
   - Server URL `https://pf-test.<your-domain>/mcp`
   - Authorization URL `https://pf-test.<your-domain>/authorize`
   - Token URL `https://pf-test.<your-domain>/token`
   - Client ID and Client secret from step 2
   - scopes as M4.1 found

   → **Connect/Authorize**. **Expected:** PrivacyFence `/login` → Google → back to the console
   showing it connected.
5. In the Gemini Enterprise web app, run M1.3 steps 5–6.
6. Post evidence on **#393**, titled "M4.2 result". → Gate C docs PR.

### M5: Spark (after WP 5.1)
1. If WP 5.1 recommends a smoke test: in the <https://gemini.google.com> app → **Settings** →
   **Apps / Connected apps** → **Add custom app** (menu per WP 5.1's findings), URL
   `https://pf-test.<your-domain>/mcp`, and repeat M1.3 steps 5–6.
2. Post the result on **#394**, or close the issue as "not a target audience" if WP 5.1
   recommended that.
