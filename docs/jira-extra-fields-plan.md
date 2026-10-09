# Plan: Jira extra fields and field lookup

## Goal

`jira_search_issues` returns a fixed set of fields: key, summary, status, issue type, priority,
assignee, reporter, labels, created, updated and url. Users want more fields: custom ones such as
Story Points, Sprint or Team, and built-in ones such as due date or fix versions. The agent does not
know a site's field ids (`customfield_10016`, or `cf[10016]` in JQL), so it also needs a way to turn
the names people use into ids.

After this change:

- **`jira_list_fields`** (auto-approved) lists the site's fields: name, id, whether it is custom,
  type, and the names JQL accepts for it. For example, the agent finds "Story Points" and learns
  that its id is `customfield_10016`, or `cf[10016]` in JQL.
- **`jira_search_issues_with_fields`** (new, **reviewed**) runs a JQL search and adds the requested
  fields to each issue. A field can be asked for by name, id or JQL id. The approval card shows a
  table of every issue and every requested value before anything is released.
- **`jira_get_issue`** takes the same optional `fields` parameter. The extra values are added to its
  existing review card.
- **`jira_search_issues`** keeps its shape and stays auto-approved. It now asks Jira only for the
  fields it returns, instead of `*all`.

The user asked for this in a planning session. There is no tracking issue.

## Current state

- `JiraClient.search_issues_page` (`src/privacyfence/jira_client.py:313`) calls
  `self._client.enhanced_jql(jql, nextPageToken=..., limit=...)` with no `fields`. The library
  default is `fields="*all"`, so every page downloads every field, description and comments
  included. `_parse_issue` (`jira_client.py:546`) then discards all but nine of them.
  - The plugin source `jira.search` (`src/privacyfence/plugins/source_ops.py:258`) also calls
    `search_issues_page`. It will get the explicit field list too; its output shape does not change.
- `JiraClient.get_issue` (`jira_client.py:352`) calls `self._client.issue(issue_key)`, whose
  library default is also `fields="*all"`. The raw issue therefore already carries every field.
- Looking up a field by name exists for writes only:
  - `_get_field_descriptor` (`jira_client.py:483`) fetches `GET /rest/api/3/field` once per client
    and caches it in `self._field_cache` (`jira_client.py:170`). It matches the display name
    case-insensitively.
  - It raises `JiraClientError("no Jira field named ...")` when nothing matches. When several
    fields share the name, it raises an error that lists their ids.
  - `resolve_custom_field` and `custom_field_kind` (`jira_client.py:443`, `:473`) use it for
    `jira_update_issue`'s `custom_fields`. Those take display names only, never ids.
- `JiraIssue` (`jira_client.py:107`) has a non-dataclass attribute, `display_description = None`, so
  `asdict()` never sends it to the agent. This plan uses the same pattern for `extra_fields`.
- The connector (`src/privacyfence/connectors/jira.py`) has 10 tools. Registering a new tool
  touches these places:
  - **Gates:** `src/privacyfence/auto_accept.py`, in `TOOL_TO_OPERATION` (around line 136) and
    `TOOL_TO_GATE` (around line 269).
  - **Verb:** a review tool also needs an entry in `TOOL_TO_VERB`
    (`src/privacyfence/policy/registry.py:195`).
  - **Card layout:** a review tool also needs an entry in `gate._TOOL_LAYOUT`
    (`src/privacyfence/gate.py:253`).
  - **Registry test:** `tests/unit/policy/test_registry.py` requires `TOOL_TO_VERB` to cover exactly
    the tools in `TOOL_TO_OPERATION`. It needs only a verb, a scope subject and a family.
    `Verb.SEARCH` maps to `EVERY_RESULT` (`registry.py:97`, `:118`).
- `propose.proposals_for` returns `[]` for a verb that no scope covers, and then the "Always allow"
  button is not shown (`src/privacyfence/policy/propose.py:543-560`). No test requires a review tool
  to have a scope; `apps_script_get_content` is an existing review tool without one.
- Generated docs and the tests that check them:
  - `docs/tools-reference.md` comes from `python3 scripts/generate_tools_reference.py`.
    `tests/unit/test_docs_tools_reference.py` fails when it is stale. Only the first sentence of a
    tool's description goes into it.
  - `docs/always-allow-rules-reference.md` comes from
    `python3 scripts/generate_always_allow_reference.py`, which writes one row per review tool.
    `tests/unit/test_generate_always_allow_reference.py` fails when it is stale.
- Tests that cover every tool:
  - Every `gate="review"` call site must pass `pii_scan_text`, and every gated tool must declare a
    required `reason` param (`tests/unit/test_systemic_gate_invariants.py`).
  - Descriptions are checked by `TestToolDefinitions`. The tools each description names are checked
    against `JIRA_SIBLINGS` (`tests/unit/connectors/test_jira_connector.py:89`).
  - `TestEveryToolIsAudited` (`test_jira_connector.py:1354`) calls every tool with stub args
    (`"stub"` for strings) plus per-tool `arg_overrides`.
- The pattern for a reviewed search card is `SalesforceConnector._search`
  (`src/privacyfence/connectors/salesforce.py:487`). It uses `preview`, `new_info`, `details_text`,
  `preview_tables` and `table_only=True`, all keyword arguments of `gated_call` (`gate.py:819-913`).
- Website copy that counts or describes tools:
  - `website/connectors/index.html:29` says "118 tools". Its Jira card is
    `data-tools="10" data-auto="5" data-review="1" data-popup="4"`.
    `tests/unit/test_website_connectors_page.py` computes the expected numbers from the tools
    reference.
  - `website/how-it-works/index.html:54` says "Each of the 118 connector tools".
  - `website/connectors/jira-confluence/index.html:43-44` lists the same gate texts as the Jira card.
    Its line 65 lists what the PII scan reads.
- `scripts/qa_fixture_recorder.py` `check_jira` (`:860-930`) is what `connector-live-check.yml`
  runs. It calls `list_projects`, `get_issue` and `search_issues_page(page_jql, 1)`. It never reads
  the field list.
