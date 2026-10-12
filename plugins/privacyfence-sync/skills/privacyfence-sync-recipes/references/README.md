# privacyfence-sync

`privacyfence-sync` runs a data-sync recipe against PrivacyFence with no AI model involved. A recipe
reads data from your connected services (Salesforce reports, Jira searches, Grist tables, files in
Google Drive), runs your own scripts on the files, writes the result back to Grist, and can publish
an HTML dashboard on Peek.

The intended split of work:

- **Designing** a sync is a job for Claude: the Grist data model, which sources to read, how to
  diff and transform them, what the dashboard shows. Claude writes the recipe, the scripts and the
  dashboard template, and you review them like any code change.
- **Running** the sync on a schedule is a job for `privacyfence-sync`. It makes the same tool calls
  Claude would make, in a fixed order, and runs your scripts between them. It costs no model tokens.

It is an ordinary MCP client of PrivacyFence, like Claude Code. Every call goes through
PrivacyFence's gate, rules and audit log, exactly as Claude's calls do. `privacyfence-sync` never
holds your Salesforce, Jira, Grist, Drive or Peek credentials, cannot approve anything, and cannot
change your rules. A scheduled run declares itself unattended, so any step that no auto-accept rule
covers fails at once instead of waiting for an approval nobody will give.

## Contents

