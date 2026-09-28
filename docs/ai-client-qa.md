# AI-client QA

How to check, by hand, that a real AI client works with PrivacyFence: the test organization
deployment the organization-mode runs need, the script each client is taken through, the evidence
each run records, and the table that holds the results. It covers what no automated tier can decide:
clients CI cannot run (claude.ai, the Claude Desktop UI), and the prompts a client shows before a
write. What CI proves automatically is in [`testing-policy.md`](testing-policy.md); when this check
is due before a release is in [`release-testing.md`](release-testing.md).

Every run ends by saving a recorded-handshake fixture, so that what a human verified once is
replayed on every pull request afterwards.

## When to run it

Run the script for each supported client family before a release that changed the client-facing
surface:

- `/mcp` itself (`web/routes_mcp.py`), the tool list it advertises (`web/mcp_tools.py`), or tool
  annotations;
- the organization-mode OAuth server (`web/oauth_provider.py`, `/register`, `/authorize`, `/token`,
  the `.well-known` metadata);
- agent attribution (`agent_identity.py`, the **AI systems** pins);
- file delivery to clients without the extension (`privacyfence_create_upload_slot`, staged
  download links, [ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md));
- the extension (`mcpb/`), for the Claude Desktop local row.

Run it for a new client before it is listed as supported, and whenever a client's own release is
suspected of changing its behavior toward PrivacyFence.

## Test organization deployment

The organization-mode rows need a PrivacyFence organization deployment reachable over public
HTTPS: claude.ai connects from Anthropic's servers, not from your browser, so `localhost` or a
private network does not work. Keep one deployment for QA only, separate from any production
install. Setting it up takes one to three hours the first time.

1. Get a small Linux VM, 2 vCPU and 2 GB RAM. Ubuntu 24.04 is the reference.
2. DNS: create an **A** record `pf-test.<your-domain>` pointing at the VM's IP address.
3. Follow [`org-mode-setup-guide.md`](org-mode-setup-guide.md) sections 2 to 9 in order. In
   particular:
   - **Section 4, identity provider.** For Google: **APIs & Services → Credentials → + Create
     credentials → OAuth client ID → Web application**, with the three redirect URIs from section
     4's table on your `pf-test` hostname.
   - **Section 7, reverse proxy and TLS.** Caddy, which obtains a Let's Encrypt certificate itself.
   - **Section 8, hardened systemd unit.** `privacyfence-org.service`.
4. From your own machine, check the authorization server is reachable:

   ```bash
   curl -s https://pf-test.<your-domain>/.well-known/oauth-authorization-server | head
   ```

   The output must list `registration_endpoint`.
5. Sign in at `/login`. On `/connect`, connect **Google Calendar** (for the read and the gated
   write) and **Google Drive** (for the file steps). Enroll a passkey on `/security` if step-up is
   on.

