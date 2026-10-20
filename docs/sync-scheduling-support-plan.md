# Plan: privacyfence-sync support for scheduled, unattended runners

## Goal

Make `privacyfence-sync` and the daemon give a scheduled runner what it needs without workarounds. The runner in question is the maintainer's planned PrivacyFence Cockpit, a container that runs recipes on its own schedule against an organization-mode server with scoped tokens, built in its own repository. Any cron job benefits equally.

Today such a runner has three problems:
- It cannot tell "nothing to do" from a full run.
- It must keep its own hand-maintained table to say which auto-accept rule a recipe is missing.
- Its owner cannot see when a scoped token was last used or last denied.

After this plan:
- `privacyfence-sync run --result-file PATH` writes the run's outcome as JSON, including `no_change`.
- `privacyfence_check_policy` names the rule an uncovered call would need, and `privacyfence-sync permissions RECIPE` lists those rules for a whole recipe.
- The scoped-token list on Settings shows each token's last use and last denial.
- The docs describe scheduled recipes in organization mode.

No tracking issue: this plan came from a conversation about the companion cockpit.

## Current state

**Exit codes**
- `sync/src/privacyfence_sync/cli.py:121-151` maps errors to exit codes: 1 recipe, 2 failure, 3 denied, 4 not signed in.
- `run` ignores `run_recipe`'s return value (`cli.py:83-91`, `cli.py:150-151`), so a run a script stopped (exit 10) exits 0.
- `runner.py:33-62`: `run_recipe` returns `False` on `StopRun`, and `_run_steps` prints `[<id>] nothing to do, run stopped`. `StopRun` carries the step id (`transform.py:13-14`, `:47-48`).

**Policy checks**
- `src/privacyfence/web/mcp_dispatch.py:330-365`, `check_policy`, returns `{gate, verdict, matched_rule, matched_rule_id, reason, pii_gate_may_apply}`. Nothing says which scope, resource or verb would cover an uncovered call.
- The scope catalogue (`policy/catalogue.py:154-195`) is visible only through `privacyfence_list_policy`. Scoped tokens may not call that (`web/routes_mcp.py:103-109`).
- The derivation already exists for the approval card's "Always allow" button. `policy/propose.py:564-595`, `proposals_for`, takes a tool and a `ReviewContext` and returns `RuleProposal`s, with the value built by each scope's `value_of`.
- Those `value_of` builders tolerate `raw_data=None`; they return `NO_VALUE`.
- `EXTRA_SCOPES` (`catalogue.py:49-77`: `grist.document`, `peek.page`, `apps_script.project` and two value-less ones) are not in `PROPOSABLE_SCOPES` and need their own derivation.
  - Their selectors live in `scopes.NEW_SCOPE_SELECTORS` (`policy/scopes.py:618`), not `scopes.SCOPE_SELECTORS` (`:393`).
  - They read `args["doc_id"]`, `args["slug"]` and `args["script_id"]` (match functions at `:579-599`).
- `scope_catalogue()` (`catalogue.py:124-160`) iterates `propose.SCOPES_BY_GROUP`, which is keyed by `widening_group`, so a proposable scope's `widening_group` is its catalogue id by construction.
- `sync/src/privacyfence_sync/preflight.py:118-161` calls `check_policy` per tool step. Arguments holding run-time `${...}` refs are dropped.

**Scoped tokens**
- `ScopedToken` (`src/privacyfence/scoped_tokens.py:52-73`) has no usage fields.
- `ScopedTokenStore` (`:280-520`) keeps `scoped_tokens.json` per principal under `paths.authority_dir(principal)`, written by `secure_files.atomic_write_json` under `self._lock`. `revoke_all` (`:442-456`) shows how to reach a principal by id.
- Calls: `web/routes_mcp.py:480-493` resolves the scope and enters `token_scope`. `_refuse_scoped_call` (`:118-135`) refuses out-of-scope and revoked calls.
- Denials: `gate._deny_scoped` (`gate.py:1667-1681`) denies uncovered calls, and `gate.audit_scoped_refusal` (`:1684-1694`) audits refusals.
- Settings data: `web/server.py:352-399`, `scoped_token_page_data`, builds the Settings data. `settings_window_html.py:1334-1360` (`scopedTokenRow`) renders each token.

**Docs**
- `sync/README.md` has "Commands" (`:568`) and "Scheduling" (`:614`). Its scheduling section covers only launchd and cron on the person's own machine.
- `docs/org-mode-setup-guide.md` has nothing on scheduled recipes.
- `sync/README.md` is copied to `plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md` (profile `dod_conditional`).

## Design

### D1. `run --result-file PATH` (sync)

**The option.** `run` takes `--result-file PATH`. When it is given, the CLI writes one JSON file before it returns from `main`, for every outcome that `main` maps to an exit code (0–4). Argparse errors, an uncaught exception and KeyboardInterrupt write no file; a scheduler treats a missing file as a failure.

```json
{
  "version": 1,
  "recipe": "pipeline-overview",
  "outcome": "no_change",
  "exit_code": 0,
  "stopped_at_step": "build",
  "started_at": "2026-10-20T06:15:00Z",
  "finished_at": "2026-10-20T06:15:42Z",
  "privacyfence_sync_version": "6.6.0"
}
```

**Fields**

| Field | Value |
|---|---|
| `outcome` | `ok` (exit 0, every step ran); `no_change` (exit 0, a script step exited 10); `recipe_error` (1); `failed` (2); `denied` (3); `not_signed_in` (4) |
| `recipe` | The recipe's `name`, or `null` when the recipe could not be loaded |
| `stopped_at_step` | The id of the step that stopped the run, set only for `no_change`; otherwise `null` |
| times | UTC, ISO-8601 with a `Z` suffix and second precision |
| `privacyfence_sync_version` | `privacyfence_sync.__version__` |

**Rules**
- No error text goes into the file. Script errors carry stderr, which can hold data. The log keeps the text.
- Exit codes are unchanged; `no_change` still exits 0.
- The file is written atomically: write `PATH.tmp` in the same folder, then `os.replace`.
- If writing fails, print `privacyfence-sync: could not write the result file: <OSError>` to stderr and keep the run's own exit code.

