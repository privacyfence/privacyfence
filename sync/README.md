# privacyfence-sync

`privacyfence-sync` runs a data-sync recipe against PrivacyFence with no AI model involved. A recipe
exports data from your connected services (Salesforce, Jira, Grist), runs your own transform script
on the files, writes the result back to Grist, and can publish an HTML dashboard on Peek.

The intended split of work:

- **Designing** a sync is a job for Claude: the Grist data model, which sources to read, how to
  transform them, what the dashboard shows. Claude writes the recipe, the transform script and the
  dashboard template, and you review them like any code change.
- **Running** the sync on a schedule is a job for `privacyfence-sync`. It does the same tool calls
  Claude would make, in a fixed order, and costs no model tokens.

It is an ordinary MCP client of PrivacyFence, like Claude Code. Every call goes through
PrivacyFence's gate, rules and audit log, exactly as Claude's calls do. `privacyfence-sync` never
holds your Salesforce, Jira, Grist or Peek credentials, cannot approve anything, and cannot change
your rules. A scheduled run declares itself unattended, so any step that no auto-accept rule covers
fails at once instead of waiting for an approval nobody will give.

## Contents

- [Install](#install)
- [Connecting to PrivacyFence](#connecting-to-privacyfence)
- [The rules a recipe needs](#the-rules-a-recipe-needs)
- [Recipe format](#recipe-format)
- [Step types](#step-types)
- [Transform scripts](#transform-scripts)
- [Dashboard templates](#dashboard-templates)
- [A complete example](#a-complete-example)
- [Commands](#commands)
- [Scheduling](#scheduling)
- [Limits](#limits)
- [Security notes](#security-notes)
- [Not covered](#not-covered)

## Install

Python 3.11 or newer. Install it in its own virtual environment:

```sh
python3 -m venv ~/.venvs/privacyfence-sync
~/.venvs/privacyfence-sync/bin/pip install privacyfence-sync
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
off `download_delivery.agent_links`, export steps fail with a message saying so.

### Unattended sessions must be allowed

Both modes need `unattended_sessions.enabled` in PrivacyFence's organization config bundle (built
with `build_org_bundle.py --enable-unattended-sessions`). Without it, a run stops before the first
step with:

```
privacyfence-sync: This PrivacyFence does not allow unattended sessions (unattended_sessions.enabled in org_config.json). Ask your administrator, or run with --attended.
```

## The rules a recipe needs

A scheduled run gets no approval cards, so every step needs an auto-accept rule. Create them in
PrivacyFence's **Settings → Auto-accept → Add a rule**. Each one asks for your passkey.

| Recipe step | Scope in Settings | Verbs to tick |
|---|---|---|
| `salesforce_export_report` | **Salesforce — report**, the report id | read |
| `jira_export_issues` | **Jira — project**, the project key(s) | search |
| `grist_export_records`, `grist_export_sql` | **Grist — document**, the document id | read |
| `grist_update_csv` | **Grist — document** | update |
| `grist_import_csv`, and `grist_update_csv` with `create_missing: true` | **Grist — document** | create |
| `peek_upload_page` with a `slug` | **Peek — page**, the page's slug | update (new versions) |

Notes:

- **Personal data.** PrivacyFence scans what a read returns for personal data (names, email
  addresses, phone numbers and similar). When it finds some, it asks a person, and an unattended
  run is denied, even when a rule matches. For the export tools and Peek page uploads, the rule
  can say otherwise: tick **Exports may include personal data** (Salesforce report, Jira project,
  Grist document) or **New versions may include personal data** (Peek page) when you add it.
  Those tools return no rows to an AI model, so the scan's purpose doesn't apply, and you decide
  where the data goes. Only you can set this box, in Settings, behind your passkey; an AI client
  cannot propose it.
- **A Jira project rule** covers an export only when every exported issue belongs to an approved
  project. A JQL query that reaches into another project needs that project in the rule too.
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
name: sales-dashboard               # required: lowercase letters, digits and dashes, up to 63
server: http://127.0.0.1:8765/mcp   # optional, see "Commands" for the order of precedence
work_dir: work                      # optional, default "work"
steps:                              # required, 1 to 50 steps, run in order
  - id: deals
    tool: salesforce_export_report
    args:
      report_id: 00O000000000001
      file_name: deals.csv
```

Every step has an `id` (lowercase letters, digits, `-` and `_`, up to 40 characters, unique in
the recipe) and exactly one of `tool`, `run` or `template`. A step may only use files from steps
above it. Any unknown key is an error, so a typo fails `check` instead of being ignored.

**The work folder** (`work_dir`) is where every file of a run lands: exports, transform outputs and
rendered pages. It is created if needed. Files from the previous run are overwritten, not
deleted, and stay there between runs. They hold the exported data, so keep the folder somewhere
only you can read.

**Tool arguments** are the tool's own arguments, as listed in PrivacyFence's
[tools reference](https://github.com/privacyfence/privacyfence/blob/main/docs/tools-reference.md).
Several tools take JSON as text (`filters`, `filter`, `args`, `fields`). In a recipe you can write
those as a YAML list or mapping; `privacyfence-sync` sends any list or mapping value as JSON text.
Strings, numbers and `true`/`false` are sent as they are. `reason` is optional: by default each call
says `privacyfence-sync <recipe name>: step <id>`, which is what the audit log and any approval
card show.

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
  can write to your folders writes it there; a privilege-separated local install or an organization
  server hands out a one-time download link, which `privacyfence-sync` fetches at once (the link
  expires after 5 minutes). Either way the file's SHA-256 is checked against what PrivacyFence
  reported, and the step fails if they differ.
- A `jira_export_issues` query that matches no issue writes no file on PrivacyFence's side;
  `privacyfence-sync` writes a CSV with just the header row (or `[]` for `.json`), so later steps
  still find their input.

What each export writes:

| Tool | Columns | Notes |
|---|---|---|
| `salesforce_export_report` | the report's detail columns, or `columns` | `cell_format: value` (default) writes raw values (dates as YYYY-MM-DD, amounts as numbers, record ids for name columns); `label` writes what Salesforce shows. A report over 2,000 rows or a grouped report needs `page_by`, a column unique per row. |
| `jira_export_issues` | key, summary, status, issue_type, priority, assignee, reporter, labels, created, updated, url, then `fields` | People are display names. Lists (labels, multi-select fields) are JSON in a CSV cell. |
| `grist_export_records` | every column of the table, including formula columns | `filter` narrows rows by column values. |
| `grist_export_sql` | what the `SELECT` returns | Cells are as Grist stores them: dates as seconds since 1970, true/false as 1/0. Only people who can copy the whole document may run SQL; others get a message pointing to `grist_export_records`. |

### Upload steps

`grist_update_csv`, `grist_import_csv`, `peek_upload_page`.

```yaml
- id: load
  tool: grist_update_csv
  file: merged
  args:
    doc_id: 8CAN8gKdxY7z
    table_id: Deals
    key_column: Deal_Id
    create_missing: true
```

- `file` names an earlier step whose file is sent. `privacyfence-sync` uploads it through a one-time
  upload slot and passes its `upload_id`; don't give `local_path`, `upload_id` or `html`.
- `grist_update_csv` matches each CSV row to the record whose `key_column` has the same value,
  changes only the cells that differ, and with `create_missing: true` adds rows that match nothing.
  The key column must be in the CSV header and unique in the table.
- `grist_import_csv` adds every row as a new record.
- `peek_upload_page` with `slug` uploads a new version of that page; its visibility stays as it is.

### Other tool steps

Any other PrivacyFence tool can be a step, called with its `args`. It produces no file. It needs a
rule like any other step.

### Transform steps

```yaml
- id: merged
  run: transforms/merge_deals.py
  inputs: [deals, current]
  output: merged.csv
  timeout_seconds: 600        # optional, 1 to 3600, default 600
```

Runs your Python script on earlier steps' files. See [Transform scripts](#transform-scripts).

### Template steps

```yaml
- id: page
  template: templates/dashboard.html
  data: dashboard_data        # an earlier step whose file ends in .json
  output: dashboard.html
```

Puts a JSON file into an HTML template. See [Dashboard templates](#dashboard-templates).

## Transform scripts

A transform is an ordinary Python script, run with the same Python as `privacyfence-sync`:

```
python -I <script> <input 1> <input 2> ... <output>
```

- The arguments are the input files' paths, in the order of `inputs`, then the output path.
- It runs in the work folder. Its output and error text are shown in the run's output.
- It must write the output file and exit with 0. A non-zero exit, a missing output file or running
  past `timeout_seconds` fails the step.
- It gets your environment **without** any variable whose name starts with `PRIVACYFENCE_`, so it
  never sees the token. It has no PrivacyFence access and should make no network call: it turns
  files into a file.
- `-I` (isolated mode) means it does not import modules from the current folder by accident. It can
  import the standard library and anything installed in the same virtual environment.

### Rows deleted at the source

PrivacyFence's Grist connector never deletes a record. A row that disappears from the source
therefore stays in Grist. Keep a Bool column such as `is_active` in the table, export the current
Grist table as one of the inputs, and have the transform write `is_active = false` for every key
that the source no longer has. `grist_update_csv` then marks them; dashboards filter on
`is_active`. Note that an empty cell in an update CSV leaves the value unchanged; write an explicit
value to change it.

A transform that does this for a Salesforce export keyed by `Deal_Id`:

```python
"""Merge the Salesforce deals export into the Grist Deals table and mark deals that are gone."""

import csv
import sys

deals_path, current_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

with open(deals_path, newline="", encoding="utf-8") as f:
    deals = list(csv.DictReader(f))
with open(current_path, newline="", encoding="utf-8") as f:
    current_keys = {row["Deal_Id"] for row in csv.DictReader(f)}

seen = set()
rows = []
for d in deals:
    seen.add(d["Opportunity.Opp_Id__c"])
    rows.append({
        "Deal_Id": d["Opportunity.Opp_Id__c"],
        "Name": d["OPPORTUNITY.NAME"],
        "Stage": d["STAGE_NAME"],
        "Amount": d["AMOUNT"],
        "Close_Date": d["CLOSE_DATE"],
        "is_active": "true",
    })
for key in sorted(current_keys - seen):
    rows.append({"Deal_Id": key, "is_active": "false"})

with open(out_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["Deal_Id", "Name", "Stage", "Amount", "Close_Date", "is_active"])
    writer.writeheader()
    writer.writerows(rows)
```

(The rows for removed deals leave the other columns empty, so `grist_update_csv` changes only their
`is_active`.)

## Dashboard templates

A template is an HTML file with this placeholder exactly once:

```html
<script>
  const DATA = /*PRIVACYFENCE_SYNC_DATA*/null;
  // ... draw the dashboard from DATA ...
</script>
```

The template step replaces the placeholder with the contents of the `data` step's JSON file. The
data is escaped so it can never close the `<script>` element, whatever text it holds. A
`grist_export_sql` export with a `.json` file name gives a list of objects, one per row, keyed by
column name.

Claude writes the template once, when the dashboard is designed; each run only refreshes `DATA`.
Remember that `grist_export_sql` writes dates as seconds since 1970 and true/false as 1/0, so the
template's script converts them.

The page should be self-contained (inline CSS and JavaScript, or libraries from a CDN your Peek
viewers can reach). Peek takes pages up to 10 MB.

## A complete example

```
sales-sync/
  recipe.yaml
  transforms/merge_deals.py      (the script above)
  templates/dashboard.html
```

```yaml
version: 1
name: sales-dashboard
work_dir: work
steps:
  - id: deals
    tool: salesforce_export_report
    args:
      report_id: 00O000000000001
      columns: Opportunity.Opp_Id__c,OPPORTUNITY.NAME,STAGE_NAME,AMOUNT,CLOSE_DATE
      page_by: Opportunity.Opp_Number__c
      file_name: deals.csv

  - id: current
    tool: grist_export_records
    args:
      doc_id: 8CAN8gKdxY7z
      table_id: Deals
      file_name: current.csv

  - id: merged
    run: transforms/merge_deals.py
    inputs: [deals, current]
    output: merged.csv

  - id: load
    tool: grist_update_csv
    file: merged
    args:
      doc_id: 8CAN8gKdxY7z
      table_id: Deals
      key_column: Deal_Id
      create_missing: true

  - id: dashboard_data
    tool: grist_export_sql
    args:
      doc_id: 8CAN8gKdxY7z
      sql: >-
        SELECT Stage, COUNT(*) AS deals, SUM(Amount) AS amount
        FROM Deals WHERE is_active = 1 GROUP BY Stage
      file_name: dashboard.json

  - id: page
    template: templates/dashboard.html
    data: dashboard_data
    output: dashboard.html

  - id: publish
    tool: peek_upload_page
    file: page
    args:
      slug: sales-dashboard
```

Rules this recipe needs: Salesforce report `00O000000000001` (read, and the personal-data box if
the report holds names), Grist document `8CAN8gKdxY7z` (read, update, create, and the personal-data
box if the table holds personal data), Peek page `sales-dashboard` (update, and its personal-data
box if the page shows names).

Before the first scheduled run:

```sh
privacyfence-sync check sales-sync/recipe.yaml
privacyfence-sync run sales-sync/recipe.yaml --attended   # approve, and add any missing rule
privacyfence-sync run sales-sync/recipe.yaml              # unattended: no card should appear
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
- **`check`** validates the recipe and its file references and prints `<name>: <n> step(s), OK`. It
  makes no network call.

The server address is, in order: `--server`, the recipe's `server`, the variable
`PRIVACYFENCE_MCP_URL`, then `http://127.0.0.1:8765/mcp`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Every step succeeded |
| 1 | The recipe, a command-line argument or a template is wrong |
| 2 | A tool call, a file transfer or a transform failed |
| 3 | PrivacyFence denied a step (in an unattended run: no rule covers it) |
| 4 | Not signed in, the sign-in expired, or no token |

Every error prints one line starting with `privacyfence-sync:` to the error output. No message ever
contains a token.

## Scheduling

Run it as your own user, the same user PrivacyFence's token belongs to. Keep the token out of the
crontab line itself, for example in a wrapper script only you can read:

```sh
#!/bin/sh
# ~/bin/sales-sync.sh, chmod 700
export PRIVACYFENCE_TOKEN='...'
exec ~/.venvs/privacyfence-sync/bin/privacyfence-sync run ~/sales-sync/recipe.yaml
```

```
# crontab -e: every weekday at 06:10
10 6 * * 1-5 ~/bin/sales-sync.sh >> ~/sales-sync/last-run.log 2>&1
```

On macOS, a launchd agent in `~/Library/LaunchAgents` running the same wrapper works the same way.
A machine that is asleep at the scheduled time skips the run; cron does not catch up.

The exit code tells your scheduler or monitoring what happened (see [Commands](#commands)). A run
that exits 3 means a rule is missing or a step's data changed so that a rule no longer covers it,
for example a new project appearing in a Jira export.

## Limits

These come from PrivacyFence's tools, not from `privacyfence-sync`:

| What | Limit |
|---|---|
| `grist_export_records`, `grist_export_sql` | 50,000 rows per export |
| `salesforce_export_report` | 2,000 rows without `page_by`; with it, up to `salesforce.report_max_pages` runs (50 by default) |
| `jira_export_issues` | 10,000 issues per export; narrow the JQL and export in parts above that |
| Any exported file | 200 MB (`file_bridge.max_download_bytes`) |
| `grist_update_csv`, `grist_import_csv` | 20,000 rows, 100 columns, 10,000 characters per cell, 50 MB per file; one key column; formula columns can't be written |
| Upload slot | 50 MB, valid 10 minutes |
| `peek_upload_page` | 10 MB per page |
| Download link | one use, 5 minutes |
| Grist writes | add and update only, never delete (see [Rows deleted at the source](#rows-deleted-at-the-source)) |

Grist writes are not atomic. If one fails part-way, the earlier chunks are in the table; running
`grist_update_csv` again with the same file finishes the rest.

## Security notes

- `privacyfence-sync` imports nothing from PrivacyFence and has no access to its credentials,
  rules or approvals. It can only make the calls your rules allow, like any AI client.
- The token (or the stored sign-in) is the key to everything your rules allow without a card.
  Treat it like a password: environment variables or a file only you can read, never a recipe,
  a crontab line or a shared folder.
- Exported files are written to the work folder in plain text. They hold whatever the export
  holds, personal data included when your rule allows it.
- Transform scripts and templates run with your user's rights. Review them like any code; a recipe
  folder in git makes every change a diff you can read.
- Every call is in PrivacyFence's audit log with the recipe and step name in its reason. An export
  that auto-accepted with personal data in it is logged as auto accepted, with the rule's id and
  the personal-data categories found.

## Not covered

- **No state between runs.** Every run is a full pull. For an incremental pull, put a relative
  filter in the source: a JQL condition such as `updated >= -7d`, or a relative date filter saved
  in the Salesforce report. `privacyfence-sync` has no "last successful run" variable.
- **No Drive or Google Sheets steps.** Drive-hosted Excel files and Google Sheets have no export
  tool that writes a file. `drive_download_file` delivers a file the same way, but a recipe cannot
  use it as an export step yet.
- **No retries.** A failed step stops the run; the next scheduled run starts again from the top.
- **No parallel steps.** Steps run one after another.
- **No new Peek pages.** A recipe can only publish new versions of an existing page.
