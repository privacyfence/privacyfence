# Plan 2 of 3: ChatGPT support

> **Temporary plan document** (see [`adr/README.md`](adr/README.md)): delete it in the PR that
> finishes its last wave. Before deleting it, move every decision marked **→ ADR** below into an ADR.
> It is listed in `scripts/build_site.py`'s `CONTRIBUTOR_DOCS` and in the contributor half of
> [`README.md`](README.md); the retiring PR removes both entries.

Covers [issue 390](https://github.com/privacyfence/privacyfence/issues/390) (ChatGPT desktop,
local mode) and [issue 391](https://github.com/privacyfence/privacyfence/issues/391) (ChatGPT, org
mode). Written `I390` and `I391` below. Docs and test code must never use the `#NNN` form
([ADR 0056](adr/0056-code-carries-no-project-history.md)).

**Starts after plan 1**, [`ai-agents-foundation-plan.md`](ai-agents-foundation-plan.md), is
finished. Plan 1 builds everything this plan uses:

| From plan 1 | What it is |
|---|---|
| Truthful tool annotations, plus the bundle switch `--tool-annotations all-read-only` | the successor ADR to ADR 0076 |
| Schema-portability test (T1) | runs on every PR |
| Recorded-handshake replay (T2) | parametrized over `tests/fixtures/ai_clients/<client>/`, so adding a directory adds a client |
| Test org deployment, evidence format and per-client script | `ai-client-qa.md` |
| One docs page per agent, `connect-<slug>.md` | GA4 content group `ai-agent` |
| The website's **AI agents** menu (`/ai-agents/<slug>/`) | held to `clients.json` by guardrail 14 |

Plan 1's rules apply here unchanged: one PR per package; no unverified instructions on `main`; a
client's website page lands only after the release that carries its docs page; no logos; ADR
numbers are the next free number at merge time. Plan 3 (Gemini) runs independently of this one.

**The goal:** say "works with ChatGPT" publicly at **Gate A**. That needs no server code.

---

## 0. How to use this plan

It works the same way as plan 1: 🤖 waves of parallel work packages, and 🧑 manual steps. To run a
wave, paste into a new Claude Code on the web session:

```text
Implement WAVE <n> of docs/chatgpt-support-plan.md. You are the coordinator: one child session per
work package (create_session, the package's "Session prompt" word for word plus this plan's §1),
tagged "chatgpt-support:wave-<n>"; track each until its PR is green (get_session, send_later about
hourly); then post a checklist of WP, PR, CI state, and unblocked 🧑 tasks. Start a package only
when its "Depends on" items are done.
```

---

## 1. ChatGPT-specific facts (checked 2026-09-26)

1. **ChatGPT on the web can't reach a local-mode daemon.** Developer Mode connectors are called
   from OpenAI's cloud, and local mode listens on `127.0.0.1`. The first ChatGPT claim therefore
   goes through an organization deployment (I391 Route A). ChatGPT desktop (I390) is the only
   local-mode route, and the least certain one.
2. **The shim ships only inside `PrivacyFence.mcpb`.** No installer puts `shim.js` at a stable path.
   ChatGPT desktop would run `node` from a macOS GUI app's minimal `PATH`, where Homebrew's Node
   isn't found. So I390 may need installer work (WP 3.1b).
