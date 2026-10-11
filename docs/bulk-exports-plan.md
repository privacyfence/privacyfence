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
  `limit=50_000` it is a single request for up to 50,001 rows. The client itself has no cap on
  `limit`; only the connector validates it.
- `src/privacyfence/connectors/salesforce.py:283-333, 449-569`: `salesforce_run_report` returns one
  run of at most 2,000 detail rows. With `page_by` and `cursor` it reads one keyset page per call,
  one card each (ADR 0133). `SalesforceClient.run_report_page` (`salesforce_client.py:808-842`)
  does one page and raises `ReportPagingError` past `report_max_pages` (default 50). A paged run
  is flattened to one table, factMap key `"T!T"`, with `reportMetadata.detailColumns` as the
  columns. Each `dataCells` entry has `value` (raw: text, number, `{"amount", "currency"}` for
  currency, record id for name columns) and `label` (formatted as Salesforce shows it).
- `filters` on `salesforce_run_report` already takes `greaterOrEqual` and is ANDed onto the saved
  filters (`_parse_report_filters`, `salesforce.py:203-228`).
- Local file delivery: `local_files.deliver_file(dest_dir, name, data, mime_type, *,
  download_mode)` (`local_files.py:423-459`). It writes directly when the daemon is the user
  (`_deliver_direct`, which overwrites a file of the same name). Otherwise it stages the bytes
  for the `.mcpb` shim (`_deliver_bridge`; the shim never overwrites and picks `name (1).csv`), or
  hands back a one-time link (`_deliver_link`). It returns `{path, name, size_bytes, delivery, …}`
  and caps the data at `file_bridge.max_download_bytes` (200 MB). `drive_download_file`
  (`connectors/drive.py:1121-1312`) is the pattern: build everything, `gated_call`, then deliver.
- Policy: `auto_accept.TOOL_TO_OPERATION` (`auto_accept.py:129-137`) and `TOOL_TO_GATE`
  (`auto_accept.py:273, 318-324`) map tools to operation keys and gates.
  `policy/registry.py:190-198` maps tools to verbs. `gate.py:258-260` gives the Grist and
  Salesforce tools the `WIDE` card layout. A Grist rule is a `grist.document` scope matching
  `args["doc_id"]` (`policy/scopes.py:552-556`, ADR 0144). A report rule is `approved_report_ids`,
  matching `args["report_id"]` (`policy/scopes.py:321-325`). `policy/propose.proposals_for` offers
  "Always allow → this report" for any tool whose registry entry has operation
  `salesforce.run_report` and verb READ (`propose.py:367`), and nothing for Grist (ADR 0144).
- PII gate (`gate.py:76-91, 1037-1072`): on a `review` gate, a PII match in `pii_scan_text`
  overrides a matching rule. In an unattended session that becomes `denied_unattended`.
- `GristConnector.download_mode` is set in `daemon_main.build_connectors` (`daemon_main.py:1586`).
  `SalesforceConnector` has no `download_mode`; it is built at `daemon_main.py:1477`.
- Grist's SQL endpoint: `POST /api/docs/{docId}/sql` with body `{"sql": "...", "args": [...]}`
  returns `{"statement": "...", "records": [{"fields": {...}}]}`. Grist allows it only for a caller
  who may copy the whole document; anyone else gets HTTP 403. Cells come back as SQLite stores them
  (dates as epoch seconds, booleans as 0/1, lists as JSON text). ADR 0146 rejected it *for paging
  `grist_get_records`*; the user has now asked for it as a separate export tool. The OAuth scope
  (`grist_auth.GRIST_SCOPES = "doc:read doc:write doc.schema:write offline_access"`) is not
  documented to cover it, so the live check in phase p5 settles that.
- Generated reference docs: `scripts/generate_tools_reference.py` writes `docs/tools-reference.md`
  and `scripts/generate_always_allow_reference.py` writes `docs/always-allow-rules-reference.md`.
  `tests/unit/test_docs_tools_reference.py` and `tests/unit/test_generate_always_allow_reference.py`
  fail when either is stale.
- Pattern commits: `2dae2772` (adding `grist_import_csv`/`grist_update_csv`: connector, registries,
  tests) and `95e85f8e` (their docs, ADR and changelog).

## Design

