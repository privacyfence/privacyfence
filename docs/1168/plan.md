# Plan: a Drive folder rule that follows listings into subfolders (issue 1168)

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** In Settings → Auto-accept → Add a rule (or in a scoped token's rules) pick "Google Drive — a folder and the subfolders reached by listing it", paste one folder id and tick read and/or download. A workflow (n8n, an agent) then walks the tree with `drive_list_folder`, one call per folder, and reads or downloads each file with `drive_get_file_content` or `drive_download_file` with no popup. A subfolder is covered only after a listing that started at the rule's folder has shown it; a file in an unlisted subfolder still asks.

**Scope:** One new scope `drive.folder_tree` with the verbs read and download. The listing tools stay ungated. The connector remembers, in memory per token, the subfolder ids a listing returned under a covered folder, for one hour. A read checks only the file's own parents against that remembered set: no ancestry walk, no extra Drive call. The existing "Drive folder" rule stays direct-parent only and no existing rule changes. Write verbs, move, copy and upload are not covered.

**Security:** This is a trust-boundary change: a rule now reaches below the folder it names. Access is bounded by what the rule's folder actually lists, shortcuts are never followed, a remembered folder expires after one hour and holds at most 2000 subfolders per rule, and a file moved out of the tree stops matching at once because its parents are re-read on every call. A folder moved out of the tree stays covered until it expires.

**Performance:** No extra Drive calls. One in-memory lookup per gated read and one per folder listing.

**Review focus:** the per-root grant map and why a grant never leaks between two rules of one token (3.3, 3.4); the expiry, cap and moved-folder trade-off (3.3, 6); that nothing else becomes ungated or newly covered (3.1).

## 1. Goal

A workflow can read a whole Drive folder tree by listing it level by level, when only the top folder has a rule, without making every single read walk the file's ancestors. Issue: https://github.com/privacyfence/privacyfence/issues/1168. Asked: "I added drive property tag based auto accept rule to workaround lack of recursive drive folder rules. I still want to be cautious with the api calls so I would not add in general recursive auto accept ... but maybe it is easier in a workflow setup like n8n: read directory recursively (list files and folders) and get contents or download files even if just the top level directory has auto access token."

## 2. Current state

- A Drive folder rule matches only a file whose direct parent is listed: `_approved_folder_matches` (`src/privacyfence/policy/scopes.py:179`). ADR 0320 declined a recursive rule because Drive has no ancestor query (one `files.get` per level on every gated call). `docs/approvals-and-policy.md:391` says "not recursive".
- `drive_list_folder`, `drive_list_files`, `drive_get_file_metadata` and `drive_list_revisions` are `auto` tools: no `gated_call`, no rule consulted, under a scoped token they only need to be in the token's tool list (`src/privacyfence/connectors/drive.py:998`, `_list_folder`). So a workflow can already list any tree; it is the per-file reads that stop at the first subfolder.
- The four gated read tools are `drive_get_file_content` (`drive.py:1166`, `args={"file_id": ...}`), `drive_get_revision_content` (`drive.py:1242`), `drive_sheets_get_values` (`drive.py:1301`, args `spreadsheet_id`, `range_a1`, ...) and `drive_download_file` (`drive.py:1435`). They map to the operations `drive.read_file_contents`, `sheets.read_values` (read) and `drive.download_file` (download). Each passes the fetched `DriveFile`, with `parent_ids`, as `raw_data`.
- A scoped token holds only its own rules and never the temp-accept grace window (`src/privacyfence/gate.py:724`, ADR 0164, ADR 0236). The grace window covers no read operation.
- The closest pattern is `gmail.query` (ADR 0222): the connector computes a value before gating and passes it in `args`, the selector reads `ctx.args["matching_queries"]` (`scopes.py:685`, `connectors/gmail.py:842`), and `_CONNECTOR_COMPUTED_ARGS` (`src/privacyfence/web/mcp_dispatch.py:55`) keeps an agent from supplying it to `privacyfence_check_policy`. The pattern for a deliberately added extra scope that no card proposes is `drive.tag` (commit `85b186ef5`, ADR 0320). `connector.session_created_ids` (`drive.py:152`) is the existing per-principal in-memory set.
- `rule_values(predicate, operation_key)` (`gate.py:741`) returns the current call's rule values for a predicate, under a token only the token's own.

## 3. Design

### 3.1 What is added and what is not

One new extra scope, one new connector-side memory, nothing else. Rejected: (a) making `approved_folder` recursive, which silently widens every existing rule and token, and needs an ancestor walk (ADR 0320); (b) a rule that walks ancestors per read, the cost the owner wants to avoid; (c) tagging (ADR 0320 to 0322), which stays and needs a popup per tree; (d) persisting remembered folders on disk or sharing them across tokens, which would outlive the decision that created them; (e) granting from `drive_list_files` search results, which are not rooted in the rule's folder. The listing tools stay `auto`; `drive_get_file_metadata` stays ungated. No gate, operation key, tool description or `ToolSpec` changes.

### 3.2 The scope

- Predicate and group id: `drive.folder_tree`; scope type `drive.folder_tree` (`describe.scope_type_label` renders it "folder tree"). Value: one or more Drive folder ids, validated by the existing `_DRIVE_FOLDER` format of `src/privacyfence/policy/value_format.py:49`.
- `EXTRA_SCOPES["drive.folder_tree"] = PolicyExtraScope(predicate="drive.folder_tree", connector="drive", verbs=(Verb.READ, Verb.DOWNLOAD), label="Google Drive — a folder and the subfolders reached by listing it", needs_value=True, value_hint="a folder id, for example 1AbCdEfGhIjKlMnOpQrSt")`. The operations it derives are `drive.read_file_contents`, `sheets.read_values` and `drive.download_file`. It is added from Settings or through a scoped token's rules and is never proposed on a card (it is not in `PROPOSABLE_SCOPES`).
- Selector `_drive_folder_tree_matches(value, ctx)` in `scopes.py`, registered in `NEW_SCOPE_SELECTORS["drive.folder_tree"]` with `kind=ScopeKind.IDENTITY`, `resolves_from=ResolvesFrom.FETCHED`. It returns False for an empty value. Otherwise `parents = set(getattr(_file_from(ctx.raw_data), "parent_ids", []) or [])`, `listed = ctx.args.get("listed_folders")` (treated as `{}` unless it is a dict), and for each `root` in `_values_of(value)` the covered set is `{root} | {str(i) for i in listed.get(root, []) if listed.get(root) is a list}`; it returns True when `parents` intersects the covered set of any root, else False. A rule therefore only ever uses the grants recorded under its own roots.
- `"listed_folders"` is added to `_CONNECTOR_COMPUTED_ARGS`.

### 3.3 The memory: `src/privacyfence/drive_listing_grants.py`

Pure module, no Drive or gate imports.

- Constants: `FOLDER_MIME = "application/vnd.google-apps.folder"`, `GRANT_TTL_SECONDS = 3600`, `MAX_GRANTED_PER_ROOT = 2000`, `_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,100}")`.
- `class ListingGrants` with `__init__(self, clock: Callable[[], float] = time.monotonic)`, a `threading.Lock`, and a dict `(key, root) -> {folder_id: expiry}`; `key` is the scoped token's id, or `""` for an unscoped call.
- `note_listing(self, key: str, roots: Iterable[str], listed_folder_id: str, child_folder_ids: Iterable[str]) -> None`: for each `root`, drop expired entries of `(key, root)`; continue only when `listed_folder_id == root` or `listed_folder_id` is a live entry of `(key, root)`; add each child id that fully matches `_ID_RE` with expiry `clock() + GRANT_TTL_SECONDS` (an id already present has its expiry refreshed); once the root holds `MAX_GRANTED_PER_ROOT` live ids stop adding for that root (never evict: fail closed).
- `granted(self, key: str, roots: Iterable[str]) -> dict[str, list[str]]`: for each `root`, drop expired entries and return only roots that still hold at least one live id, as a sorted list.
- A folder's own grant is never extended by reading a file in it; only a fresh listing of its parent renews it. A workflow that needs a tree for longer than an hour lists it again.

### 3.4 The connector (`connectors/drive.py`)

- `DriveConnector.__init__`: `self.listing_grants = ListingGrants()` (per connector instance, so per principal, like `session_created_ids`; never persisted, forgotten at restart).
- `_tree_key(self) -> str`: `scope.id` of `current_token_scope()` (import from `..scoped_tokens`), else `""`.
- `_tree_roots(self) -> list[str]`: `rule_values("drive.folder_tree", "drive.read_file_contents") + rule_values("drive.folder_tree", "drive.download_file")` plus `rule_values("drive.folder_tree", "sheets.read_values")`, deduplicated in order.
- `_tree_args(self) -> dict[str, Any]`: `granted = self.listing_grants.granted(self._tree_key(), self._tree_roots())`; returns `{"listed_folders": granted}` when `granted` is non-empty, else `{}`.
- `_list_folder`: after the listing and the audit line, call `self.listing_grants.note_listing(self._tree_key(), self._tree_roots(), folder_id, [f.id for f in files if f.mime_type == FOLDER_MIME])`. Shortcuts have another MIME type and are never granted. The returned value does not change.
- The four gated read calls (`_get_file_content`, `_get_revision_content`, `_sheets_get_values`, `_download_file`) spread `**self._tree_args()` into their `args` dict. No other call site changes.

### 3.5 Behaviour table (pinned by tests)

| Step | Result |
|---|---|
| Rule on folder R (read). List R | child folders A, B granted under root R |
| Read a file whose parent is R | covered (root itself) |
| Read a file whose parent is A | covered |
| Read a file whose parent is C, never listed | asks (token: denied) |
| List A, then read a file in A/A1 | covered once A/A1 was returned by the listing of A |
| Same token, second rule on root S, read a file in A | not covered by the S rule |
| Another token, same rule value, read a file in A | not covered until that token lists R and A |
| File moved from A to an unrelated folder, then read | not covered (parents re-read) |
| One hour after the listing, read a file in A | not covered; list R again |
| Write, move, copy, upload | never covered (verbs read and download only) |

### 3.6 ADR

ADR 0333, "A Drive folder-tree rule follows the folders a listing returned", Accepted: the opt-in scope; listing-derived, in-memory, per-token, per-root grants with a one hour expiry and a 2000 cap; the rejected alternatives of 3.1; the moved-folder trade-off. It relates to ADR 0320 (which declined recursion because of ancestry walks; this adds none), 0164 and 0236 (token rules), 0222 (the computed-arg pattern).

## 4. ADRs

- 0333, "A Drive folder-tree rule follows the folders a listing returned" (3.6).

## 5. Manual steps

### 1. Walk a real folder tree from n8n (after the last merge, about 15 minutes)

**After implementation**

1. Open PrivacyFence Settings → Auto-accept and read the new scope's description in [the approvals reference](https://github.com/privacyfence/privacyfence/blob/feature/drive-folder-tree-rule/docs/approvals-and-policy.md).
2. In a Drive test folder make `top/`, `top/sub/` and `top/sub/deep/` and put one small text file in each. You see three files.
3. Ask the AI client to propose a scoped token with the rule group `drive.folder_tree`, the id of `top/`, verbs read and download, the tools `drive_list_folder`, `drive_get_file_content` and `drive_download_file`, and deliver it to an n8n credential, following [the n8n node guide](https://github.com/privacyfence/privacyfence/blob/feature/drive-folder-tree-rule/docs/n8n-node.md). Approve it on the review page.
4. In n8n run a workflow that lists `top/`, then each subfolder it returns, then reads each file. You see all three files read with no approval card.
5. Move `sub/deep/` file to a folder outside `top/` in Drive and read it again: it must be refused.
6. Report back one pass/fail line for the pull request: "tree of three levels read without a popup; moved file refused".

## 6. Risks and open questions

- The reach of a rule is now whatever its folder lists, one hour at a time. Anyone who can create a subfolder or move a folder into the tree extends what a workflow can read, exactly as anyone who can put a file into a folder extends a direct folder rule today. A folder moved out of the tree after a listing stays readable for up to one hour. A worker that sees a design question here stops with `status=blocked`.
- Remembered folders are lost when the daemon restarts; a workflow then asks again and, under a token, is denied until it lists the tree again. That is the intended failure mode.
- A listing larger than `max_results` only grants the folders it returned. The workflow sees the same truncation and has to ask for a larger `max_results` itself.
- Personal data: a file with an IP address or an email still needs `allow_personal_data` or `allow_personal_data_reads` on the token for a read to pass, as before.
- If `rule_values` or `current_token_scope()` cannot be imported into `connectors/drive.py` without an import cycle, stop with `status=blocked`: the brief of p2 depends on `from ..gate import rule_values` working as `connectors/gmail.py` does.
- If a docs-drift test fails for the new scope (`tests/unit/test_generate_always_allow_reference.py`, `tests/unit/test_docs_tools_reference.py`), p1 adds the smallest doc change that makes it pass and reports it; it does not weaken the test.
- No docs index: the profile has none.

## Implementation manifest

```yaml
plan_slug: drive-folder-tree-rule
feature_branch: feature/drive-folder-tree-rule
tracking_issue: 1168
max_parallel: 1
manual_before: []
manual_after:
  - id: ma1-walk-a-real-tree
    title: Walk a real three-level Drive tree from n8n with a scoped token
    why: CI uses fakes for Drive and n8n; only a real tree shows that the listing, the grants and the reads work together and that a moved file is refused.
verify_after_merge:
  - python3 -m pytest tests/unit -q
final_checks:
  - "`tld plan deviations check --file docs/1168/deviations.md --plan docs/1168/plan.md --all` exits 0"
  - "ADR 0333 exists, is Accepted, and is in docs/adr/README.md"
  - "CHANGELOG.md has an entry under ## [Unreleased] and no new version heading"
  - "qa-record-fixture.yml dispatched on main with connector=drive and branch=feature/drive-folder-tree-rule finished green (the dod_conditional row for connectors/**)"
phases:
  - id: p1-scope-and-grants
    title: The drive.folder_tree scope and the listing-grants memory
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/drive_listing_grants.py
      - tests/unit/test_drive_listing_grants.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/value_format.py
      - src/privacyfence/web/mcp_dispatch.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_value_format.py
      - tests/unit/policy/test_describe.py
      - tests/unit/web/test_mcp_dispatch.py
      - scripts/generate_always_allow_reference.py
      - docs/always-allow-rules-reference.md
    brief: |
      Design: sections 3.2, 3.3 and 3.5 of docs/1168/plan.md (read them first). Pattern commits: 85b186ef5 (the drive.tag scope: selector, catalogue entry, value format, tests) and 48fcdb372 (gmail.query computed arg). Do not edit .tld/toolkit.yaml or .github/workflows/**: if you think you must, report protected-change: <path> - <why> with the diff.
      1. Create src/privacyfence/drive_listing_grants.py exactly as section 3.3 says: the four constants, class ListingGrants with __init__(self, clock=time.monotonic), note_listing and granted, a threading.Lock around every access. Module docstring one paragraph: what it remembers and that it is in memory only.
      2. In src/privacyfence/policy/scopes.py add _drive_folder_tree_matches(value, ctx) next to _drive_tag_matches and register NEW_SCOPE_SELECTORS["drive.folder_tree"] (predicate "drive.folder_tree", scope_type "drive.folder_tree", kind ScopeKind.IDENTITY, resolves_from ResolvesFrom.FETCHED), as in section 3.2. Do NOT add it to TOKEN_ONLY_PREDICATES.
      3. In src/privacyfence/policy/catalogue.py add EXTRA_SCOPES["drive.folder_tree"] with the exact fields of section 3.2, placed directly after "drive.tag".
      4. In src/privacyfence/policy/value_format.py add VALUE_FORMATS["drive.folder_tree"] = _DRIVE_FOLDER (the existing object, defined before the dict).
      5. In src/privacyfence/web/mcp_dispatch.py add "listed_folders" to _CONNECTOR_COMPUTED_ARGS.
      6. Tests, one behaviour each, copying the style of the drive.tag tests in the same files: tests/unit/test_drive_listing_grants.py (a fake clock list: grant from the root only; no grant for an unrelated listed folder; a grandchild granted after its parent was listed; expiry after 3600 s and refresh on a second listing; the 2000 cap with 2001 children grants 2000; a child id that fails _ID_RE is ignored; two keys and two roots do not see each other's grants; granted() omits an empty root). tests/unit/policy/test_scopes.py: selector true for the root as parent, true for a granted folder, false for an ungranted folder, false when the grant is listed under another root, false for an empty value, false when listed_folders is not a dict, a rule with two roots matches either. tests/unit/policy/test_catalogue.py: the entry exists, derives exactly {drive.read_file_contents, sheets.read_values} for read and {drive.download_file} for download, and rules_for_catalogue_entry("drive.folder_tree", ["<valid id>"], [Verb.READ, Verb.DOWNLOAD]) returns rules with the predicate drive.folder_tree. tests/unit/policy/test_value_format.py: a 20-character id is accepted, "not an id!" is rejected. tests/unit/policy/test_describe.py: the rule sentence names the folder id and "folder tree". tests/unit/web/test_mcp_dispatch.py: assert "listed_folders" is in _CONNECTOR_COMPUTED_ARGS next to the existing computed-arg assertion.
      7. Run python3 -m pytest tests/unit -q and ruff check . . If tests/unit/test_generate_always_allow_reference.py or another docs-drift test fails because of the new scope, make the smallest change to scripts/generate_always_allow_reference.py (the _FOOTER text) and regenerate docs/always-allow-rules-reference.md with python3 scripts/generate_always_allow_reference.py; if no test fails, leave both files alone.
      Stop with status=blocked if rules_for_catalogue_entry rejects the new group or if the existing tests that enumerate EXTRA_SCOPES fail in a way a one-line expectation update does not fix.
    acceptance:
      - python3 -m pytest tests/unit/test_drive_listing_grants.py tests/unit/policy -q passes
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py -q passes
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
      - python3 -m pytest tests/unit/test_drive_listing_grants.py -q --cov=privacyfence.drive_listing_grants --cov-branch --cov-report=term-missing reports 100%
      - grep -n '"drive.folder_tree"' src/privacyfence/policy/scopes.py src/privacyfence/policy/catalogue.py src/privacyfence/policy/value_format.py matches in each file
  - id: p2-connector-listing-grants
    title: The Drive connector records listings and passes the grants to the gate
    depends_on: [p1-scope-and-grants]
    complexity: M
    touches:
      - src/privacyfence/connectors/drive.py
      - tests/unit/connectors/test_drive_connector.py
      - tests/unit/test_gate_real_evaluator.py
    brief: |
      Design: sections 3.4 and 3.5 of docs/1168/plan.md (read them first). Pattern: connectors/gmail.py `_matching_queries` and the `args=` spread at its gated_call sites. Do not edit .tld/toolkit.yaml or .github/workflows/**: report protected-change: <path> - <why> with the diff if you think you must.
      1. In src/privacyfence/connectors/drive.py import ListingGrants and FOLDER_MIME from ..drive_listing_grants, current_token_scope from ..scoped_tokens and rule_values from ..gate (check how connectors/gmail.py imports rule_values and copy it).
      2. In DriveConnector.__init__ add self.listing_grants = ListingGrants() after self.session_created_ids, with a one-line comment: per connector instance like session_created_ids, in memory only.
      3. Add _tree_key, _tree_roots and _tree_args exactly as section 3.4 says (rule_values called for drive.read_file_contents, drive.download_file and sheets.read_values; order-preserving dedupe).
      4. In _list_folder, after the existing self._auto_audit(...) call, add the note_listing call of section 3.4. Return value unchanged.
      5. Spread **self._tree_args() into the args dict of the gated_call in _get_file_content (args={"file_id": file_id}), _get_revision_content, _sheets_get_values and _download_file. No other gated_call changes.
      6. Tests in tests/unit/connectors/test_drive_connector.py, using the module's existing stubbed gated_call and mocked DriveClient helpers: with rule_values patched to return ["root-folder-id-1"] for drive.folder_tree, listing "root-folder-id-1" returning one folder child (mime application/vnd.google-apps.folder), one shortcut child (application/vnd.google-apps.shortcut) and one file, then get_file_content gets args["listed_folders"] == {"root-folder-id-1": [<the folder id>]} and not the shortcut or the file; the same for download_file, sheets_get_values and get_revision_content; listing an unrelated folder leaves "listed_folders" out of args; with no rule values nothing is recorded and "listed_folders" is absent; the returned listing is unchanged. In tests/unit/test_gate_real_evaluator.py add one end-to-end class that builds a scoped token with a single drive.folder_tree row for read, enters token_scope, and shows with the real evaluator that a file in a listed subfolder auto-accepts and a file in an unlisted subfolder is denied (model it on the approved_folder preflight near line 1045 and on TestScopedTokenGate in tests/unit/test_gate.py:4274 for building the token). Also assert two tokens do not share grants.
      7. Run python3 -m pytest tests/unit -q and ruff check . .
      Stop with status=blocked if importing rule_values or current_token_scope into connectors/drive.py raises an import cycle, or if the real-evaluator test cannot build a scoped token without changing production code.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_drive_connector.py -q passes
      - python3 -m pytest tests/unit/test_gate_real_evaluator.py -q passes
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
      - python3 -m mypy src/privacyfence passes
      - grep -c "_tree_args" src/privacyfence/connectors/drive.py reports 5
  - id: p3-docs-adr-retire
    title: Reference docs, the n8n walk recipe, ADR 0333 and the changelog
    depends_on: [p2-connector-listing-grants]
    complexity: S
    touches:
      - docs/adr/0333-a-drive-folder-tree-rule-follows-the-folders-a-listing-returned.md
      - docs/adr/README.md
      - docs/approvals-and-policy.md
      - docs/always-allow-rules-reference.md
      - scripts/generate_always_allow_reference.py
      - docs/n8n-node.md
      - CHANGELOG.md
    brief: |
      Design: sections 3 and 6 of docs/1168/plan.md. Standing docs describe today's behaviour with no history, no issue numbers and no "previously" (docs/coding-and-testing-guidelines.md section 4). Do not edit .tld/toolkit.yaml or .github/workflows/**.
      1. Create docs/adr/0333-a-drive-folder-tree-rule-follows-the-folders-a-listing-returned.md in the format of docs/adr/0320-drive-rules-match-on-file-tags-not-folder-ancestry.md (Status Accepted with today's date 2026-11-20 and an "Implemented:" list naming src/privacyfence/drive_listing_grants.py, src/privacyfence/policy/scopes.py (drive.folder_tree), src/privacyfence/policy/catalogue.py, src/privacyfence/connectors/drive.py; Context, Decision, Alternatives considered, Consequences, Verification, Related). The Decision states in full: the opt-in scope drive.folder_tree with the verbs read and download; the listing-derived grants, in memory, per token, per root; the one hour expiry and the 2000 per root cap; shortcuts never followed; the file's parents re-read on every call; listing tools stay ungated. Alternatives: the five rejected options of plan section 3.1. Consequences: the moved-folder trade-off, restart forgets grants, the rule reaches whatever its folder lists. Relates to 0164, 0222, 0236, 0320, 0322 (links), issue https://github.com/privacyfence/privacyfence/issues/1168. The ADR must not rely on the plan to explain itself.
      2. Add its row to the table in docs/adr/README.md after the 0332 row, in the same column format, "Accepted; relates to 0164, 0222, 0236, 0320".
      3. In docs/approvals-and-policy.md add a row "Drive folder tree" to the scope table after the "Drive files with a tag" row: kind identity, value folder ids, predicate `drive.folder_tree`, text "the file's parent is one of the folders, or a subfolder that a drive_list_folder call returned under one of them within the last hour (by the same token for a scoped token). Covers read (also revisions, sheet values) and download only. Added only from Settings or a token's rules, never on a card ([ADR 0333](adr/0333-a-drive-folder-tree-rule-follows-the-folders-a-listing-returned.md))". Leave the existing "Drive folder" row's text unchanged.
      4. In scripts/generate_always_allow_reference.py add a "### Google Drive — a folder and the subfolders reached by listing it" section to _FOOTER after the tag section: where it is added, the verb table (read, download), the rule "a subfolder is covered after a listing that started at the rule's folder returned it; a file in an unlisted subfolder still asks; the memory is per token, in memory, and expires after one hour", and the link to ADR 0333. Regenerate with python3 scripts/generate_always_allow_reference.py and commit docs/always-allow-rules-reference.md.
      5. In docs/n8n-node.md add a section "Reading a whole Drive folder" after the worked example: the token needs a drive.folder_tree rule for the top folder with read and download, and the tools drive_list_folder, drive_get_file_content and/or drive_download_file; the workflow lists the folder, loops over the folders it returns (a Code node queue with a back-edge) and reads or downloads each file; reads work only for folders listed within the last hour; a token needs allow_personal_data or allow_personal_data_reads when files contain personal data. No Drive-specific node code exists or is added.
      6. Add under ## [Unreleased] in CHANGELOG.md, with a ### Added heading if none exists under it: "- **A Drive rule that follows listings into subfolders.** The new scope "Google Drive — a folder and the subfolders reached by listing it" covers read and download of files in a folder and in the subfolders a `drive_list_folder` call returned under it, so a workflow can read a whole tree with one rule on the top folder. Add it in Settings or in a scoped token's rules ([ADR 0333](docs/adr/0333-a-drive-folder-tree-rule-follows-the-folders-a-listing-returned.md), [#1168](https://github.com/privacyfence/privacyfence/issues/1168))." Never add a version heading.
      7. Run python3 -m pytest tests/unit -q and ruff check . .
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
      - python3 scripts/generate_always_allow_reference.py leaves git diff --exit-code docs/always-allow-rules-reference.md exiting 0
      - grep -n "0333" docs/adr/README.md matches one row
      - grep -n "^## \[" CHANGELOG.md lists "## [Unreleased]" before "## [8.1.0]" and no new version heading
      - grep -n "drive.folder_tree" docs/approvals-and-policy.md docs/always-allow-rules-reference.md docs/n8n-node.md matches in each file
```