3. **Attribution.** `agent_identity.py`'s `REGISTRY` maps `chatgpt` ← `openai-mcp`, which
   [ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
   marks as a guess. In org mode the DCR `client_name` wins over `clientInfo.name`, so both are
   captured. `chatgpt.png` already exists in `resources/agent_icons/`.
4. **Annotations.** With plan 1's truthful default, ChatGPT sees write tools as writes. It will
   probably ask for its own confirmation before PrivacyFence's card, and "Scan Tools" will label
   the writes honestly. An organization can switch to `all-read-only` to drop ChatGPT's prompt.
   The ChatGPT docs have to say what that switch implies (D13).
5. **Files without the shim** use
   [ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)'s capability URLs.
   Verification covers one upload and one download.
6. **Guardrail 10** (`tests/unit/test_website_clients.py`) hard-codes `NAMES` and bans "ChatGPT" on
   every hand-written page. Gate A's website PR edits that test.

---

## 2. Decisions

| # | Decision | Recommendation |
|---|---|---|
| D3 | Public claim wording | At Gate A: "ChatGPT (Developer Mode, with an organization deployment)". The workspace connector and ChatGPT desktop are added only after Gate B. The site shows no dates or versions; `ai-client-qa.md` holds them. |
| D13 | ChatGPT and the `all-read-only` switch | Document it on `connect-chatgpt.md`. Truthful (the default) means ChatGPT confirms, then PrivacyFence approves. `all-read-only` means only PrivacyFence approves, but then ChatGPT and its workspace admin see every write labelled read-only. **Tell admins not to use `all-read-only` if any ChatGPT workspace policy they rely on distinguishes read-only from write actions.** If WP 1.1 finds such a policy, say so on the page as a warning, not a footnote. |
| D14 | ChatGPT desktop outcome | Decided by M1.4: (a) docs only, (b) ship `shim.js` at a stable path (→ **ADR**), or (c) close I390 as "not planned". |

---

## 3. Roadmap

```text
🧑 M0  plan 1 finished; ChatGPT accounts; test org deployment (ai-client-qa.md)
│
🤖 WAVE 1   WP1.1 re-source ChatGPT vendor facts │ WP1.2 seeded T2 fixture + draft connect-chatgpt.md (draft PR)
│
🧑 M1  ChatGPT Developer Mode (org) → ChatGPT desktop check
│
🤖 WAVE 2   WP2.1 captured fixture, registry names, finish and merge connect-chatgpt.md
🧑 M2  stable release
🤖          WP2.2 website: clients.json + /ai-agents/chatgpt/ + guardrail 10
│
★ GATE A — "Works with ChatGPT (Developer Mode, organization deployments)"
│
🤖 WAVE 3   WP3.1 ChatGPT desktop (a/b/c) │ WP3.2 workspace-admin publishing
🧑 M3  workspace publish (+ desktop retest for b), then release
🤖          WP3.3 website: extend /ai-agents/chatgpt/ (+ "local" if desktop works)
│
★ GATE B — ChatGPT workspace connector, and possibly ChatGPT desktop   (I391, I390 done)
│
🤖 WAVE 4   retire this plan
```

---

## 4. Waves (🤖 Claude)

### 🤖 WAVE 1 (2 parallel sessions)

#### WP 1.1: re-source the ChatGPT vendor facts · research, no PR

```text
Read docs/chatgpt-support-plan.md. Research only — no commits. From OpenAI's OWN pages (not
aggregators), record URL + date for:
(a) ChatGPT desktop MCP support: exists? config path, stdio vs url, custom headers?
(b) Developer Mode menu path and OAuth redirect URI; the client_name ChatGPT registers with at
    /register; the clientInfo name; how ChatGPT treats readOnlyHint / destructiveHint (when it
    asks for confirmation); any workspace admin setting that permits only read-only actions (D13);
    the workspace-admin publish flow and the "frozen snapshot / re-publish" behaviour
    (help.openai.com 12584461 and 11509118).
Comment findings on issues 390 and 391, separating "confirmed on <url>" from "could not reach".
Summarize in chat anything that changes this plan.
```

#### WP 1.2: seeded ChatGPT fixture and draft `connect-chatgpt.md` · `tests:` + draft PR

```text
Read docs/chatgpt-support-plan.md §1–§2 and ai-client-qa.md. Implement WP 1.2 as TWO PRs:
PR A (merge it) "tests: seeded ChatGPT handshake fixture": tests/fixtures/ai_clients/chatgpt/
(register.json with redirect_uri https://chatgpt.com/connector_platform_oauth_redirect,
initialize.json, README.md "SEEDED FROM VENDOR DOCS, NOT YET CAPTURED", citing sources, expected
agent_id chatgpt). The replay test picks it up by itself; make it pass.
PR B (DRAFT, do not merge; WP 2.1 finishes it) "feature: Connect ChatGPT": docs/connect-chatgpt.md
in the connect-claude-*.md template, listed under docs/README.md "AI agent setup": organization
deployment only (and why); Developer Mode steps (Settings → Apps → Advanced settings → Developer
mode; Create; /mcp URL; OAuth); re-enable per chat; "Confirmations" per D13; files via capability
URLs; pin on Settings → AI systems; ChatGPT desktop "pending". Org guide §9: one bullet linking
it. A "Verification pending" note linking https://github.com/privacyfence/privacyfence/issues/391.
No CHANGELOG line, no website change.
```

---

### 🧑 M1: ChatGPT checks (about 1 h)

Depends on: WAVE 1, the test org deployment, and the accounts in §6. Follow `ai-client-qa.md`'s
per-client script and evidence format. Run each check under both annotation modes.

**M1.3 ChatGPT Developer Mode, org mode** (I391 Route A, 30 min)

1. Turn on Developer mode: <https://chatgpt.com> → profile icon → **Settings** → **Apps** (older
   UI: **Apps & Connectors**) → **Advanced settings** → **Developer mode** on. On a Business
   workspace the admin may have to allow it first (M3.1 steps 1–2).
