# Plan: Drive auto-accept rules by file tag (issue 1147)

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** Ask the agent to tag a file (`drive_set_file_tags`, one approval popup) or a whole folder tree in one call (`drive_tag_folder_tree`, one popup that shows how many files change). Then in Settings → Auto-accept add the rule "Google Drive — files with a tag", type the tag name and tick the verbs. From then on any call on a file carrying that tag is auto-accepted, wherever the file sits, with no folder walking and no extra Drive calls. `drive_get_file_metadata` and the listing tools now show a file's `tags`.

**Scope:** Tags are stored as Drive `properties` named `privacyfence-tag.<name>` with the value `1`. One new rule scope `drive.tag` covers read, download, update, restructure and delete of a single file. It never covers move, copy or upload, which need a destination check. There is no recursive-folder rule: Drive cannot answer "is this inside folder X at any depth", so a tag replaces it. Nothing changes for existing rules.

**Security:** Properties are writable by anyone with edit access, so a collaborator can forge a tag on a file; the owner chose this over signed tags and it is recorded as an accepted risk (section 3.2, ADR 0321). The two tag-writing tools are never coverable by any rule, enforced in the engine so even a hand-edited custom rule cannot cover them (section 3.5, ADR 0322). The agent only ever sees PrivacyFence's own tags, never other apps' properties.

**Performance:** One extra field on every Drive metadata fetch. The tree tool batches up to 40 folders per query and skips files already tagged.

**Review focus:** the forgery trade-off (3.2); the exempt-operations enforcement in the engine (3.5); the `extra_operations_for` namespace fix (3.4), which makes Docs and Sheets operations coverable by a tag rule and must leave every existing extra scope unchanged; the tree walk caps and the "apply exactly the shown snapshot" rule (3.7).

## 1. Goal

A user can label Drive files and write auto-accept rules on those labels, so a rule can cover files at any depth without checking ancestors. Issue: https://github.com/privacyfence/privacyfence/issues/1147. Asked: "is there any properties for google drive to be able read and write? I would need recursive auto accept rules (for subfolders) but I want to avoid that many calls... I have a properties idea to label files and create rules for tags". Owner decisions (board asks of 2026-11-17): store tags in `properties`, not `appProperties`; ship the single-file and the folder-tree tag tools; tag rule only, no recursive-folder rule; plain tags, not signed.

## 2. Current state

- Drive's API has `properties` (visible to every app, writable by anyone with edit access) and `appProperties` (visible only to the writing OAuth client). The connector holds the full `drive` scope (`src/privacyfence/drive_client.py:43`), so neither needs new consent. No tool reads or writes either today. `_FILE_FIELDS` (`drive_client.py:68`) has `parents` but not `properties`; `DriveFile` (`drive_client.py:1139`) has no tags; `_parse_file` is at `drive_client.py:2865`.
- Folder rules check direct parents only: `_approved_folder_matches` (`policy/scopes.py:179`). Drive has no ancestor query (`'<id>' in parents` matches direct children only), so a recursive rule would need one `files.get` per level per call. Nothing in the repo walks ancestry.
- A selector that reads an attribute of the fetched file is the pattern: `_label_match_matches` (`policy/scopes.py:236`); a deliberately configured, value-taking scope that the popup never proposes is `gmail.query` (pattern commit `48fcdb372`; ADR 0222) and `miro.board` (`9ec30d4c0`, ADR 0232). A new Drive tool follows `f0fc5691b` (`drive_copy_file`).
- Every gated Drive tool except `drive_upload_file` passes the `DriveFile` from `get_file_metadata` as `raw_data`, so a field added to `_FILE_FIELDS` reaches every item-scoped rule (`connectors/drive.py`, one `get_file_metadata` per gated tool).
- `catalogue.extra_operations_for` (`policy/catalogue.py:162`) keeps operations whose key prefix equals the connector, so `sheets.*` and `docs.*` operations never derive for a `drive` extra scope although they belong to the Drive connector (`propose.connector_of_operation` knows this). No extra scope uses `drive` today, so nobody noticed.
- `propose.operations_for` (`policy/propose.py:471`) derives the operations of a scope from the verb, so any new `UPDATE` tool would leak into the sandbox-folder rule unless excluded. Per-scope `excludes` do not bind custom rows (`policy/scope_rows.py`, `policy/engine.py:98`).

## 3. Design

### 3.1 Names and shapes

