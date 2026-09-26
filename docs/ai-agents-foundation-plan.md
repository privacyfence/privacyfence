# Plan 1 of 3: AI agents foundation, applied to Claude

> **Temporary plan document** (see [`adr/README.md`](adr/README.md)): delete it in the PR that
> finishes its last wave (WP 4.1). Before deleting it, move every decision marked **→ ADR** below
> into an ADR. It is listed in `scripts/build_site.py`'s `CONTRIBUTOR_DOCS` and in the contributor
> half of [`README.md`](README.md) so the docs allowlist (guardrail 5) accepts it; the retiring PR
> removes both entries.

**The three plans:**

1. **This plan.** It builds everything every AI client needs and proves it on the clients that
   already work (Claude Desktop, Claude Code, claude.ai):
   - the website's **AI agents** menu, and one docs page per agent;
   - truthful tool annotations, with an organization-bundle switch to advertise every tool
     read-only ([issue 46](https://github.com/privacyfence/privacyfence/issues/46));
   - the AI-client test tiers (schema portability, recorded-handshake replay, a real client in CI,
     and the manual checklist), with Claude fixtures.
2. [`chatgpt-support-plan.md`](chatgpt-support-plan.md): ChatGPT
   ([issue 390](https://github.com/privacyfence/privacyfence/issues/390),
   [issue 391](https://github.com/privacyfence/privacyfence/issues/391)). It starts once this plan
   is finished.
3. [`gemini-support-plan.md`](gemini-support-plan.md): Gemini
   ([issue 392](https://github.com/privacyfence/privacyfence/issues/392),
   [issue 393](https://github.com/privacyfence/privacyfence/issues/393),
   [issue 394](https://github.com/privacyfence/privacyfence/issues/394)). It also starts once this
   plan is finished. Plans 2 and 3 can run at the same time.

Plans 2 and 3 only add clients: a fixture, a docs page, a website page, and a `clients.json` entry
each. Everything they build on is here.

*Split from the combined AI-client plan and aligned to `main` at `ab07ab0d` (4.7.0) on
2026-09-26.*

---

## 0. How to use this plan

There are two kinds of step:

- **🤖 WAVE n** means Claude sessions do the work. Each wave holds **work packages** (WP n.m).
  Packages in one wave don't depend on each other, so each gets its own session and its own PR,
  and they run at the same time.
- **🧑 M n.m** means **you** do it by hand, because it needs an account, a real app, a release,
  or a decision.

This plan doesn't use `/implement`'s `## Implementation manifest`. Its gates are releases and
manual checks, so each package merges on its own (D10).

### Running a wave

Open a new Claude Code on the web session on `privacyfence/privacyfence` and paste:

```text
Implement WAVE <n> of docs/ai-agents-foundation-plan.md (on main; if it isn't merged yet, read it
from branch claude/bold-fermat-xlsdy4).

You are the coordinator. Do not implement anything yourself. For each work package in that wave:
1. Create one child session with create_session. Give it the package's "Session prompt" word for
   word, plus the plan's §1 "Corrections" and §2 "Testing strategy" sections as context.
2. Tag every child session "ai-agents-foundation:wave-<n>".
3. Track each child until its PR is open and CI is green. Use get_session, and check back with
   send_later about once an hour. Don't poll in a tight loop.
4. When every package's PR is green, post a checklist here: WP, PR link, CI state, and any
   manual (🧑) task that is now unblocked.
Only start a package whose "Depends on" items are all done (merged PRs, or 🧑 tasks I've reported
as done in this chat).
```

Every child session follows these rules. They already reach it through `CLAUDE.md` and the
`steward` skill, and are repeated here so you can check:

- One new branch and **one PR per package**. Put the `<type>` in the PR title (`feature:`,
  `fix:`, `chore:`, `tests:`), because the `claude/*` branch name can't carry it.
- Done means `/dod` (the §2.7 definition of done) passes and CI is green.
- User-visible changes get a line under `CHANGELOG.md`'s `## [Unreleased]`. Never add a version
  heading.
- Anything that needs a Mac, Windows or real credentials is **dispatched** as a workflow (see the
  `steward` skill). It's never skipped. A workflow can be dispatched only once it's on `main`.
- **ADR numbers:** take the next free number *at merge time*. `feature/org-mode-mobile` already
  claims 0078–0080, so the first ADR from this plan is most likely 0081. Renumber on conflict.
- Issues are cited by full URL in docs and test code, never as `#NNN` (item 9 in §1).

---

## 1. Corrections: what `main` does today

Checked against `main` at `ab07ab0d` on 2026-09-26.

1. **Local-mode token.** Packaged installs are privilege-separated
   ([ADR 0008](adr/0008-one-principal-per-os-user.md)). The token comes from `--print-mcp-token`,
   and the URL comes from the handoff directory's `mcp_url` file. The binary name is different on
   each platform:
   - macOS: `/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp`
   - Linux: `privacyfence-app`
   - Windows: the installed exe. It has no console, so redirect its output to a file.

   Each `install-*.md` → "Connect Claude Code" has the exact command.
2. **Client setup is in three places today.** WP 1.5 gives each client its own page instead:
   - `install-macos.md`, `install-windows.md`, `install-linux.md` → "Connect Claude Desktop" /
     "Connect Claude Code"
   - `how-it-works.md` → "How an AI system connects"
   - `org-mode-setup-guide.md` §9 → "Add PrivacyFence to an AI client"

   Other relevant org guide sections: §4 Identity provider, §7 Reverse proxy and TLS, §8 systemd
   unit, §11 AI systems (admin pins), §12 Approvals and step-up, §13 File delivery.
3. **QA docs.** [`connector-qa.md`](connector-qa.md) covers live *connector* QA, not clients.
   [`release-testing.md`](release-testing.md) → "What stays manual" admits only one real MCP
   client (Claude Desktop with the `.mcpb`). WP 1.4 amends that on purpose (D2).
4. **Tool annotations today** ([ADR 0076](adr/0076-every-connector-tool-is-advertised-read-only.md)):
   - `web/mcp_tools.py`'s `to_mcp_tool` gives every connector tool
     `_UNIFORM_READ_ONLY_ANNOTATIONS` (`readOnlyHint=true, destructiveHint=false,
     idempotentHint=true`), whatever `ToolSpec.read_only` says.
   - Meta-tools already carry truthful per-tool annotations.
   - The tests pinning the uniform triple are `test_mcp_tools.py`'s
     `test_every_tool_is_advertised_read_only_regardless_of_the_specs_own_flag` and
     `test_routes_mcp.py`'s `test_connector_tool_is_advertised_uniformly_read_only`.
   - `ToolSpec` only has `read_only`. It has no destructive or idempotent flag.
   - D5 reverses the default.
5. **Agent attribution** (`src/privacyfence/agent_identity.py` `REGISTRY`,
   [ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)):
   - The Claude entries are `claude-code` ← `claude-code` and `claude` ← `claude-ai`. ADR 0035
     marks `claude-ai` a guess.
   - An unmatched name is recorded as `agent_id="unknown:<name>"` (`client_info`). So the real
     name is readable from any audit entry.
   - **In org mode the DCR registration's `client_name` takes precedence over the handshake's
     `clientInfo.name`** (`web/routes_mcp.py`). The registry has to match both.
   - An admin **pin** on org **Settings → AI systems** is the only attested source
     (`agent_source=oauth_client`).
   - Every `REGISTRY` entry needs an icon in `src/privacyfence/resources/agent_icons/` plus a
     license row (`test_agent_label.py`).
6. **OAuth client store** (`web/oauth_provider.py`):
   - Records are `{"client", "last_used_at"}`.
   - Stale clients (unused for 180 days) are pruned on each `/register`, and the store is capped
     at 2000 clients.
   - The store is held in memory and rewritten on every `get_client`.
   - T2 replays go through this in-process.
7. **The replay harness.** `tests/unit/web/test_org_mcp_e2e.py` has private helpers
   (`_register_client`, `_authorize_and_get_code`, `_exchange_for_tokens`, `_mcp_session`), and
   there's no `tests/unit/web/conftest.py` yet.
8. **CI.** `tests.yml`'s `test` job sets up Node 22 and Playwright. `org-mode-smoke` has no Node.
   `tests/integration/test_shim_mcp_contract.py` and `test_mcp_daemon_contract.py` are the models
   for a real-client contract test. Neither a `tests/integration/ai_clients/` directory nor an
   AI-client canary workflow exists yet.
9. **No project history in docs or test code.**
   [ADR 0056](adr/0056-code-carries-no-project-history.md): `test_docs_no_history.py` rejects
   `#\d{3}` in published and contributor docs, and `test_code_no_history.py` covers test files
   (`tests/fixtures/` is exempt). Any `docs/*.md` path named anywhere must exist
   (`test_docs_references_exist.py`). That's why this plan writes docs that don't exist yet
   (`connect-<slug>.md`, `ai-client-qa.md`) without the `docs/` prefix.
10. **Approvals.** In a privilege-separated local install, only a companion-attested browser
    session may approve ([ADR 0062](adr/0062-only-a-companion-attested-session-may-approve.md)).
    Use the companion's **Open Approvals**. Org mode approves at `/approvals` after IdP sign-in.
11. **The website** (`scripts/build_site.py`):
    - Hand-written pages are listed in `PAGES`.
    - The header partial has two nav lists that must be equal (`test_build_site.py`).
    - `website/_data/clients.json` is the one list of tested clients. It renders the "Works with"
      strip (`/`, `/enterprise/`, `/faq/`) and the JSON-LD.
    - Guardrail 10 (`test_website_clients.py`) hard-codes `NAMES` and bans ChatGPT, Gemini,
      Copilot and Cursor from hand-written pages.
    - Guardrail 8 holds connector pages to connector modules.
    - `/docs/` is built **from the latest stable tag**, and the link check (guardrail 6) fails on a
      link into a doc that tag doesn't have.
    - `build_site.content_group()` maps doc stems to GA4 content groups: `install-*` →
      `platform`, the setup guides → `connector`, and everything else → `docs`.
    - The guardrail numbers in use go up to 13.

---

## 2. Testing strategy (the AI-client test tiers)

These four tiers come from the "AI agent protocol stability" session. This plan builds all four,
and plans 2 and 3 add their clients to them.

| Tier | What | Runs where | Catches | Built in |
|---|---|---|---|---|
| **T1** Static portability | A unit test over every advertised tool (connector and `privacyfence_*`). Checks: input schema root is `type: object`; no `$ref`/`$defs`/`definitions` and no tuple-form `items`; name matches `^[a-zA-Z0-9_-]{1,64}$` (the strictest client limit, OpenAI's); non-empty description of bounded length; every tool carries all three hints. | every PR | A new tool that some client would reject or rename | WP 1.2 |
| **T2** Recorded-handshake replay | Fixtures under `tests/fixtures/ai_clients/<client>/` hold each client's real DCR `/register` body, `clientInfo`, redirect URIs, `token_endpoint_auth_method` and request order. They're replayed in-process through the org OAuth + `/mcp` stack. Asserts attribution through the DCR `client_name` path and the handshake path, unpinned (`client_info`) and pinned (`oauth_client`), and the annotations the client receives in both annotation modes. | every PR | A server-side change that breaks a client CI can't run (claude.ai, and later ChatGPT and Gemini Enterprise) | WP 1.2 (seeded), WP 2.1 (captured) |
| **T3** Real client in CI | The pinned client CLI, installed from a committed lockfile, lists tools against a real local-mode daemon. A weekly canary does the same with `@latest`. Here that's Claude Code (`claude mcp list`). Plan 3 adds Gemini CLI to the same harness. | PR (pinned), plus weekly (`@latest`) | The client changing on its side | WP 1.3 |
| **T4** Manual pre-release check | A per-client script and evidence table in the new contributor doc `ai-client-qa.md`, with a pointer from `release-testing.md` → "Human checks". Runs when `/mcp`, OAuth, `routes_mcp.py`, `mcp_tools.py` or `agent_identity.py` change. | before a release, by a human | What can't be automated (claude.ai, Claude Desktop's UI prompts) | WP 1.4 (template), 🧑 M1 fills it |

The rule that ties them together: **each manual verification (T4) ends by saving a T2 fixture**.

---

## 3. Decisions (🧑 M0.1): confirmed 2026-09-26

| # | Decision | What was decided |
|---|---|---|
| D1 | Per-client setup docs | **One published docs page per AI agent**, like the platform pages (`install-<os>.md`) and connector guides (`<name>-setup.md`): `connect-<slug>.md`. This plan creates `connect-claude-desktop.md`, `connect-claude-code.md` and `connect-claude-ai.md`. **Why: monitoring.** Separate pages show which agents users are interested in. Each is its own GA4 page path, and `build_site.content_group()` gives every `connect-*` stem the content group **`ai-agent`** (as `install-*` is `platform`). The website's `/ai-agents/<slug>/` pages carry the same group. They're listed in a new `docs/README.md` section **"AI agent setup"**, after "Connector setup". → **ADR** (with D8) |
| D2 | AI-client checklist and evidence log | New **contributor** doc `ai-client-qa.md`, shaped like `connector-qa.md`'s "Recording results". It also holds the *test org deployment* recipe and the *evidence format* that plans 2 and 3 reuse. Registered in `CONTRIBUTOR_DOCS` and `docs/README.md`'s contributor half, with a pointer bullet in `release-testing.md` → "Human checks". "What stays manual" is amended from "one real MCP client" to "one real client per supported client family, run only when the client-facing surface changed". |
| D5 | Tool annotations | **Truthful by default; an organization bundle can switch every tool to read-only.** Default: a read tool is advertised `readOnlyHint=true`, a write tool `readOnlyHint=false`, derived from `ToolSpec.read_only`. New bundle option `--tool-annotations {truthful,all-read-only}` (key `mcp.tool_annotations`, not written = `truthful`), applying in both modes like `unattended_sessions`. `all-read-only` restores ADR 0076's uniform triple for organizations whose client would otherwise prompt on every write (claude.ai Team, see ADR 0076's context). This **changes today's behaviour** for every client and **resolves issue 46**. → **ADR superseding 0076** (WP 1.1) |
| D6 | Unverified instructions on `main` | No. Anything merged ships in the next release's `/docs/`. Draft client docs wait in a draft PR until the manual check passes. |
| D7 | How CI gets a client CLI | A committed `tests/integration/ai_clients/package.json` + `package-lock.json`, installed with `npm ci` (integrity-checked). The canary alone uses `npx …@latest`. → **ADR** |
| D8 | Website menu | Top-nav **"AI agents"** → `/ai-agents/`, one page per `clients.json` entry at `/ai-agents/<slug>/`. This plan adds `claude-desktop`, `claude-code` and `claude-ai`. |
| D9 | Website timing | A client's `/ai-agents/` page and `clients.json` changes land **after the stable release whose `/docs/` carries its `connect-<slug>.md`**. Otherwise the link check fails against the stable tag's docs. So the order is: docs PR → release → website PR. → **ADR** |
| D10 | Execution model | One PR per package, the coordinator prompt in §0. No `/implement` manifest. |
| D11 | Logos | No third-party logos on the website. Text only, like the connector pages. |
| D12 | Land the plans on `main` first | Yes, as one docs-only PR carrying all three plans. |

Decisions D3 and D4 moved to plans 2 and 3.

### Still open

| # | Question | Recommendation |
|---|---|---|
| E1 | Should a local install **without** a bundle also be able to pick `all-read-only` (a `settings.yaml` key)? | **No.** It stays an organization choice, made once and signed in the bundle. A single user on Claude Desktop can use Claude's own "Always allow" for a tool. Add the key later if users ask. |
| E2 | How truthful should writes be, given `ToolSpec` has only `read_only`? | Write tools get `readOnlyHint=false`, `destructiveHint=true` and `idempotentHint=false`, which are MCP's own defaults for a non-read-only tool. Don't add per-tool `destructive` flags now. The gate tables already say what each write does, and finer hints can come later without another decision. |

---

## 4. Roadmap at a glance

```text
🧑 M0  decisions (done except E1/E2) + a test org deployment on public HTTPS
│
🤖 WAVE 1   WP1.1 annotation setting + ADR │ WP1.2 T1+T2 harness (Claude seeds) │ WP1.3 T3 Claude Code + canary
│           WP1.4 ai-client-qa.md │ WP1.5 connect-claude-*.md docs + ai-agent content group
│
🧑 M1  Claude Code local/org, claude.ai org, Claude Desktop local/org: capture + annotation behaviour
│
🤖 WAVE 2   WP2.1 captured fixtures, Claude registry names, ADR amending 0035
│
🧑 M2  cut a stable release (carries WP1.1, WP1.5, WP2.1)
│
🤖 WAVE 3   WP3.1 website: AI agents menu + 3 pages + guardrail 14
│
🤖 WAVE 4   WP4.1 retire this plan → plans 2 and 3 can start
```

---

## 5. Waves (🤖 Claude)

### 🤖 WAVE 1 (5 parallel sessions)

Depends on: M0.1. E1 and E2 are answered before WP 1.1 starts.

#### WP 1.1: truthful tool annotations, org-bundle switch, ADR · `feature:`

**Session prompt:**

```text
Read docs/ai-agents-foundation-plan.md §1 (item 4) and §3 (D5, E1, E2). Implement WP 1.1:
1. ADR (next free number at merge time, ≥ 0081) "Tool annotations are truthful by default; an
   organization bundle can advertise every tool read-only", superseding ADR 0076 (add one Status
   line to 0076 pointing forward; never edit its body). Context: ADR 0076 and
   https://github.com/privacyfence/privacyfence/issues/46. Decision: D5 and E2. Alternatives:
   keep uniform (0076); per-client annotations keyed on agent identity (rejected: the identity is
   a claim, and one daemon serves several clients); a settings.yaml key (E1).
2. web/mcp_tools.py: to_mcp_tool(spec, *, annotations_mode) — "truthful": readOnlyHint =
   spec.read_only; for writes destructiveHint=True, idempotentHint=False; for reads
   destructiveHint=False, idempotentHint=True. "all_read_only": today's uniform triple. Thread the
   mode from the bundle (key mcp.tool_annotations, values "truthful" | "all_read_only", absent =
   truthful; any other value → the daemon refuses to start, like other malformed bundle keys)
   through daemon_main to every tools/list. Meta-tools keep their own annotations in both modes.
   Update the module comment (it cites ADR 0076 and issue 46).
3. scripts/build_org_bundle.py: --tool-annotations {truthful,all-read-only}; not written unless
   given; applies in both modes (like --enable-unattended-sessions). configuration-reference.md
   build-options row; connect-*.md pages don't exist yet — how-it-works.md "What the AI system is
   told" paragraph rewritten for the new default and the switch.
4. Tests: replace the two uniform-read-only tests (test_mcp_tools.py, test_routes_mcp.py) with
   parametrized ones for both modes, including a live /mcp list_tools; bundle parse/refuse tests;
   build_org_bundle tests.
5. CHANGELOG [Unreleased] "Changed": write tools are now advertised as writes; clients may ask
   for their own confirmation before PrivacyFence's; organizations can restore the previous
   behaviour with --tool-annotations all-read-only.
Run /dod; one PR "feature: truthful tool annotations with an org-bundle switch"; drive to green.
Close https://github.com/privacyfence/privacyfence/issues/46 from the PR description.
```

#### WP 1.2: T1 portability lint and T2 replay harness, seeded with Claude · `tests:`

**Session prompt:**

```text
Read docs/ai-agents-foundation-plan.md §1–§2. Implement WP 1.2, tests only (a real bug found →
stop and report it). Cite issues only as full URLs in test code (ADR 0056).
1. T1 — tests/unit/web/test_tool_schema_portability.py over every tool McpDispatcher lists (build
   every connector the way test_mcp_tools.py does) plus the privacyfence_* meta-tools: schema root
   type=="object"; no "$ref"/"$defs"/"definitions"; no tuple-form "items"; name matches
   ^[a-zA-Z0-9_-]{1,64}$; description non-empty and ≤ 1024 chars; all three annotation hints
   present. Do NOT assert annotation values — WP 1.1 owns them. Parametrize by tool name.
2. T2 — factor test_org_mcp_e2e.py's private helpers (_register_client, _authorize_and_get_code,
   _exchange_for_tokens, _mcp_session) into tests/unit/web/conftest.py and use them from both
   files. Fixtures tests/fixtures/ai_clients/<client>/{register.json,initialize.json,README.md}
   for claude-ai (redirect https://claude.ai/api/mcp/auth_callback) and claude-code (loopback
   redirect), each README saying SEEDED FROM VENDOR DOCS, NOT YET CAPTURED (WP 2.1 replaces it),
   citing its sources. tests/unit/web/test_ai_client_replay.py is parametrized over every
   directory under tests/fixtures/ai_clients/ (so plans 2 and 3 add a client by adding a
   directory): /register with the recorded body → /authorize → faked IdP leg → /token →
   initialize with the recorded clientInfo → tools/list → one read tools/call. Assert success and
   attribution: unpinned (client_info, reached through the DCR client_name, which wins over
   clientInfo in org mode) and pinned via the AI-systems pin store (oauth_client). Each fixture's
   README names the expected agent_id, and the test reads it from there.
3. A short tests/fixtures/ai_clients/README.md: how to capture a fixture (see ai-client-qa.md,
   written by WP 1.4), how to scrub it (pf.example.com, dummy ids, no secrets), and the layout.
Run /dod; one PR "tests: AI-client schema portability (T1) and handshake replay (T2)"; drive to
green.
```

#### WP 1.3: T3 real client in CI (Claude Code) and the weekly canary · `tests:`

**Session prompt:**

```text
Read docs/ai-agents-foundation-plan.md §1–§3 (D7). Implement WP 1.3:
1. tests/integration/ai_clients/package.json + package-lock.json pinning @anthropic-ai/claude-code
   (exact version); installed with `npm ci --prefix tests/integration/ai_clients`. Module
   docstrings say how to bump the pin. Plan 3 adds @google/gemini-cli to the same package.json.
2. tests/integration/ai_client_harness.py: start a real local-mode daemon on a free port with a
   throwaway HOME and data dir, mint a token, and yield (mcp_url, token) — modelled on
   test_mcp_daemon_contract.py. tests/integration/test_claude_code_contract.py: write the client's
   config into the throwaway HOME (`claude mcp add --transport http --scope user privacyfence
   <url> --header "Authorization: Bearer <token>"`), run `claude mcp list`, assert privacyfence is
   connected. It must need NO Anthropic credential — if the pinned version needs one even for
   `mcp list`, stop and report it rather than adding a secret. Skip when node/npm is absent.
   CLAUDE_CODE_BIN overrides the binary (the canary uses it).
3. Run it in tests.yml's `test` job (it already sets up Node), unless it costs > 60 s — then its
   own job.
4. .github/workflows/ai-client-canary.yml: weekly + workflow_dispatch, a matrix over clients
   (today: claude-code) running the same test with `npx --yes <package>@latest` as the binary; on
   failure open (or update) one issue per client titled "AI-client canary: <client> @latest broke
   /mcp". Pin actions by SHA. Add the canary to testing-policy.md's layer table.
Run /dod; one PR "tests: Claude Code contract test (T3) and weekly AI-client canary"; drive to
green. After merge, dispatch the canary once (steward: only possible from main).
```

#### WP 1.4: the AI-client QA checklist · `chore:`

**Session prompt:**

```text
Read docs/ai-agents-foundation-plan.md §1–§3 (D2). Implement WP 1.4:
1. New contributor doc docs/ai-client-qa.md (the shape of connector-qa.md's "Recording results"):
   - "Test organization deployment": the recipe in this plan's §6 M0.2, moved here so it outlives
     the plan (plans 2 and 3 point at it).
   - "Evidence to record": the list in this plan's §6, moved here.
   - A per-client script: tools/list; one read; one gated write → approval card; whether the
     client showed its own confirmation first, under both annotation modes (truthful and
     all-read-only, D5); one capability-URL upload and one download (ADR 0028) for clients without
     the shim; the audit entry's agent_* fields; in org mode, pin the registration on Settings →
     AI systems and confirm the card shows the client verified; save a T2 fixture.
   - Evidence table: Client | Version | OS | Mode | Annotations | Date | Result | Registered
     client_name | clientInfo name | Client confirmation? | Fixture captured.
   - Rows for Claude Desktop (extension, local; custom connector, org), Claude Code (local, org),
     claude.ai (org).
2. Register it: build_site.CONTRIBUTOR_DOCS and docs/README.md's contributor half.
3. release-testing.md: a bullet under "Human checks" → "Every platform" pointing at it, and amend
   "What stays manual" per D2.
Run /dod; one PR "chore: AI-client QA checklist"; drive to green.
```

#### WP 1.5: one docs page per Claude client, content group `ai-agent` · `feature:`

**Session prompt:**

```text
Read docs/ai-agents-foundation-plan.md §1 (items 1, 2, 11) and §3 (D1). Implement WP 1.5:
1. New published docs docs/connect-claude-desktop.md, docs/connect-claude-code.md,
   docs/connect-claude-ai.md. Each has the same shape (the template plans 2 and 3 copy):
   title "Connect <client>"; which deployments it works with (claude.ai: organization only, and
   why); "Local mode" with a per-platform table for the token command; "Organization mode"; "Files"
   (Claude Desktop via the extension; the others via capability URLs, ADR 0028); "Confirmations"
   (what the client asks before PrivacyFence's card, and the --tool-annotations bundle switch —
   link configuration-reference.md; if WP 1.1 isn't merged yet, describe today's behaviour and
   leave a line for WP 1.1 to update); "How the client is identified" (claimed vs pinned, link
   how-it-works.md "Which AI system is asking"); "Troubleshooting".
   Consolidate from install-*.md "Connect Claude Desktop/Claude Code", how-it-works.md "How an AI
   system connects" and org guide §9 "Add PrivacyFence to an AI client"; those keep only what is
   platform- or mode-specific plus a link to the agent's page.
2. docs/README.md: new "### AI agent setup" section right after "### Connector setup", listing the
   three pages.
3. scripts/build_site.py content_group(): "connect-" stems → "ai-agent"; extend
   test_website_docs_pages.py's content-group test (connect-claude-code → "ai-agent", and the
   built page's meta). Add the ai-agent group wherever downloads-and-release-kpi.md lists content
   groups.
4. PUBLISHED_DOCS in tests/unit/test_docs_no_history.py gains the three pages.
5. CHANGELOG [Unreleased] "Added": a setup page per AI client.
No website page links them yet (D9: WP 3.1, after the release). Run /dod; one PR "feature: one
setup page per AI client"; drive to green.
```

---

### 🧑 M1: capture the Claude clients (about 1.5 h)

Depends on: WAVE 1 merged, WP 1.1 included, and the test org deployment from M0.2. Follow
`ai-client-qa.md`'s per-client script (WP 1.4) and post the evidence as comments on
[issue 46](https://github.com/privacyfence/privacyfence/issues/46), one comment per client, so WP
2.1 has a single place to read them. Run each client twice:

- with the default bundle (**truthful**): note whether the client asks for its own confirmation
  before a write;
- with a bundle built with `--tool-annotations all-read-only`: the client should not ask.

| Step | Client | Mode | Notes |
|---|---|---|---|
| M1.1 | Claude Code | local | Use the command from `connect-claude-code.md`. |
| M1.2 | Claude Code | org | `claude mcp add --transport http privacyfence https://pf-test.<your-domain>/mcp`, then `/mcp` to sign in. Pin the registration. Save the `oauth_clients.json` entry with the secret redacted. |
| M1.3 | claude.ai | org | Add a custom connector with the `/mcp` URL and **Connect**. On a Team plan, check the write prompt under both annotation modes: this is the exact case ADR 0076 was written for. Pin the registration and save the entry. |
| M1.4 | Claude Desktop | local | The `.mcpb` extension. Note Claude Desktop's own tool prompt under both modes. |
| M1.5 | Claude Desktop | org | A custom connector with the `/mcp` URL. Pin the registration and save the entry. |

---

### 🤖 WAVE 2: fold in the captures (1 session)

#### WP 2.1: captured fixtures, Claude registry names · `tests:`/`fix:`

**Session prompt:**

```text
Read docs/ai-agents-foundation-plan.md and the M1 evidence comments on
https://github.com/privacyfence/privacyfence/issues/46. Implement WP 2.1:
1. Replace the SEEDED claude-ai and claude-code fixtures with the captured ones and add
   claude-desktop (org custom connector) — scrubbed (pf.example.com, dummy ids, no secrets);
   READMEs say "captured <date>, client version <v>".
2. agent_identity.py REGISTRY: set the Claude entries' client_names to every name observed, both
   the DCR client_name and the handshake clientInfo name. Add a Claude Desktop entry if it
   presents a name distinct from claude.ai's, with icon and license row. Unit-test each. Record
   the corrections in a new ADR amending ADR 0035 (next free number); never edit 0035's body.
3. Fill ai-client-qa.md's evidence table. Update the three connect-claude-*.md pages'
   "Confirmations" sections with what M1 observed under each annotation mode.
Run /dod; one PR "fix: Claude clients' observed names, captured handshake fixtures"; drive to
green.
```

Then **🧑 M2**: cut a stable release (`/cut-release`). It must carry WP 1.1, WP 1.5 and WP 2.1, so
that the new `connect-claude-*` pages are on `privacyfence.eu/docs/`.

---

### 🤖 WAVE 3: the AI agents menu (1 session)

Depends on: M2 (the release is published and its `/docs/` is live).

#### WP 3.1: `/ai-agents/`, three agent pages, guardrail 14 · `feature:`

The design is in §5b below.

**Session prompt:**

```text
Read docs/ai-agents-foundation-plan.md §1 (item 11), §3 (D1, D8, D9, D11) and §5b. The latest
stable release's /docs/ carries connect-claude-desktop, connect-claude-code and connect-claude-ai.
Implement WP 3.1. Do not name ChatGPT, Gemini, Copilot or Cursor anywhere on the website
(guardrail 10).
1. website/_data/clients.json: give every entry a "slug" (claude-desktop, claude-code, claude-ai).
   Its docs page is /docs/connect-<slug>/ by convention. build_site.render_clients() renders each
   strip item as a link to /ai-agents/<slug>/; load_clients() validates slugs.
2. website/ai-agents/index.html and website/ai-agents/<slug>/index.html in the connector pages'
   structure and classes, with the sections in §5b. meta pf-content-group "ai-agent". The index
   has one card per clients.json entry, plus "Other MCP clients" (Streamable HTTP, OAuth 2.1 with
   DCR). No logos (D11).
3. build_site: add the pages to PAGES; llms.txt entries. website/_partials/header.html: "AI
   agents" after "Connectors" in BOTH .nav-links and .nav-menu-panel. FAQ "Which AI clients
   work?" links /ai-agents/. Check the header still fits at the 900 px breakpoint in light and dark
   (guardrail 12's browser layout test); add screenshots to the PR.
4. Guardrail 14, tests/unit/test_website_agent_pages.py (modelled on guardrail 8's
   test_website_connector_pages.py): every clients.json entry has exactly one /ai-agents/<slug>/
   page in PAGES and vice versa; each has content group "ai-agent", names its client, and has a
   "Set it up" link to /docs/connect-<slug>/, which must be a published doc in docs/README.md's
   "AI agent setup" section; /ai-agents/ links every page; the header links /ai-agents/ in both
   lists.
5. CHANGELOG [Unreleased] "Added": the AI agents section on privacyfence.eu.
Run /dod; one PR "feature: AI agents section on privacyfence.eu"; drive to green.
```

**Done when:** `/ai-agents/` and its three pages are live, the header shows **AI agents**, and
guardrail 14 is green.

### 5b. The AI agents menu: design

The Connectors menu has an index at `/connectors/` and one page per connector, held to the code by
guardrail 8. **AI agents** works the same way for clients: `/ai-agents/` plus one page per client
at `/ai-agents/<slug>/`, held to `website/_data/clients.json` and to the `connect-<slug>` docs by
guardrail 14.

**Every per-agent page has the same sections:**

| Section | Content |
|---|---|
| Intro | kicker "AI agent · \<name\>", one-sentence summary, which deployments it works with |
| How it connects | extension (`.mcpb`), or direct HTTP with a bearer token, or OAuth with dynamic registration; why web clients need an organization deployment |
| Set it up | "Local mode" and "Organization mode" step lists (3–4 steps each), with a **Set it up** button to `/docs/connect-<slug>/` |
| Confirmations | whether the client asks before PrivacyFence's card, and the organization's `--tool-annotations` switch (D5) |
| Files | through the extension (Claude Desktop), or through capability URLs (ADR 0028) |
| How it's identified | the card says the client *says* it is \<name\> (**Not verified**), and an organization admin can pin it on **Settings → AI systems** to make it verified |
| Client settings worth knowing | for example: Claude Desktop's own tool prompt; claude.ai Team/Enterprise owners add the connector for everyone |
| Next | buttons to `/ai-agents/` and `/connectors/` |

**Monitoring (D1).** Every `/ai-agents/<slug>/` page and every `/docs/connect-<slug>/` page is in
GA4 content group **`ai-agent`**. The page path tells you which agent. One report, filtered to
that group, shows interest per agent, next to `platform` and `connector`.

**How later plans extend it.** Adding a client (plans 2 and 3) is one website PR after the release
that carries its `connect-<slug>.md`: a `clients.json` entry, one page, and guardrail 10's
not-yet-supported list. Guardrail 14 fails until all of them agree.

---

### 🤖 WAVE 4: retire this plan

#### WP 4.1

```text
Read docs/ai-agents-foundation-plan.md. Every WP is merged and WP 3.1 is live. For every "→ ADR"
marker (D1 with D8, D5 — already written by WP 1.1, D7, D9) confirm an ADR exists; write any
missing one (next free number). Then delete this plan file, remove it from
build_site.CONTRIBUTOR_DOCS and docs/README.md, and update chatgpt-support-plan.md and
gemini-support-plan.md: every link to this plan (test_docs_links.py will list them) points at the
ADR or at ai-client-qa.md that now holds the content. Say in the PR description which ADRs carry
this plan's decisions (CLAUDE.md "Retiring a plan").
```

After this merges, plans 2 and 3 can start.

---

## 6. 🧑 Your manual tasks

### M0: before Wave 1

**M0.1 Decisions.** D1–D12 are done. Answer E1 and E2 (§3).

**M0.2 A test organization deployment on public HTTPS** (1–3 h if you don't have one; M1.2–M1.5
need it, and so do plans 2 and 3). WP 1.4 moves this recipe into `ai-client-qa.md`.

1. Get a small Linux VM (Ubuntu 24.04 is the reference) with 2 vCPU and 2 GB RAM.
2. DNS: create an **A** record `pf-test.<your-domain>` pointing at the VM's IP.
3. Follow [`org-mode-setup-guide.md`](org-mode-setup-guide.md) §2 → §9 in order:
   - **§4:** the identity provider. For Google, go to **APIs & Services → Credentials → + Create
     credentials → OAuth client ID → Web application**, with the redirect URIs from §4's table.
   - **§7:** Caddy + Let's Encrypt.
   - **§8:** `privacyfence-org.service`.
4. From your laptop, `curl -s https://pf-test.<your-domain>/.well-known/oauth-authorization-server | head`
   must list `registration_endpoint`.
5. Sign in. On `/connect`, connect **Google Calendar** (one read, one gated write) and **Drive**
   (the file tests).
6. Build a second bundle with `--tool-annotations all-read-only` (after WP 1.1). You'll swap it in
   for the M1 runs under that mode.

### Evidence to post after every test

WP 1.4 moves this list into `ai-client-qa.md`.

- **The client:** name and version, OS, date, PrivacyFence version, and the annotation mode.
- **The result:** pass or fail per step, and screenshots of any failure.
- **Client confirmation:** whether the client asked for its own confirmation before a write.
- **Attribution:** the approval card's "AI system" line, and the audit entry's `agent_id`,
  `agent_name`, `agent_version` and `agent_source` (under the data directory's
  `users/<principal>/logs/audit/`).
- **Org mode only:**
  - The client's `org/oauth_clients.json` entry under `/var/lib/privacyfence-org/.privacyfence/`,
    with `client_secret` replaced by `REDACTED`.
  - Then pin it on **Settings → AI systems** and confirm that the next card says verified.

### M2: release after Wave 2

Cut a stable release with `/cut-release`. Then run WAVE 3.

### M3: after Wave 3

Open <https://privacyfence.eu>. Check that:

- the header shows **AI agents**;
- each of the three pages' **Set it up** buttons lands on its docs page;
- in GA4, the `ai-agent` content group starts receiving page views.

Also answer the open question in the "AI agent protocol stability" session
(<https://claude.ai/code/session_019R3xZ8EhKqmqmygckY8KEo>) with "tiers and docs location →
docs/ai-agents-foundation-plan.md §2 and D2".
