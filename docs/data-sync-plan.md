# Scheduled data sync: personal-data export rules, Peek page rules, Jira export, org-mode exports and privacyfence-sync

## Goal

A user keeps a Grist document as a data lake. Today Claude gathers data from Jira, Salesforce and
Drive through PrivacyFence, writes it to Grist, and a second skill reads Grist and publishes an HTML
dashboard on Peek. Designing the data model and the dashboard stays a Claude job. Refreshing them is
a regular, deterministic job that should cost no model tokens. After this change:

1. **A rule can let personal data through exports.** The Settings "Add a rule" form gets a checkbox,
   "Exports may include personal data", on the Grist document, Salesforce report and Jira project
   scopes. Under such a rule the four export tools (`grist_export_records`, `grist_export_sql`,
   `salesforce_export_report`, `jira_export_issues`) auto-accept even when the PII scan finds
   something. The PII gate exists to keep personal data out of the model's context, and an export's
   result holds no row. Every other read still scans as today. The audit entry records the rule
   and that PII was found.
2. **Peek pages get a per-page rule.** A new "Peek — page" scope in Settings, keyed by slug, covers
   reading the page's comments and stats and uploading new versions of it (`peek_upload_page` with
   `slug`). It never covers a new page, a visibility change, a rename or a delete. The same
   checkbox ("New versions may include personal data") lets a dashboard with names in it publish
   unattended.
3. **`jira_export_issues`** writes every issue a JQL query matches (up to 10,000) to a CSV or JSON
   file, behind one review card, as ADR 0155 describes for the other exports. The existing Jira
   "this project" search rule covers it.
4. **Exports work on an organization-managed install.** Instead of refusing, the export tools hand
   the file out through the one-time download link Drive already uses in org mode (ADR 0017, ADR
   0028). They refuse only when the organization turned staging off.
5. **`privacyfence-sync`**, a separate small package in this repository (`sync/`, published to PyPI
   as its own project), runs a recipe file with no model involved. It is a plain MCP client: it
   calls the export tools, runs the user's own transform script on the files, writes back with
   `grist_update_csv` or `grist_import_csv`, renders a dashboard template from a
   `grist_export_sql` JSON file, and publishes it with `peek_upload_page` by slug. It declares an
   unattended session, so anything no rule covers fails fast. It works against a local daemon
   (bearer token) and an org server (OAuth sign-in).

Grist stays add-only (ADR 0145). A row deleted at the source is handled in the data model, with an
`is_active` column the user's transform script maintains. That is recipe content, not code here.

There is no issue; the request came from the user in a brainstorming session.

## Current state

### Rules and the PII gate

- `PolicyRule` (`src/privacyfence/policy/engine.py:35-54`) has `id`, `predicate`, `value`,
  `operations` and `conditions`. `find_matching_rule` (`engine.py:69-95`) returns the first rule
  whose operations hold the operation key, whose selector matches and whose conditions hold.
- Rules are stored under `auto_accept:` in `config/settings.yaml`, schema version 2
  (`policy/store.py:40-41`). `rule_id_for` (`store.py:93-100`) hashes
  `repr((predicate, _sortable(value), conditions))` into `r-<10 hex>` (ADR 0074). `merge_rules`
  (`store.py:118-142`) unions the operations of rules sharing that key. `rule_to_dict` and
  `rule_from_dict` (`store.py:145-183`) write and read one rule; `rule_from_dict` fails closed.
- The Settings catalogue is `policy/catalogue.py`. `EXTRA_SCOPES` (`catalogue.py:48-74`) holds
  scopes the popup never proposes; `grist.document` is one (ADR 0144). `scope_catalogue()`
  (`catalogue.py:111-146`) lists every group the form offers. `rules_for_catalogue_entry`
  (`catalogue.py:167-196`) compiles a form submission into rules. The Salesforce group id is
  `salesforce.report` and the Jira one `jira.project` (the widening group defaults to the scope
  type, `propose.py:248-271`). `jira.project` compiles to two predicates:
  `approved_project_keys` (args) for READ and the writes, and `approved_project_keys_all_results`
  (fetched results) for SEARCH.
- `describe.rule_sentence` (`policy/describe.py:114-138`) renders a rule as one sentence for
  Settings and `privacyfence_list_policy`.
- The gate (`src/privacyfence/gate.py`): the PII scan runs before any rule check (`gate.py:939-990`).
  `pii_forces_confirmation` is the review-gate list and `upload_pii_categories` the popup-gate list
  (`drive_upload_file` and `peek_upload_page` pass `upload_pii_scan_text`). At `gate.py:1047-1055`
  a matching rule auto-accepts only `if auto_ok and not pii_forces_confirmation and not
  upload_pii_categories`. Otherwise an unattended session is denied (`_deny_unattended`,
  `gate.py:1479-1496`, which raises `GateDeniedError("Request denied: this connection is in an
  unattended session and no auto-accept rule matches this call, so it can't be approved without a
  human present.")`) and an attended one gets a card.
