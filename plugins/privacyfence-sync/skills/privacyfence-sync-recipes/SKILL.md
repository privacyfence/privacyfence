---
name: privacyfence-sync-recipes
description: Write and maintain privacyfence-sync recipes, the YAML files that run a data sync through PrivacyFence on a schedule with no AI model involved. Use when the user wants to schedule or automate a sync between Salesforce, Jira, Google Drive, Grist and Peek, refresh a dashboard without Claude, turn an existing data-sync skill or a manual Claude routine into a recipe, add a source or step to a recipe, or find out why a scheduled run was denied or failed.
---

# privacyfence-sync recipes

A recipe is one YAML file that `privacyfence-sync` runs on a schedule. It makes the same
PrivacyFence tool calls Claude would make, in a fixed order, and runs the user's own scripts
between them. No AI model is involved, and every call still goes through PrivacyFence's rules and
audit log.

## 1. Read the reference first

Read `references/README.md` before you write anything. It is the format this skill writes: the
recipe fields, the step types, the file and value references, the script contract, the rules table
and the exit codes. If anything below seems to disagree with it, the reference is right.

## 2. What belongs in a recipe and what stays with Claude

Some work is design work. You do it with the user, interactively, once:

- creating Grist tables and columns,
- the first load of a table,
- repairing drift between a source and its table,
- creating a new Peek page and designing the dashboard.

A recipe only repeats things: it reads from a source, runs scripts on the files, and writes into
Grist tables and Peek pages that already exist. A recipe cannot branch, loop, retry or call a tool
once per row. If the user asks for any of those, say so, and put the logic in a script that can
stop the run.

## 3. Designing the sync

- Write one recipe per source, so a failure in one source does not hold up the others.
- Give every Grist table a stable key column and a Bool flag column such as `In_Report`. PrivacyFence
  never deletes Grist rows. A row that leaves the source is flagged `false`, and dashboards filter
  on the flag.
- Split the work into scripts with three jobs:
  - **plan**: diff the source against the last committed load and write the CSVs to upload;
  - **guard**: read the plan's summary and exit non-zero on warnings or an unexpected change in
    row counts, so nothing is written;
  - **commit**: record the new load, and run only after every write has succeeded.
- Put `clear_empty: true` and `skip_if_empty: true` on every `grist_update_csv` step.
- For Salesforce, use `cell_format: both` when labels or currencies matter.
- For report totals, add a `salesforce_run_report` step with `summary_only: true` and
  `save_result`, and let a script compare the totals with the load.
- Keep the recipe, its scripts and its templates in one folder, ideally a git repository, so every
  change is a diff the user can review.

## 4. Porting an existing skill

Go through the existing skill step by step and turn each part into a recipe step:

- Each fetch becomes an export step or a `drive_download_file` step. Never use
  `salesforce_run_report` rows or `grist_get_records` pages for bulk data: those results are
  scanned for personal data and capped in size.
- Each local computation becomes a script step whose inputs and outputs are files.
- Each Grist write becomes an upload step.
- Replace per-row tool calls with one `grist_update_csv`, using `clear_empty` for values that must
  be cleared.
- Make "nothing new" exit with code 10, so the run stops cleanly and costs nothing.
- Leave the skill's state folder where it is and pass its path to the scripts as an argument.

## 5. Rules the user must create

A scheduled run gets no approval cards, so every step needs an auto-accept rule. After you write
a recipe, give the user the exact list of rules to create in Settings:

- the scope (for example Salesforce report, Jira project, Jira Assets schema, Drive folder, Grist document, Peek page),
- the value (the report id, project key, folder id, document id or slug),
- the verbs to tick,
- whether to tick the personal-data box for that scope.

Take these from the table and notes in `references/README.md`. Say plainly that only the user can
create rules, in Settings and behind their passkey, and that you cannot propose or approve them.

## 6. Testing

Tell the user to test in this order:

1. `privacyfence-sync check recipe.yaml`: validates the recipe, with no network call.
2. `privacyfence-sync permissions recipe.yaml`: asks PrivacyFence which rules and tools the recipe
   needs, and which of them already exist; no data moves. Create what it lists.
3. `privacyfence-sync preflight recipe.yaml`: connects with the token and reports, per step,
   whether it is in the token's scope and covered by a rule; no data moves.
4. `privacyfence-sync run recipe.yaml --attended`, once: each step without a rule shows an
   approval card, which finds the missing rules.
5. `privacyfence-sync run recipe.yaml`, unattended: no card should appear. A scheduler can add
   `--result-file PATH` to learn `ok` from `no_change` from a failure.

Exit codes, from the reference:

- **1**: the recipe, an argument, a reference or a template is wrong. Run `check`.
- **2**: a tool call, a file transfer or a script failed. Read the lines above the final error.
- **3**: PrivacyFence denied a step. A rule is missing, or the data moved outside the rule.
- **4**: not signed in, the sign-in expired, or no token.

## 7. Never

- Never put a token in a recipe, a script, a crontab line or the chat.
- Never use `--token`. It does not exist; the token comes from `PRIVACYFENCE_TOKEN` or from
  `privacyfence-sync login`.
- Never write a script that calls PrivacyFence itself. Scripts receive files and produce files,
  and run without any `PRIVACYFENCE_` variable.
