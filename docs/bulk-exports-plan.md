# Bulk exports to a file: Grist records, Grist SQL and Salesforce reports

## Goal

A scheduled sync job reads three Grist documents and three Salesforce reports every week, then
writes changes back to Grist with `grist_update_csv`. Today each read is a page of at most 500
Grist records or 2,000 Salesforce rows. Every page needs its own approval card (13 for one Grist
table), and every row passes through the model's context. After this change:

1. **`grist_export_records`** writes a whole Grist table, optionally filtered, to a CSV or JSON
   file. It needs one review card. The tool result holds only `{rows, columns, sha256}` and where
   the file landed.
2. **`salesforce_export_report`** does the same for a Salesforce report. It pages through the
   report internally with `page_by`. It takes the same `columns` and `filters` as
   `salesforce_run_report`, so an incremental pull (`LAST_UPDATE greaterOrEqual <last sync>`) and
   a slim IDs-only pull (`columns`) need no new parameter.
3. **Auto-accept for the scheduled run** needs no new code. A Grist document rule (read and
   update), an approved-report rule and an approved Drive folder rule already exist, and the new
   tools map to the same operation keys as `grist_get_records` and `salesforce_run_report`, so
   those rules cover them. The rules are set up after the merge (`manual_after`). The PII gate
   stays as it is, by the user's decision. An export whose content trips the PII scan still gets a
   card, and an unattended session gets an immediate `denied_unattended` (ADR 0015). The skill's
   own stop rules are the skill's business, not PrivacyFence's.
4. **`grist_export_sql`** runs one read-only `SELECT` through Grist's SQL endpoint and writes the
   result to a file, so a join across tables needs one card instead of three exports.

There is no issue; the request came from the user in a session.

## Current state

- `src/privacyfence/connectors/grist.py:381-426`: `grist_get_records` reads one page of at most
  500 records (`_LIMIT_ERROR`, line 58) behind a `review` card (`_get_records`, lines 612-688).
  Paging uses `after_id`/`next_after_id` (ADR 0146), one card per page.
- `src/privacyfence/grist_client.py:288-318`: `GristClient.get_records` asks Grist for
  `after_id + limit + 1` rows with `sort=id` and cuts at `limit`. With `after_id=0` and
  `limit=50_000` it is one request for up to 50,001 rows. The client does not cap `limit`; only
  the connector does. Every request has the client's own 30-second HTTP timeout (`_TIMEOUT`,
  line 39).
- `src/privacyfence/connectors/salesforce.py:283-333, 449-569`: `salesforce_run_report` returns one
  run of at most 2,000 detail rows. With `page_by` and `cursor` it reads one keyset page per call,
  one card each (ADR 0133). `SalesforceClient.run_report_page` (`salesforce_client.py:808-842`)
  does one page and raises `ReportPagingError` past `report_max_pages` (default 50). A paged run
  is flattened to one table, factMap key `"T!T"`, with `reportMetadata.detailColumns` as the
  columns. Each `dataCells` entry has `value` and `label`. `value` is raw: text, a number,
  `{"amount", "currency"}` for currency, the record id for name columns. `label` is the text
  Salesforce shows.
- `filters` on `salesforce_run_report` already takes `greaterOrEqual` and is ANDed onto the saved
  filters (`_parse_report_filters`, `salesforce.py:203-228`).
- Local file delivery is `local_files.deliver_file(dest_dir, name, data, mime_type, *,
  download_mode)` (`local_files.py:423-459`). It writes directly when the daemon is the user
  (`_deliver_direct`, which overwrites a file of the same name). Otherwise it stages the bytes for
  the `.mcpb` shim (`_deliver_bridge`; the shim never overwrites and picks `name (1).csv`), or it
  hands back a one-time link (`_deliver_link`). It returns `{path, name, size_bytes, delivery, …}`.
  It refuses data over `file_bridge.max_download_bytes` (200 MB; the check at line 448 reads the
  private `_max_download_bytes`). `drive_download_file` (`connectors/drive.py:1121-1312`) is the
  pattern: build everything, call `gated_call(..., delivery=...)`, then deliver.
- Policy: `auto_accept.TOOL_TO_OPERATION` (`auto_accept.py:129-137`) and `TOOL_TO_GATE`
  (`auto_accept.py:273, 318-324`) map tools to operation keys and gates.
  `policy/registry.py:190-198` maps tools to verbs, and `TOOL_REGISTRY` is built from those three
  maps. `gate.py:258-260` gives the Grist and Salesforce tools the `WIDE` card layout. A Grist
  rule is a `grist.document` scope matching `args["doc_id"]` (`policy/scopes.py:552-556`,
  ADR 0144). A report rule is `approved_report_ids`, matching `args["report_id"]`
  (`policy/scopes.py:321-325`). `policy/propose.proposals_for` offers "Always allow → this report"
  for any tool with operation `salesforce.run_report` and verb READ (`propose.py:367`), and
  nothing for Grist (ADR 0144).
- The PII gate (`gate.py:76-91`, branch at `gate.py:1046-1091`): on a `review` gate, a PII match in
  `pii_scan_text` overrides a matching rule. In an unattended session that becomes
  `denied_unattended`. `card_builder.fill_agent_placeholder` replaces `{agent}` in `new_info` rows
  only, not in `details_text`.
- `GristConnector.download_mode` is set in `daemon_main.build_connectors`
  (`daemon_main.py:1586`). `download_mode` is computed at line 1305. `SalesforceConnector` has no
  `download_mode`; it is built at line 1477.
- Grist's SQL endpoint: `POST /api/docs/{docId}/sql` with body `{"sql": "...", "args": [...]}`
  returns `{"statement": "...", "records": [{"fields": {...}}]}`. Grist allows it only for a caller
  who may copy the whole document; anyone else gets HTTP 403. Cells come back as SQLite stores
  them (dates as epoch seconds, booleans as 0/1, lists as JSON text). A full-copy caller can also
  read the document's internal tables (`_grist_*`, `_gristsys_*`), which include the action
  history with old and deleted values. ADR 0146 rejected the endpoint *for paging
  `grist_get_records`*; the user has now asked for it as a separate export tool. The QA runner
  connects with an API key (`docs/connector-qa.md:37`), so CI can show that the endpoint works,
  but not whether the OAuth scope (`grist_auth.GRIST_SCOPES`) covers it. That check goes in ma2.
