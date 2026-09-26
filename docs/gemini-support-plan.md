# Plan 3 of 3: Gemini support

> **Temporary plan document** (see [`adr/README.md`](adr/README.md)): delete it in the PR that
> finishes its last wave. Before deleting it, move every decision marked **→ ADR** below into an ADR.
> It is listed in `scripts/build_site.py`'s `CONTRIBUTOR_DOCS` and in the contributor half of
> [`README.md`](README.md); the retiring PR removes both entries.

Covers:

- [issue 392](https://github.com/privacyfence/privacyfence/issues/392): Gemini CLI, both modes
- [issue 393](https://github.com/privacyfence/privacyfence/issues/393): Gemini Enterprise, needs
  code
- [issue 394](https://github.com/privacyfence/privacyfence/issues/394): Gemini app / Spark,
  research

They're written `I392`–`I394` below. Docs and test code must never use the `#NNN` form
([ADR 0056](adr/0056-code-carries-no-project-history.md)).

**This plan starts after plan 1** ([`ai-agents-foundation-plan.md`](ai-agents-foundation-plan.md))
is finished. Plan 1 builds everything this plan uses:

| From plan 1 | What it is |
|---|---|
| Truthful tool annotations, plus the bundle switch `--tool-annotations all-read-only` | the ADR that superseded 0076 |
| T1 portability test | runs on every PR |
| T2 replay test | parametrized over `tests/fixtures/ai_clients/<client>/` |
| T3 harness | `tests/integration/ai_client_harness.py`, the lockfile in `tests/integration/ai_clients/` and the weekly `ai-client-canary.yml` matrix |
| Test org deployment, evidence format, per-client script | `ai-client-qa.md` |
| `connect-<slug>.md` docs pages | GA4 content group `ai-agent` |
| The website's **AI agents** menu | guardrail 14 |

Plan 1's rules apply unchanged:

- one PR per package;
- no unverified instructions on `main`;
- a client's website page lands only after the release that carries its docs page;
- no logos;
- ADR numbers are the next free one at merge time.

Plan 2 (ChatGPT) runs independently of this one.

---

## 0. How to use this plan

It works the same way as plan 1. To run a wave, paste into a new Claude Code on the web session:

```text
Implement WAVE <n> of docs/gemini-support-plan.md. You are the coordinator: one child session per
work package (create_session, the package's "Session prompt" word for word plus this plan's §1),
tagged "gemini-support:wave-<n>"; track each until its PR is green (get_session, send_later about
hourly); then post a checklist of WP, PR, CI state, and unblocked 🧑 tasks. Start a package only
when its "Depends on" items are done.
```

---

## 1. Gemini-specific facts (checked 2026-09-26)

1. **Gemini CLI connects straight to `/mcp`**, with `httpUrl`. In local mode it adds a bearer
   header; in org mode it uses OAuth with dynamic client registration (DCR). It doesn't use the
   `.mcpb` shim, and none should be packaged for it.
2. **Attribution.** `REGISTRY` maps `gemini-cli` ← `gemini-cli-mcp-client`, which
   [ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
   verified from Gemini CLI's source. In org mode the DCR `client_name` wins over
   `clientInfo.name`, so we capture the name it registers with. `gemini-cli.png` exists. A new
   `gemini-enterprise` entry needs an icon and a license row (`test_agent_label.py`).
3. **Guardrail 10** bans `\bGemini\b` on every hand-written page. Listing Gemini CLI while Gemini
   Enterprise isn't supported yet needs a finer not-yet list (WP 2.2).
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
6. **CI.** `org-mode-smoke` has no Node step, and its harness is
   `tests/integration/test_org_ubuntu_release_smoke.py` + `mock_idp.py`. The `test` job has Node
   and Playwright.

---

## 2. Decisions

| # | Decision | Recommendation |
|---|---|---|
| D3 | Public claim wording | At Gate A: "Gemini CLI (local and organization deployments)". Gemini Enterprise is added only after Gate C. |
| D4 | How I393 writes a client while the daemon runs → **ADR** | A `privacyfence-app --register-oauth-client` subcommand that **refuses to run while the daemon holds its lock**. It writes through a new `OrgOAuthProvider.register_static_client()`, and the record carries `"source": "admin"` so it's never pruned as stale. It gets a `gemini-enterprise` registry entry with an icon. Later, maybe: the same action on org **Settings → AI systems** with step-up (ADR 0034). |
| D15 | Spark (I394) | Research only. It's worth a smoke test only if it can reach a self-hosted host with DCR or with D4's pre-registered client. Otherwise close it as "not a target audience". |

---

## 3. Roadmap

```text
🧑 M0  plan 1 finished; Gemini CLI installed; test org deployment (ai-client-qa.md)
│
🤖 WAVE 1   WP1.1 Gemini CLI in T3 + canary │ WP1.2 seeded T2 fixture + draft connect-gemini-cli.md (draft PR)
│           WP1.3 re-source Gemini Enterprise / Spark vendor facts
│
🧑 M1  Gemini CLI local → Gemini CLI org
│
🤖 WAVE 2   WP2.1 captured fixture, finish and merge connect-gemini-cli.md
🧑 M2  stable release
🤖          WP2.2 website: clients.json + /ai-agents/gemini-cli/ + finer guardrail 10
│
★ GATE A — "Works with Gemini CLI"                    (I392 done, with WP3.1 for org CI)
│
🤖 WAVE 3   WP3.1 Gemini CLI org-mode OAuth in CI
🧑 M3.1  confirm Google's Gemini Enterprise requirements
🤖 WAVE 4   WP4.1 ADR + pre-registered OAuth clients + tests + connect-gemini-enterprise.md
🧑 M4  register, configure, verify; release
🤖          WP4.2 website: /ai-agents/gemini-enterprise/
│
★ GATE C — "Gemini Enterprise"                          (I393 done)
│
🤖 WAVE 5   WP5.1 Spark research → 🧑 M5          (I394)
🤖 WAVE 6   retire this plan
```

---

## 4. Waves (🤖 Claude)

### 🤖 WAVE 1 (3 parallel sessions)

#### WP 1.1: Gemini CLI in the T3 harness and the canary · `tests:`

```text
Read docs/gemini-support-plan.md §1. Implement WP 1.1 on plan 1's T3 harness:
1. tests/integration/ai_clients/package.json: add @google/gemini-cli (exact version); refresh the
   lockfile with npm install; tests use npm ci.
2. tests/integration/test_gemini_cli_contract.py using ai_client_harness.py: throwaway HOME with
   .gemini/settings.json {"mcpServers":{"privacyfence":{"httpUrl":"<url>","headers":
   {"Authorization":"Bearer <token>"}}}}; `gemini mcp list` shows privacyfence connected with a
   non-zero tool count. No Google credential — if the pinned version needs one even for
   `mcp list`, stop and report. GEMINI_CLI_BIN overrides the binary.
3. ai-client-canary.yml: add gemini-cli to the matrix.
Run /dod; one PR "tests: Gemini CLI contract test (T3)"; drive to green.
```

#### WP 1.2: seeded Gemini CLI fixture and draft `connect-gemini-cli.md` · `tests:` + draft PR

```text
Read docs/gemini-support-plan.md §1–§2 and ai-client-qa.md. Implement WP 1.2 as TWO PRs:
PR A (merge) "tests: seeded Gemini CLI handshake fixture": tests/fixtures/ai_clients/gemini-cli/
(register.json with redirect http://localhost:7777/oauth/callback, initialize.json with
clientInfo gemini-cli-mcp-client, README "SEEDED FROM VENDOR DOCS, NOT YET CAPTURED", citing
sources, expected agent_id gemini-cli). The replay test picks it up.
PR B (DRAFT; WP 2.1 finishes it) "feature: Connect Gemini CLI": docs/connect-gemini-cli.md in the
connect-claude-*.md template, under docs/README.md "AI agent setup": local mode (settings.json
httpUrl + headers, and `gemini mcp add --transport http privacyfence <mcp_url> --header
"Authorization: Bearer $(<platform binary> --print-mcp-token)"`, per platform); organization mode
(httpUrl without headers, `/mcp auth`); no shim; Confirmations (Gemini CLI's own tool prompt, and
the --tool-annotations switch); files via capability URLs; pinning. Org guide §9 bullet. A
"Verification pending" note linking https://github.com/privacyfence/privacyfence/issues/392.
```

#### WP 1.3: re-source Gemini Enterprise and Spark facts · research, no PR

```text
Read docs/gemini-support-plan.md. Research only. From Google's OWN pages record URL + date for:
(c) Gemini Enterprise custom MCP server connector: DCR or not, fixed redirect URI, required
    fields (authorization URL, token URL, scopes, client auth method), console menu path.
(d) Gemini app "Spark" custom apps: DCR or pre-registration, TLS, transport, can it reach a
    self-hosted host?
Comment on issues 393 and 394, separating "confirmed on <url>" from "could not reach". Unreached
pages become part of M3.1.
```

---

### 🧑 M1: Gemini CLI checks (about 30 min)

Follow `ai-client-qa.md`'s per-client script, under both annotation modes.

**M1.1 Gemini CLI, local mode** (I392)

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
3. Run `gemini`, then `/mcp`. **Expected:** `privacyfence` is 🟢 connected, with its tools
   listed.
4. Prompt: `Use privacyfence to list my calendar events for tomorrow.` **Expected:** it succeeds.
5. Prompt: `Use privacyfence to create a calendar event "PF test" tomorrow 10:00–10:15.`
   - Note whether Gemini CLI asks first, under each annotation mode.
   - Approve through the companion's **Open Approvals**.
   - Check that the event exists.
6. Upload and download one Drive file. **Expected:** it uses `privacyfence_create_upload_slot`
   plus a `PUT`, and a `/mcp-files/fetch/…` URL.
7. Post the evidence on **I392**, titled "M1.1 result".

**M1.2 Gemini CLI, org mode** (I392)

1. Run `gemini mcp add --transport http privacyfence-org https://pf-test.<your-domain>/mcp` (no
   header).
2. In `gemini`, run `/mcp auth privacyfence-org`. **Expected:** PrivacyFence `/login` → Google →
   "authentication successful".
3. `/mcp` shows it 🟢 connected. Repeat steps 4–5 of M1.1, approving at `/approvals`.
4. Pin the registration on **Settings → AI systems**. The next card says verified.
5. Post the evidence on **I392**, titled "M1.2 result", with the `oauth_clients.json` entry
   (secret redacted).

---

### 🤖 WAVE 2 → ★ GATE A

#### WP 2.1: fold in the evidence · `feature:`

Depends on: M1.1 passed.

```text
Read docs/gemini-support-plan.md and the M1 evidence on issue 392. Take over WP 1.2's draft PR B:
1. Replace the SEEDED gemini-cli fixture with the captured one (scrubbed; README "captured <date>,
   client version <v>").
2. REGISTRY gemini-cli: add the observed DCR client_name if it differs from clientInfo's; unit
   tests; if anything changes, a new ADR amending ADR 0035.
3. connect-gemini-cli.md: remove "Verification pending" for what passed; Confirmations as observed.
   ai-client-qa.md rows.
4. CHANGELOG [Unreleased] "Added": Gemini CLI (local and organization), per D3. README client
   mentions (not the canonical description).
Run /dod; "feature: verified Gemini CLI support"; drive to green.
```

Then **🧑 M2**: cut a stable release that carries WP 2.1.

#### WP 2.2: Gemini CLI on the website · `feature:`

Depends on: that release's `/docs/` has `connect-gemini-cli`.

```text
Read docs/gemini-support-plan.md §1. Implement WP 2.2:
1. clients.json: "Gemini CLI", ["local","organization"], connects "straight to /mcp over HTTP",
   slug gemini-cli.
2. website/ai-agents/gemini-cli/index.html (the /ai-agents/ structure); PAGES, llms.txt, the index.
3. tests/unit/test_website_clients.py: add to NAMES; replace the bare "Gemini" in the not-yet list
   with "Gemini Enterprise" and "Gemini app" so Gemini CLI may be named and those may not.
4. Canonical description (guardrail 1): if its client examples change, change it, README.md's
   opening and website/index.html in one commit.
5. CHANGELOG.
Run /dod; one PR "feature: privacyfence.eu lists Gemini CLI"; drive to green.
```

**★ GATE A is passed when:** WP 2.2 is merged and deployed. Close I392 when WP 3.1 is merged too.

---

### 🤖 WAVE 3: Gemini CLI org mode in CI

#### WP 3.1 · `tests:`

```text
Read docs/gemini-support-plan.md §1. Extend tests.yml's org-mode-smoke job
(test_org_ubuntu_release_smoke.py + mock_idp.py) with a Gemini CLI run against the org daemon:
add actions/setup-node (pinned by SHA, Node 22) and `npm ci` of tests/integration/ai_clients;
settings.json httpUrl without headers; drive the OAuth browser leg headlessly (first a BROWSER
script that follows the redirect chain with a cookie jar; else Playwright/Chromium, installed as
the `test` job does); `gemini mcp list` must show connected. Assert the DCR client landed in
oauth_clients.json and its client_name maps to the gemini-cli agent. Add the org variant to
ai-client-canary.yml. Run /dod; one PR; drive to green.
```

---

### 🧑 M3.1: confirm Google's Gemini Enterprise requirements (20 min)

1. Open <https://cloud.google.com/gemini/enterprise/docs> and search for **"custom MCP server"**,
   or use WP 1.3's findings or I393's source 7.
2. Write down:
   - Does it support DCR? If **yes**, WAVE 4 becomes docs only, the same shape as WP 1.2 PR B.
   - The exact redirect URI.
   - The fields it asks for.
   - The client authentication method (`client_secret_post` or `_basic`).
   - The console menu path.
3. Post them on **I393**, titled "M3.1 requirements".

---

### 🤖 WAVE 4: Gemini Enterprise, the only server code (I393) → ★ GATE C

#### WP 4.1: ADR, pre-registered OAuth clients, tests, docs · `feature:`

```text
Read docs/gemini-support-plan.md (§1 items 2 and 4, D4) and the M3.1 findings on issue 393.
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
6. docs/connect-gemini-enterprise.md (the connect template, "AI agent setup"), and an org guide
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
and take it off guardrail 10's not-yet list. CHANGELOG. Run /dod; one PR; drive to green.
```

**★ GATE C is passed when:** WP 4.2 is deployed and I393 is closed.

---

### 🤖 WAVE 5: Spark (I394, low priority)

```text
Read docs/gemini-support-plan.md (D15) and issue 394 with WP 1.3's findings. Recommend on issue
394: (i) a smoke test (DCR path, or WP 4.1's pre-registered client) or (ii) close as not a target
audience. No code.
```

**🧑 M5:** if the recommendation is a smoke test: in <https://gemini.google.com> → **Settings** →
**Apps / Connected apps** → **Add custom app**, enter the `/mcp` URL and run the
`ai-client-qa.md` script. Otherwise close I394.

### 🤖 WAVE 6: retire this plan

```text
Issues 392–394 are closed. For every "→ ADR" marker confirm an ADR exists (write any missing
one); delete this file, remove it from build_site.CONTRIBUTOR_DOCS and docs/README.md, fix links
to it, and name the ADRs in the PR description.
```

---

## 5. Issue closure map

| Issue | Closed by | Evidence it needs |
|---|---|---|
| I392 | WP 2.1 + WP 3.1 | M1.1, M1.2 logged; T2 and T3 (local and org) green |
| I393 | WP 4.1, then WP 4.2 | M3.1 and M4 logged |
| I394 | WAVE 5 → M5 | a comment with the decision |

## 6. Accounts you need

| Need | For | How |
|---|---|---|
| Node 20+ and Gemini CLI on your Mac | M1 | `brew install node`, then `npm install -g @google/gemini-cli`, then run `gemini` once → **Login with Google** |
| The test org deployment | M1.2, M4 | `ai-client-qa.md` → "Test organization deployment" |
| A Google Cloud project with **Gemini Enterprise** | M3.1, M4 | <https://console.cloud.google.com/gemini-enterprise> (a trial is enough) |