### 3.1 New module `src/privacyfence/export_files.py` (pure, no I/O)

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
def export_summary(columns: list[str], rows: list[list[Any]], data: bytes, delivered: dict[str, Any]) -> dict[str, Any]: ...
```

- `export_file_name`: strip. Empty becomes `f"{default_stem}.csv"`. Raise `ValueError(FILE_NAME_ERROR)`
  when the name contains `/`, `\` or `\x00`, starts with `.`, is longer than 200 characters, or its
  lower-cased suffix is not `.csv` or `.json`. Return `(name, "csv" | "json")`; the suffix picks
  the format, so there is no `format` parameter.
- `check_destination`: strip; empty raises `ValueError(DESTINATION_ERROR)`; otherwise return it
  unchanged (it is handed to `deliver_file` exactly as typed, ADR 0007 G1).
- `refuse_org`: when `download_mode == "org"`, raise `ValueError(f"{tool} writes a file on your
  computer, which an organization-managed install cannot do. Use {alternative} instead.")`.
- `csv_cell`: `None` → `""`; `bool` → `"true"`/`"false"`; `int` → `str(v)`; `float` →
  `str(int(v))` when `v.is_integer()`, else `repr(v)`; `str` → itself; anything else (list, dict) →
  `json.dumps(v, ensure_ascii=False)`.
- `render`: `"csv"` writes a header row of `columns`, then one row per entry of `rows` via
  `csv_cell`, using `csv.writer(buf, lineterminator="\n")` over an `io.StringIO`, encoded UTF-8
  with no BOM. `"json"` writes `json.dumps([dict(zip(columns, row)) for row in rows],
  ensure_ascii=False, separators=(",", ":")) + "\n"`, encoded UTF-8.
- `sample_table`: `{"caption": "First rows", "headers": columns, "rows": [[short(csv_cell(v)) for v in
  row] for row in rows[:SAMPLE_ROWS]], "footer": f"Showing {k} of {n} rows."}`. `short` cuts a cell
  over 200 characters to its first 200 plus `…`.
- `scan_text`: `data.decode("utf-8", errors="replace")[:SCAN_CHARS]`.
- `export_summary`: `{"rows": len(rows), "columns": columns, "sha256":
  hashlib.sha256(data).hexdigest(), **delivered}`, where `delivered` is `deliver_file`'s return
  value (`path`, `name`, `size_bytes`, `delivery` and, for a link, `download_url`, `expires_at`,
  `note`).

### 3.2 The shared export flow (every export tool, in this order)

1. Validate every argument and call `export_files.refuse_org` before any network call. A doomed
   call never costs an approval.
2. Fetch every row. Refuse more than `EXPORT_MAX_ROWS` rows (Grist) or past
   `salesforce.report_max_pages` (Salesforce, already raised by the client).
3. Build `columns`, `rows`, `data = export_files.render(...)` and `sha256`.
4. `await gated_call(gate="review", filtered_data=None, ...)`, with:
   - `raw_data` holding only identifiers, counts, column names and `sha256`, never a cell value
     (it can reach the audit trail; ADR 0148 precedent);
   - `preview_tables=[export_files.sample_table(columns, rows)]`;
   - `pii_scan_text=export_files.scan_text(data)`. The PII gate is unchanged;
   - `new_info` exactly:
     `{"Rows": f"{n:,}", "Columns": str(len(columns)), "Content returned to {agent}": "None in the
     tool result: the rows are written to the file below, where {agent} can open it", "Saved to":
     f"{destination_dir.rstrip('/')}/{file_name}"}`;
   - `details_text` as one line per entry of `preview` then `new_info`, as `f"{key}: {value}"`
     joined by `"\n"`, plus a last line `f"SHA-256: {sha256}"`;
   - `args` holding every tool argument except `reason`, plus `"sha256": sha256`, so an approval is
     bound to the exact content (ADR 0073).
5. Only after the gate: `delivered = local_files.deliver_file(destination_dir, file_name, data,
   export_files.MIME_TYPES[fmt], download_mode=self.download_mode)`, then return
   `export_files.export_summary(columns, rows, data, delivered)`.

### 3.3 `grist_export_records`

`ToolSpec(name="grist_export_records", read_only=True)`. Params, in order: `doc_id`
(`_DOC_ID_DESCRIPTION`), `table_id` (`_TABLE_ID_DESCRIPTION`), `filter` (same description as
`grist_get_records`'s), `destination_dir` (required str), `file_name` (str, optional, default
`""`), `_REASON`.

- `destination_dir` description: `"Folder on the user's computer to write the file to, absolute or
  starting with ~/."`
- `file_name` description: `"File name ending in .csv or .json; the ending picks the format.
  Empty means <table_id>.csv."`
- Description: `"Write every record of one table of a Grist document, optionally filtered, to a
  CSV or JSON file on the user's computer, in one approval. The first column is the record id
  (id), then the table's columns by column id; dates are written as YYYY-MM-DD, so the file can
  be edited and passed back to grist_update_csv with key_column id. Up to 50000 records. Returns
  {rows, columns, sha256, path, name, size_bytes, delivery}, never the records; read the file at
  path (a file of the same name may make it land under a numbered name). Use grist_get_records to
  look at a few records. Not available on an organization-managed install. Requires user
  approval."`
- Flow: `validate_doc_id`, `validate_identifier(table_id, "table")` (a `GristClientError` becomes a
  `ValueError`, as in `_get_records`), `_parse_filter(filter)`,
  `export_files.check_destination`, `export_files.export_file_name(file_name, table_id)`,
  `export_files.refuse_org("grist_export_records", self.download_mode, "grist_get_records")`. Then
  `doc = await self._doc_info(doc_id)`, `columns = await self._fetch(self._client.list_columns,
  doc_id, table_id)`, `page = await self._fetch(self._client.get_records, doc_id, table_id,
  filters=filters, limit=export_files.EXPORT_MAX_ROWS)`. If `page.truncated`, raise
  `ValueError("More than 50,000 records match; narrow the export with filter.")`.
- Columns: `["id", *[c.id for c in columns]]`. Row: `[record.id, *[_export_value(record.fields.get(c.id),
  c.type) for c in columns]]`. New module-level helper in `grist.py`:
  - `Date` column with a number (not bool): `(_EPOCH + timedelta(seconds=value)).date().isoformat()`;
  - a column whose type starts with `DateTime` with a number (not bool): `(_EPOCH +
    timedelta(seconds=value)).isoformat().replace("+00:00", "Z")`;
  - a list whose first element is `"L"` (ChoiceList, RefList): `value[1:]`;
  - anything else: unchanged.
- `gated_call` (§3.2): `tool="grist_export_records"`, `tool_name="Export Grist Records"`,
  `summary=f"Export {n} record(s) from {doc.name} / {table_id} to {file_name}"`,
  `sender=doc.name`, `preview={"Server": self._client.host, "Document": doc.name, "Team": doc.team
  or _NO_TEAM, "Table": table_id, "Filter": filter or "(none)"}`, `raw_data={"doc_id", "table_id",
  "rows": n, "columns": columns, "sha256"}`, `my_email=""`.
- Policy: `TOOL_TO_OPERATION["grist_export_records"] = "grist.read_records"`, `TOOL_TO_GATE[...] =
  "review"`, `registry.py` verb `Verb.READ`, `gate.py` layout `WIDE`. A `grist.document` read rule
  for the document therefore covers it.

### 3.4 `salesforce_export_report`

`ToolSpec(name="salesforce_export_report", read_only=True)`. Params, in order: `report_id`,
`columns`, `filters`, `page_by` (descriptions as on `salesforce_run_report`), `cell_format` (str,
optional, default `"value"`), `destination_dir`, `file_name` (as in §3.3 but `Empty means
<report_id>.csv.`), `reason`.

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
- Flow: `_parse_report_filters(filters)`, `column_list` as in `_run_report`, the `cell_format`
  check, `export_files.check_destination`, `export_files.export_file_name(file_name, report_id)`,
  `export_files.refuse_org("salesforce_export_report", self.download_mode,
  "salesforce_run_report")`.
  - With `page_by`: loop `page = await self._fetch(self._sf.run_report_page, report_id, page_by,
    column_list or None, filter_list or None, after, runs, remaining)`, starting with
    `after=None, runs=0, remaining=None`. After each page: collect its rows, `runs += 1`; stop when
    `page.all_data`; otherwise `after = page.keys[-1]` and `remaining = page.row_count -
    len(page.keys)`.
  - Without `page_by`: `result = await self._fetch(self._sf.run_report, report_id, column_list or
    None, filter_list or None, False)`, `runs = 1`. If `set(result.get("factMap") or {}) !=
    {"T!T"}`, raise `ValueError("salesforce_export_report: this report is grouped; pass page_by
    to read it as one table")`. If `result.get("allData") is False`, raise
    `ValueError("salesforce_export_report: the report has more than 2,000 rows; pass page_by (a
    column with a unique value per row) to export all of them")`.
  - Columns: the first (or only) result's `reportMetadata.detailColumns`. Rows:
    `factMap["T!T"]["rows"][i]["dataCells"]`, each cell through `_export_cell(cell, cell_format)`:
    in `"label"` mode `cell.get("label", "")`; in `"value"` mode, `v = cell.get("value")`. Return
    `v` when it is `None`, str, int, float or bool; return `v["amount"]` when it is a dict with an
    `"amount"` key; return `cell.get("label", "")` otherwise.
- `gated_call`: `tool="salesforce_export_report"`, `tool_name="Export Salesforce Report"`,
  `summary=f"Export report: {report_name}"` (the report name from `reportMetadata.name`, else
  `report_id`, as in `_run_report`), `sender="Salesforce"`. `preview` is `{"Report", "Report ID"}`
  plus `"Columns"`, `"Filters"` (same `_filter_value_summary` text) and `"Paged by"` when given, plus
  `"Report runs": str(runs)`. `raw_data={"report_id", "rows": n, "columns": columns, "sha256"}`.
  `my_email=self.my_email`.
- `SalesforceConnector.__init__` gains `self.download_mode: str = "local"`;
  `daemon_main.build_connectors` sets it like Grist's (`daemon_main.py:1477`: build
  `salesforce_connector = SalesforceConnector(salesforce_client)`, set
  `salesforce_connector.download_mode = download_mode`, append it).
