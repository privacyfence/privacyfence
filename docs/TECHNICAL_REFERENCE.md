# PrivacyFence technical reference

PrivacyFence is a local or organization-hosted MCP privacy gateway. It sits between an MCP client and third-party data providers, applies policy and approval gates, and records auditable decisions.

## Runtime architecture

A PrivacyFence daemon owns connector clients, policy evaluation, approval state, the audit log, and the embedded HTTP application.

The embedded web application serves:

- the approval list and approval cards;
- settings and connector authorization surfaces;
- state/event streams used by the web UI;
- the `/mcp` Streamable HTTP endpoint;
- org-mode identity/authorization routes when org mode is enabled;
- short-lived staged downloads when org-mode delivery requires them.

Claude Code and other HTTP-capable MCP clients can connect to `/mcp` directly. Claude Desktop uses the bundled Node/TypeScript shim in `mcpb/shim/`, which reads the daemon discovery/auth files and proxies stdio MCP traffic to the daemon's HTTP endpoint.

There is no native AppKit approval/settings runtime. The browser-based embedded UI is the approval/settings surface on every supported platform.

## Modes

### Local mode

Local mode represents one user/principal for the life of the daemon. Connector credentials and policy are resolved for that local user. The web server binds locally and uses a bootstrap/session mechanism for the human approval/settings UI plus bearer-token protection for MCP.

### Org mode

Org mode is a centralized Linux/server deployment. Human identity is established through the configured OIDC identity provider, MCP authorization is handled by PrivacyFence's org authorization flow, and connectors are scoped per principal.

`ConnectorRegistry` lazily builds and caches one connector host per authenticated principal, with bounded capacity and idle eviction. User-scoped state paths are resolved inside the active principal scope.

Org mode is normally deployed behind the configured HTTPS reverse proxy. See [`org-mode-setup-guide.md`](org-mode-setup-guide.md) and [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md).

## Local state and discovery

`src/privacyfence/paths.py` is the source of truth for application paths.

A source/unbundled development run keeps its normal configuration, credentials, and logs in the repository-local development locations. A bundled release keeps user state under the user's PrivacyFence home directory.

MCP discovery/auth files under the user's PrivacyFence home include the daemon MCP URL and token so the Desktop shim can find the running daemon independently of the checkout/install location.

The daemon enforces a single-instance lock with `portalocker`.

## MCP endpoint

The daemon exposes the MCP protocol over Streamable HTTP at `/mcp` using the official MCP Python SDK. Local mode protects MCP with the generated bearer token. Org mode uses its own authorization/identity path and principal-aware request handling.

The MCP tool registry is built from the configured connectors. Tool calls are routed through the PrivacyFence gate before connector execution where policy requires review or confirmation.