**Code**
- `runner.run_recipe` changes its return to `str | None`: the id of the step that stopped the run, or `None` when every step ran. `_run_steps` returns the same.
- The new module `sync/src/privacyfence_sync/result.py` holds `OUTCOMES` (a dict of exit code to outcome string for 1–4), `outcome_for(exit_code: int, stopped_at: str | None) -> str`, and `write_result(path: Path, *, recipe: str | None, exit_code: int, stopped_at: str | None, started_at: datetime, finished_at: datetime) -> None`.
- `cli.py` restructuring:
  - `_run` takes a loaded `Recipe` (not a path) and returns the stopped step id.
  - The new helper `_run_command(args: argparse.Namespace) -> tuple[int, str | None, str | None]` returns `(exit_code, stopped_at, recipe_name)`.
    1. It calls `load_recipe(args.recipe)` first.
    2. It then calls `asyncio.run(_run(recipe, ...))`.
    3. Both are inside one try with the same except-chain `main` has today, so every message and code is unchanged. `recipe_name` is `None` when `load_recipe` raised.
  - `main` records `started_at` (UTC) before calling `_run_command` for the `run` command. It calls `write_result` when `args.result_file` is set, and returns the exit code.
  - The other commands keep their current code path.

### D2. `check_policy` names the rules that would cover a call (daemon)

**The new module.** `src/privacyfence/policy/suggest.py` holds one public function:

```python
def suggested_rules(tool: str, args: dict[str, Any], my_email: str = "") -> list[dict[str, Any]]:
```

**How it builds the list**
- It builds `ReviewContext(connector="", tool=tool, args=args, raw_data=None, my_email=my_email)`.
- It looks up `registry.TOOL_REGISTRY[tool]` and returns `[]` when the entry is missing or has no `operation` or `verb`. The connector is `propose.connector_of_operation(entry.operation)`.
- Candidates are, in order:
  1. Every `propose.PROPOSABLE_SCOPES` entry with that connector, `entry.verb in scope.verbs`, `entry.operation not in scope.excludes`, `scope.condition is None` and `scope.widenable` true. The `widenable=False` entries are the unconditional `always_allow` scopes (`gmail.anything` and the calendar condition scopes); an unconditional rule is never suggested.
  2. Then every `catalogue.EXTRA_SCOPES` entry with that connector and `entry.verb in extra.verbs`, minus `extra.excludes`, and with `extra.needs_value` true. The value-less `gmail.filters` and `slack.group_chats` are skipped: an unconditional rule is never suggested.
- For each candidate it computes `value`:
  - A `PROPOSABLE_SCOPES` entry uses `scope.value_of(ctx)`.
  - An extra scope uses the argument its selector reads: `doc_id` for `grist.document`, `slug` for `peek.page`, `script_id` for `apps_script.project`. A string value becomes `[value]`; an empty or missing one is `propose.NO_VALUE`.
  - Each value computation and selector call is wrapped in `try/except Exception: continue  # nosec B112  # a failing builder or selector means no suggestion, the same fail-closed posture as propose._candidate_value`. A failure skips that candidate.
- The selector for a predicate is `scopes.SCOPE_SELECTORS.get(predicate) or scopes.NEW_SCOPE_SELECTORS.get(predicate)`. A predicate found in neither table skips the candidate.
- It emits:
  - `{"group", "label", "value", "value_known": True, "verb", "operation"}` when `value is not NO_VALUE`, `value is not None`, and the selector's `matches(value, ctx)` is true.
  - The same with `"value": None, "value_known": False` when `value is NO_VALUE` and the predicate's selector has `resolves_from is not scopes.ResolvesFrom.ARGS` and the scope needs a value (`propose.scope_needs_value(scope)` for a proposable scope, `extra.needs_value` for an extra one). This means "the rule names the resource this call reaches, which is known only at run time".
  - Nothing otherwise.
- Field values:
  - `group` is `scope.widening_group` for a proposable scope, or the `EXTRA_SCOPES` key.
  - `label` is that id's `"label"` from `catalogue.scope_catalogue()`, built once per call.
  - `verb` is `entry.verb.value`; `operation` is `entry.operation`.
  - `value` is passed through as the builder returned it. Every builder returns a list or `NO_VALUE`, and the extras are wrapped in a list as above.
- No de-duplication: no two candidates share a group, value and verb.

**Where `check_policy` uses it.** `mcp_dispatch.check_policy` adds `"suggested_rules"` to its result, always present:
- `[]` when `gate == "auto"` or `verdict == "auto_accept"`;
- otherwise `suggested_rules(tool, agent_args, my_email)`.

The audit row is unchanged.

**Tool description.** `CHECK_POLICY_TOOL`'s description (`web/mcp_tools.py:130-145`, 993 characters today) is replaced by exactly this text, which is 983 characters. Every meta-tool description must stay at or under 1024 (`tests/unit/web/test_tool_schema_portability.py:133-136`).

```
Before calling a gated tool, ask whether that exact call would auto-accept or need a human. Pass the connector, tool and args you're about to call. Returns {gate, verdict, matched_rule, matched_rule_id, reason, pii_gate_may_apply, suggested_rules}. verdict is 'auto_accept' (the real call will pass through identically), 'requires_review' (no configured rule can match these args), or 'unknown' (it depends on fetched content this can't see in advance). matched_rule_id is set only for 'auto_accept', usable as privacyfence_propose_policy_change's rule_id. suggested_rules lists the rules that would cover an uncovered call: {group, label, value, value_known, verb, operation}; value is null when it is what the call reaches at run time. For read tools pii_gate_may_apply is true: the PII gate scans real content and can still force a popup. No side effects -- call it freely, especially before an unattended run. reason: one sentence on why you're checking now (logged, unverified).
```

Keep it as adjacent string literals in the existing style, with no trailing spaces.

**Scoped tokens see it too.** The suggestion reflects only the caller's own arguments in catalogue terms; it reveals none of the person's rules. `_dispatch_scoped_meta_tool` needs no change.

### D3. `privacyfence-sync permissions RECIPE` (sync)

