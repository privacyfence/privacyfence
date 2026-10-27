# Configuration reference

PrivacyFence reads two configuration files:

- **`settings.yaml`**: your policy and the daemon's own settings. It carries no secrets. Most of it
  is edited from PrivacyFence **Settings**; this page lists every key, including the ones with no
  control there.
- **`org_config.json`**: the organization config bundle. It holds the app credentials for each
  connected service and, for an organization deployment, the server and identity-provider
  settings. An administrator builds it with `scripts/build_org_bundle.py` and people install it from
  **Settings > General > Organization Configuration** (or it is placed on the server by hand).

Where both files live on each platform is in [Platform support](platform-support.md). Both are
owned by PrivacyFence's service account on a packaged install, so editing either by hand needs
administrator rights.

The command-line options of `privacyfence-app` are at the end of this page
([Command-line options](#command-line-options)).

## How settings.yaml is read

- **First start.** If `settings.yaml` does not exist, the daemon creates it from the packaged
  example (`src/privacyfence/resources/settings.yaml.example`). The **Seeded** column below is the
  value that example writes.
- **Missing keys.** A key that is absent (or commented out) takes the **Code default**. Where the
  two columns differ, a fresh install behaves as **Seeded** and an install whose file lacks the key
  behaves as **Code default**.
- **When changes apply.** A change made from Settings applies straight away. A hand edit to the file
  applies at the next daemon start.
- **Validation.** An invalid privacy-filter value, step-up `scope` or `batch`, or an unreadable file
  stops the daemon at start with a configuration error naming the key. Auto-accept rules it cannot
  read are dropped, never treated as a match.
- **Organization mode.** The server's own `settings.yaml` is install-wide: `privacy`, the other
  `*_privacy` groups, `pii_detection`, `logging` and `web.approvals` apply to everyone, and
  `web.mcp.enabled` must be `true`. Each person also gets a `settings.yaml` of their own, of which
  only the `auto_accept` section is used. Keys marked *local mode only* have no effect on an
  organization server.

### Exporting and importing personal settings

**Settings > Auto-accept > Export…** writes `privacyfence-personal-settings.json` (UTF-8 JSON, at most
256 KiB and 500 rules to import); **Import…** reads one back. It works in both modes and in every
direction. Only the person's own `auto_accept` rules and step-up level move.

| Key | Meaning |
|---|---|
| `format` | Always `privacyfence-personal-settings` |
| `format_version` | `1`; a newer version is refused |
| `exported_at` | UTC ISO-8601 time of the export |
| `source` | `mode` (`local` or `org`) and the PrivacyFence `version` |
| `auto_accept` | `version: 3` and the `rules` rows, without their former ids; custom rules are left out |
| `step_up` | `scope`: the person's own step-up scope, or `null` |
| `not_portable` | Informational list of what does not move; import ignores it |

Import previews before it changes anything and gives each rule a status:

| Status | Meaning |
|---|---|
| `add` | A new rule |
| `extend` | Adds tools to a rule you already have |
| `present` | Already covered; nothing to do |
| `skip` | Not imported, with the reason shown (custom rule, unknown tool, cannot be applied by this version) |

Import never removes or narrows anything. See
[Moving a person from a desktop install](org-mode-setup-guide.md#moving-a-person-from-a-desktop-install).

### Earlier policy sections

A `settings.yaml` that contains an `auto_accept_rules` or `auto_accept_grants` section (even an
empty one) is refused at start with a configuration error naming the section. Rules in those
sections are not converted: remove the section and recreate the rules on the **Auto-accept** page.

If the same file also carries the top-level marker `migrated_to_policy_v2: true`, its rules are
already in the `auto_accept` section. The daemon then removes `auto_accept_rules`,
`auto_accept_grants` and the marker, rewrites the file, logs a warning naming what it removed, and
starts. If the rewrite fails, it starts anyway and tries again next time. See
[ADR 0041](adr/0041-only-the-current-install-layout-is-supported.md) and
[ADR 0047](adr/0047-settings-an-earlier-release-converted-are-cleaned-up-not-refused.md).

## settings.yaml keys

### Privacy filter

Six groups, one per kind of data, each with a `default_policy` and per-category values. Each value
is `allow` (pass unchanged), `redact` (replace with a placeholder that gives only the length; for a
list-shaped category, same as `block`) or `block` (replace with `[BLOCKED BY PRIVACY FILTER]`). The
filter applies before you see the data on a card, and it is a minimum: approvals still apply. Edited
from **Settings > Privacy Filter**. Details: [Approvals and policy](approvals-and-policy.md).

Code defaults: a category that is not listed takes its group's `default_policy`. A group that is
missing altogether is `allow` for everything in local mode and `block` for everything on an
organization server.

| Key | Seeded | Covers |
|---|---|---|
| `privacy.default_policy` | `block` | Gmail: any category not listed below |
| `privacy.categories.body` | `allow` | Message body text |
| `privacy.categories.metadata` | `allow` | Sender, recipients, date, subject |
| `privacy.categories.attachments` | `block` | Attachment metadata (downloading content is a separate, gated tool) |
| `privacy.categories.thread_history` | `allow` | Earlier messages in a thread |
| `drive_privacy.default_policy` | `block` | Drive, Sheets and Docs: any category not listed below |
| `drive_privacy.categories.file_content` | `allow` | Document text or bytes |
| `drive_privacy.categories.file_metadata` | `allow` | Name, owners, times, sharing, from `drive_get_file_metadata`; the version list from `drive_list_revisions` and the version time `drive_get_revision_content` returns |
| `drive_privacy.categories.file_list` | `allow` | Names and ids in list and search results |
| `drive_privacy.categories.folder_structure` | `allow` | Folder listings |
| `slack_privacy.default_policy` | `block` | Slack: any category not listed below |
| `slack_privacy.categories.message_content` | `allow` | Message text |
| `slack_privacy.categories.user_identity` | `allow` | User names, emails, real names |
| `slack_privacy.categories.channel_list` | `allow` | Channel names and metadata |
| `slack_privacy.categories.thread_content` | `allow` | Thread replies |
| `slack_privacy.categories.dm_list` | `allow` | Who you have one-to-one DMs with |
| `slack_privacy.categories.group_chat_list` | `allow` | Group DM listings (id, name, participants) |
| `contacts_privacy.default_policy` | `block` | Contacts: any category not listed below |
| `contacts_privacy.categories.notes` | `block` | The free-text notes (biography) field. Name, email, phone, organization and job title always pass. |
| `tasks_privacy.default_policy` | `block` | Tasks: any category not listed below |
| `tasks_privacy.categories.notes` | `block` | A task's free-text notes. Title, due date and status always pass. |
| `confluence_privacy.default_policy` | `block` | Confluence: any category not listed below |
| `confluence_privacy.categories.search_excerpt` | `block` | The page excerpt `confluence_search` and `confluence_cql_search` return with each match |
| `confluence_privacy.categories.attachments` | `block` | Attachment metadata |

`file_metadata` and `file_list` overlap (both can show a file's name and owners). If one is `allow`
and the other is not, the daemon logs a warning at start.

### Connector behaviour

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `calendar.free_busy_full_event_details` | bool | `true` | `true` | `calendar_get_free_busy` returns event titles, times and status for a colleague when your account can already see them (busy/free only otherwise). `false` always returns busy/free only. **Settings > Privacy Filter**. |
| `gmail.append_signature_to_drafts` | bool | `false` | `false` | Append your Gmail signature (the one Gmail stores for the sending address) to drafts. Each draft tool's `include_signature` argument overrides it per call. The signature is shown on the card. **Settings > Privacy Filter**. |
| `salesforce.report_max_pages` | int | `50` | `50` | Most report runs one paged `salesforce_run_report` read or one `salesforce_export_report` export may use (each page is one run and counts against the org's report-run limits). A read that needs more fails. |
| `connectors.<name>.enabled` | bool | `true` | not set | `false` turns a connector off. `<name>` is one of `gmail`, `drive`, `calendar`, `contacts`, `tasks`, `apps_script`, `slack`, `salesforce`, `jira`, `confluence`, `telegram`, `grist`, `miro`, `peek`. Written by **Disable**/**Enable** on **Settings > Connectors**. A connector also needs its organization config and a sign-in before it runs. |
| `connectors.assets.enabled` | bool | `false` | not set | `true` turns Jira Assets on and makes the Atlassian sign-in ask for the Assets scopes; an explicit value wins over the bundle's `atlassian.assets` flag, which is the default when this key is absent (see [Atlassian setup](atlassian-setup.md#jira-assets)). Sign in again after switching it on. |
| `connectors.assets.workspace_id` | string | none | not set | The site's Assets workspace id (a UUID); wins over the bundle's `atlassian.assets_workspace_id`. Jira Assets needs one of the two, since PrivacyFence cannot look the id up itself (see [Atlassian setup](atlassian-setup.md#jira-assets)). |

### PII detection

The PII check scans content shown on a card for likely personal data (bank account numbers, card
numbers, national ID and tax numbers and similar, in English, German and Hungarian; email addresses
and phone numbers are not detected) and asks for a second confirmation when it finds some. See
[PII detection keywords](pii-detection-keywords.md) and [Approvals and policy](approvals-and-policy.md).

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `pii_detection.enabled` | bool | `true` | `true` | Turns the PII check on. **Settings > General > PII Detection Gate**. |
| `pii_detection.detect_ip_addresses` | bool | `true` | `true` | Also flag IP addresses. Same card. |
| `pii_detection.detect_financial_figures` | bool | `true` | `true` | Also flag currency amounts. Same card. |
| `pii_detection.audit_match_details` | bool | `false` | `false` | Record the matched text in the audit log's PII match details column: redacted for value-bearing categories (bank and card numbers, IDs) on an approved request, a fixed "details hidden" text on a denied one. For a bounded trial only; no Settings control; needs a restart. |

### Auto-accept rules

The `auto_accept` section holds the rules that let a gated call run without a card. It is written
by the **Always allow** button, **Settings > Auto-accept** and `privacyfence_propose_policy_change`,
which all validate the rule and compute its id; prefer them to hand-editing. The scopes, verbs and
conditions a rule can use are in [Approvals and policy](approvals-and-policy.md).

| Key | Type | Code default | Seeded | What it is |
|---|---|---|---|---|
| `auto_accept.version` | int | — | `3` | Schema version. Always `3`; a version-2 file is converted on the next start and the previous file is kept as `settings.yaml.pre-scope-rules.bak`. |
| `auto_accept.rules` | list | empty (no rules) | one rule, below | The rules, one per scope. A call auto-accepts if any rule matches. |
| `auto_accept.rules[].id` | string | — | `r-d76028ec0b` | Stable id. A scoped rule's id is computed from its scope and value; a custom rule keeps the id it was written with. |
| `auto_accept.rules[].scope` | string | — | absent | The scope a scoped rule trusts. Absent on a custom rule. |
| `auto_accept.rules[].predicate` | string | — | `always_allow` | The scope selector of a custom rule. Absent on a scoped rule. |
| `auto_accept.rules[].value` | any | — | `null` | The resource ids or names the scope matches, or `null` for a scope without a value. |
| `auto_accept.rules[].verbs` | list | — | `[update]` | The verbs the rule allows (derived from `operations` for a custom rule; display only). |
| `auto_accept.rules[].operations` | list | — | `[contacts.edit]` | Internal operation keys the rule covers. |
| `auto_accept.rules[].conditions` | list | `[]` | `[[no_contact_info_change, null]]` | Custom rules only: `[name, value]` pairs that must all hold. |
| `auto_accept.rules[].former_ids` | list | `[]` | absent | The ids of the version-2 rules the upgrade merged into this rule. |

The seeded rule auto-accepts contact edits that change no email address or phone number. A missing
or empty `auto_accept` section means nothing auto-accepts.

### Logging and updates

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `logging.level` | string | `INFO` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. **Settings > Audit Log**, applied immediately. |
| `logging.file` | string | `logs/privacyfence.log` | `logs/privacyfence.log` | Daemon log file. A relative path is resolved against PrivacyFence's data directory. |
| `update_check.enabled` | bool | `true` | `true` | Once a day, check GitHub Releases for a newer version and show a banner. Never downloads or installs anything; a network failure is logged, not shown. **Settings > General > Check for Updates**. Local mode only. |
| `update_check.include_beta` | bool | `false` | `false` | Also offer pre-releases. Same card. |

### Web server and MCP (local mode only unless noted)

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `web.port` | int | `8765` | `8765` | Port of the one local server that carries Approvals, Settings and `/mcp`. It listens on `localhost` only. An organization server takes its port from the bundle's `server.port`. |
| `web.mcp.enabled` | bool | **`false`** | **`true`** | Serve `/mcp`. With it off, no AI system can reach PrivacyFence at all. Must be `true` on an organization server. |
| `web.settings.enabled` | bool | **`false`** | **`true`** | Serve Settings (`/settings`). With it off, the only way to change anything is to edit the files by hand. |
| `web.settings.allow_quit` | bool | `true` | `true` | Whether **Quit PrivacyFence** on **Settings > About** works. It always asks for confirmation first. |
| `web.notifications.enabled` | bool | `true` | `true` | Browser notifications for a new pending approval while the Approvals tab is open but not focused. Nothing is sent through a push service. |
| `web.notifications.detail` | string | **`minimal`** | **`standard`** | What a notification about a single approval may say: `minimal` ("1 approval pending"), `standard` (adds connector, tool and direction), `detailed` (adds the card's title line, which can contain gated content, e.g. a recipient). A notification about several approvals always uses `minimal`. **Settings > General > Approval Notifications**. |
| `web.approvals.hold_window_seconds` | number | `30` | not set | How long a gated call waits for your decision before returning `approval_pending`. Local and organization mode. |
| `web.approvals.pending_ttl_seconds` | number | `900` (15 min) | not set | How long an undecided approval stays pending before it expires. Local and organization mode. |
| `web.approvals.ledger_ttl_seconds` | number | `300` (5 min) | not set | How long a decision stays available for the AI system to repeat its call and collect it. A decided write is released once. Local and organization mode. |
| `web.approvals.max_pending` | int | `50` | not set | Most approvals pending at once, across everyone. Local and organization mode. |
| `web.approvals.max_pending_per_principal` | int | `20` | not set | Most approvals pending at once for one person. Local and organization mode. |
| `web.approvals.adaptive_hold` | bool | `true` | not set | Once one of your approvals is pending, return `approval_pending` for later gated calls straight away instead of waiting `hold_window_seconds` each. Local and organization mode. |

### Local file transfers

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `file_bridge.max_download_bytes` | int | `200000000` (200 MB) | not set (section present, key commented out) | Largest file a download tool delivers in local mode; it also caps the files the export tools (`grist_export_records`, `grist_export_sql`, `salesforce_export_report`, `jira_export_issues`) write. The file is held in memory before it is handed over, so raise it only with memory to spare. See [How PrivacyFence works](how-it-works.md#files). |

### Passkey step-up (local mode)

Step-up asks for a fresh passkey (Face ID, Touch ID, Windows Hello) before an approving decision is
released, and before a sensitive settings change (rules, grants, privacy policy, PII settings).
Denying never needs one. Passkeys are enrolled at `/security`, linked from
**Settings > General > Security > Manage passkeys**. An organization server reads the bundle's
`step_up` section instead (below). Details: [Approvals and policy](approvals-and-policy.md) and
[Security and compliance](security-and-compliance.md).

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `step_up.enabled` | bool | `true` on a packaged install, `false` otherwise | not set (commented out) | Require a passkey at decide time for the approvals `scope` covers. With nothing enrolled and `require_passkey` off, decisions go through without one (in organization mode, a single decision offers an identity-provider sign-in instead, and an approving batch is refused). |
| `step_up.scope` | string | `writes_and_pii_reads` | not set (commented out) | `writes` (write approvals), `writes_and_pii_reads` (also reads the PII check flagged), or `writes_and_reads` (every gated read too). The minimum: **Settings > General > Security > Passkey required for** can widen it for your own approvals, never narrow it (ADR 0091). An auto-accepted read never asks. Needs a restart. |
| `step_up.require_passkey` | bool | `true` on a packaged install, `false` otherwise | not set (commented out) | Make the passkey mandatory: with nothing enrolled, approving decisions that need step-up (see `step_up.scope`) and sensitive settings changes are refused (a banner points to `/security`) rather than released. The daemon still starts. Setting it to `true` on an install that is not privilege-separated stops the daemon at start. |
| `step_up.rp_id` | string | `localhost` | `localhost` | WebAuthn relying-party id. Leave as is. |
| `step_up.rp_name` | string | `PrivacyFence` | `PrivacyFence` | Name shown in your system's passkey prompt. |
| `step_up.batch` | string | `single_assertion` | `single_assertion` | `single_assertion`: one passkey prompt releases a whole selected batch on Approvals, bound to exactly that selection. `per_item`: a batch containing anything that needs step-up is refused as a whole, and each item is decided from its own card. |

"Packaged install" means the macOS `.pkg`, the Windows installer or the `.deb`, which are
privilege-separated. A source checkout or a `pip`/`pipx` install defaults both keys off. A value
written in the file always wins over these defaults, in either direction.

**The Turn on button.** Once at least one passkey is enrolled, **Settings > General > Security**
shows **Step-up for approvals** with a **Turn on** button. It writes `step_up.enabled: true` and
`step_up.require_passkey: true` together and takes effect immediately, with no restart. There is
no control to turn step-up off: set `step_up.require_passkey` (and `step_up.enabled`) to `false` in
the file and restart. Changing `scope` or `batch` also needs a file edit and a restart. The button
is not shown on an organization server.

**Passkey required for.** With step-up enabled, the same card shows the three scopes with the
configured `step_up.scope` as the minimum; the ones below it are locked. Picking a wider one applies
to your own approvals at once, with no restart, and is stored beside your passkeys
(`step_up_preference.json` under the authority directory), not in this file. In organization mode
every member sees this control, and the org bundle's `step_up.scope` is the minimum. Once a passkey
is required for settings changes, changing this needs one too. See ADR 0091.

### AI system names

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `agent_overrides.<client name>` | string | none | not set (commented out) | Label an MCP client whose handshake name PrivacyFence does not recognise as a known AI system. The value is an AI system id such as `claude-code` or `claude` (the ids the **Audit Log** shows); an unknown id is ignored with a warning. It relabels the recorded name only: the entry is still recorded as claimed (`client_info`), and the card still shows *Undetected*, as every local-mode card does. Local mode only; needs a restart. See [How PrivacyFence works](how-it-works.md#which-ai-system-is-asking). |

## Organization config bundle (org_config.json)

Build the bundle with `scripts/build_org_bundle.py` (Python standard library only; `--sign-key`
and `--generate-signing-key` also need the `cryptography` package). Pass only the options for the
services you have registered: a connector is offered only if its section is in the bundle. Install
it from **Settings > General > Organization Configuration**. Per-service registration steps are in
[Google Cloud setup](google-cloud-setup.md), [Slack setup](slack-setup.md),
[Salesforce setup](salesforce-setup.md), [Atlassian setup](atlassian-setup.md) and
[Grist setup](grist-setup.md), [Miro setup](miro-setup.md) and [Peek setup](peek-setup.md); Telegram needs no
bundle entry ([Telegram setup](telegram-setup.md)). The server side of an organization deployment is
in [Organization deployment](org-mode-setup-guide.md).

How the daemon treats the file:

- **Absent**: local mode, and every connector that needs organization credentials is unavailable.
- **Unreadable, not valid JSON, or not a JSON object**: the daemon refuses to start.
- **Signed**: the first signed bundle an install reads pins its signing key; from then on every
  bundle, unsigned ones included, must verify against that key or the daemon refuses to start. To
  accept a new key, an administrator deletes the pinned `org_config_signing_pubkey.txt` in the
  organization config directory.
- **`"mode": "org"`**: the bundle must be signed.

### Build options

| Option | Default | Writes | Notes |
|---|---|---|---|
| `--org-name NAME` | none | `org_name` | Shown to people after they install the bundle. |
| `-o`, `--output PATH` | `org_config.json` | | Written with mode `0600`. |
| `--merge` | off | | Update the bundle already at `--output` instead of starting from empty. Any existing signature is dropped; pass `--sign-key` again. |
| `--google-client-secret PATH` | none | `google` | The OAuth client JSON downloaded from Google Cloud: a *Desktop app* client for local mode, a *Web application* client for organization mode. The inner block is stored as-is. Covers Gmail, Drive, Calendar, Contacts, Tasks and Apps Script. |
| `--slack-client-id`, `--slack-client-secret` | none | `slack.client_id`, `slack.client_secret` | Give both or neither. |
| `--slack-scopes SCOPE …` | built-in list | `slack.user_scopes` | Override the Slack user-token scopes. Usually left unset. |
| `--salesforce-consumer-key`, `--salesforce-consumer-secret` | none | `salesforce.consumer_key`, `salesforce.consumer_secret` | Give both or neither. |
| `--salesforce-login-url URL` | `https://login.salesforce.com` | `salesforce.login_url` | `https://test.salesforce.com` for a sandbox. |
| `--atlassian-client-id`, `--atlassian-client-secret` | none | `atlassian.client_id`, `atlassian.client_secret` | Jira and Confluence. Give both or neither. |
| `--atlassian-assets` | off | `atlassian.assets` | Request the Jira Assets scopes at Atlassian sign-in; needs `--atlassian-client-id`/`--atlassian-client-secret`. |
| `--atlassian-assets-workspace-id ID` | none | `atlassian.assets_workspace_id` | The site's Assets workspace id, the `workspaceId` at `https://<site>.atlassian.net/rest/servicedeskapi/assets/workspace`; needs `--atlassian-client-id`/`--atlassian-client-secret`. |
| `--peek-base-url URL` | none | `peek.base_url` | The Peek server people connect to. Pins it in local mode; organization mode offers Peek only with it. |
| `--enable-telegram` | off | `telegram.enabled` | Offer Telegram. Its app credentials ship in the build, so the section is only a switch. Without it Telegram is neither listed nor usable ([ADR 0196](adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)). |
| `--enable-peek` | off | `peek.enabled` | Offer Peek without pinning a server (local mode). `--peek-base-url` implies it. |
| `--enable-grist` | off | `grist.enabled` | Offer Grist with an API key and no pinned server (local mode). `--grist-server-url` implies it. |
| `--grist-server-url URL` | none | `grist.server_url` | The Grist server people connect to. Needed for every Grist section; org mode offers Grist only with it. |
| `--grist-client-id`, `--grist-client-secret` | none | `grist.client_id`, `grist.client_secret` | A Grist OAuth app; with it people sign in with OAuth, without it they paste an API key. Give both or neither. |
| `--grist-auth-server-url URL` | the server URL | `grist.auth_server_url` | Where Grist's sign-in discovery document is served, when the server itself does not serve it. |
| `--grist-auto-provision` / `--no-grist-auto-provision` | not written (off) | `grist.auto_provision.enabled` | Give each person a Grist API key at sign-in through the Grist provisioner and connect it for them (organization mode, API keys only; needs `--grist-server-url`, `--grist-provisioner-url` and the provisioning key in the daemon's environment). |
| `--grist-provisioner-url URL` | none | `grist.auto_provision.provisioner_url` | The Grist provisioner service PrivacyFence calls (https://; http:// only for localhost). |
| `--grist-provisioning-key-env NAME` | `PRIVACYFENCE_GRIST_PROVISIONING_KEY` | `grist.auto_provision.key_env` | The environment variable the daemon reads the Grist provisioning key from (default `PRIVACYFENCE_GRIST_PROVISIONING_KEY`). |
| `--grist-trust-unverified-email` | off | `grist.auto_provision.trust_unverified_email` | Provision people whose ID token does not say `email_verified` (only for identity providers that issue only verified, organization-owned addresses). Give it again on every `--grist-auto-provision` rebuild; leaving it out turns it off. |
| `--miro-client-id`, `--miro-client-secret` | none | `miro.client_id`, `miro.client_secret` | The Miro app people sign in with. Give both or neither. |
| `--enable-unattended-sessions` / `--disable-unattended-sessions` | not written (off) | `unattended_sessions.enabled` | Allow `privacyfence_begin_unattended_session`. See [How PrivacyFence works](how-it-works.md#unattended-sessions). Applies in both modes. |
| `--enable-scoped-tokens` / `--disable-scoped-tokens` | not written (off) | `scoped_tokens.enabled` | Let each person create scoped AI client tokens in Settings (organization mode; local mode always allows them). Off unless set. Each token is named, expires, has its own rules and a chosen list of tools and is deny-only; see [Scoped tokens](security-and-compliance.md#scoped-tokens). |
| `--scoped-token-max-days N` | `90` | `scoped_tokens.max_days` | The longest a scoped token may last, 1-365 days (a value outside the range falls back to 90). Settings offers `min(90, N)` days by default. Revoke a person's tokens with `privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID`. |
| `--mode {local,org}` | not written (local) | `mode` | `org` needs `--server-issuer-url`, `--idp-issuer`, `--idp-client-id`, `--idp-client-secret` and `--sign-key`. `local` removes every organization-only section from a merged bundle. |
| `--server-issuer-url URL` | none | `server.issuer_url` | The server's public origin, e.g. `https://pf.example.com`. Must be an absolute `http(s)` URL with a host name. Register `<issuer-url>/oauth/idp/callback` and `<issuer-url>/oauth/idp/login-callback` with your identity provider. |
| `--server-bind-host HOST` | `127.0.0.1` | `server.bind_host` | Loopback only, for a reverse proxy on the same host. Change it only when the proxy runs elsewhere. |
| `--server-port PORT` | `8765` | `server.port` | |
| `--server-tls-cert PATH`, `--server-tls-key PATH` | none | `server.tls.cert_file`, `server.tls.key_file` | Terminate TLS in PrivacyFence itself. Give both or neither; leave both unset when the proxy terminates TLS. |
| `--server-trusted-proxy IP` | none | `server.trusted_proxies` | Repeatable. `X-Forwarded-For`/`X-Forwarded-Proto` are honoured only from these addresses. |
| `--idp-issuer URL` | none | `idp.issuer` | OIDC issuer; `<issuer>/.well-known/openid-configuration` must be reachable. |
| `--idp-client-id ID`, `--idp-client-secret SECRET` | none | `idp.client_id`, `idp.client_secret` | PrivacyFence's client registration at the identity provider. |
| `--idp-admin-group-claim CLAIM` | none (nobody is admin) | `idp.admin_group_claim` | ID-token claim that marks administrators, for example `email`, `groups` or `roles`. It must be in the ID token; which one works depends on the identity provider, see [Who is an admin](org-mode-setup-guide.md#who-is-an-admin). |
| `--idp-admin-group-value VALUE` | none | `idp.admin_group_values` | Repeatable. A value of that claim that makes someone an administrator. Matched exactly and case-sensitively. |
| `--idp-step-up-acr-value ACR` | none | `idp.step_up_acr_values` | Repeatable. `acr_values` to request when step-up falls back to signing in again at the identity provider; without it, plain re-authentication. |
| `--authz-allowed-domain DOMAIN` | none (any domain) | `authz.allowed_domains` | Repeatable. Admit only people whose email is at one of these domains. |
| `--authz-groups-claim CLAIM` | none | `authz.groups_claim` | ID-token claim checked against `--authz-required-group`. |
| `--authz-required-group VALUE` | none (no group needed) | `authz.required_groups` | Repeatable. Admit only people whose groups claim contains one of these. Needs `--authz-groups-claim`. |
| `--step-up-enabled` / `--step-up-disabled` | not written (off) | `step_up.enabled` | Require a passkey, or a fresh sign-in at the identity provider, before an approving decision is released. |
| `--step-up-scope {writes,writes_and_pii_reads,writes_and_reads}` | not written (`writes_and_pii_reads`) | `step_up.scope` | Same meaning as in `settings.yaml`. |
| `--step-up-rp-id DOMAIN` | not written (the issuer URL's host name) | `step_up.rp_id` | WebAuthn relying-party id. |
| `--step-up-rp-name NAME` | not written (`PrivacyFence`) | `step_up.rp_name` | Name in the passkey prompt. |
| `--step-up-require-passkey` / `--step-up-no-require-passkey` | not written (off) | `step_up.require_passkey` | Refuse, rather than fall back to an identity-provider sign-in, when a person has no passkey enrolled. |
| `--downloads-inline-max-bytes BYTES` | not written (`100000`) | `download_delivery.inline_max_bytes` | A download comes back inside the tool result when the whole result, the base64-encoded file included, is at most this many bytes (a file of about 75 KB at the default); otherwise it gets a one-time link. The default fits claude.ai's and Claude Desktop's tool-result limit ([ADR 0092](adr/0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)); raise it only if every client your organization uses accepts larger results. `0` sends every download through a one-time link. |
| `--downloads-link-ttl-seconds SECONDS` | not written (`300`) | `download_delivery.link_ttl_seconds` | How long a one-time download link can be claimed. Must be above 0. |
| `--downloads-disable-staging` | not written (staging on) | `download_delivery.allow_disk_staging: false` | Refuse a download too large to return inline instead of staging it (encrypted) for a link. |
| `--exports` / `--no-exports` | not written (on) | `download_delivery.allow_exports` | Off: the export tools (`grist_export_records`, `grist_export_sql`, `salesforce_export_report`, `jira_export_issues`, `assets_export_objects`) are left out of the tool list and refused, so no export file is staged on the server ([ADR 0197](adr/0197-an-organization-can-turn-the-export-tools-off.md)). |
| `--agent-links` / `--no-agent-links` | not written (on) | `download_delivery.agent_links` | On: a staged download is a link the AI system can fetch itself (`/mcp-files/fetch/…`), if it can reach this host: claude.ai needs it on its domain allowlist, which a claude.ai Owner or admin sets under **Admin settings → Capabilities** ([Connect claude.ai](connect-claude-ai.md#files), [ADR 0097](adr/0097-a-download-link-needs-a-client-that-can-reach-the-server.md)). Off: only a signed-in browser can fetch it (`/downloads/…`). |
| `--web-push` / `--no-web-push` | not written (on) | `web_push.enabled` | On: a person who allows notifications gets a count-only push ("N approvals pending") through their browser's push service when an approval is waiting. Off: nothing is pushed and no page offers it. See [ADR 0081](adr/0081-org-mode-sends-a-count-only-web-push.md). |
| `--enable-audit-forwarding` / `--disable-audit-forwarding` | not written (off) | `audit_forwarding.enabled` | Forward every audit entry to a syslog server or an HTTPS endpoint as well. The local audit log stays the authoritative record. |
| `--audit-forwarding-kind {syslog,http}` | not written (`syslog`) | `audit_forwarding.kind` | |
| `--audit-forwarding-syslog-host HOST` | none | `audit_forwarding.syslog.host` | Required when forwarding to syslog. |
| `--audit-forwarding-syslog-port PORT` | not written (`6514`) | `audit_forwarding.syslog.port` | |
| `--audit-forwarding-syslog-protocol {udp,tcp}` | not written (`tcp`) | `audit_forwarding.syslog.protocol` | |
| `--audit-forwarding-http-url URL` | none | `audit_forwarding.http.url` | Required when forwarding over HTTP. Must start with `https://`. |
| `--audit-forwarding-http-bearer-token-env VAR` | none | `audit_forwarding.http.bearer_token_env` | Name of an environment variable the daemon reads a bearer token from when sending. The token itself is never in the bundle. |
| `--generate-signing-key PATH` | | | Write a new Ed25519 private key to `PATH` (mode `0600`), print its public key, and exit without building a bundle. Keep the key; losing it means installs that pinned it cannot accept a new bundle until the pin is deleted by hand. |
| `--sign-key PATH` | none | `signing_public_key`, `signature` | Sign the bundle. Required with `--mode org`; recommended otherwise. |
| `--serial N` | current Unix time | `serial` | Serial signed into the bundle; needs `--sign-key`. Installs refuse a bundle whose serial is lower than the highest they have accepted. |

The organization-only options (`--server-*`, `--idp-*`, `--authz-*`, `--step-up-*`,
`--downloads-*`, `--agent-links`, `--web-push`, `--audit-forwarding-*`) are refused unless the bundle is in
organization mode (`--mode org`, or `--merge` into a bundle that already has it).

### Bundle keys not written by build_org_bundle.py

| Key | Written by | What it does |
|---|---|---|
| `version` | always `1` | Bundle format version. |
| `generated_at` | build time, UTC | When the bundle was built. |
| `rooms`, `rooms_synced_at` | `scripts/sync_room_directory.py` | The meeting-room list `calendar_list_rooms` returns, and when it was synced. Without it, `calendar_list_rooms` returns an empty list. |
| `step_up.batch` | hand edit | `single_assertion` (default) or `per_item`, as in `settings.yaml`. |

A bundle that still carries `mcp.tool_annotations` (only 5.0.0a1's `build_org_bundle.py` wrote it)
stops the daemon from starting, with a message naming the key: PrivacyFence always tells AI clients
truthfully which tools read and which write, and the option to say otherwise was removed. Rebuild
the bundle; `--merge` drops the key. See
[ADR 0089](adr/0089-tool-annotations-are-always-truthful.md).

The Google, Slack, Salesforce, Atlassian, Grist and Miro redirect URIs to register for an organization
deployment are listed in [Organization deployment](org-mode-setup-guide.md).

## Command-line options

`privacyfence-app` is the daemon's executable. On a packaged install the service starts it; the
options below are for running it by hand.

| Option | What it does |
|---|---|
| `--config PATH` | Read `settings.yaml` from `PATH` instead of its default location. |
| `--print-sign-in-link` | Print a one-time sign-in link to Approvals and exit. See [How PrivacyFence works](how-it-works.md#opening-privacyfence-opens-approvals). Local mode only. |
| `--print-mcp-token` | Print this OS account's MCP token (creating it the first time) and exit. See [Connect Claude Code](connect-claude-code.md#local-mode). Local mode only. |
| `--gmail-oauth`, `--drive-oauth`, `--calendar-oauth`, `--contacts-oauth`, `--tasks-oauth`, `--apps-script-oauth` | Sign in to that Google service in the browser from the command line, save the token, and exit. |
| `--slack-oauth`, `--salesforce-oauth`, `--atlassian-oauth` | The same for Slack, Salesforce, and Jira plus Confluence. |
| `--grist-oauth` | The same for Grist, when the organization config carries a Grist OAuth app. With an API key, use **Settings > Connectors** instead. |
| `--miro-oauth` | The same for Miro, which needs the Miro app in the organization config. |
| `--telegram-setup` | Sign in to Telegram interactively (phone number and code) and exit. |
| `--peek-login` | Sign in to Peek with Peek's device sign-in and save the token, then exit. |
| `--revoke-scoped-tokens PRINCIPAL_ID` | Revoke every scoped token that account holds (`local` in local mode, or the principal id the audit log and the org Settings page show) and exit. Run it as the account the daemon runs as. |

The sign-in options are for an install that is not privilege-separated (a source checkout or a
`pip`/`pipx` install). On a packaged install the daemon refuses to run as your account, so they
fail; connect services from **Settings > Connectors** instead (see
[Connecting a service](connecting-a-service.md)). They need the organization config bundle
installed first, except `--telegram-setup` and `--peek-login` (the latter asks for the Peek server address when the bundle names none).

### privacyfence-companion

The companion app's own command. The packaged installs start it for you. `--action`, `--serve` and
`--launch` are mutually exclusive.

| Option | What it does |
|---|---|
| (no option) | Run the tray (Windows) or menu-bar (macOS) companion. Linux has no tray, so there `--action` or `--serve` is required. |
| `--launch` | Open Approvals, starting the tray or menu-bar companion first if none is running. This is what clicking the app icon or the Start Menu entry runs ([ADR 0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md)). |
| `--serve` | Run only the companion's control channel, with no tray or menu, and stay up, so a separated daemon can hand this session a sign-in link to open. A separated Linux install's autostart entry runs this. |
| `--action open-approvals` / `open-settings` | Open Approvals / Settings and exit. |
| `--action copy-mcp-token` | Copy this account's AI client token to the clipboard ([ADR 0104](adr/0104-the-companion-copies-the-ai-client-token.md)). |
| `--action recovery-code` | Ask the daemon for a new one-time passkey recovery code and show it in a dialog. |
| `--action service-status`, `service-start`, `service-restart`, `service-stop` | Report on, start, restart or stop the background service; the last three ask for an administrator's password. |
| `--action quit` | Ask the daemon to shut down, as Settings' Quit button does (only when the install allows quitting). |