- OAuth scopes do not change. `GET /rest/api/3/field` and the search are covered by
  `read:jira-work`, which `jira_update_issue`'s custom fields already use.

## Design

### D1. Gates

**`jira_search_issues_with_fields` is a separate tool with gate `review`.** `jira_search_issues` is
auto-approved because it returns metadata only; description and comments are held back for the
reviewed `jira_get_issue`. A custom field can hold anything, such as a multi-line "Acceptance
criteria", a customer name or a free-text "Root cause". The user sees every value before the agent
does.

Rejected alternatives:

- **Extra fields on the auto-approved search.** Content would skip review, which breaks the rule
  that only metadata runs without a card.
- **Auto-approved, but refusing free-text field types.** Judging content by schema type is brittle.
  Single-line text, labels and option values can carry content too, and an admin can add a field
  type the allowlist does not know.
- **One tool whose gate depends on its arguments.** A tool has exactly one gate in `TOOL_TO_GATE`.
  The tools reference, the `privacyfence_check_policy` preflight and the gate invariant tests all
  rely on that.

**The new operation has no scope.** It is `jira.search_issues_with_fields`, with verb
`Verb.SEARCH`. The Jira scope catalogue (`policy/propose.py:332`) covers `READ`, `CREATE`,
`COMMENT`, `UPDATE` and `TRANSITION`, not `SEARCH`, and a JQL query names no single project. So no
standing rule can auto-accept the search, and every call shows a card. Do not change `propose.py`,
`scopes.py` or `resource_registry.py`.

**`jira_get_issue` keeps operation `jira.read_issue`, with or without `fields`.** Its existing
project, reporter and assignee rules still auto-accept it. Asking for more fields of the same issue
does not change whose issue it is.

**`jira_list_fields` is auto-approved.** Field names, ids, types and JQL names are the site's
schema, not issue content. `GET /rest/api/3/field` returns no values, emails or account ids. ADR
0119 made the same call for people lookups.

### D2. Client: `src/privacyfence/jira_client.py`

**New dataclass**, after `JiraTransition`:

```python
@dataclass
class JiraField:
    id: str            # "customfield_10016", "duedate"
    name: str          # "Story Points", "Due date"
    custom: bool
    type: str          # schema type: "number", "option", "array of user"; "" when Jira gives none
    jql_names: list[str] = field(default_factory=list)   # Jira's clauseNames, e.g. ["cf[10016]", "Story Points"]
```

**New attribute on `JiraIssue`**, next to `display_description`:

```python
    # Not a dataclass field either: the requested extra fields, {key: simplified value}, set only by a
    # search or get_issue that asked for them (keys from JiraClient.extra_field_keys). None otherwise.
    extra_fields = None
```

**New module constants**, after `MAX_PAGES`. Also add `import re` at the top.

```python
# The fields _parse_issue reads for a search result; description is left out on purpose.
SEARCH_FIELDS = ("summary", "status", "issuetype", "priority", "assignee", "reporter", "labels", "created", "updated")
# Keys a simplified field value never carries: links back into the API, avatars, and email addresses.
_DROPPED_VALUE_KEYS = frozenset({"self", "_links", "avatarUrls", "iconUrl", "emailAddress", "expand"})
# A Jira Service Management SLA field: it has a name, but its data is in the cycles, so it is not collapsed to the name.
_SLA_KEYS = frozenset({"ongoingCycle", "completedCycles"})
_CF_JQL_RE = re.compile(r"cf\[(\d+)\]", re.IGNORECASE)
```

**New module function `simplify_field_value(value: Any, names: Mapping[str, str] | None = None) -> Any`.**
It applies itself recursively, passing `names` down, and makes these checks in order:

1. `None`, `str`, `int`, `float` or `bool`: returned unchanged.
2. A `list`: `[simplify_field_value(v, names) for v in value]`.
3. A `dict` with `value.get("type") == "doc"` (rich text in Atlassian Document Format):
   `JiraClient._extract_adf_text(value, names)`. A mention becomes `@[Name](accountId)`, as in
   descriptions.
4. A `dict` with `"accountId"`: `mask_emails(value.get("displayName") or "", value["accountId"]) or UNKNOWN_USER_LABEL`.
   A person is shown by display name, never by email or id.
5. A `dict` with `"value"` (a select option): `simplify_field_value(value["value"], names)`.
   - If it also has a `"child"` dict with a `"value"` (a cascading select), return
     `f"{simplify_field_value(value['value'], names)} > {simplify_field_value(value['child']['value'], names)}"`.
6. A `dict` with `"name"` and no key from `_SLA_KEYS` (status, priority, version, component, sprint,
   group, team): `simplify_field_value(value["name"], names)`.
7. A `dict` with `"key"` (parent, subtask): `value["key"]`.
8. Any other `dict`: `{k: simplify_field_value(v, names) for k, v in value.items() if k not in _DROPPED_VALUE_KEYS}`.
   - SLA fields end up here.
   - So do issue links, for example `{"id": "1", "type": "Blocks", "outwardIssue": "ENG-2"}`.
   - An issue link loses its "blocks / is blocked by" wording. That is accepted; do not add special
     handling.
9. Anything else: `str(value)`.

**New `JiraClient` methods:**

- **`_all_fields(self) -> list[dict[str, Any]]`.**
  - Move the fetch-and-cache block from the top of `_get_field_descriptor` here, unchanged,
    including `JiraClientError(f"failed to list Jira fields: {exc}")`.
  - `_get_field_descriptor` then calls `_all_fields`.
  - Change only `_get_field_descriptor`'s **not-found** message, to
    `f"no Jira field named {field_name!r}; jira_list_fields lists the field names"`.
  - Keep its duplicate-name message unchanged: `jira_update_issue` cannot take ids.
- **`@staticmethod _to_jira_field(raw: dict[str, Any]) -> JiraField`.**
  - `id=raw.get("id", "")`, `name=raw.get("name", "")`, `custom=bool(raw.get("custom"))`.
  - `type` comes from `raw["schema"]`:
    - `schema["type"]`;
    - `f"array of {schema['items']}"` when the type is `"array"` and `items` is set;
    - `""` when there is no schema.
  - `jql_names=list(raw.get("clauseNames") or [])`.
