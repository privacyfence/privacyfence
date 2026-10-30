# Connector QA

How to stand up and run live connector QA: the dedicated QA accounts and their seed data, the
recorder that checks and records provider responses (`scripts/qa_fixture_recorder.py`), the
self-hosted runner that runs it on a schedule, and the exploratory pass that drives connector tools
and the approval gate by hand. When each of these is required is policy: the live tier and the
`--check` report a connector PR owes are in
[`testing-policy.md`](testing-policy.md#layer-5-live-connector), and when the exploratory pass is
due is in [`release-testing.md`](release-testing.md).

## Credential boundary

Live connector credentials — the per-account OAuth token files and the Telegram session, and the
optional QA `org_bundle.json` — exist only as local files on a developer's own machine or on the
project-controlled self-hosted runner `runner.privacyfence.eu`, labelled `privacyfence-live`. They
are never stored as GitHub Actions secrets and never reach a GitHub-hosted runner or a
`pull_request`-triggered workflow. Both
workflows that use them (`connector-live-check.yml`, `qa-record-fixture.yml`) trigger only on
`schedule` or `workflow_dispatch`. The reasoning is in
[ADR 0019](adr/0019-live-connector-credentials-only-on-a-self-hosted-runner.md).

Telegram's `api_id`/`api_hash` are not a per-account credential: they are the one shared app
identity every release build carries. `connector-live-check.yml` maps the repository secrets
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` to `PRIVACYFENCE_TELEGRAM_API_ID`/
`PRIVACYFENCE_TELEGRAM_API_HASH`. The per-account Telegram session stays on the runner host with the
other tokens.

The built-in OAuth apps' client IDs and secrets are different again: they identify the PrivacyFence
application, not an account. Both workflows write them from the `PF_APP_*` repository secrets in the
environment of one step, and delete the generated module in a last step that always runs (see
[Organization mode and the `qa-live` principal](#organization-mode-and-the-qa-live-principal)).

## QA accounts and organization config

Use dedicated accounts that share nothing with a personal or production identity:

| Service | Account | Notes |
|---|---|---|
| Google | A dedicated Google Workspace account, ideally on its own domain | A consumer Gmail account covers every Google connector except `calendar_list_rooms`, which needs Workspace admin rights (see [Seed: Calendar](#seed-calendar)). |
| Slack | A Slack Developer Program sandbox | Connect the built-in Slack app to the sandbox's workspace. Sandboxes expire; check the expiry date whenever you reconnect and extend it from **Sandboxes → Extend**. |
| Atlassian | A free Jira + Confluence Cloud site | One OAuth app covers both products. |
| Salesforce | A Developer Edition org | Connect the built-in Salesforce app; the login host is `https://login.salesforce.com`. |
| Grist | A getgrist.com account holding nothing real | Connected with that account's personal API key on the QA server's `/connect` page. The server address comes from `PRIVACYFENCE_GRIST_SERVER_URL`, set to `https://docs.getgrist.com` in the workflows. |
| Miro | A free Miro account holding nothing real | Connect the built-in Miro app on `/connect`; the runner refreshes the token. |
| Peek | A QA account on the QA Peek server | Peek's own Docker image, `ghcr.io/privacyfence/peek:main`, runs on the runner's host next to the QA server at `https://peek-qa.privacyfence.eu` (the workflows set `PRIVACYFENCE_PEEK_BASE_URL` to it). Connect it on `/connect` with the device sign-in, which opens in the maintainer's browser. |
| Telegram | Your own account | No dedicated account; connect it on `/connect` with the phone number and code. |

The runner uses the built-in OAuth apps, so there is no QA app to register. Every account is connected
on the QA organization server in a browser (see [The QA server](#the-qa-server)). An organization's
own app can still be tested: build a QA bundle for it as the "For IT admins" section of
[`google-cloud-setup.md`](google-cloud-setup.md), [`slack-setup.md`](slack-setup.md),
[`atlassian-setup.md`](atlassian-setup.md) or [`salesforce-setup.md`](salesforce-setup.md) describes
(`scripts/build_org_bundle.py --merge`), and place it on the runner as the optional
`~/privacyfence/org/org_bundle.json` (see [Persistent QA state](#persistent-qa-state)). Keep it
separate from any production bundle.

## Seed data

`scripts/qa_fixture_recorder.py` targets exactly one seed artifact per connector, located through
`tests/fixtures/qa_environment.yaml`. That file is git-ignored; create it with
`cp tests/fixtures/qa_environment.yaml.example tests/fixtures/qa_environment.yaml` and fill in each
connector's section as you work through the checklists below. The recorder exits with
`manifest not found` when the file is missing.

### Conventions

- **Identity may be real, content never is.** The project, space, channel or folder a fixture lives
  in belongs to a real QA account; anything you type into a body, title or summary is synthetic.
- **Names are exact.** Keys use `PFQA`, names use `PrivacyFence QA …`, and seeded content carries
  the tag `[QATEST]`. Lookups are exact string matches.
- **The tag is a guardrail.** Every `check_<connector>()` refuses to record an object whose
  title/summary/name/text does not contain `[QATEST]` (Drive, whose target is a folder with no body,
  matches on the exact folder name instead). A wrong id in the manifest fails the check; it never
  records the wrong object.
- **Placeholder contact data**: email addresses under `example.com`/`example.org`/`example.net`
  (RFC 2606), phone numbers in `555-0100`–`555-0199`.
- **Blank means resolve.** Where a key below says "blank to resolve", leaving it `""` makes the
  recorder find the artifact by name/title (one extra API call per run). Fill it in once known.
- **Auto-accept rules are optional.** The recorder never evaluates rules; they only matter for the
  [exploratory checks](#exploratory-connector-and-gate-qa). Add them on PrivacyFence Settings'
  **Auto-accept** page (or with **Always allow** on an approval card), which writes the
  `auto_accept:` section of `settings.yaml` and mints each rule's required `id`. The tables below
  give each rule's predicate, value and operations; predicates are described in
  [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

### Seed: Gmail

- [ ] Send yourself a message with subject `PrivacyFence QA seed message [QATEST]` and body
      `Synthetic PrivacyFence QA test message. No real information.`
- [ ] Reply to it once (`Synthetic PrivacyFence QA reply. No real information.`), so a real
      two-message thread exists.
- [ ] Set `gmail.seed_message_id` to the seed message's id. **Required**: Gmail has no
      resolve-by-subject fallback, because `GmailClient.list_messages()` makes one extra API call
      per result.

The recorder also records `list_send_as`, which is account-level and needs no seed artifact: it
passes when the account returns a primary send-as address.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Trusted sender domain | `trusted_sender_domain` | a domain you really receive mail from | `gmail.read_message` |

### Seed: Drive

- [ ] Create a folder named exactly `PrivacyFence QA Sandbox` in My Drive. Every Drive/Sheets/Docs
      artifact the exploratory checks create goes inside it.
- [ ] Set `drive.folder_name` (default `PrivacyFence QA Sandbox`) and optionally `drive.folder_id`
      (the id in the folder's URL; blank to resolve by name).

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Trusted folder (read) | `approved_folder` | the folder id | `drive.read_file_contents`, `drive.download_file`, `sheets.read_values` |
| Sandbox folder (selected writes) | `approved_sandbox_folder` | the folder id | any of `sheets.rename_sheet`, `sheets.format_range`, `sheets.insert_dimensions`, `sheets.delete_dimensions`, `docs.edit_content`, `docs.format_content` |

Add sandbox-folder rules per operation rather than as a whole-folder write grant, so the other
writes into the same folder still show an approval card for the [Drive checks](#drive-checks).
Items the checks delete go to Drive's trash, which Google purges after 30 days.

### Seed: Slack

- [ ] Pick or create an **approved** channel and join it.
- [ ] Create a **control** channel named exactly `privacyfence-qa-control`, join it, and give it no
      auto-accept rule.
- [ ] In the control channel, post `PrivacyFence QA seed message [QATEST]. No real information.`
      and reply to it in-thread with `PrivacyFence QA seed reply [QATEST]. No real information.`
- [ ] Set `slack.channel_name` (default `privacyfence-qa-control`). Optionally set
      `slack.channel_id` (blank to resolve by name) and `slack.seed_thread_ts` (blank to scan the
      channel's last 200 messages for `[QATEST]`).
- [ ] Optionally set `slack.group_dm_id`: the id of a group DM the QA account is in. It enables the
      group-DM membership and read-marker checks; without it those checks are skipped. The 1:1 DM
      read-marker check uses the QA account's first DM and is skipped when it has none.

The recorder reads that thread with `get_thread_replies` and checks that the thread starter
carries `[QATEST]` and has text and a user id.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved channel | `approved_channel` and `approved_channel_all_results` | the approved channel's id | `slack.read_messages` |

### Seed: Calendar

- [ ] Create a secondary calendar named exactly `PrivacyFence test [PFQA]` (Google Calendar →
      Settings → Add calendar → Create new calendar). Every QA activity targets it, not primary.
- [ ] On it, create one event far in the future with no attendees: title
      `PrivacyFence QA seed event [QATEST]`, description
      `Synthetic PrivacyFence QA test event. No real information.`
- [ ] Set `calendar.calendar_id` to the PFQA calendar's id. **Required**: the recorder and
      `--lifecycle` use the value as given, and the example file's blank value is not a valid
      calendar id.
- [ ] Set `calendar.seed_event_title` (default `PrivacyFence QA seed event [QATEST]`) and optionally
      `calendar.seed_event_id` (blank to resolve by a title search).

`calendar_create_out_of_office` and `calendar_set_working_location` always act on the primary
calendar, whatever `calendar_id` is passed; exploratory runs that exercise them leave entries on
primary, and neither has a delete tool.

`calendar_list_rooms` returns the room list in `org/admin_config.json` and makes no live call. To
cover it, you need Workspace admin rights: create a calendar resource (Admin console → Directory →
Buildings and resources), then run `scripts/sync_room_directory.py --output rooms.json` from a second
Cloud project as described in [`google-cloud-setup.md`](google-cloud-setup.md)'s "Room directory sync"
section, and upload `rooms.json` on the Admin page's Features section. Without it the tool returns an
empty list, not an error.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Trusted calendar (read) | `personal_calendar` | the PFQA calendar id | `calendar.read_event_details` |
| Trusted calendar (write) | `personal_calendar` | the PFQA calendar id | `calendar.create_modify_event`, `calendar.set_visibility`, `calendar.set_color`, `calendar.delete_event` |
| Own events | `i_am_organizer` | — | `calendar.read_event_details` |

### Seed: Contacts

- [ ] Create one contact: display name `PrivacyFence QA Test Contact [QATEST]`, email
      `qatest.contact@example.com`, phone `555-0142`.
- [ ] Set `contacts.seed_contact_display_name` (default `PrivacyFence QA Test Contact [QATEST]`)
      and optionally `contacts.seed_contact_resource_name` (for example `people/c12345`; blank to
      resolve by a personal-contacts name search).

This is the one fixture the recorder does **not** pass through identity redaction: the contact's
name, email and phone are the content under test. Structural ids and URLs are still
de-identified. Keep the seed contact's data synthetic for that reason.

| Optional rule | Predicate | Value | Conditions | Operations |
|---|---|---|---|---|
| Edits that leave email/phone alone | `always_allow` | — | `no_contact_info_change` | `contacts.edit` |

### Seed: Tasks

Never use the default "My Tasks" list.

- [ ] Create a list named exactly `PrivacyFence QA List` (the approved list).
- [ ] Create a second list named exactly `PrivacyFence QA Contrast List` and give it no rule. The
      recorder does not use it; the [Tasks checks](#tasks-checks) do.
- [ ] In `PrivacyFence QA List`, create the task `PrivacyFence QA seed task [QATEST]` with notes
      `Synthetic PrivacyFence QA test task. No real information.`
- [ ] Set `tasks.task_list_id` and `tasks.seed_task_id`. **Both required**: `tasks_client.py` has no
      search-by-title method. `.venv/bin/python scripts/qa_list_ids.py tasks` prints every list's
      id; `--lifecycle` also needs `tasks.task_list_id`.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved list | `approved_task_list` | the approved list's id | any of `tasks.create_task`, `tasks.update_task`, `tasks.complete_task`, `tasks.uncomplete_task`, `tasks.move_task` |

`tasks.move_task` matches only when both the source and destination list are in the rule's value.

### Seed: Apps Script

- [ ] At [script.google.com](https://script.google.com), create a **standalone** project (not bound
      to a Sheet, Doc or Form) titled `PrivacyFence QA seed script [QATEST]`, whose `Code.gs` holds
      one trivial synthetic function. `list_projects` resolves projects through Drive's `mimeType`
      filter, which returns standalone projects only.
- [ ] Turn on the per-user **Google Apps Script API** switch at
      [script.google.com/home/usersettings](https://script.google.com/home/usersettings) for the QA
      account. Until it is on, every `apps_script_*` call fails with a 403 (see
      [`google-cloud-setup.md`](google-cloud-setup.md)).
- [ ] Set `apps_script.seed_script_title` (default `PrivacyFence QA seed script [QATEST]`) and
      optionally `apps_script.seed_script_id` (blank to resolve by title).

The recorder reads the title with a separate `projects.get` call, because `projects.getContent`
returns no title, then records `get_content`, which must return at least one file with a name and
type.

### Seed: Telegram

- [ ] Send yourself one message in Saved Messages:
      `PrivacyFence QA seed message [QATEST]. No real information.`
- [ ] Leave `telegram.chat_id` blank: the recorder finds Saved Messages through the `is_self` flag.
      Set it only to debug that resolution (`scripts/qa_list_ids.py telegram` prints chat ids and
      `is_self`). `telegram.history_limit` (default `100`) is how many recent messages are scanned
      for the tag.
- [ ] Make sure at least one other chat has message history, as the contrast case for the
      [Telegram checks](#telegram-checks).

The recorder copies the session file to a temporary directory before connecting, so it can run
while the daemon holds the session open. `scripts/qa_list_ids.py` does not: stop the daemon first
if it reports `database is locked`.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved chat | `approved_chats` and `approved_chats_all_results` | a chat id (Saved Messages or a low-stakes chat) | `telegram.read_chat_messages` |

### Seed: Peek

- [ ] On the QA Peek server, upload a signed-in page named `PrivacyFence QA [QATEST].html` with the body
      `Synthetic PrivacyFence QA test page. No real information.`
- [ ] Add one comment to it whose text carries `[QATEST]`.
- [ ] Set `peek.page_slug` to the page's slug (the last path segment of its address). The recorder
      refuses to record unless the page's name and every comment on it carry `[QATEST]`.

`--lifecycle` uploads a signed-in `[QATEST-LIFECYCLE]` page, makes it public, checks its versions,
grants and revokes access for a placeholder address, renames it, posts a
comment as the QA account (Peek's account comment endpoint), reads the comment back and deletes
the page by the slug the rename returned, then checks that it is no longer listed. Visit stats are
never recorded: they carry IP addresses.

### Seed: Salesforce

- [ ] Create two or three Account records named like `PrivacyFence QA — Acme Test Co [QATEST]` and
      `PrivacyFence QA — Globex Test Co [QATEST]`.
- [ ] Create a **tabular** report on Accounts named exactly `PrivacyFence QA Report`, with the Account Name and at least one plain text column (for example Billing City).
- [ ] Add an **Auto Number** field `PF QA Number` to Account (display format `PFQA-{00000}`, starting at 1)
      and add it to `PrivacyFence QA Report`. The report-paging check pages by this column.
- [ ] Create a **summary** report on Accounts named exactly `PrivacyFence QA Summary Report`, grouped by
      Billing City, with Account Name and `PF QA Number`. The check flattens it. Override the names with
      `salesforce.page_by_label` and `salesforce.summary_report_name`.
- [ ] Set `salesforce.report_name` (default `PrivacyFence QA Report`), `salesforce.object_type`
      (default `Account`) and `salesforce.seed_record_name` (default
      `PrivacyFence QA — Acme Test Co [QATEST]`). Optionally set `salesforce.report_id` (blank to
      match by name) and `salesforce.seed_record_id` (blank to resolve through `search()` on the
      record name).

Salesforce's search index can take a few minutes to pick up new records; an empty search right
after seeding is expected.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved report | `approved_report_ids` | the report id | `salesforce.run_report` |
| Approved object type | `approved_object_types` | `[Account]` | `salesforce.read_record` (optionally `salesforce.search`) |

### Seed: Grist

- [ ] In the Grist QA account, create a document named exactly `PrivacyFence QA [QATEST]`.
- [ ] In it, a table `QaSeed` with the text columns `Name` and `Note` and two rows whose `Name`
      carries `[QATEST]`.
- [ ] Add an empty table `QaLifecycle` with the same two columns. `--lifecycle` writes only there.
- [ ] Check the table **ids**, not the page names: open **Raw Data** and, if a table is still called
      `Table1` or `Table2`, rename it there to `QaSeed` or `QaLifecycle`. Renaming a page does not
      rename its table, and the recorder addresses tables by id (a wrong id answers HTTP 404).
- [ ] Create a second document with a different name, as the contrast case.
- [ ] Set `grist.doc_id` (the full Document ID from the document's Settings, not the shorter id in its address; required),
      `grist.table_id` (default `QaSeed`) and `grist.lifecycle_table_id` (default `QaLifecycle`).

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved document | `grist.document` | the document id | `grist.read_records` |

`--lifecycle` adds one `[QATEST-LIFECYCLE]` row to `grist.lifecycle_table_id`, reads it back,
updates its `Note` and reads it back again. It never deletes the row, because PrivacyFence has no
Grist delete; the rows accumulate and need occasional manual cleanup there.

### Seed: Jira

- [ ] Create a project with key exactly `PFQA` (any template).
- [ ] Create the issue `PrivacyFence QA seed issue [QATEST]` with description
      `Synthetic PrivacyFence QA test issue. No real information.`
- [ ] Make sure at least one other project exists on the site as the contrast case.
- [ ] Set `jira.project_key` (default `PFQA`), `jira.seed_issue_summary` (default
      `PrivacyFence QA seed issue [QATEST]`) and optionally `jira.seed_issue_key` (blank to resolve
      by a JQL summary search).

`--lifecycle` creates, updates and deletes a `[QATEST-LIFECYCLE]` issue in `jira.project_key` on
every run.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved project | `approved_project_keys` | `[PFQA]` | `jira.read_issue` |
| Own issues | `i_am_reporter` / `i_am_assignee` | — | `jira.read_issue` |

### Seed: Miro

- [ ] In the Miro QA account, create the board `PrivacyFence QA [QATEST]`, with these on it:
      a frame `QaFrame [QATEST]` holding two sticky notes, one more sticky note outside the frame,
      a connector line between two of the sticky notes, and a tag titled `QATEST`. Every sticky
      note's text carries `[QATEST]`.
- [ ] Create the board `PrivacyFence QA lifecycle [QATEST]` with a frame titled exactly
      `QaLifecycleFrame [QATEST]`. `--lifecycle` writes only there.
- [ ] Keep each board under 50 items: the recorder reads one page of 50.
- [ ] Set `miro.board_id` and `miro.lifecycle_board_id` to the board ids (the part after
      `/app/board/` in the address, ending in `=`; a board link is refused). `miro.board_id` is
      required.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved board | `miro.board` | the seed board id | `miro.list_items` |

`--lifecycle` runs on `main`'s code only. On `miro.lifecycle_board_id` it creates a sticky note in
`QaLifecycleFrame [QATEST]`, a shape and a frame, updates the sticky note, reads each back, and moves
the shape into the new frame and the sticky note out of its frame. The client has no delete, so the
`[QATEST-LIFECYCLE]` items accumulate and are cleaned by hand there.

### Seed: Jira Assets

- [ ] Create an Assets schema with key exactly `PFQA`, holding the object types Account, Contract
      and Asset. Contract references Account, and Asset references Contract.
- [ ] Create two accounts, two contracts and three assets, every label tagged `[QATEST]`.
- [ ] Name one contract `PrivacyFence QA seed contract [QATEST]`.
- [ ] Optional: link the seed contract to a `[QATEST]` Jira issue through an **Assets objects**
      custom field (object schema `PFQA`, filter `objectType = Contract`), so connected tickets has
      something to return; without it the recording holds an empty list. The field is created in
      the site-wide Fields admin and attaches only to a company-managed space; a team-managed
      space's own field dialog has no Assets type.
- [ ] Build the QA bundle with `--atlassian-assets --atlassian-assets-workspace-id <workspaceId>`,
      the id shown at `https://<qa-site>.atlassian.net/rest/servicedeskapi/assets/workspace`.
- [ ] Set `assets.schema_key` (default `PFQA`), `assets.seed_object_label` (default
      `PrivacyFence QA seed contract [QATEST]`) and optionally `assets.seed_object_key` (blank to
      resolve by label inside the schema).

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved schema | `approved_schema_ids` | `[<PFQA schema id>]` | `assets.read_object` |

### Seed: Confluence

- [ ] Create a space with key exactly `PFQA`.
- [ ] Create the page `PrivacyFence QA seed page [QATEST]` with body
      `Synthetic PrivacyFence QA test page. No real information.`
- [ ] Make sure at least one other space exists as the contrast case.
- [ ] Set `confluence.space_key` (default `PFQA`), `confluence.seed_page_title` (default
      `PrivacyFence QA seed page [QATEST]`) and optionally `confluence.seed_page_id` (blank to
      resolve by title).

`--lifecycle` creates and updates a `[QATEST-LIFECYCLE]` page in `confluence.space_key` on every
run and never deletes it: deleting a page needs the `delete:page:confluence` scope, which the app
never requests (see [`atlassian-setup.md`](atlassian-setup.md)). Delete those pages by hand from
time to time.

| Optional rule | Predicate | Value | Operations |
|---|---|---|---|
| Approved space | `approved_space_keys` | `[PFQA]` | `confluence.read_page` |
| Own pages | `i_am_author` | — | `confluence.read_page` |

### Manifest reference

Every key `scripts/qa_fixture_recorder.py` reads from `tests/fixtures/qa_environment.yaml`, and the
fixture files `--record` writes under `tests/fixtures/live/<connector>/`:

| Section | Keys (required in bold) | Recorded fixtures | `--lifecycle` |
|---|---|---|---|
| `gmail` | **`seed_message_id`** | `get_message.json`, `list_send_as.json` | — |
| `drive` | `folder_name`, `folder_id` | `get_file_metadata.json` | — |
| `slack` | `channel_name`, `channel_id`, `seed_thread_ts`, `group_dm_id` | `get_thread_replies.json`; checks `conversation_membership`, `group_dm_membership`, `conversation_read_marker`, `group_dm_read_marker` and `dm_read_marker` (no fixture) | — |
| `calendar` | **`calendar_id`**, `seed_event_title`, `seed_event_id` | `get_event.json` | creates, updates and deletes one event and one two-instance recurring series on `calendar_id` |
| `contacts` | `seed_contact_display_name`, `seed_contact_resource_name` | `get_contact.json` | — |
| `tasks` | **`task_list_id`**, **`seed_task_id`** | `get_task.json` | creates, updates and deletes one task in `task_list_id` |
| `apps_script` | `seed_script_title`, `seed_script_id` | `get_content.json` | — |
| `telegram` | `chat_id`, `history_limit` | `get_messages.json` | — |
| `peek` | **`page_slug`** | `list_pages.json`, `get_comments.json` | uploads, publishes, checks versions, grants and revokes access, renames, comments on and deletes one page |
| `salesforce` | `report_name`, `report_id`, `object_type`, `page_by_label`, `summary_report_name`, `seed_record_name`, `seed_record_id`, `lifecycle_object_type`, `lifecycle_name_field`, `lifecycle_note_field` | `list_reports.json`, `run_report_page.json`, `get_record.json` | creates and updates one Task (`lifecycle_object_type`), then two more in one batch; never deletes them |
| `grist` | **`doc_id`**, `table_id`, `lifecycle_table_id` | `list_documents.json`, `list_columns.json`, `get_records.json` | adds and updates one row in `lifecycle_table_id`; never deletes it |
| `miro` | **`board_id`**, `lifecycle_board_id` | `get_board.json`, `list_items.json`, `list_connectors.json`, `list_tags.json` | creates a sticky note, a shape and a frame on `lifecycle_board_id`, updates the sticky note and moves items into and out of frames; never deletes them |
| `jira` | `project_key`, `seed_issue_summary`, `seed_issue_key` | `list_projects.json`, `get_issue.json` | creates, updates and deletes one issue in `project_key` |
| `confluence` | `space_key`, `seed_page_title`, `seed_page_id` | `list_spaces.json`, `get_page.json` | creates and updates one page in `space_key`; never deletes it |

The recorder's `CONNECTOR_CHECKS` and `EXPECTED_FIXTURES` registries list these connectors; a
connector added to one without the other fails at import time.

## Authenticating connectors

On the runner, nobody runs a loopback sign-in or copies a token file: every account is connected in a
browser on the QA server's `/connect` page, and `scripts/qa_runner_tokens.py` copies the tokens (see
[Connecting the QA accounts](#connecting-the-qa-accounts)). Reconnecting an account there is how a
token is minted again.

`scripts/qa_authenticate_connectors.py` and the per-connector `--<connector>-oauth` flags remain for
local mode on a developer's own machine, where the recorder is run with the checkout's own
`credentials/` folder. The runner does not use them, and they go away with local mode in 8.
Telegram's local sign-in is `privacyfence-app --telegram-setup` (see
[`telegram-setup.md`](telegram-setup.md)). The token files are named in `daemon_main.TOKEN_FILES`
(`token.json` for Gmail, `<connector>_token.json` for the others, `atlassian_token.json` shared by
Jira and Confluence, `telegram.session`).

## Running the recorder

Always use the project venv; the recorder imports the same third-party clients as the daemon. It
runs in organization mode as the principal `qa-live`, so it reads its tokens from
`users/qa-live/credentials/` of the checkout; on a developer's machine, copy a QA server's folder
there with `scripts/qa_runner_tokens.py pull`, or connect the accounts to a local QA server.

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --check [connector ...]
.venv/bin/python scripts/qa_fixture_recorder.py --record [connector ...]
.venv/bin/python scripts/qa_fixture_recorder.py --lifecycle [connector ...]
```

- `--check` calls each connector's targeted reads against its seed artifact and prints a report. It
  never writes a file.
- `--record` makes the same calls, redacts identity fields (`redact()` and the connector-specific
  passes), de-identifies structural ids and URLs (`deidentify_structural_fields()`), and writes
  `tests/fixtures/live/<connector>/<method>.json` for every result that passed.
- `--lifecycle` runs for `calendar`, `confluence`, `grist`, `jira`, `miro`, `peek`, `salesforce` and `tasks` only (the comment above
  `LIFECYCLE_CHECKS` says why the others are excluded) and never writes a fixture. A cleanup that
  ran but left the object behind fails the run. Confluence, Grist, Miro and Salesforce have no cleanup step. The Miro lifecycle runs on `main`'s code only.

With no connector named, every connector implemented for the mode runs. `--report-file PATH` also
saves the report; `-v`/`--verbose` turns on DEBUG logging on stderr. The exit status is non-zero
when any result fails. An unknown connector name is reported on stderr and skipped.

## Reviewing recorded fixtures

Read every recorded fixture diff before committing it, however it was produced (by hand, by a
drift PR from the live check, or by a `qa-record-fixture.yml` dispatch). The diff must contain no
real account identifier, email address, display name, token, tenant or site URL, and no private
content. The redaction runs on every recording, but a field it does not know about passes through
unchanged; if the diff shows one, fix the redaction in `scripts/qa_fixture_recorder.py` before
committing, not after. A legitimate drift diff is a small shape change: a field added, renamed or
removed.

The committed fixtures are replayed through each client's parsers by the `TestLiveFixtureParsing`
classes (see [`tests/fixtures/live/README.md`](../tests/fixtures/live/README.md)).

## The QA server

`https://runner.privacyfence.eu` runs the organization server built from the latest `main`. It
exists so the maintainer can connect the QA accounts in a browser, which organization mode makes
possible for every service, and so the built-in apps can be tested against a public server. The
live checks copy the QA principal's tokens from its data directory.

- **Deploy.** `.github/workflows/deploy-qa-server.yml` builds the organization image on a push to
  `main` that touches the server, its container files or the workflow, and on a manual dispatch. The
  `deploy` job runs on the live runner and runs exactly `sudo -n /usr/local/sbin/pf-qa-deploy`, a
  root-owned script that takes no arguments; the `verify` job then checks the public address from the
  internet. It shares the runner with the live checks, so a deploy never overlaps one. The host files
  are in [`deploy/qa-runner/`](../deploy/qa-runner/README.md).
- **`/mcp` is blocked.** The proxy answers 404 for `/mcp` and every MCP OAuth endpoint, and a
  Cloudflare WAF rule blocks the same paths. No AI client can reach a public server that holds live QA
  tokens, and nothing on the server builds the QA principal's connectors, so nothing there uses or
  refreshes the tokens. The runner is their only user, which matters for Atlassian's rotating refresh
  tokens and for Telegram, whose session must not be used from two places at once.
- **Connecting accounts.** Sign in as `test@privacyfence.eu`, the server's only administrator, offer
  every service on Admin → Services, then connect on `/connect`: Google (all six), Slack, Salesforce,
  Atlassian and Miro through the relay with the built-in apps; Telegram with the phone number and code;
  Grist with its API key; Peek with the device sign-in at `peek-qa.privacyfence.eu`. Reconnecting on
  `/connect` re-mints a token. The server sets `PRIVACYFENCE_PREVIEW_SERVICES=*`, so a service whose
  tools have not shipped is connectable here.
- **How the runner gets the tokens.** `scripts/qa_runner_tokens.py pull` copies the QA principal's
  `credentials/` from `/home/pfrunner/qa-org-data/users/<id>/` into the checkout before the recorder;
  `push` writes back only the files the run changed, and skips one that was reconnected during the run.
  Set the repository variable `PF_QA_PRINCIPAL_ID` to the principal's folder name when more than one
  folder holds tokens; without it the script uses the single folder that does, and otherwise fails
  and names the candidates.
- **A new connector** is connected here first. See
  [Adding a connector](coding-and-testing-guidelines.md#3-adding-a-connector).

## Self-hosted runner

The runner also hosts [the QA server](#the-qa-server), where the QA accounts are connected.

### Runner requirements

A dedicated Linux host, not shared with other repositories or workloads. It is set up by hand; the
repository holds the workflows and the QA server's host files
([`deploy/qa-runner/`](../deploy/qa-runner/README.md)), never a secret.

- **The host.** A Hetzner Cloud server named `runner.privacyfence.eu`: Ubuntu 24.04 LTS, x86-64, at
  least 2 vCPU, 4 GB RAM and 40 GB disk, with unattended upgrades on.
- **Cloudflare.** The name is a proxied (orange-cloud) record in the `privacyfence.eu` zone, with SSL
  mode Full (strict) and a Cloudflare Origin CA certificate on the origin. Cloudflare's Bot Fight
  Mode is off for the zone, or a WAF skip rule covers `/.well-known/privacyfence-relay` and
  `/oauth/callback/*` on this host; otherwise the relay's proof fetch and the sign-in redirects are
  challenged. A WAF custom rule also blocks `/mcp` and the MCP OAuth endpoints, in case the proxy
  configuration is changed by mistake.
- **Firewall.** A Hetzner Cloud Firewall admits inbound TCP 443 from Cloudflare's published ranges
  (`https://www.cloudflare.com/ips-v4` and `-v6`) and nothing else: no 22, no 80. Outbound is open
  (GitHub, the provider APIs and `oauth.privacyfence.eu`).
- **Administration over Tailscale only.** The Cloudflare proxy does not carry SSH. cloud-init joins
  the tailnet at first boot with a one-off, pre-approved auth key and runs `tailscale up --ssh`; the
  tailnet ACL lets only the maintainer reach the host; OpenSSH is disabled
  (`systemctl disable --now ssh`). Keep the auth key out of the repository and out of every doc. No
  token is ever moved by hand.
- **Users and Docker.** The runner service runs as `pfrunner` (uid and gid 10001, the image's
  `privacyfence` user), which is not in the `docker` group, because that group is root-equivalent.
  Its only sudo rule runs the QA server's no-argument deploy script. The QA server and Peek run as
  containers of one compose project and share exactly one directory with `pfrunner`: the server's
  data directory, `/home/pfrunner/qa-org-data` (mode 0700). The live checks need no Docker.
- **The GitHub runner.** It is registered as `runner-privacyfence-eu` with the labels
  `self-hosted,privacyfence-live`, in the organization runner group `privacyfence-live` (not to this
  repository). The group allows public repositories but is set to **Selected workflows**:
  `connector-live-check.yml`, `qa-record-fixture.yml` and `deploy-qa-server.yml`, as they stand on
  `main`, so a pull request, a fork or any other branch's copy of a workflow can never be scheduled
  on it. GitHub accepts only a workflow that exists on `main`, so add `deploy-qa-server.yml` once it
  has merged. Fork pull requests from outside collaborators also require approval before any workflow
  runs. It is installed as a service in persistent (non-ephemeral) mode, because an `--ephemeral`
  registration deregisters itself after one job.
- Python 3.11 or newer as `python3`, with a working `venv` module (on Debian/Ubuntu, the
  `pythonX.Y-venv` package matching `python3 --version`), and the build prerequisites of
  PrivacyFence's dependencies.

If the repository restricts allowed actions, `peter-evans/create-pull-request` must be on the
allowlist for the drift PR step, which runs in `connector-live-check.yml`'s `open-drift-pr` job on a
GitHub-hosted runner, not on this host.

### Organization mode and the `qa-live` principal

The recorder runs in organization mode as the fixed principal `qa-live`, so it needs no change when
local mode is removed, and it tests the apps most people use. `_qa_context()` enters that principal's
scope once, before any connector is built, and every token path then resolves to
`users/qa-live/credentials/` of the checkout, in the same file formats as before. `_qa_org_config()`
builds the connector configuration the way an organization server does, offering every service:

- the Google, Slack, Salesforce, Atlassian and Miro sections are the built-in apps, unless the
  runner's optional bundle carries an organization's own app to test;
- Peek's and Grist's addresses come from `PRIVACYFENCE_PEEK_BASE_URL`
  (`https://peek-qa.privacyfence.eu`) and `PRIVACYFENCE_GRIST_SERVER_URL`
  (`https://docs.getgrist.com`), set in both workflows.

The built-in credentials are written for the run only. Both workflows have a "Write built-in app
credentials" step that runs `scripts/builtin_apps.py write` with the ten connector `PF_APP_*`
secrets (Google web, Slack, Salesforce, Atlassian relay and Miro, each an ID and a secret; not the
Google sign-in client) in that step's environment and nowhere else. The last step of the job, which
always runs, deletes `src/privacyfence/_builtin_apps.py`, because the runner is not ephemeral. Only
the client ID and secret are used: refreshing a token needs no redirect.

Local-mode loopback sign-in, and an organization's own app unless the optional bundle carries one,
are no longer live-tested. Unit tests cover both.

### Connecting the QA accounts

All in a browser, on [the QA server](#the-qa-server): nobody copies an authorization URL, opens a
tunnel or places a token file by hand.

1. Sign in at `https://runner.privacyfence.eu` as `test@privacyfence.eu`, the server's only
   administrator and the account whose tokens the runner uses.
2. On Admin → Services, offer every service.
3. On `/connect`, connect Google (all six), Slack, Salesforce, Atlassian and Miro through the relay
   with the built-in apps; Telegram with the phone number and code; Grist with its API key; and Peek
   with the device sign-in at `peek-qa.privacyfence.eu`.

Before each run the workflows run `scripts/qa_runner_tokens.py pull --data-dir "$HOME/qa-org-data"`,
which copies the QA principal's `credentials/` from the server's data directory to
`users/qa-live/credentials/` of the checkout (files mode 0600) and records each file's hash. The
principal's folder is the one named by the repository variable `PF_QA_PRINCIPAL_ID` when it is set;
otherwise the single folder under `users/` that holds a token file, and with none or several the step
fails and names the candidate ids. After the run, `push` writes back only the files the run changed,
and skips one that was reconnected on the server meanwhile.

### Persistent QA state

The tokens are not kept in the home directory of `pfrunner`: the QA server's data directory holds
them, and the copy steps above move them. What the runner user's `~/privacyfence` holds is the
configuration the workflows copy into their fresh checkout at the start of every run:

```text
~/privacyfence/
├── org/
│   └── org_bundle.json       # optional: an organization's own app to test
└── tests/
    └── fixtures/
        └── qa_environment.yaml
```

`qa_environment.yaml` is required (see [Seed data](#seed-data)); the run fails with
`##[error] … not found` without it. `org/org_bundle.json` is copied only when present. Neither is a
secret in the sense of a token, but keep `~/privacyfence` owned by the runner user, and a bundle's
client secrets with it. `~/privacyfence` is not a git clone and holds no code or venv. No step
writes `users/` by hand.

At the end of every run, whatever its outcome, `qa_runner_tokens.py push` writes the refreshed tokens
back. Clients save a refreshed OAuth token in place, and Atlassian issues a new refresh token on every
refresh, so without the write-back the next run would use an already-spent token.

### Scheduled live check

`.github/workflows/connector-live-check.yml` runs weekly (Monday 06:00 UTC) and on
`workflow_dispatch`, with a 20-minute timeout. It:

1. checks out the repository (full history) and builds a fresh `.venv` from the hash-pinned lock
   (`pip install --require-hashes -r requirements/dev.lock.txt`, then `pip install --no-deps -e .`);
2. writes the built-in app credentials;
3. copies the QA tokens and configuration into the checkout;
4. runs `--check` with `continue-on-error`, since drift is a finding, not a crash;
5. if the check failed, runs `--record`;
6. runs `--lifecycle`, which fails the job on any failure;
7. if the check failed, the separate `open-drift-pr` job (on a GitHub-hosted runner) opens or updates a PR from `chore/connector-live-fixture-drift` that
   contains only `tests/fixtures/live/**`, with the record report as its body;
8. fetches `https://oauth.privacyfence.eu/health` with `continue-on-error` and reports `relay: up` or
   `relay: down` in the job summary, which makes the run a weekly external check of the relay;
9. writes the refreshed tokens back (always);
10. uploads `/tmp/qa-*-report.md` as the `connector-live-check-report` artifact, kept for 90 days
    (always);
11. deletes the built-in app credentials (always, and last).

The drift PR goes through the normal credential-free `tests.yml` gate and the fixture review
above.

### Recording one connector

`.github/workflows/qa-record-fixture.yml` (`workflow_dispatch` only, dispatched **on `main`**, inputs
`connector`, default `apps_script`, and `branch`, the pushed branch to record for) records one
connector's fixture and commits it to that branch. Use it to record the first fixture for a newly added `check_<connector>()`, which the live
check never records because its `--check` does not fail. It:

- the `record` job (self-hosted, read-only `contents`) refuses to run unless dispatched on `main`,
  refuses a `branch` that is `main`, `stable`, `releases/*` or starts with `refs/` or `pull/`, and
  one that does not exist on `origin` as a branch, so no pull-request ref (fork code included) can
  reach the runner; the branch is passed through an environment variable, never interpolated into a
  script;
- writes the built-in app credentials and copies the QA tokens and configuration in, as the live
  check does;
- fails unless the connector is registered in `CONNECTOR_CHECKS`;
- runs `--record <connector>` without `continue-on-error`, so a refused recording fails the job;
- writes the refreshed tokens back, uploads the report as `qa-record-report-<connector>` and the
  recorded `tests/fixtures/live/<connector>` as the `recorded-fixture` artifact, then deletes the
  built-in app credentials;
- the `commit-fixture` job (`ubuntu-latest`, the only one with `contents: write`) checks out
  `branch`, restores that artifact, commits as `github-actions[bot]` and pushes, skipping when
  nothing changed.

It shares the `connector-live-check` concurrency group with the live check (no cancellation), so
the two never race on the tokens. A queued run is waiting for the other one. GitHub
offers `workflow_dispatch` only for workflows present on the default branch.

### Rotating credentials

When a QA grant is revoked or expires, or on a regular rotation:

1. reconnect the account on the QA server's `/connect` page, signed in as `test@privacyfence.eu`;
2. revoke the old grant in the provider's console;
3. dispatch `connector-live-check.yml` and review any drift PR it opens.

Never add a QA credential as a GitHub Actions secret as a workaround.

### Cutover and decommissioning

The workflows move to the new label together, in one change: until it merges, the old runner keeps
serving `main`'s workflows and the new one sits idle. After it merges:

1. connect every QA account on the QA server (above). Until then the live check fails at its token
   copy; the fix is to finish connecting, not to point the workflows back;
2. dispatch `connector-live-check.yml` and `qa-record-fixture.yml` on `main` and confirm a green run;
3. remove the old runner, its organization runner group (named after its old label) and its QA
   state, and revoke the grants the old runner held, in each provider's console.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The run stays queued | The runner is offline, not registered to this repository's organization, or missing the `self-hosted`/`privacyfence-live` labels; the workflow is not in the group's selected workflows; or the other QA workflow, or a QA server deploy, holds the runner. |
| The runner disappears after one job | It was registered with `--ephemeral`. Re-register without it and reinstall the service. |
| `python3 -m venv` fails with "ensurepip is not available" | Install the `pythonX.Y-venv` package for the runner's Python. |
| `##[error] ~/privacyfence/… not found` | The service user or its home directory differs from the owner of `~/privacyfence`, or `tests/fixtures/qa_environment.yaml` from [Persistent QA state](#persistent-qa-state) is missing. |
| `qa_runner_tokens.py pull` names several candidate ids, or none | More than one `users/` folder holds tokens (set the repository variable `PF_QA_PRINCIPAL_ID` to the right folder name), or the QA accounts are not connected yet. |
| `manifest not found` from the recorder | `qa_environment.yaml` was not copied in; see [Seed data](#seed-data). |
| A connector reports no organization config | Its `PF_APP_*` secrets are missing, so the built-in app was not written; check the "Write built-in app credentials" step. For Peek or Grist, check the two address variables in the workflow. |
| A result says the fetched object "does not carry [QATEST]" | The manifest id points at the wrong object, or the seed artifact was renamed. Repair the artifact or the id. |
| `Telegram app credentials not available in this build.` | The `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` repository secrets are missing. An error naming `telegram.session` instead means Telegram is not connected on the QA server. |
| Atlassian `401`/`403` while refreshing the token | Check that the write-back step ran. A spent Atlassian refresh token needs a fresh reconnect on `/connect`; a rerun will not fix it. |
| `--lifecycle` shows `n/a` under Cleanup for Confluence, Grist or Salesforce | Expected; see [Seed: Confluence](#seed-confluence). Grist and Salesforce have no delete either. |
| The summary says `relay: down` | The relay at `oauth.privacyfence.eu` did not answer its health endpoint. See [`oauth-relay.md`](oauth-relay.md). It does not fail the job. |

The runner holds real QA grants: patch it, watch its health, require review on changes to the two
workflows above, and revoke every QA grant if the host is compromised.

## Exploratory connector and gate QA

A human-driven pass through a live MCP client (Claude Desktop or Cowork) connected to a running
PrivacyFence daemon, against the QA accounts, watching what the approval gate does. Deterministic
gate-state coverage (auto/review/popup, Allow/Deny, PII confirmation, Always allow, unattended
sessions) belongs to `tests/unit/test_gate.py`, and rule-by-rule verdicts with real rules to
`tests/unit/test_gate_real_evaluator.py`; do not repeat those by hand. This pass checks what they
cannot: that each tool is wired to the gate and metadata its connector intends, against real
provider responses, and that the approval card is usable.

Run it for:

- a new connector, before its first release;
- a material change to a connector's client, tool surface or gate wiring;
- an integration regression that needs a live account to reproduce;
- a broad change to `gate.py`, `auto_accept.py`, `policy/resource_registry.py` or the web approval
  UI (see [`release-testing.md`](release-testing.md)).

Before starting: seed the environment as above, authenticate only QA accounts, run the source
checkout or package under test, and tag every artifact you create with a run identifier (for
example `[QATEST 2026-09-25]`) so repeated runs never produce indistinguishable leftovers. Start the
affected connectors with `--check` and, for write-capable ones, `--lifecycle`.

### Checks for every connector

For each representative read, list, search, create, update, send or upload tool:

- the call targets the intended QA resource and account;
- the returned content and metadata are parsed correctly;
- provider errors come back in the connector's safe error shape, and pagination and empty results
  behave sensibly;
- no credential, token or internal state appears in MCP-visible output;
- the gate (`auto`, `review` or `popup`) and the card's metadata match the connector's contract;
- Deny prevents execution or release of the content;
- the audit entry records the right connector, tool, decision, rule and principal.

### Gmail checks

- **Read and deny.** `gmail_get_message` prompts for review; Deny returns an error, not data. Use a
  short message, so a denial cannot be mistaken for a size truncation.
- **Trusted sender domain.** With the `trusted_sender_domain` rule from
  [Seed: Gmail](#seed-gmail), a message from that domain, and from a subdomain of it, reads without
  a card.
- **Always allow on a write.** `gmail_add_label` with a fresh `PrivacyFence QA <run>` label offers
  Always allow proposing `label_name_allowlist` scoped to that label name only. Remove the rule it
  creates afterwards.
- **Drafts.** `gmail_create_draft` with `body_markdown` shows the raw Markdown on the card, and the
  saved draft renders as HTML in Gmail. `gmail_reply_all_draft`'s card lists every thread
  participant.

### Drive checks

- **Trusted folder.** With the `approved_folder` rule from [Seed: Drive](#seed-drive),
  `drive_get_file_content` on a file directly in the sandbox folder reads without a card.
- **Temp-accept window.** Allow once on `drive_add_comment`, `drive_sheets_write_range` or
  `drive_sheets_format_range` shows a disclosure caption, and a second call of the same operation
  on the same file within 300 seconds is auto-accepted with rule `session_temp_accept`.
  `drive_sheets_add_sheet`, `drive_sheets_rename_sheet`, `drive_sheets_delete_dimensions`,
  `drive_write_file_content`, `drive_write_doc_content`, `drive_upload_file`, `drive_copy_file` and
  `drive_move_file` never arm it.
- **Copy.** `drive_copy_file` on a QA file into a subfolder of the `PrivacyFence QA Sandbox`
  folder shows a card with the file, its current → destination folder and the copy's name. Approve
  it, then confirm the copy appears in the subfolder and the original stays where it was.
- **Always allow on a read.** Always allow on `drive_sheets_get_values` for a sheet you own
  proposes `i_am_owner`; if the sheet is also in an approved folder, the card offers a separate
  folder button rather than picking one.
- **PII on reads only.** Write a Doc containing obviously fake PII (for example a US SSN
  `123-45-6789` or a UK NI number `AB123456C`) into a subfolder of the sandbox folder: the write
  card is plain and the audit entry has `pii_detected: false`. Reading it back shows a tinted card
  listing the categories, then an "Are you sure?" confirmation, and the audit entry has
  `pii_detected: true`. Email addresses and phone numbers are never flagged.
- **PII overrides a matching rule.** The same fake-PII Doc placed directly in the approved folder
  still prompts on read, with decision `approved` and an empty `auto_accept_rule` in the audit
  entry, where the plain file above was `auto_accepted`.

### Slack checks

- **Approved channel.** `slack_get_channel_history` on the approved channel reads without a card;
  on `privacyfence-qa-control` it prompts.
- **Search.** `slack_search_messages` whose results all come from the approved channel reads
  without a card; one unapproved result gates the whole search.
- **Recent window.** `slack_search_messages` with `minutes` and no query returns the
  channel, DM, group DM and thread messages posted in that window, newest first, and the card's
  **Since** reads `last N minutes`; `slack_get_channel_history` with `minutes` returns only
  that window.
- **Member conversations.** With a 'conversations I'm a member of' rule,
  `slack_get_channel_history` on `privacyfence-qa-control` reads without a card; a `member_only`
  search of the last 60 minutes reads without a card; the same search without `member_only` prompts.
- `slack_get_thread_replies` on the seed thread prompts for review; `slack_send_message` to your
  own DM prompts.

### Calendar checks

- **Own events.** With the `i_am_organizer` rule, `calendar_get_event_details` on an event you
  created on the PFQA calendar reads without a card.
- **Visibility.** `calendar_get_event_visibility` is silent; `calendar_set_event_visibility` is
  evaluated by the same rules as `calendar_update_event` whatever value is requested, and an
  invalid value is rejected before any card appears.
- `calendar_list_rooms` returns the synced rooms, or an empty list without them.

### Contacts checks

- **Contact-info edits.** With the `no_contact_info_change` rule, `contacts_update` that changes
  only a name or note on a contact you created is auto-accepted; one that changes an email or phone
  still prompts.
- `contacts_get` with a `source` that does not match the contact fails with an error instead of
  returning the contact.

### Tasks checks

- **Approved list.** With the `approved_task_list` rule, creating or updating a task in
  `PrivacyFence QA List` is auto-accepted; the same call in `PrivacyFence QA Contrast List` prompts.
- **Moves.** `tasks_move_task` is auto-accepted only when both the source and destination list are
  approved.

### Telegram checks

- **Approved chat.** `telegram_get_messages` on the approved chat reads without a card; on another
  chat it prompts. The tool result alone can be ambiguous, so confirm with the audit entry's
  `decision`.
- **Search.** `telegram_search_messages` shares `telegram.read_chat_messages`: confined to the
  approved chat it reads without a card; reaching another chat it prompts.

### Salesforce checks

- **Approved report and object type.** `salesforce_run_report` on the QA report and
  `salesforce_get_record` on an Account read without a card with the rules from
  [Seed: Salesforce](#seed-salesforce); another report or object type prompts.
- **Search scope.** `salesforce_search` scoped to `object_types="Account"` matches
  `approved_object_types`; an unscoped search never does. `account_id` without `object_types` is
  rejected before any card appears.
- **Report overrides.** `salesforce_run_report` with `columns`, `filters` or `summary_only` narrows that run only. The approved-report rule still reads it without a card, and `--check`'s `run_report` row covers the three live.
- **Report paging.** `--check`'s `run_report_page` row covers `sortBy`, `greaterThan`, the `RowCount` aggregate and flattening a summary report live, on the QA report's `PF QA Number` column. It records only the `[QATEST]` rows of the first page.
- **Lifecycle.** `--lifecycle` creates and updates Tasks (`lifecycle_object_type`, `lifecycle_name_field`, `lifecycle_note_field`) tagged `[QATEST-LIFECYCLE]`: one single-record create and update, then two records in one batch. It never deletes them, so they are deleted by hand now and then.

### Grist checks

- **Connect.** Connecting with an API key and signing in with the OAuth app both succeed, and the
  popup cards for the four writes (`grist_add_records`, `grist_update_records`,
  `grist_create_table`, `grist_add_columns`) name the server.
- **CSV cards.** The popup cards of `grist_import_csv` and `grist_update_csv` name the server and
  show the counts and the sample.
- **Review card.** `grist_get_records` on the `QaSeed` table prompts for review.
- **Paging.** `--check`'s `get_records_paged` row reads `QaSeed` one row per page with `after_id`
  and compares it with one read, so it needs the two seed rows. It records nothing.
- **SQL.** `--check`'s `run_sql` row runs `SELECT id FROM QaSeed ORDER BY id` through Grist's SQL
  endpoint and compares the ids with one records read. It runs with the QA API key, so it does not
  show whether an OAuth sign-in may use `/sql`. It records nothing.
- **Approved document.** With the `grist.document` rule from Settings (see
  [Seed: Grist](#seed-grist)), a read of that document passes without a card; another document
  prompts.
- **Listing.** `grist_list_documents` lists the QA documents. Where the server refuses it under
  OAuth, the tool answers with that refusal message.

### Miro checks

- **Sign-in.** Signing in from Settings succeeds and the connector shows the team.
- **Review cards.** `miro_list_items`, `miro_get_items` and `miro_list_connectors` on the seed board
  prompt for review.
- **Popup cards.** `miro_create_items`, `miro_create_connectors`, `miro_update_items` and
  `miro_update_board` show a popup card with its table (new items, or old → new values).
- **Batch.** `miro_create_items` with 20 items shows all 20 on the card and creates them.
- **Approved board.** With the `miro.board` rule from Settings (see [Seed: Miro](#seed-miro)), a
  read of that board passes without a card; another board prompts.
- **Board link.** A board link is refused as a `board_id` and as a rule value.
- **Other team.** A board in another team answers 403, and the tool says so.

### Peek checks

- **Connect.** Signing in with the device flow works in Settings and on `/connect`, and the
  password is never shown.
- **Review cards.** `peek_get_comments`, `peek_get_stats` and `peek_list_grants` prompt for review; the stats card shows
  visitor names and times, with no IP address.
- **Popup cards.** `peek_upload_page`, `peek_set_visibility`, `peek_grant_access`,
  `peek_revoke_access`, `peek_rename_page` and `peek_delete_page` each name the server.
  `peek_list_versions` runs without a card (auto).
- **Comments.** `peek_add_comment` posts under the QA account's name, not as `anonymous`. A Peek server
  without the account comment endpoint answers 404 or 405, and the tool says so.

### Jira checks

- **Approved project.** With the `approved_project_keys` rule, `jira_get_issue` on a `PFQA` issue
  reads without a card; an issue in another project prompts.
- **Own issues.** An issue whose reporter or assignee is your Atlassian account (matched by
  account id, never by a name or email) reads without a card through `i_am_reporter` or
  `i_am_assignee`, independently of the project rule.
- `jira_transition_issue` with a transition name that is not available fails with an error listing
  the available transitions.
- **Mentions.** Reading an issue whose description or a comment @mentions someone shows their name, not a bare
  account id. `jira_find_users` finds a person by name and returns no
  email.

### Confluence checks

- **Approved space.** With the `approved_space_keys` rule, `confluence_get_page` on a `PFQA` page
  reads without a card; a page in another space prompts.
- **Own pages.** A page whose author is your Atlassian account (matched by account id) reads without a card through `i_am_author`, independently of
  the space rule.
- `confluence_download_attachment` prompts for review and saves a file whose size matches
  `confluence_list_attachments`.
- **Mentions.** Reading a page that @mentions someone, or that has an author, shows their name.
  `confluence_find_users` finds a person by name and returns no email.

### Approval UI checks

For changes to card content or connector metadata, in the browser approval surface:

- the list row identifies the connector and tool;
- Review opens the expected card, and its target, details and preview are enough to decide;
- Allow and Deny produce the matching connector result;
- Always allow creates only the intended, scoped rule;
- notification and list summaries expose no more content than the configured detail level.

### Connector authorization checks

When authentication code changes, connect, reconnect and revoke with the QA account. In org mode,
confirm authorization is scoped to the signed-in principal and that reconnecting rebuilds that
principal's connector host with the new credentials. Provider consent screens stay a human check.

### Recording results

Record the commit or package tested, the account type, the operations exercised and any provider
drift or unexpected behavior in the issue or PR the investigation belongs to. Delete what the run
created where a tool allows it; the rest (calendar out-of-office and working-location entries,
Gmail filters and labels, contacts) is tagged with the run identifier for manual cleanup.