**The command.** `permissions RECIPE [--server URL] [--token-env NAME] [--json]` loads the recipe, connects like `preflight`, and opens **no** unattended session.

`list_permissions` starts as `preflight_recipe` does (`preflight.py:66-89`):
- It calls `privacyfence_status`, mapping a `ToolDenied` to the same `SignInError`.
- In text mode, it prints the same `token "<label>": ...` line for a scoped token.
- It raises the same `SignInError` when a scoped token sees no tools beyond `META`.

**Each tool step.** It calls `privacyfence_check_policy` exactly as `preflight._check_step` does: same connector, same args with run-time refs dropped (reuse `preflight._check_args` and `preflight._connector_of`). A tool not in `client.list_tool_names()` is `not_available`.

Each step's status is decided in this order; the first that applies wins:
1. The tool is missing from `list_tool_names()`: `not_available`.
2. `privacyfence_check_policy` raised `ToolFailed`: `not_checked`.
3. `gate == "auto"`: `auto`.
4. `verdict == "auto_accept"` and `matched_rule != "session_temp_accept"`: `covered`, even when arguments were dropped.
5. Arguments were dropped (any `verdict` other than the one in 4): `unknown`.
6. `verdict == "requires_review"`, or `matched_rule == "session_temp_accept"`: `needs_rule`.
7. Anything else: `unknown`.

Statuses:

| Status | When | Text line |
|---|---|---|
| `auto` | `gate == "auto"` | `[<id>] <tool>: never asks for approval` |
| `covered` | `verdict == "auto_accept"` and `matched_rule != "session_temp_accept"` | `[<id>] <tool>: covered by rule <matched_rule>` |
| `not_checked` | rule 2 | `[<id>] <tool>: NOT CHECKED: <first line of the error>`, as preflight prints it |
| `needs_rule` | rule 6 | `[<id>] <tool>: NEEDS A RULE: <label> = <value joined by ", "> (<verb>)`, or `... NEEDS A RULE: <label>, naming what it reaches at run time (<verb>)` when `value_known` is false. One line per suggestion, the first carrying the step prefix and the rest indented by four spaces. With no suggestions: `[<id>] <tool>: NEEDS A RULE: this PrivacyFence does not suggest one; update it, or run once with --attended and use the card's Always allow` when the key is absent, or `[<id>] <tool>: NEEDS A RULE: PrivacyFence suggests no rule for this call; add one in Settings -> Auto-accept, or run once with --attended` when it is `[]` |
| `unknown` | rules 5 and 7 | `[<id>] <tool>: depends on what it reaches at run time (not checked: <dropped>)`, followed by any suggestions in the `needs_rule` form |
| `not_available` | the tool is missing from the server | the same text preflight prints for it |

**Summary.** After the steps it prints `Rules this recipe needs:`. Then:
- one line per distinct `(group, value)` pair across the suggestions of `needs_rule` and `unknown` steps, with the verbs merged and sorted: `  <label> = <values> (<verb>, <verb>)`;
- for `value_known: false`, the key is `(group, None)` and the line is `  <label>, naming what it reaches at run time (<verbs>)`;
- or `  none` when there are no suggestions. Then `Tools a scoped token for this recipe needs: <comma-separated sorted tool names>`, covering every tool step.

**`--json`.** Prints instead a single JSON object:
```json
{
  "version": 1,
  "recipe": "<name>",
  "steps": [{"id": "...", "tool": "...", "status": "...", "matched_rule": "...", "dropped": [], "suggested_rules": []}],
  "needed": [{"group": "...", "label": "...", "value": [], "verbs": []}],
  "tools": []
}
```
- `suggested_rules` is `null` when the server did not send the key.
- `matched_rule` is `null` unless the status is `covered`.

**Exit codes.** 0 when the listing completed, whether or not rules are needed. Otherwise the usual 1, 2 and 4 through `main`'s existing except-chain. The status `ToolDenied` → `SignInError` mapping lives in `list_permissions`, not `cli.py`. Wire the subcommand into `cli.py`'s parser and `main`.

**Code.** It lives in the new module `sync/src/privacyfence_sync/permissions.py`, exposing `async def list_permissions(recipe: Recipe, client: SyncClient, *, out: TextIO, as_json: bool) -> None`.

**Preflight also points at the rule.** In `preflight._check_step`'s two needs-approval branches (`preflight.py:149-150` and `:157-158`), when `result.get("suggested_rules")` is a non-empty list, append a suffix for the first suggestion:
- `; add: <label> = <values joined by ", "> (<verb>)`;
- or `; add: <label>, naming what it reaches at run time (<verb>)` when `value_known` is false.

### D4. Last use and last denial of a scoped token (daemon)

**Storage**
- A separate file per principal, `paths.authority_dir(principal) / "scoped_token_activity.json"`, with the shape `{"version": 1, "tokens": {"<token id>": {"last_used_at": <float|null>, "last_denied_at": <float|null>, "last_denied_tool": "<str>"}}}`.
- It is written with `secure_files.atomic_write_json`, under the store's `_lock`.
- Constants in `scoped_tokens.py`: `ACTIVITY_FILE_NAME = "scoped_token_activity.json"` and `USE_WRITE_INTERVAL_SECONDS = 60.0`.
- The new frozen dataclass `TokenActivity(last_used_at: float | None = None, last_denied_at: float | None = None, last_denied_tool: str = "")`.

**New `ScopedTokenStore` methods**
- `note_use(self, token: ScopedToken, now: float | None = None) -> None` sets `last_used_at` in memory.
- `note_denial(self, token: ScopedToken, tool: str, now: float | None = None) -> None` sets `last_denied_at` and `last_denied_tool` together, in memory.
- Both write the file only when the last write for that principal is more than `USE_WRITE_INTERVAL_SECONDS` ago, so a runner looping on denied calls costs at most one write a minute. A crash loses at most a minute of activity.
- `activity_for(self, principal: Principal) -> dict[str, TokenActivity]` returns the activity of the principal's **current** tokens only, using `self._refresh(principal)` and `self._tokens[principal.id]` inside the lock. It never calls `tokens_for`: `_lock` is a plain, non-reentrant `threading.Lock`.
- Rules for all three:
  - Each takes `self._lock` once.
  - Each first loads the principal's activity file into memory if it has not been loaded since start-up, so the first write after a restart cannot drop other tokens' stored values.
  - Loading merges per token: `last_used_at` is the newer of file and memory; `last_denied_at` and `last_denied_tool` come together from whichever side has the newer `last_denied_at`.
  - Each does nothing for `REVOKED_SCOPE`.
  - Each finds the principal like `revoke_all` does: `self._principals.get(token.principal_id) or Principal(id=token.principal_id)`.
  - A read or write failure is logged as a warning (`"Could not record scoped token activity for %s"`, the token id) and never raised. A malformed file reads as empty.