- An auto-accept audit entry records `decision="auto_accepted"`, `auto_accept_rule` and `rule_id`
  (the matched rule's id) and `pii_detected` (`gate.py:1049-1053`).
- Settings: the "Add a rule" form is built in `renderAutoAccept`
  (`src/privacyfence/settings_window_html.py:807-901`) and posted by `submitAddPolicyRule`
  (`settings_window_html.py:1597-1604`) as `add_policy_rule {group, value, verbs}`. Local mode runs
  `SettingsController.add_policy_rule(group, value, verbs)` (`settings_controller.py:1715-1737`)
  through `_call_action`, which coerces each argument by its annotation (`bool(value)` for a
  `bool`, `web/routes_settings.py:346-388`). Org mode runs `_apply_org_action`
  (`web/routes_settings.py:1542-1552`). Both require passkey step-up (`_SENSITIVE_ACTIONS`,
  `routes_settings.py:223-249`).
- `privacyfence_propose_policy_change` builds rules through `rules_for_catalogue_entry` too
  (`gate.py:1359-1476`).

### Peek

- No scope covers a Peek tool: "peek" appears in no file under `policy/` except the verb table
  (`policy/registry.py:218-224`). Operation keys (`auto_accept.py:141-147`): `peek_get_comments`
  → `peek.read_comments` (READ, review), `peek_get_stats` → `peek.read_stats` (READ, review),
  `peek_upload_page` → `peek.upload_page` (CREATE, popup).
- Every Peek `gated_call` passes `args` with `slug` (`connectors/peek.py:380, 428, 537`).
  `peek_upload_page` with a `slug` uploads a new version and refuses `visibility` and `password`
  (`peek.py:453-460`); without one, `slug` is `""` and a new page is created.

### Exports

- `src/privacyfence/export_files.py` holds the pure helpers ADR 0155 describes. `refuse_org`
  (`export_files.py:43-48`) raises `"{tool} writes a file on your computer, which an
  organization-managed install cannot do. Use {alternative} instead."` when `download_mode ==
  "org"`. Its only callers are `connectors/grist.py:836` (`grist_export_records`),
  `connectors/grist.py:914` (`grist_export_sql`) and `connectors/salesforce.py:648-650`
  (`salesforce_export_report`).
- The tool descriptions say so: `connectors/grist.py:498` and `:527` ("Not available on an
  organization-managed install."), `connectors/salesforce.py:374-375`.
- After the gate each export calls `local_files.deliver_file(dest_dir, name, data, mime_type, *,
  download_mode)` (`local_files.py:436-467`). It writes directly, stages for the `.mcpb` shim, or
  returns a one-time link (`_deliver_link`, `local_files.py:521-546`, result `{"path": None, "name",
  "size_bytes", "delivery": "link", "download_url", "expires_at": <epoch float>, "note"}`). The audit
  `delivery` label each export passes is `"local_disk"` or `"client_bridge"`
  (`grist.py:888-889, 971-972`, `salesforce.py:728-729`).
- Org-mode file delivery already exists for Drive: `DriveConnector._deliver_org_download`
  (`connectors/drive.py:1314-1344`) stages the bytes with
  `get_download_staging_store().stage(current_principal(), data, name, mime_type,
  ttl_seconds=cfg.link_ttl_seconds)` and returns `cfg.link_result(name, size_bytes,
  f"{self.download_base_url}{cfg.staged_link_path(token)}")`. `cfg` is
  `org_mode.DownloadDeliveryConfig` (`org_mode.py:200-335`): `allow_disk_staging` (default true),
  `link_ttl_seconds` (300), `agent_links` (true → `/mcp-files/fetch/<token>`, no auth header).
  `link_result` returns `{"delivery": "link", "name", "size_bytes", "download_url", "expires_at":
  <ISO string>, "note": LINK_RESULT_NOTE}`.
- `daemon_main.build_connectors` computes `download_mode`, `download_config` and
  `download_base_url` once (`daemon_main.py:1311-1330`) and sets all three on Gmail, Drive and
  Confluence (`:1347-1349`, `:1368-1370`, `:1547-1549`). Salesforce (`:1489`) and Grist (`:1613`)
  get only `download_mode`. `DriveConnector.__init__` declares `self.download_config:
  DownloadDeliveryConfig | None = None` (`drive.py:156`).
- `file_bridge.max_download_bytes` (200 MB) applies in both modes (`daemon_main.py:741-747`).

### Jira

- `jira_search_issues_with_fields` (`connectors/jira.py:349-375`, handler `:652-681`): `jql`,
  `fields` (JSON array, 1-20, `_parse_field_refs`, `jira.py:259-282`), `max_results` (≤100),
  `reason`. Operation `jira.read_issue`, Verb.SEARCH, review gate, WIDE layout. It passes
  `raw_data=issues` (the `JiraIssue` list) because `approved_project_keys_all_results`
  (`policy/scopes.py:287-293`) reads `getattr(item, "key")` from `ctx.raw_data` and requires every
  key's project to be approved, with at least one key.
- `JiraClient.search_issues_page(jql, page_size, page_token, extra)` (`jira_client.py:368-395`) is
  one `enhanced_jql` request; `search_issues` (`:397-418`) loops at most `MAX_PAGES = 10` pages.
  `JiraClient.resolve_fields` and `extra_field_keys` (`:608-650`) turn field references into
  `JiraField`s and column names. `JiraIssue` (`:144-157`) has `key, summary, status, issue_type,
  priority, assignee, reporter, description, labels, created, updated, url` and `extra_fields`.
- `JiraConnector._auto_audit(tool, tool_name, summary, sender, created_at)` (`jira.py:1019-1037`)
  records an `auto_accepted` entry with `auto_accept_rule="auto"`.
- `scripts/qa_fixture_recorder.py` `check_jira` (`:957-1035`) has rows `list_projects`, `get_issue`,
  `search_issues_page` and `list_fields`; `_check_grist_sql` (`:1156-1172`) is the pattern for a
  row that records nothing.

### MCP clients and packaging

- Any MCP client can call `/mcp`. In local mode it sends `Authorization: Bearer <token>` (the
  token from `privacyfence-app --print-mcp-token`, `docs/connect-claude-code.md:13-68`). In org mode
  it uses OAuth 2.1 with dynamic client registration, PKCE S256, exact redirect-URI matching and
  rotating refresh tokens capped at 30 days (`web/oauth_provider.py`, `web/routes_mcp.py:927-955`).
- The `mcp` SDK (`mcp>=2.2,<3.0.0`, `pyproject.toml:124`) is already a runtime dependency.
  `tests/integration/test_mcp_daemon_contract.py:132-153` is the client pattern:
  `httpx2.AsyncClient(headers=...)`, `streamable_http_client(url, http_client=...)`,
  `ClientSession(read, write)`. The SDK's `mcp.client.auth.OAuthClientProvider(server_url,
  client_metadata, storage, redirect_handler=None, callback_handler=None, client_metadata_url=None,
  validate_resource_url=None)` (an `httpx2` auth, subclass of `RedirectAwareAuth`) with a
  `TokenStorage` protocol (`get_tokens`, `set_tokens`, `get_client_info`, `set_client_info`) does
  the OAuth client side. `redirect_handler` is `async (url: str) -> None`; `callback_handler` is
  `async () -> AuthorizationCodeResult`, the pydantic model `mcp.shared.auth.AuthorizationCodeResult(code,
  state=None, iss=None)` (exported from `mcp.client.auth`).
- `privacyfence_create_upload_slot(filename, reason, size_bytes)` returns `{"upload_id",
  "upload_url", "method": "PUT", "max_bytes": 50000000, "expires_at"}`; a `PUT` with no auth header
  stages the bytes (ADR 0028). `grist_import_csv`, `grist_update_csv` and `peek_upload_page` take
  `upload_id`. `privacyfence_await_approval(approval_ids, timeout_seconds)` waits up to 120 s.
- `privacyfence_begin_unattended_session(reason)` works in both modes when
  `unattended_sessions.enabled` is set in `org_config.json` (`daemon_main.py:2002`), and errors
  otherwise.
- `pyproject.toml`: src layout with `packages.find where = ["src"]` (`:182-186`), so a second
  package must not live under `src/`. `pytest` collects `tests/` only (`testpaths`), coverage is
  `src/privacyfence` only, `ruff check .` covers the whole repo, `mypy src/privacyfence` and
  `bandit -r src` do not reach outside `src/`.
- PyPI: `.github/workflows/publish-pypi.yml` builds the root package into `dist/`, checks that it
  carries the Telegram credentials module (`check-dist`, which fails a stable build without it),
  and publishes with OIDC trusted publishing (`publish-testpypi`, then `publish-pypi`), and the
  `publish-r2` job uploads `dist/*`. A trusted publisher is per PyPI project
  (`docs/releasing.md:210-249`).

### Pattern commits and pinned counts

- `fd144e08` (`salesforce_export_report`) and `7882c602` (`grist_export_sql`, its QA row): a new
  export tool's registry entries, tests, generated docs and website counts.
- Tests that pin tool sets or counts: `tests/unit/test_website_connectors_page.py`
  (`website/connectors/index.html` Jira card `data-tools="12" data-auto="6" data-review="2"
  data-popup="4"`, total "140 tools"; `website/how-it-works/index.html` "Each of the 140 connector
  tools"), `TestEveryToolIsAudited` in each connector test file,
  `tests/unit/policy/test_propose.py` (its CORPUS), `tests/unit/test_qa_fixture_recorder.py`.
- Generated docs: `scripts/generate_tools_reference.py` → `docs/tools-reference.md` (Total row
  `140 / 50 / 28 / 62`), `scripts/generate_always_allow_reference.py` →
  `docs/always-allow-rules-reference.md`. `tests/unit/test_docs_tools_reference.py` and
  `tests/unit/test_generate_always_allow_reference.py` fail when either is stale.
- `tests/unit/test_code_no_history.py` rejects plan references in code. No comment or docstring
  may cite this plan or its sections.
- `tests/unit/test_website_docs_allowlist.py` failed on any `docs/*-plan.md`, because a plan is
  in neither half of the published-docs allowlist. The commit that adds this plan also makes that
  test skip `*-plan.md` files (as `test_docs_references_exist._is_plan_doc` already does for
  ADR paths), so every phase starts from a green suite. `test_docs_references_exist.py` rejects a
  plan line naming a `docs/` file that does not exist yet, so this plan names the new user doc as
  `privacyfence-sync.md` without its folder until phase p9 creates it.

## Design

### 3.1 The personal-data flag on a rule

`PolicyRule` gains one field, last, with a default so every existing constructor call still works:

```python
allow_personal_data: bool = False
```

Docstring addition: "``allow_personal_data`` lets the tools in ``auto_accept.PERSONAL_DATA_RULE_TOOLS``
auto-accept under this rule even when the PII scan finds something (ADR 0157); it changes nothing
for any other tool, and nothing about whether a call matches."

`policy/store.py`:

- `rule_id_for(predicate, value, conditions, allow_personal_data: bool = False) -> str`. The key is
  `repr((predicate, _sortable(value), conditions))` when the flag is false, exactly as today, so
  every stored id stays the same, and `repr((predicate, _sortable(value), conditions,
  "allow_personal_data"))` when it is true.
- `rule_id_for_rule` passes `rule.allow_personal_data`.
- `merge_rules`: the merge key becomes `(rule.predicate, _sortable(rule.value), rule.conditions,
  rule.allow_personal_data)`, and both `PolicyRule(...)` constructions carry the flag. A rule with
  the flag and one without it on the same document are two rows.
- `rule_to_dict` adds `"allow_personal_data": True` only when the flag is true. A rule without it
  serialises exactly as today.
- `rule_from_dict`: a missing key is `False`; any value other than `True` or `False` drops the rule
  (fail closed, like every other malformed field). `SCHEMA_VERSION` stays 2. An older daemon reading
  a flagged rule ignores the unknown key and treats it as an ordinary rule, which keeps the PII gate
  in force: the safe direction. If that older daemon then rewrites the rules file, the flag is lost
  and the stored id no longer matches the rule's content; a downgrade is not supported, so this is
  stated in ADR 0157's consequences and not handled in code.

`src/privacyfence/auto_accept.py`, next to `TOOL_TO_GATE`:

```python
# The tools a rule's allow_personal_data flag applies to (ADR 0157): each one's result carries no
# row (the data goes to a file or to a page the user's own rule names), so the PII scan, which
# guards the model's context, is not what protects their data.
PERSONAL_DATA_RULE_TOOLS: frozenset[str] = frozenset({
    "grist_export_records",
    "grist_export_sql",
    "salesforce_export_report",
    "peek_upload_page",
})
```

`jira_export_issues` joins the set in its own phase (§3.5), so the set never names a tool that does
not exist yet. Add it to `__all__` (`auto_accept.py:628`).

`policy/catalogue.py`:

```python
# The groups whose "Add a rule" form offers the personal-data checkbox, and its label (ADR 0157).
PERSONAL_DATA_GROUPS: dict[str, str] = {
    "grist.document": "Exports may include personal data",
    "salesforce.report": "Exports may include personal data",
    "jira.project": "Exports may include personal data",
}
```

(`peek.page` joins it in §3.3.)

- `scope_catalogue()`: each entry whose `id` is in `PERSONAL_DATA_GROUPS` gains
  `"personal_data_label": PERSONAL_DATA_GROUPS[id]`. Other entries are unchanged.
- `rules_for_catalogue_entry(group, value, verbs, *, personal_data: bool = False)`. When
  `personal_data` is true and `group not in PERSONAL_DATA_GROUPS`, return `[]`. Otherwise compile as
  today; when `personal_data` is true, replace every compiled rule with
  `dataclasses.replace(rule, allow_personal_data=True)` and return
  `store.merge_rules(<those>)`, so each id is recomputed with the flag.
- `gate.propose_policy_change` keeps calling it without `personal_data`, so a model can never
  propose the flag. It is set only by a person in Settings, behind passkey step-up.

`policy/describe.py` `rule_sentence`: after the conditions suffix, when `rule.allow_personal_data`
append `" - personal data allowed in exports and page uploads"`.

`src/privacyfence/gate.py`, a new helper next to `_evaluate_auto_accept`:

```python
def _personal_data_rule(tool: str, operation_key: str, ctx: ReviewContext) -> PolicyRule | None:
    """The stored rule with ``allow_personal_data`` that covers this call, for a tool in
    ``PERSONAL_DATA_RULE_TOOLS`` only (ADR 0157); ``None`` for every other tool or when no such
    rule matches."""
    if tool not in PERSONAL_DATA_RULE_TOOLS:
        return None
    rules = [rule for rule in get_policy_v2_store_rules() if rule.allow_personal_data]
    if not rules:
        return None
    try:
        return policy_engine.find_matching_rule(rules, operation_key, ctx)
    except Exception:
        logger.warning("Policy evaluation raised for op=%r", operation_key, exc_info=True)
        return None
```

In `gated_call`, immediately after `try:` and before the first `_evaluate_auto_accept` call
(`gate.py:1046-1047`):

```python
        if pii_forces_confirmation or upload_pii_categories:
            personal_data_rule = _personal_data_rule(tool, operation_key, ctx)
            if personal_data_rule is not None:
                audit(
                    decision="auto_accepted", auto_accept_rule=personal_data_rule.id,
                    rule_id=personal_data_rule.id, pii_detected=True,
                )
                logger.info(
                    "Auto-accepted with personal data: %s/%s rule=%r",
                    connector, tool, personal_data_rule.id,
                )
                return filtered_data
```

Nothing else in the gate changes. A call with no PII finding takes the existing path, where a
flagged rule matches like any rule. `pii_categories` keeps being written to the audit entry by the
`audit()` closure, so the entry says which categories were found.

Rejected:

- **Exempting the export tools from the PII gate outright.** The user accepted a per-rule opt-in;
  without it, a rule made for interactive reads would silently start moving personal data.
- **A `conditions` entry for the flag.** Conditions narrow whether a rule matches; this flag does not
  change matching, only what a PII finding does.
- **Letting `privacyfence_propose_policy_change` set the flag.** The model whose context the gate
  protects would be asking to switch the protection off; the flag stays a Settings-only decision,
  as ADR 0144 keeps Grist rules.
- **A new audit field.** `decision="auto_accepted"` with `pii_detected=True` and a `rule_id` already
  says it. The only other path that produces that combination is `pii_already_reviewed`, which only
  Drive's read tools pass (`connectors/drive.py:1051, 1108, 1289`); for the tools in
  `PERSONAL_DATA_RULE_TOOLS` it is unambiguous.

### 3.2 The checkbox in Settings

- `settings_window_html.py`:
  - The `ui` state object (`settings_window_html.py:300`, the line holding `aaGroup: null`) gains
    `aaPersonalData: false`.
  - In `renderAutoAccept`, after the verb chips block and before the "Add rule" button, when
    `group && group.personal_data_label`:

    ```js
    var pdOn = !!ui.aaPersonalData;
    html += '<div class="pf-chips pf-caps-row"><div class="pf-chip' + (pdOn ? ' on' : '') +
      '" role="checkbox" aria-checked="' + (pdOn ? 'true' : 'false') + '" tabindex="0" aria-label="' +
      esc(group.personal_data_label) + '" data-aa-personal-data="1">' + esc(group.personal_data_label) +
      '</div></div>';
    ```

  - In the click handler, next to the `[data-aa-verb-toggle]` branch:

    ```js
    var aaPdEl = e.target.closest('[data-aa-personal-data]');
    if (aaPdEl) { ui.aaPersonalData = !ui.aaPersonalData; render(pyState); return; }
    ```

    The keyboard handler that activates `[data-aa-verb-toggle]` on Enter/Space handles
    `[data-aa-personal-data]` the same way.
  - `submitAddPolicyRule` posts `personal_data: !!ui.aaPersonalData` with the other fields and then
    resets `ui.aaPersonalData = false`.
  - Changing the group (`:1618`, where `ui.aaGroup = el.value`) also sets
    `ui.aaPersonalData = false`.
- `SettingsController.add_policy_rule(self, group: str, value: str, verbs: list,
  personal_data: bool = False)` passes `personal_data=personal_data` to
  `_rules_for_catalogue_entry`. Docstring gains one sentence: "``personal_data`` sets the rule's
  ``allow_personal_data`` flag (ADR 0157) and is accepted only for a group in
  ``catalogue.PERSONAL_DATA_GROUPS``; for any other group the submission adds nothing."
- `web/routes_settings.py` `_apply_org_action`, `add_policy_rule` branch: read
  `personal_data = body.get("personal_data") is True` and pass it to
  `policy_catalogue.rules_for_catalogue_entry`. When it is true the audit summary string becomes
  `f"Added auto-accept rule ({group!r}, allow {verb_names!r}, personal data allowed) (principal={principal.id})"`.
- The step-up fingerprint already covers the whole JSON body (`_action_fingerprint`), so the
  checkbox is bound to the passkey assertion with no change.
- Local mode's `_coerce` turns any truthy value into `True` (`bool(value)`,
  `web/routes_settings.py:360`), so the page must post a real boolean (`!!ui.aaPersonalData`);
  org mode accepts only `True` itself. Both are fine because the only sender is this page.

### 3.3 The Peek page scope

`policy/scopes.py`, in the section with `_grist_document_matches`:

```python
def _peek_page_matches(value: Any, ctx: ReviewContext) -> bool:
    """A Peek page rule names slugs. A call without a slug (peek_upload_page creating a new page)
    never matches, so the rule covers new versions of the named pages only (ADR 0158)."""
    if not value:
        return False
    slug = ctx.args.get("slug", "") or ""
    return bool(slug) and slug in set(_values_of(value))
```

and in `NEW_SCOPE_SELECTORS`, after the `grist.document` entry:

```python
    "peek.page": ScopeSelector(
        predicate="peek.page", scope_type="peek.page", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_peek_page_matches,
    ),
```

`policy/catalogue.py` `EXTRA_SCOPES`, after `grist.document`:

```python
    "peek.page": PolicyExtraScope(
        predicate="peek.page", connector="peek",
        verbs=(Verb.READ, Verb.CREATE),
        label="Peek — page", needs_value=True, value_hint="team-dashboard…page-slug",
        verb_labels={"read": "read (comments, stats)", "create": "update (new versions)"},
    ),
```

`extra_operations_for("peek", Verb.READ)` yields `peek.read_comments` and `peek.read_stats`;
`Verb.CREATE` yields `peek.upload_page` only. `PERSONAL_DATA_GROUPS` gains
`"peek.page": "New versions may include personal data"`.

The popup never offers a Peek "Always allow" button, as with Grist (`propose.PROPOSABLE_SCOPES` is
not touched). `privacyfence_propose_policy_change` can propose a `peek.page` rule, as it can a
`grist.document` one, without the flag.

Rejected:

- **A slug rule that also covers new pages.** A new page has no slug until it is created, so there
  is nothing to name; covering every new page would be an open-ended publish rule.
- **A Peek "Always allow" button on the card.** Publishing is egress to people; the rule is set
  deliberately in Settings, as for Grist documents.

### 3.4 Exports on an organization-managed install

`src/privacyfence/export_files.py`:

- Delete `refuse_org`. Add:

  ```python
  ORG_SAVED_TO = "A one-time download link (destination_dir is ignored on an organization-managed install)"
  ORG_DELIVERY_SENTENCE = (
      "On an organization-managed install nothing is saved to destination_dir: the result carries "
      "a one-time download_url instead."
  )

  def refuse_org_without_staging(tool: str, download_mode: str, allow_disk_staging: bool, alternative: str) -> None:
      if download_mode == "org" and not allow_disk_staging:
          raise ValueError(
              f"{tool} delivers its file through a one-time download link on an organization-managed "
              "install, and this organization has turned off staging files on the server "
              f"(download_delivery.allow_disk_staging). Use {alternative} instead."
          )

  def saved_to(destination_dir: str, file_name: str, download_mode: str) -> str:
      if download_mode == "org":
          return ORG_SAVED_TO
      return f"{destination_dir.rstrip('/')}/{file_name}"
  ```

`src/privacyfence/local_files.py`, next to `deliver_file`, added to `__all__`:

```python
def deliver_export(
    dest_dir: str, name: str, data: bytes, mime_type: str, *,
    download_mode: str, download_config: DownloadDeliveryConfig | None, download_base_url: str,
) -> dict[str, Any]:
    """Where an export's file goes after its approval (ADR 0160): ``deliver_file`` in local mode;
    on an organization-managed install, a one-time staged link like a large Drive download's."""
    if download_mode != "org":
        return deliver_file(dest_dir, name, data, mime_type, download_mode=download_mode)
    check_delivery_size(len(data))
    cfg = download_config or DownloadDeliveryConfig()
    token = get_download_staging_store().stage(
        current_principal(), data, name, mime_type, ttl_seconds=cfg.link_ttl_seconds,
    )
    return cfg.link_result(name, len(data), f"{download_base_url}{cfg.staged_link_path(token)}")
```

`DownloadDeliveryConfig` is imported from `privacyfence.org_mode`; `current_principal` and
`get_download_staging_store` are what `local_files` already uses for `_deliver_bridge`.

Each export handler (`grist_export_records`, `grist_export_sql`, `salesforce_export_report`):

1. Replace its `export_files.refuse_org(...)` call with
   `export_files.refuse_org_without_staging(<tool>, self.download_mode, (self.download_config or
   DownloadDeliveryConfig()).allow_disk_staging, <same alternative as today>)`, at the same place
   (before any network call).
2. `saved_to = export_files.saved_to(destination_dir, file_name, self.download_mode)` replaces its
   inline `saved_to` expression; the card's "Saved to" row and `card_details` use it.
3. The `delivery=` passed to `gated_call` becomes `"staged_link"` when `self.download_mode ==
   "org"`, else the current expression.
4. After the gate, `delivered = local_files.deliver_export(destination_dir, file_name, data,
   export_files.MIME_TYPES[fmt], download_mode=self.download_mode,
   download_config=self.download_config, download_base_url=self.download_base_url)` replaces the
   `deliver_file` call, called directly as `deliver_file` is today: its shim-bridge path records the
   pending delivery in the call's context, which must stay on the event loop's task.
5. In the tool's description, replace the "Not available on an organization-managed install."
   sentence (`grist.py:498`, `grist.py:527`, `salesforce.py:374-375`) with
   `export_files.ORG_DELIVERY_SENTENCE`. Keep "Requires user approval." where it follows.

`GristConnector.__init__` and `SalesforceConnector.__init__`, next to `self.download_mode`:
`self.download_config: DownloadDeliveryConfig | None = None` and `self.download_base_url: str =
""`. `daemon_main.build_connectors` sets both on the Salesforce connector (after `:1489`) and the
Grist connector (after `:1613`), as it does for Drive.

The approval binding, the card, the PII scan and the result shape `{rows, columns, sha256,
**delivered}` are unchanged. In org mode `delivered` is `link_result`'s dict, so the result carries
`download_url`, an ISO `expires_at` and `LINK_RESULT_NOTE`, and `path` is absent.

Rejected:

- **Inline delivery in org mode.** The export's point is that no row enters the tool result
  (ADR 0155); an inline file would put every row there.
- **Keeping the refusal.** The user runs both modes; the link mechanism, its encryption at rest and
  single use already carry org-mode Drive downloads (ADR 0017).

### 3.5 `jira_export_issues`

`src/privacyfence/jira_client.py`:

```python
EXPORT_MAX_ISSUES = 10_000

def export_issues(self, jql: str, fields: list[str] | None = None, max_issues: int = EXPORT_MAX_ISSUES) -> list[JiraIssue]:
    """Every issue ``jql`` matches, in pages of 100, for ``jira_export_issues``. Raises once more
    than ``max_issues`` match, before reading further."""
```

It raises `JiraClientError("export_issues requires a non-empty JQL query")` for an empty `jql`,
resolves `extra = self.resolve_fields(fields) if fields else None`, then loops
`self.search_issues_page(jql, 100, page_token, extra)` until the token is `None`. As soon as the
collected count exceeds `max_issues` it raises `JiraClientError(f"More than {max_issues:,} issues
match this JQL. Narrow it, for example with updated >= -7d, and export in parts.")`. It logs
`"export_issues jql=%r returned %d issue(s)"`.

`src/privacyfence/connectors/jira.py`:

- A module-level frozen dataclass, with this comment above it: `# The project-scope matcher reads
  .key from raw_data; an export's raw_data carries the keys and no field value (ADR 0155).`

  ```python
  @dataclass(frozen=True)
  class _ExportedIssue:
      key: str
  ```

- `_EXPORT_COLUMNS = ["key", "summary", "status", "issue_type", "priority", "assignee", "reporter",
  "labels", "created", "updated", "url"]`.
- `ToolSpec(name="jira_export_issues", read_only=True)`, after `jira_search_issues_with_fields`.
  Params in order: `jql` (str, required, `"JQL query selecting the issues to export."`), `fields`
  (str, default `""`, `"Optional JSON array of up to 20 extra fields by name, id or cf[N], such as
  [\"Story Points\", \"duedate\"]; each becomes a column after the standard ones."`),
  `destination_dir` (the same description text the Salesforce export uses, `_DESTINATION_DIR` at
  `salesforce.py:266`, copied into a module constant `_DESTINATION_DIR` in `jira.py`), `file_name`
  (str, default `""`, `"File name ending in .csv or .json, which picks the format. Empty means
  jira-issues.csv."`), `reason`. Description:

  `"Write every issue a JQL query matches, up to 10,000, to a CSV or JSON file on the user's
  computer, in one approval. Columns: key, summary, status, issue_type, priority, assignee,
  reporter, labels, created, updated, url, then the requested extra fields. Returns {rows, columns,
  sha256, path, name, size_bytes, delivery}, never the issues. A query matching no issue writes no
  file and returns rows 0. " + export_files.ORG_DELIVERY_SENTENCE + " Requires user approval."`

- Handler `_export_issues(self, jql, destination_dir, fields="", file_name="")`, following
  `SalesforceConnector._export_report` (`salesforce.py:637-741`) step for step:
  1. `refs = _parse_field_refs(fields, "jira_export_issues", required=False)`,
     `destination_dir = export_files.check_destination(destination_dir)`,
     `file_name, fmt = export_files.export_file_name(file_name, "jira-issues")`,
     `export_files.refuse_org_without_staging("jira_export_issues", self.download_mode,
     (self.download_config or DownloadDeliveryConfig()).allow_disk_staging,
     "jira_search_issues_with_fields")`.
  2. `wanted = await self._fetch(self._jira.resolve_fields, refs) if refs else []`,
     `names = JiraClient.extra_field_keys(wanted)`, `issues = await self._fetch(self._jira.export_issues,
     jql, refs or None)`.
  3. When `issues` is empty: `self._auto_audit("jira_export_issues", "Export Jira Issues",
     f"Export: {jql[:80]}", "0 issue(s)", t0)` and return `{"rows": 0, "columns": [*_EXPORT_COLUMNS,
     *names], "sha256": None, "delivery": "none", "note": "No issue matches this JQL, so no file
     was written."}`. (`t0 = time.time()` at the top of the handler.)
  4. `columns = [*_EXPORT_COLUMNS, *names]`; each row is `[i.key, i.summary, i.status,
     i.issue_type, i.priority, i.assignee, i.reporter, i.labels, i.created, i.updated, i.url,
     *((i.extra_fields or {}).get(n) for n in names)]` (raw values; `export_files.csv_cell` renders
     lists and dicts as JSON in a CSV).
  5. `data = await asyncio.to_thread(export_files.render, columns, rows, fmt)`, `sha256 =
     hashlib.sha256(data).hexdigest()`, `local_files.check_delivery_size(len(data))`.
  6. `gated_call(connector=self.name, tool="jira_export_issues", tool_name="Export Jira Issues",
     summary=f"Export: {jql[:80]}", sender="Jira", raw_data=[_ExportedIssue(i.key) for i in issues],
     filtered_data=None, gate="review", preview={"JQL": jql, "Fields": ", ".join(names) or
     "standard fields only"}, preview_tables=[export_files.sample_table(columns, rows)],
     pii_scan_text=export_files.scan_text(data), new_info=<the same four entries the Salesforce
     export passes, with saved_to>, details_text=export_files.card_details(preview, len(rows),
     columns, saved_to, sha256), delivery=<as §3.4 step 3>, my_email=self.my_email,
     args={"jql": jql, "fields": fields, "destination_dir": destination_dir, "file_name": file_name,
     "sha256": sha256})`, with `saved_to = export_files.saved_to(destination_dir, file_name,
     self.download_mode)`.
  7. `delivered = local_files.deliver_export(...)` as §3.4 step 4, then
     `return export_files.export_summary(columns, rows, sha256, delivered)`.
- `JiraConnector.__init__` (`jira.py:299-301`, which has none of them today) declares
  `self.download_mode: str = "local"`, `self.download_config: DownloadDeliveryConfig | None = None`
  and `self.download_base_url: str = ""`; `daemon_main.build_connectors` sets all three on the Jira
  connector where it is built.
- Registries: `auto_accept.TOOL_TO_OPERATION["jira_export_issues"] = "jira.read_issue"`,
  `TOOL_TO_GATE["jira_export_issues"] = "review"`, `policy/registry.py`
  `TOOL_TO_VERB["jira_export_issues"] = Verb.SEARCH`, `gate._TOOL_LAYOUT["jira_export_issues"] =
  WIDE`, and `"jira_export_issues"` added to `auto_accept.PERSONAL_DATA_RULE_TOOLS`. Verb.SEARCH on
  `jira.read_issue` is what makes the existing "this project" search rule
  (`approved_project_keys_all_results`) and its card button cover the export.
- QA: in `scripts/qa_fixture_recorder.py` add `_check_jira_export(client, page_jql) ->
  CheckResult` modelled on `_check_grist_sql`: it calls `client.export_issues(page_jql,
  ["duedate"], max_issues=50)` and `client.search_issues_page(page_jql, 50, None, None)`, and
  passes when both return at least one issue and the same set of keys. The row is
  `CheckResult("jira", "export_issues", <project key>, ok, note)` with no raw data; a
  `JiraClientError` returns a failed row with the error text. `check_jira` appends it **before**
  the `search_issues_page` row: `tests/unit/test_qa_fixture_recorder.py:882-900` asserts on
  `enhanced_jql.call_args` (the last call) for that row's `limit == 1`, so the export row must not
  run after it. One page of 50 is enough for a live check; the paging loop is unit-tested.

Rejected:

- **50,000 issues, like Grist.** Jira returns at most 100 issues per request when fields are
  requested, so 50,000 would be 500 sequential requests inside one tool call; 10,000 is 100.
- **A card for an empty result.** The "this project" rule matches on the results' keys and cannot
  match none, so an incremental pull that finds nothing would need a human every time. Learning that
  a JQL matches nothing is what the auto-approved `jira_search_issues` already returns.
- **A new operation key.** The existing per-project search rules would not cover it.

### 3.6 The `privacyfence-sync` package

Layout, outside `src/` so the root wheel never picks it up:

```
sync/pyproject.toml
sync/README.md                            already written: the user-facing spec, see below
sync/src/privacyfence_sync/__init__.py
sync/src/privacyfence_sync/recipe.py      pure: load and validate a recipe
sync/src/privacyfence_sync/template.py    pure: put JSON data into an HTML template
sync/src/privacyfence_sync/transform.py   run one transform script
sync/src/privacyfence_sync/client.py      MCP session, tool calls, file transfer
sync/src/privacyfence_sync/auth.py        OAuth token storage and sign-in
sync/src/privacyfence_sync/runner.py      execute a recipe's steps
sync/src/privacyfence_sync/cli.py         argparse entry point
sync/tests/...
```

`sync/README.md` was written before implementation, with the user, as the user-facing description
of everything below, and is committed on the plan branch. It is the spec for behaviour a user sees
(messages, exit codes, step semantics). Where this section and the README disagree, stop with
`status=blocked` and quote both; never silently diverge from either.

The rule for the whole package: **it imports nothing from `privacyfence`**, and it reaches the
daemon only through `/mcp` tool calls, the capability URLs those calls return, and the OAuth
endpoints. `sync/tests/test_no_daemon_imports.py` enforces it by walking every `.py` file under
`sync/src` with `ast` and failing on an `import privacyfence` or `from privacyfence` (bare package or
submodule).

#### `sync/pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=61", "wheel", "setuptools_scm>=8"]
build-backend = "setuptools.build_meta"

[project]
name = "privacyfence-sync"
dynamic = ["version"]
description = "Run a PrivacyFence data-sync recipe as a plain MCP client, with no AI model involved."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "Apache-2.0" }
dependencies = ["mcp>=2.2,<3.0.0", "httpx2>=2.5", "pyyaml>=6"]

[project.optional-dependencies]
test = ["pytest", "pytest-asyncio", "pytest-cov"]

[project.scripts]
privacyfence-sync = "privacyfence_sync.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools_scm]
root = ".."
fallback_version = "0.0.0.dev0"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

It carries the same version as PrivacyFence (the root tag), so a release publishes both.

#### Recipe format (`recipe.py`)

YAML, loaded with `yaml.safe_load`:

```yaml
version: 1
name: sales-dashboard            # required, matches ^[a-z0-9][a-z0-9-]{0,62}$
server: http://127.0.0.1:8765/mcp  # optional
work_dir: work                   # optional, relative to the recipe file; default "work"
steps:                           # 1 to 50 entries
  - id: deals                    # required, ^[a-z0-9_-]{1,40}$, unique
    tool: salesforce_export_report
    args: {report_id: 00O000000000001, file_name: deals.csv}
  - id: current
    tool: grist_export_records
    args: {doc_id: 8CAN8gKdxY7z, table_id: Deals, file_name: current.csv}
  - id: merged
    run: transforms/deals.py     # relative to the recipe file
    inputs: [deals, current]     # earlier steps that produced a file
    output: merged.csv           # plain file name
  - id: load
    tool: grist_update_csv
    file: merged                 # an earlier step's file, uploaded
    args: {doc_id: 8CAN8gKdxY7z, table_id: Deals, key_column: Id, create_missing: true}
  - id: data
    tool: grist_export_sql
    args: {doc_id: 8CAN8gKdxY7z, sql: "SELECT ...", file_name: data.json}
  - id: page
    template: templates/dashboard.html
    data: data                   # an earlier step whose file ends in .json
    output: dashboard.html
  - id: publish
    tool: peek_upload_page
    file: page
    args: {slug: sales-dashboard}
```

Dataclasses (frozen): `Recipe(name, server, work_dir: Path, steps: tuple[Step, ...], base_dir: Path)`;
`ToolStep(id, tool, args: dict, file: str | None, reason: str | None)`; `RunStep(id, script: Path,
inputs: tuple[str, ...], output: str, timeout_seconds: int)` (optional key `timeout_seconds`,
1-3600, default 600); `TemplateStep(id, template: Path, data: str, output: str)`.

`load_recipe(path: Path) -> Recipe` raises `RecipeError(f"{path}: {message}")` (a `ValueError`
subclass) for: a top level that is not a mapping; `version` not `1`; a bad `name`; `steps` not a
list of 1-50 mappings; a step with zero or more than one of `tool`, `run`, `template`; a bad or
duplicate `id`; unknown keys in a step (`step <id>: unknown key <k>`); `args` not a mapping; `file`,
`inputs` or `data` naming a step that is not earlier or produces no file; `output` or an export's
`args.file_name` that is not a plain name (no `/`, `\`, NUL, leading `.`); an export tool without
`args.file_name`, or one whose `args` already has `destination_dir`; an upload tool without `file`,
or one whose `args` has `local_path`, `upload_id` or `html`; `file` on a tool not in
`UPLOAD_TOOLS`; a `run` or `template` path that resolves outside the recipe's directory; a
`template` step whose `data` step's file does not end in `.json`.

Constants in `recipe.py`:

```python
EXPORT_TOOLS = frozenset({"grist_export_records", "grist_export_sql", "salesforce_export_report", "jira_export_issues"})
UPLOAD_TOOLS = frozenset({"grist_import_csv", "grist_update_csv", "peek_upload_page"})
```

A step produces a file when it is an export (`args.file_name`), a `run` step or a `template` step.
Any other tool step is called with its `args` as given and produces no file.

#### Template (`template.py`)

```python
PLACEHOLDER = "/*PRIVACYFENCE_SYNC_DATA*/null"

def render_template(template_text: str, data: Any) -> str:
```

The template must contain `PLACEHOLDER` exactly once (`TemplateError("the template must contain
/*PRIVACYFENCE_SYNC_DATA*/null exactly once")` otherwise). It is replaced by
`json.dumps(data, ensure_ascii=False)` with `<`, `>` and `&` replaced by `<`, `>`,
`&` and U+2028/U+2029 by ` `/` `, so the data cannot close a `<script>` element. A
template uses it as `const DATA = /*PRIVACYFENCE_SYNC_DATA*/null;`.

#### Transform (`transform.py`)

```python
def run_transform(step: RunStep, input_paths: list[Path], output_path: Path, work_dir: Path) -> None:
```

It runs `subprocess.run([sys.executable, "-I", str(step.script), *map(str, input_paths),
str(output_path)], cwd=work_dir, env=transform_env(), capture_output=True, text=True,
timeout=step.timeout_seconds, check=False)`. `transform_env()` is `os.environ` without any key
starting with `PRIVACYFENCE_`, so the script never sees the token. A non-zero exit raises
`TransformError(f"step {step.id}: {script name} exited with {code}: {last 2000 chars of stderr}")`,
a timeout `TransformError(f"step {step.id}: {script name} ran longer than {n} seconds")`, and a
missing `output_path` afterwards `TransformError(f"step {step.id}: {script name} did not write
{output name}")`. The script's stdout and stderr are echoed to the runner's stderr.

The contract a transform script follows, stated in `sync/README.md`: it gets the input files'
paths, then the output path, as arguments; it reads CSV or JSON and writes the output; it has no
PrivacyFence credential and should make no network call.

#### Client (`client.py`)

```python
class SyncClient:
    def __init__(self, server_url: str, auth: httpx2.Auth | None, headers: dict[str, str]) -> None: ...
    async def __aenter__(self) -> "SyncClient": ...      # opens httpx2.AsyncClient, streamable_http_client, ClientSession, initialize()
    async def __aexit__(self, *exc: object) -> None: ...
    async def call(self, tool: str, args: dict[str, Any]) -> Any: ...
    async def download(self, url: str, dest: Path, expected_sha256: str | None) -> None: ...
    async def upload(self, path: Path, reason: str) -> str: ...   # returns upload_id
```

- `call` runs `session.call_tool(tool, args)`. It joins the result's text content; when the result
  `isError`, it raises `ToolDenied(text)` if the text starts with `"Request denied"`, else
  `ToolFailed(text)`. Otherwise it returns `json.loads(text)`, or the text itself when that fails.
- `download` GETs the URL with a separate `httpx2.AsyncClient()` that carries no
  `Authorization` header (the URL is the credential, ADR 0028), timeout 120 s, writes the bytes to
  `dest`, and raises `ToolFailed("downloaded file does not match the sha256 the tool reported")`
  when `expected_sha256` is given and differs.
- `upload` calls `privacyfence_create_upload_slot` with `{"filename": path.name, "size_bytes":
  path.stat().st_size, "reason": reason}`, `PUT`s the bytes to `upload_url` with no auth header, and
  returns `upload_id`.
- `connect_token(server_url, token)` builds a `SyncClient` with `{"Authorization": f"Bearer
  {token}"}`; `connect_oauth(server_url, storage)` builds one with the `OAuthClientProvider` from
  `auth.py` as `auth`.

#### Runner (`runner.py`)

```python
async def run_recipe(recipe: Recipe, client: SyncClient, *, attended: bool, out: TextIO) -> None:
```

1. `work_dir = recipe.work_dir` is created (`mkdir(parents=True, exist_ok=True)`).
2. Unless `attended`, call `privacyfence_begin_unattended_session` with `reason=f"privacyfence-sync
   {recipe.name}: scheduled run"`. A `ToolFailed` here is re-raised as `SyncError("This
   PrivacyFence does not allow unattended sessions (unattended_sessions.enabled in
   org_config.json). Ask your administrator, or run with --attended.")`.
3. For each step in order, print `f"[{step.id}] ..."` progress lines to `out`, and keep
   `files: dict[str, Path]` of produced files:
   - **Export tool**: `args = {**step.args, "destination_dir": str(work_dir), "reason": reason}`.
     The result's `delivery` decides: `"local_disk"` → the file is at `result["path"]`; copy it to
     `work_dir / file_name` if it is elsewhere, and check its sha256 against `result["sha256"]`;
     `"link"` → when the URL's path does not contain `/mcp-files/fetch/`, raise `SyncError(f"step
     {id}: this server hands out browser-only download links (download_delivery.agent_links is
     off), which privacyfence-sync cannot fetch. Ask your administrator to turn agent_links on.")`;
     otherwise `client.download(result["download_url"], work_dir / file_name, result["sha256"])`;
     `"none"` (an empty Jira export) → write a CSV with the header row `result["columns"]`, or `[]`
     for a `.json` name. Any other `delivery` raises `SyncError(f"step {id}: unexpected delivery
     {value!r}")`.
   - **Upload tool**: `upload_id = await client.upload(files[step.file], reason)`, then
     `client.call(step.tool, {**step.args, "upload_id": upload_id, "reason": reason})`.
   - **Other tool**: `client.call(step.tool, {**step.args, "reason": reason})`.
   - **Run step**: `run_transform(step, [files[i] for i in step.inputs], work_dir / step.output,
     work_dir)` in `asyncio.to_thread`.
   - **Template step**: read the template (UTF-8), `json.loads` the data file, write
     `render_template(...)` to `work_dir / step.output`.
   `reason` is `step.reason` or `f"privacyfence-sync {recipe.name}: step {step.id}"`.
   Before any tool call, every value in `step.args` that is a `list` or `dict` is replaced by
   `json.dumps(value, ensure_ascii=False)`, because the tools take JSON as text (`filters`,
   `filter`, `args`, `fields`); strings, numbers and booleans pass unchanged.
4. In attended mode, a tool result that is a dict with `status == "approval_pending"` prints
   `f"[{id}] waiting for approval: {result['url']}"`, then calls `privacyfence_await_approval` with
   `{"approval_ids": [result["approval_id"]], "timeout_seconds": 120}` until the status is not
   `pending`, at most 8 times (16 minutes; a pending approval expires after 15). `approved` → repeat
   the original call with the same args; anything else raises `ToolDenied(f"step {id}: {status}")`.
5. After the last step, unless `attended`, call `privacyfence_end_unattended_session` with the same
   reason form. Errors from the end call are ignored.

#### Sign-in (`auth.py`)

- `FileTokenStorage(path: Path)` implements the SDK's `TokenStorage`: one JSON file holding
  `{"tokens": <OAuthToken model_dump>, "client_info": <OAuthClientInformationFull model_dump>}`,
  written atomically (temp file plus `os.replace`) with mode `0o600`.
- `storage_path(server_url) -> Path`: `$XDG_CONFIG_HOME` or `~/.config`, then
  `privacyfence-sync/<first 16 hex of sha256(server_url)>.json`.
- `oauth_provider(server_url, storage, *, interactive: bool, callback_port: int)` returns the SDK's
  `OAuthClientProvider` with `OAuthClientMetadata(client_name="privacyfence-sync",
  redirect_uris=[f"http://127.0.0.1:{callback_port}/callback"], grant_types=["authorization_code",
  "refresh_token"], response_types=["code"], token_endpoint_auth_method="none")`.
  - Interactive: `redirect_handler` prints `"Open this page to sign in: <url>"` and calls
    `webbrowser.open(url)`; `callback_handler` serves one `GET /callback` on
    `127.0.0.1:callback_port` with `http.server` in a thread, answers `"Signed in to PrivacyFence.
    You can close this tab."`, and returns `AuthorizationCodeResult(code=<code>, state=<state or
    None>, iss=<iss or None>)` from the query string; it gives up after 300 s with
    `SyncError("No sign-in arrived within 5 minutes.")`.
  - Non-interactive (`run`): both handlers raise `SyncError(f"Not signed in to {server_url}, or the
    sign-in expired. Run: privacyfence-sync login --server {server_url}")`.
- A refresh-token chain ends 30 days after the sign-in (`web/oauth_provider.py`), so a scheduled
  org-mode run needs `privacyfence-sync login` again once a month; the README says so.
- `DEFAULT_CALLBACK_PORT = 33418`. The redirect URI must match exactly (PrivacyFence does no
  port-agnostic loopback matching), so the port is fixed and stored with the client registration.

#### CLI (`cli.py`)

```
privacyfence-sync run RECIPE [--server URL] [--attended] [--token-env NAME]
privacyfence-sync login --server URL [--callback-port N]
privacyfence-sync check RECIPE
```

- The server is `--server`, else the recipe's `server`, else `$PRIVACYFENCE_MCP_URL`, else
  `http://127.0.0.1:8765/mcp`.
- `run` uses a bearer token when the environment variable named by `--token-env` (default
  `PRIVACYFENCE_TOKEN`) is set, else OAuth with the stored sign-in. A token is never taken as a
  command-line argument, because arguments are visible to other users in the process list.
- `login` runs an interactive OAuth sign-in, then calls `privacyfence_status` with
  `{"reason": "privacyfence-sync login check"}` and prints `"Signed in to <server>."`.
- `check` loads and validates the recipe and prints `"<name>: <n> step(s), OK"` with no network
  call.
- Exit codes: `0` success; `1` a recipe, usage or template error; `2` a tool, transfer or transform
  failure; `3` a step denied (`ToolDenied`); `4` a sign-in problem. Every error prints one line
  `privacyfence-sync: <message>` to stderr, never a token.

#### CI and publishing

- `.github/workflows/tests.yml`: a new job `sync` with `name: Sync client (Python ${{
  matrix.python-version }})`, ubuntu-latest, matrix `python-version: ["3.11", "3.13"]`, no `paths:`
  filter (it runs on every PR, so it can be required): checkout with `fetch-depth: 0`,
  `pip install -e "./sync[test]" mypy bandit`, then `pytest sync/tests -q --cov=privacyfence_sync
  --cov-branch --cov-report=term-missing --cov-fail-under=100`, `mypy sync/src`,
  `bandit -r sync/src`. Root `ruff check .` already covers `sync/`.
- `MANIFEST.in` gains `prune sync`: setuptools_scm's file finder packages every tracked file, so
  without it the root `privacyfence` sdist would carry the `sync/` tree.
- `scripts/update_branch_protection.py`: its docstring requires `REQUIRED_STATUS_CHECKS` to change
  in the same PR as the jobs `tests.yml` runs on every PR, so add `"Sync client (Python 3.11)"` and
  `"Sync client (Python 3.13)"` to it (and to whatever `tests/unit/test_update_branch_protection.py`
  pins). Applying the ruleset needs an admin token; the maintainer runs `apply` after the merge
  (Manual steps).
- `.github/workflows/publish-pypi.yml`:
  - In `build`, after "Build sdist and wheel": a step `Build privacyfence-sync` running
    `python -m build --sdist --wheel sync --outdir sync-dist`, and an upload of `sync-dist/` as artifact
    `pypi-dist-sync`. The Telegram `check-dist` and the R2 upload keep reading `dist/` only.
  - Two new jobs, `publish-testpypi-sync` (`needs: [build, publish-testpypi]`, environment
    `testpypi`) and `publish-pypi-sync` (`needs: [build, publish-pypi, publish-testpypi-sync]`,
    environment `pypi`), each with the same `if:` stable-channel condition, `permissions: id-token:
    write`, a download of `pypi-dist-sync` into `dist/`, and the same pinned
    `pypa/gh-action-pypi-publish` step as their counterparts. Being separate jobs that run after
    the main ones, a missing trusted publisher for `privacyfence-sync` can never block the
    `privacyfence` upload.

Rejected:

- **Running the transform inside the daemon, in a sandbox.** The daemon holds the credentials under
  its own account so that nothing an AI writes executes inside the fence (ADR 0003); Apps Script got
  no run tool for the same reason (ADR 0075). The exported file is already on the user's side once
  its export was approved, so transforming it there adds no exposure.
- **A daemon-side source-to-destination pipe.** A flow that moved data between connectors with no
  gate decision would bypass the approval model the product exists for.
- **Shipping the CLI inside the `privacyfence` wheel.** In org mode the user has no local
  PrivacyFence install; the client must install alone, without the daemon's dependencies.
- **`local_path` for uploads and `destination_dir` delivery only.** A privilege-separated daemon
  cannot read or write the user's files, and an org daemon is another machine; upload slots and
  download links work in every mode.

## ADRs

- **0157** — A rule can let personal data through the tools whose result holds no row: a per-rule
  `allow_personal_data` flag, set only in Settings, applies to the export tools and to Peek
  new-version uploads; every other read still scans. Amends ADR 0155 (its "Exempting exports from
  the PII gate" alternative); ADR 0155's Status gains "Amended by 0157".
- **0158** — Peek rules name pages by slug and cover reading comments and stats and uploading new
  versions only; set from Settings, never from the card.
- **0159** — `jira_export_issues` exports up to 10,000 issues, is covered by the "this project"
  search rule through the `jira.read_issue` operation with Verb.SEARCH, and answers an empty result
  without a card.
- **0160** — On an organization-managed install an export's file is delivered through a one-time
  staged link, refused only when `download_delivery.allow_disk_staging` is off. Amends ADR 0155
  (its org refusal); ADR 0155's Status gains "Amended by 0160".
- **0161** — `privacyfence-sync` is a separate distribution and a plain MCP client: no import from
  `privacyfence`, transforms run on the user's side with no PrivacyFence credential in their
  environment, files move only through upload slots and download links, and it is published by
  its own jobs after the main package.

If a parallel branch takes these numbers first, renumber in order before merging (ADR README rule 6).

## Manual steps

Nothing is needed before implementation. After the last merge, before the PR merges, the user does
four things ([step-by-step page](https://claude.ai/artifact/DjbQqHGvLW3qAz4GivWvCX)):

- **ma1** — Run one real recipe end to end against the local daemon: a Salesforce or Jira export,
  a transform, `grist_update_csv`, a Peek page, unattended, with the rules (including the
  personal-data checkbox) created in Settings.
- **ma2** — Sign in with `privacyfence-sync login` against a real organization-mode server and run
  the same recipe there, to prove the OAuth client, the org-mode export link and the upload slot.
- **ma3** — Register the `privacyfence-sync` trusted publisher on TestPyPI and PyPI, so the next
  stable release publishes it.
- **ma4** — Apply the updated required checks (the two "Sync client" legs) to the `main` ruleset
  with `scripts/update_branch_protection.py apply`, which needs a token with rulesets admin rights.

The Jira live check is not manual: `connector-live-check.yml` is dispatched by the Jira phase.

## Risks and open questions

- **`OAuthClientProvider` in the installed `mcp` (2.3.0) may differ from the signature in Current
  state** (it subclasses `RedirectAwareAuth` and is used as an `httpx2` auth). If its constructor,
  `TokenStorage` methods or the `streamable_http_client` `http_client` parameter differ, the sync
  sign-in phase stops with `status=blocked` and quotes the real signature.
- **A test may require every `NEW_SCOPE_SELECTORS` predicate to have a resource type or a v1
  fixture.** If adding `peek.page` fails such a test, extend the test's expectations the way
  `grist.document` is listed there; if the test demands a resource resolver, stop with
  `status=blocked`.
- **Line numbers drift.** Every reference here was read on `5041f0bc`. A worker finds the named
  symbol; if a named symbol does not exist, it stops.
- **Peek's card for `peek_upload_page` with a slug fetches the page first.** A per-slug rule does not
  change that: an unknown slug still fails before the gate.
- **The sync wheel's version.** A wheel built from the unpacked sdist has no `..` git checkout for
  `setuptools_scm` with `root = ".."`. The build therefore uses `python -m build --sdist --wheel
  sync`, which builds both from the source tree, and p8's acceptance compares the wheel's version
  with `python -m setuptools_scm`.

## Implementation manifest

```yaml
plan_slug: data-sync
feature_branch: feature/data-sync
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/DjbQqHGvLW3qAz4GivWvCX
manual_steps_source: docs/data-sync-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-local-end-to-end
    title: Run one real recipe end to end against the local daemon, unattended
    why: CI fakes every connector and the daemon; only a real run shows the rules, the personal-data flag, the export files, grist_update_csv and the Peek upload working together with no card.
  - id: ma2-org-mode-run
    title: Sign in with privacyfence-sync login against a real org-mode server and run the recipe there
    why: The OAuth client, the org-mode export link and the upload slot through a reverse proxy can only be proven against a real organization server and identity provider.
  - id: ma3-pypi-trusted-publisher
    title: Register the privacyfence-sync trusted publisher on TestPyPI and PyPI
    why: A trusted publisher is per PyPI project; without it the publish-*-sync jobs fail at the next stable release (the main package still publishes).
  - id: ma4-required-check
    title: Apply the updated required checks to the main ruleset
    why: The PR adds the two Sync client checks to REQUIRED_STATUS_CHECKS, but only a run of scripts/update_branch_protection.py apply with an admin token makes GitHub require them.
verify_after_merge:
  - python3 -m pytest tests/unit/policy tests/unit/test_gate.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/test_settings_window_html.py -q
  - python3 -m pytest tests/unit/test_export_files.py tests/unit/test_local_files.py tests/unit/connectors/test_grist_connector.py tests/unit/connectors/test_salesforce_connector.py tests/unit/connectors/test_jira_connector.py tests/unit/test_jira_client.py tests/unit/test_qa_fixture_recorder.py -q
  - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_systemic_gate_invariants.py tests/unit/test_code_no_history.py -q
  - sh -c 'if [ -d sync ]; then python3 -m pip install -q -e "./sync[test]" && python3 -m pytest sync/tests -q; fi'
final_checks:
  - docs/data-sync-plan.md and docs/data-sync-plan-manual-steps.html are deleted and nothing links to them (git grep -n "data-sync-plan" prints nothing)
  - ADRs 0157, 0158, 0159, 0160 and 0161 (or the renumbered set) exist in docs/adr/, have Status Accepted and are listed in docs/adr/README.md; ADR 0155's Status has the two "Amended by" lines
  - CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading
  - The PR body links a connector-live-check.yml run whose jira export_issues row passed
  - git grep -nE "^\s*(from|import) privacyfence(\.|\s|$)" sync/src prints nothing
phases:
  - id: p1-personal-data-rules
    title: The allow_personal_data rule flag and the gate path that honours it
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/policy/engine.py
      - src/privacyfence/policy/store.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/describe.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/gate.py
      - tests/unit/policy/test_store.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_describe.py
      - tests/unit/test_gate.py
    brief: |
      Read docs/coding-and-testing-guidelines.md (§1.5 and §2.4 especially), docs/testing-policy.md
      and docs/adr/0015, 0074, 0144 and 0155. Implement Design §3.1 of docs/data-sync-plan.md
      exactly: names, strings and signatures. No comment or docstring may cite the plan
      (tests/unit/test_code_no_history.py). Comments may cite ADR 0157, which phase p9 writes.
      1. src/privacyfence/policy/engine.py: add `allow_personal_data: bool = False` as the last
         PolicyRule field and the docstring sentence from §3.1.
      2. src/privacyfence/policy/store.py: rule_id_for gains `allow_personal_data: bool = False`
         with the key rule from §3.1; rule_id_for_rule, merge_rules, rule_to_dict and
         rule_from_dict as §3.1 says. SCHEMA_VERSION stays 2.
      3. src/privacyfence/auto_accept.py: add PERSONAL_DATA_RULE_TOOLS with its comment (four
         tools; jira_export_issues is added by a later phase).
      4. src/privacyfence/policy/catalogue.py: add PERSONAL_DATA_GROUPS (three groups),
         `personal_data_label` in scope_catalogue entries, and the keyword-only `personal_data`
         parameter on rules_for_catalogue_entry, using dataclasses.replace then
         store.merge_rules. Add PERSONAL_DATA_GROUPS to __all__.
      5. src/privacyfence/policy/describe.py: the rule_sentence suffix.
      6. src/privacyfence/gate.py: import PERSONAL_DATA_RULE_TOOLS and PolicyRule as the module's
         other imports are done; add _personal_data_rule after _evaluate_auto_accept; add the
         block from §3.1 right after the `try:` that precedes the first
         `auto_ok, matched_rule, matched_rule_id = _evaluate_auto_accept(operation_key, ctx)`.
         Stop condition: if that `try:` is not directly followed by that line, stop with
         status=blocked.
      7. Tests (plain pytest, one behaviour each):
         - tests/unit/policy/test_store.py, class TestRuleIdFor:
           test_flag_off_keeps_the_existing_id (rule_id_for with the default equals the value
           computed from repr((predicate, value, conditions)) as today),
           test_flag_on_gives_a_different_id.
           class TestMergeRules: test_flagged_and_plain_rules_on_one_scope_stay_two_rows.
           class TestRuleDictRoundTrip: test_flag_round_trips, test_plain_rule_has_no_flag_key.
           class TestRuleFromDictFailsClosed: test_non_bool_flag_drops_the_rule (parametrize
           over "yes", 1 and None, each written as the value of the allow_personal_data key).
         - tests/unit/policy/test_catalogue.py: test_personal_data_label_only_on_the_three_groups,
           test_personal_data_sets_the_flag_on_every_compiled_rule (grist.document and
           jira.project; for jira.project both predicates carry it),
           test_personal_data_on_another_group_compiles_nothing (apps_script.project),
           test_flagged_rule_ids_differ_from_plain_ones.
         - tests/unit/policy/test_describe.py: test_sentence_says_personal_data_is_allowed.
         - tests/unit/test_gate.py, a new class TestPersonalDataRules next to TestPIIGate, using
           the fixtures TestPIIGate and TestUploadPiiGate already use:
           test_flagged_rule_auto_accepts_an_export_despite_pii (grist_export_records with
           pii_scan_text holding an email address, a flagged grist.document rule; audit entry has
           decision auto_accepted, rule_id the rule's id, pii_detected True and the PII category);
           test_flagged_rule_auto_accepts_in_an_unattended_session;
           test_unflagged_rule_still_shows_the_card_on_pii (existing behaviour, pinned);
           test_flag_does_not_apply_to_grist_get_records (same rule and PII, grist_get_records →
           card, and denied_unattended when unattended);
           (the popup-gate case, peek_upload_page, is tested in phase p3 once the peek.page scope
           exists);
           test_no_pii_takes_the_ordinary_path (flagged rule, no PII → auto_accepted, pii_detected
           False).
      8. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py`,
         `python3 -m pytest tests/unit -q`.
      No changelog line (phase p9 writes them).
    acceptance:
      - python3 -m pytest tests/unit/policy tests/unit/test_gate.py -q passes
      - python3 -m pytest tests/unit/test_gate.py -q -k "PersonalDataRules" passes and runs 5 tests
      - grep -n "allow_personal_data: bool = False" src/privacyfence/policy/engine.py matches
      - grep -n "PERSONAL_DATA_RULE_TOOLS" src/privacyfence/auto_accept.py src/privacyfence/gate.py matches in both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p2-personal-data-settings
    title: The personal-data checkbox on the Settings "Add a rule" form, in both modes
    depends_on: [p1-personal-data-rules]
    complexity: S
    touches:
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Read docs/coding-and-testing-guidelines.md §1.10 (design system) and Design §3.2 of
      docs/data-sync-plan.md. No comment or docstring may cite the plan.
      1. src/privacyfence/settings_window_html.py: the four JS changes in §3.2 (ui state field,
         the chip after the verb chips, the click and keyboard handling, the post body and the
         resets). Find the keyboard handler by searching for the other place
         `data-aa-verb-toggle` is handled for Enter/Space; if there is none, handle the chip in the
         click handler only and say so in the commit message.
      2. src/privacyfence/settings_controller.py: add_policy_rule gains `personal_data: bool =
         False` and passes it on; add the docstring sentence.
      3. src/privacyfence/web/routes_settings.py: the org-mode add_policy_rule branch reads
         `body.get("personal_data") is True`, passes it, and uses the longer audit summary when it
         is true.
      4. Tests:
         - tests/unit/test_settings_window_html.py: test_personal_data_chip_is_rendered_for_a_group_with_a_label
           and test_add_rule_posts_personal_data, in the style of the file's existing
           auto-accept form tests (find them by searching for data-aa-add).
         - tests/unit/test_settings_controller.py: test_add_policy_rule_with_personal_data_stores_the_flag
           (the saved settings.yaml rule has allow_personal_data: true) and
           test_add_policy_rule_personal_data_on_an_unsupported_group_adds_nothing.
         - tests/unit/web/test_routes_settings.py: test_add_policy_rule_passes_personal_data
           (a bool reaches the controller through _call_action).
         - tests/unit/web/test_routes_org_settings.py: test_org_add_policy_rule_with_personal_data
           (the stored rule carries the flag, the audit summary says "personal data allowed"), and
           test_org_add_policy_rule_ignores_a_non_true_personal_data ("true" as a string adds a
           rule without the flag).
      5. If test_browser_smoke.py or a Playwright test covers the Auto-accept form, run it with
         PRIVACYFENCE_TEST_CHROMIUM set (see .claude/skills/steward/SKILL.md); a SKIPPED run is
         not a pass.
      6. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py -q passes
      - grep -n "data-aa-personal-data" src/privacyfence/settings_window_html.py matches at least twice
      - grep -n "personal_data: bool = False" src/privacyfence/settings_controller.py matches
      - python3 -m pytest tests/unit -q passes

  - id: p3-peek-page-rules
    title: The Peek page scope (per slug, read and new versions)
    depends_on: [p1-personal-data-rules]
    complexity: S
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_registry.py
      - tests/unit/policy/test_propose.py
      - tests/unit/test_gate.py
    brief: |
      Read docs/adr/0144 and Design §3.3 of docs/data-sync-plan.md. No comment or docstring may
      cite the plan; comments may cite ADR 0158.
      1. src/privacyfence/policy/scopes.py: add _peek_page_matches and the "peek.page" entry in
         NEW_SCOPE_SELECTORS exactly as §3.3. Use the module's existing `_values_of` helper.
      2. src/privacyfence/policy/catalogue.py: add the "peek.page" EXTRA_SCOPES entry and the
         PERSONAL_DATA_GROUPS entry from §3.3.
      3. Tests:
         - tests/unit/policy/test_scopes.py, next to test_grist_document_matches_doc_id_from_args:
           test_peek_page_matches_a_named_slug, test_peek_page_never_matches_a_call_without_a_slug,
           test_peek_page_does_not_match_another_slug.
         - tests/unit/policy/test_catalogue.py: the existing assertion that only grist.document
           carries verb_labels (near line 112) must now allow grist.document and peek.page;
           change it to that set, nothing looser. Add test_peek_page_read_compiles_comments_and_stats
           (operations == {"peek.read_comments", "peek.read_stats"}),
           test_peek_page_update_compiles_upload_only (operations == {"peek.upload_page"}),
           test_peek_page_offers_the_personal_data_label.
         - tests/unit/test_gate.py, in TestPreflightAutoAccept next to
           test_stored_grist_document_rule_auto_accepts_only_that_document:
           test_stored_peek_page_rule_covers_a_new_version_only (peek.upload_page with
           {"slug": "dash"} → auto_accept; with {"slug": ""} → not auto_accept).
         - tests/unit/test_gate.py, class TestPersonalDataRules (added by p1):
           test_flagged_peek_page_rule_lets_an_upload_with_pii_through (gated_call with
           tool="peek_upload_page", gate="popup", args {"slug": "dash", ...},
           upload_pii_scan_text holding an email address, a stored peek.page rule for "dash" with
           operations {"peek.upload_page"} and allow_personal_data=True → auto_accepted, audit
           pii_detected True) and test_unflagged_peek_page_rule_shows_the_card_on_pii.
         - If tests/unit/policy/test_propose.py or test_registry.py lists operations that no
           proposable scope reaches, or extra-scope operations that stay unproposed, add the three
           peek operations there the way the grist ones are listed. Stop condition: if a test
           requires a resource resolver for every NEW_SCOPE_SELECTORS predicate, stop with
           status=blocked.
      4. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/policy tests/unit/test_gate.py -q passes
      - grep -n '"peek.page"' src/privacyfence/policy/scopes.py src/privacyfence/policy/catalogue.py matches in both files
      - python3 -m pytest tests/unit -q passes

  - id: p4-org-export-delivery
    title: Export tools deliver a one-time link on an organization-managed install
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/export_files.py
      - src/privacyfence/local_files.py
      - src/privacyfence/connectors/grist.py
      - src/privacyfence/connectors/salesforce.py
      - src/privacyfence/daemon_main.py
      - tests/unit/test_export_files.py
      - tests/unit/test_local_files.py
      - tests/unit/connectors/test_grist_connector.py
      - tests/unit/connectors/test_salesforce_connector.py
      - docs/tools-reference.md
    brief: |
      Read docs/adr/0007, 0017, 0028, 0155 and Design §3.4 of docs/data-sync-plan.md. No comment
      or docstring may cite the plan; comments may cite ADR 0160.
      1. src/privacyfence/export_files.py: delete refuse_org; add ORG_SAVED_TO,
         ORG_DELIVERY_SENTENCE, refuse_org_without_staging and saved_to exactly as §3.4.
      2. src/privacyfence/local_files.py: add deliver_export as §3.4, and add it to __all__.
      3. src/privacyfence/connectors/grist.py (grist_export_records and grist_export_sql) and
         src/privacyfence/connectors/salesforce.py (salesforce_export_report): the five changes in
         §3.4 for each handler and description, and the two new __init__ attributes.
      4. src/privacyfence/daemon_main.py: set download_config and download_base_url on the
         Salesforce and Grist connectors next to their download_mode assignment.
      5. Tests:
         - tests/unit/test_export_files.py: replace TestRefuseOrg with TestRefuseOrgWithoutStaging
           (test_raises_only_in_org_mode_with_staging_off, test_org_mode_with_staging_on_passes,
           test_local_mode_never_raises) and add TestSavedTo (test_local_joins_the_path,
           test_org_says_one_time_link).
         - tests/unit/test_local_files.py, a new class TestDeliverExport:
           test_local_mode_calls_deliver_file (monkeypatch deliver_file),
           test_org_mode_stages_and_returns_a_link (a fake staging store whose stage returns
           b"\x01"*32; result delivery "link", download_url starts with the base URL and
           "/mcp-files/fetch/", expires_at is an ISO string, note == LINK_RESULT_NOTE),
           test_org_mode_uses_the_config_ttl, test_org_mode_checks_the_delivery_size.
         - tests/unit/connectors/test_grist_connector.py (TestExportRecords, TestExportSql) and
           tests/unit/connectors/test_salesforce_connector.py (TestExportReport): replace each
           test_org_mode_is_refused_before_any_request with
           test_org_mode_delivers_a_one_time_link (monkeypatch local_files.deliver_export; assert
           it got download_mode "org", the configured download_config and download_base_url, the
           card's "Saved to" is ORG_SAVED_TO and the audit delivery is "staged_link") and
           test_org_mode_without_staging_is_refused_before_any_request (download_config with
           allow_disk_staging=False; no client call, no gated_call). Update the `delivered`
           fixtures and test_audit_delivery_is_passed to patch deliver_export instead of
           deliver_file.
      6. Regenerate docs/tools-reference.md with `python3 scripts/generate_tools_reference.py`
         (the descriptions changed). Run `python3 -m pytest tests/unit/test_docs_tools_reference.py -q`.
      7. The connector files changed, so the profile's conditional row applies: push the phase
         branch, dispatch connector-live-check.yml against it (GitHub MCP actions_run_trigger,
         ref = the phase branch, no inputs; a queued run is normal, it shares a concurrency
         group), and confirm the grist and salesforce rows passed. Report the run URL in the phase
         result. A red row is a phase failure; do not re-run it away. Then `ruff check .`, `python3 -m mypy src/privacyfence`, `bandit -c pyproject.toml -r
         src`, `python3 -m pytest tests/unit -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_export_files.py tests/unit/test_local_files.py tests/unit/connectors/test_grist_connector.py tests/unit/connectors/test_salesforce_connector.py tests/unit/test_docs_tools_reference.py -q passes
      - The phase result names a connector-live-check.yml run whose grist and salesforce rows passed
      - git grep -n "refuse_org(" src tests prints nothing
      - git grep -n "Not available on an organization-managed install" src prints nothing
      - grep -n '"deliver_export"' src/privacyfence/local_files.py matches
      - python3 -m pytest tests/unit -q passes

  - id: p5-jira-export
    title: jira_export_issues, its registry entries, docs, website counts and live-check row
    depends_on: [p1-personal-data-rules, p3-peek-page-rules, p4-org-export-delivery]
    complexity: M
    touches:
      - src/privacyfence/jira_client.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - src/privacyfence/daemon_main.py
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_jira_client.py
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/unit/policy/test_propose.py
      - tests/unit/test_gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read docs/adr/0134, 0135, 0155 and Design §3.5 of docs/data-sync-plan.md. Copy the shape of
      commit fd144e08 (salesforce_export_report: connector, registries, tests, generated docs,
      website counts) and the QA row of 7882c602. No comment or docstring may cite the plan;
      comments may cite ADR 0159.
      1. src/privacyfence/jira_client.py: EXPORT_MAX_ISSUES and JiraClient.export_issues as §3.5.
      2. src/privacyfence/connectors/jira.py: _ExportedIssue with its comment, _EXPORT_COLUMNS,
         _DESTINATION_DIR, the ToolSpec, the dispatch entry next to
         jira_search_issues_with_fields', the _export_issues handler, and the three __init__
         attributes if absent.
      3. src/privacyfence/daemon_main.py: set download_mode, download_config and
         download_base_url on the Jira connector where it is built.
      4. Registries: auto_accept.TOOL_TO_OPERATION, TOOL_TO_GATE and PERSONAL_DATA_RULE_TOOLS;
         policy/registry.py TOOL_TO_VERB (Verb.SEARCH); gate._TOOL_LAYOUT (WIDE).
      5. scripts/qa_fixture_recorder.py: _check_jira_export and its append in check_jira (§3.5).
      6. Tests:
         - tests/unit/test_jira_client.py: test_export_issues_pages_until_the_last_page,
           test_export_issues_raises_past_the_limit_without_reading_further (max_issues=150,
           pages of 100 → the third page is never requested), test_export_issues_rejects_empty_jql.
         - tests/unit/connectors/test_jira_connector.py, a new class TestExportIssues modelled on
           TestExportReport in test_salesforce_connector.py: test_result_is_summary_only,
           test_columns_are_standard_then_extra_fields, test_approval_args_carry_sha256,
           test_raw_data_carries_keys_only, test_this_project_search_rule_auto_accepts (a stored
           approved_project_keys_all_results rule with operations {"jira.read_issue"} and the
           real gate), test_empty_result_writes_no_file_and_shows_no_card,
           test_over_the_delivery_limit_is_refused_before_the_card, test_deny_writes_nothing,
           test_org_mode_delivers_a_one_time_link,
           test_org_mode_without_staging_is_refused_before_any_request. Add jira_export_issues to
           the file's TestEveryToolIsAudited arg_overrides if it needs real values (jql, fields
           "", destination_dir "/tmp/x", file_name "").
         - tests/unit/test_gate.py TestPersonalDataRules: test_flag_applies_to_jira_export_issues.
         - tests/unit/policy/test_propose.py: the CORPUS entry for jira_export_issues as for
           jira_search_issues_with_fields.
         - tests/unit/test_qa_fixture_recorder.py TestCheckJira (around line 841): add
           test_export_issues_row_passes and test_export_row_fails_on_a_key_mismatch, each picking
           the row with next(r for r in results if r.method == "export_issues"), using the class's
           existing fakes for enhanced_jql and get_all_fields. The existing test at lines 882-900
           that reads enhanced_jql.call_args must still pass unchanged, which is why the export row
           runs before the search_issues_page row.
      7. Regenerate docs/tools-reference.md (`python3 scripts/generate_tools_reference.py`) and
         docs/always-allow-rules-reference.md (`python3 scripts/generate_always_allow_reference.py`).
         Update website/connectors/index.html (Jira card data-tools 13, data-review 3, its printed
         counts line "13 tools: 6 without a card · 3 reviewed · 4 need approval", the h1 total 141,
         and the card's "Reviewed before release" text, which becomes "An issue's full details,
         including its description and comments, issue searches that return extra fields, and
         exports of a search to a file.") and website/how-it-works/index.html ("Each of the 141
         connector tools"). Stop condition: if the tools-reference Total row is not 141 / 50 / 29
         / 62 after regeneration, stop with status=blocked.
      8. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py`,
         `bandit -c pyproject.toml -r src`, `python3 -m pytest tests/unit -q`.
      9. Push the phase branch, then dispatch connector-live-check.yml against it (GitHub MCP
         actions_run_trigger, ref = the phase branch, no inputs; it shares a concurrency group,
         so a queued run is normal). Read the connector-live-check-report artifact or the job
         log and confirm the jira export_issues row passed. Report the run URL in the phase
         result, so the orchestrator puts it in the PR body. A red row is a phase failure; do
         not re-run it away.
    acceptance:
      - python3 -m pytest tests/unit/test_jira_client.py tests/unit/connectors/test_jira_connector.py tests/unit/test_qa_fixture_recorder.py tests/unit/policy tests/unit/test_gate.py -q passes
      - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_website_connectors_page.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_systemic_gate_invariants.py tests/unit/test_connector_tool_annotations.py -q passes
      - grep -n "| \*\*Total\*\* | \*\*141\*\*" docs/tools-reference.md matches
      - grep -n '"jira_export_issues"' src/privacyfence/auto_accept.py matches at least three times
      - The phase result names a connector-live-check.yml run whose jira export_issues row passed
      - python3 -m pytest tests/unit -q passes

  - id: p6-sync-recipe-core
    title: privacyfence-sync package skeleton, recipe loading, template rendering and transforms
    depends_on: []
    complexity: M
    touches:
      - sync/pyproject.toml
      - sync/src/privacyfence_sync/__init__.py
      - sync/src/privacyfence_sync/recipe.py
      - sync/src/privacyfence_sync/template.py
      - sync/src/privacyfence_sync/transform.py
      - sync/tests/conftest.py
      - sync/tests/test_recipe.py
      - sync/tests/test_template.py
      - sync/tests/test_transform.py
      - sync/tests/test_no_daemon_imports.py
    brief: |
      Read Design §3.6 of docs/data-sync-plan.md up to and including "Transform". This package
      must import nothing from privacyfence. Python >= 3.11, type hints everywhere, stdlib plus
      pyyaml only in these three modules.
      1. Create sync/pyproject.toml exactly as §3.6.
      2. sync/src/privacyfence_sync/__init__.py: a module docstring "Run a PrivacyFence data-sync
         recipe as a plain MCP client." and `__version__` read with
         `importlib.metadata.version("privacyfence-sync")`, falling back to "0.0.0.dev0" on
         PackageNotFoundError.
      3. recipe.py: RecipeError, the constants, the four dataclasses and load_recipe with every
         check listed in §3.6, each error text starting with the recipe path.
      4. template.py: PLACEHOLDER, TemplateError, render_template.
      5. transform.py: TransformError, transform_env, run_transform.
      6. sync/README.md already exists (written before implementation as the user-facing spec).
         Do not rewrite it. Read it, and make the recipe checks and error texts match what it
         promises; if the code cannot match a sentence in it, stop with status=blocked and quote
         the sentence.
      7. Tests (pytest, tmp_path, no network):
         - test_recipe.py: test_loads_the_example_recipe (the §3.6 example written to tmp_path),
           and one test per check in §3.6, parametrized where the check is the same shape
           (bad name, bad id, unknown key, step kind count, later-step reference, non-file
           reference, unsafe file names, missing file_name on an export, destination_dir given,
           upload without file, local_path/upload_id/html given, file on a non-upload tool, path
           outside the recipe dir, template data not .json, more than 50 steps, version 2).
         - test_template.py: test_replaces_the_placeholder, test_requires_exactly_one_placeholder
           (zero and two), test_escapes_script_breakout ("</script>" in data never appears in the
           output), test_escapes_line_separators.
         - test_transform.py: test_runs_the_script_with_inputs_then_output (a script written to
           tmp_path that concatenates inputs), test_env_has_no_privacyfence_keys
           (monkeypatch.setenv("PRIVACYFENCE_TOKEN", "x"); the script writes os.environ keys),
           test_nonzero_exit_raises_with_stderr, test_timeout_raises, test_missing_output_raises.
         - test_no_daemon_imports.py as §3.6 describes.
      8. `python3 -m pip install -e "./sync[test]"`, then
         `python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch
         --cov-report=term-missing`, `python3 -m mypy sync/src`, `bandit -r sync/src`,
         `ruff check .`. A bandit B404/B603 finding on the subprocess call is a genuine false
         positive (fixed argv, no shell): suppress it at the call site with `# nosec B603  #
         fixed argv, no shell; the script is the user's own reviewed file`.
    acceptance:
      - python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes (after pip install -e "./sync[test]")
      - python3 -m mypy sync/src passes
      - ruff check . passes
      - git grep -nE "^\s*(from|import) privacyfence(\.|\s|$)" sync/src prints nothing
      - python3 -m pytest tests/unit -q passes (the root suite is unaffected)

  - id: p7-sync-client-runner
    title: privacyfence-sync MCP client, step runner and the run and check commands
    depends_on: [p6-sync-recipe-core]
    complexity: M
    touches:
      - sync/src/privacyfence_sync/client.py
      - sync/src/privacyfence_sync/runner.py
      - sync/src/privacyfence_sync/cli.py
      - sync/tests/test_client.py
      - sync/tests/test_runner.py
      - sync/tests/test_cli.py
    brief: |
      Read Design §3.6 of docs/data-sync-plan.md ("Client", "Runner", "CLI" — except `login`,
      which phase p8 adds) and tests/integration/test_mcp_daemon_contract.py:132-153 for the SDK
      client pattern. Import nothing from privacyfence.
      1. client.py: ToolFailed, ToolDenied, SyncError (all Exception subclasses; SyncError the
         base of the other two), SyncClient and connect_token as §3.6. connect_oauth is added by
         p8; do not stub it.
      2. runner.py: run_recipe as §3.6 steps 1-5.
      3. cli.py: argparse with subcommands run and check (login comes in p8), the server and
         token resolution rules, the exit codes, and `main(argv: list[str] | None = None) -> int`
         with `raise SystemExit(main())` under `if __name__ == "__main__":`. In this phase, `run`
         without a token prints "privacyfence-sync: set PRIVACYFENCE_TOKEN (or --token-env), or
         sign in with privacyfence-sync login" and exits 4.
      4. Tests, with no network: a FakeClient test double in sync/tests/test_runner.py exposing
         call/download/upload with scripted results.
         - test_client.py: test_call_parses_json (patch ClientSession.call_tool to return a
           CallToolResult with text content), test_call_raises_tool_denied_on_request_denied,
           test_call_raises_tool_failed_on_other_errors, test_download_sends_no_authorization
           and test_download_checks_sha256 (httpx2.MockTransport), test_upload_puts_to_the_slot
           (MockTransport plus a scripted privacyfence_create_upload_slot result).
         - test_runner.py: test_begins_and_ends_an_unattended_session,
           test_unattended_disabled_gives_the_admin_hint, test_export_via_link_downloads_into_work_dir,
           test_export_on_local_disk_is_copied_and_checked, test_empty_jira_export_writes_a_header_only_csv,
           test_unexpected_delivery_fails, test_upload_step_passes_the_upload_id,
           test_transform_and_template_steps_chain (real files in tmp_path),
           test_browser_only_link_gives_the_agent_links_hint,
           test_list_and_mapping_args_are_sent_as_json_text,
           test_attended_waits_then_repeats_the_call,
           test_attended_denial_raises, test_default_reason_names_recipe_and_step.
         - test_cli.py: test_check_prints_ok_without_network, test_check_bad_recipe_exits_1,
           test_run_without_token_exits_4, test_server_resolution_order (flag, recipe, env,
           default), test_token_is_never_an_argument (argparse has no --token option),
           test_denied_step_exits_3, test_failed_step_exits_2.
      5. `python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch
         --cov-report=term-missing` at 100%, `python3 -m mypy sync/src`, `bandit -r sync/src`,
         `ruff check .`.
      Stop condition: if `streamable_http_client` in the installed mcp has no `http_client`
      parameter, stop with status=blocked quoting its signature.
    acceptance:
      - python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes
      - python3 -m mypy sync/src and bandit -r sync/src pass
      - python3 -m pytest sync/tests/test_cli.py -q -k "token_is_never_an_argument" passes
      - ruff check . passes

  - id: p8-sync-oauth-ci-publish
    title: privacyfence-sync OAuth sign-in, its CI job and PyPI publishing
    depends_on: [p7-sync-client-runner]
    complexity: M
    touches:
      - sync/src/privacyfence_sync/auth.py
      - sync/src/privacyfence_sync/client.py
      - sync/src/privacyfence_sync/cli.py
      - sync/tests/test_auth.py
      - sync/tests/test_cli.py
      - .github/workflows/tests.yml
      - .github/workflows/publish-pypi.yml
      - scripts/update_branch_protection.py
      - tests/unit/test_update_branch_protection.py
      - MANIFEST.in
      - docs/releasing.md
    brief: |
      Read Design §3.6 of docs/data-sync-plan.md ("Sign-in", "CLI", "CI and publishing") and
      docs/releasing.md "Publishing to PyPI". Import nothing from privacyfence.
      1. Check the installed SDK first: `python3 -c "import inspect, mcp.client.auth as a;
         print(inspect.signature(a.OAuthClientProvider.__init__))"` and the TokenStorage methods.
         Stop condition: if they differ from Current state, stop with status=blocked quoting them.
      2. auth.py: FileTokenStorage, storage_path, DEFAULT_CALLBACK_PORT, oauth_provider (both
         modes) as §3.6.
      3. client.py: connect_oauth(server_url, storage).
      4. cli.py: the login subcommand; run falls back to OAuth when no token env var is set
         (replacing p7's exit-4 message with the SyncError from the non-interactive handlers,
         still exit 4).
      5. sync/README.md already documents the commands, sign-in, exit codes and scheduling.
         Check every statement in its "Connecting to PrivacyFence" and "Commands" sections
         against the code; fix the code where it differs, and stop with status=blocked if a
         sentence cannot be made true.
      6. .github/workflows/tests.yml: the sync job as §3.6, with actions pinned to the same SHAs
         the file's other jobs use. scripts/update_branch_protection.py: add the two
         "Sync client (Python 3.x)" contexts to REQUIRED_STATUS_CHECKS, and update
         tests/unit/test_update_branch_protection.py if it pins the list; run
         `python3 -m pytest tests/unit/test_update_branch_protection.py -q`.
      7. .github/workflows/publish-pypi.yml: the build step, the artifact and the two jobs as
         §3.6, copying the pins, `if:` conditions and environment names from publish-testpypi
         and publish-pypi. MANIFEST.in: add `prune sync` with a one-line comment saying why.
         Check both builds: `python3 -m build --sdist --wheel sync --outdir /tmp/sync-dist` and
         `python3 -m build --sdist --outdir /tmp/root-dist`; the sync wheel's version (from its
         file name) equals `python3 -m setuptools_scm`, and `tar tzf /tmp/root-dist/*.tar.gz |
         grep "/sync/"` prints nothing.
      8. docs/releasing.md "Publishing to PyPI": a paragraph saying the release also publishes
         privacyfence-sync from sync/ with the same version, through publish-testpypi-sync and
         publish-pypi-sync after the main uploads, and that its trusted publisher is registered
         the same way with Project Name `privacyfence-sync` and the same workflow and
         environments.
      9. Tests: test_auth.py: test_storage_round_trips_tokens_and_client_info,
         test_storage_file_is_mode_600 (skip on Windows with a reason),
         test_storage_path_uses_xdg_config_home, test_non_interactive_handlers_raise_the_login_hint,
         test_callback_server_returns_code_and_state (drive the handler with a real loopback GET
         from a thread), test_client_metadata_is_a_public_loopback_client. test_cli.py:
         test_login_runs_status_and_prints_signed_in (fake provider and client),
         test_run_without_token_uses_oauth.
      10. `python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch` at 100%,
          `python3 -m mypy sync/src`, `bandit -r sync/src`, `ruff check .`. Validate both workflow
          files parse: `python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]"
          .github/workflows/tests.yml .github/workflows/publish-pypi.yml`. Push the phase branch,
          then dispatch tests.yml against it (GitHub MCP actions_run_trigger, ref = the phase
          branch; tests.yml runs on push only for main and releases/**, and has
          workflow_dispatch), and confirm both "Sync client" legs are green. Report the run URL.
    acceptance:
      - python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes
      - python3 -m mypy sync/src and bandit -r sync/src pass
      - grep -n "publish-pypi-sync:" .github/workflows/publish-pypi.yml matches
      - grep -n "pip install -e \"./sync\[test\]\"" .github/workflows/tests.yml matches
      - The phase result names a tests.yml run on the phase branch where both "Sync client" legs passed
      - The built sync wheel's version equals python3 -m setuptools_scm, and the root sdist lists no sync/ path
      - grep -n '"Sync client (Python 3.11)"' scripts/update_branch_protection.py matches

  - id: p9-retire
    title: ADRs 0157-0161, reference docs, changelog, and retire the plan
    depends_on: [p2-personal-data-settings, p3-peek-page-rules, p5-jira-export, p8-sync-oauth-ci-publish]
    complexity: M
    touches:
      - docs/adr/0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md
      - docs/adr/0158-peek-rules-name-pages-and-cover-only-new-versions.md
      - docs/adr/0159-jira-export-issues-is-covered-by-the-project-search-rule.md
      - docs/adr/0160-org-mode-exports-are-delivered-by-a-one-time-link.md
      - docs/adr/0161-privacyfence-sync-is-a-separate-plain-mcp-client.md
      - docs/adr/0155-exports-write-a-file-and-return-only-counts-and-a-hash.md
      - docs/adr/README.md
      - docs/approvals-and-policy.md
      - docs/how-it-works.md
      - docs/peek-setup.md
      - docs/org-mode-setup-guide.md
      - docs/atlassian-setup.md
      - docs/privacyfence-sync*.md
      - docs/README.md
      - CHANGELOG.md
      - docs/data-sync-plan.md
      - docs/data-sync-plan-manual-steps.html
      - tests/unit/test_docs_no_history.py
    brief: |
      Read docs/adr/README.md (template and rules) and docs/coding-and-testing-guidelines.md §5.
      1. Write the five ADRs listed in the plan's ADRs section, each with Status "Accepted —
         <today>. Implemented.", Context, Decision, Alternatives considered (from the "Rejected"
         lists in Design §3.1, §3.3, §3.4, §3.5 and §3.6), Consequences, Verification (the test
         classes the phases added) and Related (source files and ADRs; never this plan). 0157 and
         0160 say "Amends ADR 0155". 0157 states that an exported file lands where the user's own
         processes can read it, as before, and that the PII gate was never a data-loss control.
         If 0157-0161 are taken on main, renumber in order and fix every comment that cites them
         (git grep -n "ADR 015[7-9]\|ADR 016[01]" src sync scripts).
      2. docs/adr/0155-...md: add two Status lines only, "Amended by [ADR 0157](0157-...md)." and
         "Amended by [ADR 0160](0160-...md)." (rule 2: nothing else in its body changes).
      3. docs/adr/README.md: index rows for the five ADRs.
      4. Reference docs, describing today's behaviour only (§5):
         - docs/approvals-and-policy.md: the personal-data checkbox (which scopes offer it, what
           it does, that only Settings sets it, how it shows in the audit log) and the Peek page
           scope.
         - docs/how-it-works.md: in "Files", that exports on an organization-managed install
           return a one-time download link; in "Unattended sessions", a sentence pointing to
           the new privacyfence-sync.md in the docs folder.
         - docs/peek-setup.md: the per-page rule.
         - docs/org-mode-setup-guide.md "File delivery": exports use the same staged link and
           respect allow_disk_staging.
         - docs/atlassian-setup.md: one sentence after the paragraph that mentions
           jira_search_issues_with_fields (around line 80) saying jira_export_issues writes a
           whole search to a file in one approval, and that the same "this project" rule covers it.
         - privacyfence-sync.md (new, in the docs folder): who it is for, install, local mode (token from
           privacyfence-app --print-mcp-token in PRIVACYFENCE_TOKEN), org mode (login),
           unattended_sessions.enabled, the rules a recipe needs and how to find a missing one
           (run --attended once), and a link to sync/README.md on GitHub for the recipe format.
         - docs/README.md: link the new privacyfence-sync.md in the docs folder under "Using
           PrivacyFence" in the "User and operator docs" half (so it is published), and add it to
           PUBLISHED_DOCS in tests/unit/test_docs_no_history.py.
      5. CHANGELOG.md under "## [Unreleased]", never a version heading: one line each for the
         personal-data checkbox, the Peek page rule, jira_export_issues, exports on
         organization-managed installs, and privacyfence-sync.
      6. Delete docs/data-sync-plan.md and docs/data-sync-plan-manual-steps.html; `git grep -n
         "data-sync-plan"` must print nothing.
      7. `python3 -m pytest tests/unit -q` (it includes test_docs_links.py,
         test_docs_references_exist.py, test_website_docs_allowlist.py and test_docs_no_history.py)
         and `ruff check .`.
    acceptance:
      - ls docs/adr/0157-*.md docs/adr/0158-*.md docs/adr/0159-*.md docs/adr/0160-*.md docs/adr/0161-*.md succeeds (or the renumbered set)
      - grep -c "Amended by" docs/adr/0155-exports-write-a-file-and-return-only-counts-and-a-hash.md prints 2
      - git grep -n "data-sync-plan" prints nothing
      - sed -n '/## \[Unreleased\]/,/## \[[0-9]/p' CHANGELOG.md contains "privacyfence-sync" and "jira_export_issues"
      - python3 -m pytest tests/unit -q passes
```