The `initialize` response carries a fixed server `instructions` string (`routes_mcp.py`'s `SERVER_INSTRUCTIONS`, issue #396 Part A) explaining what PrivacyFence is, that an empty or partial tool list means this install's connectors aren't set up yet rather than "nothing to do here", and to call `privacyfence_status` (below) before the first PrivacyFence-governed action in a conversation or when a human asks why a connector is missing. This is the mechanism that makes a fresh, un-onboarded install self-describing instead of silently empty — see the client-facing side of the same problem under "Meta-tools" below.

The `initialize` response also advertises `capabilities.tools.listChanged: true` (issue #396 Part C). `routes_mcp.py`'s `_PrivacyFenceServer` overrides `create_initialization_options()` to force this on, since the MCP SDK's own `StreamableHTTPSessionManager` drives every session with no initialization options of its own, leaving the runner to call that method with no arguments, which would otherwise leave it off. `build_mcp_server()` captures each session's live `Connection` (from inside a request handler, keyed by the same per-session `session_key` `McpDispatcher.end_session` uses -- the transport's own `Mcp-Session-Id`) and wires a broadcaster onto the dispatcher (`McpDispatcher.set_tools_changed_broadcaster`/`notify_tools_changed`); `SettingsController.refresh_connectors()` calls it after actually swapping the live connector set (authenticating, disabling, or manually refreshing a connector), so an already-connected client is told `notifications/tools/list_changed` rather than needing to reconnect to see the new tool list.

## Meta-tools

Alongside the connector-derived tools, the daemon exposes nine `privacyfence_`-prefixed meta-tools over the same `/mcp` endpoint (`web/mcp_tools.py`'s `META_TOOLS`), dispatched by `routes_mcp.py`'s `_dispatch_meta_tool` to `McpDispatcher` methods (`web/mcp_dispatch.py`) that call back into `gate.py`/`auto_accept.py`/the `policy` package. Each takes a `reason` string, logged the same self-reported, unverified way as every gated connector tool's own `reason` param.

- `privacyfence_check_policy` — asks whether a specific `(connector, tool, args)` call would auto-accept or need a human, without making the call or having any side effects. Returns one of `auto_accept`, `requires_review`, or `unknown` (whether it auto-accepts can depend on fetched content this can't see in advance); for `review`-gated tools, `pii_gate_may_apply` is always `true`, since the PII gate scans real content and can never be predicted ahead of time. Since P7 of the policy v2 redesign, the result also carries `matched_rule_id` — the policy engine's own stable id for whatever will let the call through (`null` unless `verdict` is `auto_accept`), checked against the always-on v2-store layer first and the compiled-from-v1 rules second (`gate.preflight_auto_accept`), so it never predicts something the real gated call wouldn't do; pass it straight to `privacyfence_propose_policy_change`'s `rule_id` to narrow or remove that rule. Safe to call as often as needed while planning a task.
- `privacyfence_list_policy` / `privacyfence_propose_policy_change` (P7 of the policy v2 redesign) — the current, single scope+verb write path: one rule shape (`policy/store.py`'s on-disk `auto_accept:` section) instead of the older `target: "rule" | "grant"` split. `privacyfence_list_policy` returns `{rules, scope_groups}` — every configured v2 rule, sentence-rendered (`policy/describe.py`) with its stable id, `verbs`/`covered_tools` stating exactly how wide it is, plus the scope catalogue (`policy/catalogue.py`, shared with the Auto-accept Settings page's own "add a rule" form) that `privacyfence_propose_policy_change`'s `group`/`verbs` validate against. `privacyfence_propose_policy_change` adds, updates, or removes a rule by `group` (a scope type, e.g. `drive.folder`)/`value`/`verbs`, or by `rule_id` for `update`/`remove`; a verb the named group cannot govern is rejected — before any popup — rather than silently persisted as a rule nothing could ever render or remove (closing the write-time half of what the redesign proposal's F5 found: three operation groups, `apps_script.*`/`gmail.create_filter`/`gmail.update_filter`/`slack.create_group_chat`, had no configurable rule *and* no validation stopping one from being written anyway). Same confirmation contract as the tool it replaces: always blocks on a native dialog, throws if declined or if the connection is unattended. Since the self-approval review's Phase 4 that dialog is registered as *sensitive* (`approvals.PendingApprovalRegistry.register_confirm`) — nothing gated it first, so confirming one takes what the equivalent Settings action takes: an attributable session, and a passkey wherever `require_passkey` is on. See [`security-and-compliance.md`](security-and-compliance.md#a-confirmation-dialog-is-not-always-a-second-step).
- `privacyfence_list_auto_accept_rules` / `privacyfence_propose_auto_accept_rule_change` — DEPRECATED as of P7, kept for one minor release as working aliases accepting the older `auto_accept_rules`/`auto_accept_grants` *request* shape (`target: "rule" | "grant"`); new code should use `privacyfence_list_policy`/`privacyfence_propose_policy_change` above instead. Only the *request* shape is v1: `privacyfence_list_auto_accept_rules` returns exactly what `privacyfence_list_policy` returns (reading the old sections directly would show whatever they held before the one-time migration folded them into v2 — stale content, not what actually auto-accepts), and `privacyfence_propose_auto_accept_rule_change` translates its older `target: "rule" | "grant"` request into the same v2 rule `privacyfence_propose_policy_change` would create and writes it to the same `auto_accept:` section. Neither reads or writes `auto_accept_rules`/`auto_accept_grants` any more; nothing does, outside `policy/compat.py`'s one-time migration. Always blocks on a native confirmation dialog a human must approve — there is no way to change this config without one, even for an entry that already exists — and throws if declined, or outright if the connection is in an unattended session.
- `privacyfence_begin_unattended_session` / `privacyfence_end_unattended_session` — see "Scheduled / unattended Cowork tasks" below.
- `privacyfence_await_approval` — long-polls one or more `approval_id`s from a gated call's `{status: "approval_pending", approval_id, ...}` result and reports status only (`pending`, `approved`, `denied`, `expired`, or `unknown`), never content — a re-issue of the original gated call with the same arguments is still what actually retrieves data once `approved`. Both this tool's own description and `gate.py`'s `_pending_result` `message` field tell the calling agent, in-band, not to sit on a pending approval silently: relay the `url` to the human first, then either call this tool to wait, or — where the client can schedule a follow-up (a reminder, a background check) — schedule one instead of blocking the conversation.
- `privacyfence_status` (issue #396) — the one meta-tool guaranteed to answer even when `connectors == []` leaves every other tool missing, so an empty or partial tool list reads as "not set up yet", not "nothing to do here". Returns `{mode, setup_complete, connectors, next_step, message}` and, whenever setup isn't complete, `sign_in_url` (always `null` — see below): `mode` is `"local"` or `"org"`; `connectors` is a list of `{name, enabled, authenticated, blocked_by}` (`blocked_by` is `null` once authenticated or deliberately disabled, otherwise `"no_org_config"`, `"not_authenticated"`, or a short redacted reason — `SettingsController.status_connectors`, sourced from `build_connectors()`'s own per-connector failure map); `setup_complete` is `true` once at least one connector is authenticated. **This tool never mints a sign-in credential itself, and since the self-approval plan's Phase 2 nothing over `/mcp` does** — a threat-model follow-up to the original issue found that a bootstrap code handed back here would be a live credential (code → `pf_session` cookie → a gated approval's own decide route) issued because a *model* decided to check status, not because a human asked. `privacyfence_get_sign_in_link`, which minted exactly that on request, is retired: its own stated justification (the companion app "happens to be running", nothing installs or starts it automatically) expired when [ADR 0003](adr/0003-separated-installs-only.md) made the companion mandatory on all three platforms, and a session is no longer something to hand the party it governs. So when local mode is un-onboarded, `next_step` is `"open_privacyfence_companion"` and `message` tells the model to send the human to the companion's own Open Settings item, with no link for the model to relay; organization mode's `next_step` is `"contact_your_administrator"` instead, since org mode has no bootstrap-link concept at all. A human whose companion menu is out of reach runs `privacyfence-app --print-sign-in-link` themselves — see `daemon_main.run_print_sign_in_link()`. Wired via `McpDispatcher.set_connectors_state_provider`.

Every tool advertised over `/mcp`, meta-tools included, carries the same uniform read-only/non-destructive/idempotent annotations regardless of its real effect (`_UNIFORM_READ_ONLY_ANNOTATIONS` in `web/mcp_tools.py`) — those are MCP UI hints, not a security boundary. The real authorization is the gate itself, enforced here in the daemon.

### Scheduled / unattended Cowork tasks

`privacyfence_begin_unattended_session` tells PrivacyFence that the rest of this MCP connection is a scheduled/unattended run — a Cowork Routine firing on a schedule with no human necessarily watching — rather than an interactive conversation. It errors unless an administrator has opted the install into this: `unattended_sessions.enabled` in `org/org_config.json`, a deliberate per-organization setting, not a per-user one.

Once set, the flag is tracked per MCP session (`McpDispatcher._unattended_sessions`) and read by `gate.py`'s `is_unattended()` through every gated call on that connection. It changes exactly one thing: a call that isn't already covered by a configured auto-accept rule is denied immediately (audited as `denied_unattended`) instead of PrivacyFence opening a native approval dialog nobody is there to answer. It never changes what auto-accepts, only what happens when nothing does. `privacyfence_propose_auto_accept_rule_change`/`privacyfence_propose_policy_change` are likewise refused outright in an unattended session, since a config change always requires a human confirmation.

`privacyfence_end_unattended_session` clears the flag, restoring normal interactive approval behavior — not strictly required, since it also clears when the connection closes, but useful if the connection might be reused afterward for something interactive. Pairing `privacyfence_check_policy` with a scheduled run lets it plan around steps that would otherwise need a human who isn't there.

## Approval model

A gated request becomes a `PendingApproval` managed by `PendingApprovalRegistry`.

The approval list at `/approvals` can contain multiple pending requests. Each row exposes **Deny** and **Review**; there is deliberately no one-click Allow on the list. Review opens the full approval card, where the user can inspect the operation and make the decision.

Pending approvals update in the browser through the state/event stream. Decisions are idempotent: an approval that is no longer pending cannot be approved again as a fresh request.

Approval content is built by `approval_window_html.py` and confirmation content by `dialog_window_html.py`; the web routes inject the browser decision bridge and enforce the current session/CSRF/CSP controls. See [`approval-window-content-reference.md`](approval-window-content-reference.md).

## Gate behavior

Connector tools declare their gate behavior through the shared connector/tool machinery. The important policy outcomes are:

- **auto** — the operation can run without a human decision under the current policy;
- **review** — PrivacyFence shows the read/retrieval operation before releasing protected data;
- **popup/write confirmation** — PrivacyFence requires an explicit decision before a write or other sensitive action;
- **PII confirmation** — detected sensitive content can require an additional confirmation before release/delivery.

Always-allow rules can bypass a matching future approval only within the rule shape and scope the user approved. See [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

## Connectors & privacy matrix

This section lists preview/details text per tool, grouped by connector. For a cut across *what
Claude already knows from prior auto-approved calls* before it ever reaches a given gated tool —
i.e. how much of a "review" tool's return value is actually new information — see
[`claude-knowledge-boundary.md`](claude-knowledge-boundary.md). For the approval dialog's own
layout and optional sections (AI-visibility checklist, PII banner, etc.), see
[`approval-window-content-reference.md`](approval-window-content-reference.md).

### Gmail

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `gmail_list_messages` | read | auto | — | — |
| `gmail_list_threads` | read | auto | — | — |
| `gmail_get_message` | read | review | from, recipients, date, subject | Full body text |
| `gmail_get_thread` | read | review | subject, all participants, message count, date range | All messages in thread |
| `gmail_list_message_attachments` | read | auto | — | — |
| `gmail_download_attachment` | read | review | from, subject, attachment name, size, save path | — |
| `gmail_create_draft` | write | popup | — | To, cc, subject, full body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_draft` | write | popup | — | In reply to, to, cc/bcc, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_all_draft` | write | popup | — | In reply to, to, also-to (expanded participants), cc/bcc, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_create_draft_with_attachments` | write | popup | — | To, cc, subject, attachment names/sizes, full body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_draft_with_attachments` | write | popup | — | In reply to, to, cc/bcc, attachment names/sizes, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_all_draft_with_attachments` | write | popup | — | In reply to, to, also-to (expanded participants), cc/bcc, attachment names/sizes, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_add_label` | write | popup | — | From, subject, label name |
| `gmail_remove_label` | write | popup | — | From, subject, label name |
| `gmail_archive_message` | write | popup | — | From, subject, confirmation that message stays in All Mail |
| `gmail_list_filters` | read | auto | — | — |
| `gmail_list_labels` | read | auto | — | — |
| `gmail_create_filter` | write | popup | — | Criteria, actions |
| `gmail_update_filter` | write | popup | — | Filter ID, criteria, actions, note that this deletes + recreates under a new id |
| `gmail_create_label` | write | popup | — | Label name, note when a parent segment will also be created |

### Google Drive

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `drive_list_files` | read | auto | — | — |
| `drive_get_file_metadata` | read | auto | — | — |
| `drive_list_folder` | read | auto | — | — |
| `drive_list_shared_drives` | read | auto | — | — |
| `drive_create_blank_file` | write | auto | — | — |
| `drive_get_file_content` | read | review | file name, owner, size, modified date | First ~500 chars of content (a Google Doc renders as Markdown — headings, bold/italic/strikethrough/underline/code/link/highlight, horizontal-rule dividers, nested lists, real GFM tables; a non-default highlight/text color also adds `highlights`/`text_colors`) |
| `drive_download_file` | read | review | file name, owner, size, save path | File name, owner, size, modified date, save path |
| `drive_write_file_content` | write | popup | — | File name, owner, new content (plain text) |
| `drive_upload_file` | write | popup | — | File name, size, destination folder |
| `drive_write_doc_content` | write | popup | — | File name, owner, Markdown preview (headings, bold/italic/strikethrough/underline/code, ==highlight== (styles nest, e.g. bold+highlight together), links, nested lists, a `---`/`***`/`___` horizontal-rule divider, tables rendered as rich formatting in the Google Doc) |
| `drive_docs_edit_content` | write | popup | — | File name, owner; find/replace text goes in the details pane, not the preview |
| `drive_docs_format_content` | write | popup | — | File name, owner, formatting summary; the located text goes in the details pane |
| `drive_move_file` | write | popup | — | File name, from folder → to folder |
| `drive_add_comment` | write | popup | — | File name, full comment text |
| `drive_sheets_create` | write | auto | — | — |
| `drive_sheets_get_metadata` | read | auto | — | — |
| `drive_sheets_get_values` | read | review | spreadsheet name, owner, range | Cell values (or formulas, or +formatting) in the range |
| `drive_sheets_write_range` | write | popup | — | Spreadsheet name, owner, range, values/formulas being written |
| `drive_sheets_add_sheet` | write | popup | — | Spreadsheet name, owner, new tab title/dimensions |
| `drive_sheets_rename_sheet` | write | popup | — | Spreadsheet name, owner, tab id, new title |
| `drive_sheets_format_range` | write | popup | — | Spreadsheet name, owner, range, formatting being applied |
| `drive_sheets_insert_dimensions` | write | popup | — | Spreadsheet name, owner, tab id, rows/columns being inserted |
| `drive_sheets_delete_dimensions` | write | popup | — | Spreadsheet name, owner, tab id, rows/columns being deleted (data-loss warning) |

Google Sheets is not a separate connector — the `drive_sheets_*` tools live on the Drive
connector and reuse its OAuth grant (the Sheets API accepts the same `drive` scope). There is
intentionally no delete-sheet tool: `drive_sheets_rename_sheet` is the sanctioned way to mark a
tab for removal (e.g. rename it to `TO BE DELETED - <original title>`) — you delete it by hand
in the Sheets UI. `drive_sheets_write_range` has no separate "set formula" tool either — a cell
string starting with `=` is evaluated as a formula, exactly like typing it into the Sheets UI.

`drive_sheets_get_values` defaults to displayed cell values (`value_render_option=FORMATTED_VALUE`,
e.g. `"$1.00"`); pass `UNFORMATTED_VALUE` for the raw underlying value (e.g. `1`) or `FORMULA` to
read formula text (e.g. `"=A1+A2"`) instead of its computed result — there's no separate
"get formulas" tool, same reasoning as writing them. `include_formatting=true` additionally fetches
a same-shaped grid of per-cell formatting (bold/italic/text color/background color/number
format/horizontal/vertical alignment/text wrap — the same aspects `drive_sheets_format_range` can
set), returned alongside the values as `{"values": [...], "formatting": [...]}` instead of a bare
values grid. Formatting doesn't carry the same PII risk as cell text, so it isn't run through the
privacy filter/PII scan the way values are.

`drive_docs_edit_content` and `drive_docs_format_content` locate existing text in a Google Doc by
exact match against its **plain, unformatted** text — `find_text` must match exactly one location
unless `replace_all` is set, so an ambiguous match raises rather than guessing which occurrence was
meant. This is *not* the same representation `drive_get_file_content` returns for a Google Doc —
that one renders Markdown (`**bold**`, `# Heading`, etc.), so `find_text` for these two tools must
be a substring of the doc's real words, with any Markdown formatting markers
`drive_get_file_content` added stripped back out first (e.g. search for `bold text`, not
`**bold text**`). Unlike `drive_write_doc_content`, they
touch only the matched span, not the whole document. `drive_sheets_insert_dimensions`/
`drive_sheets_delete_dimensions` insert or remove whole rows/columns (not just cell content) in a
tab, shifting everything after the insertion/deletion point; there is no undo path through
PrivacyFence for a delete.

### Slack

**Auth:** OAuth2 (browser sign-in), user token scope. Sees exactly what you see — no bot to invite. See [slack-setup.md](slack-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `slack_list_channels` | read | auto | — | — (optional `participant` filter matches channel membership by user id, handle, or display name, comma-separated for multiple; one extra `conversations.members` call per channel, plus one `users.info` call per unresolved member if no needle matches by id alone) |
| `slack_list_dms` | read | auto | — | — (each entry is `id` + the other participant; optional `participant` filter matches by user id, handle, or display name) |
| `slack_list_group_chats` | read | auto | — | — (each entry is `id`, `name`, and resolved `member_ids`/`member_names`; optional `participant` filter matches by user id, handle, or display name, comma-separated to require all of them as members of the same group chat; one extra `conversations.members` call per group chat) |
| `slack_resolve_permalink` | read | auto | — | — (parses a pasted Slack message permalink into `channel_id`/`channel_name`/`ts`/`thread_ts`, ready for `slack_get_channel_history`/`slack_get_thread_replies`; no Slack API call beyond a best-effort channel-name lookup) |
| `slack_refresh_user_cache` | read | auto | — | — (forces an immediate `users.list` re-sync of the on-disk, weekly-refreshed user name/email cache that `slack_get_channel_history`/`slack_get_thread_replies`/`slack_search_messages` use to resolve message authors without a per-message `users.info` call; call after a teammate joins mid-week so they resolve correctly before the next automatic refresh) |
| `slack_refresh_channel_cache` | read | auto | — | — (forces an immediate re-sync of the on-disk, weekly-refreshed channel/DM/group-DM name cache those same read tools use to resolve which conversation a message belongs to without a per-message `conversations.info` call; call after a new channel is created so it resolves by name right away. On a workspace with enough channels that one call can't finish before the calling MCP client's own tool-call timeout, the result's `has_more` flag comes back `true` — call the tool again to resume from where it left off) |
| `slack_get_channel_history` | read | review | channel name, message count, first message (80 chars) | All messages |
| `slack_get_thread_replies` | read | review | channel name, thread starter (80 chars), reply count | All replies |
| `slack_search_messages` | read | review | query and/or participant, result count | All results |
| `slack_create_group_chat` | write | popup | — | Resolved participant names (or raw user ids when unresolvable); returns the new/reopened conversation's `id`, ready for `slack_send_message` |
| `slack_send_message` | write | popup | — | Channel name, full message text (optional `mark_unread=true` leaves the message unread after sending; requires `mark` scope) |

`slack_list_group_chats` only lists group DMs that already exist — there's nothing to list until one
has been opened at least once. To start a brand-new group chat, call `slack_create_group_chat` with
2+ participant user ids (from `slack_list_dms`/`slack_list_group_chats`, or a message's `user_id`
field — it does not resolve email addresses or handles), then pass the returned `id` to
`slack_send_message` as `channel_id`.

`slack_list_channels`/`slack_list_group_chats` both accept the same comma-separated `participant`
matching (user id, handle, or display name; comma-separated to require all of them as members of the
same channel/chat) — the tool to reach for when a lookup is "the group chat/channel with Alice and
Bob", as opposed to `slack_search_messages`'s participant matching below, which is for message
content once the right conversation is already known.

`slack_search_messages` accepts an optional `participant` (user id, handle, or display name;
comma-separated to require a group chat containing all of them) alongside or instead of `query`.
When given, it skips Slack's own search index — whose `from:`/`in:` modifiers need exact handle
syntax and don't reliably index every message — and instead reads the matching DM/group-chat
conversation(s) directly via the same matching `slack_list_dms`/`slack_list_group_chats` use,
optionally narrowed by `query` as a client-side text filter. Prefer `participant` over a
text-only `query` for "messages from Bob" or "messages with Bob and Jane" style lookups.

`slack_resolve_permalink` decodes a message permalink (Slack's "Copy link" on any message) into the
`channel_id`/`ts` (and, for a link to a threaded reply, the thread root's `thread_ts`) that
`slack_get_channel_history`/`slack_get_thread_replies` need — the permalink already carries both, so
this needs no `slack_list_channels`/`slack_search_messages` call at all to resolve a link a human
pasted directly.

`slack_refresh_user_cache`/`slack_refresh_channel_cache` refresh the on-disk snapshots that
`get_user_info`/`resolve_channel_name`/`resolve_is_group_dm` check before falling back to a live,
per-item Slack call — without them, resolving every message author/channel in a `slack_search_messages`
result costs one `users.info` and one `conversations.info` call per unique sender/channel, every time.
Both snapshots refresh automatically about once a week — checked once the IPC server comes up on every
daemon restart (so a snapshot that went stale while the app was closed is caught then, not on whatever
tool call happens to run first), in the background so the refresh can't delay startup completing,
and, in between restarts, lazily on first use once seven days have passed. These two tools
exist purely for the exception: someone new (a hire, a channel) needs to resolve correctly *before* the
next automatic refresh. Neither tool reads any message content; both are auto-approved.
`slack_refresh_channel_cache` specifically bounds each call to a fixed number of `conversations.list`
pages so a large workspace can't run one call past the calling MCP client's own timeout — see the table
above for the `has_more`/resume behavior; the eager background refresh at startup isn't subject to this
bound, since it isn't racing anyone's timeout.

### Google Calendar

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `calendar_list_calendars` | read | auto | — | — |
| `calendar_list_events` | read | auto | — | — |
| `calendar_get_free_busy` | read | auto | — | — (returns full events when calendar access is available; falls back to busy-slot list otherwise — set `calendar.free_busy_full_event_details: false` in `settings.yaml` to always fall back regardless of access) |
| `calendar_list_rooms` | read | auto | — | — (lists meeting rooms — name, email, building, floor, capacity — from a static directory IT syncs into `org_config.json` via `scripts/sync_room_directory.py`; not a live lookup, so it may be empty until IT has synced one; the Calendar connector's own OAuth client never holds Workspace admin directory access) |
| `calendar_get_event_details` | read | review | title, time, organizer, attendee count | Description, full attendee list, conferencing link, file attachments (e.g. Gemini meeting notes/transcript) |
| `calendar_get_event_visibility` | read | auto | — | — |
| `calendar_list_colors` | read | auto | — | — (lists Calendar's fixed event color palette — id, name e.g. "Tomato", hex background/foreground — via the Calendar API's own `colors().get()`) |
| `calendar_create_event` | write | popup | — | Title, time, attendees, description, location, Google Meet flag, room bookings, color, recurrence rule |
| `calendar_update_event` | write | popup | — | Title, time, fields changing (old → new), Google Meet flag, room bookings, color, "Applies to" scope (recurring events only) |
| `calendar_delete_event` | write | popup | — | Event title, calendar, time, "Applies to" scope (recurring events only) |
| `calendar_set_event_visibility` | write | popup | — | Event title, calendar, visibility change (old → new) |
| `calendar_set_event_color` | write | popup | — | Event title, calendar, color change (old → new) |
| `calendar_create_out_of_office` | write | popup | — | Title, time, fixed "auto-decline new conflicts only" note, decline message |
| `calendar_set_working_location` | write | popup | — | Date, location (office/home), building/label if given |

`calendar_create_out_of_office` and `calendar_set_working_location` are only supported on the
primary calendar (a Google Calendar API restriction) and always create the event there regardless
of any `calendar_id` used elsewhere. The out-of-office auto-decline behavior is fixed to "decline
new conflicting invitations only" — Calendar also supports declining all conflicts or none, but
that isn't exposed here. Working-location presence only offers "office" or "home" (Calendar's third
"custom location" option isn't exposed either).

`calendar_get_event_visibility` returns just the `visibility` field ("default", "public",
"private", or "confidential") without the full attendee/description/attachment fetch
`calendar_get_event_details` does — cheap enough to be auto-approved on its own, the same way
`calendar_list_events` is. `calendar_set_event_visibility` changes only that one field; every other
property of the event is left untouched. There's no separate `calendar_create_event`/
`calendar_update_event` visibility parameter — set it via `calendar_set_event_visibility` after
creating or alongside updating the event.

`calendar_create_event`/`calendar_update_event`'s `color` parameter and the standalone
`calendar_set_event_color` tool (which, like `calendar_set_event_visibility`, changes only that one
field) all accept either a numeric Calendar event color id (`"1"`-`"11"`) or a case-insensitive name
(`"Tomato"`, `"Sage"`, ...) — see `calendar_list_colors` for the full id → name → hex mapping. Names
are this connector's own static table (the Calendar API's `colors().get()` returns hex values per id
but never a name), matching what Calendar's own web UI shows for each id.

**Recurring events.** `calendar_create_event`'s `recurrence` parameter is one or more
RRULE/EXDATE/RDATE/EXRULE lines (e.g. `"RRULE:FREQ=WEEKLY;COUNT=10"`, one per line for more than
one), passed straight through to the Calendar API's own `recurrence` field; omit it for a
non-recurring event. `calendar_update_event` and `calendar_delete_event` both take a `scope`
parameter — `"this"` (default), `"following"`, or `"all"` — matching Google Calendar's own "This
event" / "This and following events" / "All events" edit picker:

- `"this"` acts on exactly the given `event_id`, and is the only meaning `scope` has for a
  non-recurring event.
- `"all"` redirects to the series' master event so the whole series changes in one call.
- `"following"` splits the series at this instance: the old series gets an `UNTIL` ending it just
  before this instance, and (for `calendar_update_event` only — a delete has nothing to insert)
  a new event is created starting here with this call's changes and the original recurrence
  pattern continued open-ended. There is no single Calendar API call for this — Google's own
  documented approach is exactly these two calls. Known limitations of the split, documented
  rather than silently wrong: the new half's recurrence drops any `EXDATE`/`RDATE`/`EXRULE` the old
  series had, and conferencing on the old event isn't carried over to the new half (pass
  `add_google_meet` again on the same call to get one there too).

Both tools also accept `send_updates` (`"none"`, `"all"`, or `"externalOnly"`) — the Calendar API's
own `sendUpdates`, controlling who gets a notification email about the change or cancellation.
Omit it to leave the Calendar API's own default in effect.

### Google Contacts

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `contacts_list` | read | auto | — | — |
| `contacts_search` | read | auto | — | — |
| `contacts_get` | read | auto | — | — |
| `contacts_update` | write | popup | — | Contact name, fields changing (old → new) |
| `contacts_create` | write | popup | — | Name, fields being set |
| `contacts_add_label` | write | popup | — | Contact name, label (creates the label if it doesn't exist) |
| `contacts_remove_label` | write | popup | — | Contact name, label |

Contact deletion is not supported by this connector.

Google's People API blends personally-saved contacts together with Workspace
directory profiles (colleagues) into a single response by default. `contacts_list`,
`contacts_search`, and `contacts_get` each accept a `source` parameter
(`personal`, `directory`, or `both` — default `both`) to split them apart, and
every returned contact carries a `source` field (`personal`, `directory`, `both`
if it's a saved contact who's also a colleague, or `other` for unclassifiable
entries) plus the raw `source_types` it was derived from. `contacts_get` fails
if the fetched resource doesn't match the requested `source`. Directory search
(`contacts_search` with `source="directory"`) is limited to directory profiles
you already have some contact history with — there is no full company-directory
search under this connector's OAuth scope.

### Telegram

**Auth:** Telethon (MTProto). Reads your chats as you, not as a bot.

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `telegram_list_chats` | read | auto | — | — |
| `telegram_refresh_chat_cache` | read | auto | — | — (forces an immediate `get_dialogs` re-sync of the on-disk, weekly-refreshed chat/group/channel name cache that `telegram_get_messages`/`telegram_search_messages` use to resolve which chat a message belongs to; call after a new chat starts so it resolves by name right away) |
| `telegram_get_messages` | read | review | chat name, message count | All messages |
| `telegram_search_messages` | read | review | query, result count | All results |
| `telegram_send_message` | write | popup | — | Chat name, full message text |

`telegram_refresh_chat_cache` refreshes the on-disk snapshot that `get_chat_name`/`get_messages`/
`search_messages` check before falling back to a bare numeric chat id — without it, any chat not
already primed by a recent `telegram_list_chats` call (in particular, any chat surfaced only via
`telegram_search_messages`, or after a daemon restart) shows up unresolved. The snapshot refreshes
automatically about once a week — same as Slack's directory caches, checked once the IPC server comes
up on every daemon restart, in the background so a large account's re-sync can't delay startup
completing — and, in between restarts, lazily on first use once seven days have passed. This does
mean a daemon restart now connects to Telegram eagerly rather than waiting for the first Telegram tool
call, an accepted tradeoff now that the connection happens off the startup critical path. Reads no
message content; auto-approved.

### Salesforce

**Auth:** OAuth2 (browser sign-in via a Connected App). See [salesforce-setup.md](salesforce-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `salesforce_list_reports` | read | auto | — | — |
| `salesforce_get_record` | read | review | object type, record name, record ID | All field values |
| `salesforce_run_report` | read | review | report name, report ID | All report rows |
| `salesforce_search` | read | review | search term, object types, result count | One line per match: object type, name, id |

`salesforce_search` is the same mechanism (SOSL) behind the search bar at the top of the
Salesforce UI — search by name or id across one or more object types, optionally scoped to one
Account's related records (`account_id`, requires `object_types` to be set). Results are
lightweight Id/Name matches, not full records — call `salesforce_get_record` for full field
details on a match, the same search-then-drill-in split `jira_search_issues`/`jira_get_issue`
already use.

### Jira

**Auth:** OAuth2 (browser sign-in, Atlassian 3LO). Shared with Confluence — one sign-in covers both. See [atlassian-setup.md](atlassian-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `jira_list_projects` | read | auto | — | — |
| `jira_search_issues` | read | auto | — | — |
| `jira_get_issue` | read | review | project name, key, summary, status, assignee | Description, comments, all fields |
| `jira_get_transitions` | read | auto | — | — |
| `jira_create_issue` | write | popup | — | Project, type, summary, full description |
| `jira_add_comment` | write | popup | — | Issue key + summary, full comment |
| `jira_update_issue` | write | popup | — | Issue key + summary, fields (old → new), including custom fields |
| `jira_transition_issue` | write | popup | — | Issue key + summary, status (old → new) |

`jira_update_issue`'s `custom_fields` parameter takes a JSON object keyed by each custom field's
**display name** exactly as shown in the Jira UI (e.g. `{"Story Points": 5}`) — never the internal
`customfield_NNNNN` id. The connector resolves the name via Jira's field metadata and shapes the
value for select-list (single- and multi-option) fields automatically; fields needing a structured
reference the name alone can't supply (e.g. a user-picker field, which needs an `accountId`) are
passed through as-is and surface Jira's own validation error if the shape is wrong.
`jira_transition_issue` moves an issue by transition name (e.g. "Done") — call
`jira_get_transitions` first to see which names are valid from the issue's current status.

### Confluence

**Auth:** OAuth2 (browser sign-in, Atlassian 3LO), shared with Jira — one sign-in covers both. See [atlassian-setup.md](atlassian-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `confluence_list_spaces` | read | auto | — | — |
| `confluence_search` | read | auto | — | — |
| `confluence_cql_search` | read | auto | — | — |
| `confluence_list_pages` | read | auto | — | — |
| `confluence_get_page` | read | review | title, space, author, last modified | Full page body |
| `confluence_get_page_by_title` | read | review | title, space, author, last modified | Full page body |
| `confluence_list_attachments` | read | auto | — | — |
| `confluence_download_attachment` | read | review | title, space, attachment name, type, size, save path | — |
| `confluence_create_page` | write | popup | — | Space, title, parent page, full body |
| `confluence_update_page` | write | popup | — | Title, space, full new body |

### Google Tasks

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `tasks_list_task_lists` | read | auto | — | — |
| `tasks_list_tasks` | read | auto | — | — |
| `tasks_get_task` | read | auto | — | — |
| `tasks_create_task` | write | popup | — | Task list, title, due date, full notes |
| `tasks_update_task` | write | popup | — | Task list, task, new title/due date, full notes |
| `tasks_complete_task` | write | popup | — | Task list, task |
| `tasks_uncomplete_task` | write | popup | — | Task list, task |
| `tasks_move_task` | write | popup | — | Task, from list, to list |

### Apps Script

**Auth:** OAuth2. Reads/writes script *source* only — PrivacyFence never runs a script. There is
deliberately no execute/run tool: see issue #154's "Non-goals" and `apps_script_client.py`'s
module docstring. The user runs a script themselves in the Apps Script editor (or via its own
triggers), under their own Google account, through Apps Script's own separate consent screen —
untouched by PrivacyFence.

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `apps_script_list_projects` | read | auto | — | — |
| `apps_script_get_content` | read | review | project name, file count | Full source of every file |
| `apps_script_write_content` | write | popup | — | Project name, file names/types, full new source of every file |
| `apps_script_get_execution_log` | read | review | project name, execution count | Function, status, start time, duration per recent run |

`apps_script_get_execution_log` surfaces the result of a run the **user** triggered outside
PrivacyFence (via the Processes API's `listScriptProcesses`) — status/duration/which function ran,
not a live `console.log` transcript; see `apps_script_client.py`'s module docstring for why.
`apps_script_write_content` always replaces a project's entire file set (there is no
single-file/partial update in the underlying API), the same "show full resulting content, not a
diff" precedent `drive_write_doc_content` set. All three Apps Script tools above (`apps_script_
get_content`/`write_content`/`get_execution_log`) were previously ungovernable — no auto-accept rule
could be configured for them at all, on any surface (the redesign proposal's F5). The policy v2
redesign's P6 makes them configurable, by script id, from PrivacyFence Settings' **Auto-accept**
page's "Apps Script — project" scope (`policy.scopes.NEW_SCOPE_SELECTORS["apps_script.project"]`) —
there is still no `auto_accept_rules`/`auto_accept_grants` (v1) equivalent, since v1 never had a
predicate for this at all.

### The `auto` tier, across all connectors

The tables above gate 42 tools `auto` — allowed to proceed with no human in the loop, but still
recorded in the audit log as `auto_accepted` (see
[Audit integrity and forwarding](security-and-compliance.md#audit-integrity-and-forwarding): the
`auto` gate is a logged, IT-and-user-configured exception, never a default absence of control).
This section gives that tier its own documented view rather than leaving it implicit across eleven
separate per-connector tables.

**What qualifies a tool for `auto`, as a rule rather than a case-by-case judgment call:** every tool
below is either (a) a listing/metadata operation whose result names *what exists* (message subjects
lists, channel names, calendar names, project keys) without returning a message body, document
content, or any other free-text personal data, or (b) a narrow administrative action with no data
disclosure of its own (creating a blank spreadsheet, refreshing a name-resolution cache). Nothing
that returns full message/document/record content is ever `auto` — that boundary is what keeps this
tier's risk bounded regardless of how many tools sit in it; see
[claude-knowledge-boundary.md](claude-knowledge-boundary.md) for the exact fields each `review`-gated
tool discloses once a human does approve it.

| Connector | Auto tools | Count |
|---|---|---|
| Gmail | `gmail_list_messages`, `gmail_list_threads`, `gmail_list_message_attachments`, `gmail_list_filters`, `gmail_list_labels` | 5 |
| Google Drive (incl. Sheets) | `drive_list_files`, `drive_get_file_metadata`, `drive_list_folder`, `drive_list_shared_drives`, `drive_create_blank_file`, `drive_sheets_create`, `drive_sheets_get_metadata` | 7 |
| Slack | `slack_list_channels`, `slack_list_dms`, `slack_list_group_chats`, `slack_resolve_permalink`, `slack_refresh_user_cache`, `slack_refresh_channel_cache` | 6 |
| Google Calendar | `calendar_list_calendars`, `calendar_list_events`, `calendar_get_free_busy`, `calendar_list_rooms`, `calendar_get_event_visibility`, `calendar_list_colors` | 6 |
| Google Contacts | `contacts_list`, `contacts_search`, `contacts_get` | 3 |
| Telegram | `telegram_list_chats`, `telegram_refresh_chat_cache` | 2 |
| Salesforce | `salesforce_list_reports` | 1 |
| Jira | `jira_list_projects`, `jira_search_issues`, `jira_get_transitions` | 3 |
| Confluence | `confluence_list_spaces`, `confluence_search`, `confluence_cql_search`, `confluence_list_pages`, `confluence_list_attachments` | 5 |
| Google Tasks | `tasks_list_task_lists`, `tasks_list_tasks`, `tasks_get_task` | 3 |
| Apps Script | `apps_script_list_projects` | 1 |
| **Total** | | **42** |

A few things worth calling out explicitly about this tier as a whole, rather than tool by tool:

- **`auto` is not "unauditable" or "silent."** Every one of these 41 calls still writes an
  `auto_accepted` entry to the same hash-chained audit log a `review`/`popup` decision writes to
  (`audit_log.py`) — the difference from `review`/`popup` is *when* the call proceeds (immediately,
  vs. after a human decision), not *whether* it's recorded.
- **`auto` here means "IT and the review model decided this category is safe by design," not
  "unconfigurable."** Nothing in this tier can be moved to `review`/`popup` by a user today — the
  gate each tool goes through is fixed in code (`auto_accept.py`'s `TOOL_TO_GATE`, the single source
  of truth the connector tables above are checked against —
  `tests/unit/connectors/test_readme_manifest_alignment.py`), not a `settings.yaml` setting, so an
  organization that wants a *narrower* `auto` tier than what ships today has no way to configure
  that yet; this is a real, current limitation worth naming rather than leaving implicit.
- **Two tools carry a search/filter parameter that narrows results without changing their gate.**
  `slack_list_channels`/`slack_list_group_chats`/`slack_list_dms`'s `participant` matching and
  `calendar_get_free_busy`'s access-fallback behavior (`calendar.free_busy_full_event_details`) both
  still return only metadata-shaped results — narrowing *what's listed* never crosses into returning
  message/document content, so neither changes the `auto` classification.
- **Some `auto` tools are themselves prerequisites for a later `review`-gated call**, not endpoints
  in their own right — e.g. `gmail_list_messages` (auto) is how a message id reaches
  `gmail_get_message` (review); the list operation discloses subjects/senders/dates but never a body,
  and the body-returning call is exactly where the gate steps up. See
  [claude-knowledge-boundary.md](claude-knowledge-boundary.md) for this pattern worked through in
  detail across every connector.

---

## Auto-accept

Routine, low-risk requests can be approved automatically, skipping the human review gate. Every
auto-accept rule is a **scope** (which resources it trusts) plus a set of **verbs** (what may be
done to them), optionally narrowed by **conditions** (a property of the request that must also
hold) — one grammar, one config section (`auto_accept:` in `config/settings.yaml`), written
identically by all three surfaces that can create a rule: the approval popup's own **Always
allow** button, PrivacyFence Settings' **Auto-accept** page, and the MCP bridge's
`privacyfence_propose_policy_change`.

Through 4.1, trusting a resource meant two different, overlapping config models — a
per-operation `auto_accept_rules` section and a resource-scoped `auto_accept_grants` section, each
writable from different surfaces with different width. Both are gone as anything live writes or
evaluates: every rule, however it was created, now lives in one place, described the same way
everywhere it's shown. See [Migration from v1](#migration-from-v1) below for what happens to an
existing hand-edited config.

### The `auto_accept:` schema

```yaml
auto_accept:
  version: 2
  rules:
    - id: r-3f9a1c2b8e                # stable; content-derived, never reused
      predicate: approved_sandbox_folder
      value: ["1CdeFghIJKLmnoPQRstuVWxyz0123456789AbCdEfGh"]
      operations: [drive.write_file, sheets.write_range, docs.edit_content]
      conditions: [[shared_drive_exclusion, null]]
```

`id` is derived from `(predicate, value, conditions)` — the same triple always mints the same id
regardless of which operations carry it or how many times a config is (re-)migrated, so a rule's
identity in the audit log and in Settings doesn't churn on every restart. `predicate` is the scope
selector (see [Scope catalogue](#scope-catalogue) below); `operations` is the engine's own internal
address space — the set of connector operation keys this rule governs — never shown to a user
directly, since [Verb catalogue](#verb-catalogue) is what every surface renders instead.

A rule set is always a **union**: a call auto-accepts if any rule's scope matches, one of its
`operations` is the operation being gated, and every one of its `conditions` holds. More rows
always means more allowed, never less — there is no rule that narrows what another rule already
allows. `effect` is reserved for a future `deny` value; only `allow` exists today.

The loader fails closed on anything it doesn't recognize: an unknown `predicate`, an operation key
no scope type can govern, or a malformed `conditions` entry never matches — it's dropped from
evaluation and surfaced in Settings as a rule PrivacyFence doesn't understand, never treated as
"matches everything." A missing or empty `auto_accept:` section means no auto-accept at all, not
universal auto-accept.

### Scope catalogue

Twenty-five scope types. **Identity** scopes name a specific resource (a folder id, a project
key); **attribute** scopes name a property that selects a set of resources (a sender's domain, "I
own it").

| Scope type | Kind | Value | Predicate(s) |
|---|---|---|---|
| `drive.folder` | identity | folder id | `approved_folder`, `approved_sandbox_folder`, `parent_folder_allowlist`, `move_within_approved_folders` |
| `drive.file_type` | attribute | MIME types | `file_type_allowlist` |
| `drive.owned_by_me` | attribute | — | `i_am_owner`, `created_by_me` |
| `drive.created_this_session` | attribute | — | `created_this_session` |
| `gmail.sender` | identity | addresses | `i_am_sender` |
| `gmail.sender_domain` | attribute | domains (+ subdomains) | `trusted_sender_domain` |
| `gmail.recipient` | identity | addresses | `to_is_myself`, `i_am_sole_recipient` |
| `gmail.recipient_domain` | attribute | domains | `approved_recipient_domain` |
| `gmail.label` | identity | label names | `label_match`, `label_name_allowlist` |
| `gmail.anything` | attribute | — | `always_allow` (drafting only — D4: an unconditional grant that reads as unconditional) |
| `slack.channel` | identity | channel/user ids | `approved_channel`, `approved_channel_all_results`, `approved_recipient`, `send_to_myself` |
| `slack.channel_kind` | attribute | `self_dm` · `group_dm` · `public` | `dm_with_myself`, `group_dm`, `public_channels_only` |
| `telegram.chat` | identity | chat ids | `approved_chats`, `approved_chats_all_results` |
| `calendar.calendar` | identity | calendar ids | `personal_calendar` |
| `calendar.organized_by_me` | attribute | — | `i_am_organizer` |
| `tasks.list` | identity | task list ids | `approved_task_list` |
| `jira.project` | identity | project keys | `approved_project_keys` |
| `jira.my_issues` | attribute | reporter · assignee | `i_am_reporter`, `i_am_assignee` |
| `confluence.space` | identity | space keys | `approved_space_keys` |
| `confluence.authored_by_me` | attribute | — | `i_am_author` |
| `salesforce.object_type` | attribute | object names | `approved_object_types` |
| `salesforce.report` | identity | report ids | `approved_report_ids` |
| `contacts.label` | identity | label names | `label_name_allowlist` |
| `apps_script.project` | identity | script ids | — (Settings/bridge only; see [note](#three-f5-operation-groups) below) |

`drive.folder` matches on a file's **direct** parent only — a file one level down in a trusted
folder does not match (not recursive).

### Conditions

Ten predicates that narrow a scope further, never widen one — attached under a rule's `when:`
key. Every one of them is `data_dependent` (needs the fetched item, never just the call's
arguments), which is what makes `privacyfence_check_policy`'s three-way verdict
(`auto_accept`/`requires_review`/`unknown`) correct without a hand-maintained classification list.

| Condition | Holds when… |
|---|---|
| `older_than_days: N` | message is at least N days old |
| `within_days: N` | event starts within N days |
| `past_only` | event has already ended |
| `no_attachments` | no files or media attached |
| `no_external_attendees` | every attendee shares your domain |
| `no_conferencing_link` | event carries no meeting link |
| `not_private` | event visibility is not private |
| `not_shared_drive` | file is not in a shared drive |
| `no_contact_info_change` | edit touches no phone/email field |
| `in_existing_thread` | message is a reply, not a new post |

### Verb catalogue

Eighteen verbs in four risk families. A verb is what a surface actually renders and lets a user
choose — the operation key it compiles to internally never appears on screen.

| Verb | Family | Scope measured against |
|---|---|---|
| `read` | read | the item |
| `download` | read | the item |
| `search` | read | **every** result |
| `create` | write | the container |
| `update` | write | the item |
| `format` | write | the item |
| `restructure` | write | the item |
| `comment` | write | the item |
| `label` | write | the label being applied |
| `move` | write | **both** source and destination |
| `archive` | write | the item |
| `complete` | write | the list |
| `transition` | write | the project |
| `configure` | write | the account |
| `send` | send | the destination |
| `draft` | send | **every** recipient |
| `share` | send | the audience |
| `delete` | destructive | the item |

A rule always stores its verbs expanded, never as "every verb in this family" — a future verb
added to a family is never retroactively granted by a rule written before it existed. A
multi-result operation (`search`) is checked against **every** item a call actually returned, not
a single argument; a `move` is checked against **both** the source and the destination, so a
trusted-folder rule can never be used to move a file *out* of the folder it trusts.

<a id="three-f5-operation-groups"></a>Three operation groups have no resource identity a popup
could derive a proposal from, so Settings and the bridge are the only way to configure them: Apps
Script's tools (scoped by `apps_script.project`, a real identity scope — just not one any call's
own arguments suggest a value for), Gmail's two filter tools (`gmail.anything`, since a filter's
criteria/action combination is too open-ended for identity scoping and an unconditional rule here
is exactly the risk worth surfacing rather than hiding), and Slack's group-chat creation (no scope
type measures "the audience", so it stays ungated by resource identity, gated only by verb).

### The three surfaces

**The approval popup** — clicking **Always allow** proposes the narrowest rule that covers the
item just reviewed: one scope, the single verb just gated, nothing wider. The confirmation dialog
then offers further verbs as named widening chips (e.g. "also allow format · comment ·
restructure (4 tools)") before anything is written — the width is shown and chosen, never implied
by a boolean. Where more than one scope plausibly contains the same item (a file you own that's
also in an approved folder, say), the popup renders one button per candidate instead of picking
one for you.

**PrivacyFence Settings' Auto-accept page** — one filterable list across every connector, each
rule rendered as the sentence it is (*"Drive · folder 'Claude scratch space' — allow read,
update, format, comment · not in a shared drive"*), with an **Unblocks N tools** disclosure
listing exactly which tools it covers, verb chips colour-coded by family so a rule carrying
`delete` or a send-family verb is visible without reading closely, and a connector/scope-type/verb
filter bar. An **Add a rule** form picks a scope, an optional value, and which verbs to allow.
**✕ Remove** deletes a rule entirely — narrowing an existing rule is always remove-and-re-add-
narrower, matching the model's own additive-only design, never an in-place edit. Once a rule has
matched at least once, the page also shows a match count and last-matched date, and offers to
remove a rule that's never matched.

**The MCP bridge** — `privacyfence_list_policy` lists every configured rule (with its stable id,
sentence, and covered tools) plus the scope catalogue `privacyfence_propose_policy_change`
validates a submission against; `privacyfence_propose_policy_change` adds, updates, or removes a
rule, blocking on the same confirmation dialog the popup's Always-allow uses. A verb a scope type
cannot govern, or a value-needing scope submitted with none, is rejected before any popup is shown
— a rule that could never be rendered or removed by any surface is refused at write time rather
than silently persisted. `privacyfence_check_policy` predicts a call's verdict ahead of time and
returns `matched_rule_id`, so a planning agent can say *why* something will auto-accept.

The two pre-redesign bridge tools (`privacyfence_list_auto_accept_rules`,
`privacyfence_propose_auto_accept_rule_change`) are kept as deprecated aliases: the list tool now
returns the identical v2 listing `privacyfence_list_policy` does, and the propose tool translates
its older `target: "rule" | "grant"` shape into the same v2 rule the new tool would create.

Org mode's own per-principal settings page (`web/routes_org_settings.py`) writes the identical
schema through the identical primitives (`auto_accept.add_policy_v2_rules`/`remove_policy_v2_rule`)
— a signed-in principal manages their own rules from `/settings` the same way local mode's Auto-
accept page does, scoped to `current_principal()` throughout so an admin has no more mutation
power over another principal's rules than that principal does.

### Related but distinct mechanisms

**Temp-accept grace window** — an in-memory, non-persisted acceptance for six `popup`-gate writes
expected to fire repeatedly against the same file in a burst (`drive_sheets_write_range`,
`drive_sheets_format_range`, `drive_sheets_insert_dimensions`, `drive_add_comment`,
`drive_docs_edit_content`, `drive_docs_format_content` —
`auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS`), scoped to one file/spreadsheet for 5 minutes and
gone on daemon restart. There's no separate button for it: these six popups show only Deny / Allow
once, with a plain disclosure caption explaining that Allow once also arms the grace window.
Deliberately *not* offered on `drive_sheets_delete_dimensions` (no undo path) or on
`drive_sheets_add_sheet`/`drive_sheets_rename_sheet` (one-shot per file, not called in a burst) —
those get a plain Deny/Allow once with no caption at all.

### Migration from v1

A not-yet-migrated, hand-edited `settings.yaml`'s `auto_accept_rules`/`auto_accept_grants`
sections are folded into the v2 `auto_accept:` section once, automatically, the next time the
daemon starts (`policy.compat.migrate_to_policy_v2`, run from `daemon_main.run_app` for the local
principal and from `daemon_main._load_principal_settings` for every org principal). Migration is
provably behaviour-preserving: it can only ever produce the *exact* rule set the old two-model
config would have evaluated, backed by an equivalence harness that checks every predicate and
every fixture against the pre-redesign implementation. A migration that actually changed anything
backs up the pre-migration file to `settings.yaml.bak` first and logs a summary naming every rule
whose expansion carries a destructive or send-family verb — surfacing what a grant's boolean used
to hide rather than silently dropping it.

`auto_accept_rules`/`auto_accept_grants` are never deleted or written to again after migration —
they stay on disk, readable, for reference on a hand-edited install. Nothing evaluates them
directly anymore; the migrated v2 section is the only thing any surface reads or writes going
forward.

---

## Web surfaces (`/approvals`, `/settings`)

Every approval card and the settings page are served over the embedded web server (`web/server.py`). This used to run alongside a native macOS menu bar/approval dialogs/settings window; that native UI layer was later deleted entirely ("two approval surfaces means two places for a security fix to land"), so the web surface is now the only one, on every platform this daemon runs on. Two config keys under `web:` in `settings.yaml`:

- `web.mcp.enabled: true` (default) — turns on the `/mcp` Streamable HTTP endpoint Claude talks to.
- `web.settings.enabled: true` (default) — turns on `GET /settings` and its `POST /api/settings/{action}` dispatcher. `web.settings.allow_quit` (default `true`) gates whether the About page's Quit button works from a browser at all — always behind an in-page confirmation either way. The approval surface itself (`/approvals`) has no such switch — P10 is the phase with no rollback, since it deleted the fallback.

`GET /settings/connectors` (issue #396 Part C) serves the identical document with its Connectors section pre-selected server-side, instead of the client-side JS's own `general` default — the one deliberate exception to that page's nav state otherwise being purely client-side (see `settings_window_html.py`'s own module docstring). It is where the companion's own Open Settings item lands a human, and where `privacyfence_status` tells a model to send one, so they arrive at the screen that actually unblocks the install rather than `/settings`'s own General page. That page also renders a short, dismissible welcome banner (client-side, `renderWelcomeBanner` in `settings_window_html.py`) whenever no connector is authenticated yet.

Both pages share one origin, one session (the same local `pf_session` cookie), and one shared chrome (`web_shell.py`): a header with Approvals/Settings navigation and a live-connection indicator bound to `GET /api/state/stream` — one SSE channel carrying both a `settings` event (`SettingsController.snapshot()`, pushed the moment something changes it from anywhere — a rule edited over MCP, a background OAuth flow finishing) and an `approvals` event (the pending-approval list), so an open tab never needs a manual refresh.

`/settings`'s own action dispatcher is an **explicit allowlist** — an unlisted or misspelled action name is a 404 before any lookup happens at all, and every argument is validated against the controller method's own type annotations (a bad `idx` is a 400, not a 500). Three actions that don't fit "POST an action, get a snapshot back" get their own routes instead: uploading an organization config bundle (multipart, JSON/`version`-validated, written `0600`), downloading the current week's audit log export (`Content-Disposition: attachment`), and an in-page "update available" banner (Download/Remind Me Later/Skip). Since Phase 3 of the self-approval review those bespoke routes are classified rather than merely present: `build_routes` itself refuses to start unless every route it mounts appears in `_BESPOKE_SENSITIVE_ROUTE_PATHS` (today, the bundle upload — which can rewrite the PII policy, every auto-accept rule and every connector's OAuth client config at once) or in `_BESPOKE_EXEMPT_ROUTE_PATHS` with a written reason. A new bespoke route cannot slip the sensitive-action net by existing, which is how `org_config_upload` did.

The `/approvals` list (`docs/approval-list-ui-ux.md`) shows every currently-pending card as its own row — connector icon, title, a relative timestamp, a **Deny** button right on the row, and a **Review →** link to the full card at `/approvals/{id}`. There is deliberately no **Allow** on the row: denying without reading the card can't leak anything, and putting an "Allow" button on a one-line summary is exactly the habituation failure the full card exists to prevent. Deciding a card navigates back to the list (not a dead "close this tab" page) with a toast saying what happened, including the 409 case where a rule created elsewhere already resolved it first.

What that decide route requires before it releases anything — a passkey, an attributable session, and which results each applies to — is [`security-and-compliance.md`](security-and-compliance.md)'s subject, not this file's.

## Privacy filtering and PII

Privacy filtering runs before protected content is released to the MCP client. Organization policy can allow, redact, or block configured PII categories. Invalid configured policy values fail closed at startup/config validation rather than silently becoming permissive.

The PII detector and privacy filter are implemented in `pii_detector.py` and `privacy_filter.py`. Tool-specific preview/extraction limits are documented in [`file-type-support.md`](file-type-support.md).

## Audit logging

PrivacyFence records tool/gate decisions to the audit log, including the connector/tool, decision, request metadata, and principal information where applicable. Security-sensitive audit integrity/forwarding behavior is implemented in the audit modules and described in [`security-and-compliance.md`](security-and-compliance.md).

Treat the audit log as security-relevant state: protect its directory, include it in operational backup decisions where required, and do not expose it through connector content paths.

## Web authentication and CSRF

Local browser sessions are established from the one-time bootstrap exchange and then represented by the HttpOnly session cookie. Mutating browser requests require same-origin/session checks plus the CSRF value carried by the page/request flow.

CSP nonces are generated and applied to the inline scripts/styles required by the rendered application. Security headers are applied by the web-server middleware.

Org-mode routes use the org session/identity machinery and apply principal-aware authorization rather than the local single-user session model.

## Browser notifications

The shared web shell maintains the state stream, pending-approval count, and optional browser notifications. Notification detail is controlled by the web notification configuration:

- `minimal` exposes only a pending-count style notification;
- `standard` can include safe operation metadata such as connector/direction;
- `detailed` may include the approval summary and therefore can expose gated content in the OS/browser notification surface.

Notification permission is requested only after a user interaction/decision path, not automatically on initial page load.

## Connector lifecycle

Local mode builds one `ConnectorHost` at daemon startup. Org mode uses `ConnectorRegistry` to build connector sets lazily per principal and evict idle hosts.

Connector setup documentation:

- [`google-cloud-setup.md`](google-cloud-setup.md)
- [`slack-setup.md`](slack-setup.md)
- [`salesforce-setup.md`](salesforce-setup.md)
- [`atlassian-setup.md`](atlassian-setup.md)
- [`telegram-setup.md`](telegram-setup.md)

The definitive tool surface lives in `src/privacyfence/connectors/` and the connector registry/daemon construction code.

## File and download handling

PrivacyFence extracts/normalizes supported attachment types for preview and PII inspection using the bounded extraction paths documented in [`file-type-support.md`](file-type-support.md).

Local-mode downloads can be written on the user's machine. Org-mode downloads are delivered inline or through encrypted short-lived staged downloads as documented in [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Configuration

`config/settings.yaml` (and the packaged example under resources) defines local/web/connector behavior. Org deployments additionally use the signed/validated organization configuration bundle and org-specific identity/settings.

Configuration that affects security boundaries is validated strictly; invalid values should stop startup rather than silently widen access.

## Installation and packaging

Current packaging paths are documented in [`platform-support.md`](platform-support.md):

- macOS signed/notarized DMG, carrying the `.pkg` installer and the `.mcpb`;
- Windows Inno Setup installer;
- Debian/Ubuntu self-contained `.deb` for local desktop mode;
- Python package/system-service path for Linux/server deployments.

### Windows

`installer/privacyfence.iss` (built by `scripts/build_installer.ps1`) installs the PyInstaller
onedir output under `%ProgramFiles%\PrivacyFence\`, the bundled `.mcpb` alongside it, and a Start
Menu entry pointing at the embedded web settings UI rather than at the daemon executable directly.
The installer requires admin elevation (`PrivilegesRequired=admin`) — it used to allow a
per-user-writable install without elevation (`PrivilegesRequired=lowest`), but that path could
never register the Task Scheduler autostart task below at all: `schtasks /create /xml` registering
a task with a `LogonTrigger` needs the `SeCreateGlobalPrivilege` user right, which a non-elevated
token lacks regardless of the task's principal (see `platform-support.md`'s "Known open items").

Autostart is a Task Scheduler task (`PrivacyFence`), not a Startup-folder shortcut, registered from
`installer/privacyfence.iss`'s `[Code]` section (`CurStepChanged(ssPostInstall)` calling
`RegisterAutostartTask`) rather than a plain `[Run]` entry, and removed by the uninstaller's
`[UninstallRun]` section (`schtasks /delete`) — visible and removable through normal Windows
install/uninstall UI, the same way the macOS LaunchAgent plist and the Linux `.deb`'s XDG autostart
entry are. Registration is a real Task Scheduler XML task definition
(`installer/privacyfence-task.xml.tmpl`, extracted at install time, `__EXEC_PATH__` substituted for
the real installed path, registered via `schtasks /create /xml`), not plain `schtasks /create` CLI
flags — an earlier CLI-flag-only version of this mechanism shipped briefly with two real bugs
(invalid `/ri`/`/du` flags for an `ONLOGON` schedule, then a trigger scoped to only the installing
account), both superseded by this XML-based rewrite rather than patched in place; see that
template's own header comment and `platform-support.md`'s "Known open items" for the full history.
`<LogonTrigger>` with no `<UserId>` fires for any interactive logon, `<Principal>` uses `GroupId`
(`Builtin\Users`) rather than a specific account so the task runs as whichever user just signed in,
in their own session, at the non-elevated `LeastPrivilege` run level.
`<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>` was first added as
parity with the macOS LaunchAgent's `KeepAlive`/`SuccessfulExit=false` and the Linux `.deb`'s systemd
restart policy, but **does not actually restart a crashed daemon** — Task Scheduler logs an action
that ran and then died as a successfully completed task, so the setting never engages for that case
(measured, with the event-log evidence, in `platform-support.md`'s "Known open items"). It stays in
the definition anyway, for the narrower thing it still does: a faster (`PT1M`) retry of a launch
failure right at logon. Real crash-restart is a second trigger,
`<TimeTrigger><StartBoundary>2020-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>
<Repetition><Interval>PT5M</Interval></Repetition></TimeTrigger>`, alongside the `<LogonTrigger>`: a
past `StartBoundary` and an indefinite `<Repetition>` make it live immediately rather than waiting for
a sign-in, and every tick relaunches the daemon (a tick that finds one already running exits at once,
via the single-instance lock) — `daemon_main.run_app()` logs that case at INFO and exits `0` rather
than ERROR/`1`, so Task Scheduler logs a clean success on every ordinary tick. `<DisallowStartIfOnBatteries>` and
`<StopIfGoingOnBatteries>` are both set to `false`, inverting Task Scheduler's own defaults: left at
the defaults, a laptop on battery power would not start PrivacyFence at sign-in and would stop it
when unplugged — a privacy gate that quietly isn't running, with the MCP client simply finding no
daemon. This closes the crash-restart gap in Windows autostart — measured, not assumed, on a real
`windows-latest` runner: killing the
Scheduler-started daemon produces a new pid, under the same signed-in account, before the
`<TimeTrigger>`'s own next tick would otherwise be due. See
[`platform-support.md`](platform-support.md)'s "Known open items" for this mechanism's current
verification status. In short: `windows-graphical-session.yml` verifies the definition Task
Scheduler itself stored, that Task Scheduler really starts the daemon for an account that installed
nothing, and that it really relaunches it after a crash; the `<LogonTrigger>`'s own firing is a
human check on a real machine (`release-testing.md`), because a hosted runner cannot produce the
Terminal Services session logon the trigger subscribes to.

Per-user state (credentials, settings, the audit log) lives under `%LOCALAPPDATA%\PrivacyFence\`
(`paths.py`'s `data_dir()`, via its `_windows_data_dir()` branch — not the same `~/.privacyfence`
dotfile POSIX uses reused verbatim under `%USERPROFILE%`, since a dot-prefixed name isn't a hiding
convention Explorer honors the way it is on POSIX; `%LOCALAPPDATA%` rather than the Roaming
`%APPDATA%` because this directory holds credentials and audit logs that shouldn't follow a roaming
profile across machines), created by the app on first run — the installer never touches it, and
uninstalling removes only the program files, the scheduled task(s) and, if one exists, the
privilege-separation service.

**That layout describes a source checkout or `pip`/`pipx` run.** On every packaged Windows install
— where the installer runs privilege separation itself as a post-install step, and where a daemon
that finds itself unseparated refuses to serve ([ADR 0003](adr/0003-separated-installs-only.md)
decisions 4 and 6, see `platform-support.md`'s Windows section) — that state lives at
`%ProgramData%\PrivacyFence\` under the `NT SERVICE\PrivacyFence` virtual account instead, the
daemon is a Windows service rather than the Scheduled Task above (which is left registered but
disabled), and a second task starts the companion tray app in each user session. Uninstall leaves
`%ProgramData%\PrivacyFence\` in place exactly as it leaves `%LOCALAPPDATA%\PrivacyFence\`, which
on a separated install means a directory no ordinary account can read afterwards — so
`privilege-separation.ps1 disable` before uninstalling is the documented order.

**File-permissions caveat, accepted for v1**: elsewhere on this codebase, credential/token files are
written with `chmod(0o600/0o700)` to lock them down to the owning user. On Windows, `chmod` is a
silent no-op — there is no POSIX permission bit to set — so those files rely on the default NTFS ACLs
a per-user Windows profile already has (restricted to that user and Administrators) rather than an
explicit lock-down step. This is a deliberate, accepted gap, not an oversight: a single-user Windows
profile's own default ACLs already provide the same practical protection the `chmod` calls give on
POSIX.

**#428 Phase 4 is the security review that found the default insufficient — for one specific
reason, and it does not generalize.** The profile's own ACLs protect that data from *other accounts
on the machine*, which was always the threat this caveat was written against, and they still do.
What they cannot do is protect it from a process running *as that same user*, which is exactly what
the AI client is. Privilege separation moves the data out of the profile to `%ProgramData%`
precisely because a service account cannot own something inside a human's profile, and at that
point the profile's default ACLs protect nothing at all — so that layout is explicit `icacls`
grants, written by `scripts/windows_privilege_separation.ps1` and audited on every daemon start by
`src/privacyfence/windows_acl.py` (which uses `pywin32`, already a Windows dependency for the
control channel's named pipes). On an unseparated install — a source checkout, a `pip`/`pipx` run, or a
packaged build started with `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1`, never a packaged build
otherwise — everything in the paragraph above is unchanged: no `icacls`, no ACL code in the write
path, the profile's defaults as before.

### Linux

Two distinct install paths, not one — see `platform-support.md`'s support matrix for the full
comparison:

- **Local desktop mode**: a self-contained `.deb` (`PrivacyFenceApp.linux.spec`,
  `scripts/build_deb.sh`, `debian/`) installing the PyInstaller onedir output under
  `/opt/privacyfence`, exposing `/usr/bin/privacyfence-app`, and registering an XDG autostart entry
  under `/etc/xdg/autostart/` — the Linux analogue of the macOS LaunchAgent/Windows Task Scheduler
  task. Package removal does not delete per-user state from the home directory. Currently `amd64`
  only; `arm64` is a deliberate, undecided follow-up rather than a gap (see `platform-support.md`'s
  "Architecture and CPU constraints").
- **Org mode / server deployments**: `pip`/`pipx install privacyfence`, walked end to end by
  [`org-mode-setup-guide.md`](org-mode-setup-guide.md) (dedicated system user, OIDC registration, org
  config bundle, reverse proxy, a **system** systemd unit distinct from the repo-root
  `privacyfence.service` below), or the repo-root `privacyfence.service` — a systemd **`--user`** unit
  for a single-user Linux desktop install via the same `pip`/`pipx` path, requiring
  `loginctl enable-linger` or a graphical session to autostart at login the way the `.deb`'s XDG entry
  does. Nothing in `src/privacyfence/` imports a platform-specific module any more, and the full suite
  runs headlessly on Linux CI on every PR (`org-mode-smoke` exercises the daemon's own
  startup/authz/audit contract against a real subprocess and a mocked IdP); both paths have also now
  been run end to end against a real install, including a live third-party IdP's actual OIDC
  round-trip. See [`platform-support.md`](platform-support.md) for current status.

## Testing

[`testing-policy.md`](testing-policy.md) describes the checks that currently run, the seven-layer taxonomy they map to, and what deliberately stays manual. Standing open items that aren't phase-shaped work live in [`platform-support.md`](platform-support.md)'s "Known open items".

The source files, tests, workflow definitions, build scripts, and configuration examples are authoritative if this reference drifts.