- Stale entries for revoked tokens stay in the file; `activity_for` filters them out.
- What counts:
  - Every allowed call is a use, meta-tools (`privacyfence_status`, `privacyfence_check_policy`) included.
  - Every refusal is a denial, including a `check_policy` probe for a tool outside the token's scope.
  - Both are intended: they are what the person sees the token doing.

**Hooks**
- `web/routes_mcp.py`: right after the `_scoped_token_allows` refusal block, for a scoped call (`scope is not None`), look up `store = scoped_tokens.current_store()` (never `get_store()`, which would create a store). If it is not `None`, run `await asyncio.to_thread(store.note_use, scope)`. This follows coding guideline §1.7, file I/O in a hot path goes through `asyncio.to_thread`.
- `gate.py`: add `def _note_scoped_denial(scope: ScopedToken, tool: str) -> None`, which looks up `scoped_tokens.current_store()`, returns if it is `None` or `scope is REVOKED_SCOPE`, calls `note_denial` inside `try/except Exception` with a `logger.warning`, and never raises. Call it in `_deny_scoped`, before the `raise`, and in `audit_scoped_refusal`. These are synchronous deny paths, so the throttled write runs inline: at most once a minute per principal, accepted and recorded in ADR 0206.

**On Settings**
- `scoped_token_page_data` adds `last_used_at`, `last_denied_at` and `last_denied_tool` to each token dict, from `activity_for(principal)` (or `None`, `None`, `""`).
- `scopedTokenRow` in `settings_window_html.py` builds `activityHtml = '<div class="pf-hint pf-st-activity">' + … + '</div>'` and changes the row's description argument from `esc(scopedExpiryText(t)) + detail` to `esc(scopedExpiryText(t)) + activityHtml + detail` (`settings_window_html.py:1342-1366`, the `rowHtml(...)` call). Its text is `Never used`, or `Last used <YYYY-MM-DD HH:MM> UTC`. When `last_denied_at` is set, it appends ` · Last denied <YYYY-MM-DD HH:MM> UTC (<tool>)`, escaped with `esc`. The time format is `new Date(x * 1000).toISOString().slice(0, 16).replace('T', ' ')`.
- `privacyfence_status` is unchanged.

### D5. Docs

**`sync/README.md`**
- "Commands" gains `permissions` and `run --result-file`, with the D1 outcome table and the D3 output.
- "Scheduling" gains a subsection, "On a server, against organization mode", which covers:
  1. The administrator enables scoped tokens (`--enable-scoped-tokens`). A scoped token needs no unattended-sessions opt-in (ADR 0164); a plain OAuth sign-in does.
  2. Create a scoped token with the recipe's rules and tools. `permissions` lists both.
  3. Store it in the scheduler's secret store, never in the recipe.
  4. Run `preflight`, then schedule `run --result-file`.
  5. Renew the token before it expires; Settings shows its expiry, last use and last denial.

  It says the stored OAuth sign-in (`login`) needs a browser on the same machine and a monthly re-login, so a server uses a scoped token.
- "The rules a recipe needs" (`sync/README.md:97-145`) keeps its table as background. It opens by saying `privacyfence-sync permissions RECIPE` lists the exact rules and tools for a given recipe from the server, so the table is no longer the way to work them out.
- The sentence at `sync/README.md:87-95` saying both modes need `unattended_sessions.enabled` is corrected: a scoped token does not.
- Copy the README to `plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md`.
- `plugins/privacyfence-sync/skills/privacyfence-sync-recipes/SKILL.md` (around `:83`, the preflight workflow) gains one step: run `permissions` to list the rules before `preflight`.

**Other docs**
- `docs/privacyfence-sync.md` gains one paragraph pointing at that subsection.
- `docs/org-mode-setup-guide.md` gains a subsection, `### Scheduled recipes`, at the end of `## 13. File delivery`. The existing sections are numbered, so nothing is renumbered. It covers:
  - what the administrator enables: `--enable-scoped-tokens` (the table row at `:340`);
  - what must stay as it is: `download_delivery.agent_links`, which is on by default (`org_mode.py:238`) and which privacyfence-sync needs;
  - a link to the README subsection;
  - the note that recipe scripts run on the scheduler's machine, never on this server (ADR 0161).

  The Contents list names only top-level sections, so it does not change.
- `docs/how-it-works.md:182`: the `privacyfence_check_policy` row of the meta-tool table mentions `suggested_rules`.
- `docs/security-and-compliance.md` (around `:287`): the `authority/` file inventory gains `scoped_token_activity.json`, holding the last use and last denial time and tool per token id, with no secrets.
- `CHANGELOG.md` `## [Unreleased]` gets three lines (D1+D3 as one sync line, D2, D4).

### Rejected alternatives (for the ADRs)

