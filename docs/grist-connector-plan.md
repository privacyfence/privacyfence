# Grist connector plan

## 1. Goal

Add a **Grist** connector, so an AI client can list a user's Grist documents, tables and columns,
read records after review, and (with approval on a card) add and update records and add tables and
columns. It works on any Grist server: `docs.getgrist.com`, a team site such as
`https://acme.getgrist.com`, or a self-hosted instance. Grist has no OAuth that a desktop app can
register for, so the user connects by pasting a **personal API key**. This is the first connector
that works that way. It is offered in local mode (a form on **Settings > Connectors**) and in org
mode (an API-key form on `/connect`, offered only when the organization bundle has a `grist`
section, which also pins the server URL). Nothing deletes: no record, column or table delete, no
rename and no type change.

Scope was confirmed with the maintainer when the plan was written: any server URL; records plus
schema writes (add and update records, create tables, add columns); records-only reads (no SQL
tool); local and org mode.

## 2. Current state

- **Connectors** are `Connector` subclasses (`src/privacyfence/connector.py:78-96`;
  `ToolParam`/`ToolSpec` at `:16-75`), discovered automatically by
  `connector_catalog.connector_classes()` (`src/privacyfence/connector_catalog.py:20-29`). The
  closest template for the shape of a connector is `connectors/salesforce.py` (653 lines, four tools,
  all reads): `_fetch` (`:628-632`) wraps the client in `asyncio.to_thread` and re-raises
  `SalesforceClientError` as `RuntimeError`; `_auto_audit` (`:634-653`) audits ungated tools;
  `_get_record` (`:403-447`) is the review-gated read pattern, with `preview`, `new_info`,
  `details_text`, `preview_tables`, `table_only=True` and `args`. Salesforce has **no write
  tools**. The popup write pattern is `connectors/contacts.py:305-342` (`contacts_update`,
  `old → new` previews) and `connectors/jira.py:806-839` (`jira_create_issue`).
- **No connector takes a typed API key or server URL today.** Every per-user credential comes from
  an OAuth browser flow (Google, Slack, Salesforce, Atlassian) or Telegram's phone/code/2FA form.
  The Telegram form is the only typed-input precedent: `settings_controller.py:1424-1535`
  (`telegram_start_auth` and friends, run through `_run_async`), the modal in
  `settings_window_html.py:1355-1440` and `:1499-1573`, and in org mode `web/routes_connect.py`
  (`_check_telegram_post` `:421-426`, `telegram_start` `:428-454`, `_telegram_box_html` `:613-671`,
  routes `:538-541`).
- **Credential files**: `daemon_main.TOKEN_FILES` (`src/privacyfence/daemon_main.py:169-180`),
  resolved per principal by `_resolve_path` (`:280-299`). `build_connectors()` (`:1275-1578`)
  builds each connector in a `try`; `_classify_connector_failure` (`:1222-1258`) maps a message
  containing `"Use Authenticate…"` to `not_authenticated` and one containing
  `"organization config not installed"` to `no_org_config`. The Salesforce block (`:1466-1481`) is
  the pattern. Token writers go through `secure_files.atomic_write_json` (mode `0600`) and are listed
  in `TOKEN_WRITE_SITES` (`tests/unit/test_systemic_gate_invariants.py:177-188`, 10 entries, pinned
  by `test_ten_token_write_sites_are_listed` at `:248`).
- **Settings page**: `settings_controller.ALL_CONNECTORS` (`:89-92`), `ORG_CONFIG_SERVICE`
  (`:115-121`, no Telegram entry), `_connectors_state` (`:1797-1830`, special-cases Telegram's
  `has_org`). Actions are dispatched by `POST /api/settings/{action}` (`web/routes_settings.py:847`),
  allowlisted through `web/org_settings_scope.py`'s `ACTION_SCOPES` (Telegram's are `LOCAL_MODE`
  only, `:134-137`) and classified in `_SENSITIVE_ACTIONS`/`_NON_SENSITIVE_ACTIONS`
  (`web/routes_settings.py:223`, `:254-261`). The settings audit records only the action name
  (`:886`), never its arguments.
- **Org bundle**: `scripts/build_org_bundle.py` (standard library only) writes one section per
  service. A connector is offered in org mode when its section is present (`web/routes_connect.py`
  `_is_configured` `:138-148`).
- **Policy tables** a new tool must appear in: `auto_accept.TOOL_TO_GATE` (`:177`),
  `TOOL_TO_OPERATION` (`:84`), `policy/registry.py` `TOOL_TO_VERB` (`:141`),
  `write_effects.EFFECT_BY_TOOL` (`:42`), optionally `gate._TOOL_LAYOUT` (`:254-296`).
  `policy/scopes.SCOPE_SELECTORS` is the frozen set checked against `tests/unit/policy/_v1_reference.py`;
  a scope with no v1 predicate goes in `NEW_SCOPE_SELECTORS` (`:555`) and is offered through
  `policy/catalogue.EXTRA_SCOPES` (`:47`), the Apps Script precedent (`apps_script.project`).
  `policy/propose.py` only proposes from `SCOPE_SELECTORS` (`:514-516`), so an `EXTRA_SCOPES`
  operation never gets the popup's "Always allow" (ADR 0077).
- **Every-connector bookkeeping enforced by tests**: `tests/unit/connectors/test_readme_manifest_alignment.py`
  `CONNECTOR_CLASSES` (`:36-40`, hand list), `scripts/generate_tools_reference.py`
  `CONNECTOR_TITLES`/`CONNECTOR_SHORT` (`:44-70`; a connector missing there makes `render()` raise),
  `docs/tools-reference.md` (generated; `tests/unit/test_docs_tools_reference.py` fails when stale),
  `scripts/pyinstaller_common.py` hidden imports (`:74-86`), `tests/unit/test_website_connector_pages.py`
  `CONNECTORS` (`:28-40`, must equal the modules in `connectors/`, needs a README row, a setup guide in
  `scripts/build_site.py` `CONNECTOR_GUIDES` (`:194`) and a page in `PAGES` (`:107`)),
  `tests/unit/test_website_connectors_page.py` (one card per tools-reference summary row with the same
  counts, `"120 tools"` / `"Eleven connectors"` in `website/connectors/index.html:29`, `"120 connector
  tools"` in `website/how-it-works/index.html:54`, and `len(REFERENCE) == 11` at `:80`),
  `tests/unit/test_connector_tool_annotations.py` (only deleting tools are `destructive`; Grist adds
  none), `tests/unit/test_systemic_gate_invariants.py` (required `reason` param on every gated tool,
  `pii_scan_text` on every review-gated call).
- **QA**: `scripts/qa_fixture_recorder.py` `CONNECTOR_CHECKS` (`:1776`) and `EXPECTED_FIXTURES`
  (`:1802`) must have equal keys (import-time assert); `RawCapture` (`:595-628`) wraps a client's
  `_request(fn, *args, **kwargs)`; `LIFECYCLE_CHECKS` (`:2353`); `lifecycle_confluence`
  (`:2195-2240`) is the create-and-update-without-delete precedent. Live credentials exist only on
  the self-hosted runner under `~/privacyfence/credentials/` (ADR 0019, `docs/connector-qa.md`
  "Persistent QA state").
- Next free ADR number: **0142**.

## 3. Design

### 3.0 Rules for every phase

- No project history in code, comments, docstrings, user-visible strings or standing docs: no phase
  ids (`p3`), plan names, "Phase N", bare `#123` issue numbers or "as of" phrasing
  (`tests/unit/test_code_no_history.py`, `tests/unit/test_docs_no_history.py`, ADR 0056). Cite an ADR
  number for a reason instead.
- Every `gated_call(...)` in `connectors/grist.py` passes its tool name as a string literal
  (`tool="grist_add_records"`) and its gate as a literal (`gate="popup"`), inline at the call site.
  `test_readme_manifest_alignment.py` (`_SRC_TOOL_GATE_RE`), `test_write_effects.py` and
  `test_systemic_gate_invariants.py` read the source; a shared helper that passes the tool name as a
  variable defeats them and is not allowed.
- This plan and its manual-steps page are listed in `scripts/build_site.py` `CONTRIBUTOR_DOCS` and in
  `docs/README.md`'s contributor half while the work is open (the plan branch's own commit does
  that, so `tests/unit/test_website_docs_allowlist.py` passes); the last phase removes both entries.
- Every phase ends with `ruff check .` and `python3 -m pytest tests/unit -q` passing in full.

### 3.1 Grist API used

Base: `{server_url}/api`. Auth header `Authorization: Bearer <api_key>`. Endpoints (Grist OpenAPI,
`gristlabs/grist-help` `api/grist.yml`):

| Client method | HTTP |
|---|---|
| `check_connection` | `GET /api/orgs` |
| `list_documents` | `GET /api/orgs`, then `GET /api/orgs/{orgId}/workspaces` per org (each workspace has `docs[]` with `id`, `name`, `urlId`) |
| `get_document` | `GET /api/docs/{docId}` (`name`, `workspace.name`, `workspace.org.name`) |
| `list_tables` | `GET /api/docs/{docId}/tables`, then `GET /api/docs/{docId}/tables/{tableId}/columns` per table |
| `list_columns` | `GET /api/docs/{docId}/tables/{tableId}/columns` (`columns[].id`, `fields.label`, `fields.type`, `fields.isFormula`) |
| `get_records` | `GET /api/docs/{docId}/tables/{tableId}/records?filter=<json>&sort=<csv>&limit=<n>` → `{"records":[{"id","fields"}]}` |
| `add_records` | `POST …/records` body `{"records":[{"fields":{…}}]}` → `{"records":[{"id"}]}` |
| `update_records` | `PATCH …/records` body `{"records":[{"id","fields":{…}}]}` → empty |
| `add_table` | `POST /api/docs/{docId}/tables` body `{"tables":[{"id","columns":[{"id","fields":{"label","type"}}]}]}` → `{"tables":[{"id"}]}` |
| `add_columns` | `POST /api/docs/{docId}/tables/{tableId}/columns` body `{"columns":[{"id","fields":{"label","type"}}]}` → `{"columns":[{"id"}]}` |

The `records/delete` endpoint and every other delete, rename and column-modify endpoint are never
called (§3.7).

### 3.2 `src/privacyfence/grist_client.py`