2. **Settings** → **Apps** → **Create**, and fill in:
   - Name: `PrivacyFence`
   - MCP Server URL: `https://pf-test.<your-domain>/mcp`
   - Authentication: **OAuth**
   - tick **I understand and want to continue**, then **Create**
3. **Expected:** PrivacyFence `/login` → Google → back to ChatGPT, connected, with the tool list.
   - Note how ChatGPT labels the tools under each annotation mode.
   - If OAuth finishes but the app doesn't appear, retry once. This is a known ChatGPT flakiness.
4. New chat → **+** → **More** → **Developer mode** → enable **PrivacyFence**. You have to do this
   in every chat.
5. Prompt: `List my calendar events for tomorrow.` **Expected:** it succeeds.
6. Prompt: `Create a calendar event "PF test" tomorrow 10:00–10:15.` **Expected:**
   - Truthful mode: ChatGPT's **Confirm** first, then the PrivacyFence card at `/approvals`.
   - `all-read-only` mode: only the PrivacyFence card.

   Screenshot both.
7. Upload and download one file through capability URLs.
8. Pin the registration on **Settings → AI systems**, then repeat step 5. The card now says
   verified.
9. Post the evidence on **I391**, titled "M1.3 result (Route A)".

**M1.4 ChatGPT desktop check** (I390, 15 min)

1. In ChatGPT desktop → **ChatGPT** menu → **Settings…**, look for an **MCP servers** (or
   **Connectors / Apps → Advanced**) page.
2. Record which of these applies:
   - (a) you can add a **URL** with custom headers
   - (b) you can add only a **command** (stdio)
   - (c) there's no MCP page at all

   Also check `ls ~/Library/Application\ Support/ChatGPT/`.