- **`list_fields(self, query: str = "", custom_only: bool = False, max_results: int = 50) -> list[JiraField]`.**
  1. Clamp `max_results` to 1..200.
  2. Keep the fields whose name or id contains `query.strip().lower()`, ignoring case. An empty
     query keeps all fields.
  3. When `custom_only` is true, drop the fields that are not custom.
  4. Sort by `name.lower()`, then by `id`, and truncate to `max_results`.
  5. Log `"list_fields returned %d field(s)"`.
- **`resolve_fields(self, refs: list[str]) -> list[JiraField]`.** Returns the fields in the order
  given, with duplicate ids removed (the first one wins). Each ref is first stripped
  (`ref = ref.strip()`), then:
  - Empty: raise `JiraClientError("field names must not be empty")`.
  - Matches `_CF_JQL_RE.fullmatch(ref)`: look up id `f"customfield_{n}"`. If that id is not found,
    raise `JiraClientError(f"no Jira field with id {ref!r}; jira_list_fields lists the field names")`.
  - Equals the `id` of an entry in `_all_fields()` exactly: use that entry.
  - Otherwise match by name over `_all_fields()`, ignoring case:
    - no match: raise the same not-found message as `_get_field_descriptor`;
    - more than one match: raise
      `JiraClientError(f"multiple Jira fields are named {ref!r} ({ids}); pass one of these ids instead")`,
      where `ids` is the ids joined with `", "`.
- **`@staticmethod extra_field_keys(wanted: list[JiraField]) -> list[str]`.** Each field's `name`.
  When two fields in `wanted` share a name (different ids, both asked for by id), both are keyed
  `f"{f.name} ({f.id})"` instead.
- **`_extra_fields(self, raw_fields: Mapping[str, Any], wanted: list[JiraField]) -> dict[str, Any]`.**
  1. Collect the mention ids from every requested value that is an ADF doc, using
     `self._collect_adf_mention_ids`. Look inside lists at any depth.
  2. `names = self.resolve_user_names(ids) if ids else None`.
  3. Return `{k: simplify_field_value(raw_fields.get(f.id), names) for k, f in zip(self.extra_field_keys(wanted), wanted, strict=True)}`.

**Changed `JiraClient` methods:**

- **`search_issues_page(self, jql, page_size=100, page_token=None, extra: list[JiraField] | None = None)`.**
  - Pass `fields=[*SEARCH_FIELDS, *(f.id for f in extra or [] if f.id not in SEARCH_FIELDS)]` to
    `enhanced_jql`.
  - When `extra` is not None, set
    `issue.extra_fields = self._extra_fields(raw.get("fields") or {}, extra)` on each issue after
    `_parse_issue`.
- **`search_issues(self, jql, max_results=20, fields: list[str] | None = None)`.**
  - When `fields` is given, call `extra = self.resolve_fields(fields)` before the first page and
    pass `extra` to every `search_issues_page` call.
  - A resolution error is raised as is, not wrapped in `search_issues failed:`.
- **`get_issue(self, issue_key, fields: list[str] | None = None)`.**
  - When `fields` is given, resolve them before the API call. After parsing, set
    `issue.extra_fields = self._extra_fields(raw.get("fields") or {}, extra)`.
  - The API call does not change, because `*all` already includes the fields.
  - Every existing caller passes only the key and is unaffected.

### D3. Connector: `src/privacyfence/connectors/jira.py`

**Module level**, after `_parse_json_object`:

- `MAX_EXTRA_FIELDS = 20`.
- **`_parse_field_refs(value: str, tool: str, *, required: bool) -> list[str]`:**
  - `value` empty or whitespace: return `[]` when not `required`; otherwise raise
    `ValueError(f"{tool}: fields must name at least one field; use jira_search_issues for the standard fields.")`.
  - `json.loads` fails, or the result is not a list of strings: raise
    `ValueError(f'{tool}: fields must be a JSON array of field names, e.g. ["Story Points", "Sprint"].')`.
  - An empty list: the same "at least one field" error when `required`, otherwise `[]`.
  - More than `MAX_EXTRA_FIELDS` names: raise
    `ValueError(f"{tool}: fields can name at most {MAX_EXTRA_FIELDS} fields.")`.
- **`_field_cell(value: Any) -> str`:**
  - `None` → `""`.
  - `str` → itself.
  - `list` → `", ".join(_field_cell(v) for v in value)`.
  - `bool` and `dict` → `json.dumps(value, ensure_ascii=False)`, so a boolean reads `true` or
    `false`, the same as inside a dict.
  - Anything else → `str(value)`.

**Description texts.** These are written as Python literals. Inline each one verbatim in its
`ToolSpec(description=(...))`, the way the file already does. The constant names are only labels
for this plan.

```python
LIST_FIELDS_DESCRIPTION = (
    "List the Jira site's fields, built-in and custom, with the id and JQL names behind each "
    "display name. Returns a list of {id, name, custom, type, jql_names}, sorted by name, at most "
    "max_results (default 50, capped at 200). Pass a name in the fields parameter of "
    "jira_search_issues_with_fields or jira_get_issue; in a JQL query for jira_search_issues use one "
    "of jql_names, e.g. cf[10016] > 3 or \"Story Points\" > 3. Auto-approved."
)
SEARCH_WITH_FIELDS_DESCRIPTION = (
    "Search Jira issues using JQL and add extra fields to each one: custom fields such as Story "
    "Points or Sprint, or built-in ones such as due date or fix versions. Returns the list "
    "jira_search_issues returns, each issue with an added fields object mapping each requested "
    "field's Jira name to its value (people as display names, options and statuses as their names, "
    "rich text as plain text with mentions as @[Name](accountId), an empty field as null), at most "
    "max_results (default 20, capped at 100), in the order the JQL gives. Find field names with "
    "jira_list_fields. Use jira_search_issues instead when its standard fields are enough: it needs "
    "no approval. Requires user approval."
)
GET_ISSUE_DESCRIPTION = (
    "Fetch full details of a Jira issue by key (e.g. PROJ-123), "
    "including description and comments. Returns the issue as {key, summary, "
    "status, issue_type, priority, assignee, reporter, description, labels, "
    "created, updated, url, comments: a list of {id, author, body, created, "
    "updated}}; with fields, it also has a fields object mapping each requested field's Jira name "
    "to its value, as jira_search_issues_with_fields returns it. Use jira_search_issues instead to "
    "find issues without reading them in full. Mentions appear as @[Name](accountId). Requires user "
    "approval."
)
```