- Policy: operation `salesforce.run_report`, gate `review`, verb `Verb.READ`, layout `WIDE`. An
  approved-report rule therefore covers it, and the popup's "Always allow → this report" proposal
  applies to it automatically through `TOOL_REGISTRY`.

### 3.5 `grist_export_sql`

Client, `grist_client.py`: `def run_sql(self, doc_id: str, sql: str, args: list[Any]) ->
list[dict[str, Any]]`. It calls `validate_doc_id(doc_id)`, then `self._request("POST",
f"/api/docs/{doc_id}/sql", json_body={"sql": sql, "args": args})`, and returns
`[_as_dict(_as_dict(r).get("fields")) for r in _as_list(_as_dict(body).get("records"))]`. It sends
no `timeout`, so Grist's server default applies. It logs only the host and the row count, never
the query.

Connector, `grist.py`: `ToolSpec(name="grist_export_sql", read_only=True)`. Params: `doc_id`, `sql`
(required str), `args` (str, optional, default `""`), `destination_dir`, `file_name` (as §3.3 but
`Empty means query.csv.`), `_REASON`.

- `sql` description: `"One read-only SQLite SELECT (or WITH … SELECT) over the document's tables,
  by table and column id, such as SELECT c.id, c.Email, o.Stage FROM Contacts c JOIN Deals o ON
  o.Contact = c.id. Use ? for values and pass them in args. No semicolons or comments; up to
  10000 characters."`