- New module `src/privacyfence/drive_tags.py` (pure, no I/O):
  - `TAG_PROPERTY_PREFIX = "privacyfence-tag."`, `TAG_VALUE = "1"`, `TAG_NAME_RE = r"[a-z0-9][a-z0-9._-]{0,39}"`, `MAX_TAGS_PER_CALL = 10`.
  - `normalise_tag(raw: str) -> str` strips and lowercases, raises `ValueError(f"{raw!r} is not a valid tag name. Use lowercase letters, digits, '.', '_' and '-', 1 to 40 characters, starting with a letter or digit.")`.
  - `parse_tag_list(text: str) -> list[str]`: split on commas, drop empty items, `normalise_tag` each, dedupe keeping order, raise `ValueError("At most 10 tags per call.")` above `MAX_TAGS_PER_CALL`.
  - `tag_property_key(name: str) -> str` returns `TAG_PROPERTY_PREFIX + name`.
  - `tags_from_properties(properties: dict[str, str] | None) -> list[str]`: sorted names whose key starts with the prefix, whose remainder fully matches `TAG_NAME_RE` and whose value equals `TAG_VALUE`. Every other property is ignored and never exposed.
- `DriveFile` gains `tags: list[str] = field(default_factory=list)`. `_FILE_FIELDS` gains `properties`; `_parse_file` sets `tags=tags_from_properties(raw.get("properties"))`. Raw property values never leave the client.

### 3.2 Why plain `properties`, and the accepted forgery risk

