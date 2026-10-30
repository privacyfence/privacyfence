# Configuration reference

PrivacyFence reads these configuration files and variables:

- **`settings.yaml`**: your policy and the daemon's own settings. It carries no secrets. Most of it
  is edited from PrivacyFence **Settings**; this page lists every key, including the ones with no
  control there.
- **`org_bundle.json`**: on a desktop install, the signed bundle of app credentials for the
  services it connects ([Desktop configuration bundle](#desktop-configuration-bundle-org_bundlejson)).
  It is `org_config.json` in 6.x.
- **On an organization server**, instead of a bundle: environment variables, `org/admin_config.json`
  (written by the Admin page) and signed connector configs
  ([Organization server settings](#organization-server-settings)).

Where the files live on each platform is in [Platform support](platform-support.md). They are
owned by PrivacyFence's service account on a packaged install, so editing one by hand needs
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
| `connectors.assets.enabled` | bool | `false` | not set | `true` turns Jira Assets on and makes the Atlassian sign-in ask for the Assets scopes; an explicit value wins over the Jira Assets setting on Admin → Features (`services.atlassian.assets` in `org/admin_config.json`; before 7.0 the bundle's `atlassian.assets` flag), which is the default when this key is absent (see [Atlassian setup](atlassian-setup.md#jira-assets)). Sign in again after switching it on. |
| `connectors.assets.workspace_id` | string | none | not set | The site's Assets workspace id (a UUID); wins over the workspace id on Admin → Features (before 7.0 the bundle's `atlassian.assets_workspace_id`). Jira Assets needs one of the two, since PrivacyFence cannot look the id up itself (see [Atlassian setup](atlassian-setup.md#jira-assets)). |

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
| `web.port` | int | `8765` | `8765` | Port of the one local server that carries Approvals, Settings and `/mcp`. It listens on `localhost` only. An organization server takes its port from `PRIVACYFENCE_PORT` ([Environment variables](#environment-variables)). |
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
**Settings > General > Security > Manage passkeys**. An organization server reads the `step_up` section of `org/admin_config.json` instead (set on Admin → Step-up, [below](#admin-config-orgadmin_configjson)). Details: [Approvals and policy](approvals-and-policy.md) and
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
every member sees this control, and the admin config's `step_up.scope` is the minimum. Once a passkey
is required for settings changes, changing this needs one too. See ADR 0091.

### AI system names

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `agent_overrides.<client name>` | string | none | not set (commented out) | Label an MCP client whose handshake name PrivacyFence does not recognise as a known AI system. The value is an AI system id such as `claude-code` or `claude` (the ids the **Audit Log** shows); an unknown id is ignored with a warning. It relabels the recorded name only: the entry is still recorded as claimed (`client_info`), and the card still shows *Undetected*, as every local-mode card does. Local mode only; needs a restart. See [How PrivacyFence works](how-it-works.md#which-ai-system-is-asking). |

## Desktop configuration bundle (org_bundle.json)

A desktop install reads one signed file with the app credentials for the services it connects. An
administrator builds it with `scripts/build_org_bundle.py` (Python standard library only;
`--sign-key` and `--generate-signing-key` also need the `cryptography` package), and people install
it from **Admin → Organization configuration** (or it is placed in the `org/` directory by hand).
An organization server does not take a bundle: see
[Organization server settings](#organization-server-settings). Pass only the options for the
services you have registered: a connector is offered only if its section is in the bundle.
Per-service registration steps are in [Google Cloud setup](google-cloud-setup.md),
[Slack setup](slack-setup.md), [Salesforce setup](salesforce-setup.md),
[Atlassian setup](atlassian-setup.md), [Grist setup](grist-setup.md) and
[Miro setup](miro-setup.md); Telegram needs no registration ([Telegram setup](telegram-setup.md)).

How the daemon treats the file:

- **Absent**: a desktop install has no organization credentials, and every connector that needs them
  is unavailable.
- **Unreadable, not valid JSON, or not a JSON object**: the daemon refuses to start.
- **Signed**: the first signed bundle an install reads pins its signing key; from then on every
  bundle, unsigned ones included, must verify against that key or the daemon refuses to start. To
  accept a new key, an administrator deletes the pinned `org_bundle_signing_pubkey.txt` in the
  organization config directory. Rotation after an upgrade is covered under
  [Files in the org directory](#files-in-the-org-directory).
- **File name**: PrivacyFence 7 reads `org_bundle.json`. `org_config.json` is the 6.x name: on the
  first start after an upgrade the daemon copies it (and its two pin files) to the new names and
  leaves the originals untouched, and a later change to `org_config.json` is ignored with a warning.
- **Operational settings in an older bundle** (services offered, file delivery, step-up and the
  others listed under [Refused options](#refused-options)) are imported once into
  `org/admin_config.json` and afterwards ignored; every later start logs a warning naming them, and
  `build_org_bundle.py --merge` drops them.

### Build options

| Option | Default | Writes | Notes |
|---|---|---|---|
| `--org-name NAME` | none | `org_name` | Shown to people after they install the bundle. |
| `-o`, `--output PATH` | `org_bundle.json` | | Written with mode `0600`. |
| `--merge` | off | | Update the bundle already at `--output` instead of starting from empty. Any existing signature is dropped; pass `--sign-key` again. Refuses an organization bundle, and drops operational keys an older bundle still carries. |
| `--mode {local,org}` | `local` | | `local` is the only mode the script builds; leaving it out is the same. `org` is refused. |
| `--google-client-secret PATH` | none | `google` | The OAuth client JSON downloaded from Google Cloud: a *Desktop app* client. The inner block is stored as-is. Covers Gmail, Drive, Calendar, Contacts, Tasks and Apps Script. |
| `--slack-client-id`, `--slack-client-secret` | none | `slack.client_id`, `slack.client_secret` | Give both or neither. |
| `--slack-scopes SCOPE …` | built-in list | `slack.user_scopes` | Override the Slack user-token scopes. Usually left unset. |
| `--salesforce-consumer-key`, `--salesforce-consumer-secret` | none | `salesforce.consumer_key`, `salesforce.consumer_secret` | Give both or neither. |
| `--salesforce-login-url URL` | `https://login.salesforce.com` | `salesforce.login_url` | `https://test.salesforce.com` for a sandbox. |
| `--atlassian-client-id`, `--atlassian-client-secret` | none | `atlassian.client_id`, `atlassian.client_secret` | Jira and Confluence. Give both or neither. |
| `--grist-server-url URL` | none | `grist.server_url` | The Grist server the OAuth app belongs to; needed with `--grist-client-id` and `--grist-client-secret`. |
| `--grist-client-id`, `--grist-client-secret` | none | `grist.client_id`, `grist.client_secret` | A Grist OAuth app; with it people sign in with OAuth, without it they paste an API key. Give both or neither. |
| `--grist-auth-server-url URL` | the server URL | `grist.auth_server_url` | Where Grist's sign-in discovery document is served, when the server itself does not serve it. |
| `--miro-client-id`, `--miro-client-secret` | none | `miro.client_id`, `miro.client_secret` | The Miro app people sign in with. Give both or neither. |
| `--generate-signing-key PATH` | | | Write a new Ed25519 private key to `PATH` (mode `0600`), print its public key, and exit without building a bundle. Keep the key; losing it means installs that pinned it cannot accept a new bundle until the pin is deleted by hand. |
| `--sign-key PATH` | none | `signing_public_key`, `signature` | Sign the bundle. Recommended: an install that has pinned a key refuses an unsigned bundle. |
| `--serial N` | current Unix time | `serial` | Serial signed into the bundle; needs `--sign-key`. Installs refuse a bundle whose serial is lower than the highest they have accepted. |

### Refused options

Every option that wrote an operational setting, and every option that described an organization
server, is hidden from `--help` and refused with a message that says where the setting went; nothing
is written. They are listed here because older runbooks and scripts still pass them.

| Refused options | Where the setting is now |
|---|---|
| `--mode org`, `--server-issuer-url`, `--server-bind-host`, `--server-port`, `--server-tls-cert`, `--server-tls-key`, `--server-trusted-proxy` | Environment variables on the server: `PRIVACYFENCE_MODE`, `PRIVACYFENCE_ISSUER_URL`, `PRIVACYFENCE_BIND_HOST`, `PRIVACYFENCE_PORT`, `PRIVACYFENCE_TRUSTED_PROXIES` ([below](#environment-variables)). TLS in the daemon has no environment variable: terminate TLS in the reverse proxy. |
| `--idp-issuer`, `--idp-client-id`, `--idp-client-secret`, `--idp-admin-group-claim`, `--idp-admin-group-value`, `--idp-step-up-acr-value` | `PRIVACYFENCE_IDP_ISSUER`, `PRIVACYFENCE_IDP_CLIENT_ID`, `PRIVACYFENCE_IDP_CLIENT_SECRET` or `_FILE`, `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM`, `PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES`, `PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES`; administrators can also be named by email with `PRIVACYFENCE_ADMIN_EMAILS`. |
| `--enable-telegram`, `--enable-peek`, `--enable-grist`, `--peek-base-url` | Admin → Services (offer or stop offering a service) and, for Peek and Grist, the server address in Admin → Features or `PRIVACYFENCE_PEEK_BASE_URL` / `PRIVACYFENCE_GRIST_SERVER_URL`. |
| `--atlassian-assets`, `--atlassian-assets-workspace-id` | Admin → Features, Jira Assets (`services.atlassian.assets`, `assets_workspace_id`). |
| `--authz-allowed-domain`, `--authz-groups-claim`, `--authz-required-group` | Admin → Sign-in (`authz`). |
| `--step-up-enabled`, `--step-up-disabled`, `--step-up-scope`, `--step-up-require-passkey`, `--step-up-no-require-passkey` | Admin → Step-up (`step_up`). Only stricter choices are offered there. |
| `--step-up-rp-id`, `--step-up-rp-name` | `step_up.rp_id` and `step_up.rp_name` in `org/admin_config.json`, edited by hand, then restart. There is no Admin control for them, nor for `step_up.batch`. |
| `--enable-unattended-sessions`, `--disable-unattended-sessions` | Admin → Features (`unattended_sessions.enabled`). |
| `--enable-scoped-tokens`, `--disable-scoped-tokens`, `--scoped-token-max-days`, `--enable-scoped-token-proposals`, `--disable-scoped-token-proposals` | Admin → Scoped tokens (`scoped_tokens.enabled`, `max_days`, `allow_ai_proposals`). |
| `--downloads-inline-max-bytes`, `--downloads-link-ttl-seconds`, `--downloads-disable-staging`, `--agent-links`, `--no-agent-links`, `--exports`, `--no-exports` | Admin → File delivery (`download_delivery`). |
| `--web-push`, `--no-web-push` | Admin → Features (`web_push.enabled`). |
| `--enable-audit-forwarding`, `--disable-audit-forwarding`, `--audit-forwarding-kind`, `--audit-forwarding-syslog-host`, `--audit-forwarding-syslog-port`, `--audit-forwarding-syslog-protocol`, `--audit-forwarding-http-url`, `--audit-forwarding-http-bearer-token-env` | Admin → Audit forwarding (`audit_forwarding`). The page shows the bearer-token variable's name read-only; set `audit_forwarding.http.bearer_token_env` in `org/admin_config.json` by hand and put the token itself in that environment variable. |
| `--peek-auto-provision`, `--no-peek-auto-provision`, `--peek-provisioning-key-env`, `--peek-trust-unverified-email`, `--grist-auto-provision`, `--no-grist-auto-provision`, `--grist-provisioner-url`, `--grist-provisioning-key-env`, `--grist-trust-unverified-email` | Admin → Services (`services.peek.auto_provision`, `services.grist.auto_provision`), plus `PRIVACYFENCE_GRIST_PROVISIONER_URL` and the provisioning key variables ([below](#environment-variables)). |

### Bundle keys not written by build_org_bundle.py

| Key | Written by | What it does |
|---|---|---|
| `version` | always `1` | Bundle format version. |
| `generated_at` | build time, UTC | When the bundle was built. |
| `rooms`, `rooms_synced_at` | older `scripts/sync_room_directory.py` | The meeting-room list. It is imported into `org/admin_config.json` on the first start; `sync_room_directory.py --output rooms.json` writes a file you upload on Admin → Features instead. |

A bundle that still carries `mcp.tool_annotations` (only 5.0.0a1's `build_org_bundle.py` wrote it)
stops the daemon from starting, with a message naming the key: PrivacyFence always tells AI clients
truthfully which tools read and which write, and the option to say otherwise was removed. Rebuild
the bundle; `--merge` drops the key. See
[ADR 0089](adr/0089-tool-annotations-are-always-truthful.md).

## Organization server settings

An organization server (Docker image or [the systemd install](org-mode-setup-guide.md)) reads no
bundle. It takes its settings from four places, and nothing else:

| What | Where it lives | Changed by |
|---|---|---|
| Mode, server address, identity provider | Environment variables ([below](#environment-variables)) | The operator, then restart |
| Operational settings (services, file delivery, step-up, scoped tokens, audit forwarding, sign-in restrictions, web push, unattended sessions, rooms) | `org/admin_config.json` ([below](#admin-config-orgadmin_configjson)) | Administrators, on the [Admin page](org-mode-setup-guide.md#the-admin-page) |
| An organization's own OAuth app for a provider | A signed connector config, `org/connector_configs/<provider>.txt` ([below](#connector-configs)) | Administrators, on Admin → Services |
| A provider with no connector config | The built-in PrivacyFence app through the OAuth relay at `oauth.privacyfence.eu` ([terms](https://privacyfence.eu/relay-terms/)), or none (Grist and Peek have no built-in app) | |

The reasoning is in [ADR 0256](adr/0256-org-config-holds-only-startup-settings-and-app-credentials.md),
[ADR 0257](adr/0257-operational-settings-live-in-a-daemon-written-admin-config.md),
[ADR 0264](adr/0264-an-organization-server-can-start-from-environment-variables.md) and
[ADR 0269](adr/0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md).

### Environment variables

An unset variable and one set to an empty value are the same. Several variables are comma-separated
lists; spaces around each item are ignored.

| Variable | Default | What it sets |
|---|---|---|
| `PRIVACYFENCE_MODE` | `local` (`org` in the Docker image) | `local` or `org`. Any other value refuses the start. A legacy bundle that sets `mode` to something else refuses the start too. |
| `PRIVACYFENCE_ISSUER_URL` | `http://localhost:<port>` in single-user mode, otherwise required | The public origin, with no path, such as `https://pf.acme.example.com`. Every redirect URI and link is built from it. |
| `PRIVACYFENCE_BIND_HOST` | `127.0.0.1` (`0.0.0.0` in the Docker image) | The address the daemon listens on. |
| `PRIVACYFENCE_PORT` | `8765` | The port the daemon listens on (1 to 65535). |
| `PRIVACYFENCE_TRUSTED_PROXIES` | none | The reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are trusted. `*`, `0.0.0.0/0` and `::/0` are refused. With the shipped Caddy this is `172.30.87.2`. |
| `PRIVACYFENCE_IDP_ISSUER`, `PRIVACYFENCE_IDP_CLIENT_ID` | none | The OIDC client of your identity provider. Set both or neither: setting only one refuses the start. Setting neither (and nothing else of the `PRIVACYFENCE_IDP_*` family or `PRIVACYFENCE_ADMIN_EMAILS`) is single-user mode. |
| `PRIVACYFENCE_IDP_CLIENT_SECRET`, `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE` | none | The client secret, or the path of a file that holds it (a Docker secret). Set one of the two, never both. The secret is never logged. |
| `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM`, `PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES` | none | The ID-token claim that makes a person an administrator and its values (comma-separated), matched exactly and case-sensitively. See [Who is an admin](org-mode-setup-guide.md#who-is-an-admin). |
| `PRIVACYFENCE_ADMIN_EMAILS` | none | Administrators by email. A person is an administrator when the identity provider reports the address as verified (`email_verified` is exactly `true`), and with Google only when the account's Workspace domain (`hd`) matches the address. Matching ignores case and surrounding spaces; an address with a non-ASCII character never matches. |
| `PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES` | none | `acr_values` sent when step-up asks the identity provider for a fresh sign-in. |
| `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL` | `false` | `true` or `false`. Allowed email domains (Admin → Sign-in) trust an address only when the identity provider reports it verified. Set `true` for a provider that never sends `email_verified`, such as Microsoft Entra ID. It never makes anyone an administrator. |
| `PRIVACYFENCE_IDP` | none | `builtin-google` signs people in with PrivacyFence's built-in Google app through `oauth.privacyfence.eu`. It needs `PRIVACYFENCE_ADMIN_EMAILS` and a public `https://` `PRIVACYFENCE_ISSUER_URL`, cannot be combined with any `PRIVACYFENCE_IDP_*` variable, and refuses everyone not named as an administrator or in an allowed domain. Any other value refuses the start. See [the built-in Google sign-in](org-mode-setup-guide.md#built-in-google-sign-in). |
| `PRIVACYFENCE_ALLOWED_DOMAINS` | none | With `PRIVACYFENCE_IDP=builtin-google`: the allowed sign-in domains, used to fill Admin → Sign-in on the first start only. Afterwards edit them on that page. |
| `PRIVACYFENCE_PREVIEW_SERVICES` | none | A comma-separated list of service names, or `*`. A service whose connect flow has shipped but whose tools have not (`oauth_apps.PREVIEW_SERVICES`, empty in a release unless one is in progress) is hidden on the Admin page and in the organization config unless it is named here. Used by the QA server, which sets `*`. |
| `PRIVACYFENCE_PEEK_BASE_URL` | none | The Peek server's address. When set it is fixed: the Admin page shows it and does not let anyone change it. |
| `PRIVACYFENCE_GRIST_SERVER_URL`, `PRIVACYFENCE_GRIST_PROVISIONER_URL` | none | The Grist server's address and the Grist provisioner service PrivacyFence calls for automatic accounts (`https://`; `http://` only for localhost). Fixed in the same way. |
| `PRIVACYFENCE_PEEK_PROVISIONING_KEY`, `PRIVACYFENCE_GRIST_PROVISIONING_KEY` | none | The provisioning keys for automatic Peek and Grist accounts. The names are the defaults of `services.peek.auto_provision.key_env` and `services.grist.auto_provision.key_env`; a different name there is read the same way. |
| `PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE`, `PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE` | none | The path of a file that holds the key (a Docker secret). For a custom key variable `NAME`, the file form is `NAME_FILE`. Setting both the plain variable and the file refuses the start, and so does a file that cannot be read. The key is never logged. |
| `PRIVACYFENCE_BUILTIN_APPS` | none | Development builds only: the path of a JSON file that stands in for the built-in apps a release carries. An object with the keys `google_web`, `slack`, `salesforce`, `atlassian_relay`, `miro` and `google_signin`, each `{"client_id": "…", "client_secret": "…"}`; `salesforce` may also carry `install_url`. A release build ignores the variable. |

**Precedence.** A legacy bundle's `idp` section wins over `PRIVACYFENCE_IDP_*` and
`PRIVACYFENCE_ADMIN_EMAILS`, and the start logs that the variables are ignored. The same holds for a
bundle's `mode` and `server` values. Without a bundle the environment is the only source. See
[Legacy bundle](#legacy-bundle-orgorg_bundlejson) for moving off it.

**A server stays a server.** The first start with an identity provider records `org_server` in
`org/admin_config.json`. After that, removing the identity provider variables is refused as a
single-user start instead of exposing the server on its address with a setup code.

### Admin config (org/admin_config.json)

Written by the daemon when an administrator saves a change on the
[Admin page](org-mode-setup-guide.md#the-admin-page); a desktop install keeps one too. It holds
everything operational and is part of the backup. Do not edit it while the daemon runs, and do not
rely on a hand edit surviving an Admin page save unless you restart afterwards. The only reason to
edit it by hand is to relax a setting the page only lets you tighten (step-up), or to set a key the
page has no control for. Stop the daemon, edit, start. A file that is not valid JSON, is not an
object, or has an unknown `version` refuses the start.

| Key | Holds | Admin page |
|---|---|---|
| `version` | `1` | |
| `org_server` | `true` once this data directory has run with an identity provider; never changed by the page | |
| `imported_from_bundle_sha256` | the hash of the 6.x bundle the settings were first copied from; empty on a new install | |
| `services.<name>.offered` | whether the service is offered; `<name>` is `google`, `slack`, `salesforce`, `atlassian`, `miro`, `telegram`, `grist` or `peek`. Applies at once. | Services |
| `services.salesforce.login_url` | the org's My Domain login URL; empty means `login.salesforce.com` | Services → Login URL |
| `services.atlassian.assets`, `assets_workspace_id` | Jira Assets | Features |
| `services.peek.base_url`, `services.grist.server_url`, `address_locked` | the Peek and Grist addresses; `address_locked` marks one carried over from a signed 6.x bundle that automatic accounts depend on | Features |
| `services.peek.auto_provision`, `services.grist.auto_provision` | `enabled`, `trust_unverified_email`, `key_env` (the environment variable the key is read from) and, for Grist, `provisioner_url` | Services |
| `rooms`, `rooms_synced_at` | the meeting-room list `calendar_list_rooms` returns | Features |
| `unattended_sessions.enabled` | allow `privacyfence_begin_unattended_session` | Features |
| `download_delivery.inline_max_bytes`, `link_ttl_seconds`, `allow_disk_staging`, `agent_links`, `allow_exports` | file delivery ([guide section 13](org-mode-setup-guide.md#13-file-delivery)) | File delivery |
| `web_push.enabled` | count-only push notifications ([ADR 0081](adr/0081-org-mode-sends-a-count-only-web-push.md)) | Features |
| `authz.allowed_domains`, `groups_claim`, `required_groups` | who may sign in | Sign-in |
| `step_up.enabled`, `scope`, `require_passkey` | step-up for approvals; the page only offers stricter values | Step-up |
| `step_up.rp_id`, `rp_name`, `batch` | the WebAuthn relying-party id and name, and `single_assertion` or `per_item` | none: edit the file, then restart |
| `scoped_tokens.enabled`, `max_days`, `allow_ai_proposals` | scoped tokens; `allow_ai_proposals` lets an AI client propose one ([ADR 0253](adr/0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md)) | Scoped tokens |
| `audit_forwarding.enabled`, `kind`, `syslog.host`, `syslog.port`, `syslog.protocol`, `http.url`, `http.bearer_token_env` | audit forwarding; the token itself is read from the named environment variable and is never stored | Audit forwarding |

Changes to services and the Salesforce login URL take effect at once. Every other section is saved
and applied at the next restart; the page lists what waits. Privacy Filter and PII Detection
changes on the page apply to the next request.

### Connector configs

An organization's own OAuth app for Google, Slack, Salesforce, Atlassian, Miro or Grist is a signed
text file, one per provider, stored unchanged at `org/connector_configs/<provider>.txt` (mode
`0600`) and verified against the pinned key at every start. A file that no longer verifies refuses
the start and names the provider.

```text
privacyfence-connector-config v1
provider: slack
serial: 2026110201
client_id: 1234.5678
client_secret: ...
signed_at: 2026-11-02T10:00:00Z
key: <base64 Ed25519 public key>
signature: <base64 Ed25519 signature over every line before "signature:">
```

- Fields come in exactly this order. Salesforce adds an optional `login_url` before `signed_at`
  (and calls the client ID and secret `consumer_key` and `consumer_secret` in its own section);
  Grist adds `auth_server_url` and `server_url` there.
- A value is printable ASCII, at most 512 characters; a file is at most 8 KB.
- Sign it with `scripts/sign_connector_config.py` (`keygen`, then `sign`) or the browser tool at
  <https://privacyfence.eu/tools/connector-config/>. Both produce the same bytes for the same input.
  The steps are in [the organization guide](org-mode-setup-guide.md#signed-connector-configs).
- The first upload pins its key in `org/connector_config_signing_pubkey.txt`, after the
  administrator confirms the key's fingerprint on the page. A different key is refused. To rotate
  the key, stop the daemon and delete that file; the next upload pins the new one.
- Each provider's serial must be higher than any accepted before for that provider. The highest
  accepted serials are kept in `org/connector_config_serials.json`, which survives removal, so an
  older file cannot be put back.
- An upgraded server's app credentials are imported once from its legacy bundle as
  `org/connector_configs/<provider>.json` (not re-signed: the bundle's signature covered them), and a
  later upload replaces them.

### Legacy bundle (org/org_bundle.json)

A server upgraded from 6.x has `org/org_bundle.json`, copied once from `org/org_config.json`. The
daemon reads it only for `mode`, `server` and `idp`, and every start logs that. Its app credentials
were imported as connector configs on the first start, and its operational settings into
`org/admin_config.json`; the Admin page shows a notice while the file is present. A server installs
no new bundle: an `org_bundle.json` that differs from the one adopted is refused at start.

To leave it, move the three settings to the environment and retire the file:

```bash
privacyfence-app --print-env-from-bundle --idp-secret-file ./idp_client_secret
# copy the printed PRIVACYFENCE_* lines to privacyfence.env, restart, then:
privacyfence-app --retire-bundle
```

`--print-env-from-bundle` never prints the identity provider's client secret: with
`--idp-secret-file PATH` it writes the secret to a new file (mode `0600`, never overwriting) and
prints `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE=PATH`. `--retire-bundle` checks that the environment
yields the same mode, server address and identity provider, imports the credentials if that has not
happened, records the retirement in `org/org_bundle_adopted.json` and deletes `org_bundle.json`.
Going back to 6.x still works afterwards, because 6.x reads the untouched `org_config.json`.

### Files in the org directory

`org/` is under the data directory (`~/.privacyfence/org/` of the account the daemon runs as). All
files are mode `0600`.

| File | Written by | What it is |
|---|---|---|
| `org_bundle.json` | the daemon, once (copied from `org_config.json`) | The legacy bundle ([above](#legacy-bundle-orgorg_bundlejson)); desktop installs also take a bundle here. |
| `org_bundle_signing_pubkey.txt`, `org_bundle_serial.txt` | the daemon | The pinned bundle signing key and the highest bundle serial accepted. Deleting the key file accepts a new key at the next start; keep the serial file unless you want to go back to an older bundle on purpose. After an upgrade, deleting the new-name pin is the rotation: the 6.x pin (`org_config_signing_pubkey.txt`) is not copied again. Delete the 6.x pins only when going back to 6.x is no longer wanted. |
| `org_config.json`, `org_config_signing_pubkey.txt`, `org_config_serial.txt` | PrivacyFence 6 | The 6.x names, never changed by 7.x. |
| `org_bundle_adopted.json` | the daemon | Records the copy from `org_config.json` (its hash and time), that the pins were adopted, and a retirement. |
| `admin_config.json` | the daemon | [Above](#admin-config-orgadmin_configjson). Back it up. |
| `connector_configs/`, `connector_config_signing_pubkey.txt`, `connector_config_serials.json` | the daemon | [Connector configs](#connector-configs). |
| `relay_token.txt` | the daemon, on first need | The 43-character token the server proves its host name with at `/.well-known/privacyfence-relay`, and, with the built-in Google sign-in, the value behind every `_privacyfence-relay` DNS TXT record (`pf-relay=<SHA-256 of the token>`). Back it up: replacing it means publishing every record again. |
| `relay_status.json` | the daemon | When the relay last accepted this server, and which sign-in domains it has verified. |
| `oauth_clients.json`, `oauth_refresh.json`, `web_push_vapid_key.pem` | the daemon | Registered AI clients, their refresh tokens and the push key ([backup](org-mode-setup-guide.md#backup)). |
| `../data_format.json` | the daemon | The marker of the data format, written once every person's data is up to date ([upgrade](org-mode-setup-guide.md#upgrading-to-8x)). |

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
| `--grist-oauth` | The same for Grist, when the bundle (or, on a server, a connector config) carries a Grist OAuth app. With an API key, use **Settings > Connectors** instead. |
| `--miro-oauth` | The same for Miro, which needs the Miro app in the bundle, a connector config or the built-in app. |
| `--telegram-setup` | Sign in to Telegram interactively (phone number and code) and exit. |
| `--peek-login` | Sign in to Peek with Peek's device sign-in and save the token, then exit. |
| `--revoke-scoped-tokens PRINCIPAL_ID` | Revoke every scoped token that account holds (`local` in local mode, or the principal id the audit log and the org Settings page show) and exit. Run it as the account the daemon runs as. |
| `--peek-release PRINCIPAL_ID` | Revoke the Peek token PrivacyFence minted for that account, stop renewing it, and exit. The Peek account stays. Needs the Peek provisioning key in the environment; run it as the account the daemon runs as. |
| `--relay-dns-records` | Print the DNS TXT records the built-in Google sign-in still needs, one line per sign-in domain the relay has not found yet, and exit. Organization mode ([guide](org-mode-setup-guide.md#built-in-google-sign-in)). |
| `--print-env-from-bundle` | Print the `PRIVACYFENCE_*` lines that say what a legacy `org/org_bundle.json` says about the mode, the server and the identity provider, and exit. The client secret is never printed. See [Legacy bundle](#legacy-bundle-orgorg_bundlejson). |
| `--idp-secret-file PATH` | With `--print-env-from-bundle`: write the identity provider's client secret to `PATH` (a new file, mode `0600`) and print the variable that names it. |
| `--retire-bundle` | Import the legacy bundle's app credentials as connector configs if that has not happened, and delete `org/org_bundle.json` once the environment says the same mode, server address and identity provider. |
| `--grist-release PRINCIPAL_ID` | Release the Grist key PrivacyFence holds for that account: the provisioner deletes it in Grist if it created it, and PrivacyFence forgets it. Then the daemon exits. Needs the Grist provisioning key in the environment; run it as the account the daemon runs as. |

The sign-in options are for an install that is not privilege-separated (a source checkout or a
`pip`/`pipx` install). On a packaged install the daemon refuses to run as your account, so they
fail; connect services from **Settings > Connectors** instead (see
[Connecting a service](connecting-a-service.md)). They need the app's credentials first (a desktop
bundle, or on an organization server a connector config or the built-in app), except `--telegram-setup` and
`--peek-login` (the latter asks for the Peek server address when none is configured).

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