**New tool `jira_list_fields`** (auto, `read_only=True`), placed after `jira_get_transitions`.

- Description: `LIST_FIELDS_DESCRIPTION`.
- Params:
  - `query` (`str`, not required, default `""`): `"Part of a field name or id, case-insensitive, e.g. 'story' or 'customfield_100'. Empty lists every field."`
  - `custom_only` (`bool`, not required, default `False`): `"True to list only custom fields. Default false lists built-in fields too."`
  - `max_results` (`int`, not required, default 50): `"Maximum number of fields to return. Default 50, capped at 200."`
  - `reason`: the standard required reason param.
- `_list_fields(self, query: str = "", custom_only: bool = False, max_results: int = 50)`:
  1. Fetch the fields through the client.
  2. Return `[asdict(f) for f in fields]`.
  3. Audit with
     `self._auto_audit("jira_list_fields", "List Jira Fields", "List fields", f"{len(fields)} field(s)", t0)`.
     The query text does not go into the audit summary.

**New tool `jira_search_issues_with_fields`** (review, `read_only=True`), placed after
`jira_search_issues`.

- Description: `SEARCH_WITH_FIELDS_DESCRIPTION`.
- Params:
  - `jql`: the same as in `jira_search_issues`.
  - `fields` (`str`, required): `"JSON array of the fields to add, by the name shown in Jira (case-insensitive), by id (customfield_10016, duedate) or by JQL id (cf[10016]), e.g. [\"Story Points\", \"Sprint\"]. 1 to 20 fields; jira_list_fields lists them."`
  - `max_results` (`int`, default 20): `"Maximum number of issues to return. Default 20, capped at 100."`
  - `reason`: the standard required reason param.
- `_search_issues_with_fields(self, jql: str, fields: str, max_results: int = 20)`:
  1. Call `refs = _parse_field_refs(fields, "jira_search_issues_with_fields", required=True)` before
     any API call. Then set `max_results = max(1, min(max_results, 100))`.
  2. Call `wanted = await self._fetch(self._jira.resolve_fields, refs)`, then
     `names = JiraClient.extra_field_keys(wanted)`. This way the card names the fields even when
     there are no results.
  3. Call `issues = await self._fetch(self._jira.search_issues, jql, max_results, refs)`. The client
     resolves the fields again, from its cached field list.
  4. `result = [{**asdict(i), "fields": i.extra_fields or {}} for i in issues]`.
  5. `rows = [[i.key, i.summary, *(_field_cell((i.extra_fields or {}).get(n)) for n in names)] for i in issues]`.
  6. Call `gated_call` as below and return its result.

     ```python
     gated_call(
         connector=self.name,
         tool="jira_search_issues_with_fields",
         tool_name="Search Jira Issues with Fields",
         summary=f"Search: {jql[:80]}",
         sender="Jira",
         raw_data=issues,
         filtered_data=result,
         gate="review",
         preview={"JQL": jql, "Fields": ", ".join(names)},
         new_info={"Results": str(len(issues)), "Field values": f"{', '.join(names)} for each issue"},
         details_text="\n".join(
             f"{r[0]} — {r[1]}: " + "; ".join(f"{n}: {c}" for n, c in zip(names, r[2:], strict=True))
             for r in rows
         ) or "(no matches)",
         pii_scan_text="\n".join(" ".join(r[2:]) for r in rows),
         preview_tables=[{"headers": ["Key", "Summary", *names], "rows": rows}] if issues else [],
         table_only=True,
         my_email=self.my_email,
         args={"jql": jql, "fields": fields},
     )
     ```

**`jira_get_issue` gains `fields`** (this is phase p3).

- New param after `issue_key`: `fields` (`str`, not required, default `""`):
  `"Optional JSON array of extra fields to return, as in jira_search_issues_with_fields, e.g. [\"Story Points\"]. Empty returns only the standard fields."`
- Description: `GET_ISSUE_DESCRIPTION`, which is the current text with a clause inserted after the
  return shape. The first sentence does not change, so the tools reference does not change.
- `_get_issue(self, issue_key: str, fields: str = "")`:
  1. `refs = _parse_field_refs(fields, "jira_get_issue", required=False)`.
  2. Fetch the issue:
     - when `refs` is empty, `issue = await self._fetch(self._jira.get_issue, issue_key)`, exactly as
       today;
     - otherwise `issue = await self._fetch(self._jira.get_issue, issue_key, refs)`.
  3. When `refs` is non-empty:
     - set `result["fields"] = issue.extra_fields or {}`;
     - set `new_info["Requested fields"] = ", ".join(issue.extra_fields or {})`;
     - add the block
       `{"type": "table", "caption": "Requested fields", "headers": ["Field", "Value"], "rows": [[n, _field_cell(v)] for n, v in (issue.extra_fields or {}).items()]}`
       after the Description text block and before the Comments table;
     - append `"\n" + "\n".join(_field_cell(v) for v in (issue.extra_fields or {}).values())` to
       `pii_scan_text`;
     - set `args` to `{"issue_key": issue_key, "fields": fields}`.
  4. Without `fields`, everything stays exactly as today: the same `args`, blocks and result keys.
- `call()` already passes `**args`, so it needs no change for `fields`.

### D4. Gate and policy wiring (phase p2)