- Tests that pin tool sets or counts by hand:
  - `tests/unit/test_website_connectors_page.py` checks `website/connectors/index.html` (each
    card's `data-tools/-auto/-review/-popup` and the "N tools" total, today 129; Grist card 9/2/1/6,
    Salesforce card 4/1/3/0) and `website/how-it-works/index.html` ("Each of the 129 connector
    tools", line 54) against the generated tools reference.
  - `TestEveryToolIsAudited` in `tests/unit/connectors/test_grist_connector.py:827` and
    `tests/unit/connectors/test_salesforce_connector.py:920` calls every tool through
    `tests/helpers.py:assert_all_tools_leave_an_audit_trail`, with `"stub"` for every required
    string unless `arg_overrides` says otherwise.
  - `tests/unit/policy/test_propose.py:273-276` (`test_the_extra_scope_operations_stay_unproposed`)
    lists the Grist tools.
  - `tests/unit/test_qa_fixture_recorder.py`: `_FakeGristApi` (line ~2955) raises `AssertionError`
    on any request it does not know, and `test_records_three_fixtures` (line 3022) pins the list of
    Grist check rows.
  - These are derived and need no edit: `test_systemic_gate_invariants.py`,
    `test_connector_tool_annotations.py`, `test_readme_manifest_alignment.py`, `test_registry.py`.
- Generated reference docs: `scripts/generate_tools_reference.py` writes `docs/tools-reference.md`
  and `scripts/generate_always_allow_reference.py` writes `docs/always-allow-rules-reference.md`.
  `tests/unit/test_docs_tools_reference.py` and `tests/unit/test_generate_always_allow_reference.py`
  fail when either is stale.
- Pattern commits: `2dae2772` (adding `grist_import_csv`/`grist_update_csv`: connector, registries,
  tests, the daemon_main `download_mode` assertion) and `95e85f8e` (their docs, website counts,
  ADR and changelog).
- `tests/unit/test_code_no_history.py` rejects plan references in code. No comment or docstring
  may cite this plan or its sections.

## Design

### 3.1 New module `src/privacyfence/export_files.py` (pure, no I/O) and one `local_files` helper

```python
EXPORT_MAX_ROWS = 50_000
SAMPLE_ROWS = 20
SCAN_CHARS = 1_000_000
_SAMPLE_CELL_CHARS = 200

FILE_NAME_ERROR = "file_name must be a plain file name ending in .csv or .json, such as Contacts.csv."
DESTINATION_ERROR = "destination_dir must be a folder path, absolute or starting with ~/."
MIME_TYPES = {"csv": "text/csv", "json": "application/json"}

def export_file_name(file_name: str, default_stem: str) -> tuple[str, str]: ...
def check_destination(destination_dir: str) -> str: ...
def refuse_org(tool: str, download_mode: str, alternative: str) -> None: ...
def csv_cell(value: Any) -> str: ...
def render(columns: list[str], rows: list[list[Any]], fmt: str) -> bytes: ...
def sample_table(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]: ...
def scan_text(data: bytes) -> str: ...
def card_details(preview: dict[str, str], rows: int, columns: list[str], saved_to: str, sha256: str) -> str: ...
def export_summary(columns: list[str], rows: list[list[Any]], sha256: str, delivered: dict[str, Any]) -> dict[str, Any]: ...
```

- `export_file_name`: strip. Empty becomes `f"{default_stem}.csv"`. Raise `ValueError(FILE_NAME_ERROR)`
  when the name contains `/`, `\` or `\x00`, starts with `.`, is longer than 200 characters, or its
  lower-cased suffix is not `.csv` or `.json`. Return `(name, "csv" | "json")`. The suffix picks
  the format, so there is no `format` parameter.
- `check_destination`: return `destination_dir.strip()`. Raise `ValueError(DESTINATION_ERROR)` when
  that is empty. The stripped value is what the connector passes to `deliver_file` (ADR 0007 G1:
  never expanded by the daemon).
- `refuse_org`: when `download_mode == "org"`, raise `ValueError(f"{tool} writes a file on your
  computer, which an organization-managed install cannot do. Use {alternative} instead.")`.
- `csv_cell`: `None` → `""`; `bool` → `"true"`/`"false"`; `int` → `str(v)`; `float` →
  `str(int(v))` when `v.is_integer()`, else `repr(v)`; `str` → itself; anything else (list, dict) →
  `json.dumps(v, ensure_ascii=False)`.
- `render`: for `"csv"`, a header row of `columns`, then one row per entry of `rows` through
  `csv_cell`. It uses `csv.writer(buf, lineterminator="\n")` over an `io.StringIO` and encodes UTF-8
  with no BOM. For `"json"`, `json.dumps([dict(zip(columns, row)) for row in rows],
  ensure_ascii=False, separators=(",", ":")) + "\n"`, encoded UTF-8.
- `sample_table`: `{"caption": "First rows", "headers": columns, "rows": [[short(csv_cell(v)) for v in
  row] for row in rows[:SAMPLE_ROWS]], "footer": f"Showing {k} of {n} rows."}`. `short` cuts a cell
  over 200 characters to its first 200 plus `…`.
- `scan_text`: `data.decode("utf-8", errors="replace")[:SCAN_CHARS]`.
- `card_details`: lines `f"{key}: {value}"` for each `preview` entry, then `f"Rows: {rows:,}"`,
  `f"Columns: {', '.join(columns)}"`, `f"Saved to: {saved_to}"`, `f"SHA-256: {sha256}"`, joined
  by `"\n"`. It never contains `{agent}`.
- `export_summary`: `{"rows": len(rows), "columns": columns, "sha256": sha256, **delivered}`.
  `delivered` is `deliver_file`'s return value: `path`, `name`, `size_bytes`, `delivery` and, for
  a link, `download_url`, `expires_at` and `note`.

`src/privacyfence/local_files.py` gains a public `check_delivery_size(size: int) -> None`. It raises
the same `LocalFileAccessError` text `deliver_file` raises today. `deliver_file` calls it in place
of its inline check, so the message keeps one source. It goes into `__all__`.

### 3.2 The shared export flow (every export tool, in this order)

1. Validate every argument and call `export_files.refuse_org` before any network call. A doomed
   call never costs an approval.
2. Fetch every row. Refuse more than `EXPORT_MAX_ROWS` rows (Grist), or past
   `salesforce.report_max_pages` (Salesforce; the client already raises it).
3. Build `columns` and `rows`. Then `data = await asyncio.to_thread(export_files.render, columns,
   rows, fmt)` and `sha256 = hashlib.sha256(data).hexdigest()`, so up to 100,000 rows never
   serialise on the event loop. Then `local_files.check_delivery_size(len(data))`, before the card.
4. `await gated_call(gate="review", filtered_data=None, ...)`, with:
   - `raw_data` holding only identifiers, counts, column names and `sha256`, never a cell value,
     because it can reach the audit trail (ADR 0148 precedent);
   - `preview_tables=[export_files.sample_table(columns, rows)]`;
   - `pii_scan_text=export_files.scan_text(data)`. The PII gate is unchanged;
   - `new_info`, exactly these four entries. The two `{agent}` strings are plain literals, not
     f-strings:
     `"Rows": f"{n:,}"`, `"Columns": str(len(columns))`,
     `"Content returned to {agent}": "None in the tool result: the rows are written to the file below, where {agent} can open it"`,
     `"Saved to": saved_to`, where `saved_to = f"{destination_dir.rstrip('/')}/{file_name}"`;
   - `details_text=export_files.card_details(preview, n, columns, saved_to, sha256)`;
   - `delivery="local_disk" if local_files.can_access_user_files(self.download_mode) else
     "client_bridge"` (the audit record of the decision, as `drive_download_file` does);
   - `args` holding every tool argument except `reason`, plus `"sha256": sha256`, so an approval is
     bound to the exact content (ADR 0073).
5. Only after the gate: `delivered = local_files.deliver_file(destination_dir, file_name, data,
   export_files.MIME_TYPES[fmt], download_mode=self.download_mode)`. Then return
   `export_files.export_summary(columns, rows, sha256, delivered)`.

### 3.3 `grist_export_records`

`ToolSpec(name="grist_export_records", read_only=True)`. Params, in order: `doc_id`
(`_DOC_ID_DESCRIPTION`), `table_id` (`_TABLE_ID_DESCRIPTION`), `filter` (the `grist_get_records`
filter description, moved into a module constant `_FILTER_DESCRIPTION` that both specs use),
`destination_dir` (required str), `file_name` (str, optional, default `""`), `_REASON`.

- The `destination_dir` description, a module constant `_DESTINATION_DIR` used by all three
  export tools: `"Folder on the user's computer to write the file to, absolute or starting with
  ~/."`
- `file_name` description: `"File name ending in .csv or .json; the ending picks the format.
  Empty means <table_id>.csv."`
- Description: `"Write every record of one table of a Grist document, optionally filtered, to a
  CSV or JSON file on the user's computer, in one approval. The first column is the record id
  (id), then the table's columns by column id; dates are written as YYYY-MM-DD and choice lists
  as JSON arrays. To write changes back with grist_update_csv (key_column id, up to 20000 rows),
  keep only id and the data columns you change: formula and list columns cannot be written. Up
  to 50000 records. Returns {rows, columns, sha256, path, name, size_bytes, delivery}, never the
  records; read the file at path, which may be a numbered name if a file of that name exists. Use
  grist_get_records to look at a few records. Not available on an organization-managed install.
  Requires user approval."`
- Flow:
  1. `validate_doc_id` and `validate_identifier(table_id, "table")`; a `GristClientError` becomes
     a `ValueError`, as in `_get_records`.
  2. `_parse_filter(filter)`.
  3. `destination_dir = export_files.check_destination(destination_dir)`.
  4. `file_name, fmt = export_files.export_file_name(file_name, table_id)`.
  5. `export_files.refuse_org("grist_export_records", self.download_mode, "grist_get_records")`.
  6. `doc = await self._doc_info(doc_id)`.
  7. `columns = await self._fetch(self._client.list_columns, doc_id, table_id)`.
  8. `page = await self._fetch(self._client.get_records, doc_id, table_id, filters=filters,
     limit=export_files.EXPORT_MAX_ROWS)`. If `page.truncated`, raise `ValueError("More than
     50,000 records match; narrow the export with filter.")`.
- Columns: `["id", *[c.id for c in columns]]`. Row: `[record.id, *[_export_value(record.fields.get(c.id),
  c.type) for c in columns]]`. `_export_value` is a new module-level helper in `grist.py`:
  - a `Date` column with a number (not bool): `(_EPOCH + timedelta(seconds=value)).date().isoformat()`;
  - a column whose type starts with `DateTime`, with a number (not bool): `(_EPOCH +
    timedelta(seconds=value)).isoformat().replace("+00:00", "Z")`;
  - a list whose first element is `"L"` (ChoiceList, RefList): `value[1:]`;
  - anything else: unchanged.
- `gated_call` (§3.2), with these arguments:
  - `tool="grist_export_records"`, `tool_name="Export Grist Records"`;
  - `summary=f"Export {n} record(s) from {doc.name} / {table_id} to {file_name}"`;
  - `sender=doc.name`;
  - `preview={"Server": self._client.host, "Document": doc.name, "Team": doc.team or _NO_TEAM,
    "Table": table_id, "Filter": filter or "(none)"}`;
  - `raw_data={"doc_id": doc_id, "table_id": table_id, "rows": n, "columns": columns, "sha256":
    sha256}`;
  - `my_email=""`;
  - `args={"doc_id": doc_id, "table_id": table_id, "filter": filter, "destination_dir":
    destination_dir, "file_name": file_name, "sha256": sha256}`.
- Policy: `TOOL_TO_OPERATION["grist_export_records"] = "grist.read_records"`, `TOOL_TO_GATE[...] =
  "review"`, `registry.py` verb `Verb.READ`, `gate.py` layout `WIDE`. A `grist.document` read rule
  for the document therefore covers it.

### 3.4 `salesforce_export_report`

The four `salesforce_run_report` parameter descriptions for `report_id`, `columns`, `filters` and
`page_by` move into module constants (`_REPORT_ID_DESCRIPTION`, `_COLUMNS_DESCRIPTION`,
`_FILTERS_DESCRIPTION`, `_PAGE_BY_DESCRIPTION`) with their text unchanged. Both specs use them.

`ToolSpec(name="salesforce_export_report", read_only=True)`. Params, in order: `report_id`,
`columns` (optional, default `""`), `filters` (optional, default `""`), `page_by` (optional,
default `""`), `cell_format` (optional, default `"value"`), `destination_dir` (required,
`grist.py`'s `_DESTINATION_DIR` text copied into a constant here), `file_name` (optional, default
`""`; description as §3.3 but ending `Empty means <report_id>.csv.`), `reason`.

- `cell_format` description: `"value (default) writes Salesforce's raw value: dates as
  YYYY-MM-DD, amounts as numbers, record ids for name columns. label writes the text Salesforce
  shows, such as $1,200.00."` Any other value raises `ValueError("cell_format must be value or
  label.")`.
- Description: `"Write every row of a Salesforce report to a CSV or JSON file on the user's
  computer, in one approval. Columns are the report's column API names. columns and filters
  narrow this export as on salesforce_run_report: for example [{\"column\": \"LAST_UPDATE\",
  \"operator\": \"greaterOrEqual\", \"value\": \"2026-10-04\"}] exports only rows changed since
  that date. A report of more than 2,000 rows, or a grouped report, needs page_by; the export
  then reads it page by page internally. Returns {rows, columns, sha256, path, name, size_bytes,
  delivery}, never the rows; read the file at path. Not available on an organization-managed
  install. Requires user approval."`
- Flow:
  1. `filter_list = _parse_report_filters(filters)`, `column_list` as in `_run_report`,
     `page_by = page_by.strip()`, then the `cell_format` check.
  2. `destination_dir = export_files.check_destination(destination_dir)`.
  3. `file_name, fmt = export_files.export_file_name(file_name, report_id)`.
  4. `export_files.refuse_org("salesforce_export_report", self.download_mode,
     "salesforce_run_report")`.
  5. With `page_by`: start from `after = None`, `runs = 0`, `remaining = None` and loop `page =
     await self._fetch(self._sf.run_report_page, report_id, page_by, column_list or None,
     filter_list or None, after, runs, remaining)`. After each page, collect its rows and
     `runs += 1`. Stop when `page.all_data`. Otherwise set `after = page.keys[-1]` and
     `remaining = page.row_count - len(page.keys)`.
  6. Without `page_by`: `result = await self._fetch(self._sf.run_report, report_id, column_list or
     None, filter_list or None, False)`, `runs = 1`.
     - If `set(result.get("factMap") or {}) != {"T!T"}`, raise
       `ValueError("salesforce_export_report: this report is grouped; pass page_by to read it as one table")`.
     - If `result.get("allData") is False`, raise
       `ValueError("salesforce_export_report: the report has more than 2,000 rows; pass page_by (a column with a unique value per row) to export all of them")`.
  7. Columns are the first (or only) result's `reportMetadata.detailColumns`. Rows are each run's
     `factMap["T!T"]["rows"][i]["dataCells"]`, each cell through `_export_cell(cell, cell_format)`:
     - `"label"` mode: `cell.get("label", "")`;
     - `"value"` mode: `v = cell.get("value")`. Return `v` when it is `None`, str, int, float or
       bool. Return `v["amount"]` when it is a dict with an `"amount"` key. Otherwise return
       `cell.get("label", "")`.
- `gated_call`, with these arguments:
  - `tool="salesforce_export_report"`, `tool_name="Export Salesforce Report"`;
  - `summary=f"Export report: {report_name}"`, with `report_name` from `reportMetadata.name`, else
    `report_id`, as in `_run_report`;
  - `sender="Salesforce"`;
  - `preview` is `{"Report": str(report_name), "Report ID": report_id}`. Add `"Columns"`,
    `"Filters"` (the same `_filter_value_summary` text as `_run_report`) and `"Paged by"` when they
    are given, then `"Report runs": str(runs)`;
  - `raw_data={"report_id": report_id, "rows": n, "columns": columns, "sha256": sha256}`;
  - `my_email=self.my_email`;
  - `args={"report_id": report_id, "columns": columns, "filters": filters, "page_by": page_by,
    "cell_format": cell_format, "destination_dir": destination_dir, "file_name": file_name,
    "sha256": sha256}`.
- `SalesforceConnector.__init__` gains `self.download_mode: str = "local"`. At
  `daemon_main.py:1477`, `daemon_main.build_connectors` builds `salesforce_connector =
  SalesforceConnector(salesforce_client)`, sets `salesforce_connector.download_mode =
  download_mode` and appends it.
- Policy: operation `salesforce.run_report`, gate `review`, verb `Verb.READ`, layout `WIDE`. An
  approved-report rule therefore covers it, and "Always allow → this report" is offered through
  `TOOL_REGISTRY` with no extra code.

### 3.5 `grist_export_sql`

Client, `grist_client.py`, after `get_records_by_id`: `def run_sql(self, doc_id: str, sql: str,
args: list[Any]) -> list[dict[str, Any]]`. It calls `validate_doc_id(doc_id)`, then
`body = self._request("POST", f"/api/docs/{doc_id}/sql", json_body={"sql": sql, "args": args})`, and
returns `[_as_dict(_as_dict(r).get("fields")) for r in _as_list(_as_dict(body).get("records"))]`.
The JSON body has no `timeout` field, so Grist's server default applies. The client's own 30-second
HTTP timeout still applies. It logs only the host and row count with `logger.debug`, never the
query.

Connector, `grist.py`: `ToolSpec(name="grist_export_sql", read_only=True)`. Params, in order:
`doc_id`, `sql` (required str), `args` (optional str, default `""`), `destination_dir`
(`_DESTINATION_DIR`), `file_name` (optional, default `""`; description as §3.3 but ending `Empty
means query.csv.`), `_REASON`.

- `sql` description: `"One read-only SQLite SELECT (or WITH … SELECT) over the document's tables,
  by table and column id, such as SELECT c.id, c.Email, o.Stage FROM Contacts c JOIN Deals o ON
  o.Contact = c.id. Use ? for values and pass them in args. No comments, no semicolon except one at
  the end, and no Grist internal tables (names starting with _grist); up to 10000 characters."`
- `args` description: `"A JSON array of values for the ? placeholders in sql, such as [\"CF\",
  2026]. Empty means none."`
- Description: `"Run one read-only SQL query over a Grist document and write the result to a CSV
  or JSON file on the user's computer, in one approval. Use it to join or filter across tables;
  use grist_export_records for one table. Cells are written as Grist stores them: dates as
  seconds since 1970, true/false as 1/0. Grist runs SQL only for people who can copy the whole
  document. Up to 50000 rows. Returns {rows, columns, sha256, path, name, size_bytes, delivery},
  never the rows. Not available on an organization-managed install. Requires user approval."`
- `_validate_sql(raw: str) -> str`:
  1. Strip it, remove one trailing `;`, and strip again.
  2. Raise `ValueError(_SQL_ERROR)` when the result is empty, is longer than 10000 characters, or
     contains `;`, `--` or `/*`.
  3. Raise `ValueError(_SQL_ERROR)` when `re.match(r"[A-Za-z]+", sql)` is `None`, or when its
     match lower-cased is not `select` or `with`.
  4. Raise `ValueError(_SQL_INTERNAL_ERROR)` when `"_grist"` is in `sql.lower()`.
  5. Return the result.
  - `_SQL_ERROR = "sql must be one SELECT (or WITH … SELECT) statement of up to 10000
    characters, with no comments and no semicolon except one at the end."`
  - `_SQL_INTERNAL_ERROR = "sql cannot read Grist's internal tables (names starting with _grist);
    query the document's own tables."`
- `_parse_sql_args(raw: str) -> list[Any]`: empty (after strip) gives `[]`. Otherwise parse it as
  JSON. It must be a list of at most 100 values, each an instance of `_SCALARS`; anything else
  raises `ValueError(_SQL_ARGS_ERROR)`. `_SQL_ARGS_ERROR = "args must be a JSON array of up to
  100 text, number, true/false or null values for the ? placeholders in sql."`
- Flow:
  1. `validate_doc_id(doc_id)`; a `GristClientError` becomes a `ValueError`.
  2. `sql = _validate_sql(sql)`.
  3. `values = _parse_sql_args(args)`.
  4. `destination_dir = export_files.check_destination(destination_dir)`.
  5. `file_name, fmt = export_files.export_file_name(file_name, "query")`.
  6. `export_files.refuse_org("grist_export_sql", self.download_mode, "grist_get_records")`.
  7. `doc = await self._doc_info(doc_id)`.
  8. `records = await self._fetch(self._client.run_sql, doc_id, f"SELECT * FROM ({sql}) LIMIT
     {export_files.EXPORT_MAX_ROWS + 1}", values)`. The statement Grist sees is always one
     `SELECT`, with the caller's query as a subquery.
     - A `RuntimeError` whose `__cause__` is `GristAccessDenied` becomes `RuntimeError("Grist
       refused the SQL query (HTTP 403). Grist runs SQL only for people who can copy the whole
       document; use grist_export_records instead.")`, raised `from exc.__cause__`, as
       `_list_documents` does.
     - More than 50,000 records raises `ValueError("The query returns more than 50,000 rows;
       narrow it.")`.
- Columns: the keys of the first record, in order (`[]` with no rows). Rows: `[record.get(c) for c
  in columns]`, written unconverted.
- `gated_call`, with these arguments:
  - `tool="grist_export_sql"`, `tool_name="Export Grist Query"`;
  - `summary=f"Export {n} row(s) from a query on {doc.name} to {file_name}"`;
  - `sender=doc.name`;
  - `preview={"Server": self._client.host, "Document": doc.name, "Team": doc.team or _NO_TEAM,
    "Query": sql if len(sql) <= 200 else sql[:200] + "…", "Arguments": str(len(values))}`;
  - `details_text=export_files.card_details(...) + "\n\nQuery:\n" + sql + "\n\nArguments: " +
    json.dumps(values, ensure_ascii=False)`;
  - `raw_data={"doc_id": doc_id, "rows": n, "columns": columns, "sha256": sha256}`;
  - `my_email=""`;
  - `args={"doc_id": doc_id, "sql": sql, "args": args, "destination_dir": destination_dir,
    "file_name": file_name, "sha256": sha256}`.
- Policy: operation `grist.read_records`, gate `review`, verb `Verb.READ`, layout `WIDE`. A
  `grist.document` read rule covers it: the same document and the same Grist access rules, and
  Grist itself already requires full copy access. Refusing `_grist` keeps a records rule from
  reaching the action history.
- Live check (API key only): `scripts/qa_fixture_recorder.py` gains `_check_grist_sql(client:
  GristClient, doc_id: str, table_id: str) -> CheckResult`, appended in `check_grist` right after
  `_check_grist_paging`. It runs `client.run_sql(doc_id, f"SELECT id FROM {table_id} ORDER BY
  id", [])` and compares the ids with those of `client.get_records(doc_id, table_id, limit=500)`.
  The row name is `"run_sql"`. On success the note is `f"{n} id(s) match one records read"`. A
  mismatch fails with `f"sql ids {sql_ids} differ from one read's {expected}"`. A
  `GristAccessDenied` fails with `"Grist refused /sql (HTTP 403)"`, and any other
  `GristClientError` fails with `str(exc)`. Nothing is recorded.

### 3.6 Website counts (one step in each tool phase)

Each tool phase updates `website/connectors/index.html` and `website/how-it-works/index.html`,
as `95e85f8e` did:

| After | Total (`connectors` h1 "N tools" and `how-it-works` "N connector tools") | Grist card | Salesforce card |
|---|---|---|---|
| today | 129 | 9 / 2 / 1 / 6 | 4 / 1 / 3 / 0 |
| p2 | 130 | 10 / 2 / 2 / 6 | unchanged |
| p3 | 131 | unchanged | 5 / 1 / 4 / 0 |
| p4 | 132 | 11 / 2 / 3 / 6 | unchanged |

The card values are `data-tools / data-auto / data-review / data-popup`, and the card's
`connector-counts` line must say the same (for example `10 tools: 2 without a card · 2 reviewed ·
6 need approval`). The card's "Reviewed before release" text becomes:

- Grist after p2: `Records in a table, and a whole table exported to a file.`
- Salesforce after p3: `A record, search results, a report's results, and a whole report exported to a file.`
- Grist after p4: `Records in a table, a whole table or a SQL query's result exported to a file.`

### 3.7 Rejected alternatives

- **An `output_path` option on `grist_get_records` and `salesforce_run_report`.** The return shape
  would depend on an argument (ADR 0116 keeps a tool's return shape stable), and the card and
  tool description would have to explain two behaviours. Separate tools keep both honest.
- **New operation keys for the exports.** The existing per-document and per-report rules would not
  cover them, and `approved_report_ids` sits in the frozen v1 `SCOPE_SELECTORS` set. The export
  reads the same data with the same rights; only where the rows go differs.
- **One `output_path`.** `deliver_file` takes a folder and a name (ADR 0007), as
  `drive_download_file` does. Splitting them lets the default name come from the table or report.
- **Paging a Grist export by `after_id`.** Paging costs about the square of the page count in
  transfer (ADR 0146). One request of up to 50,001 rows costs Grist the same table load as one
  page.
- **Delivery by link in organization mode.** A staged link would hand the whole table out through
  the org daemon's staging store, which is a separate delivery decision. Exports are refused
  there.
- **Exempting exports from the PII gate.** The user chose to keep it.
- **Leaving formula columns out of a Grist export.** The export is a read of what the table
  shows. The description tells the caller which columns `grist_update_csv` can take back.
- **Allowing `_grist` tables in SQL.** A document read rule was made for the document's records.
  The action history holds old and deleted values that rule never meant to cover.
- **A `timeout` in the SQL request body.** The Grist server caps it by configuration PrivacyFence
  cannot see. The server default applies.

## ADRs

Numbers are the next free ones in `docs/adr/` today (the last is 0150). Renumber if a parallel
branch takes them first (`docs/adr/README.md` rule 6).

- **ADR 0151 — Exports write a file and return only counts and a hash.**
  `grist_export_records` and `salesforce_export_report` are separate tools. They reuse their read
  tool's operation key, so an existing read rule covers them. Each shows one review card after
  the whole fetch, keeps the PII gate, checks the delivery size before the card, binds the
  approval to the file's SHA-256, and is refused on an organization-managed install. The ADR
  rejects §3.7's first seven alternatives.
- **ADR 0152 — `grist_export_sql` reads through Grist's SQL endpoint.** It amends ADR 0146,
  whose paging decision for `grist_get_records` stands. The ADR records:
  - the connector-side `SELECT`-only validation, the `LIMIT` subquery wrap and the `_grist`
    refusal;
  - the 403 message, cells written as SQLite stores them, and the `grist.read_records` mapping;
  - that the CI live check proves the endpoint with an API key only, and that ma2's OAuth result
    goes in the PR;
  - that ADR 0145 is unchanged.

  ADR 0146's Status gains one forward line: `Amended by [ADR 0152](0152-…): grist_export_sql uses
  the SQL endpoint for exports; paging grist_get_records is unchanged.`

## Manual steps

There is nothing to do before implementation. After the last merge, and before the PR merges,
work through the
[step-by-step page](https://claude.ai/artifact/CBHByCthEfgm3xzbyCQbQd):

- **ma1:** set up the auto-accept rules for the scheduled run in Settings → Auto-accept.
- **ma2:** run each export once against real data. Check for one card per export, the file
  contents, the Grist round trip, an incremental Salesforce filter, and `grist_export_sql` under
  an OAuth sign-in.

## Risks and open questions

- **The OAuth scope may not cover `/sql`.** CI cannot tell, because the QA runner uses an API key.
  ma2 does, and the result goes in the PR. If an OAuth sign-in gets 403 there, that is a follow-up
  decision. This plan does not handle it.
- **Grist's SQL time cap.** A large join can hit Grist's default SQL timeout, and the client's
  30-second HTTP timeout still applies. That error reaches the caller as Grist's message, and ma2
  exercises the real join.
- **Grist's records endpoint with `limit=50001`.** It is untested against a real server with that
  many rows. ma2 exports the real tables.
- **Salesforce date filters.** `greaterOrEqual` with `"2026-10-04"` on `LAST_UPDATE` is what the
  Analytics API documents, but the column's API name differs per report type. ma2 checks it.
- **Same operation key as a read.** A rule someone made for interactive report reads now also
  covers exports of that report. ADR 0151 states this; it is intended.
- **The live-check workflow's side effect.** When any `--check` row fails,
  `connector-live-check.yml` runs `--record` and opens a fixture-drift PR on
  `chore/connector-live-fixture-drift`. p5 says not to merge that PR.
- **Pinned tests.** If a test not named in `Current state` fails because it lists tools or counts
  them, add the new tool the way its neighbour appears. Stop with `status=blocked` only if the test
  encodes a policy rule the design does not mention.

## Implementation manifest

```yaml
plan_slug: bulk-exports
feature_branch: feature/bulk-exports
max_parallel: 1
manual_steps_artifact: https://claude.ai/artifact/CBHByCthEfgm3xzbyCQbQd
manual_steps_source: docs/bulk-exports-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-auto-accept-rules
    title: Create the auto-accept rules the scheduled sync needs
    why: The rules are user configuration, not code; without them every export and grist_update_csv still shows a card.
  - id: ma2-live-export-check
    title: Run each export tool once against real data and check the files
    why: CI uses fakes and an API key; only a real Grist server and Salesforce org show the OAuth SQL scope, real date filters, real cell formats and the 50,000-row fetch.
verify_after_merge:
  - python3 -m pytest tests/unit/test_export_files.py tests/unit/test_local_files.py tests/unit/connectors/test_grist_connector.py tests/unit/connectors/test_salesforce_connector.py tests/unit/test_grist_client.py tests/unit/test_qa_fixture_recorder.py tests/unit/policy -q
  - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_systemic_gate_invariants.py tests/unit/test_code_no_history.py -q
final_checks:
  - docs/bulk-exports-plan.md and docs/bulk-exports-plan-manual-steps.html are deleted and nothing links to them (git grep -n "bulk-exports-plan" prints nothing)
  - The two new ADRs (0151 and 0152, or the renumbered pair) exist in docs/adr/, have Status Accepted and are listed in docs/adr/README.md; ADR 0146's Status has the forward line to the SQL ADR
  - CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading
  - The PR body links a connector-live-check.yml run whose grist run_sql row passed
phases:
  - id: p1-export-files
    title: Pure export-file helpers and the delivery size check
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/export_files.py
      - src/privacyfence/local_files.py
      - tests/unit/test_export_files.py
      - tests/unit/test_local_files.py
    brief: |
      Read docs/coding-and-testing-guidelines.md (§1.7 and §2.2 especially) and
      docs/testing-policy.md. Then implement Design §3.1 of docs/bulk-exports-plan.md exactly:
      its constants, error strings and signatures. No comment or docstring may cite the plan
      (tests/unit/test_code_no_history.py enforces this).
      1. Create src/privacyfence/export_files.py. Module docstring: "Pure helpers the export
         tools share: the file name and format, CSV and JSON rendering, the card's sample table
         and details, and the result summary. No I/O; local_files.deliver_file writes the file."
         Use `from __future__ import annotations`, type hints on every function, stdlib only
         (csv, io, json, typing).
      2. Implement export_file_name, check_destination, refuse_org, csv_cell, render,
         sample_table, scan_text, card_details and export_summary as §3.1 specifies.
      3. src/privacyfence/local_files.py: add `check_delivery_size(size: int) -> None` next to
         deliver_file. It raises the exact LocalFileAccessError message deliver_file raises today,
         with `{size:,}` in place of `{len(data):,}`. Replace deliver_file's inline check with a
         call to it, and add it to __all__ in alphabetical position.
      4. Create tests/unit/test_export_files.py with a module docstring and
         `pytestmark = pytest.mark.unit`, tests grouped in classes. Each test checks one behaviour:
         - class TestExportFileName:
           - test_defaults_to_stem_csv
           - test_picks_json_from_suffix (also upper-case .JSON)
           - test_rejects_paths_dotfiles_and_other_suffixes, parametrized over "a/b.csv",
             "a\\b.csv", ".x.csv", "x.txt", "x" and a 201-character name
         - class TestCheckDestination:
           - test_returns_stripped
           - test_rejects_empty
         - class TestRefuseOrg:
           - test_raises_only_in_org_mode
         - class TestCsvCell:
           - test_formats, parametrized over None, True, False, 3, 3.0, 2.5, "x", ["a", "b"]
             and {"k": 1}
         - class TestRender:
           - test_csv_quotes_commas_and_newlines
           - test_csv_has_no_bom_and_lf_line_ends
           - test_json_is_a_list_of_objects
         - class TestSampleTable:
           - test_shows_twenty_rows_and_truncates_long_cells
         - class TestScanText:
           - test_caps_at_one_million_chars
         - class TestCardDetails:
           - test_lists_preview_then_counts_path_and_hash
           - test_has_no_agent_placeholder
         - class TestExportSummary:
           - test_merges_delivery
      5. tests/unit/test_local_files.py: in class TestDeliverFile (line 293), next to the test
         that calls `configure_file_bridge(max_download_bytes=5)` (line 359), add
         test_check_delivery_size_raises_over_the_limit and
         test_check_delivery_size_allows_the_limit.
      6. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py`.
      No changelog line (internal module).
    acceptance:
      - python3 -m pytest tests/unit/test_export_files.py tests/unit/test_local_files.py -q passes
      - python3 -m pytest tests/unit/test_export_files.py -q --cov=privacyfence.export_files --cov-branch --cov-report=term-missing reports 100% for export_files.py
      - grep -n '"check_delivery_size"' src/privacyfence/local_files.py matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p2-grist-export-records
    title: grist_export_records tool
    depends_on: [p1-export-files]
    complexity: M
    touches:
      - src/privacyfence/connectors/grist.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/gate.py
      - src/privacyfence/policy/registry.py
      - tests/unit/connectors/test_grist_connector.py
      - tests/unit/policy/test_propose.py
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read docs/coding-and-testing-guidelines.md, ADRs 0007, 0144, 0146 and 0148, and the
      Current state and Design §3.2, §3.3 and §3.6 of docs/bulk-exports-plan.md. Commit 2dae2772
      is the pattern for wiring a Grist tool through the registries and tests, and 95e85f8e for
      the website counts. No comment or docstring may cite the plan.
      1. src/privacyfence/connectors/grist.py:
         - Import `export_files` alongside `local_files`.
         - Add the module constants `_FILTER_DESCRIPTION` (grist_get_records' current filter
           text, unchanged; use it in both specs) and `_DESTINATION_DIR`.
         - Add the grist_export_records ToolSpec after grist_get_records, with §3.3's exact
           params and description, and its dispatch branch in call().
         - Add `_export_value(value: Any, column_type: str) -> Any` per §3.3, next to
           `_csv_cell_text`.
         - Add `async def _export_records(self, doc_id: str, table_id: str, destination_dir: str,
           filter: str = "", file_name: str = "") -> Any` in the "Review gate (reads)" section,
           following §3.2 steps 1-5 and §3.3 exactly, strings included.
      2. src/privacyfence/auto_accept.py: add `"grist_export_records": "grist.read_records"` to
         TOOL_TO_OPERATION and `"grist_export_records": "review"` to TOOL_TO_GATE, each next to
         grist_get_records.
      3. src/privacyfence/policy/registry.py: add `"grist_export_records": Verb.READ` next to
         grist_get_records.
      4. src/privacyfence/gate.py: add `"grist_export_records": WIDE` to the layout map
         (line 259).
      5. tests/unit/policy/test_propose.py: add "grist_export_records" to the Grist tool tuple
         of test_the_extra_scope_operations_stay_unproposed (line 273).
      6. tests/unit/connectors/test_grist_connector.py:
         - TestEveryToolIsAudited (line 827): add
           `"grist_export_records": {"doc_id": "DOC1", "table_id": "Table1", "destination_dir": str(tmp_path)}`
           to arg_overrides. Before the assert call, add
           `monkeypatch.setattr(grist_module.local_files, "deliver_file", lambda *a, **k: {"path": None, "name": "x.csv", "size_bytes": 0, "delivery": "local_disk"})`.
           No file is written.
         - Add `class TestExportRecords`, using make_connector() and gated_call_spy. Fake
           `grist_module.local_files.deliver_file` with monkeypatch and capture its arguments.
           Tests:
           - test_writes_csv_with_id_first_and_dates_as_iso
           - test_writes_json_when_file_name_ends_json
           - test_choice_list_drops_the_L_marker
           - test_datetime_column_written_as_utc_iso
           - test_one_card_with_sample_and_no_cell_in_raw_data
           - test_details_text_has_no_agent_placeholder
           - test_audit_delivery_is_passed
           - test_approval_args_carry_sha256_and_options
           - test_filter_is_passed_to_the_client
           - test_more_than_50000_records_is_refused_before_the_card
           - test_over_the_delivery_limit_is_refused_before_the_card (monkeypatch
             `local_files.check_delivery_size` to raise LocalFileAccessError)
           - test_org_mode_is_refused_before_any_request
           - test_bad_file_name_is_refused_before_any_request
           - test_deny_writes_nothing (a gated_call that raises leaves deliver_file uncalled)
           - test_result_is_summary_only (keys exactly rows, columns, sha256 plus the delivery
             keys)
      7. Website, per §3.6 row p2: in website/connectors/index.html, set the h1 total to 130 and
         the Grist card to data-tools="10" data-auto="2" data-review="2" data-popup="6", with its
         connector-counts line and its "Reviewed before release" text. In
         website/how-it-works/index.html, set "Each of the 130 connector tools".
      8. Regenerate the reference docs with `python3 scripts/generate_tools_reference.py` and
         `python3 scripts/generate_always_allow_reference.py`, and commit
         docs/tools-reference.md and docs/always-allow-rules-reference.md.
      9. Run `python3 -m pytest tests/unit -q`. Then run `ruff check .`,
         `python3 -m mypy src/privacyfence` and `python3 scripts/mypy_strict_modules.py`. If a
         test outside this list fails because it pins tools, follow the plan's "Pinned tests" risk.
      No changelog line here; p5 writes it.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q -k "TestExportRecords" passes, with the 15 tests named in the brief
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py::TestEveryToolIsAudited tests/unit/test_website_connectors_page.py tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py -q passes
      - python3 -m pytest tests/unit -q passes
      - grep -nE '"grist_export_records"[[:space:]]*:[[:space:]]*"grist.read_records"' src/privacyfence/auto_accept.py matches
      - grep -n "130 tools" website/connectors/index.html matches
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p3-salesforce-export-report
    title: salesforce_export_report tool
    depends_on: [p2-grist-export-records]
    complexity: M
    touches:
      - src/privacyfence/connectors/salesforce.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/gate.py
      - src/privacyfence/policy/registry.py
      - tests/unit/connectors/test_salesforce_connector.py
      - tests/unit/test_daemon_main.py
      - tests/unit/policy/test_propose.py
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read ADRs 0007 and 0133 and the Current state and Design §3.2, §3.4 and §3.6 of
      docs/bulk-exports-plan.md. The p2 grist_export_records commit is the pattern for the
      registries, tests, website and docs. No comment or docstring may cite the plan.
      1. src/privacyfence/connectors/salesforce.py:
         - Add `import hashlib` and import `export_files` and `local_files` alongside `cursors`.
         - Set `self.download_mode: str = "local"` in __init__.
         - Move the four salesforce_run_report param descriptions into module constants, as
           §3.4 says.
         - Add the salesforce_export_report ToolSpec after salesforce_run_report, with §3.4's
           exact params and description, and its dispatch branch in call().
         - Add the module-level `_export_cell(cell: dict, cell_format: str) -> Any` and
           `async def _export_report(self, report_id: str, destination_dir: str, columns: str = "",
           filters: str = "", page_by: str = "", cell_format: str = "value", file_name: str = "")
           -> Any`, following §3.2 and §3.4 exactly.
         - SalesforceConnector._fetch takes positional args only, so call run_report_page
           positionally: (report_id, page_by, columns, filters, after, pages_done, remaining).
      2. src/privacyfence/daemon_main.py, the Salesforce block at line 1477: replace
         `connectors.append(SalesforceConnector(salesforce_client))` with three lines that build
         `salesforce_connector`, set `salesforce_connector.download_mode = download_mode`, and
         append it, as the Grist block at line 1585 does.
      3. src/privacyfence/auto_accept.py: add `"salesforce_export_report": "salesforce.run_report"`
         to TOOL_TO_OPERATION and `"review"` to TOOL_TO_GATE, next to salesforce_run_report. In
         src/privacyfence/policy/registry.py, add `Verb.READ`. In src/privacyfence/gate.py, add
         `WIDE` (line 258).
      4. tests/unit/policy/test_propose.py: add a CORPUS entry
         `("salesforce_export_report", make_ctx(connector="salesforce", args={"report_id": "R1"}, raw_data={}))`
         after the salesforce_run_report entry (line 79). In class TestProposalsFor, add
         test_export_report_is_offered_this_report, asserting that
         proposals_for("salesforce_export_report", that ctx) has one proposal whose scope
         predicate is "approved_report_ids" with value ["R1"], the same as
         salesforce_run_report gets.
      5. tests/unit/connectors/test_salesforce_connector.py:
         - TestEveryToolIsAudited (line 920): set `client.run_report.return_value` to a tabular
           result from tests/fixtures/salesforce_analytics (the builder the file already uses
           for a "T!T" report). Pass
           `arg_overrides={"salesforce_export_report": {"report_id": "00O000000000001", "destination_dir": str(tmp_path)}}`,
           and monkeypatch `salesforce_module.local_files.deliver_file` as p2 did for Grist.
         - Add `class TestExportReport`, reusing the file's fixtures and the
           tests/fixtures/salesforce_analytics builders. Fake
           `salesforce_module.local_files.deliver_file` via monkeypatch. Tests:
           - test_unpaged_tabular_report_writes_all_rows
           - test_paged_export_runs_until_all_data (three pages; asserts the after, pages_done
             and remaining values passed on each call)
           - test_grouped_report_without_page_by_is_refused
           - test_cut_off_report_without_page_by_is_refused
           - test_value_format_uses_amount_and_raw_values
           - test_label_format_uses_labels
           - test_bad_cell_format_is_refused_before_any_request
           - test_filters_are_passed_to_each_run
           - test_one_card_with_report_runs_line
           - test_audit_delivery_is_passed
           - test_org_mode_is_refused_before_any_request
           - test_deny_writes_nothing
           - test_result_is_summary_only
      6. tests/unit/test_daemon_main.py: in
         TestBuildConnectorsSalesforce.test_built_when_configured_and_reachable_merges_org_and_token
         (line 935), add `assert connectors[0].download_mode == "local"`, as 2dae2772 did for Grist.
      7. Website, per §3.6 row p3: set the total to 131 on both pages, and the Salesforce card to
         data-tools="5" data-auto="1" data-review="4" data-popup="0", with its connector-counts
         line and its "Reviewed before release" text.
      8. Regenerate both reference docs. Then run `python3 -m pytest tests/unit -q`,
         `ruff check .`, `python3 -m mypy src/privacyfence` and
         `python3 scripts/mypy_strict_modules.py`.
      No changelog line here.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_salesforce_connector.py -q -k "TestExportReport" passes, with the 13 tests named in the brief
      - python3 -m pytest tests/unit/connectors/test_salesforce_connector.py::TestEveryToolIsAudited tests/unit/policy/test_propose.py tests/unit/test_website_connectors_page.py -q passes
      - python3 -m pytest tests/unit -q passes
      - grep -nE '"salesforce_export_report"[[:space:]]*:[[:space:]]*"salesforce.run_report"' src/privacyfence/auto_accept.py matches
      - grep -n "salesforce_connector.download_mode = download_mode" src/privacyfence/daemon_main.py matches
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p4-grist-export-sql
    title: grist_export_sql tool, GristClient.run_sql and its live check
    depends_on: [p3-salesforce-export-report]
    complexity: M
    touches:
      - src/privacyfence/grist_client.py
      - src/privacyfence/connectors/grist.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/gate.py
      - src/privacyfence/policy/registry.py
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_grist_client.py
      - tests/unit/connectors/test_grist_connector.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/unit/policy/test_propose.py
      - website/connectors/index.html
      - website/how-it-works/index.html
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read ADRs 0145 and 0146 and the Current state and Design §3.2, §3.5 and §3.6 of
      docs/bulk-exports-plan.md. The p2 commit is the pattern for the tool. `_check_grist_paging`
      in scripts/qa_fixture_recorder.py is the pattern for the live check. No comment or
      docstring may cite the plan.
      1. src/privacyfence/grist_client.py: add `run_sql` after get_records_by_id, exactly as §3.5
         says.
      2. tests/unit/test_grist_client.py, using the file's `net` and `client` fixtures:
         - test_run_sql_posts_sql_and_args_to_the_sql_endpoint
         - test_run_sql_returns_fields
         - test_run_sql_403_raises_access_denied
         - test_run_sql_validates_doc_id
      3. src/privacyfence/connectors/grist.py:
         - Add `import re`, the constants _SQL_ERROR, _SQL_INTERNAL_ERROR and _SQL_ARGS_ERROR,
           and `_validate_sql` and `_parse_sql_args` next to `_parse_filter`.
         - Add the grist_export_sql ToolSpec after grist_export_records, with §3.5's exact params
           and description, and its dispatch branch.
         - Add `async def _export_sql(self, doc_id: str, sql: str, destination_dir: str, args: str
           = "", file_name: str = "") -> Any`, following §3.2 and §3.5 exactly.
      4. src/privacyfence/auto_accept.py: `"grist_export_sql": "grist.read_records"` and
         `"review"`. src/privacyfence/policy/registry.py: `Verb.READ`. src/privacyfence/gate.py:
         `WIDE`. tests/unit/policy/test_propose.py: add "grist_export_sql" to the Grist tuple at
         line 273.
      5. tests/unit/connectors/test_grist_connector.py:
         - TestEveryToolIsAudited: set `client.run_sql.return_value = []` and add
           `"grist_export_sql": {"doc_id": "DOC1", "sql": "SELECT 1", "destination_dir": str(tmp_path)}`
           to arg_overrides.
         - Add `class TestExportSql`:
           - test_wraps_query_in_a_limited_subquery
           - test_passes_args_to_the_client
           - test_rejects_bad_sql, parametrized over "DELETE FROM T", "SELECT 1; SELECT 2",
             "SELECT 1 -- x", "SELECT /* x */ 1", "(SELECT 1)", "" and a 10001-character query
           - test_rejects_internal_tables, parametrized over "SELECT * FROM _grist_Tables" and
             "select * from _GRISTSYS_ActionHistory"
           - test_allows_one_trailing_semicolon_and_with
           - test_rejects_bad_args, parametrized over not JSON, an object, 101 values and a
             nested list
           - test_more_than_50000_rows_is_refused
           - test_403_gives_the_copy_access_message
           - test_columns_follow_first_record_and_cells_are_unconverted
           - test_card_shows_query_and_arguments_count
           - test_org_mode_is_refused_before_any_request
           - test_deny_writes_nothing
           - test_result_is_summary_only
      6. scripts/qa_fixture_recorder.py: add `_check_grist_sql` exactly as §3.5 says, and append
         its result in check_grist right after `_check_grist_paging`'s.
      7. tests/unit/test_qa_fixture_recorder.py:
         - In `_FakeGristApi.__call__` (line ~2965), answer `POST /api/docs/{DOC}/sql` with
           `{"records": [{"fields": {"id": i}} for i in sorted(self.rows)]}`.
         - Add a constructor option `sql_status=None`: raise it like records_status when set.
         - Add a constructor option `sql_ids=None`: return those ids instead of the rows' when
           set.
         - Add `("run_sql", True, "")` after `("get_records_paged", True, "")` in
           test_records_three_fixtures (line 3029).
         - Add test_run_sql_403_fails_the_row (`sql_status=GristAccessDenied(...)`) and
           test_run_sql_id_mismatch_fails_the_row (`sql_ids=[1]`), next to the paging tests at
           line 3036.
      8. Website, per §3.6 row p4: set the total to 132 on both pages, and the Grist card to
         data-tools="11" data-auto="2" data-review="3" data-popup="6", with its counts line and
         its text.
      9. Regenerate both reference docs. Then run `python3 -m pytest tests/unit -q`,
         `ruff check .`, `python3 -m mypy src/privacyfence`,
         `python3 scripts/mypy_strict_modules.py` and `bandit -c pyproject.toml -r src`.
      No changelog line here.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q -k "TestExportSql or TestEveryToolIsAudited" passes
      - python3 -m pytest tests/unit/test_grist_client.py -q -k "run_sql" passes, with the 4 tests named in the brief
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes, including test_records_three_fixtures with the run_sql row
      - python3 -m pytest tests/unit -q passes
      - grep -n "_check_grist_sql(client, doc_id, table_id)" scripts/qa_fixture_recorder.py matches
      - grep -n "132 tools" website/connectors/index.html matches
      - ruff check ., python3 -m mypy src/privacyfence and bandit -c pyproject.toml -r src pass

  - id: p5-retire
    title: ADRs, reference docs, changelog, live check, and retire the plan
    depends_on: [p4-grist-export-sql]
    complexity: S
    touches:
      - docs/adr/0151-*.md
      - docs/adr/0152-*.md
      - docs/adr/0146-grist-records-page-by-record-id.md
      - docs/adr/README.md
      - docs/grist-setup.md
      - docs/salesforce-setup.md
      - docs/connector-qa.md
      - docs/configuration-reference.md
      - docs/approvals-and-policy.md
      - CHANGELOG.md
      - docs/bulk-exports-plan.md
      - docs/bulk-exports-plan-manual-steps.html
    brief: |
      Read CONTRIBUTING.md "Decisions, plans and ADRs", docs/adr/README.md (its template and
      rules 2, 3 and 6), ADR 0148 as the model ADR, and the Design, ADRs and Risks sections of
      docs/bulk-exports-plan.md.
      1. Live check. Push this phase's branch first. Then dispatch connector-live-check.yml on
         that branch with the GitHub MCP actions_run_trigger tool (no inputs). It shares a
         concurrency group with qa-record-fixture.yml, so a queued run is normal.
         - Read the result from the job log (GitHub MCP get_job_logs) or the
           connector-live-check-report artifact. Find the grist run_sql row.
         - When any row fails, the workflow also opens a fixture-drift PR on
           chore/connector-live-fixture-drift. Do not merge or follow that PR.
         - If run_sql passes, keep the run URL for step 2 and the final report.
         - If run_sql fails, stop with status=blocked and quote the row.
      2. Write the two ADRs from the plan's ADRs section and Design §3.7, in the README's
         template: Status "Accepted — <today>. Implemented.", Context, Decision, Alternatives
         considered, Consequences, Verification and Related.
         - Files: docs/adr/0151-exports-write-a-file-and-return-only-counts-and-a-hash.md and
           docs/adr/0152-grist-export-sql-reads-through-the-sql-endpoint.md.
         - Verification names the test classes TestExportRecords, TestExportReport and
           TestExportSql, and the run_sql live-check row.
         - 0151 states that the PII gate is unchanged and that an existing read rule covers the
           export.
         - 0152 says "Amends ADR 0146" in its Status, states that ADR 0145 is unchanged, and
           links the step-1 run.
         - If 0151 or 0152 is already taken on main, take the next free numbers and use them
           everywhere below.
      3. docs/adr/0146-grist-records-page-by-record-id.md: add the one forward line from the
         plan's ADRs section to its Status section, and change nothing else (rule 2).
         docs/adr/README.md: add both new ADRs to the index, in number order, with Status
         Accepted.
      4. Reference docs. Link the ADRs; do not retell them.
         - docs/grist-setup.md, "What the assistant can do": add two table rows after
           grist_get_records:
           `| grist_export_records | Reviewed before release | Writes a whole table, optionally filtered, to a CSV or JSON file on your computer, in one approval |`
           and
           `| grist_export_sql | Reviewed before release | Writes the result of one read-only SQL query over the document to a CSV or JSON file, in one approval |`
           (tool names in backticks like the other rows). After the CSV import/update section,
           add a short "Exporting to a file" section. It says: the 50,000-row limit; that org
           mode is refused; that a document read rule covers both tools; that the PII gate still
           applies; that SQL needs full copy access and cannot read the _grist tables; and that
           writing an export back with grist_update_csv needs only id and the data columns.
         - docs/salesforce-setup.md, "Reading large reports" (line 91): add a paragraph on
           salesforce_export_report with the incremental LAST_UPDATE filter example from §3.4
           and an IDs-only `columns` example.
         - docs/connector-qa.md: add the run_sql row to the Grist checks list, saying it runs
           with the QA API key.
         - docs/configuration-reference.md: line 102 (salesforce.report_max_pages) also governs
           salesforce_export_report, and line 169 (file_bridge.max_download_bytes) also caps the
           export tools' files. Add those words.
         - docs/approvals-and-policy.md: where it lists what a Grist document rule and an
           approved-report rule cover (around lines 386-389), add one sentence saying the export
           tools are included and the PII gate still applies.
      5. CHANGELOG.md, under "## [Unreleased]": a "### Added" subsection with three bullets in
         the file's style (a bold lead sentence, then one or two plain sentences). Each bullet
         names its tool in backticks: `grist_export_records` (a whole table to a file in one
         approval), `salesforce_export_report` (internal paging and incremental filters) and
         `grist_export_sql` (one read-only query to a file). Open no version heading.
      6. Delete docs/bulk-exports-plan.md and docs/bulk-exports-plan-manual-steps.html. Run
         `git grep -n "bulk-exports-plan"` and remove any reference it prints.
      7. Run the full /dod gate.
    acceptance:
      - ls docs/adr/ | grep -c "exports-write-a-file-and-return-only-counts-and-a-hash\|grist-export-sql-reads-through-the-sql-endpoint" prints 2
      - grep -c "exports-write-a-file-and-return-only-counts-and-a-hash\|grist-export-sql-reads-through-the-sql-endpoint" docs/adr/README.md prints 2
      - grep -n "Amended by" docs/adr/0146-grist-records-page-by-record-id.md matches
      - git grep -n "bulk-exports-plan" prints nothing
      - awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -c "grist_export_records\|salesforce_export_report\|grist_export_sql" prints 3 or more
      - the connector-live-check.yml run URL is in the phase's final report, with its grist run_sql row passing
      - python3 -m pytest tests/unit -q and ruff check . pass
```
