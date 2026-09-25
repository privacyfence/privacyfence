# Connector QA

How to stand up and run live connector QA: the dedicated QA accounts and their seed data, the
recorder that checks and records provider responses (`scripts/qa_fixture_recorder.py`), the
self-hosted runner that runs it on a schedule, and the exploratory pass that drives connector tools
and the approval gate by hand. When each of these is required is policy: the live tier and the
`--check` report a connector PR owes are in
[`testing-policy.md`](testing-policy.md#layer-5-live-connector), and when the exploratory pass is
due is in [`release-testing.md`](release-testing.md).

## Credential boundary

Live connector credentials — the per-account OAuth token files, the Telegram session and the QA
`org_config.json` — exist only as local files on a developer's own machine or on the
project-controlled self-hosted runner labelled `privacyfence-test`. They are never stored as GitHub
Actions secrets and never reach a GitHub-hosted runner or a `pull_request`-triggered workflow. Both
workflows that use them (`connector-live-check.yml`, `qa-record-fixture.yml`) trigger only on
`schedule` or `workflow_dispatch`. The reasoning is in
[ADR 0019](adr/0019-live-connector-credentials-only-on-a-self-hosted-runner.md).

Telegram's `api_id`/`api_hash` are not a per-account credential: they are the one shared app
identity every release build carries. `connector-live-check.yml` maps the repository secrets
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` to `PRIVACYFENCE_TELEGRAM_API_ID`/
`PRIVACYFENCE_TELEGRAM_API_HASH`. The per-account Telegram session stays runner-local with the
other credentials.

## QA accounts and organization config

Use dedicated accounts that share nothing with a personal or production identity:

| Service | Account | Notes |
|---|---|---|
| Google | A dedicated Google Workspace account, ideally on its own domain | A consumer Gmail account covers every Google connector except `calendar_list_rooms`, which needs Workspace admin rights (see [Seed: Calendar](#seed-calendar)). |
| Slack | A Slack Developer Program sandbox | Register the app inside the sandbox and never activate public distribution (see [`slack-setup.md`](slack-setup.md)). Sandboxes expire; check the expiry date whenever you rotate credentials and extend it from **Sandboxes → Extend**. |
| Atlassian | A free Jira + Confluence Cloud site | One OAuth app covers both products. |
| Salesforce | A Developer Edition org | Use `--salesforce-login-url https://login.salesforce.com` when building the bundle. |
| Telegram | Your own account | No dedicated account; `privacyfence-app --telegram-setup` signs in with a phone number and code. |

For each of Google, Slack, Atlassian and Salesforce, follow the "For IT admins" section of
[`google-cloud-setup.md`](google-cloud-setup.md), [`slack-setup.md`](slack-setup.md),
[`atlassian-setup.md`](atlassian-setup.md) and [`salesforce-setup.md`](salesforce-setup.md), and
merge every client id/secret into one QA bundle with `scripts/build_org_bundle.py --merge` (for
example `-o org_config.qa.json`). Keep it separate from any production bundle. It is installed as
`org/org_config.json`, by `scripts/qa_authenticate_connectors.py --org-config` (see
[Authenticating connectors](#authenticating-connectors)) or by **Install/Update Organization
Config…** in PrivacyFence Settings.

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

`calendar_list_rooms` returns the `rooms` list in `org_config.json` and makes no live call. To
cover it, you need Workspace admin rights: create a calendar resource (Admin console → Directory →
Buildings and resources), then run `scripts/sync_room_directory.py` from a second Cloud project as
described in [`google-cloud-setup.md`](google-cloud-setup.md)'s "Room directory sync" section, and
reinstall the refreshed bundle. Without it the tool returns an empty list, not an error.

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

### Seed: Salesforce

- [ ] Create two or three Account records named like `PrivacyFence QA — Acme Test Co [QATEST]` and
      `PrivacyFence QA — Globex Test Co [QATEST]`.
- [ ] Create a report on Accounts named exactly `PrivacyFence QA Report`.
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
| `slack` | `channel_name`, `channel_id`, `seed_thread_ts` | `get_thread_replies.json` | — |
| `calendar` | **`calendar_id`**, `seed_event_title`, `seed_event_id` | `get_event.json` | creates, updates and deletes one event and one two-instance recurring series on `calendar_id` |
| `contacts` | `seed_contact_display_name`, `seed_contact_resource_name` | `get_contact.json` | — |
| `tasks` | **`task_list_id`**, **`seed_task_id`** | `get_task.json` | creates, updates and deletes one task in `task_list_id` |
| `apps_script` | `seed_script_title`, `seed_script_id` | `get_content.json` | — |
| `telegram` | `chat_id`, `history_limit` | `get_messages.json` | — |
| `salesforce` | `report_name`, `report_id`, `object_type`, `seed_record_name`, `seed_record_id` | `list_reports.json`, `get_record.json` | — |
| `jira` | `project_key`, `seed_issue_summary`, `seed_issue_key` | `list_projects.json`, `get_issue.json` | creates, updates and deletes one issue in `project_key` |
| `confluence` | `space_key`, `seed_page_title`, `seed_page_id` | `list_spaces.json`, `get_page.json` | creates and updates one page in `space_key`; never deletes it |

The recorder's `CONNECTOR_CHECKS` and `EXPECTED_FIXTURES` registries list these connectors; a
connector added to one without the other fails at import time.

## Authenticating connectors

With the QA bundle installed, authenticate each connector against its QA account:

```bash
.venv/bin/python scripts/qa_authenticate_connectors.py --org-config org_config.qa.json
```

The script runs `privacyfence.daemon_main --config config/settings.yaml --<connector>-oauth` for
Gmail, Drive, Calendar, Contacts, Tasks, Apps Script, Slack, Atlassian (one flow for Jira and
Confluence) and Salesforce, in that order. Each step opens a browser tab for consent.

| Option | Effect |
|---|---|
| `--org-config PATH` | Installs `PATH` as `org/org_config.json` first, backing up an existing file as `org_config.json.bak.<UTC timestamp>`. |
| `--only NAME ...` | Runs only these groups (`google`, `slack`, `atlassian`, `salesforce`) or connectors (`gmail`, `drive`, `calendar`, `contacts`, `tasks`, `apps_script`, `slack`, `atlassian`, `salesforce`). |
| `--continue-on-error` | Runs every requested step instead of stopping at the first failure. |
| `--config PATH` | Settings file passed through to the daemon. Default: `config/settings.yaml`. |

Telegram is not included; run `privacyfence-app --telegram-setup` by hand (see
[`telegram-setup.md`](telegram-setup.md)). Tokens are written to the git-ignored `credentials/`
directory under the file names in `daemon_main.TOKEN_FILES` (`token.json` for Gmail,
`<connector>_token.json` for the others, `atlassian_token.json` shared by Jira and Confluence,
`telegram.session`).

## Running the recorder

Always use the project venv; the recorder imports the same third-party clients as the daemon.

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
- `--lifecycle` runs for `calendar`, `confluence`, `jira` and `tasks` only (the comment above
  `LIFECYCLE_CHECKS` says why the others are excluded) and never writes a fixture. A cleanup that
  ran but left the object behind fails the run.

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

## Self-hosted runner

### Runner requirements

A dedicated Linux host or VM, not shared with other repositories or workloads:

- a GitHub Actions runner registered to this repository with the label `privacyfence-test`;
- installed as a service in persistent (non-ephemeral) mode — an `--ephemeral` registration
  deregisters itself after one job;
- running as a dedicated unprivileged user whose home directory holds the persistent QA state;
- Python 3.11 or newer as `python3`, with a working `venv` module (on Debian/Ubuntu, the
  `pythonX.Y-venv` package matching `python3 --version`);
- the build prerequisites of PrivacyFence's dependencies;
- outbound HTTPS to GitHub and the provider APIs (no inbound ports are needed);
- OS updates enabled and key-only SSH.

If the repository restricts allowed actions, `peter-evans/create-pull-request` must be on the
allowlist for the drift PR step.

### Persistent QA state

Both workflows copy the runner user's `~/privacyfence` into their fresh checkout at the start of
every run, and fail with `##[error] … not found` if any of these is missing:

```text
~/privacyfence/
├── credentials/              # token files from "Authenticating connectors", plus telegram.session
├── org/
│   └── org_config.json       # the QA bundle, under exactly this name
└── tests/
    └── fixtures/
        └── qa_environment.yaml
```

The copy lands at the same relative paths in the checkout, because a source checkout resolves
`credentials/` and `org/` relative to the repository root. `~/privacyfence` is not a git clone and
holds no code or venv. Keep it owned by the runner user and `chmod 600` the credential files.

At the end of every run, whatever its outcome, the workflow copies `credentials/` back into
`~/privacyfence/credentials/`. Clients save a refreshed OAuth token in place, and Atlassian issues a
new refresh token on every refresh, so without the copy-back the next run would use an
already-spent token.

### Scheduled live check

`.github/workflows/connector-live-check.yml` runs weekly (Monday 06:00 UTC) and on
`workflow_dispatch`, with a 20-minute timeout. It:

1. checks out the repository (full history) and builds a fresh `.venv` with
   `pip install -e ".[test]"`;
2. copies the persistent QA state into the checkout;
3. runs `--check` with `continue-on-error`, since drift is a finding, not a crash;
4. if the check failed, runs `--record`;
5. runs `--lifecycle`, which fails the job on any failure;
6. if the check failed, opens or updates a PR from `chore/connector-live-fixture-drift` that
   contains only `tests/fixtures/live/**`, with the record report as its body;
7. copies refreshed credentials back (always);
8. uploads `/tmp/qa-*-report.md` as the `connector-live-check-report` artifact, kept for 90 days
   (always).

The drift PR goes through the normal credential-free `tests.yml` gate and the fixture review
above.

### Recording one connector

`.github/workflows/qa-record-fixture.yml` (`workflow_dispatch` only, input `connector`, default
`apps_script`) records one connector's fixture and commits it to the branch it was dispatched
against. Use it to record the first fixture for a newly added `check_<connector>()`, which the live
check never records because its `--check` does not fail. It:

- refuses to run on `main` or a `releases/*` branch, because it pushes directly to the branch;
- fails unless the connector is registered in `CONNECTOR_CHECKS`;
- runs `--record <connector>` without `continue-on-error`, so a refused recording fails the job;
- stages only `tests/fixtures/live/**`, commits as `github-actions[bot]` and pushes;
- copies refreshed credentials back and uploads the report as `qa-record-report-<connector>`.

It shares the `connector-live-check` concurrency group with the live check (no cancellation), so
the two never race on the credential store. A queued run is waiting for the other one. GitHub
offers `workflow_dispatch` only for workflows present on the default branch.

### Rotating credentials

When a QA grant is revoked or expires, or on a regular rotation:

1. re-authenticate the connector against its QA account
   (`scripts/qa_authenticate_connectors.py --only <name>`, or `--telegram-setup`);
2. replace the file under `~/privacyfence/credentials/`, keeping its ownership and permissions;
3. revoke the old grant in the provider's console;
4. dispatch `connector-live-check.yml` and review any drift PR it opens.

Never add a QA credential as a GitHub Actions secret as a workaround.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The run stays queued | The runner is offline, not registered to this repository, or missing the `self-hosted`/`privacyfence-test` labels; or the other QA workflow holds the concurrency group. |
| The runner disappears after one job | It was registered with `--ephemeral`. Re-register without it and reinstall the service. |
| `python3 -m venv` fails with "ensurepip is not available" | Install the `pythonX.Y-venv` package for the runner's Python. |
| `##[error] ~/privacyfence/… not found` | The service user or its home directory differs from the owner of `~/privacyfence`, or a file from [Persistent QA state](#persistent-qa-state) is missing. |
| `manifest not found` from the recorder | `qa_environment.yaml` was not copied in; see [Seed data](#seed-data). |
| A result says the fetched object "does not carry [QATEST]" | The manifest id points at the wrong object, or the seed artifact was renamed. Repair the artifact or the id. |
| `Telegram app credentials not available in this build.` | The `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` repository secrets are missing. An error naming `credentials/telegram.session` instead means the session file is missing from `~/privacyfence/credentials/`. |
| Atlassian `401`/`403` while refreshing the token | Check that the copy-back step ran and that the runner user can write to `~/privacyfence/credentials/`. A spent Atlassian refresh token needs a fresh reconnect; a rerun will not fix it. A `403` from the token endpoint can also mean the client id/secret in `org_config.json` does not match the app at developer.atlassian.com. |
| `--lifecycle` shows `n/a` under Cleanup for Confluence | Expected; see [Seed: Confluence](#seed-confluence). |

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
  `drive_write_file_content`, `drive_write_doc_content`, `drive_upload_file` and `drive_move_file`
  never arm it.
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

### Jira checks

- **Approved project.** With the `approved_project_keys` rule, `jira_get_issue` on a `PFQA` issue
  reads without a card; an issue in another project prompts.
- **Own issues.** An issue you created reads without a card through `i_am_reporter` or
  `i_am_assignee`, independently of the project rule.
- `jira_transition_issue` with a transition name that is not available fails with an error listing
  the available transitions.

### Confluence checks

- **Approved space.** With the `approved_space_keys` rule, `confluence_get_page` on a `PFQA` page
  reads without a card; a page in another space prompts.
- **Own pages.** A page you created reads without a card through `i_am_author`, independently of
  the space rule.
- `confluence_download_attachment` prompts for review and saves a file whose size matches
  `confluence_list_attachments`.

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