- `src/privacyfence/auto_accept.py`:
  - `TOOL_TO_OPERATION["jira_search_issues_with_fields"] = "jira.search_issues_with_fields"`, after
    `jira_get_issue`.
  - `TOOL_TO_GATE` gains `"jira_list_fields": "auto"` after `jira_get_transitions`, and
    `"jira_search_issues_with_fields": "review"` after `jira_get_issue`.
- `src/privacyfence/policy/registry.py`: `TOOL_TO_VERB["jira_search_issues_with_fields"] = Verb.SEARCH`,
  after `jira_get_issue`.
- `src/privacyfence/gate.py`: `_TOOL_LAYOUT["jira_search_issues_with_fields"] = WIDE`, next to
  `"jira_get_issue": WIDE`.
- `call()` gains the two dispatch lines.
- No change to `propose.py`, `scopes.py`, `resource_registry.py` or `write_effects.py` (D1).
- Regenerate `docs/tools-reference.md` with `python3 scripts/generate_tools_reference.py`.
- Regenerate `docs/always-allow-rules-reference.md` with
  `python3 scripts/generate_always_allow_reference.py`. The new row's cell is empty, like
  `apps_script_get_content`'s.

### D5. Website, docs and changelog

- **`website/connectors/index.html`:**
  - Line 29 becomes "Eleven connectors, 120 tools, each with a fixed gate."
  - The Jira card becomes `data-tools="12" data-auto="6" data-review="2" data-popup="4"`, with the
    text "12 tools: 6 without a card · 2 reviewed · 4 need approval".
  - Its "Without a card" `<dd>` becomes "Projects, issue search with JQL, an issue's available
    transitions, the site's field names, and looking up people's account ids."
  - Its "Reviewed before release" `<dd>` becomes "An issue's full details, including its
    description and comments, and issue searches that return extra fields."
- **`website/how-it-works/index.html:54`:** "Each of the 118 connector tools" becomes "Each of the
  120 connector tools". This and `connectors/index.html:29` are the only two counts written in prose.
- **`website/connectors/jira-confluence/index.html`:**
  - The two `<dd>` texts at lines 43-44 become the same new texts as on the Jira card above.
  - At line 65, "It scans a Jira issue's description and comments, a Confluence page's body," becomes
    "It scans a Jira issue's description, comments and any extra fields requested, a Confluence
    page's body,".
- **`docs/atlassian-setup.md`:** a new section `## Fields`, after `## Names and mentions`, reading:
  "`jira_list_fields` lists the site's fields with their ids and JQL names, so the agent can use
  the names you see in Jira. `jira_search_issues` returns a fixed set of fields without asking. To
  read other fields, custom fields included, the agent uses `jira_search_issues_with_fields` or the
  `fields` parameter of `jira_get_issue`, and you review the values on the approval card first. No
  extra scope is needed."
- **`CHANGELOG.md`**, as the last bullet under `## [Unreleased]` → `### Added`:
  "- **Jira fields.** `jira_list_fields` finds a field's id and JQL name from the name shown in Jira.
  The new `jira_search_issues_with_fields`, and a new `fields` parameter on `jira_get_issue`, return
  custom and other extra fields after you review them. `jira_search_issues` is unchanged and now
  downloads only the fields it returns."

### D6. Live check: `scripts/qa_fixture_recorder.py` (phase p1)

Two changes to `check_jira`:

- The existing `search_issues_page` step becomes
  `client.search_issues_page(page_jql, 1, extra=client.resolve_fields(["duedate"]))`. `duedate` is a
  system field id present on every site, so the check does not depend on the site's language.
- A new `list_fields` step follows it:

  ```python
      try:
          fields = client.list_fields(max_results=5)
          results.append(CheckResult("jira", "list_fields", project_key, bool(fields),
                                     f"{len(fields)} field(s)" if fields else "no fields returned"))
      except JiraClientError as exc:
          results.append(CheckResult("jira", "list_fields", project_key, False, str(exc)))
  ```

Nothing new is recorded.

## ADRs

- **ADR 0133**, "Extra Jira fields are read through a separate reviewed search with no scope",
  records:
  - D1's decision and its three rejected alternatives;
  - that no standing rule can auto-accept the new search;
  - that `jira_get_issue` with `fields` keeps `jira.read_issue`, so the existing project, reporter
    and assignee rules still auto-accept it.
- **ADR 0134**, "The Jira field list is auto-approved": field schema is metadata and holds no
  values, emails or account ids. It sits next to ADR 0119.
- If either number is taken on `main` when the phase runs, use the next free numbers.

## Manual steps

There are no steps before implementation.