For the local-mode rows, use a packaged install of the build under test (the `build.yml`
pre-flight's artifact, see [`release-testing.md`](release-testing.md#gates-in-order)), connected to
the same Google Calendar and Drive accounts.

## What the client receives

PrivacyFence advertises every tool truthfully, and there is only one annotation mode
([ADR 0089](adr/0089-tool-annotations-are-always-truthful.md)): read tools with
`readOnlyHint=true`; write tools with `readOnlyHint=false`; the tools that delete with
`destructiveHint=true`. The question each run answers is whether the client asks for its own
confirmation before a write, and whether it lets the user always allow the tool so that it stops
asking. PrivacyFence's approval card appears either way; a client prompt comes on top of it.

## Per-client script

Take each client through these steps, in order. Set the client up as its
own docs say: for local mode, the "Connect Claude Desktop" and "Connect Claude Code" sections of
[`install-macos.md`](install-macos.md), [`install-windows.md`](install-windows.md) and
[`install-linux.md`](install-linux.md); for organization mode,
[`org-mode-setup-guide.md`, "Add PrivacyFence to an AI client"](org-mode-setup-guide.md#add-privacyfence-to-an-ai-client).

1. **Tool list.** The client lists PrivacyFence's tools (`tools/list`): the connector tools of the
   connected services and the `privacyfence_*` tools. Note any tool the client drops, renames or
   refuses.
2. **One read.** Ask for something ungated, for example today's events from `calendar_list_events`.
   It returns without an approval card.
3. **One gated write.** Ask for a write, for example a new event with `calendar_create_event`.
   - Note whether the client showed **its own confirmation first**, before any call reached
     PrivacyFence, and what it said, and whether it offered to always allow the tool. If it did,
     always-allow it and repeat the write: the client no longer asks, and PrivacyFence's card
     still does.
   - An approval card appears: in local mode through the companion's **Open Approvals**
     ([ADR 0062](adr/0062-only-a-companion-attested-session-may-approve.md)), in organization mode
     at `/approvals` after sign-in. Note its "AI system" line.
   - Approve it. The client repeats the identical call and the write completes.
4. **Files**, for a client without the extension (every client except Claude Desktop with
   `PrivacyFence.mcpb`) ([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)):
   - **Upload:** ask the client to upload a small local file to Drive. It calls
     `privacyfence_create_upload_slot`, `PUT`s the file to the returned URL, and passes the
     `upload_id` to `drive_upload_file`. Approve the card; the file appears in Drive.
   - **Download:** ask it to download a Drive file larger than the inline limit (organization mode:
     `--downloads-inline-max-bytes`, a 100,000-byte tool result by default, so any file over about
     75 KB). The tool result carries a one-time link, and the file reaches you through it. On
     claude.ai, check that the host is on **Settings → Capabilities → Domain allowlist** first
     ([ADR 0097](adr/0097-a-download-link-needs-a-client-that-can-reach-the-server.md)); without
     it the fetch fails from claude.ai's sandbox, so the access log shows no
     `/mcp-files/fetch/` request.

   For Claude Desktop with the extension, do the same upload and download through local paths
   instead: the extension reads and writes the file as you.
5. **Attribution.** Open the audit entry for the gated write and note `agent_id`, `agent_name`,
   `agent_version` and `agent_source`
   ([`how-it-works.md`, "Which AI system is asking"](how-it-works.md#which-ai-system-is-asking)).
   The audit log is under the data directory: `authority/logs/audit/` for a local install's first
   account, `users/<principal>/logs/audit/` in organization mode. An unrecognised client is
   recorded as `agent_id="unknown:<name>"`, which gives the exact name it sent.
6. **Pin (organization mode only).** On **Settings → AI systems**, find the client's registration,
   note its registered name, and pin it to the right AI system
   ([`org-mode-setup-guide.md` section 11](org-mode-setup-guide.md#11-ai-systems)). Make another
   gated write: the card shows the client as verified, and the audit entry records
   `agent_source=oauth_client`. Unpin afterwards if the next run should start unverified.
7. **Save a fixture.** Record what the client sent, so the replay test can repeat it on every pull
   request, in `tests/fixtures/ai_clients/<client>/`:
   - organization mode: the client's entry in `org/oauth_clients.json` under the data directory
     (`/var/lib/privacyfence-org/.privacyfence/` on the reference deployment), with
     `client_secret` replaced by `REDACTED` — its `client_name`, `redirect_uris` and
     `token_endpoint_auth_method` are what the client registered;
   - the handshake's `clientInfo` name and version, from the audit entry in step 5;
   - the order of the client's requests (`/.well-known/…`, `/register`, `/authorize`, `/token`,
     `/mcp`), from the reverse proxy's access log for the run.

   Open a pull request with the fixture; a run is not finished until it is saved.

## Evidence to record

Post one comment per client run on the issue or pull request the run belongs to, with:

- **The client:** name and version, OS, date, and the PrivacyFence version.
- **The result:** pass or fail per step of the script, and screenshots of any failure.
- **Client confirmation:** whether the client asked for its own confirmation before the write, its
  wording, and whether always-allowing the tool stopped it asking.
- **Attribution:** the approval card's "AI system" line, and the audit entry's `agent_id`,
  `agent_name`, `agent_version` and `agent_source`.
- **Organization mode only:**
  - the client's `org/oauth_clients.json` entry, with `client_secret` replaced by `REDACTED`;
  - that after pinning it on **Settings → AI systems**, the next card said verified.

## Recording results

Add a row per run to the table below in the pull request that saves its fixture, replacing the row
of the same client and mode. **Result** is `pass`, or `fail` with a link to the evidence.
**Registered `client_name`** is organization mode only (`—` for local mode). **Client
confirmation?** is `yes` (what it asked, and whether always-allowing the tool stopped it) or `no`.
**Fixture captured** links the fixture directory, or says `no` and why.

| Client | Version | OS | Mode | Date | Result | Registered `client_name` | `clientInfo` name | Client confirmation? | Fixture captured |
|---|---|---|---|---|---|---|---|---|---|
| Claude Desktop (extension) | 2.9939.2 | macOS 27.0 | local | 2026-09-28 | [pass](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865556451) | — | `local-agent-mode-privacyfence` 1.0.0 | yes, before the write with default tool permissions; always-allow stopped it, PrivacyFence's card still shown | no: local mode has no registration |
| Claude Desktop (custom connector) | | | org | | not run: no account available allowed custom connectors | | | | no: most likely registers like claude.ai, unverified |
| Claude Code | 2.1.283 | macOS 27.0 | local | 2026-09-28 | [pass](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865032001) | — | `claude-code` 2.1.283 | no: Claude Code does not gate MCP tools by their annotations | no: local mode has no registration |
| Claude Code | 2.1.283 | macOS 27.0 | org | 2026-09-28 | [pass](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865527889) | `Claude Code (privacyfence)` (the part in parentheses is the `claude mcp add` server name) | `claude-code` 2.1.283 | not recorded | [`claude-code/`](../tests/fixtures/ai_clients/claude-code/) |
| claude.ai | web | web | org | 2026-09-28 | [fail at step 3](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865753049): a 1.7 MB inline download was truncated by claude.ai, so step 4 did not run; steps 1–2 passed. The default inline limit has since been lowered ([ADR 0092](adr/0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)) | `Claude` | not observable (the registered name decides first); version 1.0.0 | yes, before the write; always-allow stopped it, PrivacyFence's card still shown | [`claude-ai/`](../tests/fixtures/ai_clients/claude-ai/) |
| ChatGPT (Developer Mode, personal plan) | web | web | org | 2026-09-28 | [pass](https://github.com/privacyfence/privacyfence/issues/391#issuecomment-5871022834), against 5.0.0: the download link and the upload `PUT` worked from ChatGPT's code sandbox with no allowlist; the approved upload then failed because its approval had waited, a PrivacyFence bug since fixed ([ADR 0102](adr/0102-an-upload-slot-is-consumed-after-the-gate-not-before.md)); `drive_get_file_content` not run | `ChatGPT` | not observable (the registered name decides first; not `openai-mcp`); version 1.0.0 | no before the calendar write, under the permission setting "Allow low-risk tools"; yes once, before the upload, and always-allow stopped it; PrivacyFence's card still shown | [`chatgpt/`](../tests/fixtures/ai_clients/chatgpt/) |
| ChatGPT desktop (personal plan) | 26.924.22138 | macOS | local | 2026-09-28 | [pass](https://github.com/privacyfence/privacyfence/issues/390#issuecomment-5872180177), against 5.0.0: added as a Streamable HTTP server with an `Authorization: Bearer` header; the one-time download link on `127.0.0.1` fetched from ChatGPT's command sandbox; upload through `privacyfence_create_upload_slot` | — | `codex-mcp-client` 0.158.0-alpha.2.1 (the Codex command-line tool's name too) | yes, before tools that are not read-only, with an always-allow option | no: local mode has no registration |