Module docstring: the Grist REST client; authenticates with a personal API key; never follows a
redirect (the key would otherwise be resent to wherever the redirect points); logs the server's host
and counts only, never the key or a cell value.

- HTTP: `requests` (already a dependency), one `requests.Session`, `timeout=30`,
  `allow_redirects=False`, TLS verification on (the `requests` default; never pass `verify=False`).
- `class GristClientError(Exception)`.
- Dataclasses (all fields typed):
  - `GristDocument(id: str, name: str, workspace: str, team: str)`
  - `GristColumn(id: str, label: str, type: str, is_formula: bool)`
  - `GristTable(id: str, columns: list[GristColumn] = field(default_factory=list))`
  - `GristRecord(id: int, fields: dict[str, Any] = field(default_factory=dict))`
  - `GristRecordPage(records: list[GristRecord], truncated: bool)`
- Validation (module-level, raise `GristClientError` with exactly these messages):
  - `DOC_ID_RE = re.compile(r"[A-Za-z0-9_~-]{1,128}")`, `IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")`,
    both used with `.fullmatch()`. `validate_doc_id(v)` → `f"Not a Grist document id: {v!r}"`;
    `validate_identifier(v, kind)` (kind is `"table"` or `"column"`) → `f"Not a Grist {kind} id: {v!r}"`.
    They exist because the ids go into the URL path, where `..`, `/` or `?` would redirect the call.
  - `normalize_server_url(url: str) -> str`: strip whitespace and trailing `/`; parse with
    `urllib.parse.urlsplit`. Reject (message `"Enter the Grist server address, such as https://docs.getgrist.com."`)
    an empty value; reject (message `"The Grist server address must start with https:// (http:// is allowed only for localhost)."`)
    any scheme but `https`, except `http` when the hostname is `localhost`, `127.0.0.1` or `::1`;
    reject (message `"The Grist server address must not contain a user name, password, query or fragment."`)
    userinfo, query or fragment; reject (message `"Enter the server address without /api."`) a path
    whose segments include `api`. A path prefix is kept (self-hosted Grist can live under one). Returns
    `scheme://netloc[/path]` with the scheme and host lower-cased.
- Credential file `credentials/grist_token.json`, JSON object `{"server_url": "<normalized>", "api_key": "<key>"}`:
  - `save_token_file(path: str, server_url: str, api_key: str) -> None` → `secure_files.atomic_write_json`.
  - `load_token_file(path: str) -> dict[str, str]`: missing file or missing/empty keys → `GristClientError("Grist is not authenticated. Use Authenticate… in PrivacyFence Settings.")`;
    a file that is not valid JSON, or not a JSON object →
    `GristClientError("Grist's saved credentials could not be read. Use Authenticate… in PrivacyFence Settings to connect again.")`
    (so a damaged file disables Grist only, never the whole `build_connectors` run).
  - `effective_server_url(token: dict[str, str], pinned: str) -> str`: with `pinned` empty, returns
    `normalize_server_url(token["server_url"])`; otherwise returns `normalize_server_url(pinned)`,
    and if the token's normalized `server_url` differs, raises
    `GristClientError("Grist was connected to a different server than your organization uses. Use Authenticate… in PrivacyFence Settings to connect again.")`
    so a key is never sent to a server it was not entered for.