The owner chose `properties` over `appProperties` and plain over signed values. Rejected: `appProperties` (hidden from the owner's own scripts and other tools, which is what the owner wants to keep usable); an HMAC-signed value (a per-install secret, tags only settable by PrivacyFence; recommended by review, declined by the owner). Consequence, to be stated verbatim in ADR 0321, the tool descriptions and the docs: anyone with edit access to a file can add or remove its tags through their own tokens or an Apps Script, so a tag rule is exactly as trustworthy as the set of people who can edit the tagged files. A READ tag rule can release content an outsider pre-tagged; an UPDATE rule can let the agent write into a file an outsider shared and pre-tagged. The rule's label carries a short warning (3.4). Tags are not copied by `files.copy` as far as Drive documents, and a `properties`-only update may change `modifiedTime` (which only makes PrivacyFence's own-write PII shortcut fail closed); the recorder check in p6 records what Drive actually does.

### 3.3 Client (p1)

In `DriveClient` (`drive_client.py`), each method uses `self._get_service()`, `supportsAllDrives=True` and the `except Exception as exc: raise DriveClientError(...) from exc` wrapping of `move_file` (`drive_client.py:2259`):

- `set_tags(file_id: str, add: list[str], remove: list[str]) -> dict`: refuses an empty `file_id`, no tags, a name in both lists, or more than `MAX_TAGS_PER_CALL` names in total. Calls `files().update(fileId=file_id, body={"properties": {tag_property_key(n): "1" for n in add} | {tag_property_key(n): None for n in remove}}, fields="id,properties", supportsAllDrives=True)`. Returns `{"file_id": file_id, "tags": tags_from_properties(result.get("properties"))}`. Retries up to 3 times (sleep 1, 2, 4 seconds) when the error text contains `rateLimitExceeded`, `userRateLimitExceeded` or `429`. A 403 `insufficientFilePermissions` becomes `DriveClientError("You need edit access to this file to change its tags.")`.
- `walk_folder_files(folder_id: str, max_depth: int = 10, max_items: int = 200) -> FolderWalk`, with `FolderWalk` a new dataclass `files: list[DriveFile]`, `folders_seen: int`, `truncated: bool`. Breadth-first: each level queries at most 40 folder ids at once with `'(' + ' or '.join(f"'{i}' in parents" for i in ids) + ') and trashed = false'`, `fields="nextPageToken,files(id,name,mimeType,parents,properties)"`, `pageSize=1000`, all pages, copying the pagination and error handling of `_collect_file_pages`. Folders (`application/vnd.google-apps.folder`) feed the next level; shortcuts (`application/vnd.google-apps.shortcut`) are skipped; everything else is a file. `truncated` is True when more than `max_items` files were found (stop collecting at that point) or folders remain below `max_depth`. Folder ids are validated against `^[A-Za-z0-9_-]{10,100}$` before they enter a query string.

### 3.4 The scope (p2)

- `policy/scopes.py`: `_drive_tag_matches(value, ctx)`: False for an empty value, else `bool(set(getattr(_file_from(ctx.raw_data), "tags", []) or []) & {v.lower() for v in _values_of(value)})`. Registered in `NEW_SCOPE_SELECTORS["drive.tag"]` with `scope_type="drive.tag"`, `kind=ScopeKind.ATTRIBUTE`, `resolves_from=ResolvesFrom.FETCHED`.
- `policy/catalogue.py`: `EXTRA_SCOPES["drive.tag"] = PolicyExtraScope(predicate="drive.tag", connector="drive", verbs=(Verb.READ, Verb.DOWNLOAD, Verb.UPDATE, Verb.RESTRUCTURE, Verb.DELETE), label="Google Drive — files with a tag (anyone who can edit a file can set its tags)", needs_value=True, value_hint="client-a, public-docs")`. MOVE and ADD are deliberately absent: they quantify over a destination the tag does not describe.
- `extra_operations_for` fix: replace `entry.operation.split(".", 1)[0] == extra.connector` with `propose.connector_of_operation(entry.operation) == extra.connector`. This is what makes RESTRUCTURE and DELETE (which exist only as `sheets.*` operations) and the Docs/Sheets UPDATE operations derive for `drive.tag`. Existing extra scopes (apps_script, grist, peek, miro, gmail, slack) must derive exactly what they derive today; a test pins that.
- `policy/value_format.py`: `VALUE_FORMATS["drive.tag"]` accepts `TAG_NAME_RE` only, `what` "a Drive tag name", `how` "a lowercase tag name of up to 40 characters: letters, digits, '.', '_' or '-'", example `client-a`.
- `policy/describe.py` is not changed: `scope_type_label("drive.tag")` already yields "tag", so the existing sentence builders read "Drive - tag client-a: allow read". A test pins that the sentence names the tag value.
- `policy/suggest.py`: nothing to add beyond `_extra_value_of` (which already returns `propose.NO_VALUE` for a group with no call argument, as for `gmail.query`); add a test that a Drive read suggests `drive.tag` with no value.

### 3.5 Operations no rule can cover (p3)

`policy/registry.py` gains `RULE_EXEMPT_OPERATIONS: frozenset[str] = frozenset({"drive.set_file_tags", "drive.tag_folder_tree"})`. It is applied in four places:

1. `propose.operations_for` subtracts it.
2. `catalogue.extra_operations_for` subtracts it.
3. `policy/engine.py`: `find_matching_rule` returns `None`, and `preflight` returns `("requires_review", "", "No auto-accept rule can cover this operation.")`, at the top when `operation_key in RULE_EXEMPT_OPERATIONS`, before any rule is read (`unknown` would wrongly suggest a rule could apply). This is what stops a custom row, a hand-edited `settings.yaml`, a scoped token's rule and an upstream approval.
4. `auto_accept._TEMP_ACCEPT_EXCLUDED_OPERATIONS` gains both keys (defence in depth: the grace window is built only from `DRIVE_SANDBOX_WRITE_TARGETS`, which the tag tools never join), and `policy/suggest.py` returns no suggestions for an exempt operation.

Why: an agent that could tag a file without a popup could tag its way into any tag rule. A scoped token whose rules list these tools is always denied the popup-less path and therefore always needs an approval; nothing else changes for tokens.

### 3.6 `drive_set_file_tags` (p4)

`ToolSpec(name="drive_set_file_tags", params: file_id str, add_tags str (required False, default ""), remove_tags str (required False, default ""), reason str)`. Description: `"Add or remove PrivacyFence tags on one Drive file. Tags are comma-separated lowercase names (letters, digits, '.', '_', '-'; up to 40 characters; at most 10 per call). A tag rule in Settings can auto-accept calls on files carrying the tag. Tags are visible to and changeable by anyone with edit access to the file. Folders cannot be tagged; use drive_tag_folder_tree. Always requires user approval, which no rule can grant. Returns {file_id, tags}."` Behaviour: parse both lists with `parse_tag_list`; at least one tag overall; fetch `get_file_metadata`; refuse a folder with `ValueError("drive_set_file_tags tags files, not folders: use drive_tag_folder_tree to tag everything under a folder.")`; `gated_call(connector=self.name, tool="drive_set_file_tags", tool_name="Set Drive File Tags", summary=f"Change tags on \"{name}\"", sender=<owners or "(unknown)">, raw_data={"file": drive_file}, filtered_data=None, gate="popup", preview={"File": name, "Owner": ..., "Add tags": ", ".join(add) or "(none)", "Remove tags": ", ".join(remove) or "(none)", "Current tags": ", ".join(drive_file.tags) or "(none)"}, details_text="A tag lets any auto-accept rule that names it cover this file from now on. Anyone with edit access to the file can also change its tags.", my_email=..., session_created_ids=..., args={"file_id": file_id, "add_tags": ..., "remove_tags": ...})`, then `await self._fetch(self._drive.set_tags, file_id, add, remove)`. Not recorded as an own write. Operation `drive.set_file_tags`, verb `Verb.UPDATE`, gate `popup`, layout `NARROW`, write effect `"The file's tags change. Its content is not touched."`. The same phase adds `tags` to the returned-field lists in the `drive_list_files` and `drive_get_file_metadata` descriptions (`connectors/drive.py`, after `thumbnail_link`).

### 3.7 `drive_tag_folder_tree` (p5)

`ToolSpec(name="drive_tag_folder_tree", params: folder_id str, tag str, remove bool (required False, default False), reason str)`. Description: `"Add (or with remove=true, remove) one tag on every file under a folder, including all subfolders, in one approved call. Files only, not folders; shortcuts are skipped; at most 200 files and 10 levels deep, otherwise nothing is changed and you are told how many were found. Files added to the folder later do not inherit the tag. Tags are visible to and changeable by anyone with edit access. Always requires user approval, which no rule can grant. Returns {folder_id, tag, action, updated, skipped, failed}."` Behaviour: `normalise_tag(tag)`; fetch the folder with `get_file_metadata` and refuse a non-folder with `ValueError("drive_tag_folder_tree needs a folder id.")`; `walk_folder_files`; when `truncated`, raise `ValueError(f"The folder holds more than {max_items} files or more than {max_depth} levels. Nothing was changed. Tag a smaller subfolder.")`; split the files into `to_change` (add: not yet tagged; remove: tagged) and `skipped`; with an empty `to_change` return the result with `updated=0` and show no popup; otherwise one `gated_call(..., tool="drive_tag_folder_tree", raw_data={"file": folder}, gate="popup", preview={"Folder": name, "Action": "Add tag"/"Remove tag", "Tag": tag, "Files to change": str(len(to_change)), "Already in that state": str(len(skipped))}, details_text=<up to 50 lines "- <name> (<id>)" then "…and N more">, args={"folder_id": folder_id, "tag": tag, "remove": remove})`. After approval it updates exactly the `to_change` snapshot the popup showed (no second walk), sequentially in `asyncio.to_thread`, collecting `failed: [{"file_id", "error"}]` (error text cut to 200 characters) and continuing past a failure. Operation `drive.tag_folder_tree`, `Verb.UPDATE`, `popup`, `NARROW`, write effect `"Every listed file gains or loses the tag. File content is not touched."`.

### 3.8 Checks this plan adds

`scripts/qa_fixture_recorder.py` gets `lifecycle_drive` in `LIFECYCLE_CHECKS` (the section that creates a uniquely tagged QA object, reads it back, updates it and cleans up; pattern `lifecycle_tasks`): create a blank file named `f"{LIFECYCLE_TAG} drive file {suffix}"` in the QA account's root with `DriveClient.create_blank_file`; `set_tags` add two tags and read back with `get_file_metadata`; `set_tags` remove one and read back; record `modified_time` before and after (no assertion); then clean up by trashing the file with `client._get_service().files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()` through `_attempt_delete`, and confirm with `_confirm_deleted` using `files().get(..., fields="trashed")`. It trashes and never permanently deletes, and touches only the file it just created. The comment above `LIFECYCLE_TAG` that lists drive as excluded is updated. The live run happens on the scheduled `--lifecycle` job after merge; the implementing session has no credentials and says so in the pull request. The profile's `dod_conditional` row for `*_client.py` is met by dispatching `qa-record-fixture.yml` for `drive` with `branch=`, which re-records the metadata fixture now carrying `properties`.

## 4. ADRs

- **0320**: Drive rules can match on a file tag stored as a namespaced Drive property, instead of a recursive-folder rule (Drive has no ancestor query); the `drive.tag` scope covers single-file verbs only and never move, copy or upload. Consequences note the `extra_operations_for` fix. Relates to 0172, 0222, 0232.
- **0321**: Tags are plain `properties`, not `appProperties` and not signed values; the collaborator-forgery risk is accepted by the owner. Relates to 0320.
- **0322**: The tag-writing tools are popup-only and exempt from every rule, enforced in the engine (`RULE_EXEMPT_OPERATIONS`), not by per-scope excludes alone. Relates to 0077, 0164, 0236.

## 5. Manual steps

**Before implementation:** none.

**After implementation:**

### ma1-real-drive-tag-check (about 10 minutes)

1. Open your PrivacyFence Settings → Auto-accept (how the page works: https://github.com/privacyfence/privacyfence/blob/feature/drive-property-tag-rules/docs/always-allow-rules-reference.md). You should see "Google Drive — files with a tag" in the scope picker.
2. In a Claude session connected to PrivacyFence, ask the agent to tag one real test file with `drive_set_file_tags` (add tag `pf-test`). You should see one approval popup naming the file and the tag. Approve it.
3. Ask the agent for the file's metadata. You should see `tags: ["pf-test"]`.
4. In Settings → Auto-accept add the rule: scope "Google Drive — files with a tag", value `pf-test`, verb Read. Ask the agent to read the file's content. You should see no popup and the call appear as auto-accepted in the audit log.
5. Ask the agent to tag a whole test folder with `drive_tag_folder_tree`. You should see one popup with a file count; approve it and confirm a file two levels down now reads without a popup.
6. Ask the agent to set a tag again with the rule in place: you should still get a popup (no rule can cover tagging).
7. Report back: pass or fail for each of steps 2 to 6, with the failing step's text if any.

## 6. Risks and open questions

- Forgery by editors (3.2): accepted by the owner; a worker must not "fix" it with signing.
- Drive limits on public properties (size per key plus value, count per file) and whether a properties-only update changes `modifiedTime` or whether `files.copy` copies properties were not settled offline. The design stays well under the commonly documented limits (a key of at most 57 bytes, the value `1`, at most 10 names per call). The p6 lifecycle check reads back what Drive does after merge. Stop condition for p6: if a live fixture the owner pastes shows the update rejected for size or count, stop with `status=blocked` and a `Research:` line.
- The recorder's drive lifecycle trashes a QA file in the QA account's root; the live job runs after merge, so a Drive rejection of the property update surfaces there, not in CI.
- `extra_operations_for` fix (3.4) changes derivation for every connector whose operations are not prefixed with its name. A worker that sees an existing extra scope derive a different set must stop with `status=blocked`.
- Import cycle: `policy/engine.py` importing `policy/registry.py` for `RULE_EXEMPT_OPERATIONS`. If one appears, stop with `status=blocked`; do not move the constant.
- `files.update` with a property set to `None` deleting it is v3 patch semantics; the lifecycle check in p6 proves it live.
- The scoped-token "New token" form may list the two tag tools as selectable; p3's engine refusal makes that harmless, and the retirement phase notes it in the docs.

## Implementation manifest

```yaml
plan_slug: drive-property-tag-rules
feature_branch: feature/drive-property-tag-rules
tracking_issue: 1147
max_parallel: 1
manual_before: []
manual_after:
  - id: ma1-real-drive-tag-check
    title: Tag a real file and folder tree, then add a tag rule and confirm it auto-accepts
    why: CI cannot exercise a real Drive account, the approval popup or the Settings page together.
verify_after_merge:
  - python3 -m pytest tests/unit/test_drive_tags.py tests/unit/test_drive_client.py -q
final_checks:
  - "`tld plan deviations check --file docs/1147/deviations.md --plan docs/1147/plan.md --all` exits 0"
  - ADRs 0320, 0321 and 0322 exist, are Accepted, and are in docs/adr/README.md
  - CHANGELOG.md has an entry under `## [Unreleased]` and no new version heading
phases:
  - id: p1-client-and-tags
    title: Tag helpers, DriveFile.tags and the client's set_tags and walk_folder_files
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/drive_tags.py
      - src/privacyfence/drive_client.py
      - tests/unit/test_drive_tags.py
      - tests/unit/test_drive_client.py
    brief: |
      Implement design sections 3.1 and 3.3 of docs/1147/plan.md exactly.
      1. Create src/privacyfence/drive_tags.py with the constants and the four functions of 3.1 (module docstring per docs/coding-and-testing-guidelines.md 1.2; type hints; no I/O).
      2. In src/privacyfence/drive_client.py add `properties` to `_FILE_FIELDS`, add `tags: list[str] = field(default_factory=list)` to `DriveFile` after `thumbnail_link`, and set it in `_parse_file` via `tags_from_properties`.
      3. Add the `FolderWalk` dataclass and `DriveClient.set_tags` and `DriveClient.walk_folder_files` as specified in 3.3. Read `_collect_file_pages` and `move_file` first and copy their parameters, pagination and error wrapping. Add the retry sleep through a module-level `_sleep = time.sleep` so tests can replace it.
      4. Tests: tests/unit/test_drive_tags.py (every normalise/parse/properties case: uppercase input lowered, bad characters rejected, 11 tags rejected, a foreign property and a tag with value "2" are ignored); extend tests/unit/test_drive_client.py following its existing fake-service style for: `_parse_file` sets tags; `set_tags` body for add and remove (remove sends None), rejects overlap and empty input, retries on a rateLimitExceeded error then succeeds, maps insufficientFilePermissions; `walk_folder_files` batches 40 folders per query, skips shortcuts and trashed, sets `truncated` for more than max_items files and for depth, never queries an invalid folder id.
      5. Do not touch connectors/drive.py or any policy module. If any existing test fails because `DriveFile` or the fields string changed, update that test's expectation only.
    acceptance:
      - python3 -m pytest tests/unit/test_drive_tags.py tests/unit/test_drive_client.py -q passes
      - python3 -m pytest tests/unit/test_drive_tags.py -q --cov=privacyfence.drive_tags --cov-branch --cov-report=term-missing reports 100%
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "properties" src/privacyfence/drive_client.py matches the _FILE_FIELDS line
  - id: p2-tag-scope
    title: The drive.tag selector, catalogue entry, value format and the extra_operations_for fix
    depends_on: [p1-client-and-tags]
    complexity: M
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/value_format.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_value_format.py
      - tests/unit/policy/test_describe.py
      - tests/unit/policy/test_suggest.py
    brief: |
      Implement design section 3.4. Pattern commit: 48fcdb372 (`git show 48fcdb372 -- src/privacyfence/policy`); copy its structure for the selector, the catalogue entry and the value format.
      1. In policy/scopes.py add `_drive_tag_matches` and register `"drive.tag"` in `NEW_SCOPE_SELECTORS` exactly as 3.4 says.
      2. In policy/catalogue.py add the `EXTRA_SCOPES["drive.tag"]` entry with the label and hint strings of 3.4, and change `extra_operations_for` to filter with `propose.connector_of_operation(entry.operation) == extra.connector`.
      3. In policy/value_format.py add `VALUE_FORMATS["drive.tag"]` per 3.4 (import `TAG_NAME_RE` from privacyfence.drive_tags).
      4. Do not change policy/describe.py.
      5. Tests: selector (match, no match, empty value, file with no tags, case-insensitive value); catalogue (the entry appears in `scope_catalogue()`; `extra_operations_for(EXTRA_SCOPES["drive.tag"], Verb.RESTRUCTURE)` equals the sheets operations with that verb; READ includes `docs`/`sheets` read operations; a pinned test that every other EXTRA_SCOPES entry derives the same operation sets as before this change, written from the sets computed on `origin/main` before your edit); value_format (accepts `client-a`, rejects `Client A` and a 41-character name); a describe test that `describe.rule_sentence` of a `drive.tag` rule with value `client-a` contains "tag" and "client-a"; suggest (a Drive read suggests `drive.tag` with no value).
      6. Run `python3 -m pytest tests/unit -q`; fix only test expectations that count catalogue entries.
      Stop with status=blocked if any existing extra scope derives a different set of operations after the `extra_operations_for` change.
    acceptance:
      - python3 -m pytest tests/unit/policy -q passes
      - python3 -m pytest tests/unit/policy/test_catalogue.py -q -k drive_tag passes
      - grep -n "connector_of_operation" src/privacyfence/policy/catalogue.py matches inside extra_operations_for
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p3-exempt-operations
    title: RULE_EXEMPT_OPERATIONS enforced in the engine, propose, catalogue, temp-accept and suggest
    depends_on: [p2-tag-scope]
    complexity: M
    touches:
      - src/privacyfence/policy/registry.py
      - src/privacyfence/policy/engine.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/suggest.py
      - src/privacyfence/auto_accept.py
      - tests/unit/policy/test_rule_exempt_operations.py
    brief: |
      Implement design section 3.5.
      1. In policy/registry.py add `RULE_EXEMPT_OPERATIONS` with a comment saying why (ADR 0321: tagging must never be auto-accepted, or an agent could tag its way into a tag rule).
      2. policy/propose.py `operations_for` and policy/catalogue.py `extra_operations_for`: subtract `registry.RULE_EXEMPT_OPERATIONS`.
      3. policy/engine.py: read `find_matching_rule` and `preflight`; at the top of each, when the operation is in `registry.RULE_EXEMPT_OPERATIONS`, return `None` from `find_matching_rule` and `("requires_review", "", "No auto-accept rule can cover this operation.")` from `preflight`, before reading any rule.
      4. auto_accept.py: add both operation keys to `_TEMP_ACCEPT_EXCLUDED_OPERATIONS`. policy/suggest.py: return an empty list for an exempt operation (read `suggested_rules` first).
      5. New tests/unit/policy/test_rule_exempt_operations.py: (a) for every `propose.PROPOSABLE_SCOPES` entry and verb, and every `catalogue.EXTRA_SCOPES` entry and verb, the derived operations contain neither exempt key; (b) a `PolicyRule` with predicate `gmail.anything` (an always-true selector) and `operations=frozenset({"drive.set_file_tags"})` gets no match from `engine.find_matching_rule` and `("requires_review", "", ...)` from `preflight`; (c) `suggested_rules` for those operations returns nothing; (d) the keys are in `_TEMP_ACCEPT_EXCLUDED_OPERATIONS`. The tests use the constant's values directly; the tools do not exist yet.
      Stop with status=blocked if importing registry from engine creates an import cycle.
    acceptance:
      - python3 -m pytest tests/unit/policy/test_rule_exempt_operations.py -q passes
      - python3 -m pytest tests/unit/policy tests/unit/test_auto_accept.py -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p4-set-file-tags-tool
    title: The drive_set_file_tags tool
    depends_on: [p3-exempt-operations]
    complexity: M
    touches:
      - src/privacyfence/connectors/drive.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/gate.py
      - src/privacyfence/write_effects.py
      - tests/unit/**
    brief: |
      Implement design section 3.6. Pattern commit: f0fc5691b (`git show f0fc5691b -- src/privacyfence tests`): copy where it registered `drive_copy_file`, but note the verbs there are older; use the current names (`Verb.UPDATE`).
      1. connectors/drive.py: add the `ToolSpec` (text of 3.6) next to `drive_copy_file`, the dispatch branch in `call`, and `_set_file_tags` following `_copy_file`.
      2. policy/registry.py `TOOL_TO_VERB["drive_set_file_tags"] = Verb.UPDATE`; auto_accept.py `TOOL_TO_OPERATION["drive_set_file_tags"] = "drive.set_file_tags"` and `TOOL_TO_GATE["drive_set_file_tags"] = "popup"`; gate.py `_TOOL_LAYOUT["drive_set_file_tags"] = NARROW`; write_effects.py `EFFECT_BY_TOOL` text of 3.6. Do NOT add it to `DRIVE_SANDBOX_WRITE_TARGETS`.
      3. Tests in tests/unit/connectors/test_drive_connector.py (mirror the drive_copy_file tests of f0fc5691b): popup is requested with the preview of 3.6 and args; the client gets the parsed lists; a folder is refused before any popup; no tags given is refused; an invalid name is refused; the call is not recorded as an own write. Extend test_rule_exempt_operations.py: with the real registry, no scope derives `drive.set_file_tags` for any verb.
      4. Add `tags` to the returned-field lists in the `drive_list_files` and `drive_get_file_metadata` descriptions (after `thumbnail_link`), and fix their pinned test text if any.
      5. Run `python3 -m pytest tests/unit -q`. Fix pinned lists the new tool trips (tool annotations: not read-only, not destructive; any tool count or tool-name table in tests) by adding the tool, never by loosening a check. `tests/helpers.py::assert_all_tools_leave_an_audit_trail` must pass.
      Stop with status=blocked if a systemic gate-invariants test fails in a way that needs this tool to skip the gate.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_drive_connector.py -q -k set_file_tags passes
      - python3 -m pytest tests/unit -q passes
      - grep -n "drive_set_file_tags" src/privacyfence/auto_accept.py src/privacyfence/policy/registry.py src/privacyfence/gate.py src/privacyfence/write_effects.py matches in each file
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p5-tag-folder-tree-tool
    title: The drive_tag_folder_tree tool
    depends_on: [p4-set-file-tags-tool]
    complexity: M
    touches:
      - src/privacyfence/connectors/drive.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/gate.py
      - src/privacyfence/write_effects.py
      - tests/unit/**
    brief: |
      Implement design section 3.7, copying the registration steps of p4 for `drive_tag_folder_tree` / `drive.tag_folder_tree`.
      1. connectors/drive.py: `ToolSpec` (text of 3.7), dispatch branch, `_tag_folder_tree`. Run the blocking client calls with `asyncio.to_thread` as the other tools do (guidelines 1.7).
      2. Registrations: `TOOL_TO_VERB` (`Verb.UPDATE`), `TOOL_TO_OPERATION`, `TOOL_TO_GATE` ("popup"), `_TOOL_LAYOUT` (NARROW), `EFFECT_BY_TOOL` text of 3.7. Not in `DRIVE_SANDBOX_WRITE_TARGETS`.
      3. Tests: one popup with the counts and the file-name list capped at 50 plus "…and N more"; exactly the snapshot is updated (the fake client's walk is called once); a failure on one file lands in `failed` and the rest proceed; `truncated` raises the ValueError with no popup and no write; an already-tagged folder returns `updated=0` with no popup; `remove=True` removes only tagged files; a non-folder is refused. Extend test_rule_exempt_operations.py to cover `drive.tag_folder_tree` against the real registry.
      4. Run `python3 -m pytest tests/unit -q` and fix pinned lists as in p4.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_drive_connector.py -q -k tag_folder_tree passes
      - python3 -m pytest tests/unit -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p6-recorder-check
    title: The live recorder's drive lifecycle check with a tag round trip
    depends_on: [p5-tag-folder-tree-tool]
    complexity: M
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
    brief: |
      Implement design section 3.8. Read `lifecycle_tasks`, `_attempt_delete`, `_confirm_deleted`, `LifecycleResult` and the comment block above `LIFECYCLE_TAG` in scripts/qa_fixture_recorder.py first; and `git show 48fcdb372 -- tests/unit/test_qa_fixture_recorder.py` for the test style.
      1. Add `_build_drive_client` next to the other `_build_*_client` helpers, copying how `check_drive` builds its client, and `lifecycle_drive(manifest)` following `lifecycle_tasks` step by step with the calls of 3.8. A failure at any step is the finding (`ok=False`), cleanup still runs in `finally`.
      2. Register `"drive": lifecycle_drive` in `LIFECYCLE_CHECKS`. Edit the comment above `LIFECYCLE_TAG`: add drive to the connectors list, remove drive from the "deliberately excludes" list (leave slack there), and say drive's cleanup trashes the file and never deletes permanently.
      3. Extend tests/unit/test_qa_fixture_recorder.py with a fake client: success path (tags after add are the two names, after remove one name, file trashed), a failure after create still trashes the file, and `"drive"` is in `LIFECYCLE_CHECKS`. Fix any existing test that pins the key set of `LIFECYCLE_CHECKS`.
      4. Do not run the live recorder or dispatch workflows.
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes
      - grep -n "lifecycle_drive" scripts/qa_fixture_recorder.py matches the definition and the LIFECYCLE_CHECKS entry
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p7-retire
    title: ADRs 0320 to 0322, reference docs, website, changelog
    depends_on: [p6-recorder-check]
    complexity: M
    touches:
      - docs/adr/0320-drive-rules-match-on-file-tags-not-folder-ancestry.md
      - docs/adr/0321-drive-tags-are-plain-properties-and-collaborators-can-forge-them.md
      - docs/adr/0322-tag-writing-tools-are-popup-only-and-exempt-from-every-rule.md
      - docs/adr/README.md
      - docs/always-allow-rules-reference.md
      - docs/approvals-and-policy.md
      - docs/tools-reference.md
      - docs/connector-qa.md
      - website/**
      - CHANGELOG.md
    brief: |
      Write the ADRs and bring the docs in line, using only what plan sections 3 to 6 decided.
      1. ADRs 0320, 0321 and 0322, one decision each as plan section 4 lists: template and header rules in docs/adr/README.md; Status "Accepted — 2026-11-17. Implemented."; decisions and rejected alternatives from plan sections 3.2 to 3.5; state the forgery risk and the owner's choice plainly in 0321. State each decision in full; do not rely on the plan. Add the three rows to docs/adr/README.md's index in number order.
      2. Mirror every place the commit f0fc5691b added `drive_copy_file` in docs (`grep -rn "drive_copy_file" docs website` shows them): add rows for `drive_set_file_tags` and `drive_tag_folder_tree` (popup, never auto-acceptable) to tools-reference.md; add the "Google Drive — files with a tag" scope, its verbs, what each verb covers (read also covers revisions; update also covers comments, Docs and Sheets writes), the forgery warning and the never-covers-move-copy-upload note to always-allow-rules-reference.md and approvals-and-policy.md; add the drive lifecycle tag round trip to connector-qa.md; update tool counts and tool lists on the website pages the grep shows.
      3. CHANGELOG.md: one line under `## [Unreleased]` for the user-visible change, no version heading.
      4. There is no docs index and no configured website build: skip both; run the unit tests and ruff once at the end.
    acceptance:
      - ls docs/adr/0320-*.md docs/adr/0321-*.md docs/adr/0322-*.md lists all three and grep -c "0320\|0321\|0322" docs/adr/README.md prints 3 or more
      - grep -rn "drive_tag_folder_tree" docs/tools-reference.md docs/always-allow-rules-reference.md matches in both
      - grep -n "files with a tag" docs/always-allow-rules-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
```