- [Install](#install)
- [Connecting to PrivacyFence](#connecting-to-privacyfence)
- [The rules a recipe needs](#the-rules-a-recipe-needs)
- [Recipe format](#recipe-format)
- [Files and references](#files-and-references)
- [Step types](#step-types)
- [Script steps](#script-steps)
- [Dashboard templates](#dashboard-templates)
- [Examples](#examples)
- [Commands](#commands)
- [Scheduling](#scheduling)
- [Limits](#limits)
- [Security notes](#security-notes)
- [Not covered](#not-covered)

## Install

Python 3.11 or newer. Install it in its own virtual environment, together with anything your
scripts import (for example `openpyxl` to read Excel files):

```sh
python3 -m venv ~/.venvs/privacyfence-sync
~/.venvs/privacyfence-sync/bin/pip install privacyfence-sync openpyxl
```

Its version matches the PrivacyFence release it ships with. It needs no local PrivacyFence install
of its own: in organization mode it talks to your organization's server over HTTPS.

## Connecting to PrivacyFence

### Local mode (PrivacyFence on your own computer)

1. Get your AI client token: choose **Copy AI Client Token** in the PrivacyFence companion, or run
   `privacyfence-app --print-mcp-token`. It is the same token Claude Code uses.
2. Put it in the environment variable `PRIVACYFENCE_TOKEN` of the process that runs
   `privacyfence-sync`. A token is never accepted as a command-line argument, because other users
   on the machine can read the process list.
3. The server address defaults to `http://127.0.0.1:8765/mcp`. If you changed PrivacyFence's port,
   pass `--server` or set it in the recipe.

### Organization mode (a PrivacyFence server)

1. Sign in once:

   ```sh
   privacyfence-sync login --server https://privacyfence.example.com/mcp
   ```

   A browser opens on your organization's sign-in page. After you sign in, the tab says "Signed in
   to PrivacyFence" and the terminal prints `Signed in to https://privacyfence.example.com/mcp.`
2. Runs then use the stored sign-in automatically, as long as `PRIVACYFENCE_TOKEN` is not set.
3. A sign-in lasts 30 days. After that, a run fails with exit code 4 and the message
   `Not signed in to <server>, or the sign-in expired. Run: privacyfence-sync login --server <server>`.
   Sign in again once a month.

The sign-in is stored in `~/.config/privacyfence-sync/<hash of the server URL>.json` (or under
`$XDG_CONFIG_HOME`), readable only by you. `login` listens on `http://127.0.0.1:33418/callback` for
the browser's return; pass `--callback-port` if that port is taken. The port is registered with
the server at the first sign-in, so keep using the same one.

The server must hand out agent download links. That is the default; if your administrator turned
off `download_delivery.agent_links`, steps that receive a file fail with a message saying so.

### Unattended sessions must be allowed

Both modes need `unattended_sessions.enabled` in PrivacyFence's organization config bundle (built
with `build_org_bundle.py --enable-unattended-sessions`). Without it, a run stops before the first
step with:

```
privacyfence-sync: This PrivacyFence does not allow unattended sessions (unattended_sessions.enabled in org_config.json). Ask your administrator, or run with --attended.
```

## The rules a recipe needs

A scheduled run gets no approval cards, so every step needs an auto-accept rule, unless the tool is
one PrivacyFence never asks about (listing tools such as `drive_list_files`). Create rules in
PrivacyFence's **Settings → Auto-accept → Add a rule**. Each one asks for your passkey.

| Recipe step | Scope in Settings | Verbs to tick |
|---|---|---|
| `salesforce_export_report`, `salesforce_run_report` | **Salesforce — report**, the report id | read |
| `jira_export_issues` | **Jira — project**, the project key(s) | search |
| `drive_download_file` | **Drive — folder**, the folder id the file is in | download |
| `drive_list_files` | none needed | |
| `grist_export_records`, `grist_export_sql`, `grist_get_records` | **Grist — document**, the document id | read |
| `grist_update_csv`, `grist_update_records` | **Grist — document** | update |
| `grist_import_csv`, `grist_add_records` | **Grist — document** | create |
| `peek_upload_page` with a `slug` | **Peek — page**, the page's slug | update (new versions) |

Notes:

- **Personal data.** PrivacyFence scans what a read returns for personal data (names, email
  addresses, phone numbers and similar). When it finds some, it asks a person, and an unattended
  run is denied, even when a rule matches. For the tools whose data goes to a file or a page
  rather than to an AI model, the rule can say otherwise. When you add it, tick:
  - **Exports may include personal data** on a Salesforce report, Jira project or Grist document
    rule (it covers `salesforce_export_report`, `jira_export_issues`, `grist_export_records` and
    `grist_export_sql`);
  - **Downloads may include personal data** on a Drive folder rule (it covers
    `drive_download_file` when the file is delivered as a file; on an organization server a small
    file, up to about 75 KB by default, comes back inside the tool result instead, and is still
    scanned);
  - **New versions may include personal data** on a Peek page rule.

  Only you can set these boxes, in Settings, behind your passkey; an AI client cannot propose
  them. `salesforce_run_report`, `grist_get_records` and the other reads that return data in the
  tool result are always scanned.
- **A Jira project rule** covers an export only when every exported issue belongs to an approved
  project. A JQL query that reaches into another project needs that project in the rule too.
- **A Drive folder rule** covers files whose parent folder is the one named. A file in a subfolder
  needs the subfolder's id.
- **A Peek page rule** covers new versions of pages that already exist. Publish the page once by
  hand (or through Claude) to get its slug. Creating a page, changing who can see it, renaming or
  deleting it always needs your approval.
- To find out which step is missing a rule, run the recipe once with `--attended`: each uncovered
  step shows its approval card, with a link, and the card offers the matching "Always allow"
  button where one exists.

## Recipe format

A recipe is one YAML file. Paths in it are relative to the recipe file.

```yaml
version: 1                          # required, always 1
name: sales-mirror                  # required: lowercase letters, digits and dashes, up to 63
server: http://127.0.0.1:8765/mcp   # optional, see "Commands" for the order of precedence
work_dir: work                      # optional, default "work"
steps:                              # required, 1 to 50 steps, run in order
  - id: opps
    tool: salesforce_export_report
    args:
      report_id: 00O000000000001
      cell_format: both
      file_name: opps.csv
```

Every step has an `id` (lowercase letters, digits, `-` and `_`, up to 40 characters, unique in
the recipe) and exactly one of `tool`, `run` or `template`. A step may only refer to steps above
it. Any unknown key is an error, so a typo fails `check` instead of being ignored.

**The work folder** (`work_dir`) is where every file of a run lands. It is created if needed.
Files from the previous run are overwritten, not deleted, and stay there between runs. They hold
the data you read, so keep the folder somewhere only you can read. Your scripts may keep their own
state anywhere else you choose (for example a "last committed load" folder); `privacyfence-sync`
does not touch it.

**Tool arguments** are the tool's own arguments, as listed in PrivacyFence's
[tools reference](https://github.com/privacyfence/privacyfence/blob/main/docs/tools-reference.md).
Several tools take JSON as text (`filters`, `filter`, `args`, `fields`). In a recipe you can write
those as a YAML list or mapping; `privacyfence-sync` sends any list or mapping value as JSON text.
Strings, numbers and `true`/`false` are sent as they are. `reason` is optional: by default each call
says `privacyfence-sync <recipe name>: step <id>`, which is what the audit log and any approval
card show.

## Files and references

Steps pass files to each other by name. These steps produce files:

| Step | Its files | Referred to as |
|---|---|---|
| An export tool | `args.file_name` | `<step id>` |
| `drive_download_file` | `save_as` | `<step id>` |
| Any other tool step with `save_result` | the tool's result, as JSON | `<step id>` |
| A script step | each entry of `outputs`, and `stdout` if given | `<step id>.<output name>` |
| A template step | `output` | `<step id>` |

Two kinds of reference use them:

- **A file reference** (`file:` on an upload step, `data:` on a template step, and `{file:...}` in
  a script step's arguments) names a step, or a script step's output: `file: plan.upsert`.
- **A value reference** reads one value out of an earlier JSON file and puts it into a tool
  argument or a script argument: `${pick.stdout:pick.drive_id}` is the value at the path
  `pick` → `drive_id` in the `stdout` output of step `pick`. Path parts are object keys or list
  indexes (`files.0.id`). When a value reference is the whole string, the value keeps its JSON
  type (a number stays a number); inside a longer string it is inserted as text. A path that does
  not exist fails the step before anything is called.

## Step types

### Export steps

`salesforce_export_report`, `jira_export_issues`, `grist_export_records`, `grist_export_sql`.

```yaml
- id: issues
  tool: jira_export_issues
  args:
    jql: project = OPS AND updated >= -30d
    fields: ["Story Points", "duedate"]
    file_name: issues.csv
```

- `file_name` is required. It must be a plain file name ending in `.csv` or `.json`, which picks
  the format. The file lands in the work folder under that name.
- Don't give `destination_dir`: `privacyfence-sync` sets it.
- How the file arrives depends on the install, and the recipe doesn't change: a PrivacyFence that
  can write to your folders writes it there; a privilege-separated local install or an
  organization server hands out a one-time download link, which `privacyfence-sync` fetches at
  once (the link expires after 5 minutes). Either way the file's SHA-256 is checked against what
  PrivacyFence reported, and the step fails if they differ.
- A `jira_export_issues` query that matches no issue writes no file on PrivacyFence's side;
  `privacyfence-sync` writes a CSV with just the header row (or `[]` for `.json`), so later steps
  still find their input.

What each export writes:

| Tool | Columns | Notes |
|---|---|---|
| `salesforce_export_report` | the report's detail columns, or `columns` | `cell_format: value` (default) writes raw values: dates as YYYY-MM-DD, amounts as numbers, record ids for lookups. `label` writes what Salesforce shows. `both` writes, for each column `C`, the raw value as `C` and the shown text as `C.label`, plus `C.currency` for money columns. A report over 2,000 rows or a grouped report needs `page_by`, a column unique per row. |
| `jira_export_issues` | key, summary, status, issue_type, priority, assignee, reporter, labels, created, updated, url, then `fields` | People are display names. Lists (labels, multi-select fields) are JSON in a CSV cell. |
| `grist_export_records` | every column of the table, including formula columns | `filter` narrows rows by column values. |
| `grist_export_sql` | what the `SELECT` returns | Cells are as Grist stores them: dates as seconds since 1970, true/false as 1/0. Only people who can copy the whole document may run SQL; others get a message pointing to `grist_export_records`. |

The report totals (the aggregates at the bottom of a Salesforce report) are not in the export. To
check your load against them, add a `salesforce_run_report` step with `summary_only: true` and
`save_result`; it returns the groupings and totals, no rows.

### Download steps

`drive_download_file`, for a file such as an Excel workbook in Google Drive.

```yaml
- id: workbook
  tool: drive_download_file
  args:
    file_id: "${pick.stdout:pick.drive_id}"
  save_as: workbook.xlsx
```

- `save_as` is required: the plain file name the download gets in the work folder, whatever the
  file is called in Drive.
- Don't give `destination_dir`: `privacyfence-sync` sets it.
- The file arrives like an export's (written in place, or by one-time link), or, on an
  organization server, inside the tool result for a small file; `privacyfence-sync` handles all
  three.

### Tool steps that save their result

Any tool step except an export or download step (whose file is already its result) can keep the
tool's result as a JSON file for later steps. The name must end in `.json`:

```yaml
- id: listing
  tool: drive_list_files
  args:
    query: "'1AbCdEfGhIjKlMnOpQrStUvWxYz' in parents and trashed = false"
    max_results: 200
  save_result: listing.json
```

Use it for a listing a script picks from, for report totals, or to check a write's counts
afterwards. Remember that a tool result is what an AI model would see: tools that return rows
(`grist_get_records`, `salesforce_run_report` without `summary_only`) are always PII-scanned and
capped in size. For bulk data, use an export.

### Upload steps

`grist_update_csv`, `grist_import_csv`, `peek_upload_page`.

```yaml
- id: load
  tool: grist_update_csv
  file: plan.upsert
  skip_if_empty: true
  args:
    doc_id: 8CAN8gKdxY7z
    table_id: Deals
    key_column: Deal_Id
    create_missing: true
    clear_empty: true
  save_result: load_result.json
```

- `file` names an earlier file; `privacyfence-sync` uploads it through a one-time upload slot and
  passes its `upload_id`. Don't give `local_path`, `upload_id` or `html`.
- `skip_if_empty: true` skips the step when the file has no data rows (a CSV with no row after
  the header that has a non-empty cell, or a JSON `[]`). Without it, `grist_update_csv` and `grist_import_csv` refuse such a file and the
  run fails. A skipped step with `save_result` writes `{"skipped": true}` there, so later steps can
  tell.
- `grist_update_csv` matches each CSV row to the record whose `key_column` has the same value,
  changes only the cells that differ, and with `create_missing: true` adds rows that match nothing.
  The key column must be in the CSV header and unique in the table. An empty cell leaves the value
  unchanged, unless `clear_empty: true`, in which case it clears it. Columns missing from the CSV
  are never touched.
- `grist_import_csv` adds every row as a new record.
- `peek_upload_page` with `slug` uploads a new version of that page; its visibility stays as it is.

### Other tool steps

Any other PrivacyFence tool can be a step, called with its `args`, for example
`grist_add_records`. It produces no file unless it has `save_result`. It needs a rule like any
other step.

### Script steps

```yaml
- id: plan
  run: scripts/datalake.py
  args: [plan, deals, --home, /Users/me/datalake, --source, "{file:opps}", --out, "{work_dir}/plan"]
  outputs:
    upsert: plan/upsert.csv
    flag_out: plan/flag_out.csv
    summary: plan/summary.json
  timeout_seconds: 600        # optional, 1 to 3600, default 600
```

Runs your Python script. See [Script steps](#script-steps) below.

### Template steps

```yaml
- id: page
  template: templates/dashboard.html
  data: dashboard_data        # a file ending in .json
  output: dashboard.html
```

Puts a JSON file into an HTML template. See [Dashboard templates](#dashboard-templates).

## Script steps

A script step runs an ordinary Python script with the same Python as `privacyfence-sync`:

```
python <run> <args...>
```

- **Arguments.** `args` is a list of strings. In each one, `{file:<ref>}` becomes that file's full
  path, `{work_dir}` the work folder, `{recipe_dir}` the recipe's folder, `{output:<name>}` the
  path of one of this step's own `outputs`, and a value reference `${<ref>:<path>}` the value
  (as text). Any other `{name}` or `{name:...}` is an error, caught by `check`, because it is
  almost always a typo; other text, JSON included, is passed as written. Arguments go to the script
  directly, never through a shell.
- **Outputs.** `outputs` maps names (lowercase letters, digits, `-` and `_`) to paths inside the
  work folder (a subfolder is fine, `..` is not). After the script exits with 0, every declared output must exist. `stdout: <file name>`
  also saves what the script prints, as output `stdout`; the printed text is shown in the run's
  log either way.
- **Exit codes.**
  - `0`: the step succeeded; the run continues.
  - `10`: there is nothing to do this time (for example no new source file). The run stops here
    and `privacyfence-sync` exits with 0, printing `[<id>] nothing to do, run stopped`.
  - Anything else: the step failed and the run stops with exit code 2. The last 2,000 characters
    of the script's error output are shown.
- **Guards.** A script is also how a recipe stops before writing when something looks wrong. Put a
  small check after your plan step: it reads the plan's summary and exits non-zero on warnings, a
  row count that moved more than you expect, or an empty parse. Nothing after it runs, and a
  person looks at it.
- **Environment.** The script runs in the work folder and gets your environment **without** any
  variable whose name starts with `PRIVACYFENCE_`, so it never sees the token. It has no
  PrivacyFence access: everything it needs comes in as files, and everything it produces goes out
  as files. It may read and write your own files elsewhere (state, run history).
- The script's own folder is on its import path, so it can import modules that sit next to it.
  The work folder is not, so a file that lands there is never imported by accident.
- `run` must be inside the recipe's folder. Keep the recipe, its scripts and templates in one
  folder (or one git repository).

### Rows deleted at the source

PrivacyFence's Grist connector never deletes a record. A row that disappears from the source
therefore stays in Grist. Keep a Bool flag column such as `In_Report` in the table, diff the source
against your last committed load (or against an export of the Grist table), and write the flag as
`false` for every key the source no longer has. Dashboards filter on the flag, and history
survives.

## Dashboard templates

A template is an HTML file with this placeholder exactly once:

```html
<script>
  const DATA = /*PRIVACYFENCE_SYNC_DATA*/null;
  // ... draw the dashboard from DATA ...
</script>
```

The template step replaces the placeholder with the contents of the `data` JSON file. The data is
escaped so it can never close the `<script>` element, whatever text it holds. The JSON can come
from a `grist_export_sql` export with a `.json` file name (a list of objects, one per row), or from
a script step that shapes it exactly as the template expects.

Claude writes the template once, when the dashboard is designed; each run only refreshes `DATA`.
`grist_export_sql` writes dates as seconds since 1970 and true/false as 1/0, so convert them in the
template's script or in a script step.

The page should be self-contained (inline CSS and JavaScript, or libraries from a CDN your Peek
viewers can reach). Peek takes pages up to 10 MB.

## Examples

Two recipes in the shape of a real datalake: one engine script (`scripts/datalake.py`) that parses
a source, diffs it against the last committed load, writes the CSVs for Grist, and commits the load
once Grist has accepted it.

### A Salesforce report mirrored into Grist

```yaml
version: 1
name: sf-opportunities
steps:
  - id: report
    tool: salesforce_export_report
    args: {report_id: 00O000000000001, cell_format: both, file_name: opps.csv}

  - id: totals
    tool: salesforce_run_report
    args: {report_id: 00O000000000001, summary_only: true}
    save_result: totals.json

  - id: plan
    run: scripts/datalake.py
    args: [plan, sf-opportunities, --home, /Users/me/datalake, --source, "{file:report}",
           --totals, "{file:totals}", --out, "{work_dir}/plan"]
    outputs: {upsert: plan/upsert.csv, flag_out: plan/flag_out.csv, summary: plan/summary.json,
              sync_log: plan/sync_log.csv}

  - id: guard
    run: scripts/guard.py
    args: ["{file:plan.summary}", --max-change, "0.15", --max-flagged-out, "0.10"]

  - id: upsert
    tool: grist_update_csv
    file: plan.upsert
    skip_if_empty: true
    args: {doc_id: 8CAN8gKdxY7z, table_id: Opportunities, key_column: Opportunity_Id,
           create_missing: true, clear_empty: true}
    save_result: upsert_result.json

  - id: flag_out
    tool: grist_update_csv
    file: plan.flag_out
    skip_if_empty: true
    args: {doc_id: 8CAN8gKdxY7z, table_id: Opportunities, key_column: Opportunity_Id}
    save_result: flag_out_result.json

  - id: verify
    run: scripts/datalake.py
    args: [verify, "{file:plan.summary}", "{file:upsert}", "{file:flag_out}"]

  - id: commit
    run: scripts/datalake.py
    args: [commit, sf-opportunities, --home, /Users/me/datalake, --run, "{work_dir}/plan"]

  - id: sync_log
    tool: grist_import_csv
    file: plan.sync_log
    args: {doc_id: 8CAN8gKdxY7z, table_id: Sync_Log}
```

### The newest Excel workbook in a Drive folder

```yaml
version: 1
name: cashflow
steps:
  - id: listing
    tool: drive_list_files
    args: {query: "'1AbCdEfGhIjKlMnOpQrStUvWxYz' in parents and trashed = false", max_results: 200}
    save_result: listing.json

  - id: pick
    run: scripts/datalake.py
    args: [pick, cashflow, --home, /Users/me/datalake, --listing, "{file:listing}", --exit-10-if-current]
    stdout: pick.json

  - id: workbook
    tool: drive_download_file
    args: {file_id: "${pick.stdout:pick.drive_id}"}
    save_as: cashflow.xlsx

  - id: plan
    run: scripts/datalake.py
    args: [plan, cashflow, --home, /Users/me/datalake, --source, "{file:workbook}",
           --source-info, "${pick.stdout:source_info_arg}", --out, "{work_dir}/plan"]
    outputs: {upsert: plan/upsert.csv, flag_out: plan/flag_out.csv, summary: plan/summary.json}

  - id: guard
    run: scripts/guard.py
    args: ["{file:plan.summary}"]

  - id: upsert
    tool: grist_update_csv
    file: plan.upsert
    skip_if_empty: true
    args: {doc_id: 9DbO9hLeyZ8a, table_id: Cashflow_Revenue, key_column: Opportunity_Id,
           create_missing: true, clear_empty: true}

  - id: flag_out
    tool: grist_update_csv
    file: plan.flag_out
    skip_if_empty: true
    args: {doc_id: 9DbO9hLeyZ8a, table_id: Cashflow_Revenue, key_column: Opportunity_Id}

  - id: commit
    run: scripts/datalake.py
    args: [commit, cashflow, --home, /Users/me/datalake, --run, "{work_dir}/plan"]
```

Here `pick` prints its JSON and exits with 10 when the newest workbook is the one already loaded, so
a run with nothing new stops after two cheap steps.

Rules for these: Salesforce report `00O000000000001` (read), Drive folder `1AbCd…` (download), and
each Grist document (update, create), with the personal-data boxes where the data holds names.

### Publishing a dashboard

```yaml
version: 1
name: pipeline-dashboard
steps:
  - id: data
    tool: grist_export_sql
    args:
      doc_id: 8CAN8gKdxY7z
      sql: >-
        SELECT Stage, COUNT(*) AS deals, SUM(Amount_USD) AS amount
        FROM Opportunities WHERE In_Report = 1 GROUP BY Stage
      file_name: data.json

  - id: page
    template: templates/dashboard.html
    data: data
    output: dashboard.html

  - id: publish
    tool: peek_upload_page
    file: page
    args: {slug: pipeline-dashboard}
```

Before the first scheduled run of any recipe:

```sh
privacyfence-sync check recipe.yaml
privacyfence-sync run recipe.yaml --attended   # approve, and add any missing rule
privacyfence-sync run recipe.yaml              # unattended: no card should appear
```

## Commands

```
privacyfence-sync run RECIPE [--server URL] [--attended] [--token-env NAME]
privacyfence-sync login --server URL [--callback-port N]
privacyfence-sync check RECIPE
```

- **`run`** runs every step in order and stops at the first failure. Each step prints a line
  starting with `[<step id>]`.
  - `--attended` skips the unattended declaration. A step that needs approval prints
    `[<id>] waiting for approval: <link>`, waits for your decision (up to 16 minutes), and repeats
    the call once you approve. Use it for a first run and to find missing rules.
  - `--token-env NAME` reads the token from the variable `NAME` instead of `PRIVACYFENCE_TOKEN`.
  - Without a token in the environment, `run` uses the stored sign-in from `login`.
- **`login`** signs in to an organization server, then checks the connection.
- **`check`** validates the recipe, its references and its paths, and prints
  `<name>: <n> step(s), OK`. It makes no network call and runs no script.

The server address is, in order: `--server`, the recipe's `server`, the variable
`PRIVACYFENCE_MCP_URL`, then `http://127.0.0.1:8765/mcp`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Every step succeeded, or a script step said there was nothing to do (exit 10) |
| 1 | The recipe, a command-line argument, a reference or a template is wrong |
| 2 | A tool call, a file transfer or a script failed |
| 3 | PrivacyFence denied a step (in an unattended run: no rule covers it) |
| 4 | Not signed in, the sign-in expired, or no token |

Every error prints one line starting with `privacyfence-sync:` to the error output; when a script
failed, the script's own error output is shown just above it. No message ever contains a token.

## Scheduling

Run recipes as your own user, the same user PrivacyFence's token belongs to. Keep one recipe per
source, so a failure in one source doesn't hold up the others, and give each recipe its own folder
with its scripts, templates and work folder. `run` and `template` paths must stay inside the
recipe's folder, and the default `work_dir: work` then lands next to the recipe. Group the folders
by schedule and number them for the order they run in, with the dashboard last:

```
~/datalake/                     a git repository
  .gitignore                    work/  state/  logs/
  daily/
    10-sf-opportunities/
      recipe.yaml
      scripts/plan.py
      work/                     this recipe's run files (not in git)
    20-drive-cashflow/
      recipe.yaml
      scripts/...
    90-dashboard/
      recipe.yaml
      templates/dashboard.html
  hourly/
    ...
  state/                        your scripts' own state, e.g. state/sf-opportunities/
  logs/
```

One wrapper runs every recipe of a schedule, so adding a recipe means adding a folder, not another
scheduler entry. Recipes hand data to each other through Grist, not through each other's work
folders. Code several recipes share can't be linked in, since a symbolic link that points outside
the recipe's folder is refused; make it a small package and install it into the same virtual
environment (`pip install -e ~/datalake/lib`), and keep the scripts in each folder thin.
Keep `~/datalake` out of `~/Documents`, `~/Desktop` and `~/Downloads` on macOS: a background job
is not allowed to read them.

### macOS: launchd and the Keychain

Use a launchd agent, not cron. A launchd calendar job that was due while the Mac slept runs once
when it wakes; cron skips it. (Neither runs a job that was due while the Mac was shut down.)

Keep each recipe's token in your login Keychain, under the recipe's folder name. `-U` replaces an
existing item, so the same command renews an expired token; `-w` with no value asks for the token,
so it stays out of your shell history:

```sh
security add-generic-password -U -s privacyfence-sync -a 10-sf-opportunities -T /usr/bin/security -w
```

`-T /usr/bin/security` lets the wrapper read the item without a prompt. One token for all recipes
works too; store it under each name, or change the lookup below.

The wrapper, `~/bin/pf-sync.sh` (`chmod 700`), runs every recipe of a schedule with its own token,
carries on after a failure, and shows a notification naming the recipes that failed:

```zsh
#!/bin/zsh
set -u
group="${1:-daily}"
S="$HOME/.venvs/privacyfence-sync/bin/privacyfence-sync"
cd "$HOME/datalake" || exit 1

# Just after wake, give PrivacyFence up to two minutes to come up.
for i in {1..24}; do /usr/bin/curl -s -o /dev/null http://127.0.0.1:8765/mcp && break; sleep 5; done

failed=()
for recipe in $group/*/recipe.yaml(N); do
  name="${recipe:h:t}"
  token="$(/usr/bin/security find-generic-password -s privacyfence-sync -a "$name" -w 2>/dev/null)" \
    || { echo "$(date) $name: no token in the Keychain"; failed+="$name(no token)"; continue; }
  echo "$(date) start $name"
  PRIVACYFENCE_TOKEN="$token" "$S" run "$recipe"
  rc=$?
  echo "$(date) end $name rc=$rc"
  [ $rc -ne 0 ] && failed+="$name($rc)"
done

if (( ${#failed} )); then
  /usr/bin/osascript -e "display notification \"${failed[*]}\" with title \"privacyfence-sync: $group\""
  exit 2
fi
```

The agent, `~/Library/LaunchAgents/local.pf-sync.daily.plist`, runs it every day at 06:10. launchd
does not expand `~` or `$HOME`, so write your home folder out, and create the log folder first:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.pf-sync.daily</string>
  <key>ProgramArguments</key>
  <array><string>/Users/you/bin/pf-sync.sh</string><string>daily</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>10</integer></dict>
  <key>StandardOutPath</key><string>/Users/you/datalake/logs/daily.log</string>
  <key>StandardErrorPath</key><string>/Users/you/datalake/logs/daily.log</string>
</dict>
</plist>
```

For weekdays only, make `StartCalendarInterval` an array of five such dicts, each with a `Weekday`
key (1 to 5). Load it, and run it once now to check it:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.pf-sync.daily.plist
launchctl kickstart -p gui/$(id -u)/local.pf-sync.daily
```

`launchctl bootout gui/$(id -u)/local.pf-sync.daily` turns it off. The job runs only while you are
logged in (a locked screen is fine), which is also when the Keychain and a local PrivacyFence are
available.

### Linux: cron

cron does not catch up on a run that was due while the machine was off or asleep. Keep the token
out of the crontab line, for example in a wrapper script only you can read:

```sh
#!/bin/sh
# ~/bin/datalake-sync.sh, chmod 700
export PRIVACYFENCE_TOKEN='...'
S=~/.venvs/privacyfence-sync/bin/privacyfence-sync
status=0
for r in ~/datalake/daily/*/recipe.yaml; do
  "$S" run "$r" || status=1
done
exit $status
```

```
# crontab -e: every weekday at 06:10
10 6 * * 1-5 ~/bin/datalake-sync.sh >> ~/datalake/last-run.log 2>&1
```

The exit code tells your scheduler or monitoring what happened (see [Commands](#commands)). A run
that exits 3 means a rule is missing, or a step's data changed so that a rule no longer covers it,
for example a new project appearing in a Jira export or a file moved to another Drive folder.

## Limits

These come from PrivacyFence's tools, not from `privacyfence-sync`:

| What | Limit |
|---|---|
| `grist_export_records`, `grist_export_sql` | 50,000 rows per export |
| `salesforce_export_report` | 2,000 rows without `page_by`; with it, up to `salesforce.report_max_pages` runs (50 by default) |
| `jira_export_issues` | 10,000 issues per export; narrow the JQL and export in parts above that |
| Any delivered file | 200 MB (`file_bridge.max_download_bytes`) |
| `grist_update_csv`, `grist_import_csv` | 20,000 rows, 100 columns, 10,000 characters per cell, 50 MB per file; one key column; formula columns can't be written |
| Upload slot | 50 MB, valid 10 minutes |
| `peek_upload_page` | 10 MB per page |
| Download link | one use, 5 minutes |
| Grist writes | add and update only, never delete (see [Rows deleted at the source](#rows-deleted-at-the-source)) |

Grist writes are not atomic. If one fails part-way, the earlier chunks are in the table; running
`grist_update_csv` again with the same file finishes the rest, which is why a commit step belongs
after the writes, not before.

## Security notes

- `privacyfence-sync` imports nothing from PrivacyFence and has no access to its credentials,
  rules or approvals. It can only make the calls your rules allow, like any AI client.
- The token (or the stored sign-in) is the key to everything your rules allow without a card.
  Treat it like a password: environment variables or a file only you can read, never a recipe,
  a crontab line or a shared folder.
- Files are written to the work folder in plain text. They hold whatever the source holds,
  personal data included when your rule allows it.
- Scripts and templates run with your user's rights. Review them like any code; a recipe folder in
  git makes every change a diff you can read.
- Every call is in PrivacyFence's audit log with the recipe and step name in its reason. A call that
  auto-accepted with personal data in it is logged as auto accepted, with the rule's id and the
  personal-data categories found.

## Not covered

- **No branching beyond stopping.** A script can stop a run successfully (exit 10) or fail it, and
  an upload can be skipped when its file is empty. There are no conditions or loops in a recipe.
- **No retries.** A failed step stops the run; the next scheduled run starts again from the top.
  A script that commits state only after the writes makes that safe.
- **No parallel steps.** Steps run one after another.
- **No new Peek pages and no new Grist tables.** A recipe publishes new versions of an existing page
  and writes to existing tables. Creating them is a one-time design step, done with Claude.
- **No per-row tool calls.** A recipe can't call a tool once per row of a file. Bulk writes go
  through `grist_update_csv` and `grist_import_csv`; for clearing values, use `clear_empty`.