3. If (a): add your `mcp_url` value with the header `Authorization: Bearer <output of
   PrivacyFenceApp --print-mcp-token>` (see `connect-claude-code.md`'s macOS row). Then run steps
   5–6 of M1.3. Approve through the companion's **Open Approvals**.
4. If (b): don't point it at Claude's extension folder. Just post the result.
5. Post the evidence on **I390**, titled "M1.4 result: (a)/(b)/(c)", with screenshots.

---

### 🤖 WAVE 2 → ★ GATE A

#### WP 2.1: fold in the evidence · `feature:`

Depends on: M1.3 passed.

```text
Read docs/chatgpt-support-plan.md and the M1 evidence on issues 390/391. Implement WP 2.1, taking
over WP 1.2's draft PR B:
1. Replace the SEEDED chatgpt fixture with the captured one (scrubbed); README "captured <date>,
   client version <v>".
2. agent_identity.py REGISTRY chatgpt: every observed name (DCR client_name and clientInfo name);
   drop "openai-mcp" unless observed. Unit tests. A new ADR amending ADR 0035 records it (never
   edit 0035's body).
3. connect-chatgpt.md: remove "Verification pending" for what passed; "Confirmations" states what
   M1.3 observed under each mode (D13). Fill ai-client-qa.md's ChatGPT rows.
4. CHANGELOG [Unreleased] "Added": ChatGPT (Developer Mode, organization deployments), per D3.
   README.md client mentions (not the canonical description — WP 2.2 does that with the site).
5. Tick I391's Route A checkboxes.
No website change. Run /dod; mark the PR ready: "feature: verified ChatGPT (Developer Mode)
support"; drive to green.
```

Then **🧑 M2**: cut a stable release that carries WP 2.1.

#### WP 2.2: ChatGPT on the website · `feature:`

Depends on: the release is published, and its `/docs/` has `connect-chatgpt`.

```text
Read docs/chatgpt-support-plan.md §1–§2. Implement WP 2.2:
1. website/_data/clients.json: "ChatGPT", deployments ["organization"], connects "straight to an
   organization deployment's /mcp over HTTPS, with OAuth sign-in (Developer Mode)", slug chatgpt.
2. website/ai-agents/chatgpt/index.html in the /ai-agents/ page structure (organization only and
   why, re-enable per chat, Confirmations per D13, files via capability URLs, pinning); add to
   PAGES, llms.txt and the /ai-agents/ index. Guardrail 14 checks the rest.
3. tests/unit/test_website_clients.py: add ChatGPT to NAMES; assert it is organization-only like
   claude.ai; replace the bare "ChatGPT" in the not-yet list with the still-unsupported forms
   ("ChatGPT desktop", "ChatGPT workspace") until Gate B.
4. Canonical description (guardrail 1): if its client examples change, change
   website/canonical-description.md, README.md's opening and website/index.html in one commit.
5. CHANGELOG [Unreleased]: privacyfence.eu lists ChatGPT.
Run /dod; one PR "feature: privacyfence.eu lists ChatGPT"; drive to green.
```

**★ GATE A is passed when:** WP 2.2 is merged and `pages.yml` has deployed it.

---

### 🤖 WAVE 3 → ★ GATE B

Depends on: GATE A. WP 3.1 also needs M1.4's result.

#### WP 3.1: ChatGPT desktop, the branch that matches M1.4

- **(a) URL with custom headers:** docs only. A "ChatGPT desktop (local mode)" section in
  `connect-chatgpt.md`, using the per-platform token table.
- **(b) stdio only:** packaging. Ship `shim.js` at a stable path from all three installers:
  - macOS: `PrivacyFenceApp.app/Contents/Resources/shim/shim.js` (`build_dmg.sh`/`build_pkg.sh`)
  - `.deb`: `/usr/lib/privacyfence/shim/shim.js`
  - Windows: `{app}\shim\shim.js`

  Extend each packaged smoke test to assert that the file exists and answers `initialize` over
  stdio. Document an absolute `node` path. Verify by dispatching `build.yml` against the branch.
  → **ADR** ("the shim is also shipped outside the `.mcpb`").
- **(c) no usable MCP support:** post findings on I390, close it as "not planned — revisit when…",
  and point the docs at org mode.

```text
Read docs/chatgpt-support-plan.md and the M1.4 result on issue 390. Implement WP 3.1, only the
branch matching that result. For (b): dispatch build.yml against your branch and confirm build,
build-windows and build-deb are green before review; write the ADR. No website change. Run /dod;
one PR; drive to green.
```

#### WP 3.2: ChatGPT workspace-admin publishing (I391 Route B) · `chore:`

```text
Read docs/chatgpt-support-plan.md (D13) and WP 1.1's findings on issue 391. Add a "ChatGPT
Business/Enterprise/Edu: workspace-published connector" section to connect-chatgpt.md, linked
from org guide §9: the admin flow (enable custom MCP connectors → create → Scan Tools → test as
draft → publish); what Scan Tools shows under each annotation mode (D13); a callout that
connecting or disconnecting a PrivacyFence service requires the ChatGPT admin to RE-PUBLISH the
connector (PrivacyFence's tool list is dynamic, ChatGPT's published copy is frozen); pinning the
registration on Settings → AI systems. "Verification pending" linking the full issue 391 URL.
ai-client-qa.md row. CHANGELOG line. Run /dod; one PR; drive to green.
```

#### 🧑 M3

**M3.1 Workspace-published connector** (I391 Route B, 45 min; you need to be admin of a
Business or Enterprise workspace)

1. Open **Workspace settings** (<https://chatgpt.com/admin>) → **Permissions & roles**, and turn
   on **Developer mode / Create custom MCP connectors** for admins.
2. Under the **Apps / Connectors** permission, allow members to use custom apps (or allow one test
   group). **Screenshot any setting that distinguishes read-only from write actions (D13).**
3. **Apps** → **Create** → the `/mcp` URL, **OAuth** → **Scan tools**. Screenshot what the scan
   shows under each annotation mode.
4. Choose **Test as draft**, then run steps 5–6 of M1.3.
5. **Publish** the connector. Then, as a non-admin member with Developer Mode off, confirm that
   PrivacyFence is available.
6. Frozen-snapshot test:
   1. Connect one more service on `/connect`.
   2. Check a member chat. **Expected:** the new tools are missing.
   3. As admin, choose **Re-publish**. **Expected:** now they appear.
7. Post the evidence on **I391**, titled "M3.1 result (Route B)".

**M3.2 ChatGPT desktop retest** (only for branch (b))

1. Install a pre-release that carries WP 3.1: `/cut-release` with an `aN` tag, then
   <https://privacyfence.eu/download/>.
2. In ChatGPT desktop → **MCP servers** → **Add server**, set the command to the absolute `node`
   path and the args to the documented `shim.js` path.
3. Quit PrivacyFence first. Then run steps 5–6 of M1.3.
4. Post the evidence on **I390**, titled "M3.2 result".

Then cut a stable release that carries WP 3.1 and WP 3.2.

#### WP 3.3: website for Gate B · `feature:`

```text
Read docs/chatgpt-support-plan.md. The latest stable release's connect-chatgpt.md carries the
workspace section (and the desktop section, if WP 3.1 took (a) or (b)). Extend
/ai-agents/chatgpt/ with workspace publishing (and desktop); clients.json's ChatGPT entry gains
"local" if desktop works (then drop the organization-only assertion for ChatGPT); remove the now
supported forms from guardrail 10's not-yet list. CHANGELOG. Run /dod; one PR; drive to green.
```

**★ GATE B is passed when:** WP 3.3 is merged, and I390 and I391 are closed.

---

### 🤖 WAVE 4: retire this plan

```text
Read docs/chatgpt-support-plan.md. Issues 390 and 391 are closed. For every "→ ADR" marker and
D13, confirm an ADR exists (write any missing one); delete this file, remove it from
build_site.CONTRIBUTOR_DOCS and docs/README.md, fix any link to it (test_docs_links.py lists
them), and name the ADRs in the PR description.
```

---

## 5. Issue closure map

| Issue | Closed by | Evidence it needs |
|---|---|---|
| I391 | WP 3.2, verified | M1.3 (Route A) and M3.1 (Route B) logged |
| I390 | WP 3.1 (a or b), or closed via (c) | M1.4 (+ M3.2 for branch b) |

## 6. Accounts you need

| Need | For | How |
|---|---|---|
| ChatGPT **Plus/Pro** or a Business seat | M1.3 | <https://chatgpt.com/#pricing> |
| ChatGPT desktop for macOS | M1.4 | <https://openai.com/chatgpt/desktop/> |
| A ChatGPT **Business/Enterprise workspace, as admin** | M3.1 | <https://chatgpt.com/team> (a Business trial is enough) |
| The test org deployment | M1.3, M3.1 | `ai-client-qa.md` → "Test organization deployment" |

Vendor menu names were current as of 2026-09. If a menu doesn't match, use the nearest equivalent
and **write the real path in your evidence comment**.
