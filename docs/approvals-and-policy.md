# Approvals and policy

How PrivacyFence decides whether an AI system's request runs, waits for you, or is refused — and
every control you have over that: the approvals list, the approval card, the PII check,
always-allow rules, the privacy filter, and notifications.

Appendices:

- [Tools reference](tools-reference.md) — every connector tool and its gate.
- ["Always allow" per-tool reference](always-allow-rules-reference.md) — which always-allow buttons
  each tool's card can offer.
- [PII detection keywords](pii-detection-keywords.md) — exactly what the PII check matches.

Every `settings.yaml` key mentioned here is described in full in the
[configuration reference](configuration-reference.md).

---

## How requests are gated

Every connector tool has one of three gates:

| Gate | Used for | What happens |
|---|---|---|
| `auto` | Low-risk lookups: listing, searching metadata, free/busy | Runs straight away. Still passes through the [privacy filter](#privacy-filter) and is written to the audit log |
| `review` | Reads that release content (an email body, a document, a page) | PrivacyFence fetches the content, shows it to you on a card, and releases it only if you approve |
| `popup` | Writes (drafts, messages, edits, moves, deletes) | PrivacyFence shows exactly what it is about to do and does it only if you confirm |

[Tools reference](tools-reference.md) lists the gate of every tool.

A `review` or `popup` request is decided in this order:

1. **An always-allow rule matches** — the request runs without a card and the audit log records
   which rule matched. A read with a [PII match](#the-pii-check) is the exception: it always goes
   to a card.
2. **A same-file grace window is open** — some Drive writes, see
   [below](#same-file-grace-window).
3. **Otherwise it becomes a pending approval** — a card on your [approvals list](#the-approvals-list).

### The 30-second wait and `approval_pending`

When a request needs you, PrivacyFence holds the AI system's call open for up to **30 seconds**
(`web.approvals.hold_window_seconds`). Decide within that time and the call simply completes.

If you don't, the call returns a result with `"status": "approval_pending"`, the approval's id, a
link to the card, and an `expires_at` time. The AI system is told to send you that link and then
call `privacyfence_await_approval` (or retry the same call later). Your decision is picked up when
the AI system repeats **the identical call** — same tool, same arguments. There is no tool that
fetches content by approval id: approved content only ever reaches the AI system through the
original call.

If you already have an approval waiting, a second one returns `approval_pending` immediately
instead of waiting another 30 seconds (`web.approvals.adaptive_hold`, on by default), and the
result points at the approvals list so you can decide them together.

| Limit | Default | Key under `web.approvals` |
|---|---|---|
| Wait before returning `approval_pending` | 30 seconds | `hold_window_seconds` |
| An undecided approval expires after | 15 minutes | `pending_ttl_seconds` |
| A decision can be collected by repeating the call within | 5 minutes | `ledger_ttl_seconds` |
| Pending approvals per person | 20 | `max_pending_per_principal` |
| Pending approvals in total | 50 | `max_pending` |

An approved **read** can be collected more than once within those 5 minutes. An approved **write**
is used up by the first repeat of the call, so it can never run twice on one approval.

In an unattended session (a scheduled task started with `privacyfence_begin_unattended_session`)
nobody is there to answer, so any request that would need a card is refused at once — see
[How it works](how-it-works.md).

---

## The approvals list

**Approvals** (`/approvals`) is the page you get when you open PrivacyFence (it opens through the
companion app on every platform). It lists every request waiting for you and updates live while
it's open. In an organization deployment each person sees only their own requests, and the
list is current as of when the page loaded (it does not update live).

The heading counts what is waiting, for example "4 approvals pending · 3 reads · 1 write". Each row
shows:

- the connector, the tool, and how long ago the request arrived;
- **who is asking** — the AI system's name, marked "not verified" unless it is verified (see
  [below](#who-is-asking));
- a title that says what the request is about ("Read email: Q3 budget"), with a read or write pill;
- three buttons, in this order: **Details**, **Review →**, **Deny**.

**Details** expands a short, metadata-only summary of the request (for example sender, file name,
recipients). **Review →** opens the full [card](#the-approval-card), which is the only place you can
approve a single request. **Deny** refuses it outright; there is no undo.

When nothing is waiting the page says "Nothing is waiting. PrivacyFence is watching." — or, if no
connector has been authenticated yet, "Nothing is governed yet." with a link to
**Settings → Connectors**.

### Deciding several at once

Rows are grouped by connector and operation. Each row that can be batched has a checkbox, and there
is a **Select all** per group and for the whole page. With rows selected:

- **Deny selected** denies them all. Denying never needs a passkey.
- **Approve selected** approves them all. Its label names what you are approving, for example
  "Approve 12 · 9 reads, 3 writes", so a write can't hide inside a batch of reads. If step-up
  applies to anything in the batch, one passkey prompt covers exactly the selected set — see
  [step-up](#step-up-passkey-on-approvals).

Approving a batch only ever shows you the **Details** summary of each item, not the full card. Use
**Review →** on any item you want to read in full first.

Two kinds of request have no checkbox and must be decided from their own card:

- a request the [PII check](#the-pii-check) flagged, because it needs a second confirmation;
- a follow-up confirmation dialog (for example "Confirm Auto-Accept Rule").

---

## The approval card

**Review →** opens the card at `/approvals/<id>`. After you decide, the browser returns to the
list with a short message saying what happened. A card that was already decided elsewhere, or has
expired, says it is no longer pending and links back to the list — it can't be decided twice.

### What's on a card

A **read** card shows, top to bottom:

- **Requested by** — who is asking ([below](#who-is-asking)).
- **The AI system's stated reason**, labelled "unverified": it is text the AI system wrote.
- **The risk section**, only when the PII check matched: the categories found. The matches are
  also highlighted where they sit in the preview.
- **What will be provided to the AI system** — one line per part of the result (for example "File
  metadata", "Document content") saying whether the AI system gets it in full, redacted, or not at
  all, as set by the [privacy filter](#privacy-filter).
- **The preview** — the content itself: the message, the document text, the table, an image, or
  the text extracted from a file (see [File previews](#file-previews)).
- **The frequency line** — "First time this week" or "Seen 4 times this week", counted from the
  audit log, so you notice when a request has quietly become routine.

A **write** card shows the stated reason, the action and its target (recipients, channel, file,
event, …), the content that will be written, and an **Effect** row: one sentence saying what the
write changes and whether it can be undone ("The message is posted and cannot be unsent.").

The buttons are **Deny**, **Allow once**, and — when a rule could cover this request — one or more
**Always allow** buttons (see [Always-allow and policy rules](#always-allow-and-policy-rules)).

### Keyboard

- **Esc** denies the request, wherever the focus is.
- **Enter** and **Space** never activate **Allow once**, so a stray keypress can't approve
  anything. They do activate a focused **Deny** or **Always allow** button; **Always allow** then
  opens a confirmation dialog, and in that dialog Enter can't confirm either (Esc cancels).

### Who is asking

Every card and list row names the AI system that made the request. How much to trust that name
depends on where it came from:

| Shown as | Meaning |
|---|---|
| The AI system's name and logo, with a **Verified** badge | Organization deployments only: an administrator pinned the OAuth client this request was made with to that AI system, on the **AI systems** settings page. The client could not have chosen this name itself |
| "Says it is …" with a **Not verified** badge | The name the AI system reported about itself, or a relabel set in `agent_overrides:`. PrivacyFence cannot confirm it |
| "Unrecognised AI system", with the claimed name in quotes if one was sent | The name matches no known AI system, or none was sent |

On a local install nothing is ever **Verified** — a local relabel changes the displayed name but
never makes it verified. When the name is not verified, the card's wording says "the AI system"
rather than borrowing the claimed name.

The name is for your information only. It never changes what is allowed: the same request gets the
same rule match and the same decision whoever claims to be asking. The audit log records the same
identity and where it came from (`agent_source`). See
[ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md),
[ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md) and
[ADR 0037](adr/0037-a-local-override-is-a-relabel-and-never-attests.md).

### Gmail signature on drafts

The Gmail draft tools can append your Gmail signature — the one Gmail stores for the sending
address. Turn it on for every draft with **Settings → Privacy Filter → Gmail → Append Gmail
signature to drafts** (`gmail.append_signature_to_drafts`, off by default); each call can override
it with `include_signature`.

The signature is fetched before the card is shown, so the card's preview shows the draft exactly as
it will be saved, signature included, and a **Signature** row says which address's signature is
appended (or that none is set). The informational PII note on write cards scans only the text the
AI system wrote, not your signature, which would otherwise flag nearly every draft because of your
own phone number. See [ADR 0038](adr/0038-gmail-draft-signature-is-shown-but-not-write-scanned.md).

---

## The PII check

Before a `review` card is shown, PrivacyFence scans the content being released for likely personal
data in Hungarian, English and German: IBANs, card numbers, national ID, tax and social-security
numbers, salary information, and similar. The exact patterns, and which part of each item is
scanned, are in [PII detection keywords](pii-detection-keywords.md). Email addresses and phone
numbers are deliberately not detected.

When something matches:

- **It overrides your rules.** Even if an always-allow rule covers the request, it goes to a card.
- **The card is tinted** and names the categories found, highlighting them in the preview.
- **Approving needs a second confirmation.** After **Allow once** (or **Always allow**) a
  "Possible PII Detected" dialog asks "Are you sure you want to proceed?". **Cancel** (or Esc)
  denies the request.
- **The audit log records the category names**, never the matched text.

**Re-reading your own content skips the second confirmation.** When a Drive file (read with
`drive_get_file_content`, `drive_sheets_get_values` or `drive_download_file`) was last written by
PrivacyFence itself and nobody has changed it since, you already saw that content on the write's
card, so the read is not re-confirmed. The moment anyone else edits the file, the next read is
checked normally. This is remembered only until PrivacyFence restarts.

**Writes** are not held by the PII check — they carry content the AI system wrote, not personal
data arriving from outside. A write card may show an informational note listing categories found in
the text, but it never asks for a second confirmation. The one exception is `drive_upload_file`:
an uploaded file can be something the AI system never read, so it gets the same scan and second
confirmation as a read.

Turn the check off, or turn off just IP addresses or currency amounts, in
**Settings → General → PII Detection Gate** (`pii_detection.enabled`,
`pii_detection.detect_ip_addresses`, `pii_detection.detect_financial_figures`; all on by default).
In an organization deployment these settings are install-wide and set by an administrator.

---

## Always-allow and policy rules

An always-allow rule lets matching requests run without a card. Every rule has three parts:

- a **scope** — which resources it trusts: one folder, one sender domain, "files I own", one
  Slack channel, …;
- the **operations** it covers — shown everywhere as verbs such as read, update, send;
- optional **conditions** that must also hold — "no external attendees", "no attachments", ….

A request runs without a card when any rule's scope contains the item, the rule covers the
operation, and every one of the rule's conditions holds. Rules only ever add permissions; no rule
takes away what another allows. With no rules, everything that is gated asks.

A fresh `settings.yaml` contains one rule: editing a Google contact runs without a card as long as
the edit changes no email address or phone number.

### Three ways to create a rule

**From a card — Always allow.** A card offers an **Always allow** button for **every** scope that
contains the item under review, each labelled with its scope ("Always allow — this folder",
"Always allow — if I own it"). A file you own that sits in a folder therefore shows two buttons;
pick the one you mean. Clicking one opens a "Confirm Auto-Accept Rule" dialog that states the rule
as a sentence and lists every tool it covers. **Confirm** writes the rule and approves this
request; **Cancel** approves this request once and writes nothing.

A rule created from a card covers the scope's value (that folder, that label, that calendar) and
only the operation you just approved — reading from a folder does not also allow writing to it.
Most write cards offer no Always allow button; the ones that do propose a rule scoped to the
resource the call touched. The exception is Gmail drafting, whose button writes an unconditional
rule: every future draft is created without a card. Which tool offers which buttons is listed in
the ["Always allow" per-tool reference](always-allow-rules-reference.md).

**From Settings — Auto-accept.** **Settings → Auto-accept** lists every rule as a sentence, with
colour-coded verb chips, an **Unblocks N tools** disclosure listing exactly which tools it covers,
how often it has matched ("Matched 42x, last 3 days ago", or "Never matched"), and **✕ Remove**.
You can filter by connector, verb family or text. **Add a rule** takes a scope, a value where the
scope needs one (comma-separated for several), and the verbs to allow. To narrow a rule, remove it
and add a narrower one; rules are not edited in place. When a passkey is required
(`step_up.require_passkey`), adding or removing a rule asks for it (see
[Security and compliance](security-and-compliance.md)).

**From the AI system — `privacyfence_propose_policy_change`.** The AI system can propose adding,
replacing or removing a rule. `privacyfence_list_policy` shows it the current rules and the scope
catalogue, and `privacyfence_check_policy` predicts whether a call would run without a card. A
proposal always waits for you on the same confirmation dialog; nothing is written unless you
confirm, and it is refused outright in an unattended session.

Some operations can only be allowed from Settings or a proposal, never from a card, because nothing
on the card names a resource to scope the rule to: Apps Script projects (scope "Apps Script —
project"), Gmail filters ("Gmail — anything (unconditional)") and creating a Slack group chat
("Slack — anything (unconditional)").

In an organization deployment each person manages their own rules on their own **Settings** page;
rules are never shared between people.

### Where rules are stored

All three ways write the same `auto_accept:` section of `settings.yaml`:

```yaml
auto_accept:
  version: 2
  rules:
    - id: r-3f9a1c2b8e
      predicate: approved_sandbox_folder
      value: ["1CdeFghIJKLmnoPQRstuVWxyz0123456789AbCdEfGh"]
      operations: [docs.edit_content, drive.write_file, sheets.write_range]
      conditions: [[not_shared_drive, null]]
```

- `predicate` and `value` are the scope (see the [scope catalogue](#scope-catalogue)).
- `operations` are internal operation keys; Settings shows them as verbs and tool names.
- `conditions` is a list of `[name, value]` pairs (see [conditions](#conditions)). The value is
  `null` for a condition that takes none.
- `id` is derived from the predicate, value and conditions, so the same rule always has the same
  id — the audit log and the Auto-accept page's match counts use it.

Prefer the three surfaces above to hand-editing: they validate the rule and mint its id. A
hand-edited entry that is malformed, or names an unknown predicate or condition, never matches
anything. A `settings.yaml` that still has an `auto_accept_rules:` or `auto_accept_grants:` section
is refused at startup with an error naming the section; recreate those rules on the Auto-accept
page. If the file also carries `migrated_to_policy_v2: true`, those sections' rules are already in
`auto_accept:`, so PrivacyFence removes the old sections and the marker, rewrites the file, and
starts. See [ADR 0041](adr/0041-only-the-current-install-layout-is-supported.md) and
[ADR 0047](adr/0047-settings-an-earlier-release-converted-are-cleaned-up-not-refused.md).

### Scope catalogue

**Identity** scopes name specific resources; **attribute** scopes name a property.

| Scope | Kind | Value | Predicate(s) | Matches when |
|---|---|---|---|---|
| Drive folder | identity | folder ids | `approved_folder`, `approved_sandbox_folder`, `parent_folder_allowlist`, `move_within_approved_folders` | the file's direct parent is one of the folders (not recursive). For an upload or new file, the destination folder. For a move, the file's current folder — the destination is not checked |
| Drive file | identity | file ids | `drive.file` | the file is one of these |
| Drive file type | attribute | MIME types | `file_type_allowlist` | the file has one of these MIME types |
| Drive files I own | attribute | — | `i_am_owner`, `created_by_me` | you are an owner of the file |
| Drive files created this session | attribute | — | `created_this_session` | PrivacyFence created the file since it last started |
| Gmail sender | identity | — | `i_am_sender` | you sent the message |
| Gmail sender domain | attribute | domains | `trusted_sender_domain` | the sender's domain is one of these or a subdomain of one |
| Gmail recipient | identity | — | `to_is_myself`, `i_am_sole_recipient` | every `to` address is yours (`to_is_myself`); you are the message's only recipient (`i_am_sole_recipient`) |
| Gmail recipient domain | attribute | domains | `approved_recipient_domain` | every recipient is in one of these domains |
| Gmail label | identity | label names | `label_match`, `label_name_allowlist` | the message carries one of the labels; the label being applied or removed is one of these |
| Gmail / Calendar — anything | attribute | — | `always_allow` | always (unconditional) |
| Gmail — anything | attribute | — | `gmail.anything` | always (used for Gmail filters) |
| Slack channel | identity | channel or user ids | `approved_channel`, `approved_channel_all_results`, `approved_recipient`, `send_to_myself` | the channel is one of these; for a search, every result is in one of them |
| Slack channel kind | attribute | — | `dm_with_myself`, `group_dm`, `public_channels_only` | the channel is a direct message (any 1:1 DM, not only your self-DM); a group DM; every result is from a public channel |
| Slack — anything | attribute | — | `slack.anything` | always (used for creating group chats) |
| Telegram chat | identity | chat ids | `approved_chats`, `approved_chats_all_results` | the chat is one of these; for a search, every result is in one of them |
| Calendar | identity | calendar ids | `personal_calendar` | the call's calendar is one of these |
| Calendar events I organize | attribute | — | `i_am_organizer` | you are the event's organizer |
| Tasks list | identity | task list ids | `approved_task_list` | the list is one of these; for a move, both lists are |
| Jira project | identity | project keys | `approved_project_keys` | the project (or the issue key's project) is one of these |
| Jira issues I report / am assigned | attribute | — | `i_am_reporter`, `i_am_assignee` | you are the reporter; you are the assignee |
| Confluence space | identity | space keys | `approved_space_keys` | the page's space is one of these |
| Confluence pages I wrote | attribute | — | `i_am_author` | you are the page's author |
| Salesforce object type | attribute | object names | `approved_object_types` | every requested object type is one of these |
| Salesforce report | identity | report ids | `approved_report_ids` | the report is one of these |
| Contacts label | identity | label names | `label_name_allowlist` | the label being applied or removed is one of these |
| Apps Script project | identity | script ids | `apps_script.project` | the script is one of these |

Matching is case-insensitive for domains, labels, space keys, project keys and object types.

### Conditions

Conditions narrow a rule; they never widen it. They go in the rule's `conditions:` list. The card
and Settings offer only two of them, as calendar read scopes of their own — "no external attendees"
and "non-private events"; add any other condition by editing `settings.yaml`.

| Condition | Value | Holds when |
|---|---|---|
| `older_than_days` | days | the message is at least that many days old |
| `within_days` | days | the event starts within that many days from now |
| `past_only` | — | the event has already ended |
| `no_attachments` | — | the Gmail message, Slack message or Telegram message carries no attachment, file or media |
| `no_external_attendees` | — | every attendee's address contains your own domain |
| `no_conferencing_link` | — | the event has no meeting link |
| `not_private` | — | the event's visibility is not private |
| `not_shared_drive` | — | Google Drive reports the file as not shared. Despite the name, this reads Drive's "shared" flag, which Drive does not set for files in a shared drive — so it does not exclude shared-drive files |
| `no_contact_info_change` | — | a contact edit changes no email address or phone number |
| `in_existing_thread` | — | the Slack message is a reply in an existing thread |

`no_contact_info_change` and `in_existing_thread` are decided from the call's arguments; every other
condition needs the fetched item, so `privacyfence_check_policy` reports a rule that uses one as
"unknown" rather than guessing.

### Verbs

Settings and the confirmation dialog describe operations with 18 verbs in four families, colour-coded
so a risky verb stands out:

| Family | Verbs |
|---|---|
| read | read, download, search |
| write | create, update, format, restructure, comment, label, move, archive, complete, transition, configure |
| send | send, draft, share |
| destructive | delete |

### Same-file grace window

Some Drive writes tend to come in bursts against one file — filling a sheet range by range, editing
a document paragraph by paragraph. For these six tools, clicking **Allow once** also allows further
calls of the same tool on the same file for **5 minutes**, without a card:

`drive_sheets_write_range`, `drive_sheets_format_range`, `drive_sheets_insert_dimensions`,
`drive_docs_edit_content`, `drive_docs_format_content`, `drive_add_comment`.

The card says so above its buttons ("Approving this also allows further calls like this to the same
file for a few minutes without asking again."). The window is not a rule: it is never written to
`settings.yaml` and ends when PrivacyFence restarts. Deleting rows or columns, adding or renaming a
sheet, replacing a whole file or document, uploading and moving never open one.

---

## Privacy filter

The privacy filter decides, per category of data, what may leave PrivacyFence at all. It runs
before the card is built, so what it removes never reaches the card, the AI system or the audit
log. It is a floor under your review, not a replacement for it: filtered content still needs your
approval where the gate asks for it.

Each category is set to one of:

| Policy | Text becomes | A list becomes |
|---|---|---|
| `allow` | unchanged | unchanged |
| `redact` | `[REDACTED BY PRIVACY FILTER — N characters withheld]` (only the length is revealed) | empty |
| `block` | `[BLOCKED BY PRIVACY FILTER]` | empty |

Set them in **Settings → Privacy Filter**, which has one page per group, or in `settings.yaml`.
Changes apply immediately. Each group also has a `default_policy` for any category not listed.

| Group (`settings.yaml` key) | Settings page | Categories — value in a fresh `settings.yaml` |
|---|---|---|
| `privacy` | Gmail | `body` allow · `metadata` allow · `attachments` block · `thread_history` allow |
| `drive_privacy` | Drive & Sheets | `file_content` · `file_metadata` · `file_list` · `folder_structure` — all allow |
| `slack_privacy` | Slack | `message_content` · `user_identity` · `channel_list` · `thread_content` · `dm_list` · `group_chat_list` — all allow |
| `contacts_privacy` | Contacts | `notes` block |
| `tasks_privacy` | Tasks | `notes` block |
| `confluence_privacy` | Confluence | `search_excerpt` block · `attachments` block |

In a fresh `settings.yaml` every group's `default_policy` is `block`. If a group is missing from
`settings.yaml` altogether, its default is `allow` on a local install and `block` in an
organization deployment. A group that is present but malformed — an unknown policy value, a
non-mapping `categories` — stops PrivacyFence from starting rather than falling back to `allow`.

The filter applies only to tools that return content from the service; writes are not filtered.
Other connectors have no categories. **Settings → Privacy Filter → Calendar** has one switch
instead: whether `calendar_get_free_busy` may show event titles and status
(`calendar.free_busy_full_event_details`, on by default) or only busy/free blocks.

If `file_metadata` and `file_list` are set differently, PrivacyFence logs a warning at startup:
a file's name and owners reach the AI system through whichever of the two tools is still allowed.

In an organization deployment the privacy filter and the PII check are install-wide and edited by an
administrator; see [Organization deployment](org-mode-setup-guide.md).

---

## File previews

For attachments and files — `gmail_download_attachment`, `confluence_download_attachment`,
`drive_download_file`, `drive_upload_file` — the card previews the file's content so you can see
what you are releasing or uploading. The same extracted text is what the PII check scans.

| Type | Preview |
|---|---|
| Images | shown as an image (no text is read from it) |
| Plain text, CSV, JSON and other `text/*` | the text |
| HTML | converted to Markdown |
| PDF | the text of each page, as plain text; scanned PDFs have little or none |
| DOCX, PPTX | text with headings, emphasis, lists and tables kept as Markdown |
| XLSX | each sheet as a table, first 200 rows and 20 columns |
| ZIP | a list of the first 200 files and their sizes; nothing inside is opened |
| Anything else | no preview, only the file's name, type and size |

Limits:

| Limit | Value |
|---|---|
| Extracted text shown and scanned | first 20,000 characters |
| Gmail and Confluence attachments fetched for a preview | up to 5 MB; larger ones show metadata only and are not scanned |
| Drive files fetched in full for a preview | up to 5 MB; larger ones are not scanned |
| Local file read for an upload preview | up to 5 MB |
| A single part inside a DOCX or PPTX | 10 MiB decompressed; a larger part is skipped |
| `drive_upload_file` from a local path | 50 MB |
| Attachments on one Gmail draft | 18 MB in total |

Reading a document with `drive_get_file_content` is different: it fetches at most the first
100 KB (102,400 bytes) of the file, and that text is what the AI system receives. The card shows,
and the PII check scans, only the first 2,000 characters of it. A PDF that fits within 100 KB is
rendered on the card as a PDF when the file's content is allowed by the privacy filter. A sheet
read (`drive_sheets_get_values`) shows and scans the first 50 rows.

Macros and embedded scripts are never run, and a file PrivacyFence can't parse simply gets no
preview.

---

## Notifications

While the Approvals page is open in a browser tab:

- the tab title shows the number waiting, for example "(3)", and screen readers announce changes —
  no permission needed;
- if you allow notifications, a desktop notification appears when a new request arrives and the tab
  is not focused (at most one every 5 seconds; several at once become one "N approvals pending"
  notification). They come from your browser; nothing leaves your machine.

PrivacyFence offers to turn notifications on right after you decide a request, never on page load.
You can also turn them on in **Settings → General → Approval Notifications**, which is also where
you set how much a notification says:

| Level | A notification for one new request shows |
|---|---|
| `minimal` | "1 approval pending" and nothing else |
| `standard` | adds the connector and whether it is a read or a write |
| `detailed` | also adds the request's title, for example "Send email to alice@example.com" — which can put private content on your lock screen |

`web.notifications.detail` is `standard` in a fresh `settings.yaml`; if the key is missing, the
level is `minimal`. `web.notifications.enabled: false` turns off the title count, the announcements
and the notifications together.

Organization deployments have no live updates, so they have no notifications.

---

## Step-up: passkey on approvals

When step-up is on, approving asks for your passkey (Touch ID, Face ID, Windows Hello, or a
security key) — proof that a person, not a program on your computer, made the decision. It is on
by default on installs from the macOS, Windows and Linux installers, with the scope
`writes_and_pii_reads`: every approved write, and every read the PII check flagged. Denying never
needs a passkey. A batch approved with **Approve selected** needs one prompt for the whole set.

Add a passkey at **Security** (`/security`); on a packaged install the companion app walks you
through adding the first one. On Windows, set up Windows Hello first (**Settings → Accounts →
Sign-in options**, add a PIN): if it isn't set up, adding a passkey fails with a message saying the
device has no built-in passkey authenticator ready. The same `NotAllowedError` also appears when the
Windows Hello or Touch ID prompt was cancelled, timed out, or opened behind the browser window — the
message says which of the two it was. A device that already has a passkey for this install says so.

The scopes, `require_passkey`, the batch setting and exactly what a passkey does and does not prove
are in [Security and compliance](security-and-compliance.md).
