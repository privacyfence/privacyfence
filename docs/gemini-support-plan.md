# Plan 3 of 3: Gemini support (Gemini Enterprise)

> **Temporary plan document; it lives only on branch `claude/bold-fermat-xlsdy4` and is never merged to `main`.**
> Work-package sessions start from `main` and read it with
> `git fetch origin claude/bold-fermat-xlsdy4 && git show FETCH_HEAD:docs/<plan>.md`. Their PRs never add plan files or
> plan entries to `main`. Before the plan is finished, every decision marked **→ ADR** below goes
> into an ADR on `main` (see [`adr/README.md`](adr/README.md)). ADRs cite issues and PRs, never
> this plan.

Covers [issue 393](https://github.com/privacyfence/privacyfence/issues/393): Gemini Enterprise,
which needs server code. It's written `I393` below. Docs and test code must never use the `#NNN`
form ([ADR 0056](adr/0056-code-carries-no-project-history.md)).

> **Scope change, 2026-09-29.** This plan started with three Gemini clients. Two are dropped, and
> [ADR 0106](adr/0106-gemini-cli-and-antigravity-are-not-supported-clients.md)
> ([PR 806](https://github.com/privacyfence/privacyfence/pull/806)) records why:
>
> - **Gemini CLI** ([issue 392](https://github.com/privacyfence/privacyfence/issues/392), closed
>   as not planned). Since 2026-06-18 it no longer serves individual Google accounts, and Google
>   treats it as its legacy tool. Its successor, Antigravity, can't yet authenticate to an
>   OAuth-protected MCP server. PR 792 (the draft setup page) is closed, and PR 806 reverts PR 790's
>   contract test and canary entry. PR 791's seeded replay fixture and the `gemini-cli` registry
>   entry stay.
> - **Gemini app / Spark** ([issue 394](https://github.com/privacyfence/privacyfence/issues/394),
>   closed as not planned). It appears to be for personal accounts only, and PrivacyFence targets
>   organizations.
>
> The old waves 2, 3 and 5, milestones M1, M2 and M5, and Gate A are gone. The remaining parts keep
> their old numbers (M3.1, WAVE 4, M4, WAVE 6), so earlier issue comments and PR links still
> point at the right step.

**Plan 1** ([`ai-agents-foundation-plan.md`](ai-agents-foundation-plan.md)) **is finished**
(2026-09-28; shipped in v5.0.0), so this plan can start. Everything it uses is on `main`:

| From plan 1 | Where it is on `main` |
|---|---|
| Truthful tool annotations, the **only** mode: reads read-only, writes not read-only, `destructiveHint` only on `calendar_delete_event` and `drive_sheets_delete_dimensions`. There is no `all-read-only` switch, no bundle key and no `X-PrivacyFence-Tool-Annotations` header any more | ADR 0086 (decisions 1–2), [ADR 0089](adr/0089-tool-annotations-are-always-truthful.md) |
| T1 portability test | `tests/unit/web/test_tool_schema_portability.py`, every PR |
| T2 replay test | `tests/unit/web/test_ai_client_replay.py`, parametrized over `tests/fixtures/ai_clients/<client>/` (read that directory's `README.md`: layout, the two "Expected agent_id" lines, scrubbing) |
| T3 harness | `tests/integration/ai_client_harness.py`; the pinned CLIs in `tests/integration/ai_clients/package.json` + committed `package-lock.json`, installed with `npm ci` ([ADR 0085](adr/0085-ci-installs-ai-client-clis-from-a-committed-lockfile.md)); `test_claude_code_contract.py` as the model (binary override `CLAUDE_CODE_BIN`, set by `tests.yml`'s `test` job so the test fails rather than skips); the weekly `ai-client-canary.yml` matrix, one `include:` entry per client |
| Registry name templates (`"… ({server})"`) and the rule that an unrecognised DCR `client_name` yields to a recognised `clientInfo.name` | `agent_identity.py`, [ADR 0094](adr/0094-claude-clients-are-matched-by-their-observed-names.md) (amends ADR 0035) |
| Local mode shows every requester as **"Undetected"** on cards and the Audit Log; the audit entry still records `agent_id` | [ADR 0088](adr/0088-local-mode-shows-every-requester-as-undetected.md) |
| A download or upload link works only for a client that can reach the server's host; `drive_get_file_content` returns a document's text | [ADR 0097](adr/0097-a-download-link-needs-a-client-that-can-reach-the-server.md) |
| Test org deployment, "What the client receives", per-client script, evidence format, "Recording results" table | `docs/ai-client-qa.md` |
| One docs page per agent, `connect-<slug>.md`, listed under `docs/README.md` "AI agent setup" and in org guide §9 "Add PrivacyFence to an AI client"; GA4 content group `ai-agent` | [ADR 0098](adr/0098-each-ai-agent-has-its-own-setup-doc-and-website-page.md); template: `connect-claude-code.md` (the closest client: direct HTTP, both modes, per-platform token table) |
| The website's **AI agents** menu | held to `website/_data/clients.json` by guardrail 14 (`tests/unit/test_website_agent_pages.py`) |

Plan 1's rules apply unchanged, and are now ADRs:

- one PR per package;
- no unverified setup instructions on `main`: a new client's doc waits in a draft PR until its
  manual check passes ([ADR 0100](adr/0100-unverified-client-setup-instructions-never-merge-to-main.md));
- a client's website page lands only after the stable release that carries its docs page
  ([ADR 0099](adr/0099-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md));
- no logos ([ADR 0101](adr/0101-the-website-shows-no-third-party-logos.md));
- ADR numbers are the next free one at merge time (0101 was the last one when plan 1 finished).

Plan 2 (ChatGPT) runs independently of this one.

---


## 0. How to use this plan

It works the same way as plan 1. To run a wave, paste into a new Claude Code on the web session:

```text
Implement WAVE <n> of docs/gemini-support-plan.md. The plan lives only on branch claude/bold-fermat-xlsdy4:
read it with `git fetch origin claude/bold-fermat-xlsdy4 && git show FETCH_HEAD:docs/gemini-support-plan.md`. You are the coordinator: one child session per
work package (create_session, the package's "Session prompt" word for word plus this plan's §1),
tagged "gemini-support:wave-<n>"; track each until its PR is green (get_session, send_later about
hourly); then post a checklist of WP, PR, CI state, and unblocked 🧑 tasks. Start a package only
when its "Depends on" items are done.
```

---


## 1. Facts for Gemini Enterprise (re-checked 2026-09-29)

1. **Attribution.** A new `gemini-enterprise` registry entry needs a PNG in
   `src/privacyfence/resources/agent_icons/`, a row in that directory's `README.md`, and it must pass
   `test_approval_icons.py`'s one-icon-per-registry-entry test. In org mode a DCR `client_name` the
   registry matches wins over `clientInfo.name`, and an unrecognised one yields to a recognised
   `clientInfo.name` ([ADR 0094](adr/0094-claude-clients-are-matched-by-their-observed-names.md)).
   A pre-registered client has no DCR name, so the entry keys on the name the registration
   command stores.
2. **Guardrail 10** (`tests/unit/test_website_clients.py`) has `NOT_YET_SUPPORTED = ("ChatGPT",
   "Gemini", "Copilot", "Cursor")`, banned on every hand-written page. Naming Gemini Enterprise
   needs a finer list (WP 4.2), with "Gemini CLI", "Gemini app" and "Antigravity" staying banned
   (ADR 0106).
3. **Annotations are always truthful**
   ([ADR 0089](adr/0089-tool-annotations-are-always-truthful.md)). How Gemini Enterprise confirms a
   write is what the setup page's "Confirmations" section records. PrivacyFence offers no switch.
4. **Traps for I393 in `web/oauth_provider.py`:**
   - The running daemon keeps `oauth_clients.json` in memory and rewrites the whole file on
     every `get_client`. A script that edits the file while the daemon runs gets overwritten.
   - The daemon holds a `portalocker` single-instance lock (`daemon_main._acquire_instance_lock`).
   - Stale clients (unused for 180 days) are pruned on every `/register`. A hand-registered
     Gemini Enterprise client would vanish without warning.
   - The store is capped at 2000 clients (`_MAX_REGISTERED_CLIENTS`).
   - The secret is stored in plaintext inside the SDK's `OAuthClientInformationFull`.
   - Records are `{"client", "last_used_at"}`. There's no older format to migrate
     ([ADR 0041](adr/0041-only-the-current-install-layout-is-supported.md)).
5. **Org deployment names.** The service account is `privacyfence-org`, the unit is
   `privacyfence-org.service`, the venv is `/opt/privacyfence/venv`, and the data directory is
   `/var/lib/privacyfence-org/.privacyfence/`.
6. **Unconfirmed vendor leads** (WAVE 1's research could not open any Google page; see
   [its comment on issue 393](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5869685272)).
   M3.1 confirms or corrects each one:
   - fields: server URL, authorization URL, token URL, client ID, client secret, scopes;
     possibly a PKCE switch (`pkce_support_enabled`);
   - no DCR: Gemini Enterprise is registered by hand as an OAuth client;
   - redirect URI `https://vertexaisearch.cloud.google.com/oauth-redirect` (from a third-party
     guide only);
   - the server needs a publicly trusted TLS certificate;
   - a recommended limit of 100 enabled actions per data store;
   - an org-policy constraint that may have to be overridden before a custom MCP data store can be
     created.

---

## 2. Decisions

| # | Decision | Recommendation |
|---|---|---|
| D3 | Public claim wording | At Gate C: "Gemini Enterprise (organization deployments)". Nothing else Gemini is claimed (ADR 0106). |
| D4 | How I393 writes a client while the daemon runs → **ADR** | A `privacyfence-app --register-oauth-client` subcommand that **refuses to run while the daemon holds its lock**. It writes through a new `OrgOAuthProvider.register_static_client()`, and the record carries `"source": "admin"` so it's never pruned as stale. It gets a `gemini-enterprise` registry entry with an icon. Later, maybe: the same action on org **Settings → AI systems** with step-up (ADR 0034). If M3.1 finds that Gemini Enterprise supports DCR, D4 is not needed and WAVE 4 is docs only. |
| D16 | Gemini CLI, Antigravity and Spark → **ADR** | Not supported. Recorded as [ADR 0106](adr/0106-gemini-cli-and-antigravity-are-not-supported-clients.md). |

---

## 3. Roadmap

```text
🧑 M0  ✅ plan 1 finished (v5.0.0); test org deployment (ai-client-qa.md) up to date
│
🤖 WAVE 1  ✅ done 2026-09-28 (research on I393; the Gemini CLI parts were later dropped, ADR 0106)
│
🧑 M3.1  confirm Google's Gemini Enterprise requirements          ← next
🤖 WAVE 4   WP4.1 ADR + pre-registered OAuth clients + tests + connect-gemini-enterprise.md
🧑 M4  register, configure, verify; docs PR removes "Verification pending"; stable release
🤖          WP4.2 website: /ai-agents/gemini-enterprise/
│
★ GATE C — "Gemini Enterprise"                          (I393 done)
│
🤖 WAVE 6   retire this plan
```

---

## 4. Waves and milestones

### ✅ WAVE 1 (done 2026-09-28)

- WP 1.1, the Gemini CLI contract test: merged as
  [PR 790](https://github.com/privacyfence/privacyfence/pull/790), then reverted by
  [PR 806](https://github.com/privacyfence/privacyfence/pull/806).
- WP 1.2: the seeded fixture was merged as
  [PR 791](https://github.com/privacyfence/privacyfence/pull/791) and kept. Draft
  [PR 792](https://github.com/privacyfence/privacyfence/pull/792) was closed unmerged.
- WP 1.3, the research: comments on
  [I393](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5869685272) and
  [I394](https://github.com/privacyfence/privacyfence/issues/394#issuecomment-5869688091). No
  Google page was reachable from a Claude Code on the web session, so **M3.1 is a human step**.

---

### 🧑 M3.1: confirm Google's Gemini Enterprise requirements (20 min)

Depends on: nothing. Start now.

1. Read these pages. The research session couldn't open them.
   - <https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server>
     (the main page);
   - <https://support.google.com/g/answer/17106276?hl=en> (the Business edition);
   - <https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/override-constraint-for-custom-mcp-data-stores>;
   - <https://codelabs.developers.google.com/google-workspace-ge> (may state the redirect URI).
2. Write down each item in §1.6, confirmed or corrected, and also:
   - Does it support DCR? If **yes**, WAVE 4 becomes docs only, with no D4.
   - The exact redirect URI.
   - The fields it asks for, and the scopes.
   - The client authentication method (`client_secret_post` or `_basic`), and whether PKCE is
     used.
   - The console menu path.
   - Whether the 100-action limit is lower than PrivacyFence's tool count for a fully connected
     user. If it is, WP 4.1 has to say which tools to enable.
3. Post them on **I393**, titled "M3.1 requirements".

---

### 🤖 WAVE 4: Gemini Enterprise, the only server code (I393) → ★ GATE C

#### WP 4.1: ADR, pre-registered OAuth clients, tests, docs · `feature:`

```text
Read docs/gemini-support-plan.md (branch claude/bold-fermat-xlsdy4; see the plan's header) (§1 items 1, 4 and 6, D4) and the M3.1 findings on issue 393.
Implement WP 4.1:
1. ADR (next free number at merge time): "Pre-registered OAuth clients for clients that cannot use
   DCR" — D4, the stale-prune exemption, secret storage matching the SDK, why the command refuses
   while the daemon holds its lock; rejected alternatives: hand-editing the JSON; a live
   Settings → AI systems action (deferred, not rejected).
2. OrgOAuthProvider.register_static_client(client_info, *, source="admin") and
   remove_static_client(client_id); _StoredClient grows `source` ("dcr" | "admin", "dcr" when
   absent — no migration, ADR 0041); _prune_stale_clients_locked skips "admin"; the 2000 cap still
   applies; Settings → AI systems shows the source.
3. `privacyfence-app --register-oauth-client --name "Gemini Enterprise" --redirect-uri <uri>
   [--client-id ...] [--token-endpoint-auth-method client_secret_post|client_secret_basic]`, plus
   `--list-oauth-clients` and `--remove-oauth-client <id>`: org mode only; refuse while the lock is
   held; generate client_id/secret with `secrets` when not given; print them ONCE; never log the
   secret.
4. Tests (test_oauth_provider.py + daemon_main CLI tests): hand-registered and DCR clients are
   indistinguishable to get_client/authorize/token; admin clients survive pruning; records
   without `source` load as "dcr"; lock-held refusal; secret never logged. T2 fixture
   tests/fixtures/ai_clients/gemini-enterprise/ driving /authorize → /token with client_secret
   auth (extend the replay test if pre-registration needs a different first step).
5. REGISTRY "gemini-enterprise" keyed on the name the command sets, with icon and license row.
6. connect-gemini-enterprise.md (the connect template, "AI agent setup"), and an org guide
   §9 subsection "Clients that can't register themselves" linking it; authorization URL
   https://<host>/authorize, token URL https://<host>/token, redirect URI from M3.1;
   "Verification pending" linking the full issue 393 URL. configuration-reference.md: the new
   command-line options. ai-client-qa.md row. CHANGELOG.
Run /dod; one PR "feature: pre-registered OAuth clients (Gemini Enterprise)"; drive to green.
```

#### 🧑 M4: register and verify (45 min; needs a release carrying WP 4.1 on your org VM)

1. Stop the daemon: `sudo systemctl stop privacyfence-org`.
2. Register the client:

   ```bash
   sudo -u privacyfence-org /opt/privacyfence/venv/bin/privacyfence-app --register-oauth-client \
     --name "Gemini Enterprise" --redirect-uri <URI from M3.1>
   ```

   Add the same config/data-dir flags as the systemd unit, if `connect-gemini-enterprise.md` says
   so. Copy the `client_id` and `client_secret` **now**. They're shown only once.
3. Start the daemon: `sudo systemctl start privacyfence-org`.
4. In the Gemini Enterprise console, open your project → **Apps** → your app → **+ Add** →
   **Custom MCP server**, and fill in:
   - Server URL: `https://pf-test.<your-domain>/mcp`
   - Authorization URL: `/authorize` on the same host
   - Token URL: `/token` on the same host
   - Client ID and secret: from step 2
   - Scopes: as M3.1 found

   Click **Connect**. **Expected:** `/login` → Google → connected.
5. In the Gemini Enterprise app, run the read, the write and the pin from `ai-client-qa.md`.
6. Post the evidence on **I393**, titled "M4 result".

After that:

- open a small docs PR that removes "Verification pending";
- cut a stable release;
- run WP 4.2.

#### WP 4.2: Gemini Enterprise on the website · `feature:`

```text
The latest stable release's /docs/ has connect-gemini-enterprise. Add "Gemini Enterprise" to
clients.json (["organization"], slug gemini-enterprise), website/ai-agents/gemini-enterprise/,
PAGES, llms.txt and the /ai-agents/ index. Guardrail 10 (tests/unit/test_website_clients.py): add
"Gemini Enterprise" to NAMES, and replace the bare "Gemini" in NOT_YET_SUPPORTED with "Gemini CLI",
"Gemini app" and "Antigravity" (ADR 0106: not supported), so Gemini Enterprise may be named and
those may not. Canonical description (guardrail 1): if its client examples change, change it,
README.md's opening and website/index.html in one commit. CHANGELOG. Run /dod; one PR; drive to
green.
```

**★ GATE C is passed when:** WP 4.2 is deployed and I393 is closed.

---

### 🤖 WAVE 6: retire this plan

```text
Issue 393 is closed. For every "→ ADR" marker confirm an ADR exists on main (D4 from WP 4.1, D16 as
ADR 0106; write any missing one) in one PR to main, citing issues and PRs, never the plan; name the
ADRs in the PR description. Do not add or delete plan files on main.
```

---

## 5. Issue closure map

| Issue | Closed by | Evidence it needs |
|---|---|---|
| I392 | ✅ closed as not planned, 2026-09-29 | ADR 0106 |
| I393 | WP 4.1, then WP 4.2 | M3.1 and M4 logged |
| I394 | ✅ closed as not planned, 2026-09-29 | ADR 0106 |

## 6. Accounts you need

| Need | For | How |
|---|---|---|
| The test org deployment | M4 | `ai-client-qa.md` → "Test organization deployment" |
| A Google Cloud project with **Gemini Enterprise** | M3.1, M4 | <https://console.cloud.google.com/gemini-enterprise> (a trial is enough) |