- `args` description: `"A JSON array of values for the ? placeholders in sql, such as [\"CF\",
  2026]. Empty means none."`
- Description: `"Run one read-only SQL query over a Grist document and write the result to a CSV
  or JSON file on the user's computer, in one approval. Use it to join or filter across tables;
  use grist_export_records for one table. Cells are written as Grist stores them: dates as
  seconds since 1970, true/false as 1/0. Grist runs SQL only for people who can copy the whole
  document. Up to 50000 rows. Returns {rows, columns, sha256, path, name, size_bytes, delivery},
  never the rows. Not available on an organization-managed install. Requires user approval."`
- `_validate_sql(raw) -> str`: strip; remove one trailing `;` and strip again. Raise
  `ValueError(_SQL_ERROR)` when the result is empty, longer than 10000 characters, contains `;`,
  `--` or `/*`, or its first word (`re.match(r"[A-Za-z]+", sql)`) lower-cased is not `select` or
  `with`. `_SQL_ERROR = "sql must be one SELECT (or WITH … SELECT) statement of up to 10000
  characters, with no semicolons or comments."`
- `_parse_sql_args(raw) -> list[Any]`: empty means `[]`; otherwise JSON, a list of at most 100
  values each of type `_SCALARS`, else `ValueError(_SQL_ARGS_ERROR)`. `_SQL_ARGS_ERROR = "args
  must be a JSON array of up to 100 text, number, true/false or null values for the ?
  placeholders in sql."`
- The connector sends `f"SELECT * FROM ({sql}) LIMIT {export_files.EXPORT_MAX_ROWS + 1}"`, so the
  statement Grist sees is always one `SELECT` with the caller's query as a subquery. More than
  50,000 rows back raises `ValueError("The query returns more than 50,000 rows; narrow it.")`.
- A `RuntimeError` whose `__cause__` is `GristAccessDenied` becomes
  `RuntimeError("Grist refused the SQL query (HTTP 403). Grist runs SQL only for people who can
  copy the whole document; use grist_export_records instead.")`, raised `from exc.__cause__`, the
  way `_list_documents` does.
- Columns: the keys of the first record in order (`[]` with no rows). Rows: `[record.get(c) for c
  in columns]`, written unconverted.
- `gated_call`: `tool="grist_export_sql"`, `tool_name="Export Grist Query"`, `summary=f"Export
  {n} row(s) from a query on {doc.name} to {file_name}"`, `sender=doc.name`, `preview={"Server",
  "Document", "Team", "Query": sql if len(sql) <= 200 else sql[:200] + "…", "Arguments":
  str(len(args))}`. `details_text` (§3.2) also gets `"\n\nQuery:\n" + sql + "\n\nArguments: " +
  json.dumps(args, ensure_ascii=False)` appended. `raw_data={"doc_id", "rows": n, "columns":
  columns, "sha256"}`, `my_email=""`.
- Policy: operation `grist.read_records`, gate `review`, verb `Verb.READ`, layout `WIDE`. A
  `grist.document` read rule covers it: the same document, the same Grist access rules, and Grist
  itself already requires full copy access.
- Live check: `scripts/qa_fixture_recorder.py` gains `_check_grist_sql(client, doc_id, table_id)
  -> CheckResult`, appended in `check_grist` after `_check_grist_paging`. It runs
  `client.run_sql(doc_id, f"SELECT id FROM {table_id} ORDER BY id", [])` and compares the ids with
  `client.get_records(doc_id, table_id, limit=500)`. Row name `"run_sql"`; on `GristAccessDenied`
  it fails with note `"Grist refused /sql (HTTP 403): the QA account or the OAuth scope does not
  cover it"`. Nothing is recorded.

### 3.6 Rejected alternatives