There is one step after implementation, **ma1**, described on
[the manual steps page](https://claude.ai/artifact/2ntM6TqCwcb5wKwEkfUpyB). On your real Jira site,
through an MCP client:

1. List the fields.
2. Run a search with two custom fields.
3. Read one issue with `fields`.
4. Check the approval cards.

`connector-live-check.yml` runs the field list, and a search with one system extra field, against
the QA account (D6). Only a person can check the cards and a production site's own custom fields.

## Risks and open questions

- **`JiraIssue.extra_fields` is a class attribute.** It is `None` on the class and assigned per
  instance, like `display_description`. `asdict()` must not carry it; a p1 test asserts that.
- **Mention resolution in extra fields costs a user lookup.** It happens only when a requested
  rich-text value contains a mention, and it goes through the existing, cached
  `resolve_user_names`. If a p1 test shows `resolve_user_names` called for a search with no
  mentions, that is a bug in `_extra_fields`, not something to accept.
- **Checked, with no action needed:**
  - no test requires a review tool to have a scope;
  - the website counts in D5 match what `tests/unit/test_website_connectors_page.py` computes;
  - `TestLiveFixtureParsing` never calls `enhanced_jql`.

## Implementation manifest

```yaml
plan_slug: jira-extra-fields
feature_branch: feature/jira-extra-fields
max_parallel: 1
manual_steps_artifact: https://claude.ai/artifact/2ntM6TqCwcb5wKwEkfUpyB
manual_steps_source: docs/jira-extra-fields-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-real-site-cards
    title: On your real Jira site, list fields, search with two custom fields, read one issue with fields, and check the cards
    why: the review card's table and a production site's own custom fields, which the QA account and unit tests cannot show
verify_after_merge:
  - ruff check .
  - python3 -m pytest tests/unit -q
final_checks:
  - docs/jira-extra-fields-plan.md and docs/jira-extra-fields-plan-manual-steps.html are deleted and nothing links to them (grep -rn "jira-extra-fields-plan" --exclude-dir=.git . finds nothing)
  - docs/adr/0133-*.md and docs/adr/0134-*.md (or the next free numbers) exist, are Accepted, and are listed in docs/adr/README.md
  - CHANGELOG.md has the Jira fields entry under ## [Unreleased] and no new version heading
  - grep -n '"jira_search_issues_with_fields":' src/privacyfence/auto_accept.py finds two lines (operation and gate)
  - python3 -m pytest tests/integration -q passes
  - connector-live-check.yml has been dispatched on feature/jira-extra-fields after the last code phase merged, its list_fields and search_issues_page rows pass, and the PR body links the run
phases:
  - id: p1-client-fields
    title: JiraClient field lookup, value simplification, extra fields on search and get_issue, and the live check
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/jira_client.py
      - tests/unit/test_jira_client.py
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
    brief: |
      Implement Design D2 and D6 of docs/jira-extra-fields-plan.md, with names, signatures and
      error texts exactly as written there. Must-read first: CLAUDE.md, CONTRIBUTING.md,
      docs/coding-and-testing-guidelines.md, docs/testing-policy.md.
      1. In src/privacyfence/jira_client.py, add `import re`. Add SEARCH_FIELDS,
         _DROPPED_VALUE_KEYS, _SLA_KEYS and _CF_JQL_RE after MAX_PAGES.
      2. Add the JiraField dataclass after JiraTransition. Add the `extra_fields = None` class
         attribute, with its comment, on JiraIssue next to display_description.
      3. Add the module function simplify_field_value after _text_to_adf, with the nine checks in
         D2's order. It calls JiraClient._extract_adf_text, which is defined later in the module;
         the name resolves at call time.
      4. In JiraClient:
         - add _all_fields, moving the cache-filling block out of _get_field_descriptor into it;
         - change only _get_field_descriptor's not-found text;
         - add _to_jira_field, list_fields, resolve_fields (with its own duplicate-name text),
           extra_field_keys and _extra_fields.
      5. Change search_issues_page, search_issues and get_issue as D2 says. Keep the existing
         `search_issues failed:` wrapping for API errors. A resolve_fields error propagates as is.
      6. Tests in tests/unit/test_jira_client.py, in the MagicMock style of TestSearchIssuesPage
         and TestResolveCustomField:
         - TestSearchIssuesPage:
           - the existing assert_called_once_with gains fields=list(SEARCH_FIELDS);
           - new test: extra fields are appended to fields= without repeating a SEARCH_FIELDS
             entry, and issue.extra_fields is set on each issue.
         - TestSearchIssues:
           - in test_pages_with_next_page_token_and_truncates, both expected kwargs dicts gain
             "fields": list(SEARCH_FIELDS); nothing else in its assertions changes;
           - new test: fields=["Story Points"] resolves once (get_all_fields is called once) and
             every page call passes the same fields list;
           - new test: an unknown name raises JiraClientError whose text contains
             "jira_list_fields" and does not contain "search_issues failed".
         - New class TestSimplifyFieldValue, one test per case:
           - scalars are returned unchanged;
           - a list is simplified item by item;
           - an ADF doc with a mention gives "@[Ann](a1)" when names={"a1": "Ann"};
           - a person gives its displayName; an email-shaped displayName is masked as mask_emails
             does; a person with no displayName gives UNKNOWN_USER_LABEL;
           - {"id": "1", "value": "High"} gives "High";
           - {"value": "A", "child": {"value": "B"}} gives "A > B";
           - {"value": {"accountId": "a1", "displayName": "Ann", "emailAddress": "x@y.z"}} gives "Ann";
           - {"name": "Sprint 4", "id": 7} gives "Sprint 4";
           - the SLA value {"id": "1", "name": "Time to resolution", "ongoingCycle": {"breached": False}, "_links": {"self": "u"}}
             gives {"id": "1", "name": "Time to resolution", "ongoingCycle": {"breached": False}};
           - {"key": "ENG-2", "fields": {...}} gives "ENG-2";
           - a generic dict drops self, _links, avatarUrls, iconUrl, emailAddress and expand at any
             depth;
           - {"comments": [{"self": "u", "id": "1", "author": {"accountId": "a1", "displayName": "Ann"}, "body": <ADF doc with text "hi">}], "total": 1}
             gives {"comments": [{"id": "1", "author": "Ann", "body": "hi"}], "total": 1};
           - the issue link {"id": "1", "type": {"name": "Blocks"}, "outwardIssue": {"key": "ENG-2"}}
             gives {"id": "1", "type": "Blocks", "outwardIssue": "ENG-2"}.
         - New class TestListFields:
           - query matches the name or the id, ignoring case;
           - custom_only drops the fields that are not custom;
           - results are sorted by name;
           - max_results is clamped to 1..200;
           - the type strings are "number", "array of user", and "" without a schema;
           - jql_names comes from clauseNames;
           - get_all_fields is called once across list_fields and resolve_fields.
         - New class TestResolveFields:
           - resolves by name (ignoring case), by exact id, by cf[10016] and by CF[10016];
           - the unknown-cf-id error text;
           - the empty-ref error;
           - duplicate refs are removed and the order is kept;
           - two fields named "Sprint", asked for by name, raise the "pass one of these ids
             instead" error;
           - extra_field_keys keys two same-named fields as "Sprint (customfield_1)" and
             "Sprint (customfield_2)", and leaves unique names alone.
         - TestGetIssue:
           - get_issue("ENG-1", ["Story Points"]) sets extra_fields to {"Story Points": 5};
           - get_issue("ENG-1") leaves extra_fields as None;
           - asdict(issue) never has an extra_fields key;
           - a rich-text extra field with a mention calls resolve_user_names once and renders
             "@[Name](id)";
           - a search whose extra values have no mentions never calls the user lookup.
         - Update test_unknown_field_name_raises for the new not-found text.
           test_ambiguous_field_name_raises stays unchanged.
      7. Implement D6 in scripts/qa_fixture_recorder.py. In tests/unit/test_qa_fixture_recorder.py,
         class TestCheckJira, in the style of test_search_issues_page_called_with_fallback_jql:
         - that test also asserts that "duedate" is in the fields= kwarg;
         - the list_fields row is ok when get_all_fields returns one field;
         - the row is not ok when it returns [];
         - the row is not ok when get_all_fields raises.
      8. Run ruff check . and python3 -m pytest tests/unit/test_jira_client.py tests/unit/test_qa_fixture_recorder.py -q.
      Stop condition: if any test outside these two test files fails because of the explicit
      fields= list (for example a plugin test of jira.search that asserts enhanced_jql's call),
      stop with status=blocked and quote the failure. Do not edit tests outside touches.
    acceptance:
      - python3 -m pytest tests/unit/test_jira_client.py tests/unit/test_qa_fixture_recorder.py -q passes
      - python3 -m pytest tests/unit/test_jira_client.py -q --collect-only -k "SimplifyFieldValue or ListFields or ResolveFields" | tail -1 reports at least 25 tests
      - python3 -m pytest tests/unit -q passes
      - grep -n "^SEARCH_FIELDS = " src/privacyfence/jira_client.py matches
      - grep -c "def resolve_fields\|def list_fields\|def simplify_field_value\|def extra_field_keys" src/privacyfence/jira_client.py prints 4
      - grep -n "list_fields" scripts/qa_fixture_recorder.py matches
      - ruff check . passes

  - id: p2-search-with-fields
    title: jira_list_fields and jira_search_issues_with_fields, gate wiring, generated references and website copy
    depends_on: [p1-client-fields]
    complexity: M
    touches:
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - tests/unit/connectors/test_jira_connector.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - website/connectors/jira-confluence/index.html
    brief: |
      Implement the jira_list_fields and jira_search_issues_with_fields parts of Design D3, all of
      D4, and the website parts of D5 of docs/jira-extra-fields-plan.md, with every name,
      description string and error text exactly as written there. Do not touch jira_get_issue in
      this phase; that is p3. The review card follows SalesforceConnector._search in
      src/privacyfence/connectors/salesforce.py. Must-read first: CLAUDE.md, CONTRIBUTING.md,
      docs/coding-and-testing-guidelines.md, docs/testing-policy.md.
      1. In src/privacyfence/connectors/jira.py:
         - add MAX_EXTRA_FIELDS, _parse_field_refs and _field_cell at module level, after
           _parse_json_object;
         - add the two ToolSpecs where D3 places them;
         - add the two dispatch lines in call();
         - add _list_fields in the "Auto" section and _search_issues_with_fields in the
           "Review gate (reads)" section.
      2. Wire the tools per D4 in src/privacyfence/auto_accept.py,
         src/privacyfence/policy/registry.py and src/privacyfence/gate.py. Change nothing else in
         policy/.
      3. Tests in tests/unit/connectors/test_jira_connector.py, using the existing fixtures
         (gated_call_spy, make_connector) and the style of TestAutoTools, TestFindUsers and
         TestGetIssue:
         - New class TestParseFieldRefs:
           - each ValueError text from D3: bad JSON, a non-list, a list holding a non-string, an
             empty list when required, and 21 names;
           - "" when not required gives [];
           - 20 names pass.
         - New class TestFieldCell: None, a str, a list, a nested list, True giving "true", and a
           dict giving JSON.
         - New class TestListFields:
           - the result is the asdict list;
           - query, custom_only and max_results are passed through to the client;
           - the audit entry's summary is "List fields" and does not contain the query.
         - New class TestSearchIssuesWithFields:
           - bad fields mean the client is never called;
           - gated_call gets gate="review" and the exact preview and new_info;
           - preview_tables has headers ["Key", "Summary", *names] and cells from _field_cell;
           - pii_scan_text holds the cell values and not the summaries;
           - args is {"jql": ..., "fields": ...} and table_only=True;
           - each result item carries "fields";
           - with zero results, the preview still names the fields and preview_tables=[];
           - max_results 500 reaches the client call as 100.
         - JIRA_SIBLINGS gains "jira_search_issues_with_fields": ("jira_search_issues", "jira_list_fields")
           and "jira_list_fields": ("jira_search_issues_with_fields",).
         - TestEveryToolIsAudited:
           - arg_overrides gains "jira_search_issues_with_fields": {"fields": '["Story Points"]'};
           - set client.resolve_fields.return_value to one JiraField and
             client.search_issues.return_value to one make_issue() with extra_fields set, so the
             call reaches the gate;
           - leave the fields param required.
         - TestDispatch: both new names dispatch.
      4. Run python3 scripts/generate_tools_reference.py and
         python3 scripts/generate_always_allow_reference.py. Commit both regenerated docs.
      5. Update website/connectors/index.html, website/how-it-works/index.html and
         website/connectors/jira-confluence/index.html with the exact texts in D5.
      6. Run ruff check ., python3 -m pytest tests/unit -q and python3 -m pytest tests/integration -q.
      Stop condition: if any test in tests/unit/policy fails for jira_search_issues_with_fields
      because it has no scope, stop with status=blocked and quote the assertion. D1 leaves it
      without a scope on purpose, and adding one is the user's decision.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_jira_connector.py -q passes
      - python3 -m pytest tests/unit/connectors/test_jira_connector.py -q --collect-only -k "ParseFieldRefs or FieldCell or ListFields or SearchIssuesWithFields" | tail -1 reports at least 20 tests
      - python3 -m pytest tests/unit/test_generate_always_allow_reference.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_systemic_gate_invariants.py tests/unit/policy -q passes
      - python3 -m pytest tests/unit -q passes
      - python3 -m pytest tests/integration -q passes
      - grep -n "jira_search_issues_with_fields" docs/tools-reference.md docs/always-allow-rules-reference.md matches in both files
      - grep -n 'data-tools="12" data-auto="6" data-review="2" data-popup="4"' website/connectors/index.html matches
      - grep -n "any extra fields requested" website/connectors/jira-confluence/index.html matches
      - ruff check . passes

  - id: p3-get-issue-fields
    title: The fields parameter on jira_get_issue
    depends_on: [p2-search-with-fields]
    complexity: S
    touches:
      - src/privacyfence/connectors/jira.py
      - tests/unit/connectors/test_jira_connector.py
    brief: |
      Implement the "jira_get_issue gains fields" part of Design D3 of
      docs/jira-extra-fields-plan.md exactly as written there, reusing _parse_field_refs and
      _field_cell from p2. Must-read first: CLAUDE.md, docs/coding-and-testing-guidelines.md.
      1. In src/privacyfence/connectors/jira.py:
         - add the fields ToolParam after issue_key in the jira_get_issue ToolSpec;
         - replace its description with the text of GET_ISSUE_DESCRIPTION;
         - extend _get_issue as D3 says. Without fields, the client call stays
           self._fetch(self._jira.get_issue, issue_key), with no second argument.
      2. Tests in tests/unit/connectors/test_jira_connector.py, class TestGetIssue:
         - with fields='["Story Points"]':
           - result["fields"] is set;
           - the "Requested fields" table block sits between the Description text block and the
             Comments table;
           - new_info has "Requested fields";
           - pii_scan_text contains the value;
           - args is {"issue_key": ..., "fields": ...};
           - get_issue was called with (key, ["Story Points"]);
         - without fields:
           - args is exactly {"issue_key": ...};
           - the result has no "fields" key;
           - get_issue was called with (key,) only;
         - fields='not json' raises ValueError with the jira_get_issue text and does not call the
           client.
      3. Run python3 scripts/generate_tools_reference.py and confirm that
         git diff docs/tools-reference.md is empty, because the first sentence did not change.
      4. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_jira_connector.py -q -k GetIssue passes
      - python3 -m pytest tests/unit -q passes
      - git diff --exit-code docs/tools-reference.md succeeds after python3 scripts/generate_tools_reference.py
      - grep -n "with fields, it also has a fields object" src/privacyfence/connectors/jira.py matches
      - ruff check . passes

  - id: p4-retire-plan
    title: ADRs 0133 and 0134, Atlassian setup docs, changelog, and delete the plan
    depends_on: [p3-get-issue-fields]
    complexity: S
    touches:
      - docs/adr/0133-*.md
      - docs/adr/0134-*.md
      - docs/adr/README.md
      - docs/atlassian-setup.md
      - CHANGELOG.md
      - docs/jira-extra-fields-plan.md
      - docs/jira-extra-fields-plan-manual-steps.html
    brief: |
      Retire docs/jira-extra-fields-plan.md. Must-read first: CONTRIBUTING.md ("Decisions, plans
      and ADRs") and docs/adr/README.md (rules and template).
      1. Check the next free ADR numbers with ls docs/adr. They should be 0133 and 0134.
      2. Write docs/adr/0133-extra-jira-fields-go-through-a-reviewed-search.md from the README
         template:
         - Title: "ADR 0133: Extra Jira fields are read through a separate reviewed search with no scope".
         - Status: "Accepted — <today>. Implemented: src/privacyfence/connectors/jira.py,
           src/privacyfence/jira_client.py, src/privacyfence/auto_accept.py."
         - Context, Decision, Alternatives considered and Consequences: from Design D1. Cover the
           three rejected alternatives; Verb.SEARCH with no scope, so every call shows a card; and
           jira_get_issue with fields keeping jira.read_issue, so the existing project, reporter
           and assignee rules still auto-accept it.
         - Verification: TestSearchIssuesWithFields and TestGetIssue in
           tests/unit/connectors/test_jira_connector.py, and the gate rows in auto_accept.py.
         - Related: ADR 0115, ADR 0116.
      3. Write docs/adr/0134-the-jira-field-list-is-auto-approved.md:
         - Title: "ADR 0134: The Jira field list is auto-approved".
         - Status: as for 0133.
         - Content: from D1's jira_list_fields paragraph. Field schema holds no values, emails or
           account ids. The rejected alternative is a review card for each lookup, which would add
           a card showing nothing the search card does not.
         - Verification: TestListFields in tests/unit/connectors/test_jira_connector.py.
         - Related: ADR 0119, ADR 0133.
      4. Link no plan document from either ADR. Add both rows to the index table in
         docs/adr/README.md after 0132, with status Accepted.
      5. Add the "## Fields" section to docs/atlassian-setup.md after "## Names and mentions",
         with the text in Design D5.
      6. Add the CHANGELOG.md bullet from Design D5 as the last bullet under "## [Unreleased]" →
         "### Added". Open no version heading.
      7. Run git rm docs/jira-extra-fields-plan.md docs/jira-extra-fields-plan-manual-steps.html.
         Then grep -rn "jira-extra-fields-plan" --exclude-dir=.git . must find nothing.
      8. Run ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if 0133 or 0134 is already taken on main, take the next free numbers. Use
      them in the file names, titles, cross-links and index rows, and say so in the report.
    acceptance:
      - for f in docs/adr/0133-*.md docs/adr/0134-*.md; do grep -A2 "^## Status" "$f" | grep -q "^Accepted" || echo "FAIL $f"; done prints nothing
      - grep -c "0133-\|0134-" docs/adr/README.md prints 2
      - grep -n "jira_list_fields" docs/atlassian-setup.md CHANGELOG.md matches in both files
      - test ! -e docs/jira-extra-fields-plan.md && test ! -e docs/jira-extra-fields-plan-manual-steps.html
      - grep -rn "jira-extra-fields-plan" --exclude-dir=.git . finds nothing
      - python3 -m pytest tests/unit -q passes
```
