# Salesforce report paging plan

Baseline: this plan branch is cut from `feature/plugin-framework-cr`
([PR #856](https://github.com/privacyfence/privacyfence/pull/856), head `f6ebb5ec`), not from
`main`, because it builds on #856's paged source API (ADR 0128, `plugins/cursors.py`,
`ctx.source.pages`). The feature branch is cut from this plan branch, so its PR targets `main`
only after #855 and #856 have merged. Until then its diff also contains theirs. Say so at the top
of the PR body, the way #856 does for #855.

## Goal

[Issue #854](https://github.com/privacyfence/privacyfence/issues/854). Salesforce's Analytics API
returns at most 2,000 detail rows per report run. Today, both the MCP tool `salesforce_run_report`
and the plugin source operation `salesforce.report_run` stop there and say "narrow it with
filters". After this change, a caller passes `page_by`, a report column whose values are unique per
row, and reads the whole report one page at a time. Each page is one run of the saved report,
sorted by that column and starting after the last value of the previous page. The saved report is
never changed and no SOQL is used. A paged read either finishes or fails with a clear error. It
never returns a partial, silently cut result. Without `page_by`, nothing changes.

## Current state

- `src/privacyfence/salesforce_client.py:117` `build_report_metadata(saved, columns, filters)`
  narrows one run. `columns` must be a subset of the saved `detailColumns`. Each filter becomes one
  or more `reportFilters` entries. Saved `reportBooleanFilter` logic is wrapped in parentheses and
  ANDed with the new groups (`:183-191`). Values with a comma are refused (`:150-155`), and there
  are at most `MAX_REPORT_FILTERS = 20` filters (`:110`).
- `salesforce_client.py:548` `SalesforceClient.run_report(report_id, columns, filters,
  summary_only)` does a GET for a plain run. With overrides it does `describe` and then a POST with
  `{"reportMetadata": ...}`, always with `includeDetails`. It has no sort and no paging.
- `src/privacyfence/connectors/salesforce.py:388` `_run_report` parses before gating, fetches, and
  then calls `gated_call` with `gate="review"`. When `allData is False` (`:449-457`), it prefixes the
  details with "Salesforce returned only the first 2,000 detail rows…" and sets
  `new_info["Rows"] = "Cut off at Salesforce's 2,000-row limit"`. The ToolSpec description is at
  `:247-258`. `docs/tools-reference.md` is generated from it by `scripts/generate_tools_reference.py`.
- `src/privacyfence/plugins/source_ops.py:212-233`: `_validate_salesforce` takes `report_id` and
  `filters`, and `_run_salesforce` returns `client.run_report(...)` with no cursor. The adapter
  entry (`:419-426`) has `bound=lambda p: {}`. It is the one operation ADR 0128 left unpaged, and
  `docs/plugin-protocol.md:345,366` says so.
- `src/privacyfence/plugins/cursors.py` is the shared cursor envelope:
  `encode(operation, bound, state)` and `decode(cursor, operation, bound)` raise `CursorError`
  (a `ValueError`) for malformed or foreign cursors. The cursor is base64url JSON, at most 4,096
  characters, unsigned. `source_ops._serve` decodes it with `adapter.bound(params)`, and adapters
  check their own state (`_state_keys`, `_state_count`). `_fit_prefix(items, budget)` cuts a list
  to the result budget (`SOURCE_PAGE_BUDGET_BYTES`, 12 MiB minus 64 KiB).
- SDK (`plugin-sdk/src/privacyfence_plugin_sdk/plugin.py:136-229`): `ctx.source.pages(op, **params)`
  follows `next_cursor` for any operation. `collect` is limited to `_PAGED_OPERATIONS`
  (`jira.search`, `calendar.list_events`). The test host (`testing/_source.py`) serves
  `returns_pages(...)` with real cursors for any operation. `samples.get(op, page=2)` exists only
  for Jira and Calendar.
- Tests: `tests/unit/test_salesforce_client.py` has `TestRunReport` (`:520`),
  `TestBuildReportMetadata` (`:592-699`) and `TestLiveFixtureParsing` (`:854`, which loads
  `tests/fixtures/live/salesforce/*.json`; only `list_reports.json` and `get_record.json` exist).
  `tests/unit/connectors/test_salesforce_connector.py` has `TestRunReport` (`:235`), with
  `test_all_data_false_is_surfaced` at `:387`. `tests/unit/plugins/test_source_ops.py` has
  `TestAdapterSalesforce` (`:247`) and the paging pattern (`PagedJira`, `_follow`, `TestPaging`,
  `TestCursorBinding`, `TestCursorState`, `:451-700`).
- Live QA: `scripts/qa_fixture_recorder.py:970` `_check_salesforce_run_report` checks columns, a
  split ID filter and `summary_only` on the QA org's tabular report "PrivacyFence QA Report". It
  records no report fixture. `EXPECTED_FIXTURES["salesforce"]` (`:1684`) is checked on every CI
  run, so a file added there must be committed.
- Config: per-connector behaviour keys live in `settings.yaml`
  (`src/privacyfence/resources/settings.yaml.example`, for example
  `calendar.free_busy_full_event_details`). `daemon_main.build_connectors` reads them and sets them
  on the connector (`daemon_main.py:1363`). `docs/configuration-reference.md` "Connector behaviour"
  documents them, and `tests/unit/test_docs_configuration_reference.py` checks that coverage. There
  is no Salesforce key yet.
- Gate: `gate.py:1035` dedupes on all args, so calls with different `cursor` values are separate
  approvals. The approved-report rule (`policy/scopes.py:312`) matches only `report_id`, and
  `salesforce_client.py:105-109` says it relies on overrides only narrowing a report.

## Design

### D1. Client: keyset page of one run (`salesforce_client.py`)

New public names:

```python
DEFAULT_REPORT_MAX_PAGES = 50

class ReportPagingError(SalesforceClientError):
    """A paged report read that cannot continue. ``reason`` is one of REPORT_PAGING_REASONS."""
    def __init__(self, reason: str, message: str) -> None: ...
    # attribute: reason: str

REPORT_PAGING_REASONS = frozenset({"bad_page_by", "not_unique", "not_advancing", "page_limit", "not_flat"})

@dataclass
class ReportPage:
    result: dict          # the run's report result, as Salesforce returned it
    keys: list[str]       # the page_by key text of each detail row, in row order
    all_data: bool        # Salesforce's allData for this run

def build_keyset_metadata(
    saved: dict, columns: list[str] | None, filters: list[ReportFilter] | None,
    page_by: str, after: str | None,
) -> dict: ...

def report_page_keys(result: dict, page_by: str, after: str | None) -> list[str]: ...

def report_page_info(number: int, rows_before: int, count: int, more: bool) -> dict: ...

class SalesforceClient:
    report_max_pages: int   # set in __init__ to DEFAULT_REPORT_MAX_PAGES
    def run_report_page(
        self, report_id: str, page_by: str, columns: list[str] | None = None,
        filters: list[ReportFilter] | None = None, after: str | None = None, pages_done: int = 0,
    ) -> ReportPage: ...
```

**`build_keyset_metadata`**, in this order:

1. If `saved.get("topRows")` is truthy, raise
   `ReportPagingError("bad_page_by", "page_by cannot page a report that has a row limit; remove the row limit from the saved report")`.
   Removing a row limit would widen the report.
2. **Flatten for the run** (only when `saved.get("reportFormat")` is not `"TABULAR"`). Take
   `grouping_columns` as the `name` of each entry of `saved["groupingsDown"]` and then
   `saved["groupingsAcross"]` (missing lists count as empty), in order, without duplicates.
3. Call `build_report_metadata(saved, columns, extra_filters)`. `extra_filters` is
   `list(filters or [])`, plus `ReportFilter(page_by, "greaterThan", [after])` when `after` is not
   `None`. This keeps the saved boolean logic and the caller's filters, and adds the key filter as
   one more ANDed group. The 20-filter limit and its message apply unchanged.
4. If the report was flattened: set `metadata["reportFormat"] = "TABULAR"`,
   `metadata["groupingsDown"] = []`, `metadata["groupingsAcross"] = []`,
   `metadata["aggregates"] = ["RowCount"]`, and
   `metadata["detailColumns"] = [g for g in grouping_columns if g not in cols] + cols`, where `cols`
   is the `detailColumns` that `build_report_metadata` produced. The grouping columns come first.
   Their values were already visible as group labels, so the run shows nothing new.
5. If `page_by` is not in the final `metadata["detailColumns"]`, raise
   `ReportPagingError("bad_page_by", f"page_by {page_by!r} is not a column of this run: {', '.join(final_columns)}")`.
6. Set `metadata["sortBy"] = [{"sortColumn": page_by, "sortOrder": "Asc"}]`.

`saved` is never mutated. This is pure and unit-testable.

**`report_page_keys`**:

1. Rows: `result["factMap"]["T!T"]["rows"]`. If `factMap` has any key other than `"T!T"`, or that
   path is missing, raise
   `ReportPagingError("not_flat", "Salesforce did not return the report as one table, so it cannot be paged")`.
2. `index = result["reportMetadata"]["detailColumns"].index(page_by)`. If it is missing, raise
   `not_flat` with the same message.
3. For each row, take `cell = row["dataCells"][index]` and `key = _key_text(cell.get("value"))`.
   `_key_text` returns a `str` value that is non-empty and has no comma unchanged, an `int` (not
   `bool`) as `str(v)`, and a `float` as `str(int(v))` when `v.is_integer()` and `repr(v)`
   otherwise. Anything else returns `None`. A `None` key raises
   `ReportPagingError("bad_page_by", f"page_by {page_by!r} has a value that cannot be paged by (empty, containing a comma, or not text or a number); choose an auto-number column")`.
4. Duplicate keys within the page raise
   `ReportPagingError("not_unique", f"page_by {page_by!r} is not unique: a value repeats within one page")`.
5. If `after is not None` and `after in keys`, raise
   `ReportPagingError("not_advancing", f"page_by {page_by!r} did not advance: Salesforce returned the previous page's last value again, so the column cannot be paged with greaterThan; choose an auto-number column")`.
6. If `keys == []` and `result.get("allData") is False`, raise
   `ReportPagingError("not_advancing", f"page_by {page_by!r} did not advance: a page had no rows but Salesforce reported more")`.

**No error message ever contains a cell value**, only column API names and counts. MCP errors
reach the AI client before any card, so a value in a message would bypass the gate.

**`report_page_info(number, rows_before, count, more)`** returns
`{"number": number, "first_row": rows_before + 1 if count else 0, "last_row": rows_before + count if count else 0, "more": more}`.

**`run_report_page`**:

1. `report_id` is required (the same check as `run_report`).
2. If `pages_done >= self.report_max_pages`, raise
   `ReportPagingError("page_limit", f"stopped after {self.report_max_pages} pages without reaching the end of the report; narrow it with filters or raise salesforce.report_max_pages")`
   before any API call.
3. Inside `self._call`: `describe`, then `build_keyset_metadata(saved, columns, filters, page_by, after)`,
   then `sf.restful(path, params={"includeDetails": "true"}, method="POST", json={"reportMetadata": metadata})`.
   This is the same request shape as `run_report`'s override path.
4. `keys = report_page_keys(result, page_by, after)`. Return
   `ReportPage(result, keys, result.get("allData") is not False)`.
5. `ReportPagingError` raised inside `fn` must propagate unchanged. `_call` already re-raises
   `SalesforceClientError` subclasses.
6. Log line: `logger.info("run_report_page %s completed (%d rows)", report_id, len(keys))`. This is
   a count only.

### D2. Cursor state (both surfaces)

Both surfaces use `privacyfence.plugins.cursors` (`encode`, `decode`, `CursorError`).

- MCP: operation `"salesforce_run_report"`. Plugin: operation `"salesforce.report_run"`.
- `bound = {"report_id": ..., "page_by": ..., "columns": [...], "filters": [...]}`. MCP uses
  `column_list` and `[dataclasses.asdict(f) for f in filter_list]`. The plugin uses the validated
  `columns` (default `[]`) and `filters` (default `[]`) params as given.
- `state = {"n": <report runs done, ≥ 1>, "a": <rows returned so far, ≥ 0>, "l": <last key returned, a non-empty str>}`.
  A cursor with other keys or types is "cursor is not valid".
- The next run passes `after=state["l"]` and `pages_done=state["n"]`.

The cursor is stateless, so rows are not de-duplicated across pages by memory. Instead, the
`greaterThan` filter plus the D1 checks make a repeat impossible unless Salesforce ignored the
filter, and that case is refused (`not_advancing`). The SDK helper (D5) also refuses a key it has
already seen, as a second guard.

### D3. MCP tool `salesforce_run_report` (`connectors/salesforce.py`)

New params, in this order after `summary_only` and before `reason`:

- `ToolParam("page_by", "str", required=False, default="", description="Report column API name whose values are unique per row (an auto-number or ID column the report includes), e.g. 'Opportunity.Opp_Number__c'. Reads every row of the report, page by page in order of this column; a grouped report is read as one flat table. Leave empty for one run as saved.")`
- `ToolParam("cursor", "str", required=False, default="", description="Opaque cursor from a previous paged result's next_cursor. Pass it with the same report_id, columns, filters and page_by. Leave empty for the first page.")`

New ToolSpec description (replace the whole string):

> Run a Salesforce report by id and return the results. Returns Salesforce's Analytics API report
> result as-is: reportMetadata, reportExtendedMetadata, groupingsDown/groupingsAcross and a factMap
> holding each group's rows and aggregates. By default it runs the report as saved; columns, filters
> and summary_only narrow this one run without changing the saved report. Salesforce returns at most
> 2,000 detail rows per run: allData false means rows were cut off. To read every row, pass page_by
> (a column with a unique value per row) and call again with cursor=next_cursor until next_cursor is
> null; each page is one report run and carries page {number, first_row, last_row, more}. Get
> report_id from salesforce_list_reports. Requires user approval.

`_run_report(self, report_id, columns="", filters="", summary_only=False, page_by="", cursor="")`:

1. Parse `filters` and `columns` as today. Set `page_by = page_by.strip()`.
2. If `cursor` is set and `page_by` is empty, raise
   `ValueError("salesforce_run_report: cursor needs page_by")`. If `page_by` and `summary_only`
   are both set, raise `ValueError("salesforce_run_report: page_by has no effect with summary_only")`.
   Both are raised before any fetch or card.
3. Without `page_by`, the existing path runs byte-for-byte unchanged, including the "Cut off" text.
4. With `page_by`:
   1. Decode `cursor` per D2, or start from `n=0, a=0, l=None`. A `CursorError` or invalid state
      raises `ValueError(f"salesforce_run_report: {exc}")` (or `"salesforce_run_report: cursor is not valid"`)
      before any fetch or card.
   2. Call `page = await self._fetch(self._sf.run_report_page, report_id, page_by, column_list or None, filter_list or None, l, n)`.
      `_fetch` turns `ReportPagingError` into `RuntimeError` with the same message. No rows are
      returned and no card is shown.
   3. Compute `number = n + 1`, `count = len(page.keys)` and `more = not page.all_data`. Build
      `result_dict = dict(page.result)`, then set `result_dict["page"] = report_page_info(number, a, count, more)`
      and `result_dict["next_cursor"] = cursors.encode("salesforce_run_report", bound, {"n": number, "a": a + count, "l": page.keys[-1]})`
      if `more`, else `None`.
   4. Card: `preview` adds `"Paged by": page_by` after the Filters entry. `new_info["Rows"]` is
      `f"Page {number}: rows {first:,}–{last:,}, more pages follow"` when `more`,
      `f"Page {number}: rows {first:,}–{last:,}, last page"` otherwise, and
      `f"Page {number}: no rows, last page"` when `count == 0`. The dash is an en dash (U+2013).
      There is no "Cut off" prefix and no "Cut off" row on a paged read. `summary` stays
      `f"Run report: {report_name}"`. `report_data` follows today's rules.
   5. `gated_call(... raw_data=page.result, filtered_data=result_dict, args={... existing ..., "page_by": page_by, "cursor": cursor})`.
      Every other argument is as today, with `preview_tables=_report_tables(result_dict)`. Each page
      is its own card. The approved-report rule auto-approves pages exactly as it auto-approves a
      single run, because paging only narrows.
5. Keep one `gated_call` site. Compute the paged differences before it instead of duplicating the
   call.

Config: `daemon_main.build_connectors` sets
`client.report_max_pages = _salesforce_report_max_pages(config)` after the client is built. The
helper returns `DEFAULT_REPORT_MAX_PAGES` when `config["salesforce"]["report_max_pages"]` is
missing. When the value is not an `int` (a `bool` counts as not an `int`) or is below 1, it logs
`logger.warning("salesforce.report_max_pages must be a whole number of at least 1; using %d", DEFAULT_REPORT_MAX_PAGES)`
and returns the default. `settings.yaml.example` gets a `salesforce:` section with
`report_max_pages: 50` and a comment. `docs/configuration-reference.md` "Connector behaviour" gets
the row
`| \`salesforce.report_max_pages\` | int | \`50\` | \`50\` | Most report runs one paged \`salesforce_run_report\` or plugin \`salesforce.report_run\` read may use (each page is one run and counts against the org's report-run limits). A read that needs more fails. |`.

### D4. Plugin source operation `salesforce.report_run` (`plugins/source_ops.py`)

`_validate_salesforce` keeps everything it does today and adds:

- `columns`: optional. When present it must be a list of 1 to 100 strings, each non-empty and at
  most 256 characters, else `_bad("params.columns must be a list of column names")`. The default
  is `[]`.
- `page_by`: `_str(params, "page_by", max_len=256, required=False)`.
- `cursor`: `_cursor_param(params)`. If `cursor` is set and `page_by` is not, raise
  `_bad("cursor needs page_by")`.

Bound: `_bound_salesforce(p) = {"report_id": p["report_id"], "page_by": p["page_by"], "columns": p["columns"], "filters": p["filters"]}`
replaces `lambda p: {}`.

Targets: `f"{report_id}; filters={len(filters)}"`, plus `f"; columns={len(columns)}"` when
`columns` is non-empty, plus `f"; page_by={page_by}"` when `page_by` is set. Without the new
params this is exactly today's string.

`_run_salesforce`:

- Without `page_by`: today's call. Pass `columns=` only when `columns` is non-empty, so
  `client.run_report(report_id, filters=...)` is still the exact call when there are no columns.
  It returns `(result, None)`.
- With `page_by`:
  1. With no state, start from `n=0, a=0, l=None`. Otherwise call
     `_state_keys(state, {"n", "a", "l"})`, take `n` and `a` through `_state_count`, require
     `n >= 1`, and require `l` to be a non-empty `str`. Anything else is `_bad_cursor()`.
  2. Call `page = client.run_report_page(report_id, page_by, columns or None, filters or None, l, n)`.
     Catch `ReportPagingError` and raise `RpcError("invalid_params", str(exc), extra={"reason": exc.reason})`.
     Other client errors still become `upstream_error` in `_serve`.
  3. Take `rows = page.result["factMap"]["T!T"]["rows"]` (D1 guarantees this path). Build
     `shell = {**page.result, "factMap": {**fact_map, "T!T": {**fact_map["T!T"], "rows": []}}, "page": report_page_info(n + 1, a, len(rows), True)}`.
     Set `fitted = _fit_prefix(rows, SOURCE_PAGE_BUDGET_BYTES - _encoded_size(shell))`. A single
     row over budget raises `payload_too_large` through `_fit_prefix`.
  4. Set `more = fitted < len(rows) or not page.all_data`. `data` is the shell with
     `rows[:fitted]` and `"page": report_page_info(n + 1, a, fitted, more)`. The cursor is
     `cursors.encode("salesforce.report_run", _bound_salesforce(params), {"n": n + 1, "a": a + fitted, "l": page.keys[fitted - 1]})`
     if `more`, else `None`. Rows that did not fit are picked up by the next run, because it
     starts after the last key served. No offset is kept.
- `data` keeps Salesforce's `allData` as returned for that run. A plugin reads `page.more` or
  `next_cursor`.

### D5. SDK (`plugin-sdk`)

`SourceClient.report_pages(self, report_id: str, *, page_by: str, columns: list[str] | None = None, filters: list[dict] | None = None) -> AsyncIterator[SourceResult]`
has the docstring "Yield each page of a Salesforce report, read in order of the unique column
``page_by``, until the host gives no ``next_cursor``." It:

1. Builds `params = {"report_id": report_id, "page_by": page_by}`, adding `columns` and `filters`
   only when they are not `None`.
2. Runs `async for page in self.pages("salesforce.report_run", **params)`. For each page:
   - `index = page.data["reportMetadata"]["detailColumns"].index(page_by)` and
     `rows = page.data["factMap"]["T!T"]["rows"]`. A `KeyError`, `ValueError`, `TypeError` or
     `IndexError` while doing this raises `SourceError("internal_error", "malformed salesforce.report_run page")`.
   - For each row, `key = json.dumps(row["dataCells"][index].get("value"), sort_keys=True)`. A key
     already seen raises
     `SourceError("invalid_params", f"page_by {page_by!r} is not unique: a value repeats across pages", reason="not_unique")`.
   - Yields `page`.

`collect` stays limited to Jira and Calendar.

Test host: add the samples `testing/samples/salesforce.report_run.paged.json` and
`salesforce.report_run.paged.page2.json`. Page 1 has `params {"report_id": "00OEXAMPLE0000001", "page_by": "Account.PF_QA_Number__c"}`
and 3 rows with keys `PFQA-00001`…`PFQA-00003`. Its `data["page"]` is
`{"number": 1, "first_row": 1, "last_row": 3, "more": true}`, and its `next_cursor` is a real
daemon cursor: `cursors.encode("salesforce.report_run", {"report_id": ..., "page_by": ..., "columns": [], "filters": []}, {"n": 1, "a": 3, "l": "PFQA-00003"})`.
Page 2 carries that cursor in `params`, has 2 rows `PFQA-00004`…`PFQA-00005`,
`{"number": 2, "first_row": 4, "last_row": 5, "more": false}` and `next_cursor: null`. Both use
the `data` shape of the existing `salesforce.report_run.json` sample (tabular, `T!T`). Add
`_Samples.salesforce_report_pages(self) -> list[dict]`, which returns both fixtures as fresh
copies and has the docstring "The two pages of a paged ``salesforce.report_run``: load both, and
the first page's ``next_cursor`` fetches the second."

### D6. Synthetic report server for tests (`tests/fixtures/salesforce_analytics.py`)

This is a test-only fake of the two Analytics calls `run_report_page` makes, used by phases p3–p5.

```python
class FakeAnalytics:
    """sf.restful stand-in: serves describe and POSTed runs of one saved report from in-memory rows."""
    def __init__(self, saved_metadata: dict, columns: list[str], rows: list[dict], *, row_limit: int = 2000,
                 extended: dict | None = None): ...
    calls: list[tuple[str, dict | None]]   # (path, posted reportMetadata or None)
    def restful(self, path, params=None, method="GET", json=None) -> dict: ...

def make_rows(n: int, *, start: int = 1) -> list[dict]: ...      # {"Account.PF_QA_Number__c": "PFQA-00001", "ACCOUNT.NAME": "Account 1", "BILLING_CITY": <cycles over 3 cities>, "TYPE": <"Customer"/"Partner" alternating>}
def tabular_report(**overrides) -> dict: ...   # saved reportMetadata: TABULAR, detailColumns of the 4 columns, no filters
```

- `restful(f"analytics/reports/{id}/describe")` returns
  `{"reportMetadata": saved, "reportExtendedMetadata": extended or {}}`.
- `restful(f"analytics/reports/{id}", params={"includeDetails": "true"}, method="POST", json={"reportMetadata": m})`:
  1. Filters rows by `m["reportFilters"]`, combined with `m["reportBooleanFilter"]` (or ANDed
     when that is empty), using a small recursive-descent evaluator over integers, `AND`, `OR`,
     `NOT` and parentheses. No `eval`. It supports the operators `equals`, `notEqual`,
     `greaterThan`, `lessThan` and `contains`. A comma in `value` means "any of", as Salesforce
     does. Comparison is plain `str` comparison.
  2. Sorts by `m["sortBy"][0]["sortColumn"]` ascending when present.
  3. Cuts the rows to `row_limit`. `allData` is `len(matched) <= row_limit`.
  4. Returns `{"attributes": {...}, "allData": ..., "hasDetailRows": True, "reportMetadata": m, "reportExtendedMetadata": extended or {}, "groupingsDown": {"groupings": []}, "groupingsAcross": {"groupings": []}, "factMap": {"T!T": {"rows": [{"dataCells": [{"value": r[c], "label": r[c]} for c in m["detailColumns"]]} ...], "aggregates": [{"label": str(len(matched)), "value": len(matched)}]}}}`.
  5. Raises `AssertionError` for a non-TABULAR `m` with groupings. The client must flatten before
     posting.
- When `tests/fixtures/live/salesforce/run_report_page.json` exists (recorded in p2), a test checks
  that the fake's result has the same top-level keys and the same `factMap["T!T"]` row and cell key
  sets as the recording. It skips with a reason when the file is missing, the way
  `TestLiveFixtureParsing` does.

### D7. Live check and recorded shape (`scripts/qa_fixture_recorder.py`)

- QA manifest keys, with defaults (`tests/fixtures/qa_environment.yaml.example` documents them
  under `salesforce:`): `page_by_label: "PF QA Number"` and
  `summary_report_name: "PrivacyFence QA Summary Report"`.
- `_check_salesforce_report_paging(client, report_id, summary_report_id, page_by_label) -> CheckResult`
  produces row `("salesforce", "run_report_page", label, ok, note)`:
  1. `describe` the QA report through `client._call(lambda sf: sf.restful(f"analytics/reports/{report_id}/describe"))`.
     Find the detail column whose `reportExtendedMetadata.detailColumnInfo[col]["label"] == page_by_label`.
     If there is none: `ok=False`, note
     `"no column labelled <label> in the QA report -- see connector-qa.md, Seed: Salesforce"`.
  2. Call `first = client.run_report_page(report_id, col)`. Fail with
     `"fewer than 2 rows in the QA report"` when `len(first.keys) < 2`, and with
     `"sortBy not honoured -- keys are not in ascending order"` when
     `first.keys != sorted(first.keys)`. Auto-number values are zero-padded, so `str` order is
     correct here.
  3. Call `rest = client.run_report_page(report_id, col, after=first.keys[0], pages_done=1)`. Fail
     with `"greaterThan on page_by not honoured"` when `rest.keys != first.keys[1:]`.
  4. When `summary_report_id` is set, call `s = client.run_report_page(summary_report_id, col)`.
     Fail with `"flattened summary report returned no rows"` when `not s.keys`.
  5. On `ReportPagingError` or `SalesforceClientError`, return `ok=False` with `str(exc)`. A
     `ReportPagingError` message holds no values. The success note is
     `"sortBy, greaterThan and flattening honoured"`. No note ever holds a cell value.
- `check_salesforce` resolves `summary_report_name` through the same `list_reports` result as the
  main report, and adds the new row after `run_report`.
- Recording: in `--record` mode, capture the raw response of `first`'s run with
  `RawCaptureCall(client)`, the same way `list_reports` does. Write it as `run_report_page.json`
  after `deidentify_structural_fields(redact(...))`. Add `"run_report_page.json"` to
  `EXPECTED_FIXTURES["salesforce"]`.

### Rejected alternatives (for the ADR)

- **Offset paging, or Salesforce's async runs with more rows.** The Analytics API has no offset,
  and asynchronous runs are capped at 2,000 detail rows as well.
- **SOQL over the report's objects.** The business maintains the report, so re-deriving its
  filters, joins and columns in SOQL would drift from it. Out of scope per the issue.
- **Refusing non-tabular reports.** This was simpler, but the reports that need paging are often
  summary reports (milestones, line items). Flattening for the run shows no new data, because the
  grouping values were already the group labels, so it stays a narrowing.
- **A stateful server-side cursor that de-duplicates across pages.** It would mean daemon state
  per read with a lifetime and cleanup. The stateless keyset cursor plus refusing a repeated
  last key gives the same guarantee, and the SDK refuses a repeat across pages too.
- **Splitting a run into sub-pages with a row offset (plugin size limit).** Re-running the same
  page and skipping `k` rows breaks when rows change in between. Continuing after the last key
  served is plain keyset paging and needs no offset.
- **Comparing key order in Python** to detect a column that does not advance. Salesforce's
  collation differs from Python's for text, so this would reject valid reports. The plan detects
  "the previous last key came back" instead.

## ADRs

- **ADR 0132 — Salesforce reports page by a unique key column, never partially.** `page_by`
  keyset paging: one run per page, sorted by the column, `greaterThan` the last key, ANDed onto the
  saved and caller filters. Grouped reports are flattened for the run. The cursor is stateless and
  bound to the parameters. A read fails with no rows on a bad, non-unique or non-advancing column
  or past `salesforce.report_max_pages` (default 50). It amends ADR 0128's "Salesforce report runs
  do not page yet". The rejected alternatives are listed above.

## Manual steps

The manual-steps page is at https://claude.ai/artifact/ANJmZoK5KbeZeCEkXb8Vhi (source
`docs/salesforce-report-paging-plan-manual-steps.html`).

- **Before (`mb1-qa-org-key-column`):** in the QA Salesforce org, add an Auto Number field
  "PF QA Number" to Account, add it to "PrivacyFence QA Report", and create a summary report
  "PrivacyFence QA Summary Report" grouped by Billing City with that column. The live check (p2)
  needs both reports.
- **After (`ma1-real-org-paging`):** on your real org, page a report with more than 2,000 rows
  through `salesforce_run_report` with `page_by`, including one summary report. This checks the
  issue's three "verify first" items end to end: sortBy is honoured, greaterThan works on the key
  column, and the 2,000-row cap holds.

## Risks and open questions

- **Salesforce rejects a POSTed `reportFormat`/`groupings` change or `sortBy`.** The flatten and
  sort run on the QA org in p2's live check. If p2's `run_report_page` row fails with a Salesforce
  error naming `reportFormat`, `groupings` or `sortBy`, p2 stops with `status=blocked` and quotes
  the error (no cell values). Every later phase depends on this.
- **`topRows` key name.** D1 assumes the saved row limit appears as `reportMetadata.topRows`. If
  p2's describe of the QA report shows no `topRows` key at all, keep the check as is: it is a no-op
  then. Do not invent another key.
- **The recording in p2 needs a dispatch.** `qa-record-fixture.yml` (connector `salesforce`) runs
  on the self-hosted runner against the phase branch and commits the fixture back. If the dispatch
  is refused or the run fails, p2 stops with `status=blocked`. Do not hand-write
  `run_report_page.json`, because `EXPECTED_FIXTURES` makes CI depend on it.
- **ID columns.** Sorting and `greaterThan` on a record-ID column are not verified anywhere before
  `ma1`. The docs recommend an auto-number column and say ID columns are not guaranteed.
- **Aggregates on later pages** cover the rows after the previous key, not the whole report.
  Document it. Do not change Salesforce's result.
- **Base branch.** Phases are cut from this plan branch (PR #856's head). If #856 changes
  `plugins/cursors.py`, `source_ops.py` or the SDK's `pages()` before this lands, the feature
  branch merges `origin/feature/plugin-framework-cr` (or `main` after #856 merges) before the
  final review.

## Implementation manifest

```yaml
plan_slug: salesforce-report-paging
feature_branch: feature/salesforce-report-paging
tracking_issue: 854
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/ANJmZoK5KbeZeCEkXb8Vhi
manual_steps_source: docs/salesforce-report-paging-plan-manual-steps.html
manual_before:
  - id: mb1-qa-org-key-column
    title: Add the "PF QA Number" auto-number column and a summary report to the QA Salesforce org
    why: p2's live check pages the QA report by that column and flattens the summary report; without them its run_report_page row fails and p2 blocks
    done_when: '"PrivacyFence QA Report" shows a PF QA Number column with values like PFQA-00001, and "PrivacyFence QA Summary Report" (grouped by Billing City, with PF QA Number) runs in Salesforce'
manual_after:
  - id: ma1-real-org-paging
    title: Page a report with more than 2,000 rows (and one summary report) on your real org through salesforce_run_report
    why: proves on a production-sized org what the QA org cannot hold -- sortBy honoured past 2,000 rows, greaterThan on your key column, the 2,000-row cap, every row exactly once
verify_after_merge:
  - ruff check .
  - python3 -m pytest tests/unit/test_salesforce_client.py tests/unit/connectors/test_salesforce_connector.py tests/unit/plugins tests/unit/plugin_sdk tests/unit/test_qa_fixture_recorder.py tests/unit/test_daemon_main.py tests/unit/test_docs_configuration_reference.py -q
  - python3 -m pytest tests/integration/test_plugin_paging.py tests/integration/test_sdk_testhost_conformance.py -q
final_checks:
  - docs/salesforce-report-paging-plan.md and docs/salesforce-report-paging-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/0132-*.md exists, is Accepted, and is listed in docs/adr/README.md
  - CHANGELOG.md has [Unreleased] entries and no version heading
  - grep -rn "do not page yet\|Not paged" docs CHANGELOG.md returns nothing about Salesforce
  - the PR body says it is stacked on #856 (and #855) and links the connector-live-check.yml run
phases:
  - id: p1-client-keyset
    title: Client keyset paging -- build_keyset_metadata, report_page_keys, run_report_page
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/salesforce_client.py
      - tests/unit/test_salesforce_client.py
    brief: |
      Implement the plan's Design D1 in src/privacyfence/salesforce_client.py, exactly as specified
      (names, signatures, reasons, error messages).
      1. Add DEFAULT_REPORT_MAX_PAGES, REPORT_PAGING_REASONS, ReportPagingError (subclass of
         SalesforceClientError, with a .reason attribute; constructor raises ValueError for a reason
         not in REPORT_PAGING_REASONS) and the ReportPage dataclass next to ReportFilter.
      2. Add build_keyset_metadata, the private helper _key_text, report_page_keys and
         report_page_info as module functions directly below build_report_metadata. Reuse
         build_report_metadata for the filters; do not copy its logic.
      3. In SalesforceClient.__init__ set self.report_max_pages = DEFAULT_REPORT_MAX_PAGES. Add
         run_report_page after run_report, following D1 steps 1-6.
      4. Tests in tests/unit/test_salesforce_client.py, a new class TestBuildKeysetMetadata and a new
         class TestRunReportPage, modelled on TestBuildReportMetadata and TestRunReport (MagicMock sf
         whose restful returns describe then the run):
         - sortBy set; saved metadata not mutated; after adds a greaterThan filter ANDed onto saved
           reportBooleanFilter "(1 OR 2)" -> "((1 OR 2)) AND 3"-style string exactly as
           build_report_metadata produces; caller filters plus after filter both ANDed;
         - flattening a SUMMARY report: reportFormat TABULAR, groupings emptied, aggregates
           ["RowCount"], grouping columns prepended once; a TABULAR report keeps its aggregates;
         - page_by not a column -> bad_page_by with the column list; topRows -> bad_page_by;
         - report_page_keys: str/int/float keys; empty, comma, dict, bool values -> bad_page_by;
           duplicate -> not_unique; after in keys -> not_advancing; [] with allData False ->
           not_advancing; factMap key other than T!T -> not_flat;
         - every ReportPagingError message in these tests contains none of the row values used;
         - run_report_page: pages_done >= report_max_pages raises page_limit with no restful call;
           POST body carries the keyset metadata and includeDetails "true"; returns keys and
           all_data; report_page_info's four fields for count 0 and count > 0.
      5. Do not touch run_report or build_report_metadata behaviour; existing tests must pass
         unchanged.
      Stop condition: if build_report_metadata cannot express the after-filter (for example it
      rejects greaterThan on the column), stop with status=blocked and say what it rejected.
    acceptance:
      - python3 -m pytest tests/unit/test_salesforce_client.py -q passes
      - git diff --stat origin/plan/salesforce-report-paging -- tests/unit/test_salesforce_client.py shows only additions (no existing test edited)
      - grep -n "class ReportPagingError\|def run_report_page\|def build_keyset_metadata\|def report_page_keys\|def report_page_info\|DEFAULT_REPORT_MAX_PAGES = 50" src/privacyfence/salesforce_client.py finds all six
      - ruff check src/privacyfence/salesforce_client.py tests/unit/test_salesforce_client.py passes
      - python3 scripts/mypy_strict_modules.py passes

  - id: p2-live-check
    title: Live check for sortBy, greaterThan and flattening, and the recorded run_report_page.json
    depends_on: [p1-client-keyset]
    complexity: M
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/qa_environment.yaml.example
      - tests/fixtures/live/salesforce/**
      - docs/connector-qa.md
    brief: |
      Implement Design D7.
      1. scripts/qa_fixture_recorder.py: add _check_salesforce_report_paging (exact notes from D7),
         call it from check_salesforce after the run_report row, resolve summary_report_name via the
         same list_reports result, add the record-mode capture of the first run as
         run_report_page.json, and add "run_report_page.json" to EXPECTED_FIXTURES["salesforce"].
      2. tests/fixtures/qa_environment.yaml.example: under salesforce:, add page_by_label and
         summary_report_name with their defaults and a one-line comment pointing at connector-qa.md.
      3. docs/connector-qa.md "Seed: Salesforce": add the two checklist items of mb1 (Auto Number
         field "PF QA Number" on Account, display format PFQA-{00000}, added to the QA report;
         summary report "PrivacyFence QA Summary Report" on Accounts grouped by Billing City with
         Account Name and PF QA Number). In the Salesforce checks list add a "Report paging" bullet
         saying --check's run_report_page row covers sortBy, greaterThan and flattening live.
      4. tests/unit/test_qa_fixture_recorder.py: add test_report_paging_check_* tests next to the
         existing test_run_report_check_* (:987-1050), covering: success; missing label column;
         fewer than 2 rows; unsorted keys; greaterThan not honoured; summary report with no rows;
         ReportPagingError surfaced; and that no note contains a row value.
      5. Run the unit tests. Commit and push the phase branch.
      6. Dispatch qa-record-fixture.yml with input connector=salesforce against this phase branch
         (GitHub MCP actions_run_trigger, ref = the phase branch). Wait for it (it may queue behind
         connector-live-check). Pull: it commits tests/fixtures/live/salesforce/*.json back.
         Review the fixture diff per docs/connector-qa.md "Reviewing recorded fixtures": only
         [QATEST] synthetic accounts, no real identifiers, tenant URLs or tokens.
      7. Dispatch connector-live-check.yml against this phase branch and record the run URL and the
         salesforce rows (run_report, run_report_page) in your phase report for the final PR body.
      Stop conditions: the run_report_page row fails with a Salesforce error about reportFormat,
      groupings or sortBy, or with "greaterThan on page_by not honoured" -> status=blocked, quote the
      note. The QA org lacks the column or the summary report (mb1 not done) -> status=blocked. A
      dispatch is refused or the recording run fails -> status=blocked; never hand-write the fixture.
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes
      - tests/fixtures/live/salesforce/run_report_page.json exists, is valid JSON and has factMap with a T!T entry
      - the connector-live-check.yml run against the phase branch is green and its report has a passing salesforce run_report_page row (URL in the phase report)
      - ruff check scripts/qa_fixture_recorder.py tests/unit/test_qa_fixture_recorder.py passes

  - id: p3-analytics-fake
    title: Synthetic Analytics server and the 4,500-row paging tests at client level
    depends_on: [p1-client-keyset]
    complexity: M
    touches:
      - tests/fixtures/salesforce_analytics.py
      - tests/unit/test_salesforce_report_paging.py
    brief: |
      Implement Design D6 and client-level end-to-end tests.
      1. Create tests/fixtures/salesforce_analytics.py with FakeAnalytics, make_rows and
         tabular_report exactly as D6 describes. The boolean-logic evaluator is a recursive-descent
         parser (no eval); unknown tokens raise AssertionError.
      2. Create tests/unit/test_salesforce_report_paging.py. Build a SalesforceClient whose _get_sf
         returns an object with restful=FakeAnalytics(...).restful (monkeypatch _get_sf, as
         tests/unit/test_salesforce_client.py does for its sf mocks). Tests:
         - test_4500_rows_come_back_once_in_key_order_over_three_pages: loop run_report_page with
           after=last key and pages_done=n until all_data; 3 pages (2000, 2000, 500); keys equal
           make_rows(4500) keys in order, no duplicates.
         - test_custom_filter_logic_is_anded_with_the_key_filter: saved reportFilters on BILLING_CITY
           and TYPE with reportBooleanFilter "1 OR 2" plus a caller filter; the union of pages equals
           exactly the rows the fake's own evaluator selects for "(1 OR 2) AND caller", computed
           independently in the test from make_rows data.
         - test_summary_report_is_flattened_and_paged: saved SUMMARY with groupingsDown on
           BILLING_CITY; pages return T!T only, BILLING_CITY first column, all rows once.
         - refusals, each asserting no ReportPage is returned and the reason: unknown page_by
           (bad_page_by); non-unique column TYPE (not_unique); a fake variant that ignores
           greaterThan (not_advancing on page 2); page limit with report_max_pages = 2 on 4,500 rows
           (page_limit on the third call).
         - test_fake_matches_recorded_shape: compares with tests/fixtures/live/salesforce/
           run_report_page.json per D6; pytest.skip("run_report_page.json not recorded yet") when
           absent.
      Stop condition: if a test needs a change to salesforce_client.py to pass, stop with
      status=blocked and describe the mismatch with D1; do not edit p1's code here.
    acceptance:
      - python3 -m pytest tests/unit/test_salesforce_report_paging.py -q passes (the shape test may skip only if p2 has not merged yet)
      - grep -n "eval(" tests/fixtures/salesforce_analytics.py finds nothing
      - ruff check tests/fixtures/salesforce_analytics.py tests/unit/test_salesforce_report_paging.py passes

  - id: p4-mcp-tool
    title: salesforce_run_report page_by and cursor, card text, report_max_pages setting
    depends_on: [p3-analytics-fake]
    complexity: M
    touches:
      - src/privacyfence/connectors/salesforce.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/resources/settings.yaml.example
      - docs/configuration-reference.md
      - docs/tools-reference.md
      - tests/unit/connectors/test_salesforce_connector.py
      - tests/unit/test_daemon_main.py
    brief: |
      Implement Design D3 exactly (param names and descriptions, the new ToolSpec description,
      error texts, card strings with the en dash, result keys "page" and "next_cursor").
      1. connectors/salesforce.py: import cursors from privacyfence.plugins and the new client
         names; add the two ToolParams; replace the description; extend _run_report per D3 step 4
         with a single gated_call site; add _page_rows_text(info: dict) -> str for new_info["Rows"].
      2. daemon_main.py: add _salesforce_report_max_pages(config) -> int per D3 and set
         client.report_max_pages before SalesforceConnector(client). Pattern: the calendar
         free_busy_full_event_details wiring at daemon_main.py:1363.
      3. resources/settings.yaml.example: add a salesforce: section with report_max_pages: 50 and a
         two-line comment (each page is one report run; counts against the org's report-run limits).
      4. docs/configuration-reference.md: add the D3 row to "Connector behaviour".
      5. Regenerate docs/tools-reference.md with python3 scripts/generate_tools_reference.py (never
         hand-edit it).
      6. Tests in tests/unit/connectors/test_salesforce_connector.py, new class TestRunReportPaged
         next to TestRunReport (do not edit existing tests):
         - three pages over FakeAnalytics with 4,500 rows through connector.call with the cursor
           from each result: every row once, in key order; page dicts (1,1..2000,True),
           (2,2001..4000,True),(3,4001..4500,False); last next_cursor None;
         - card: new_info["Rows"] strings for more/last/no rows, preview["Paged by"], no "Cut off"
           text even though allData is False on pages 1 and 2;
         - cursor without page_by, page_by with summary_only, a cursor reused with other filters or
           columns, a garbled cursor: ValueError before any fetch or gate (assert the client was not
           called and gated_call was not reached, as test_invalid_filters_json_rejected_before_fetch
           does);
         - ReportPagingError from the client becomes RuntimeError with the same message and no card;
         - args passed to gated_call include page_by and cursor.
         tests/unit/test_daemon_main.py TestBuildConnectorsSalesforce: report_max_pages default 50,
         config value 7 applied, values 0, -1, "x" and True fall back to 50 with the warning.
      Stop condition: tests/unit/web/test_tool_schema_portability.py or the tool-definition tests
      reject the new description or params (length, wording rules) -> shorten the wording keeping
      every fact in D3; if that is impossible, status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_salesforce_connector.py tests/unit/test_daemon_main.py tests/unit/test_docs_configuration_reference.py tests/unit/web -q passes
      - git diff origin/plan/salesforce-report-paging -- tests/unit/connectors/test_salesforce_connector.py shows no removed lines
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - grep -n "page_by" docs/tools-reference.md finds the salesforce_run_report row
      - ruff check . passes

  - id: p5-source-op
    title: Plugin source operation salesforce.report_run pages by page_by
    depends_on: [p3-analytics-fake]
    complexity: M
    touches:
      - src/privacyfence/plugins/source_ops.py
      - tests/unit/plugins/test_source_ops.py
      - tests/integration/test_plugin_paging.py
      - tests/fixtures/plugins/echo/harness.py
      - tests/fixtures/plugins/echo/echo_plugin.py
      - docs/plugin-protocol.md
    brief: |
      Implement Design D4 exactly.
      1. source_ops.py: extend _validate_salesforce; add _bound_salesforce and use it in the
         SOURCE_ADAPTERS entry; extend the targets lambda; implement the paged branch of
         _run_salesforce with _fit_prefix and keyset continuation; map ReportPagingError to
         RpcError("invalid_params", str(exc), extra={"reason": exc.reason}). Import
         ReportPagingError and report_page_info from privacyfence.salesforce_client.
      2. tests/unit/plugins/test_source_ops.py, new classes next to TestAdapterSalesforce (do not
         edit existing tests): a client backed by FakeAnalytics (tests/fixtures/salesforce_analytics.py)
         with 4,500 rows; follow next_cursor with the existing _follow helper: every row once in key
         order, 3 calls, audit summaries end "; page_by=<col>; bytes=..." with "; more" on the first
         two. A budget test: monkeypatch SOURCE_PAGE_BUDGET_BYTES in source_ops so a run's rows do not
         fit; pages still deliver every row once and the cursor's next run starts after the last
         served key (assert FakeAnalytics.calls' greaterThan values). Refusals: cursor without
         page_by; cursor with changed columns/filters/page_by (the "different call" message); a
         garbled state (keys, n=0, l="" or l=5); each ReportPagingError reason comes back as
         invalid_params with extra reason; columns validation messages. Without page_by: the
         existing tests still pass, and passing columns calls run_report with columns=.
      3. Integration. tests/fixtures/plugins/echo/echo_plugin.py: add a page "/report" next to
         "/pages" that reads request.query["report_id"] and request.query["page_by"], iterates
         ctx.source.pages("salesforce.report_run", report_id=..., page_by=...), and for each page
         takes the page_by cell value of every row of data["factMap"]["T!T"]["rows"] (column index
         from data["reportMetadata"]["detailColumns"]); it returns
         Text(json.dumps({"pages": n, "items": len(keys), "keys": keys})). harness.py: add
         self.salesforce, a SalesforceClient({}) whose _get_sf is replaced by an object whose
         restful is a FakeAnalytics(...).restful set by the test (default: 0 rows), and add
         "salesforce": SimpleNamespace(_sf=self.salesforce) to connectors_provider.
         tests/integration/test_plugin_paging.py: add "salesforce.report_run" to the stack
         fixture's install_echo source_operations, and class TestSalesforce with
         test_every_row_of_a_4500_row_report_arrives_once (3 pages, keys == make_rows(4500) keys)
         and test_a_non_unique_column_fails_with_no_rows (the /report page answers an error, no
         keys).
      4. docs/plugin-protocol.md: update the salesforce.report_run row of the operations table
         (columns, page_by, cursor; data gains page), the Paging intro (drop "but
         salesforce.report_run"), the bound-params sentence (report_id, page_by, columns and filters
         for Salesforce), and the "What a page is" row: "One report run sorted by page_by, starting
         after the previous page's last value, cut to fit; grouped reports are read as one table.
         A column that is not unique, does not advance or is not a column of the run is
         invalid_params with reason bad_page_by, not_unique, not_advancing or not_flat; more than
         salesforce.report_max_pages runs is invalid_params with reason page_limit." Remove the
         issue-854 link.
      Stop condition: the Stack's connector_state lambda or the plugin host refuses
      salesforce.report_run for a reason not covered here (for example the manifest validator
      rejects it) -> status=blocked naming the check; do not loosen the validator.
    acceptance:
      - python3 -m pytest tests/unit/plugins -q passes
      - python3 -m pytest tests/integration/test_plugin_paging.py -q passes with TestSalesforce collected and not skipped
      - git diff origin/plan/salesforce-report-paging -- tests/unit/plugins/test_source_ops.py shows no removed lines
      - grep -n "Not paged" docs/plugin-protocol.md finds nothing
      - ruff check . and bandit -c pyproject.toml -r src pass

  - id: p6-sdk
    title: SDK report_pages iterator and test-host paged Salesforce samples
    depends_on: [p5-source-op]
    complexity: M
    touches:
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_source.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/samples/salesforce.report_run.paged.json
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/samples/salesforce.report_run.paged.page2.json
      - plugin-sdk/README.md
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/unit/plugin_sdk/test_testhost.py
      - tests/unit/plugins/test_sdk_samples.py
    brief: |
      Implement Design D5 exactly.
      1. plugin.py: add SourceClient.report_pages after pages(); import json if not imported.
      2. Generate the two sample files: write them with a short throwaway Python snippet that calls
         privacyfence.plugins.cursors.encode for the page-1 next_cursor and page-2 params.cursor
         (bound and state exactly as D5), so the cursor is the daemon's real one; do not commit the
         snippet. Copy the data shape from samples/salesforce.report_run.json, set detailColumns to
         include "Account.PF_QA_Number__c", and add the "page" entries.
      3. testing/_source.py: add _Samples.salesforce_report_pages(). Leave _PAGED_SAMPLES and get()
         unchanged.
      4. Tests: test_plugin.py -- report_pages yields both pages and passes cursor; a key repeated
         across pages raises SourceError reason "not_unique"; malformed data raises internal_error;
         columns/filters omitted when None. test_testhost.py -- loading salesforce_report_pages()
         and iterating ctx.source.report_pages gives 5 rows; a cursor for other params is refused
         (pattern: test_a_cursor_for_another_query_or_none_of_ours_is_refused).
         test_sdk_samples.py -- run the daemon adapter on each paged sample's params against a
         FakeAnalytics-backed client serving the same 5 rows with row_limit 3 and assert the daemon
         returns the sample's data["page"] and the same next_cursor (pattern: the parametrized
         cursor tests at :159-190).
      5. plugin-sdk/README.md: a report_pages example after the pages/collect examples, saying
         page_by must be unique per row (an auto-number column is best) and each page is one report
         run.
      Stop condition: the daemon's next_cursor for the sample differs from the one written into
      the sample because the bound or state differs from D5 -> fix the sample generation, never the
      daemon; if the daemon disagrees with D4 itself, status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk tests/unit/plugins/test_sdk_samples.py -q passes
      - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py -q passes
      - grep -n "async def report_pages" plugin-sdk/src/privacyfence_plugin_sdk/plugin.py finds it
      - ruff check . passes

  - id: p7-retire
    title: ADR 0132, reference docs, changelog, retire the plan
    depends_on: [p2-live-check, p4-mcp-tool, p6-sdk]
    complexity: S
    touches:
      - docs/adr/0132-salesforce-reports-page-by-a-unique-key-column.md
      - docs/adr/README.md
      - docs/adr/0128-plugin-source-reads-never-truncate.md
      - docs/plugins.md
      - docs/salesforce-setup.md
      - CHANGELOG.md
      - docs/salesforce-report-paging-plan.md
      - docs/salesforce-report-paging-plan-manual-steps.html
    brief: |
      1. Write docs/adr/0132-salesforce-reports-page-by-a-unique-key-column.md from the plan's ADRs
         bullet and "Rejected alternatives", in the template of docs/adr/README.md and the style of
         ADR 0128 (Status Accepted with today's date and the implementing files; Context; Decision;
         Alternatives considered; Consequences; Verification naming tests/unit/test_salesforce_report_paging.py,
         TestRunReportPaged, the source_ops tests, tests/integration/test_plugin_paging.py
         TestSalesforce, and the live check row run_report_page). Link issue #854, never this plan.
         Add it to the index in docs/adr/README.md.
      2. ADR 0128: change only its Status line to add "Salesforce report paging: [ADR 0132](0132-salesforce-reports-page-by-a-unique-key-column.md)."
         (accepted ADR bodies stay frozen; PR #856 did the same for 0121 and 0123).
      3. docs/plugins.md: replace the "Salesforce report runs do not page yet… issue 854" text
         (around :131) with one sentence on page_by and ctx.source.report_pages; add report_pages
         to the ctx.source list (around :297).
      4. docs/salesforce-setup.md: add a "Reading large reports" section before Troubleshooting:
         the 2,000-row limit; page_by needs a column unique per row, an Auto Number field added to
         the report is best, record-ID columns are not guaranteed to work; grouped reports are read
         as one table with the grouping columns first; each page is one report run and counts
         against the org's hourly report-run limits; salesforce.report_max_pages (default 50,
         100,000 rows); later pages' totals cover only the rows from that page on.
      5. CHANGELOG.md [Unreleased]: in the "Plugin reads no longer truncate" entry delete
         "Salesforce report runs do not page yet." and add a new Added entry: "**Salesforce
         reports past 2,000 rows.** salesforce_run_report and the plugin source operation
         salesforce.report_run take page_by, a column with a unique value per row, and read the
         whole report page by page with a cursor, each page one report run; a grouped report is
         read as one table. A column that is not unique or cannot be paged fails with no rows, and
         salesforce.report_max_pages (default 50) caps the runs one read may use. The plugin SDK
         adds ctx.source.report_pages."
      6. git rm docs/salesforce-report-paging-plan.md docs/salesforce-report-paging-plan-manual-steps.html;
         grep -rn "salesforce-report-paging-plan" . must find nothing outside .git.
    acceptance:
      - test -f docs/adr/0132-salesforce-reports-page-by-a-unique-key-column.md and grep -n "0132" docs/adr/README.md finds it
      - test ! -e docs/salesforce-report-paging-plan.md and test ! -e docs/salesforce-report-paging-plan-manual-steps.html
      - grep -rn "salesforce-report-paging-plan" --exclude-dir=.git . finds nothing
      - grep -n "do not page yet" CHANGELOG.md docs/plugins.md finds nothing
      - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit -k "adr or docs or changelog" -q passes
```