- **Exit 10 for "no change".** Rejected: every existing cron or launchd wrapper treats a non-zero exit as failure, so this would turn quiet days into alerts. The result file is opt-in.
- **A tool-to-rule table inside privacyfence-sync** (what the cockpit carries today). Rejected: it duplicates `policy/propose.py` and `catalogue.py` and drifts the moment a connector or verb changes. The server is the only source of truth.
- **Letting scoped tokens call `privacyfence_list_policy`.** Rejected: it shows every rule the person has (ADR 0164's reason for hiding it). A suggestion derived from the call's own arguments shows nothing new.
- **Usage fields inside `scoped_tokens.json`.** Rejected: that file is rewritten by create, rename, revoke and migration. Writing it on every call would race with those and churn the file's stamp, which the store uses to detect outside edits.
- **Deriving last use from the audit log.** Rejected: it means a scan per Settings render over a rotated, chained log.

## ADRs

- **0204**: `privacyfence_check_policy` names the rules that would cover an uncovered call, derived from the call's own arguments, for scoped tokens too (D2; rejects the client-side table and list_policy for tokens).
- **0205**: `privacyfence-sync` reports a run's outcome in an opt-in result file; exit codes are unchanged (D1; rejects exit 10).
- **0206**: A scoped token's last use and last denial are kept in a separate activity file, written at most once a minute per person, off the event loop for uses and inline for denials (D4; rejects fields in `scoped_tokens.json` and audit-log derivation). The index row adds "relates to 0164".

## Manual steps

There is nothing before implementation. After implementation, one check against a real install: [manual steps](https://claude.ai/artifact/PvJfxJZYQ3mSFE4Jaef2vX).

## Risks and open questions

- **`proposals_for` semantics differ from the card's.** The card proposes from `raw_data`; `suggested_rules` has none. D2 does not call `proposals_for`; it iterates `PROPOSABLE_SCOPES` itself so it can emit the `value_known: False` case. Its filters match `proposals_for` (`propose.py:564-595`), plus the `condition` and `widenable` exclusions.
- **The description length limit.** D2's text is 983 characters against a 1024 limit. If `test_tool_schema_portability.py` still fails, the text was not copied exactly; do not shorten other tools' descriptions. A test that pins the old wording (grep the tests for `call it freely while planning`) is updated to the new text.
- **Tests writing to the real data directory.** In a source checkout `paths.data_dir()` is the repository root. Any test that installs a scoped-token store and makes a scoped call must patch `paths.data_dir` to `tmp_path`, as `tests/unit/test_scoped_tokens.py:36-37` does, or p2's hook writes `<repo>/authority/scoped_token_activity.json`.
- **Activity writes on the hot path.** `note_use` runs on every scoped call. It must stay O(1) and in memory except for the throttled write, and the write must never raise into the call. A test asserts a second call within 60 seconds does not write, and the routes hook runs through `asyncio.to_thread`.
- **The `StopRun` return-type change.** If any caller of `run_recipe` other than `cli.py` and the tests exists, update it too. If one is in another package, stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: sync-scheduling-support
feature_branch: feature/sync-scheduling-support
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/PvJfxJZYQ3mSFE4Jaef2vX
manual_steps_source: docs/sync-scheduling-support-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-real-install-check
    title: Run permissions, preflight and a scheduled-style run with a real scoped token, and check Settings
    why: The unit tests fake the daemon and the browser page; only a real install shows the suggestions for real connector arguments and the activity line on the live Settings page.
verify_after_merge:
  - 'python3 -m pytest tests/unit/policy tests/unit/web tests/unit/test_scoped_tokens.py tests/unit/test_gate.py tests/unit/test_settings_window_html.py -q'
  - 'python3 -m pip install -q -e "./sync[test]" && python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100'
final_checks:
  - 'docs/sync-scheduling-support-plan.md and docs/sync-scheduling-support-plan-manual-steps.html are deleted; grep -rn "sync-scheduling-support-plan" . --exclude-dir=.git matches only the Related lines of the three new ADRs (git show <sha>^:... citations)'
  - 'git diff --name-only --diff-filter=A origin/main -- docs/adr | wc -l prints 3; each of those ADRs is Accepted and has a row in docs/adr/README.md''s index'
  - 'CHANGELOG.md has three new lines under "## [Unreleased]" and no new version heading'
  - 'plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md is identical to sync/README.md'
phases:
  - id: p1-check-policy-suggestions
    title: check_policy names the rules that would cover an uncovered call
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/policy/suggest.py
      - tests/unit/policy/test_suggest.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/mcp_tools.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_mcp_tools.py
    brief: |
      Implement Design D2 of docs/sync-scheduling-support-plan.md exactly.
      1. Read src/privacyfence/policy/propose.py (PROPOSABLE_SCOPES, proposals_for, _candidate_value,
         scope_needs_value, connector_of_operation, NO_VALUE), policy/catalogue.py (EXTRA_SCOPES,
         scope_catalogue), policy/scopes.py (SCOPE_SELECTORS, ResolvesFrom, the three extra-scope
         match functions at :579-599, NEW_SCOPE_SELECTORS at :618) and policy/registry.py
         (TOOL_REGISTRY, ToolRegistryEntry).
      2. Create src/privacyfence/policy/suggest.py with a module docstring saying what it is for (a
         scheduled runner learns which rule an uncovered call needs, from the call's arguments alone,
         the same catalogue the card and Settings use) and the one function suggested_rules exactly
         as D2 specifies: the candidate order, the widenable/condition exclusions, the selector lookup
         across SCOPE_SELECTORS and NEW_SCOPE_SELECTORS, the value_known False case, and the
         try/except with the exact `# nosec B112` comment D2 gives.
      3. Create tests/unit/policy/test_suggest.py. Copy the module header style of
         tests/unit/policy/test_propose.py (module docstring naming the module under test) and add
         `pytestmark = pytest.mark.unit`. Group the tests in `class TestScenario:` blocks
         (docs/coding-and-testing-guidelines.md section 2.2): TestArgumentDerivedSuggestions,
         TestRunTimeResources, TestNeverSuggested, TestFailClosed. One behaviour per test:
         salesforce_export_report with report_id "00O1" suggests group "salesforce.report" (use the
         real group id you found in step 1) with value ["00O1"], value_known True, verb "read";
         grist_update_csv with doc_id "docA" suggests group "grist.document", value ["docA"], verb
         "update"; peek_upload_page with slug "pipeline" suggests "peek.page" ["pipeline"]; peek_upload_page
         without slug suggests nothing for peek.page; drive_download_file with only file_id yields an
         entry for the drive folder group with value None and value_known False; an unknown tool returns
         []; gmail_create_filter yields []; gmail_create_draft yields nothing for gmail.anything (not
         widenable); a selector that raises is skipped (monkeypatch.setitem on scopes.SCOPE_SELECTORS
         for approved_report_ids with a ScopeSelector whose matches raises); a value_of that raises is
         skipped (monkeypatch.setattr propose.PROPOSABLE_SCOPES to a tuple holding one
         dataclasses.replace of the salesforce report entry with a raising value_of); a predicate in
         neither selector table is skipped (same replace with predicate "no_such_predicate").
         If a named tool's real registry verb differs from the one written here, assert the registry's
         verb and say so in your report.
      4. In src/privacyfence/web/mcp_dispatch.py check_policy, add "suggested_rules" to both result
         dicts per D2 ([] for gate auto and for verdict auto_accept). Import suggested_rules from
         ..policy.suggest.
      5. In src/privacyfence/web/mcp_tools.py replace CHECK_POLICY_TOOL's description with the exact
         983-character text in D2, as adjacent string literals.
      6. tests/unit/web/test_mcp_dispatch.py: update test_auto_gated_tool_is_always_auto_accept
         (:506-513, an exact dict comparison) to include "suggested_rules": []; add, in the same class
         as the other check_policy tests, that a covered call has [] and an uncovered
         salesforce_export_report has a non-empty list whose first entry has value_known True.
         If tests/unit/web/test_mcp_tools.py pins the old wording (grep "call it freely while
         planning"), update it to the new text.
      7. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence,
         bandit -c pyproject.toml -r src, and the coverage floor: python3 -m pytest tests/unit -q
         --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json && python3
         scripts/check_coverage_floor.py coverage.json.
      No CHANGELOG or doc edits in this phase (the last phase does them).
    acceptance:
      - 'python3 -m pytest tests/unit/policy/test_suggest.py -q passes'
      - 'python3 -m pytest tests/unit/policy/test_suggest.py -q --cov=privacyfence.policy.suggest --cov-branch --cov-fail-under=100 passes'
      - 'python3 -m pytest tests/unit/web/test_tool_schema_portability.py -q passes'
      - 'python3 -m pytest tests/unit -q --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json && python3 scripts/check_coverage_floor.py coverage.json passes'
      - 'bandit -c pyproject.toml -r src passes'
      - 'python3 -m pytest tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_mcp_tools.py -q passes'
      - 'grep -n "suggested_rules" src/privacyfence/web/mcp_dispatch.py matches at least twice'
      - 'grep -n "suggested_rules lists the" src/privacyfence/web/mcp_tools.py matches'
      - 'ruff check . and python3 -m mypy src/privacyfence pass'

  - id: p2-token-activity
    title: Record and show each scoped token's last use and last denial
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - tests/unit/test_scoped_tokens.py
      - src/privacyfence/web/routes_mcp.py
      - tests/unit/web/test_routes_mcp.py
      - tests/unit/web/test_routes_mcp_agent_identity.py
      - src/privacyfence/gate.py
      - tests/unit/test_gate.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_server.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Implement Design D4 of docs/sync-scheduling-support-plan.md exactly.
      1. In src/privacyfence/scoped_tokens.py add ACTIVITY_FILE_NAME, USE_WRITE_INTERVAL_SECONDS, the
         frozen dataclass TokenActivity, and ScopedTokenStore.note_use, note_denial and activity_for as
         D4 specifies. Keep per-principal in-memory state in new dicts initialised in __init__
         (self._activity: dict[str, dict[str, TokenActivity]], self._activity_written: dict[str, float],
         self._activity_loaded: set[str]). Each method takes self._lock exactly once and calls a private
         _load_activity(principal) helper first (lock held). Never call tokens_for or any other public
         method while holding the lock (it is not reentrant); activity_for uses self._refresh and
         self._tokens directly. Write with secure_files.atomic_write_json, the same way _write does for
         the token file. A malformed or unreadable activity file is treated as empty, with a
         logger.warning, like _reload does.
      2. Extend tests/unit/test_scoped_tokens.py (it already patches paths.data_dir; add a new
         `class TestTokenActivity:`): note_use writes the file on the first call; a second note_use
         within 60 s (pass now=) does not rewrite it (compare the file's JSON content); one after 61 s
         does; note_denial within 60 s of a write updates memory but not the file, and activity_for
         still returns it; last_denied_tool always travels with last_denied_at; a fresh store that has
         never loaded the file, given note_use for token A, keeps token B's stored last_denied_at from
         the file; activity_for omits ids of tokens no longer present; REVOKED_SCOPE is never recorded;
         an OSError from atomic_write_json (monkeypatch) is logged, not raised; a corrupt activity file
         reads as empty.
      3. In src/privacyfence/web/routes_mcp.py, after the `if scope is not None and not
         _scoped_token_allows(...)` refusal block (around :491-493), add the D4 hook exactly:
         current_store(), then `await asyncio.to_thread(store.note_use, scope)`.
         In tests/unit/web/test_routes_mcp.py and tests/unit/web/test_routes_mcp_agent_identity.py,
         every fixture that installs a scoped-token store (the `_OneTokenStore` fixtures around
         :570-620 and :448-473) also does `monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)`.
         `_OneTokenStore` keeps `_tokens` empty, so activity_for returns {}; assert instead on the
         store's in-memory `_activity` dict: an allowed scoped call sets last_used_at for the token id;
         a refused call does not. After the unit suite, authority/scoped_token_activity.json must
         not exist at the repository root.
      4. In src/privacyfence/gate.py add _note_scoped_denial per D4 and call it in _deny_scoped before
         its raise and in audit_scoped_refusal. Extend tests/unit/test_gate.py's scoped-token denial
         tests to assert last_denied_tool is set, and that a store whose note_denial raises still lets
         the GateDeniedError through unchanged.
      5. In src/privacyfence/web/server.py scoped_token_page_data add the three fields per token from
         get_store().activity_for(principal). Extend tests/unit/web/test_server.py's
         scoped_token_page_data test for the new keys (None/None/"" when there is no activity).
      6. In src/privacyfence/settings_window_html.py scopedTokenRow add the pf-st-activity line with the
         exact strings in D4. Extend tests/unit/test_settings_window_html.py with a string assertion that
         the generated page contains "pf-st-activity", "Never used" and "Last denied". If that file has a
         browser test pattern for the token list (test_browser_smoke.py), do not add one in this phase.
      7. If tests/conftest.py resets the scoped token store with set_store(None), nothing more is needed;
         if the store is a module-level singleton that tests share without a reset, add the reset there and
         list tests/conftest.py in your report (it is not in touches; stop with status=blocked instead if a
         reset is missing, because another phase may run in parallel).
      8. Run ruff check ., python3 -m pytest tests/unit -q, python3 -m mypy src/privacyfence,
         bandit -c pyproject.toml -r src, and the coverage floor: python3 -m pytest tests/unit -q
         --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json && python3
         scripts/check_coverage_floor.py coverage.json (routes_mcp.py has a 100% floor).
      No CHANGELOG or doc edits in this phase.
    acceptance:
      - 'python3 -m pytest tests/unit/test_scoped_tokens.py -q passes'
      - 'python3 -m pytest tests/unit/test_scoped_tokens.py -q --cov=privacyfence.scoped_tokens --cov-branch --cov-report=term-missing shows no missing lines in note_use, note_denial or activity_for'
      - 'python3 -m pytest tests/unit/web/test_routes_mcp.py tests/unit/test_gate.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q passes'
      - 'grep -n "scoped_token_activity.json" src/privacyfence/scoped_tokens.py matches'
      - 'grep -n "pf-st-activity" src/privacyfence/settings_window_html.py matches'
      - 'python3 -m pytest tests/unit -q --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json && python3 scripts/check_coverage_floor.py coverage.json passes'
      - 'python3 -m pytest tests/unit -q && test ! -e authority/scoped_token_activity.json succeeds'
      - 'grep -n "asyncio.to_thread(store.note_use" src/privacyfence/web/routes_mcp.py matches'
      - 'ruff check . and python3 -m mypy src/privacyfence pass'

  - id: p3-sync-result-file
    title: privacyfence-sync run --result-file reports the outcome, including no_change
    depends_on: []
    complexity: S
    touches:
      - sync/src/privacyfence_sync/result.py
      - sync/src/privacyfence_sync/runner.py
      - sync/src/privacyfence_sync/cli.py
      - sync/tests/test_result.py
      - sync/tests/test_runner.py
      - sync/tests/test_cli.py
    brief: |
      Implement Design D1 of docs/sync-scheduling-support-plan.md exactly. Work in sync/:
      python3 -m pip install -e "./sync[test]" mypy bandit first.
      1. In sync/src/privacyfence_sync/runner.py change run_recipe and _run_steps to return str | None
         (the stopping step id from StopRun, else None) and update run_recipe's docstring. grep -rn
         "run_recipe(" sync/ src/ to find every caller; if one exists outside sync/src/privacyfence_sync/cli.py
         and sync/tests, stop with status=blocked.
      2. Update sync/tests/test_runner.py's assertions on run_recipe's return value (True becomes None,
         False becomes the step id).
      3. Create sync/src/privacyfence_sync/result.py with OUTCOMES, outcome_for and write_result per D1
         (atomic write via a ".tmp" sibling and os.replace, json.dumps with indent=1 and a trailing
         newline, times formatted with .strftime("%Y-%m-%dT%H:%M:%SZ") on UTC datetimes).
      4. In sync/src/privacyfence_sync/cli.py add --result-file (type=Path, default None) to the run
         subparser. Make _run return the stopped step id. Restructure main so that for the run command
         the exit code is computed by the existing except-chain and then, if args.result_file is set,
         write_result is called with the recipe name (None when load_recipe raised), the exit code,
         stopped_at, and the start/finish times; a write OSError prints exactly
         "privacyfence-sync: could not write the result file: <exc>" to stderr and does not change
         the exit code. Keep every existing message and code path otherwise unchanged.
      5. Create sync/tests/test_result.py (outcome_for for 0/None, 0/"x", 1, 2, 3, 4; write_result's
         JSON keys and Z timestamps; no .tmp left behind) and extend sync/tests/test_cli.py using its
         FakeSession/daemon fixtures: run with --result-file writes outcome "ok"; a script step exiting
         10 gives "no_change" with stopped_at_step and exit 0; ToolDenied gives "denied" and exit 3; a
         bad recipe gives "recipe_error", recipe null, exit 1; an unwritable result path keeps exit 0 and
         prints the message.
      6. Run, from the repo root: python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch
         --cov-report=term-missing --cov-fail-under=100, mypy sync/src, bandit -r sync/src, ruff check .
      No README or CHANGELOG edits in this phase.
    acceptance:
      - 'python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes'
      - 'python3 -m pytest sync/tests/test_result.py sync/tests/test_cli.py -q -k "result" passes'
      - 'grep -n "\-\-result-file" sync/src/privacyfence_sync/cli.py matches'
      - 'mypy sync/src and bandit -r sync/src pass'

  - id: p4-sync-permissions
    title: privacyfence-sync permissions lists the rules a recipe needs; preflight points at them
    depends_on: [p3-sync-result-file]
    complexity: M
    touches:
      - sync/src/privacyfence_sync/permissions.py
      - sync/src/privacyfence_sync/preflight.py
      - sync/src/privacyfence_sync/cli.py
      - sync/tests/test_permissions.py
      - sync/tests/test_preflight.py
      - sync/tests/test_cli.py
    brief: |
      Implement Design D3 of docs/sync-scheduling-support-plan.md exactly. The daemon side (D2) is
      built in a parallel phase; here the server is faked, so rely only on the suggested_rules shape
      written in D2. Work in sync/ (python3 -m pip install -e "./sync[test]" mypy bandit).
      1. Create sync/src/privacyfence_sync/permissions.py with list_permissions per D3. Reuse
         preflight._check_args, preflight._connector_of and preflight.META (import them; do not copy).
         Status text, the summary lines and the --json shape are exactly as in D3's table and text.
      2. In sync/src/privacyfence_sync/preflight.py extend _check_step's needs-approval branch with the
         "; add: ..." suffix per D3 (first suggestion only).
      3. In sync/src/privacyfence_sync/cli.py add the permissions subparser (recipe, --server,
         --token-env, --json; allow_abbrev=False) and a _permissions coroutine shaped like _preflight;
         in main it returns 0 on completion and goes through the existing except-chain on errors. The
         ToolDenied -> SignInError mapping for privacyfence_status, the scoped token line and the
         "token sees no tools" SignInError all live in list_permissions, as in preflight.py:66-89.
      4. Create sync/tests/test_permissions.py with a scripted fake client (copy the FakeSession pattern
         from sync/tests/test_cli.py; sync/tests use plain functions, keep that style): the D3 status
         order (covered wins over dropped args; dropped args with requires_review is unknown); one test
         per status (auto, covered, not_checked from a ToolFailed, needs_rule with a known value,
         needs_rule with value_known false, needs_rule with suggested_rules absent, needs_rule with [],
         unknown with dropped args, not_available); the "Rules this recipe needs" union merges verbs for
         the same group and value; the tools line lists every tool step sorted; --json output parses and
         has suggested_rules null when the key was absent; no privacyfence_begin_unattended_session call
         is made.
      5. Extend sync/tests/test_preflight.py: a needs-approval finding with suggested_rules prints the
         "; add:" suffix; without it the line is unchanged.
      6. Extend sync/tests/test_cli.py: permissions exits 0 when rules are needed, 4 when status is denied,
         1 for a bad recipe.
      7. Run python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch
         --cov-report=term-missing --cov-fail-under=100, mypy sync/src, bandit -r sync/src, ruff check .
      No README or CHANGELOG edits in this phase.
    acceptance:
      - 'python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes'
      - 'python3 -m pytest sync/tests/test_permissions.py -q passes'
      - 'grep -n "Rules this recipe needs:" sync/src/privacyfence_sync/permissions.py matches'
      - 'grep -n "; add: " sync/src/privacyfence_sync/preflight.py matches'
      - 'mypy sync/src and bandit -r sync/src pass'

  - id: p5-docs-adrs-retire
    title: Docs, changelog, ADRs 0204-0206, and retire the plan
    depends_on: [p1-check-policy-suggestions, p2-token-activity, p4-sync-permissions]
    complexity: S
    touches:
      - sync/README.md
      - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md
      - docs/privacyfence-sync.md
      - docs/org-mode-setup-guide.md
      - docs/how-it-works.md
      - docs/security-and-compliance.md
      - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/SKILL.md
      - CHANGELOG.md
      - docs/adr/0204-check-policy-names-the-rules-that-would-cover-an-uncovered-call.md
      - docs/adr/0205-privacyfence-sync-reports-a-runs-outcome-in-an-opt-in-result-file.md
      - docs/adr/0206-scoped-token-activity-is-kept-in-its-own-file.md
      - docs/adr/README.md
      - docs/sync-scheduling-support-plan.md
      - docs/sync-scheduling-support-plan-manual-steps.html
    brief: |
      1. Update sync/README.md per Design D5: the Commands section documents `run --result-file` (with
         the D1 field table and outcome list) and `permissions` (with one example text output and the
         --json shape from D3); the Scheduling section gains the subsection "On a server, against
         organization mode" with the five numbered points of D5. Match the README's existing voice and
         heading levels. Also make the two README edits D5 lists for :87-95 and :97-145. Then copy it
         byte-for-byte to
         plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md.
      2. docs/privacyfence-sync.md: one paragraph pointing at that subsection. Make the D5 edits to
         docs/how-it-works.md, docs/security-and-compliance.md and the recipe skill's SKILL.md.
      3. docs/org-mode-setup-guide.md: add "### Scheduled recipes" at the end of "## 13. File delivery"
         per D5 (no renumbering), linking ../sync/README.md's subsection anchor and ADR 0161; add it to
         the Contents list only if that list already names subsections.
      4. CHANGELOG.md under "## [Unreleased]" (never a version heading): three lines, user-facing:
         privacyfence-sync's new `permissions` command and `run --result-file`; privacyfence_check_policy's
         suggested_rules; Settings showing each scoped token's last use and last denial.
      5. Before writing the ADRs, delete docs/sync-scheduling-support-plan.md and
         docs/sync-scheduling-support-plan-manual-steps.html in a commit of their own ("Retire the
         sync scheduling support plan"), and note that commit's SHA. Read the plan from your working
         copy before deleting it (or with git show <SHA>^:docs/sync-scheduling-support-plan.md after).
      6. Write ADRs 0204, 0205, 0206 from the plan's ADRs section and "Rejected alternatives", using
         docs/adr/README.md's template: Status "Accepted — <today>"; Verification naming the code and
         tests from phases p1-p4; Related citing the deleted plan only as
         `git show <SHA>^:docs/sync-scheduling-support-plan.md` (ADR rule 4: never a link). Add the three
         rows to docs/adr/README.md's index. If 0204-0206 are taken on main by then, take the next free
         numbers and rename the files accordingly. Afterwards
         grep -rn "sync-scheduling-support-plan" . --exclude-dir=.git matches only those Related lines.
      7. Run ruff check ., python3 -m pytest tests/unit -q, and the website/docs link checks if the repo has
         them (grep tests/ for a docs link test and run it).
    acceptance:
      - 'test ! -e docs/sync-scheduling-support-plan.md && test ! -e docs/sync-scheduling-support-plan-manual-steps.html succeeds'
      - 'cmp sync/README.md plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md succeeds'
      - 'grep -n "result-file" sync/README.md matches and grep -n "privacyfence-sync permissions" sync/README.md matches'
      - 'grep -n "Scheduled recipes" docs/org-mode-setup-guide.md matches'
      - 'git diff --name-only --diff-filter=A origin/main -- docs/adr | wc -l prints 3'
      - 'grep -n "scoped_token_activity.json" docs/security-and-compliance.md matches'
      - 'grep -n "suggested_rules" docs/how-it-works.md matches'
      - 'grep -n "permissions" plugins/privacyfence-sync/skills/privacyfence-sync-recipes/SKILL.md matches'
      - 'awk ''/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f'' CHANGELOG.md | grep -c "result-file\|suggested_rules\|last use" is at least 3'
```
