# Configuration reference

PrivacyFence reads these configuration files and variables:

- **`settings.yaml`**: your policy and the daemon's own settings. It carries no secrets. Most of it
  is edited from PrivacyFence **Settings**; this page lists every key, including the ones with no
  control there.
- **`org_bundle.json`**: the legacy signed bundle of an organization server upgraded from 6.x
  ([Legacy bundle](#legacy-bundle-orgorg_bundlejson)). It is `org_config.json` in 6.x.
- **On an organization server**, instead of a bundle: environment variables, `org/admin_config.json`
  (written by the Admin page) and signed connector configs
  ([Organization server settings](#organization-server-settings)).

In the Docker image the files live in the data volume
([What stays in the data volume](org-mode-docker.md#what-stays-in-the-data-volume)).

The command-line options of `privacyfence-app` are at the end of this page
([Command-line options](#command-line-options)).

## How settings.yaml is read

- **First start.** If `settings.yaml` does not exist, the daemon creates it from the shipped
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
  only the `auto_accept` section is used.

### Exporting and importing personal settings

**Settings > Auto-accept > Export…** writes `privacyfence-personal-settings.json` (UTF-8 JSON, at most
256 KiB and 500 rules to import); **Import…** reads one back. It works in both modes and in every
direction. Only the person's own `auto_accept` rules and step-up level move.

| Key | Meaning |
|---|---|
| `format` | Always `privacyfence-personal-settings` |
| `format_version` | `1`; a newer version is refused |
| `exported_at` | UTC ISO-8601 time of the export |
| `source` | `mode` (`org`; a file exported by PrivacyFence 7 on a desktop install says `local`) and the PrivacyFence `version` |
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
missing altogether is `block` for everything.

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

### Web server and MCP

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `web.port` | int | `8765` | `8765` | Port of the one server that carries Approvals, Settings and `/mcp`. An organization server takes its port from `PRIVACYFENCE_PORT` ([Environment variables](#environment-variables)). |
| `web.mcp.enabled` | bool | **`false`** | **`true`** | Serve `/mcp`. With it off, no AI system can reach PrivacyFence at all. Must be `true` on an organization server. |
| `web.settings.enabled` | bool | **`false`** | **`true`** | Serve Settings (`/settings`). With it off, the only way to change anything is to edit the files by hand. |
| `web.notifications.enabled` | bool | `true` | `true` | Browser notifications for a new pending approval while the Approvals tab is open but not focused. Nothing is sent through a push service. |
| `web.notifications.detail` | string | **`minimal`** | **`standard`** | What a notification about a single approval may say: `minimal` ("1 approval pending"), `standard` (adds connector, tool and direction), `detailed` (adds the card's title line, which can contain gated content, e.g. a recipient). A notification about several approvals always uses `minimal`. **Settings > General > Approval Notifications**. |
| `web.approvals.hold_window_seconds` | number | `30` | not set | How long a gated call waits for your decision before returning `approval_pending`. |
| `web.approvals.pending_ttl_seconds` | number | `900` (15 min) | not set | How long an undecided approval stays pending before it expires. |
| `web.approvals.ledger_ttl_seconds` | number | `300` (5 min) | not set | How long a decision stays available for the AI system to repeat its call and collect it. A decided write is released once. |
| `web.approvals.max_pending` | int | `50` | not set | Most approvals pending at once, across everyone. |
| `web.approvals.max_pending_per_principal` | int | `20` | not set | Most approvals pending at once for one person. |
| `web.approvals.adaptive_hold` | bool | `true` | not set | Once one of your approvals is pending, return `approval_pending` for later gated calls straight away instead of waiting `hold_window_seconds` each. |

### File transfers

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `file_bridge.max_download_bytes` | int | `200000000` (200 MB) | not set (section present, key commented out) | Largest file a download or an export delivers; it caps the files the export tools (`grist_export_records`, `grist_export_sql`, `salesforce_export_report`, `jira_export_issues`) produce. The file is held in memory before it is handed over, so raise it only with memory to spare. See [How PrivacyFence works](how-it-works.md#files). |

### Passkey step-up

Step-up asks for a fresh passkey (Face ID, Touch ID, Windows Hello, a security key) before an approving decision is
released, and before a sensitive settings change (rules, grants, privacy policy, PII settings).
Denying never needs one. Passkeys are enrolled at `/security`, linked from
**Settings > General > Security > Manage passkeys**. An organization server reads the `step_up` section of `org/admin_config.json` instead (set on Admin → Step-up, [below](#admin-config-orgadmin_configjson)). Details: [Approvals and policy](approvals-and-policy.md) and
[Security and compliance](security-and-compliance.md).

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `step_up.enabled` | bool | `false` (`true` on a single-user install) | not set (commented out) | Require a passkey at decide time for the approvals `scope` covers. With nothing enrolled and `require_passkey` off, decisions go through without one (in organization mode, a single decision offers an identity-provider sign-in instead, and an approving batch is refused). |
| `step_up.scope` | string | `writes_and_pii_reads` | not set (commented out) | `writes` (write approvals), `writes_and_pii_reads` (also reads the PII check flagged), or `writes_and_reads` (every gated read too). The minimum: **Settings > General > Security > Passkey required for** can widen it for your own approvals, never narrow it (ADR 0091). An auto-accepted read never asks. Needs a restart. |
| `step_up.require_passkey` | bool | `false` (`true` on a single-user install) | not set (commented out) | Make the passkey mandatory: with nothing enrolled, approving decisions that need step-up (see `step_up.scope`) and sensitive settings changes are refused (a banner points to `/security`) rather than released. The daemon still starts. |
| `step_up.rp_id` | string | `localhost` | `localhost` | WebAuthn relying-party id. Leave as is. |
| `step_up.rp_name` | string | `PrivacyFence` | `PrivacyFence` | Name shown in your system's passkey prompt. |
| `step_up.batch` | string | `single_assertion` | `single_assertion` | `single_assertion`: one passkey prompt releases a whole selected batch on Approvals, bound to exactly that selection. `per_item`: a batch containing anything that needs step-up is refused as a whole, and each item is decided from its own card. |

A value written in the file always wins over these defaults, in either direction.

**The Turn on button.** Once at least one passkey is enrolled, **Settings > General > Security**
shows **Step-up for approvals** with a **Turn on** button. It writes `step_up.enabled: true` and
`step_up.require_passkey: true` together and takes effect immediately, with no restart. There is
no control to turn step-up off: set `step_up.require_passkey` (and `step_up.enabled`) to `false` in
the file and restart. Changing `scope` or `batch` also needs a file edit and a restart.

**Passkey required for.** With step-up enabled, the same card shows the three scopes with the
configured `step_up.scope` as the minimum; the ones below it are locked. Picking a wider one applies
to your own approvals at once, with no restart, and is stored beside your passkeys
(`step_up_preference.json` under the authority directory), not in this file. Every member sees this control, and the admin config's `step_up.scope` is the minimum. Once a passkey
is required for settings changes, changing this needs one too. See ADR 0091.

### AI system names

| Key | Type | Code default | Seeded | What it does |
|---|---|---|---|---|
| `agent_overrides.<client name>` | string | none | not set (commented out) | Label an MCP client whose handshake name PrivacyFence does not recognise as a known AI system. The value is an AI system id such as `claude-code` or `claude` (the ids the **Audit Log** shows); an unknown id is ignored with a warning. It relabels the recorded name only: the entry is still recorded as claimed (`client_info`), and the card still shows the caller as claimed. Needs a restart. See [How PrivacyFence works](how-it-works.md#which-ai-system-is-asking). |

## Bundle file (org_bundle.json)

An organization server does not need a bundle: see
[Organization server settings](#organization-server-settings). A legacy `org/org_bundle.json` is
read, as described below and under [Legacy bundle](#legacy-bundle-orgorg_bundlejson).
Per-service registration steps are in [Google Cloud setup](google-cloud-setup.md),
[Slack setup](slack-setup.md), [Salesforce setup](salesforce-setup.md),
[Atlassian setup](atlassian-setup.md), [Grist setup](grist-setup.md) and
[Miro setup](miro-setup.md); Telegram needs no registration ([Telegram setup](telegram-setup.md)).

How the daemon treats the file:

- **Absent**: the built-in apps and connector configs supply the credentials.
- **Unreadable, not valid JSON, or not a JSON object**: the daemon refuses to start.
- **Signed**: the first signed bundle an install reads pins its signing key; from then on every
  bundle, unsigned ones included, must verify against that key or the daemon refuses to start. To
  accept a new key, an administrator deletes the pinned `org_bundle_signing_pubkey.txt` in the
  organization config directory. Rotation after an upgrade is covered under
  [Files in the org directory](#files-in-the-org-directory).
- **File name**: PrivacyFence reads `org_bundle.json`. `org_config.json` is the 6.x name: on the
  first start after an upgrade the daemon copies it (and its two pin files) to the new names and
  leaves the originals untouched, and a later change to `org_config.json` is ignored with a warning.
- **Operational settings in an older bundle** (services offered, file delivery, step-up and the
  others listed in the legacy bundle) are imported once into
  `org/admin_config.json` and afterwards ignored; every later start logs a warning naming them.

## Organization server settings

An organization server (the Docker image, [guide](org-mode-setup-guide.md)) reads no
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
| `PRIVACYFENCE_MODE` | `org` | Organization mode is the only mode, so leave it unset or set it to `org`. `local` and any other value refuse the start, and so does a legacy bundle whose `mode` disagrees. |
| `PRIVACYFENCE_ISSUER_URL` | `http://localhost:<port>` in single-user mode, otherwise required | The public origin, with no path, such as `https://pf.acme.example.com`. Every redirect URI and link is built from it. |
| `PRIVACYFENCE_BIND_HOST` | `127.0.0.1` (`0.0.0.0` in the Docker image) | The address the daemon listens on. |
| `PRIVACYFENCE_PORT` | `8765` | The port the daemon listens on (1 to 65535). |
| `PRIVACYFENCE_TRUSTED_PROXIES` | none | The reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are trusted. `*`, `0.0.0.0/0` and `::/0` are refused. With the shipped Caddy this is `172.30.87.2`. |
| `PRIVACYFENCE_IDP_ISSUER`, `PRIVACYFENCE_IDP_CLIENT_ID` | none | The OIDC client of your identity provider. Set both or neither: setting only one refuses the start. Setting neither (and nothing else of the `PRIVACYFENCE_IDP_*` family or `PRIVACYFENCE_ADMIN_EMAILS`) is single-user mode. |
| `PRIVACYFENCE_IDP_CLIENT_SECRET`, `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE` | none | The client secret, or the path of a file that holds it (a Docker secret). Set one of the two, never both. The secret is never logged. |
| `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM`, `PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES` | none | The ID-token claim that makes a person an administrator and its values (comma-separated), matched exactly and case-sensitively. See [Who is an admin](org-mode-setup-guide.md#who-is-an-admin). |
| `PRIVACYFENCE_ADMIN_EMAILS` | none | Administrators by email. A person is an administrator when the identity provider reports the address as verified (`email_verified` is exactly `true`), and with Google only when the account's Workspace domain (`hd`) matches the address. Matching ignores case and surrounding spaces; an address with a non-ASCII character never matches. |
| `PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES` | none | `acr_values` sent when step-up asks the identity provider for a fresh sign-in. |
| `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL` | `false` | `true` or `false`. Allowed email domains (Admin → Sign-in) trust an address only when the identity provider reports it verified. Set `true` for a provider that never sends `email_verified`, such as Microsoft Entra ID. It never makes anyone an administrator. It applies only to addresses in an allowed domain, so set `PRIVACYFENCE_ALLOWED_DOMAINS` (or the Admin page's list) too; it also decides the `email_verified` that Peek and Grist receive ([OIDC provider](oidc-provider.md)). |
| `PRIVACYFENCE_IDP` | none | `builtin-google` signs people in with PrivacyFence's built-in Google app through `oauth.privacyfence.eu`. It needs `PRIVACYFENCE_ADMIN_EMAILS` and a public `https://` `PRIVACYFENCE_ISSUER_URL`, cannot be combined with any `PRIVACYFENCE_IDP_*` variable, and refuses everyone not named as an administrator or in an allowed domain. Any other value refuses the start. See [the built-in Google sign-in](org-mode-setup-guide.md#built-in-google-sign-in). |
| `PRIVACYFENCE_ALLOWED_DOMAINS` | none | Comma-separated domain names (for example `acme.com`). On an organization server's first start, unless `org/admin_config.json` was written before it, they fill Admin → Sign-in → Allowed email domains, with your own identity provider or with `builtin-google`; afterwards edit them on that page (the variable is then ignored). A bundle that sets allowed domains wins. A value that is not a domain name refuses the start. |
| `PRIVACYFENCE_PREVIEW_SERVICES` | none | A comma-separated list of service names, or `*`. A service whose connect flow has shipped but whose tools have not (`oauth_apps.PREVIEW_SERVICES`, empty in a release unless one is in progress) is hidden on the Admin page and in the organization config unless it is named here. Used by the QA server, which sets `*`. |
| `PRIVACYFENCE_PEEK_BASE_URL` | none | The Peek server's address. When set it is fixed: the Admin page shows it and does not let anyone change it. |
| `PRIVACYFENCE_GRIST_SERVER_URL`, `PRIVACYFENCE_GRIST_PROVISIONER_URL` | none | The Grist server's address and the Grist provisioner service PrivacyFence calls for automatic accounts (`https://`; `http://` only for localhost). Fixed in the same way. |
| `PRIVACYFENCE_PEEK_AUTO_PROVISION`, `PRIVACYFENCE_GRIST_AUTO_PROVISION` | unset | Unset or empty: the Admin page decides. `true`: Peek (or Grist) is offered and Automatic accounts are on, shown on Admin → Services as set by the server and locked. Any other value refuses the start; removing the variable hands the switch back. Needs the service's address and provisioning key, and the web server (`web.mcp.enabled`). Organization mode only. On a single-user install it connects the one person, `admin@privacyfence.localhost`, and needs `localhost` addresses. |
| `PRIVACYFENCE_PEEK_PROVISIONING_KEY`, `PRIVACYFENCE_GRIST_PROVISIONING_KEY` | none | The provisioning keys for automatic Peek and Grist accounts. The names are the defaults of `services.peek.auto_provision.key_env` and `services.grist.auto_provision.key_env`; a different name there is read the same way. |
| `PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE`, `PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE` | none | The path of a file that holds the key (a Docker secret). For a custom key variable `NAME`, the file form is `NAME_FILE`. Setting both the plain variable and the file refuses the start, and so does a file that cannot be read. The key is never logged. |
| `PRIVACYFENCE_N8N_URL` | none | The n8n address a delivered scoped token is written to ([guide](org-mode-setup-guide.md#delivering-tokens-to-n8n)). `https://` on any host except a link-local, unspecified or multicast IP literal, a host written only as numbers (such as `2852039166`) and `metadata.google.internal`; no userinfo, query or fragment. Read only from the environment, never from the Admin page. A malformed value refuses the start; unset makes delivery unavailable. Organization mode only. |
| `PRIVACYFENCE_N8N_API_KEY`, `PRIVACYFENCE_N8N_API_KEY_FILE` | none | The n8n API key (create and delete credentials), or the path of a file that holds it (a Docker secret). Set one, never both: both, or an unreadable file, refuses the start. The key is never logged, audited, stored by PrivacyFence or returned. An internal CA is trusted through `REQUESTS_CA_BUNDLE`. |
| `PRIVACYFENCE_N8N_DELIVERY` | unset | Unset or empty: the Admin page decides. `true`: n8n delivery is on, and so are scoped tokens and AI token proposals (`agent_runtimes.n8n.enabled`, `scoped_tokens.enabled`, `scoped_tokens.allow_ai_proposals`), shown on Admin as set by the server and locked; `scoped_tokens.max_days` stays editable. Any other value (`false`, `1`) refuses the start, and so does `true` without `PRIVACYFENCE_N8N_URL` and the API key. Removing the variable hands the fields back to what Admin stored. Organization mode only ([ADR 0294](adr/0294-n8n-delivery-can-be-switched-on-from-the-server-environment.md)). |
| `PRIVACYFENCE_N8N_PROJECT_ID` | unset | The n8n project the credential is created in: 1 to 36 letters and digits. When set it locks `agent_runtimes.n8n.project_id` on Admin. Any other value refuses the start. Works with or without `PRIVACYFENCE_N8N_DELIVERY`. Organization mode only. |
| `PRIVACYFENCE_N8N_MCP_URL` | unset | The PrivacyFence `/mcp` address Claude is told to use: `https://`, or `http://` on localhost. When set it locks `agent_runtimes.n8n.mcp_url` on Admin. Any other value refuses the start. Works with or without `PRIVACYFENCE_N8N_DELIVERY`. Organization mode only. |
| `PRIVACYFENCE_OIDC_CLIENTS` | none | The web apps, such as Peek and Grist, that sign people in through PrivacyFence (OpenID Connect at `<issuer>/oidc`), comma-separated lower-case names.  |
| `PRIVACYFENCE_OIDC_CLIENT_<NAME>_REDIRECT_URIS` | none | The app's redirect addresses, comma-separated, matched exactly (`https://`, or `http://` for localhost). `<NAME>` is the app name in capitals with `-` as `_`. |
| `PRIVACYFENCE_OIDC_CLIENT_<NAME>_SECRET`, `PRIVACYFENCE_OIDC_CLIENT_<NAME>_SECRET_FILE` | none | The app's client secret (at least 32 characters), or the path of a file that holds it. Set one, never both. Never logged. See [Sign in to other apps with PrivacyFence](oidc-provider.md). |
| `PRIVACYFENCE_OIDC_CLIENT_<NAME>_REQUIRE_PKCE` | `true` | `true` or `false` (exact, lower case; anything else refuses the start). With `false` the app may sign in without PKCE, for an app that cannot send it, such as Forgejo; an app that sends PKCE anyway is still checked, and the daemon warns at start. See [Sign in to other apps with PrivacyFence](oidc-provider.md). |
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
[Admin page](org-mode-setup-guide.md#the-admin-page). It holds
everything operational and is part of the backup. Do not edit it while the daemon runs; to change it, stop the daemon first. Do not
rely on a hand edit surviving an Admin page save unless you restart afterwards. The only reason to
edit it by hand is to relax a setting the page only lets you tighten (step-up), or to set a key the
page has no control for. For any other key, stop the daemon, edit, start. A file that is not valid JSON, is not an
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
| `scoped_tokens.enabled`, `max_days`, `allow_ai_proposals` | scoped tokens; `allow_ai_proposals` lets an AI client propose one ([ADR 0253](adr/0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md)); `max_days` is the longest lifetime of a new token and the most a person can extend one by. `PRIVACYFENCE_N8N_DELIVERY=true` sets `enabled` and `allow_ai_proposals` to true and locks them on the page; the file keeps what Admin stored | Scoped tokens |
| `agent_runtimes.n8n.enabled`, `project_id`, `mcp_url`, `credential_type` | n8n delivery of AI-proposed scoped tokens ([ADR 0278](adr/0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md)): on or off, the n8n project the credential is created in (empty: the API key owner's personal space), and the PrivacyFence `/mcp` address Claude is told to use (empty: this server's address). `credential_type`: `httpBearerAuth` (the default: n8n's built-in bearer credential, for an MCP Client node) or `privacyFenceApi` (a PrivacyFence API credential for the [PrivacyFence node](n8n-node.md); it needs `mcp_url`, and a restart applies it). The n8n address and key come from the environment, and so can `enabled` (`PRIVACYFENCE_N8N_DELIVERY=true`), `project_id` (`PRIVACYFENCE_N8N_PROJECT_ID`) and `mcp_url` (`PRIVACYFENCE_N8N_MCP_URL`): a variable that is set locks its field on the page | Agent runtimes |
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
| `org_bundle.json` | the daemon, once (copied from `org_config.json`) | The legacy bundle ([above](#legacy-bundle-orgorg_bundlejson)) |
| `org_bundle_signing_pubkey.txt`, `org_bundle_serial.txt` | the daemon | The pinned bundle signing key and the highest bundle serial accepted. Deleting the key file accepts a new key at the next start; keep the serial file unless you want to go back to an older bundle on purpose. After an upgrade, deleting the new-name pin is the rotation: the 6.x pin (`org_config_signing_pubkey.txt`) is not copied again. Delete the 6.x pins only when going back to 6.x is no longer wanted. |
| `org_config.json`, `org_config_signing_pubkey.txt`, `org_config_serial.txt` | PrivacyFence 6 | The 6.x names, never changed by 7.x. |
| `org_bundle_adopted.json` | the daemon | Records the copy from `org_config.json` (its hash and time), that the pins were adopted, and a retirement. |
| `agent_runtime_credentials.json` | the daemon | One record per scoped token delivered to n8n: the n8n credential's id and name, the token id, the proposal, the project, the n8n address it went to and its state (`creating`, `delivered`, `abandoned`). Never a secret. The cleanup sweeper reads it to delete the credential when the token is revoked. An expired token's credential is kept until the person extends the token (Settings → Scoped tokens → Extend) or revokes it. |
| `admin_config.json` | the daemon | [Above](#admin-config-orgadmin_configjson). Back it up. |
| `connector_configs/`, `connector_config_signing_pubkey.txt`, `connector_config_serials.json` | the daemon | [Connector configs](#connector-configs). |
| `relay_token.txt` | the daemon, on first need | The 43-character secret the server proves its host name with: it sends the secret to the relay and serves its SHA-256 at `/.well-known/privacyfence-relay`. Keep it private. With the built-in Google sign-in it is also behind every `_privacyfence-relay` DNS TXT record (`pf-relay=v1:<SHA-256 of "v1\|<host>\|<that hash>">`). Back it up: replacing it means publishing every record again. |
| `relay_status.json` | the daemon | When the relay last accepted this server, which sign-in domains it has verified, and which it refused as public suffixes. |
| `oidc_signing_key.pem` | the daemon, when `PRIVACYFENCE_OIDC_CLIENTS` is set | The RSA key that signs ID tokens for [OIDC apps](oidc-provider.md#the-signing-key). Deleting it rotates the key at the next start. |
| `oauth_clients.json`, `oauth_refresh.json`, `web_push_vapid_key.pem` | the daemon | Registered AI clients, their refresh tokens and the push key ([backup](org-mode-setup-guide.md#backup)). |
| `../data_format.json` | the daemon | The marker of the data format, written once every person's data is up to date ([upgrade](org-mode-setup-guide.md#upgrading-to-8x)). |

## Command-line options

`privacyfence-app` is the server's executable; the image starts it. The options below are for
running it by hand, for example with `docker compose exec`.

| Option | What it does |
|---|---|
| `--config PATH` | Read `settings.yaml` from `PATH` instead of its default location. |
| `--revoke-scoped-tokens PRINCIPAL_ID` | Revoke every scoped token that account holds (the principal id the audit log and the Settings page show) and exit. Run it as the account the daemon runs as. |
| `--peek-release PRINCIPAL_ID` | Revoke the Peek token PrivacyFence minted for that account, stop renewing it, and exit. The Peek account stays. Needs the Peek provisioning key in the environment; run it as the account the daemon runs as. |
| `--relay-dns-records` | Print the DNS TXT records the built-in Google sign-in still needs, one line per sign-in domain the relay has not found yet, and exit. Organization mode ([guide](org-mode-setup-guide.md#built-in-google-sign-in)). |
| `--print-env-from-bundle` | Print the `PRIVACYFENCE_*` lines that say what a legacy `org/org_bundle.json` says about the mode, the server and the identity provider, and exit. The client secret is never printed. See [Legacy bundle](#legacy-bundle-orgorg_bundlejson). |
| `--idp-secret-file PATH` | With `--print-env-from-bundle`: write the identity provider's client secret to `PATH` (a new file, mode `0600`) and print the variable that names it. |
| `--retire-bundle` | Import the legacy bundle's app credentials as connector configs if that has not happened, and delete `org/org_bundle.json` once the environment says the same mode, server address and identity provider. |
| `--grist-release PRINCIPAL_ID` | Release the Grist key PrivacyFence holds for that account: the provisioner deletes it in Grist if it created it, and PrivacyFence forgets it. Then the daemon exits. Needs the Grist provisioning key in the environment; run it as the account the daemon runs as. |

Connect services from **/connect** (see [Connecting a service](connecting-a-service.md)).