- `class GristClient`:
  - `__init__(self, server_url: str, api_key: str)`; stores `normalize_server_url(server_url)`.
    `__repr__` must not include the key. Read-only property `host -> str`: the server URL's host
    (with port, if any), used in log lines and on every approval card (§3.3).
  - `_request(self, method: str, path: str, *, params: dict[str, str] | None = None, json_body: Any = None) -> Any`:
    the single choke point (the QA recorder's `RawCapture` wraps it; keep `method` as the first
    positional parameter). Returns parsed JSON, or `None` for an empty body. Error mapping:
    - `requests.RequestException` → `f"Could not reach the Grist server at {host}: {type(exc).__name__}"`
    - 3xx → `f"The Grist server answered with a redirect (HTTP {status}). Check the server address in PrivacyFence Settings."`
    - 401 or 403 → `f"Grist refused the request (HTTP {status}). The API key may be wrong or revoked, or it has no access to this document. Reconnect Grist in PrivacyFence Settings with a current key."`
    - 404 → `f"Grist found no such document, table or record (HTTP 404)."`
    - any other non-2xx → `f"Grist API error (HTTP {status}): {detail}"`, with `detail` the response
      JSON's `"error"` string cut to 200 characters, or `"no detail"`.
    - a 2xx whose non-empty body is not JSON (a proxy's or login page's HTML, for example) →
      `f"The Grist server answered with something other than JSON (HTTP {status}). Check the server address in PrivacyFence Settings."`
  - `check_connection() -> int` (number of orgs).
  - `list_documents() -> list[GristDocument]`: at most 20 orgs and 500 documents, sorted by
    `(team, workspace, name)`; `id` is the document's `id` (never `urlId`: a urlId resolves only on
    its own team site, and `grist.document` rules need one stable id); `team` is the org's `name`.
  - `get_document(doc_id) -> GristDocument`.
  - `list_columns(doc_id, table_id) -> list[GristColumn]` (`label` falls back to `id`).
  - `list_tables(doc_id) -> list[GristTable]` (at most 100 tables).
  - `get_records(doc_id, table_id, *, filters: dict[str, list[Any]] | None, sort: str, limit: int) -> GristRecordPage`:
    requests `limit + 1` rows and sets `truncated` when more than `limit` came back.
  - `get_records_by_id(doc_id, table_id, ids: list[int]) -> list[GristRecord]` (filter `{"id": ids}`).
  - `add_records(doc_id, table_id, rows: list[dict[str, Any]]) -> list[int]`.
  - `update_records(doc_id, table_id, rows: list[tuple[int, dict[str, Any]]]) -> None`.
  - `add_table(doc_id, table_id, columns: list[GristColumn]) -> str` (returns Grist's id for it).
  - `add_columns(doc_id, table_id, columns: list[GristColumn]) -> list[str]`.
  Every method validates its ids first.

### 3.3 `src/privacyfence/connectors/grist.py`

`class GristConnector(Connector)`, `name == "grist"`, `__init__(self, client: GristClient)`.
`_auto_audit` copied from `connectors/salesforce.py:634-653`. `_fetch` is the Salesforce one
(`:628-632`) widened to keyword arguments: `async def _fetch(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any`
calling `await asyncio.to_thread(func, *args, **kwargs)` and re-raising `GristClientError` as
`RuntimeError(str(exc)) from exc` (after `logger.warning`).
Argument validation raises `ValueError` before anything is fetched or gated. JSON-string parameters
(`ToolParam` has no list type, as in Salesforce's `filters`) are parsed with `json.loads`; a parse
failure or wrong shape raises `ValueError` with the message given below.

Tools (every one ends with the required `ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")`;
`read_only` is `True` for the three reads and `False` for the four writes; none is `destructive`):

| Tool | Gate | Params (besides `reason`) | Returns |
|---|---|---|---|
| `grist_list_documents` | auto | none | `{"documents": [{"id","name","workspace","team"}]}` |
| `grist_list_tables` | auto | `doc_id` | `{"doc_id", "tables": [{"id", "columns": [{"id","label","type","is_formula"}]}]}` |
| `grist_get_records` | review | `doc_id`, `table_id`, `filter` (str, default `""`), `sort` (str, default `""`), `limit` (int, default 100) | `{"doc_id","table_id","records":[{"id","fields"}],"truncated": bool}` |
| `grist_add_records` | popup | `doc_id`, `table_id`, `records` (str) | `{"doc_id","table_id","added_ids":[…]}` |
| `grist_update_records` | popup | `doc_id`, `table_id`, `records` (str) | `{"doc_id","table_id","updated_ids":[…]}` |
| `grist_create_table` | popup | `doc_id`, `table_id`, `columns` (str) | `{"doc_id","table_id": <id Grist assigned>,"column_ids":[…]}` |
| `grist_add_columns` | popup | `doc_id`, `table_id`, `columns` (str) | `{"doc_id","table_id","column_ids":[…]}` |

Descriptions follow ADR 0115 (`assert_tool_definitions_complete`): a one-sentence summary first, a
`Returns` sentence, the related tool, and the approval wording (`"Auto-approved."`,
`"Requires user approval."`, as the Salesforce tools word them). First sentences (they become the
tools-reference rows):

- `grist_list_documents`: "List the Grist documents this account can open, with their workspace and team."
- `grist_list_tables`: "List the tables of one Grist document and each table's columns (id, label and type)."
- `grist_get_records`: "Read records from one table of a Grist document, optionally filtered and sorted."
- `grist_add_records`: "Add new records to one table of a Grist document."
- `grist_update_records`: "Change cells of existing records in one table of a Grist document."
- `grist_create_table`: "Create a new table, with its columns, in a Grist document."
- `grist_add_columns`: "Add new columns to an existing table of a Grist document."

Parameter descriptions: `doc_id` "The document id from grist_list_documents (the part of a Grist
URL after /doc/ or the docs.getgrist.com/ host, such as 8CAN8gKdxY7z)."; `table_id` "The table id
from grist_list_tables, such as Contacts (not the label shown in Grist)."; `filter` "A JSON object
mapping column ids to lists of allowed values, such as {\"Status\": [\"Open\", \"Waiting\"]}. Rows
match when every listed column has one of its values. Empty means no filter."; `sort` "Column ids
separated by commas, each optionally prefixed with - for descending, such as -Date,Name. Empty means
Grist's own order."; `limit` "Most records to return, 1 to 500. Default 100; the result says
truncated when there were more."; `records` (add) "A JSON array of objects, each mapping column ids
to the new record's values, such as [{\"Name\": \"Ada\", \"Score\": 3}]. 1 to 100 records; values
are text, numbers, true/false or null."; `records` (update) "A JSON array of {\"id\": <record id>,
\"fields\": {<column id>: <new value>}} objects, such as [{\"id\": 5, \"fields\": {\"Status\":
\"Done\"}}]. 1 to 100 records; only the listed columns change."; `columns` "A JSON array of {\"id\":
<column id>, \"label\": <optional label>, \"type\": <optional type>} objects. Types: Text (default),
Numeric, Int, Bool, Date, Choice, ChoiceList, Any, Ref:<TableId>, RefList:<TableId>. 1 to 50 columns."

Siblings map for `TestToolDefinitions`: `grist_list_documents`↔`grist_list_tables`,
`grist_list_tables`↔`grist_get_records`, `grist_add_records`↔`grist_update_records`,
`grist_create_table`↔`grist_add_columns`. Each description names its sibling.

Validation (shared helpers in the connector module; messages exact):

- `filter`: object with at most 20 keys, each key a valid column id, each value a non-empty list of
  scalars (`str`, `int`, `float`, `bool`, `None`) → otherwise `ValueError('filter must be a JSON object mapping column ids to lists of values, such as {"Status": ["Open"]}.')`.
- `sort`: comma-separated; each item an optional `-` then a valid column id → otherwise
  `ValueError("sort must be column ids separated by commas, each optionally prefixed with -.")`.
- `limit` outside 1–500 → `ValueError("limit must be between 1 and 500.")`.
- `records` (add): list of 1–100 objects, keys valid column ids, values scalars → otherwise
  `ValueError('records must be a JSON array of 1 to 100 objects mapping column ids to text, numbers, true/false or null.')`.
- `records` (update): list of 1–100 `{"id": int > 0, "fields": {non-empty object as above}}`, no
  duplicate id → otherwise `ValueError('records must be a JSON array of 1 to 100 {"id": <record id>, "fields": {...}} objects with distinct ids.')`.
- `columns`: list of 1–50 objects with a valid column `id`, optional `label` (str, ≤100 chars),
  optional `type` (default `"Text"`), no duplicate id → otherwise
  `ValueError('columns must be a JSON array of 1 to 50 {"id": ..., "label": ..., "type": ...} objects with distinct ids.')`.
  `type` must be one of `Text`, `Numeric`, `Int`, `Bool`, `Date`, `Choice`, `ChoiceList`, `Any`,
  or `Ref:<TableId>` / `RefList:<TableId>` with a valid table id → otherwise
  `ValueError(f"Unsupported column type {t!r}. Use one of Text, Numeric, Int, Bool, Date, Choice, ChoiceList, Any, Ref:<TableId>, RefList:<TableId>.")`.
- Before gating a record write, the connector fetches `list_columns`. A column id not in the table →
  `ValueError(f"Unknown column(s) in table {table_id}: {', '.join(sorted(unknown))}. Call grist_list_tables to see the column ids.")`;
  a formula column → `ValueError(f"Column(s) {', '.join(sorted(cols))} in table {table_id} are formula columns and cannot be written.")`.
- `grist_update_records` fetches `get_records_by_id`; ids not found →
  `ValueError(f"Record id(s) not found in table {table_id}: {', '.join(map(str, sorted(missing)))}.")`.
- `grist_create_table`: a table with that id already in `list_tables` →
  `ValueError(f"Table {table_id} already exists in this document. Use grist_add_columns to add columns to it.")`.
- `grist_add_columns`: the table must exist (else `ValueError(f"Table {table_id} does not exist in this document. Use grist_create_table to create it.")`);
  an existing column id → `ValueError(f"Column(s) {', '.join(sorted(dupes))} already exist in table {table_id}.")`.

Cell display helper `_cell_text(value) -> str`: `None` → `""`; `str` as-is; anything else
`json.dumps(value, ensure_ascii=False)`; result cut to 200 characters with a trailing `…`.

`gated_call` arguments (all keyword; `connector=self.name`; every call passes
`args={"doc_id": doc_id, "table_id": table_id}` plus, for `grist_get_records`, `"filter"`, `"sort"`
and `"limit"`). Every `preview` dict below starts with `"Server": self._client.host`: in local mode
the server is whatever the user typed, so the card always says where the data comes from or goes
to. The dicts are listed without that first key:

- `grist_get_records` (fetch `get_document` and `get_records`, then gate): `tool_name="Read Grist Records"`,
  `summary=f"Read {len(records)} record(s) from {doc.name} / {table_id}"`, `sender=doc.name`,
  `raw_data=page`, `filtered_data=<the return dict>`, `gate="review"`,
  `preview={"Document": doc.name, "Team": doc.team, "Table": table_id, "Filter": filter or "(none)", "Sort": sort or "(none)"}`,
  `new_info={"Records": str(len(records)), "Record content": "values of every visible column"}`,
  `details_text` = one block per record (`#<id>` then `<column>: <_cell_text>` lines), or `"(no records)"`,
  `pii_scan_text=details_text`, `preview_tables=[{"headers": ["id", *column ids in first-seen order], "rows": [...]}]`
  (omitted when there are no records), `table_only=True`, `my_email=""`.
  When `truncated`, prefix `details_text` with `f"Showing the first {limit} records; more match.\n\n"`.
- `grist_add_records` (fetch `get_document`, `list_columns`, validate, then gate, then write):
  `tool_name="Add Grist Records"`, `summary=f"Add {n} record(s) to {doc.name} / {table_id}"`,
  `sender=doc.name`, `raw_data={"doc_id","table_id","records": rows}`, `filtered_data=None`,
  `gate="popup"`, `preview={"Document": doc.name, "Table": table_id, "Records": str(n)}`,
  `preview_tables=[{"headers": [column ids], "rows": [[_cell_text(...)]]}]`, `details_text` = the
  records as indented JSON.
- `grist_update_records` (fetch `get_document`, `list_columns`, `get_records_by_id`, validate, gate,
  write): `tool_name="Update Grist Records"`, `summary=f"Update {n} record(s) in {doc.name} / {table_id}"`,
  `preview={"Document": doc.name, "Table": table_id, "Records": str(n), "Cells changed": str(cells)}`,
  `preview_tables=[{"headers": ["Record", "Column", "Current", "New"], "rows": [[f"#{id}", col, _cell_text(old), _cell_text(new)], ...]}]`,
  `details_text` one line per cell `f"#{id} {col}: {_cell_text(old)} → {_cell_text(new)}"`,
  `raw_data={"doc_id","table_id","records": [...]}`, `filtered_data=None`, `gate="popup"`.
- `grist_create_table`: `tool_name="Create Grist Table"`, `summary=f"Create table {table_id} in {doc.name}"`,
  `preview={"Document": doc.name, "New table": table_id, "Columns": str(n)}`,
  `preview_tables=[{"headers": ["Column", "Label", "Type"], "rows": [...]}]`,
  `details_text` the column lines `f"{id} ({type}) {label}"`, `gate="popup"`, `filtered_data=None`.
- `grist_add_columns`: `tool_name="Add Grist Columns"`, `summary=f"Add {n} column(s) to {doc.name} / {table_id}"`,
  `preview={"Document": doc.name, "Table": table_id, "New columns": str(n)}`, same table and details
  shape as `grist_create_table`, `gate="popup"`, `filtered_data=None`.

A write runs only after `gated_call` returns; a denial raises out of `gated_call` exactly as in every
other connector. Auto tools audit with `_auto_audit("grist_list_documents", "List Grist Documents", "List documents", f"{n} document(s)", t0)`
and `_auto_audit("grist_list_tables", "List Grist Tables", f"List tables of {doc_id}", f"{n} table(s)", t0)`.

### 3.4 Policy tables

| Tool | `TOOL_TO_GATE` | `TOOL_TO_OPERATION` | `TOOL_TO_VERB` | `EFFECT_BY_TOOL` | `_TOOL_LAYOUT` |
|---|---|---|---|---|---|
| `grist_list_documents` | `auto` | — | — | — | — |
| `grist_list_tables` | `auto` | — | — | — | — |
| `grist_get_records` | `review` | `grist.read_records` | `READ` | — | `WIDE` |
| `grist_add_records` | `popup` | `grist.add_records` | `CREATE` | `"New records are added to that table."` | `WIDE` |
| `grist_update_records` | `popup` | `grist.update_records` | `UPDATE` | `"The cells shown change to their new values. Other cells and records are not touched."` | `WIDE` |
| `grist_create_table` | `popup` | `grist.create_table` | `RESTRUCTURE` | `"A new table is added to the document. Nothing existing changes."` | — (NARROW) |
| `grist_add_columns` | `popup` | `grist.add_columns` | `RESTRUCTURE` | `"New, empty columns are added to that table. Existing columns and values do not change."` | — (NARROW) |

Rule scope: `grist.document`, a new **identity** scope on `doc_id`, resolved from the call's args.
It goes in `policy/scopes.NEW_SCOPE_SELECTORS` (`"grist.document": ScopeSelector(predicate="grist.document", scope_type="grist.document", kind=ScopeKind.IDENTITY, resolves_from=ResolvesFrom.ARGS, matches=_grist_document_matches)`,
where `_grist_document_matches` is the `_apps_script_project_matches` shape on `ctx.args.get("doc_id", "")`)
and in `policy/catalogue.EXTRA_SCOPES`:
`"grist.document": PolicyExtraScope(predicate="grist.document", connector="grist", verbs=(Verb.READ, Verb.CREATE, Verb.UPDATE, Verb.RESTRUCTURE), label="Grist — document", needs_value=True, value_hint="8CAN8gKdxY7z…document-id")`.
It gets no `policy/propose.PROPOSABLE_SCOPES` entry, so the approval popup never offers "Always
allow" for a Grist operation; rules are made on **Settings > Auto-accept** or through
`privacyfence_propose_policy_change` (§3.8, ADR 0144).

### 3.5 Local mode: daemon and Settings

- `daemon_main.TOKEN_FILES["grist"] = "credentials/grist_token.json"`.
- `build_connectors()`, after the Telegram block:
  ```python
  if enabled("grist"):
      try:
          pinned = (org_config.get("grist") or {}).get("server_url", "")
          if download_mode == "org" and not pinned:
              raise GristClientError("Grist organization config not installed")
          token = load_grist_token(_resolve_path(TOKEN_FILES["grist"]))
          client = GristClient(server_url=effective_server_url(token, pinned), api_key=token["api_key"])
          client.check_connection()
          connectors.append(GristConnector(client))
      except GristClientError as exc:
          logger.warning("Grist connector disabled: %s", exc)
          failures["grist"] = _classify_connector_failure(exc)
  ```
  (`load_grist_token` is `grist_client.load_token_file` imported under that name, like
  `load_salesforce_token`. `download_mode` is the `org_mode.resolve_mode(org_config)` value
  `build_connectors` already computes near its top (`daemon_main.py:1306`); reuse it rather than
  calling `resolve_mode` again.)
- `settings_controller.py`: `"grist"` appended to `ALL_CONNECTORS`; `_connectors_state` sets
  `has_org = True` for `"grist"` (no bundle is needed in local mode) instead of reading
  `ORG_CONFIG_SERVICE`; `connector_label("grist")` is already `"Grist"`.
- New action `grist_connect(self, server_url: str, api_key: str) -> dict[str, Any]`:
  1. `api_key = api_key.strip()`; empty → `self._grist_auth = {"error": "Enter your Grist API key."}`, return snapshot.
  2. `pinned = (self._org_config_or_empty().get("grist") or {}).get("server_url", "")`
     (`settings_controller.py:1136`); the URL used is `pinned` if set,
     else `server_url`; `normalize_server_url` failures set `_grist_auth = {"error": str(exc)}`.
  3. Mark `"grist"` busy and `_run_async` a worker that builds `GristClient(url, api_key)`, calls
     `check_connection()`, then `save_token_file(str(data_dir() / TOKEN_FILES["grist"]), url, api_key)`.
  4. On success: `_grist_auth = None`, `self.error = ""`, `refresh_connectors()`. On failure:
     `_grist_auth = {"error": str(exc)}` and `_push_snapshot()`.
  Also `grist_cancel_auth(self) -> dict[str, Any]` (clears `_grist_auth`). `snapshot()` gains
  `"grist_auth": {"error": <str or "">}` and `"grist_server_url_pinned": pinned` (never the key, never
  the stored URL's key).
- `web/org_settings_scope.py`: `"grist_connect"` and `"grist_cancel_auth"` as
  `ActionScope(modes=frozenset({LOCAL_MODE}))`, next to the Telegram ones.
  `web/routes_settings.py`: both in `_NON_SENSITIVE_ACTIONS`. The module docstring
  (`web/routes_settings.py:74-80`) already states that connector auth stays ungated, alongside
  `authenticate_connector` and `telegram_submit_2fa`; ADR 0070 makes *enabling* a connector
  sensitive, and that still applies to Grist. The arbitrary-server risk is answered by the card
  naming the server on every Grist approval (§3.3) and recorded in ADR 0143.
- `settings_window_html.py`: the Grist row gets `data-grist-auth="1"` (as Telegram's
  `data-telegram-auth`), opening a modal with two fields: **Server address** (`type="url"`,
  prefilled `https://docs.getgrist.com`, hidden and replaced by the text "Your organization uses
  <pinned>" when `grist_server_url_pinned` is set) and **API key** (`type="password"`,
  `autocomplete="off"`), a short line "Create a key in Grist under Profile settings → API.", and
  **Connect** / **Cancel**. Connect posts `grist_connect` with `{server_url, api_key}` and sets a
  client-side `ui.gristSubmitted = true`. The result arrives by snapshot push (the work runs through
  `_run_async`), so on each render: while the Grist row is `busy`, show "Connecting…"; once
  `ui.gristSubmitted` is true, the row is no longer `busy` and `grist_auth.error` is `""`, close the
  modal and reset the flag (the same shape as Telegram's `telegramAuthWasActive` check,
  `settings_window_html.py:1416-1421`); a non-empty `grist_auth.error` is shown in the modal, which
  stays open. The key field is cleared after every submit. `snapshot()["grist_auth"]` is therefore
  always `{"error": <str>}`, with `""` for no error.

### 3.6 Org mode

- Bundle section `"grist": {"server_url": "<url>"}` written by `scripts/build_org_bundle.py
  --grist-server-url URL`. The script stays standard-library only: it strips a trailing `/` and
  rejects (via `SystemExit("--grist-server-url must be an https:// address.")`) anything whose
  `urlsplit` scheme is not `https` or whose host is empty. `"grist"` joins the
  `services = [...]` tuple at `:692` so a Grist-only bundle is not "nothing to write", and
  `_CONNECTOR_CALLBACKS` gets `"grist": ()` (no OAuth callback; the org-mode summary at `:744`
  indexes that dict for every service, so a missing key would raise `KeyError`).
- `web/routes_connect.py` (its test asserts the number of service rows, `tests/unit/web/test_routes_connect.py:170`
  `== 11`, which becomes 12): `_is_configured("grist")` is `bool((org_config.get("grist") or {}).get("server_url"))`;
  `_is_connected("grist")` checks the token file. A new `_grist_box_html(...)` renders, when
  configured, a form `POST /connect/grist` with the CSRF field, the pinned server shown as text,
  an `<input type="password" name="api_key" autocomplete="off">`, and **Connect**; when connected,
  "Connected to <server>" plus the same form labelled **Reconnect**; when not configured, "Not set up
  by your organization". It is placed after the Telegram box in `_render_connect_page`.
- `grist_connect` handler: signed-out → `_signed_out_redirect()`; CSRF and origin checked by the
  same helper as Telegram (`_check_telegram_post`; rename it `_check_form_post` and use it for both);
  empty key → error "Enter your Grist API key."; otherwise `GristClient(pinned, key).check_connection()`
  in `asyncio.to_thread`, `save_token_file(str(paths.user_dir(principal) / TOKEN_FILES["grist"]), pinned, key)`,
  `connector_registry.evict(principal.id)`. Errors are kept per principal the way
  `telegram_states` keeps Telegram's (a small `grist_errors` dict keyed by principal id, cleared on
  success) and shown in the box. Always `RedirectResponse("/connect", 303, Cache-Control: no-store)`.
  Route: `Route("/connect/grist", grist_connect, methods=["POST"])`.
- Per-user key files in org mode sit under `paths.user_dir(principal)` with the other per-user
  third-party credentials (ADR 0072 notes those are stored as-is, per principal).

### 3.7 What is deliberately not built

- No delete of records, columns or tables; no column rename, modify or type change; no SQL tool; no
  attachments. Adding any of them later is a new decision.
- No `--grist-setup` CLI flag: the Settings form and `/connect` cover both modes, and the QA
  runner's credential file is written by hand (it is two JSON keys).
- No connector icon (only real brand assets go in `resources/connector_icons/`).

### 3.8 ADR-worthy decisions (written in the last phase)

See §4.

### 3.9 Setup guide `grist-setup.md` (in `docs/`)

Sections: `# Grist setup`; `## What you need` (a Grist account on docs.getgrist.com, a team site,
or a self-hosted server; nothing for an administrator in local mode); `## Create an API key`
(Grist → profile picture → **Profile settings** → **API** → **Create**; the key acts as you, with
your access to every document); `## Connect in local mode` (Settings > Connectors > Grist >
Authenticate…, server address, key); `## Connect in organization mode` (the administrator adds
`--grist-server-url` to the bundle, see `configuration-reference.md`; each person pastes their own
key on the connections page); `## What the assistant can do` (the seven tools and their gates, link
to the tools reference `#grist`); `## Auto-accept rules` (the `grist.document` scope is set on
Settings > Auto-accept; the approval card has no Always allow for Grist); `## Troubleshooting` (the
exact error texts from §3.2 with what to do).

## 4. ADRs

- **0142** — Grist connects with a personal API key the user pastes, stored per principal like an
  OAuth token (`credentials/grist_token.json`, `atomic_write_json`, mode 0600), never in
  `settings.yaml`, the settings snapshot, a log line or the audit log. Rejected: Grist OAuth (only for
  integrations registered with Grist Labs, and unavailable on self-hosted servers); the OS keyring (no
  other connector credential uses one, and the daemon runs under its own service account).
  Consequence to record: `grist` becomes a reserved plugin name, so a plugin already called `grist`
  is refused from this release on.
- **0143** — The Grist server is the user's choice in local mode and the organization's in org mode:
  an org-mode install offers Grist only when the bundle's `grist.server_url` is set, a bundle's URL
  overrides the user's in either mode, and a key is never sent to a server other than the one it was
  entered for (a mismatch asks to reconnect). Only `https` (or `http` to loopback); redirects are
  never followed. Every Grist approval card names the server. Connecting Grist stays a
  non-sensitive Settings action like every other connector sign-in (`web/routes_settings.py`'s
  docstring), because the card shows the server on every read and write. Rejected: getgrist.com only
  (rules out self-hosted Grist, which privacy-minded users run); a free URL in org mode (a person
  could send organization data to any server); classing `grist_connect` as a step-up action (it
  would make Grist the only connector whose sign-in needs a passkey, for a risk the card already
  shows).
- **0144** — Grist auto-accept rules are scoped per document (`grist.document`) and made on
  Settings or through the bridge tool, never from the approval card's "Always allow". Rejected:
  adding the scope to `SCOPE_SELECTORS`, which is the frozen v1-equivalence set
  (`tests/unit/policy/_v1_reference.py`); the popup proposes only from that set (ADR 0077 is the
  precedent).
- **0145** — The Grist connector only adds: records, tables and columns. Nothing deletes, renames or
  changes a column's type. Rejected: `grist_delete_records` and schema-modify tools (a delete
  cannot be previewed as anything but ids, and a type change can rewrite every value in a column).

## 5. Manual steps

Step-by-step page: see `manual_steps_artifact` in the manifest.

- **Before implementation**: create a Grist QA account, seed document and API key
  (`mb1-grist-qa-account`); put the key and the seed ids on the self-hosted runner
  (`mb2-runner-qa-state`). Phase `p9-qa-recorder` dispatches `qa-record-fixture.yml`, which fails
  without them.
- **After implementation**: drive the connector from a real AI client in local mode
  (`ma1-local-mode-check`), and, if an org-mode test deployment exists, the `/connect` form
  (`ma2-org-mode-check`).

## 6. Risks and open questions

- **Grist response shapes.** §3.1 comes from Grist's OpenAPI file, not from a recorded response.
  `get_document`'s `workspace.org.name` and `/orgs/{id}/workspaces`' `docs[].urlId` are the least
  certain. The parsers must tolerate missing optional keys (empty string). `p9-qa-recorder` records
  real responses; if a fixture contradicts a parser, that phase fixes the parser and its unit test.
  If the recorded shape is so different that a tool's behaviour in §3.3 cannot be kept, stop with
  `status=blocked`.
- **`grist.document` and the popup.** If after `p6-policy-scope` the approval card does offer an
  "Always allow" for a Grist tool (a test in `tests/unit/test_gate.py` shows it), the catalogue
  route is not what this plan assumed: stop with `status=blocked`.
- **Settings tests that enumerate connectors.** `tests/unit/test_daemon_main.py`,
  `test_settings_controller.py` and the settings HTML tests may assert the exact connector list or a
  count. Update those assertions to include `grist`; if one asserts something this plan does not
  account for (for example that every connector has an `ORG_CONFIG_SERVICE` entry), stop with
  `status=blocked` rather than special-casing around it.
- **Website goes live on merge.** `pages.yml` deploys `website/` from `main`, so
  `/connectors/grist/` is public once the feature PR merges, before a release carries the connector.
  The page's "Set it up" button therefore links the GitHub copy of the guide on `main`
  (the GitHub copy of the guide on `main`; the exact href is in p2's brief), which the test
  accepts. If the maintainer wants the page held back to the release, that is a change to
  `p2-connector-listing` only.
- **Counts on the website.** Each tool phase changes the totals in `website/connectors/index.html`,
  `website/how-it-works/index.html` and the Grist card; the numbers in each brief assume the previous
  phase landed. Always take them from the regenerated `docs/tools-reference.md` summary table.
- **The runner credential.** If the dispatched `qa-record-fixture.yml` fails at its "copy QA state"
  step or with "Grist is not authenticated", `mb2-runner-qa-state` is not done: stop with
  `status=blocked` and say so.

## Implementation manifest

```yaml
plan_slug: grist-connector
feature_branch: feature/grist-connector
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/RejYXshpnB1gBwT6ySJQNH
manual_steps_source: docs/grist-connector-plan-manual-steps.html
manual_before:
  - id: mb1-grist-qa-account
    title: Create a Grist QA account, a seed document with a QaSeed table, and an API key
    why: p9-qa-recorder records live fixtures from this document; without it the recording has nothing to read.
    done_when: A Grist document named "PrivacyFence QA [QATEST]" has a table QaSeed (columns Name, Note) with two [QATEST] rows, a second document exists as a contrast case, and an API key for this account exists (kept only in your password manager).
  - id: mb2-runner-qa-state
    title: Put the Grist key and seed ids on the self-hosted QA runner
    why: p9-qa-recorder dispatches qa-record-fixture.yml, which reads ~/privacyfence/credentials/grist_token.json and the grist section of ~/privacyfence/tests/fixtures/qa_environment.yaml on the runner; without them the run fails.
    done_when: On the runner, ~/privacyfence/credentials/grist_token.json exists with mode 600 and the keys server_url and api_key, and ~/privacyfence/tests/fixtures/qa_environment.yaml has a grist section with doc_id, table_id and seed_record_name.
manual_after:
  - id: ma1-local-mode-check
    title: Connect Grist in Settings and drive every Grist tool from an AI client
    why: Proves the Settings form, the approval cards' content (values, old→new diffs) and real writes against a real Grist server, which unit tests and the recorder cannot show.
  - id: ma2-org-mode-check
    title: (If you run an org-mode test deployment) connect Grist on /connect with a bundle that pins the server
    why: Proves the bundle section, the per-person key form and that the pinned server is used; no CI job runs an org deployment against Grist.
verify_after_merge:
  - python3 -m pytest tests/unit/test_grist_client.py tests/unit/test_systemic_gate_invariants.py -q
  - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py -q
  - python3 -m pytest tests/unit/connectors -q -k grist
  - python3 -m pytest tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_generate_always_allow_reference.py -q
  - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/test_settings_window_html.py tests/unit/web/test_routes_connect.py tests/unit/test_build_org_bundle.py tests/unit/test_qa_fixture_recorder.py -q
final_checks:
  - docs/grist-connector-plan.md and docs/grist-connector-plan-manual-steps.html are deleted and nothing links to them (grep -rn "grist-connector-plan" . --exclude-dir=.git finds nothing)
  - ADRs 0142, 0143, 0144 and 0145 exist in docs/adr/, are Accepted, and are in the docs/adr/README.md index
  - CHANGELOG.md has the Grist entry under "## [Unreleased]" and no new version heading
  - After python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py, git diff --exit-code docs/tools-reference.md docs/always-allow-rules-reference.md exits 0
  - The PR description links the qa-record-fixture.yml run (p9-qa-recorder) and the connector-live-check.yml run (p10-docs-adrs-retire), which is the definition-of-done QA row
phases:
  - id: p0-reserve-name
    title: Reserve the plugin name grist in the daemon, the SDK, the protocol schema and its doc
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/plugins/constants.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - docs/plugin-protocol/protocol.schema.json
      - docs/plugin-protocol.md
    brief: |
      Read first: the must-read docs and plan §3.0. A new connector's name is reserved so no plugin can
      take it; the list exists in four copies that tests keep equal.
      1. src/privacyfence/plugins/constants.py RESERVED_PLUGIN_NAMES (l.104-109): add "grist" after "telegram".
      2. plugin-sdk/src/privacyfence_plugin_sdk/plugin.py _RESERVED_PLUGIN_NAMES (l.73-78): the same.
      3. docs/plugin-protocol/protocol.schema.json: add "grist" to $defs.Manifest.properties.name.not.enum
         (after "telegram", near l.554). Do not change "x-protocol-version".
      4. docs/plugin-protocol.md (l.146-148): add `grist` to the list of connector names after `telegram`.
      5. grep -rn '"telegram"' plugin-sdk docs/plugin-protocol* src/privacyfence/plugins and add "grist" to any
         other copy of this list the grep finds; name it in your report.
      6. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if a test demands a protocol or SDK version bump for this change, stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk/test_plugin.py tests/unit/plugins/test_protocol.py -q passes
      - 'grep -n ''"grist"'' src/privacyfence/plugins/constants.py plugin-sdk/src/privacyfence_plugin_sdk/plugin.py docs/plugin-protocol/protocol.schema.json matches in all three files'
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p1-client
    title: Grist REST client, credential file helpers and their tests
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/grist_client.py
      - tests/unit/test_grist_client.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Read first: CLAUDE.md, CONTRIBUTING.md, docs/coding-and-testing-guidelines.md (§1, §2, §3),
      docs/testing-policy.md, docs/adr/README.md, and this plan's §3.1 and §3.2 (the spec).
      1. Create src/privacyfence/grist_client.py exactly as plan §3.2: module docstring, GristClientError,
         the five dataclasses, DOC_ID_RE/IDENT_RE with validate_doc_id/validate_identifier,
         normalize_server_url, save_token_file (secure_files.atomic_write_json), load_token_file,
         effective_server_url, and GristClient with _request as the single HTTP choke point
         (requests.Session, timeout=30, allow_redirects=False) and every public method in §3.2.
         Use the exact error strings from §3.2. Never log the API key or a cell value; log the host and counts.
         Use requests.Session.request(method, url, params=..., json=..., headers=..., timeout=30, allow_redirects=False).
      2. Create tests/unit/test_grist_client.py (pytestmark = pytest.mark.unit; module docstring naming the
         module and the invariant "the API key never leaves for any server but the one it was entered for").
         Fake the HTTP boundary by monkeypatching the client's requests.Session.request (no network). Classes:
         TestNormalizeServerUrl (https kept, trailing slash stripped, path prefix kept, http rejected except
         localhost/127.0.0.1/::1, userinfo/query/fragment rejected, /api rejected, empty rejected — each with its
         exact message), TestValidateIds (doc id with ~ accepted, "../x", "a/b", "a?b" rejected; table/column
         regex), TestTokenFile (save then load round-trips; file mode 0o600 on POSIX; missing file and missing
         key raise the "not authenticated" message), TestEffectiveServerUrl (no pin → token URL; pin equal →
         pin; pin different → the "different server" error), TestRequestErrors (connection error, 302, 401,
         403, 404, 500 with {"error": ...} cut to 200 chars, 500 without JSON — exact messages; assert
         allow_redirects=False and the Authorization header were sent; assert the key is not in any raised
         message), and one class per public method asserting method, path, params/body and parsing:
         TestListDocuments (the doc's id is used even when urlId is present, sorting, org cap 20),
         TestTokenFile also covers invalid JSON and a non-object file (the "could not be read" message),
         TestRequestErrors also covers a 200 with an HTML body (the "other than JSON" message), TestHost, TestGetDocument (missing workspace/org
         keys give ""), TestListTables, TestListColumns (label falls back to id), TestGetRecords (limit+1
         requested, truncated flag, filter JSON-encoded), TestGetRecordsById, TestAddRecords, TestUpdateRecords,
         TestAddTable, TestAddColumns. Each id-taking method rejects a bad id before any request.
      3. In tests/unit/test_systemic_gate_invariants.py add ("grist_client", None, "save_token_file") to
         TOKEN_WRITE_SITES and rename test_ten_token_write_sites_are_listed to
         test_eleven_token_write_sites_are_listed asserting len == 11.
      4. Run ruff check . and python3 -m pytest tests/unit/test_grist_client.py tests/unit/test_systemic_gate_invariants.py -q.
      Stop condition: if requests is not importable in the project venv, or secure_files has no
      atomic_write_json, stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/test_grist_client.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/test_grist_client.py -q --cov=privacyfence.grist_client --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/grist_client.py
      - grep -n "allow_redirects=False" src/privacyfence/grist_client.py matches
      - grep -n "verify=False" src/privacyfence/grist_client.py finds nothing
      - ruff check . passes
  - id: p2-connector-listing
    title: GristConnector with the two auto listing tools, and every per-connector table, page and guide a new connector module requires
    depends_on: [p0-reserve-name, p1-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - scripts/build_site.py
      - docs/tools-reference.md
      - docs/grist-setup*.md
      - docs/README.md
      - README.md
      - website/connectors/grist/index.html
      - website/_partials/other-connectors.html
      - website/connectors/index.html
      - website/how-it-works/index.html
      - website/compare/mcp-gateways/index.html
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_website_connectors_page.py
    brief: |
      Read first: the must-read docs, plan §3.3 (connector spec), §3.9 (setup guide), and
      src/privacyfence/connectors/salesforce.py (template for _fetch, _auto_audit, tool_specs style).
      1. Create src/privacyfence/connectors/grist.py: module docstring, GristConnector(Connector) with
         name "grist", __init__(client: GristClient), client property, _fetch and _auto_audit as plan §3.3,
         tool_specs() returning ONLY grist_list_documents and grist_list_tables with the descriptions,
         params and sibling names from §3.3, and call() dispatching them (unknown tool →
         ValueError(f"Unknown Grist tool: {tool!r}")). Return shapes per the §3.3 table. Leave room for the
         other five tools (later phases add them); do not add them now.
      2. src/privacyfence/auto_accept.py TOOL_TO_GATE: "grist_list_documents": "auto", "grist_list_tables": "auto"
         (with a "# Grist" comment line like the other connectors' groups).
      3. Bookkeeping ("grist" is already a reserved plugin name, from p0-reserve-name):
         "privacyfence.connectors.grist" to scripts/pyinstaller_common.py's list; "grist": "Grist" to both
         CONNECTOR_TITLES and CONNECTOR_SHORT in scripts/generate_tools_reference.py (after "confluence");
         GristConnector to CONNECTOR_CLASSES (and its import) in tests/unit/connectors/test_readme_manifest_alignment.py.
      4. Run python3 scripts/generate_tools_reference.py to regenerate docs/tools-reference.md (Grist row:
         2 tools, 2 auto, 0 review, 0 popup).
      5. Website and docs, required by tests/unit/test_website_connector_pages.py and test_website_connectors_page.py:
         a. The setup guide grist-setup.md in docs/, with the sections in plan §3.9 (describe all seven tools and both modes now;
            the feature ships as a whole).
         b. README.md "## Connectors" table: row "| Grist | List documents and tables; read records after review; add and update records, add tables and columns (nothing is deleted) |" after Confluence.
         c. website/connectors/grist/index.html, modelled on website/connectors/telegram/index.html (same head,
            meta pf-content-group connector, canonical/og URLs for /connectors/grist/). Copy: list straight away,
            read records after review, add and change only with approval, works with docs.getgrist.com, team
            sites and self-hosted Grist, you paste your own API key. Its "Set it up" button is exactly
            <a class="button primary" href="GUIDE_URL">Set it up</a>.
            where GUIDE_URL is https://github.com/privacyfence/privacyfence/blob/main/ followed by docs/ and the guide's
            file name grist-setup.md, with no space (written split here because test_docs_references_exist.py
            rejects a plan naming a doc path that does not exist yet).
         d. scripts/build_site.py: "/connectors/grist/": "connectors/grist/index.html" in PAGES (after telegram)
            and "grist-setup" in CONNECTOR_GUIDES.
         e. tests/unit/test_website_connector_pages.py CONNECTORS: "grist": ("/connectors/grist/", "grist-setup", "Grist").
         f. website/connectors/index.html: a Grist card in the same position as Grist's row in the
            tools-reference summary table (last), copying the Telegram card's markup with id="grist",
            data-connector="Grist", data-tools="2" data-auto="2" data-review="0" data-popup="0", the printed line
            "2 tools: 2 without a card · 0 reviewed · 0 need approval", links to /docs/tools-reference/#grist and
            /connectors/grist/; change the heading to "Twelve connectors, 122 tools, each with a fixed gate."
         g. website/how-it-works/index.html: "120 connector tools" → "122 connector tools".
         h. website/compare/mcp-gateways/index.html: "Its own eleven connectors" → "Its own twelve connectors".
         i. tests/unit/test_website_connectors_page.py: len(REFERENCE) == 12 and "Twelve connectors".
         j. website/_partials/other-connectors.html: add
            <li data-connector="grist"><a href="/connectors/grist/">Grist</a></li> after the Telegram line, and
            update the partial's header comment ("the other four") to the new count. The Grist page includes
            the partial with current="grist" exactly as the Telegram page does with current="telegram";
            scripts/build_site.py's _without_current_connector (l.304-310) raises BuildError without that line.
         k. docs/README.md: add "- [`grist-setup.md`](grist-setup.md)" after the telegram-setup.md line in the
            user-and-operator half's connector guide list (l.46-51), so test_website_docs_allowlist.py passes.
      6. Create tests/unit/connectors/test_grist_connector.py (module docstring; pytestmark unit): TestDispatch
         (unknown tool → ValueError); TestListDocuments and TestListTables (never call gated_call — use the
         gated_call_spy fixture pattern from tests/unit/connectors/test_salesforce_connector.py:75-84 and assert it
         stays empty; each writes an auto_accepted audit entry with the tool name; a GristClientError becomes
         RuntimeError; a bad doc_id raises before the client is called); TestToolDefinitions with
         GRIST_SIBLINGS = {"grist_list_documents": ["grist_list_tables"], "grist_list_tables": ["grist_list_documents"]}
         (shape as SALESFORCE_SIBLINGS) calling assert_tool_definitions_complete; TestEveryToolIsAudited calling
         assert_all_tools_leave_an_audit_trail.
      7. Run ruff check . and python3 -m pytest tests/unit -q (the whole unit suite: many cross-connector tests discover
         the new module), and python3 scripts/build_site.py --out /tmp/pf-site-check --no-docs --offline to confirm the
         site builds and its link check passes.
      Stop condition: if a unit test outside the files in touches fails because of the new connector module
      (another hand-maintained connector list this plan does not name), stop with status=blocked and name the test.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_connector_tool_annotations.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit -q passes
      - grep -n "| \[Grist\](#grist) | 2 | 2 | 0 | 0 |" docs/tools-reference.md matches
      - python3 scripts/build_site.py --out /tmp/pf-site-check --no-docs --offline exits 0
      - ruff check . passes
  - id: p3-get-records
    title: grist_get_records, the review-gated read
    depends_on: [p2-connector-listing]
    complexity: S
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/always-allow-rules-reference.md
    brief: |
      Read first: plan §3.3 (grist_get_records row, filter/sort/limit validation, _cell_text, its gated_call
      arguments) and §3.4; src/privacyfence/connectors/salesforce.py _get_record (l.403-447) as the pattern.
      1. In connectors/grist.py add grist_get_records to tool_specs() (description, params, sibling
         grist_list_tables) and call(); implement the filter/sort/limit validation helpers and _cell_text exactly
         as §3.3; fetch get_document and get_records through _fetch, then call gated_call with exactly the §3.3
         arguments including pii_scan_text=details_text; return the §3.3 dict. Update GRIST_SIBLINGS in the test
         (grist_list_tables also lists grist_get_records; grist_get_records lists grist_list_tables).
      2. auto_accept.py: TOOL_TO_GATE "grist_get_records": "review"; TOOL_TO_OPERATION "grist_get_records":
         "grist.read_records". policy/registry.py TOOL_TO_VERB "grist_get_records": Verb.READ. gate.py
         _TOOL_LAYOUT "grist_get_records": WIDE.
      3. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py (it lists
         every review and popup tool); then website/connectors/index.html Grist card →
         data-tools="3" data-auto="2" data-review="1" data-popup="0", printed line
         "3 tools: 2 without a card · 1 reviewed · 0 need approval", heading total 123 tools;
         website/how-it-works/index.html "123 connector tools".
      4. Tests in tests/unit/connectors/test_grist_connector.py: TestGetRecords — preview dict has exactly the
         keys Server, Document, Team, Table, Filter, Sort (Server first, equal to client.host) and no cell value (data minimization); details_text carries the
         cells; pii_scan_text == details_text; preview_tables headers start with "id"; args carry doc_id,
         table_id, filter, sort, limit; truncated prefix; empty result gives "(no records)" and no table; each
         filter/sort/limit ValueError message (gated_call_spy stays empty); GristClientError → RuntimeError.
         TestFieldCompleteness — a real GristClient with a faked _request returning a fully populated
         /api/docs/{id} and records response, run through grist_get_records, checked with
         assert_no_placeholder_fields(gated_call_spy[0]["preview"]) (pattern:
         tests/unit/connectors/test_confluence_connector.py TestFieldCompleteness).
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/policy/test_registry.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Grist\](#grist) | 3 | 2 | 1 | 0 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p4-record-writes
    title: grist_add_records and grist_update_records, popup-gated
    depends_on: [p3-get-records]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/always-allow-rules-reference.md
    brief: |
      Read first: plan §3.3 (records validation, column checks, the two tools' gated_call arguments) and §3.4;
      the popup patterns in src/privacyfence/connectors/contacts.py:305-342 and connectors/jira.py:806-839.
      1. connectors/grist.py: add grist_add_records and grist_update_records (specs, call(), siblings of each
         other). Order inside each: parse and validate args (ValueError) → _fetch get_document and list_columns
         (and get_records_by_id for update) → unknown/formula column and missing-id checks (ValueError) →
         gated_call(gate="popup", …) with the §3.3 arguments → the client write through _fetch → return the §3.3
         dict. The write must not run if gated_call raises.
      2. Tables: auto_accept TOOL_TO_GATE popup ×2; TOOL_TO_OPERATION grist.add_records / grist.update_records;
         registry TOOL_TO_VERB CREATE / UPDATE; write_effects.EFFECT_BY_TOOL with the two exact strings in §3.4
         (under a "# ── Grist ──" header like the others); gate._TOOL_LAYOUT WIDE for both.
      3. Regenerate docs/tools-reference.md (python3 scripts/generate_tools_reference.py) and
         docs/always-allow-rules-reference.md (python3 scripts/generate_always_allow_reference.py); website card → 5 tools: 2 auto, 1 review, 2 popup
         ("5 tools: 2 without a card · 1 reviewed · 2 need approval"), heading 125 tools; how-it-works 125.
      4. Tests: TestAddRecords and TestUpdateRecords — preview dict holds only the §3.3 keys; preview_tables
         shape; update's Current/New table and "→" details lines; gate == "popup"; filtered_data is None; the
         client write is called only after the spy returns and not at all when the spy raises
         (monkeypatch gated_call to raise RuntimeError); every validation message in §3.3 for records,
         unknown column, formula column, missing ids, duplicate ids — and gated_call_spy stays empty for each;
         added_ids/updated_ids returned. Extend GRIST_SIBLINGS.
         TestEveryToolIsAudited: build the connector on a MagicMock client whose get_document returns
         GristDocument("DOC1", "QA doc", "Home", "Personal"), list_columns returns [GristColumn("Name", "Name",
         "Text", False)], list_tables returns [GristTable("Table1", [GristColumn("Name", "Name", "Text", False)])],
         get_records_by_id returns [GristRecord(1, {"Name": "old"})], add_records returns [2], host is
         "docs.getgrist.com"; and pass arg_overrides {"grist_add_records": {"doc_id": "DOC1", "table_id": "Table1",
         "records": '[{"Name": "a"}]'}, "grist_update_records": {"doc_id": "DOC1", "table_id": "Table1",
         "records": '[{"id": 1, "fields": {"Name": "b"}}]'}} plus doc_id/table_id overrides for the read tools.
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy/test_registry.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Grist\](#grist) | 5 | 2 | 1 | 2 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p5-schema-writes
    title: grist_create_table and grist_add_columns, popup-gated
    depends_on: [p4-record-writes]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - tests/unit/connectors/test_grist_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - docs/tools-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/always-allow-rules-reference.md
    brief: |
      Read first: plan §3.3 (columns validation, type allowlist, the two tools' gated_call arguments), §3.4, §3.7.
      1. connectors/grist.py: add grist_create_table and grist_add_columns (specs, call(), siblings of each other)
         with the same ordering rule as the record writes: validate → _fetch get_document and list_tables
         (create_table) or list_columns via list_tables (add_columns) → existence checks → gated_call(gate="popup")
         → write → return the §3.3 dict (create_table returns the table id Grist assigned).
      2. Tables: TOOL_TO_GATE popup ×2; TOOL_TO_OPERATION grist.create_table / grist.add_columns; TOOL_TO_VERB
         RESTRUCTURE ×2; EFFECT_BY_TOOL with the exact §3.4 strings. No _TOOL_LAYOUT entry (NARROW default).
      3. Regenerate docs/tools-reference.md (python3 scripts/generate_tools_reference.py) and
         docs/always-allow-rules-reference.md (python3 scripts/generate_always_allow_reference.py); website card → 7 tools: 2 auto, 1 review, 4 popup
         ("7 tools: 2 without a card · 1 reviewed · 4 need approval"), heading 127 tools; how-it-works 127.
      4. Tests: TestCreateTable and TestAddColumns — preview keys, the Column/Label/Type table, default type Text,
         every type in the allowlist accepted (parametrize), Ref:Bad-Id and "DateTime" rejected with the exact
         message, existing table / missing table / existing column messages, no write when the gate raises.
         Extend GRIST_SIBLINGS. In TestEveryToolIsAudited's fake client, add_table returns "NewTable" and add_columns
         returns ["Count"]; arg_overrides add {"grist_create_table": {"doc_id": "DOC1", "table_id": "NewTable",
         "columns": '[{"id": "Title"}]'}, "grist_add_columns": {"doc_id": "DOC1", "table_id": "Table1",
         "columns": '[{"id": "Count", "type": "Int"}]'}}.
      5. Confirm no Grist tool sets destructive (test_connector_tool_annotations.py unchanged and passing).
      6. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy/test_registry.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Grist\](#grist) | 7 | 2 | 1 | 4 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q --cov=privacyfence.connectors.grist --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/connectors/grist.py
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p6-policy-scope
    title: The grist.document auto-accept scope, offered on Settings and the bridge but never by the popup
    depends_on: [p5-schema-writes]
    complexity: S
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/registry.py
      - docs/always-allow-rules-reference.md
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_propose.py
      - tests/unit/policy/test_registry.py
      - tests/unit/test_gate.py
    brief: |
      Read first: plan §3.4 "Rule scope", and how apps_script.project is wired: policy/scopes.py:515-572,
      policy/catalogue.py:1-60, the propose.py docstring's "What the popup deliberately does not propose",
      tests/unit/policy/test_scopes.py:395-420, tests/unit/policy/test_catalogue.py:20-70, tests/unit/test_gate.py:1040-1060.
      1. scopes.py: add _grist_document_matches (the _apps_script_project_matches shape on ctx.args["doc_id"]) and
         the "grist.document" entry in NEW_SCOPE_SELECTORS exactly as §3.4; extend the "Scope types with no old
         predicate" comment and the module docstring's NEW_SCOPE_SELECTORS sentence to name grist.document.
      2. catalogue.py: add the "grist.document" PolicyExtraScope exactly as §3.4; update the docstrings that say
         EXTRA_SCOPES covers "three operation groups" to name Grist's five operation keys too.
      3. propose.py docstring "What the popup deliberately does not propose": add a bullet that grist.read_records,
         add_records, update_records, create_table and add_columns are governed by grist.document, a scope with no
         entry here. registry.py docstring's sentence listing what policy/catalogue.py offers: add grist.document.
         No PROPOSABLE_SCOPES entry.
      4. python3 scripts/generate_always_allow_reference.py to regenerate docs/always-allow-rules-reference.md.
      5. Tests: test_scopes.py — grist.document matches the doc_id from args, not another doc_id, and an empty
         value never matches. test_catalogue.py — the grist.document catalogue entry exists with verbs read,
         create, update, restructure; rules_for_catalogue_entry("grist.document", ["DOC1"], [Verb.READ, Verb.CREATE])
         compiles to grist.read_records and grist.add_records with predicate grist.document; an empty value is
         rejected like apps_script.project's. test_gate.py — next to the apps_script.project case: a stored
         grist.document rule auto-accepts grist_get_records for that doc_id and not for another.
         test_propose.py::test_the_extra_scope_operations_stay_unproposed (l.261): add the five Grist tools with
         make_ctx(connector="grist", args={"doc_id": "DOC1", "table_id": "Table1"}) to its tuple.
         test_registry.py: add the five grist.* operation keys to _GRANT_MANIFEST_UNREACHABLE_OPERATIONS (l.31) and
         update its comment ("six" → "eleven", and name grist.document among the EXTRA_SCOPES).
      6. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if propose.proposals_for returns a proposal for a Grist tool without a PROPOSABLE_SCOPES
      entry, stop with status=blocked (plan §6).
    acceptance:
      - python3 -m pytest tests/unit/policy -q passes
      - python3 -m pytest tests/unit/test_gate.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n '"grist.document"' src/privacyfence/policy/scopes.py src/privacyfence/policy/catalogue.py matches in both files
      - grep -c "grist" src/privacyfence/policy/propose.py prints a number of at least 1, and grep -n '_scope("grist' src/privacyfence/policy/propose.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p7-local-settings
    title: Build the connector in the daemon and connect it from the local Settings page
    depends_on: [p2-connector-listing]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read first: plan §3.5 (the spec), §3.2 (load_token_file, effective_server_url, save_token_file), the
      Salesforce and Telegram blocks of daemon_main.build_connectors (l.1466-1481, 1543-1576), and the Telegram
      settings flow (settings_controller.py:1420-1535, settings_window_html.py:1355-1440 and 1499-1573).
      1. daemon_main.py: TOKEN_FILES["grist"]; imports of GristClient, GristClientError, GristConnector,
         effective_server_url and load_token_file as load_grist_token; the build_connectors block exactly as §3.5.
      2. settings_controller.py: ALL_CONNECTORS gains "grist"; _connectors_state has_org True for grist;
         self._grist_auth initialised to None next to _telegram_auth; grist_connect and grist_cancel_auth as §3.5;
         snapshot() gains grist_auth and grist_server_url_pinned. The key is never stored on self, never put in
         the snapshot and never logged.
      3. web/org_settings_scope.py and web/routes_settings.py: the two actions as §3.5 (LOCAL_MODE, non-sensitive).
      4. settings_window_html.py: the Grist modal as §3.5 (data-grist-auth row, fields, pinned text, Connect/Cancel,
         error line, key cleared after submit), reusing the Telegram modal's CSS classes and helper functions.
      5. Tests: test_daemon_main.py — grist built when a valid token file exists (client.check_connection
         monkeypatched); missing file → failures["grist"] == "not_authenticated"; org mode without a grist bundle
         section → "no_org_config"; pinned URL different from the token's → "not_authenticated"; disabled →
         no entry. test_settings_controller.py — grist_connect with empty key sets the error; a bad URL sets the
         normalize error; success (GristClient.check_connection monkeypatched, run synchronously the way the
         Telegram tests drive _run_async) writes credentials/grist_token.json under data_dir() with both keys
         and calls refresh_connectors; check_connection failure sets the error and writes nothing; a pinned
         bundle URL wins over the submitted one; the snapshot never contains the key (assert the key string is
         not in json.dumps(snapshot)). web/test_routes_settings.py — both actions allowed and classified
         (TestSensitiveActionsCoverAllAllowedActions passes), and a POST to grist_connect reaches the controller.
         test_settings_window_html.py — the rendered script contains data-grist-auth and an input of type password
         for the key. Update any assertion that pins the exact connector list or count to include grist.
      6. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: plan §6 "Settings tests that enumerate connectors".
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/test_settings_window_html.py -q passes
      - 'grep -n ''"grist": "credentials/grist_token.json"'' src/privacyfence/daemon_main.py matches'
      - grep -n '"grist_connect"' src/privacyfence/web/org_settings_scope.py src/privacyfence/web/routes_settings.py matches in both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p8-org-mode
    title: Org bundle grist section and the per-person API-key form on /connect
    depends_on: [p7-local-settings]
    complexity: M
    touches:
      - scripts/build_org_bundle.py
      - src/privacyfence/web/routes_connect.py
      - docs/configuration-reference.md
      - tests/unit/test_build_org_bundle.py
      - tests/unit/web/test_routes_connect.py
    brief: |
      Read first: plan §3.6 (the spec), web/routes_connect.py (the whole module, in particular the Telegram routes
      and _telegram_box_html), scripts/build_org_bundle.py's Salesforce option group (l.188-193, 505-511, 692).
      1. scripts/build_org_bundle.py: a "Grist" argument group with --grist-server-url, validation and section
         writing exactly as §3.6; "grist" in the services tuple at l.692; "grist": () in _CONNECTOR_CALLBACKS.
         docs/configuration-reference.md "Build options" table: a row
         "| `--grist-server-url URL` | none | `grist.server_url` | The Grist server people connect to (https only). Each person pastes their own API key; see [Grist setup](grist-setup.md). |"
         after the Atlassian rows (tests/unit/test_docs_configuration_reference.py requires every option documented).
      2. web/routes_connect.py: rename _check_telegram_post to _check_form_post (update its callers);
         _is_configured/_is_connected for grist; SERVICE_LABELS gets "grist": "Grist" if the page uses it;
         _grist_box_html; the grist_connect handler and Route("/connect/grist", …, methods=["POST"]) exactly as
         §3.6, with per-principal errors kept beside telegram_states. The check_connection call runs in
         asyncio.to_thread. The key never goes into a log line, an error message or the redirect.
      3. Tests: test_build_org_bundle.py — --grist-server-url writes {"grist": {"server_url": ...}} with the trailing
         slash stripped; an http:// or host-less URL exits with the exact message; a Grist-only bundle is written;
         an org-mode build (--mode org with the existing tests' signing and IdP arguments) that includes
         --grist-server-url completes and prints no Grist redirect URI. web/test_routes_connect.py's service-row
         count assertion (l.170) becomes 12.
         web/test_routes_connect.py — the connect page shows "Not set up by your organization" without the bundle
         section and the key form with it (pinned server shown); POST without CSRF → 401, cross-origin → 403,
         signed out → the signed-out redirect; empty key → error shown on the next GET; success (check_connection
         monkeypatched) writes user_dir(principal)/credentials/grist_token.json with the pinned server_url, evicts
         the principal's connectors and shows Connected; check_connection failure writes nothing and shows the
         error; the key does not appear in any response body.
      4. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/web/test_routes_connect.py tests/unit/test_docs_configuration_reference.py -q passes
      - grep -n '"/connect/grist"' src/privacyfence/web/routes_connect.py matches
      - grep -n "_check_telegram_post" src/privacyfence/web/routes_connect.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p9-qa-recorder
    title: Live check, lifecycle and recorded fixtures for Grist
    depends_on: [p5-schema-writes, p7-local-settings]
    complexity: M
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/qa_environment.yaml.example
      - tests/fixtures/live/grist/**
      - tests/unit/test_grist_client.py
      - src/privacyfence/grist_client.py
      - docs/connector-qa.md
    brief: |
      Read first: docs/connector-qa.md (whole), docs/testing-policy.md "Layer 5", scripts/qa_fixture_recorder.py
      check_confluence (l.811-865), lifecycle_confluence (l.2195-2240), RawCapture (l.595-628), and the
      `.claude/skills/steward/SKILL.md` notes on qa-record-fixture.yml.
      1. scripts/qa_fixture_recorder.py:
         - _build_grist_client(): org_config = daemon_main.load_org_config(); pinned = (org_config.get("grist") or {}).get("server_url", "");
           token = grist_client.load_token_file(daemon_main._resolve_path(daemon_main.TOKEN_FILES["grist"]));
           return GristClient(effective_server_url(token, pinned), token["api_key"]).
         - check_grist(record, manifest): cfg = manifest.get("grist") or {}; doc_id (required; missing → a failed
           CheckResult "grist.doc_id missing from qa_environment.yaml"), table_id default "QaSeed", seed_record_name
           default "PrivacyFence QA seed [QATEST]". Three CheckResults recorded through RawCapture:
           list_documents ("list_documents.json": only the workspace containing doc_id, with only that doc, then
           deidentify_structural_fields(redact(...))), list_tables ("list_columns.json": the raw columns response for
           table_id), get_records ("get_records.json": the raw records response, ok only when every returned row's
           Name contains [QATEST], refusing to record otherwise).
         - lifecycle_grist(manifest): add one record {"Name": f"{LIFECYCLE_TAG} grist row {suffix}", "Note": "created by
           qa_fixture_recorder.py --lifecycle"}, read it back by id, update Note to "updated", read back; no delete
           (the client has none, plan §3.7) — docstring says rows accumulate and are cleaned by hand, like
           lifecycle_confluence. LifecycleResult("grist", ok, note, cleanup_ok=None).
         - Register "grist" in CONNECTOR_CHECKS, EXPECTED_FIXTURES ("list_documents.json", "list_columns.json",
           "get_records.json") and LIFECYCLE_CHECKS.
      2. tests/fixtures/qa_environment.yaml.example: a grist section (doc_id: "", table_id: QaSeed,
         seed_record_name: "PrivacyFence QA seed [QATEST]") with comments in the file's style.
      3. docs/connector-qa.md: Grist row in the QA accounts table (a free docs.getgrist.com account; the key file is
         written by hand, {"server_url", "api_key"}, chmod 600); "### Seed: Grist" checklist matching the
         manual_before step mb1 (document "PrivacyFence QA [QATEST]", table QaSeed with Name and Note, two [QATEST]
         rows, a contrast document); the Manifest reference row; a sentence in "Authenticating connectors" that
         Grist has no OAuth step and its credential file is written by hand; "### Grist checks" in the exploratory
         section (Settings form, review card for get_records, popup cards for the four writes, the
         grist.document rule from Settings auto-accepts a read).
      4. Commit and push this phase branch, then dispatch .github/workflows/qa-record-fixture.yml against it with
         input connector=grist (GitHub MCP actions_run_trigger, ref = this phase branch). A queued run is
         waiting on the connector-live-check concurrency group: wait, do not re-dispatch. When it succeeds, git pull.
         Review every file under tests/fixtures/live/grist/ before continuing: no real e-mail, name other than
         the QA placeholders, team domain, API key or non-[QATEST] content. If something leaks, extend the
         redaction in check_grist, push and dispatch again.
      5. tests/unit/test_grist_client.py: TestLiveFixtureParsing replaying the three fixtures through the real
         parsers (list_columns, get_records via a faked _request), skipping with the record hint when a file is
         missing, exactly like tests/unit/test_salesforce_client.py:1316-1360. If a fixture shows a shape the
         parser mishandles, fix grist_client.py and its unit test (plan §6).
      6. tests/unit/test_qa_fixture_recorder.py: check_grist and lifecycle_grist against a fake client
         (pattern: the existing per-connector tests there); TestFixturePresence passes with the recorded files.
      7. ruff check . and python3 -m pytest tests/unit -q. New text in docs/connector-qa.md and the recorder follows
         plan §3.0 (no phase ids or issue numbers; test_docs_no_history.py scans it).
      Stop condition: the dispatched run fails at its QA-state copy step or with "Grist is not authenticated" —
      manual step mb2-runner-qa-state is not done; stop with status=blocked (plan §6).
    acceptance:
      - ls tests/fixtures/live/grist/ lists list_documents.json, list_columns.json and get_records.json
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py tests/unit/test_grist_client.py -q passes, with TestLiveFixtureParsing not skipped
      - python3 -c "import sys; sys.path.insert(0,'scripts'); import qa_fixture_recorder as q; assert 'grist' in q.CONNECTOR_CHECKS and 'grist' in q.LIFECYCLE_CHECKS" exits 0
      - The qa-record-fixture.yml run for connector=grist on this phase branch concluded success (URL in the final report)
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p10-docs-adrs-retire
    title: Reference docs, changelog, the four ADRs, and retiring the plan
    depends_on: [p6-policy-scope, p8-org-mode, p9-qa-recorder]
    complexity: S
    touches:
      - docs/grist-setup*.md
      - docs/configuration-reference.md
      - docs/README.md
      - docs/approvals-and-policy.md
      - CHANGELOG.md
      - docs/adr/0142-grist-connects-with-a-pasted-personal-api-key.md
      - docs/adr/0143-the-grist-server-is-the-users-in-local-mode-and-the-organizations-in-org-mode.md
      - docs/adr/0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md
      - docs/adr/0145-the-grist-connector-only-adds.md
      - docs/adr/README.md
      - docs/grist-connector-plan.md
      - docs/grist-connector-plan-manual-steps.html
      - scripts/build_site.py
    brief: |
      Read first: docs/adr/README.md (template and rules), plan §3 and §4, docs/configuration-reference.md
      (l.95-110 and 219-260), CHANGELOG.md's "## [Unreleased]" section.
      1. The setup guide grist-setup.md in docs/: check every statement against the code as it now is (error texts from grist_client.py,
         the Settings labels, the /connect wording) and correct it.
      2. docs/configuration-reference.md: add grist to the connectors.<name>.enabled list (l.103); add the
         --grist-server-url row to "Build options" and Grist to the sentence listing per-service guides
         ("Grist needs only a server address; each person pastes their own key, see Grist setup").
         (The --grist-server-url row is already there.)
         docs/approvals-and-policy.md: in the scope table that has the "Apps Script project" row (l.386), add
         "| Grist document | identity | document ids | `grist.document` | the document is one of these |" after it.
      3. CHANGELOG.md under "## [Unreleased]" (never a version heading): one Added line —
         "Grist connector: list documents and tables, read records after review, and add or update records and add
         tables and columns with approval, on docs.getgrist.com, team sites or a self-hosted server. Connect with
         your own API key in Settings, or on the connections page in organization mode. Nothing is deleted."
      4. Write ADRs 0142–0145 from plan §4 with the docs/adr/README.md template (Status "Accepted — <today's date>.
         Implemented.", Context, Decision, Alternatives considered, Consequences, Verification naming the tests that
         enforce each, Related). Link source files and ADRs 0019, 0070, 0072, 0077, 0115 where relevant; never link
         the plan. Add the four rows to the index in docs/adr/README.md.
      5. git rm docs/grist-connector-plan.md docs/grist-connector-plan-manual-steps.html; remove
         "grist-connector-plan.md" from scripts/build_site.py CONTRIBUTOR_DOCS and its line from docs/README.md's
         contributor half; grep -rn "grist-connector-plan" . --exclude-dir=.git must find nothing.
      6. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py leave no
         diff; ruff check . and python3 -m pytest tests/unit -q pass.
      7. Push this phase branch and dispatch .github/workflows/connector-live-check.yml (no inputs) against it with the
         GitHub MCP actions_run_trigger: it carries every phase, so it is the definition-of-done live check for
         src/privacyfence/*_client.py and connectors/**. A queued run is waiting on the connector-live-check
         concurrency group: wait, never re-dispatch. Put the run URL in your final report. If it opens or updates the
         chore/connector-live-fixture-drift PR for another connector, leave that PR alone (steward skill) and mention it.
    acceptance:
      - ls docs/adr/0142-*.md docs/adr/0143-*.md docs/adr/0144-*.md docs/adr/0145-*.md lists four files, each containing "Accepted"
      - grep -c "014[2-5]" docs/adr/README.md prints at least 4
      - test ! -e docs/grist-connector-plan.md && test ! -e docs/grist-connector-plan-manual-steps.html
      - grep -rn "grist-connector-plan" . --exclude-dir=.git finds nothing
      - grep -n "Grist connector" CHANGELOG.md matches a line below "## [Unreleased]" and above the next "## [" heading
      - The connector-live-check.yml run on this phase branch concluded success, or failed only in another connector's row (URL in the final report)
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
```