- **An `output_path` option on `grist_get_records` and `salesforce_run_report`.** The return shape
  would depend on an argument (ADR 0116 keeps a tool's return shape stable), and the card and
  tool description would have to explain two behaviours. Separate tools keep both honest.
- **New operation keys for the exports.** The existing per-document and per-report rules would not
  cover them, and `approved_report_ids` sits in the frozen v1 `SCOPE_SELECTORS` set. The export
  reads the same data with the same rights; only where the rows go differs.
- **One `output_path`.** `deliver_file` takes a folder and a name (ADR 0007), as
  `drive_download_file` does. Splitting them lets the default name come from the table or report.
- **Paging a Grist export by `after_id`.** It costs about the square of the page count in transfer
  (ADR 0146). One request of up to 50,001 rows costs Grist the same table load as one page.
- **Organization mode delivery by link.** A staged link would hand the whole table out through
  the org daemon's staging store, which is a separate delivery decision. Exports are refused there.
- **Exempting exports from the PII gate.** The user chose to keep it.
- **A server-side `timeout` on the SQL call.** Grist caps it by server configuration, which
  PrivacyFence cannot see. The default applies.

## ADRs

Numbers are the next free ones in `docs/adr/` today (last is 0150). Renumber if a parallel branch
takes them first (`docs/adr/README.md` rule 6).

- **ADR 0151 — Exports write a file and return only counts and a hash.** `grist_export_records`
  and `salesforce_export_report` are separate tools that reuse their read tool's operation key,
  show one review card after the whole fetch, keep the PII gate, bind the approval to the file's
  SHA-256, and are refused on an organization-managed install. Rejects §3.6's first five
  alternatives.
- **ADR 0152 — `grist_export_sql` reads through Grist's SQL endpoint.** Supersedes ADR 0146's
  "SQL endpoint" alternative for this one tool, not for paging `grist_get_records`. It covers
  connector-side `SELECT`-only validation, the `LIMIT` subquery wrap, the 403 message, cells written
  as SQLite stores them, the `grist.read_records` mapping, and the live check that settled the
  OAuth scope.

## Manual steps

There is nothing to do before implementation. After the last merge, before the PR merges, see the
[step-by-step page](https://claude.ai/artifact/CBHByCthEfgm3xzbyCQbQd):

- **ma1:** set up the auto-accept rules for the scheduled run in Settings → Auto-accept.
- **ma2:** run each export once against real data and confirm one card per export, the file
  contents, and an incremental Salesforce filter.

## Risks and open questions

- **The OAuth scope may not cover `/sql`.** The live check in p5 reports it. If it fails with 403
  for an OAuth sign-in but passes with an API key, ADR 0152 records that and the tool description
  gains the sentence `"With an OAuth sign-in Grist may refuse SQL; an API key works."` A p5
  worker who sees any other result stops with `status=blocked`.
- **Salesforce date filters.** `greaterOrEqual` with `"2026-10-04"` on `LAST_UPDATE` is what the
  Analytics API documents, but the column's API name differs per report type. The worker does not
  check this; ma2 does.
- **`daemon_main.build_connectors` shape.** If `download_mode` is not in scope at the Salesforce
  block (`daemon_main.py:1477`), the p3 worker stops with `status=blocked` rather than moving code.
- **Same operation key as a read.** A rule someone made for interactive report reads now also
  covers exports of that report. ADR 0151 states this; it is intended.
- **Tests pin tool sets.** `tests/unit/test_connector_tool_annotations.py`,
  `tests/unit/connectors/test_readme_manifest_alignment.py`, `tests/unit/test_systemic_gate_invariants.py`
  and `tests/unit/policy/test_registry.py` may each need the new tool name added. The brief says to
  add it in the same way as the neighbouring entry. A worker who finds such a test asserting a
  count or a claim that the change would make false (for example "the connector defines seven
  tools" in a docstring) updates the count, and stops with `status=blocked` only if the test
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
    why: CI uses fakes; only a real Grist server and Salesforce org show the SQL scope, real date filters, and real cell formats.
verify_after_merge:
  - python3 -m pytest tests/unit/test_export_files.py tests/unit/connectors/test_grist_connector.py tests/unit/connectors/test_salesforce_connector.py tests/unit/test_grist_client.py tests/unit/policy -q
  - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_connector_tool_annotations.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_systemic_gate_invariants.py -q
final_checks:
  - docs/bulk-exports-plan.md and docs/bulk-exports-plan-manual-steps.html are deleted and nothing links to them (git grep -n "bulk-exports-plan" prints nothing)
  - ADR 0151 and ADR 0152 exist in docs/adr/, have Status Accepted, and are listed in docs/adr/README.md
  - CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading
  - A connector-live-check.yml run on the feature branch is linked in the PR, with its grist run_sql row
phases:
  - id: p1-export-files
    title: Pure export-file helpers (export_files.py)
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/export_files.py
      - tests/unit/test_export_files.py
    brief: |
      Read docs/coding-and-testing-guidelines.md and docs/testing-policy.md first. Then implement
      Design §3.1 of docs/bulk-exports-plan.md exactly: the constants, error strings and function
      signatures there.
      1. Create src/privacyfence/export_files.py with a module docstring of one or two sentences
         ("Pure helpers the export tools share: file name and format, CSV/JSON rendering, the card's
         sample table and the result summary. No I/O; delivery is local_files.deliver_file.").
         Type hints on every function; `from __future__ import annotations`; stdlib only (csv, io,
         json, hashlib, typing).
      2. Implement export_file_name, check_destination, refuse_org, csv_cell, render,
         sample_table, scan_text and export_summary as specified in §3.1.
      3. Create tests/unit/test_export_files.py (`pytestmark = pytest.mark.unit`, plain functions).
         One behaviour per test, at least: test_export_file_name_defaults_to_stem_csv,
         test_export_file_name_picks_json_from_suffix (also upper-case .JSON),
         test_export_file_name_rejects_paths_dotfiles_and_other_suffixes (parametrized over
         "a/b.csv", "a\\b.csv", ".x.csv", "x.txt", "x", 201-char name),
         test_check_destination_rejects_empty, test_refuse_org_raises_only_in_org_mode,
         test_csv_cell_formats (parametrized: None, True, False, 3, 3.0, 2.5, "x", ["a", "b"],
         {"k": 1}), test_render_csv_quotes_commas_and_newlines,
         test_render_csv_has_no_bom_and_lf_line_ends, test_render_json_is_a_list_of_objects,
         test_sample_table_shows_twenty_rows_and_truncates_long_cells,
         test_scan_text_caps_at_one_million_chars, test_export_summary_merges_delivery.
      4. Run `ruff check src/privacyfence/export_files.py tests/unit/test_export_files.py` and
         `python3 -m mypy src/privacyfence/export_files.py`.
      No changelog line (internal module).
    acceptance:
      - python3 -m pytest tests/unit/test_export_files.py -q passes
      - python3 -m pytest tests/unit/test_export_files.py -q --cov=privacyfence.export_files --cov-branch --cov-report=term-missing reports 100% for export_files.py
      - ruff check src/privacyfence/export_files.py tests/unit/test_export_files.py passes
      - python3 -m mypy src/privacyfence/export_files.py passes

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
      - tests/unit/policy/**
      - tests/unit/test_connector_tool_annotations.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_systemic_gate_invariants.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read docs/coding-and-testing-guidelines.md, ADRs 0007, 0144, 0146 and 0148, and Design
      §3.2 and §3.3 of docs/bulk-exports-plan.md. Commit 2dae2772 is the pattern for wiring a new
      Grist tool through the registries and tests.
      1. src/privacyfence/connectors/grist.py: `from .. import export_files`. Add the
         grist_export_records ToolSpec after grist_get_records in tool_specs(), with the exact
         params and description in §3.3. Add the dispatch branch in call(). Add the module-level
         helper `_export_value(value: Any, column_type: str) -> Any` per §3.3 next to
         `_csv_cell_text`. Add `async def _export_records(self, doc_id: str, table_id: str,
         destination_dir: str, filter: str = "", file_name: str = "") -> Any` in the "Review gate
         (reads)" section, following §3.2 steps 1-5 and §3.3 exactly (strings included).
      2. src/privacyfence/auto_accept.py: add `"grist_export_records": "grist.read_records"` to
         TOOL_TO_OPERATION and `"grist_export_records": "review"` to TOOL_TO_GATE, each next to
         grist_get_records.
      3. src/privacyfence/policy/registry.py: `"grist_export_records": Verb.READ` next to
         grist_get_records.
      4. src/privacyfence/gate.py: add `"grist_export_records": WIDE` to the layout map at line
         259.
      5. Run the unit suite once (`python3 -m pytest tests/unit -q`). Each test that pins the set
         of tools (annotations, readme/manifest alignment, systemic gate invariants, policy
         registry/propose) gets grist_export_records added the way grist_get_records appears
         there. Stop with status=blocked if a test asserts a policy rule that §3.3 contradicts.
      6. tests/unit/connectors/test_grist_connector.py: add `class TestExportRecords`, using the
         existing make_connector() and gated_call_spy fixtures. Fake `local_files.deliver_file`
         with monkeypatch on `grist_module.local_files` so no file is written, capturing its
         arguments. Tests: test_writes_csv_with_id_first_and_dates_as_iso;
         test_writes_json_when_file_name_ends_json; test_choice_list_drops_the_L_marker;
         test_datetime_column_written_as_utc_iso; test_one_card_with_sample_and_no_cell_in_raw_data;
         test_approval_args_carry_sha256_and_options; test_filter_is_passed_to_the_client;
         test_more_than_50000_records_is_refused_before_the_card;
         test_org_mode_is_refused_before_any_request; test_bad_file_name_is_refused_before_any_request;
         test_deny_writes_nothing (gated_call raising leaves deliver_file uncalled);
         test_result_is_summary_only (keys exactly rows, columns, sha256 plus the delivery keys).
         Add grist_export_records to the TestToolDefinitions/TestEveryToolIsAudited coverage the
         way grist_get_records is covered there.
      7. Regenerate docs: `python3 scripts/generate_tools_reference.py` and
         `python3 scripts/generate_always_allow_reference.py`, and commit the changed
         docs/tools-reference.md and docs/always-allow-rules-reference.md.
      8. `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py`.
      No changelog line here; p5 writes it.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q -k "ExportRecords" passes, with at least the 12 tests named in the brief
      - python3 -m pytest tests/unit -q passes
      - grep -nE '"grist_export_records": +"grist.read_records"' src/privacyfence/auto_accept.py matches
      - grep -n "grist_export_records" docs/tools-reference.md docs/always-allow-rules-reference.md matches in both files
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q --cov=privacyfence.connectors.grist --cov-branch --cov-report=term-missing shows no missed line inside _export_records or _export_value
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
      - tests/unit/policy/**
      - tests/unit/test_connector_tool_annotations.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_systemic_gate_invariants.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read ADRs 0007 and 0133 and Design §3.2 and §3.4 of docs/bulk-exports-plan.md. The
      grist_export_records commit from p2 is the pattern to copy for registries, tests and docs.
      1. src/privacyfence/connectors/salesforce.py: `from .. import export_files, local_files`.
         `self.download_mode: str = "local"` in __init__. Add the salesforce_export_report ToolSpec
         after salesforce_run_report, with the exact params and description in §3.4, and the
         dispatch branch in call(). Add the module-level `_export_cell(cell: dict, cell_format: str)
         -> Any` and `async def _export_report(self, report_id: str, destination_dir: str,
         columns: str = "", filters: str = "", page_by: str = "", cell_format: str = "value",
         file_name: str = "") -> Any`, following §3.2 and §3.4 exactly. The paging loop calls
         self._sf.run_report_page with positional args in the order _run_report uses (report_id,
         page_by, columns, filters, after, pages_done, remaining).
      2. src/privacyfence/daemon_main.py, the Salesforce block near line 1477: build
         `salesforce_connector = SalesforceConnector(salesforce_client)`, set
         `salesforce_connector.download_mode = download_mode`, append it. If `download_mode` is not
         defined in build_connectors before that block, stop with status=blocked.
      3. auto_accept.py: `"salesforce_export_report": "salesforce.run_report"` and `"review"`, next
         to salesforce_run_report. policy/registry.py: `Verb.READ`. gate.py: `WIDE` at line 258.
      4. Run `python3 -m pytest tests/unit -q` and add salesforce_export_report to every test that
         pins the tool set, the way salesforce_run_report appears there. In
         tests/unit/policy/test_propose.py, add a case asserting proposals_for("salesforce_export_report",
         ctx with args {"report_id": "R1"}) offers the approved_report_ids "this report" proposal,
         next to the salesforce_run_report case at line 79.
      5. tests/unit/connectors/test_salesforce_connector.py: add `class TestExportReport`, reusing
         the file's fixtures and tests/fixtures/salesforce_analytics builders; fake
         local_files.deliver_file via monkeypatch on salesforce_module.local_files. Tests:
         test_unpaged_tabular_report_writes_all_rows; test_paged_export_runs_until_all_data
         (three pages, asserts after/pages_done/remaining passed on each call);
         test_grouped_report_without_page_by_is_refused; test_cut_off_report_without_page_by_is_refused;
         test_value_format_uses_amount_and_raw_values; test_label_format_uses_labels;
         test_bad_cell_format_is_refused_before_any_request; test_filters_are_passed_to_each_run;
         test_one_card_with_report_runs_line; test_org_mode_is_refused_before_any_request;
         test_deny_writes_nothing; test_result_is_summary_only.
      6. tests/unit/test_daemon_main.py: one test that build_connectors sets download_mode on the
         Salesforce connector, copied from the existing Grist download_mode test if there is one,
         else modelled on the nearest test that builds the Salesforce connector.
      7. Regenerate docs/tools-reference.md and docs/always-allow-rules-reference.md with the two
         scripts, then `ruff check .`, `python3 -m mypy src/privacyfence`,
         `python3 scripts/mypy_strict_modules.py`.
      No changelog line here.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_salesforce_connector.py -q -k "ExportReport" passes, with at least the 12 tests named in the brief
      - python3 -m pytest tests/unit -q passes
      - grep -nE '"salesforce_export_report": +"salesforce.run_report"' src/privacyfence/auto_accept.py matches
      - grep -n "salesforce_connector.download_mode = download_mode" src/privacyfence/daemon_main.py matches
      - python3 -m pytest tests/unit/connectors/test_salesforce_connector.py -q --cov=privacyfence.connectors.salesforce --cov-branch --cov-report=term-missing shows no missed line inside _export_report or _export_cell
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p4-grist-export-sql
    title: grist_export_sql tool and GristClient.run_sql
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
      - tests/unit/policy/**
      - tests/unit/test_connector_tool_annotations.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_systemic_gate_invariants.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read ADRs 0145 and 0146 and Design §3.2 and §3.5 of docs/bulk-exports-plan.md. The p2
      commit is the pattern for the tool; `_check_grist_paging` in scripts/qa_fixture_recorder.py
      is the pattern for the live check.
      1. src/privacyfence/grist_client.py: add `run_sql` exactly as §3.5 says, after
         get_records_by_id. Log with logger.debug the host and row count only.
      2. tests/unit/test_grist_client.py: tests in the file's existing style (its fake session):
         test_run_sql_posts_sql_and_args_to_the_sql_endpoint, test_run_sql_returns_fields,
         test_run_sql_403_raises_access_denied, test_run_sql_validates_doc_id.
      3. src/privacyfence/connectors/grist.py: `import re`; the constants _SQL_ERROR and
         _SQL_ARGS_ERROR; `_validate_sql` and `_parse_sql_args` next to `_parse_filter`; the
         grist_export_sql ToolSpec after grist_export_records with the exact params and
         description in §3.5; the dispatch branch; `async def _export_sql(self, doc_id: str, sql:
         str, destination_dir: str, args: str = "", file_name: str = "") -> Any` following §3.2 and
         §3.5 exactly. The connector passes the wrapped statement (`SELECT * FROM (<sql>) LIMIT
         50001`) to client.run_sql.
      4. auto_accept.py: `"grist_export_sql": "grist.read_records"` and `"review"`.
         policy/registry.py: `Verb.READ`. gate.py: `WIDE`. Add grist_export_sql to every test that
         pins the tool set, as p2 did.
      5. tests/unit/connectors/test_grist_connector.py: `class TestExportSql` with
         test_wraps_query_in_a_limited_subquery; test_passes_args_to_the_client;
         test_rejects_non_select_semicolons_and_comments (parametrized over "DELETE FROM T",
         "SELECT 1; SELECT 2", "SELECT 1 -- x", "SELECT /* x */ 1", "", 10001-char query);
         test_allows_one_trailing_semicolon_and_with; test_rejects_bad_args (parametrized:
         not JSON, object, 101 values, a nested list); test_more_than_50000_rows_is_refused;
         test_403_gives_the_copy_access_message; test_columns_follow_first_record_and_cells_are_unconverted;
         test_card_shows_query_and_arguments_count; test_org_mode_is_refused_before_any_request;
         test_deny_writes_nothing; test_result_is_summary_only.
      6. scripts/qa_fixture_recorder.py: add `_check_grist_sql` exactly as §3.5 says, and append
         its result in check_grist right after `_check_grist_paging`. In
         tests/unit/test_qa_fixture_recorder.py add tests with a MagicMock client for the pass,
         id-mismatch and 403 cases, next to the tests of `_check_grist_paging` (or, if it has
         none, next to the other check_grist tests).
      7. Regenerate the two reference docs; `ruff check .`, `python3 -m mypy src/privacyfence`,
         `python3 scripts/mypy_strict_modules.py`, `bandit -c pyproject.toml -r src`.
      No changelog line here.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_grist_connector.py -q -k "ExportSql" passes
      - python3 -m pytest tests/unit/test_grist_client.py -q -k "run_sql" passes, with the 4 tests named in the brief
      - python3 -m pytest tests/unit -q passes
      - grep -n "_check_grist_sql(client, doc_id, table_id)" scripts/qa_fixture_recorder.py matches
      - grep -nE '"grist_export_sql": +"grist.read_records"' src/privacyfence/auto_accept.py matches
      - ruff check ., python3 -m mypy src/privacyfence and bandit -c pyproject.toml -r src pass

  - id: p5-retire
    title: ADRs, reference docs, changelog, live check, and retire the plan
    depends_on: [p4-grist-export-sql]
    complexity: S
    touches:
      - docs/adr/0151-*.md
      - docs/adr/0152-*.md
      - docs/adr/README.md
      - docs/grist-setup.md
      - docs/salesforce-setup.md
      - docs/connector-qa.md
      - docs/approvals-and-policy.md
      - CHANGELOG.md
      - src/privacyfence/connectors/grist.py
      - docs/tools-reference.md
      - docs/bulk-exports-plan.md
      - docs/bulk-exports-plan-manual-steps.html
    brief: |
      Read CONTRIBUTING.md "Decisions, plans and ADRs", docs/adr/README.md (template and rules),
      ADR 0148 as the model ADR, and sections 3, 4 and 6 of docs/bulk-exports-plan.md.
      1. Dispatch connector-live-check.yml on this phase's branch with the GitHub MCP
         actions_run_trigger tool (no inputs). It shares a concurrency group with
         qa-record-fixture.yml, so a queued run is normal. Read the
         connector-live-check-report artifact's grist run_sql row. If it passes, continue. If it
         fails with the 403 note and the report shows the QA account signs in with OAuth, add the
         sentence "With an OAuth sign-in Grist may refuse SQL; an API key works." to the end of
         grist_export_sql's description (before "Not available on…") in
         src/privacyfence/connectors/grist.py, regenerate docs/tools-reference.md with
         scripts/generate_tools_reference.py, and record this in ADR 0152's Consequences. Any
         other result: stop with status=blocked and quote the row.
      2. Write docs/adr/0151-exports-write-a-file-and-return-only-counts-and-a-hash.md and
         docs/adr/0152-grist-export-sql-reads-through-the-sql-endpoint.md from section 4 and
         §3.6, in the README's template (Status "Accepted — <today>. Implemented.", Context,
         Decision, Alternatives considered, Consequences, Verification naming the test classes
         TestExportRecords, TestExportReport, TestExportSql, Related). 0151 states that the PII
         gate is unchanged and that an existing read rule covers the export. 0152 states that it
         supersedes only ADR 0146's SQL-endpoint alternative, for this tool, and links the
         live-check run. If 0151 or 0152 is already taken on main, take the next free numbers and
         use them everywhere below. Never edit ADR 0146's body.
      3. docs/adr/README.md: add both rows to the index, in number order, with Status Accepted.
      4. docs/grist-setup.md: a short "Exporting to a file" section after the CSV import/update
         section, covering grist_export_records and grist_export_sql, their limits (50000 rows,
         org mode refused), and that a document read rule covers them. docs/salesforce-setup.md:
         the same for salesforce_export_report, with the incremental LAST_UPDATE filter example
         from §3.4 and the IDs-only `columns` example. docs/connector-qa.md: add the run_sql row to
         the Grist checks list. docs/approvals-and-policy.md: one sentence where it lists what a
         Grist document rule and an approved-report rule cover, saying the export tools are
         included and that the PII gate still applies. Link the ADRs; do not retell them.
      5. CHANGELOG.md, under "## [Unreleased]", a "### Added" subsection with three bullets in the
         file's style (bold lead sentence, then one or two plain sentences): Grist table export
         to a file in one approval; Salesforce report export with internal paging and incremental
         filters; Grist SQL query export. Open no version heading.
      6. Delete docs/bulk-exports-plan.md and docs/bulk-exports-plan-manual-steps.html. Run
         `git grep -n "bulk-exports-plan"` and remove any reference it prints.
      7. Run the full /dod gate.
    acceptance:
      - test -f docs/adr/0151-exports-write-a-file-and-return-only-counts-and-a-hash.md && test -f docs/adr/0152-grist-export-sql-reads-through-the-sql-endpoint.md (or the renumbered files named in the commit message)
      - grep -n "0151\|0152" docs/adr/README.md matches two rows
      - git grep -n "bulk-exports-plan" prints nothing
      - awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -c "grist_export_records\|salesforce_export_report\|grist_export_sql" prints 3 or more
      - the connector-live-check.yml run URL is in the phase's final report, with the grist run_sql row's result
      - python3 -m pytest tests/unit -q and ruff check . pass
```
