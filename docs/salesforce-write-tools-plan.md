# Plan: Salesforce write tools (create and update, one record or a batch)

## Goal

Today the Salesforce connector can only read: an assistant can look up a record, search and run
reports, but cannot change anything in the org. This plan adds four write tools, each behind an
approval card:

- `salesforce_create_record`: create one record.
- `salesforce_update_record`: change fields of one record, by its id.
- `salesforce_create_records`: create up to 200 records of one object type in one approval.
- `salesforce_update_records`: change up to 200 records of one object type in one approval. Records
  are matched by `Id` or by an External ID field.

Every card is checked against the object's own field definitions (describe) before it is shown. An
update card shows each changed field's current and new value. A batch is written all or none.
Nothing deletes. The existing per-object-type "Always allow" rule (`approved_object_types`) can now
also allow create and update for an object type. A rule saved before this change is not widened
(ADR 0166).

These decisions were made with the maintainer while the plan was written:

- **Bulk is inline JSON only.** There is no CSV-file tool like Grist's `grist_import_csv`.
- **Rules are per object type**, and they cover all four write tools.
- **Batch updates match records by `Id` or by an External ID field.**
- **There is no delete.**

There is no tracking issue.

## Current state

- **Connector.** `src/privacyfence/connectors/salesforce.py` (865 lines) has five tools: list reports
  (`auto`), and get record, run report, export report and search (all `review`). Every `ToolSpec`
  has `read_only=True` (`salesforce.py:337-469`). Dispatch is in `call()` (`salesforce.py:472-483`).
  `_fetch` (`salesforce.py:840-844`) runs a client call in a thread and turns
  `SalesforceClientError` into `RuntimeError`. `_format_flat_fields` (`salesforce.py:36-43`)
  renders a field dict as `Field: value` lines.
- **Client.** `src/privacyfence/salesforce_client.py` (842 lines) wraps `simple-salesforce>=1.12.0`.
  - `_call(fn)` (`:642-654`) refreshes an expired session once and retries.
  - `_validate_object_type_name` (`:105-109`) and `_validate_salesforce_id` (`:112-118`) guard
    REST paths.
  - `get_record` (`:702-719`) returns `SalesforceRecord(object_type, id, fields)` and strips
    `attributes`.
  - There is no write, describe or SOQL-by-key method.
  - `simple_salesforce.Salesforce.restful(path, params=None, method='GET', **kwargs)` returns `None`
    on 204. HTTP errors raise `SalesforceMalformedRequest` (400), `SalesforceRefusedRequest` (403),
    `SalesforceResourceNotFound` (404), `SalesforceGeneralError` (other) and
    `SalesforceExpiredSession` (401). Each carries `.content`: Salesforce's parsed JSON error body,
    usually a list of `{"errorCode"|"statusCode", "message", "fields"}`.
- **OAuth scope.** `DEFAULT_SCOPES = "api refresh_token"` (`salesforce_client.py:32`). `api` already
  allows writes, so nobody has to authenticate again. The user's own Salesforce profile and
  permission sets decide what they can actually change.
- **Gate and policy tables** that every new tool must be added to, all checked by tests:
  - `auto_accept.TOOL_TO_OPERATION` (`auto_accept.py:133-136` for Salesforce)
  - `auto_accept.TOOL_TO_GATE` (`auto_accept.py:290-294`)
  - `policy/registry.TOOL_TO_VERB` (`registry.py:174-177`), plus the test-side
    `_VERB_MATRIX`, whose length is asserted as 93 at `tests/unit/policy/test_registry.py:287`
  - `write_effects.EFFECT_BY_TOOL` (`write_effects.py:42-170`; `tests/unit/test_write_effects.py`
    scans connector sources for `gated_call(... tool="X" ... gate="popup")`)
  - `gate._TOOL_LAYOUT` (`gate.py:263-264`)
  - `policy/propose.py:366-368`: the `approved_object_types` scope covers `(Verb.READ,
    Verb.SEARCH)` and excludes `salesforce.run_report`
  - `tests/unit/policy/test_propose.py:79-82`, the per-tool context list
- **Scope matcher.** `scopes._approved_object_types_matches` (`policy/scopes.py:311-318`) reads
  `ctx.args["object_type"]`. A write tool's `args` must carry `object_type` for a rule to match.
- **Generated docs and their drift tests:**
  - `docs/tools-reference.md` comes from `scripts/generate_tools_reference.py` and is checked by
    `tests/unit/test_docs_tools_reference.py`. Its Salesforce summary row is
    `| [Salesforce](#salesforce) | 5 | 1 | 4 | 0 |`.
  - `docs/always-allow-rules-reference.md` comes from `scripts/generate_always_allow_reference.py`
    and is checked by `tests/unit/test_generate_always_allow_reference.py`.
  - `website/connectors/index.html:122-131` has the Salesforce card
    (`data-tools="5" data-auto="1" data-review="4" data-popup="0"`), and line 29 says "144 tools".
    `website/how-it-works/index.html:54` says "144 connector tools". Both are checked by
    `tests/unit/test_website_connectors_page.py` against the tools reference.
  - `tests/unit/connectors/test_readme_manifest_alignment.py` checks the gate of each tools-reference
    row against `TOOL_TO_GATE` and against the source regex `tool="X",.*?gate="popup"`.
- **Read-only claims** that become false:
  - `website/connectors/salesforce/index.html` lines 6, 7, 20, 30, 36 and 50
  - `website/connectors/index.html` lines 128 and 130
  - `website/compare/claude-connectors/index.html:78` ("Salesforce (read-only)")
  - `README.md:75`
- **Precedent.** Grist's `grist_add_records` and `grist_update_records`
  (`connectors/grist.py:1018-1121`) are the pattern to copy:
  - inline JSON records;
  - columns checked before the card;
  - the update reads current values first and shows a `Record | Column | Current | New` table;
  - `gate="popup"`;
  - `filtered_data=None`.

  ADR 0145 is the "the connector only adds" precedent. ADR 0148 sets the rule that errors never
  carry provider cell text before approval.
- **QA.**
  - `scripts/qa_fixture_recorder.py` `check_salesforce` (`:1374-1460`) runs reads only.
  - `--lifecycle` (`LIFECYCLE_CHECKS`, `:2683`) has a create/read/update/read check for calendar,
    confluence, grist, jira, peek and tasks. `lifecycle_grist` (`:2462-2510`) never deletes and
    reports `cleanup_ok=None`.
  - `connector-live-check.yml` runs `--check`, `--record` and `--lifecycle` on the self-hosted
    runner, and is dispatchable.

## Design

### D1. Tools

All four tools use `gate="popup"`, `read_only=False` and `destructive=False`, have a required
`reason` param, and are `WIDE` in `gate._TOOL_LAYOUT`.

| Tool | Params (besides `reason`) | Returns |
|---|---|---|
| `salesforce_create_record` | `object_type`, `fields` | `{"object_type", "id"}` |
| `salesforce_update_record` | `object_type`, `record_id`, `fields` | `{"object_type", "id", "changed_fields": [names]}` |
| `salesforce_create_records` | `object_type`, `records` | `{"object_type", "created": n, "ids": [ids in input order]}` |
| `salesforce_update_records` | `object_type`, `records`, `key_field` (default `"Id"`) | `{"object_type", "updated": n, "unchanged": m, "ids": [ids of updated records]}` |

Tool descriptions. Use this exact text; each is under 1024 characters:

- `salesforce_create_record`: "Create one new Salesforce record. fields maps field API names to
  values, such as {\"Name\": \"Acme\", \"Industry\": \"Retail\"}; the names are checked against the
  object's own fields before you are asked. Returns {object_type, id}, the new record's
  18-character id. Use salesforce_create_records to create several records of one object type in
  one approval, and salesforce_get_record to see an existing record's field names. Requires user
  approval."
- `salesforce_update_record`: "Change fields of one existing Salesforce record, by its id. Only
  the fields you list change; a field set to null is cleared. The approval card shows each field's
  current and new value, and a call that would change nothing is refused. Returns {object_type, id,
  changed_fields}. Use salesforce_update_records to change several records in one approval, and
  salesforce_get_record to read the record first. Requires user approval."
- `salesforce_create_records`: "Create 1 to 200 new records of one Salesforce object type in one
  approval, all or none: if Salesforce refuses one record, none is created and the error names
  each refused record by its position. Returns {object_type, created, ids}, the new ids in input
  order. Use salesforce_create_record for a single record. Requires user approval."
- `salesforce_update_records`: "Change 1 to 200 existing records of one Salesforce object type in
  one approval, all or none. Each record is found by key_field: Id (default) or a field marked
  External ID. Only the fields listed change, records that already have those values are left
  alone, and a key that matches no record or several records refuses the whole call. Returns
  {object_type, updated, unchanged, ids}. Use salesforce_update_record for one record by id and
  salesforce_search to find ids. Requires user approval."

Param descriptions. Each is at least 20 characters:

- `object_type`: "API name of the Salesforce object, such as 'Account', 'Contact' or a custom
  object like 'Invoice__c'." Reuse the wording `salesforce_get_record` already has.
- `record_id`: "The record's 15- or 18-character Salesforce id, from salesforce_search or
  salesforce_get_record (its id field)."
- `fields`: "A JSON object mapping field API names to values: text, numbers, true/false, dates as
  YYYY-MM-DD and date-times as ISO 8601. Lookup fields take a record id."
  For `salesforce_update_record`, append: " null clears a field."
- `records` (create): "A JSON array of 1 to 200 objects, each mapping field API names to one new
  record's values, as fields on salesforce_create_record."
- `records` (update): "A JSON array of 1 to 200 objects, each holding key_field's value and the
  fields to change, such as [{\"Id\": \"001...\", \"Phone\": \"+1 555 0100\"}]. null clears a field."
- `key_field`: "Field that identifies each record: Id (the default), or the API name of a field
  marked External ID on this object, such as 'Customer_Number__c'. It is never changed itself."

### D2. Validation module `src/privacyfence/salesforce_writes.py` (new, pure, no I/O)

It imports `SalesforceField` from `salesforce_client` (D3) and nothing else from PrivacyFence.

Every error is `ValueError`. A message names the tool parameter (`fields`), the record position
(`records[3]`) and the field API name. It never contains a value read from Salesforce. It may
repeat a key value the caller passed in, because that is the caller's own input.

Constants:

```python
MAX_BATCH_RECORDS = 200
MAX_PAYLOAD_CHARS = 1_000_000
MAX_KEY_CHARS = 255
MAX_ERRORS_SHOWN = 10
MAX_CELL_PREVIEW_CHARS = 200
# describe type -> kind of JSON value accepted
WRITABLE_TYPES: dict[str, str] = {
    "string": "text", "textarea": "text", "picklist": "text", "multipicklist": "text",
    "combobox": "text", "email": "text", "phone": "text", "url": "text", "reference": "text",
    "encryptedstring": "text", "time": "text",
    "boolean": "bool", "int": "int", "double": "number", "currency": "number",
    "percent": "number", "date": "date", "datetime": "datetime",
}
KEY_TYPES = frozenset({"id", "string", "email"})
```

Functions, with these exact signatures:

- `parse_fields_object(raw: str, param: str) -> dict[str, Any]`
  - Requires `len(raw) <= MAX_PAYLOAD_CHARS`, otherwise:
    `f"{param}: larger than {MAX_PAYLOAD_CHARS:,} characters"`.
  - `raw` must be JSON. Errors:
    - not valid JSON: `f"{param}: invalid JSON: {exc.msg}"`
    - not a JSON object: `f"{param}: must be a JSON object of field names to values"`
    - empty object: `f"{param}: names no field"`
    - a value that is not str, int, float, bool or None:
      `f"{param}: {name} must be text, a number, true/false or null"`
- `parse_records(raw: str) -> list[dict[str, Any]]`
  - Applies the same size check as above, with param `records`.
  - Errors:
    - not a list: `"records: must be a JSON array of objects"`
    - wrong length: `f"records: must hold 1 to {MAX_BATCH_RECORDS} records, got {n}"`
  - Each element is checked as by `parse_fields_object` with param `f"records[{i}]"`. Factor out a
    shared `_check_object(obj, param)` helper for this.
- `field_index(fields: list[SalesforceField]) -> dict[str, SalesforceField]`, keyed by
  `name.lower()`.
- `resolve_fields(values: dict[str, Any], index: dict[str, SalesforceField], object_type: str, mode: str, where: str) -> dict[str, Any]`
  - `mode` is `"create"` or `"update"`.
  - Returns a dict keyed by each field's canonical `SalesforceField.name`, in input order.
  - Errors, in this order per field:
    1. not in `index`: `f"{where}: {object_type} has no field {name!r}"`
    2. the same field named twice in different case:
       `f"{where}: field {field.name} is named twice"`
    3. not writable in this mode (`createable` for create, `updateable` for update):
       `f"{where}: {object_type}.{field.name} cannot be set when {'creating' if create else 'updating'} a record"`
    4. type not in `WRITABLE_TYPES`:
       `f"{where}: {object_type}.{field.name} has type {field.type}, which PrivacyFence does not write"`
    5. `None` on create: `f"{where}: {field.name} is null; leave it out instead"`
    6. `None` on update when the field is not `nillable`: `f"{where}: {field.name} cannot be cleared"`
    7. the value does not match its kind:
       `f"{where}: {field.name} must be {expected}"`. `expected` is one of:
       - `"text"`: a str
       - `"true or false"`: a bool
       - `"a whole number"`: an int that is not a bool
       - `"a number"`: an int or float that is not a bool
       - `"a date as YYYY-MM-DD"`: a str matching `^\d{4}-\d{2}-\d{2}$`
       - `"a date-time in ISO 8601"`: a str matching
         `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?(Z|[+-]\d{2}:?\d{2})$`
- `resolve_key_field(key_field: str, index: dict[str, SalesforceField], object_type: str) -> SalesforceField`
  - Looks the name up case-insensitively.
  - Unknown name: `f"key_field: {object_type} has no field {key_field!r}"`.
  - Allowed when its type is `"id"`. Otherwise it must have `external_id` true and a type in
    `KEY_TYPES`, else:
    `f"key_field: {object_type}.{field.name} is not an External ID field; use Id or a field marked External ID"`.
- `key_text(value: Any, where: str, key_name: str) -> str`
  - The value must be a non-empty str, `<= MAX_KEY_CHARS`, with no character below `" "`. Else:
    `f"{where}: {key_name} must be non-empty text without control characters, at most {MAX_KEY_CHARS} characters"`.
- `key_norm(value: str, is_id: bool) -> str`
  - Returns `value[:15]` when `is_id`; otherwise `value.casefold()`.
  - Why, as a one-line comment: a 15- and an 18-character id name the same record, and Salesforce
    compares External ID text without case.
- `values_differ(new: Any, current: Any, field_type: str) -> bool`
  - `None` and `""` are both "empty". Two empty values are equal; empty against non-empty differs.
  - `boolean`: compare with `bool()`.
  - `int`, `double`, `currency`, `percent`: compare as `float()`.
  - `datetime`: parse both with `datetime.fromisoformat` after replacing a trailing `Z` with
    `+00:00` and a trailing `±HHMM` with `±HH:MM`. Compare the instants. If either fails to parse,
    compare the strings.
  - `multipicklist`: compare `set(s.split(";"))`.
  - Anything else: compare `str()`.
- `cell_text(value: Any, limit: int | None = MAX_CELL_PREVIEW_CHARS) -> str`
  - `None` or `""` becomes `"(empty)"`, and bool becomes `"true"` or `"false"`. Anything else is
    `str()`.
  - Cut to `limit` characters with a trailing `"…"` when it is longer. `limit=None` means no cut.
- `payload_sha256(payload: Any) -> str`
  - `hashlib.sha256` of
    `json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))` as UTF-8.

### D3. Client additions in `salesforce_client.py`

```python
@dataclass
class SalesforceField:
    name: str
    label: str
    type: str
    createable: bool
    updateable: bool
    nillable: bool
    external_id: bool

@dataclass
class WriteFailure:
    index: int            # position in the request (0 for a single-record write)
    status_code: str      # Salesforce's errorCode/statusCode, or "UNKNOWN"
    fields: list[str]
    message: str          # Salesforce's message, cut to 200 characters

class SalesforceWriteError(SalesforceClientError):
    """Salesforce refused a write; nothing was saved (every batch is sent with allOrNone)."""
    def __init__(self, failures: list[WriteFailure], total: int) -> None: ...
```

`str(SalesforceWriteError)` takes one of two forms:

- When `total == 1`:
  `"Salesforce did not save the record: " + "; ".join(parts)`.
- Otherwise:
  `f"Salesforce saved none of the {total} records: " + "; ".join(parts)`.

Each part is `f"records[{f.index}]: {f.status_code}"`, plus `f" ({', '.join(f.fields)})"` when
`fields` is non-empty, plus `f": {f.message}"` when `message` is non-empty. Only the first
`10` failures are listed, then `f"; and {n} more"`. Failures with status code
`ALL_OR_NONE_OPERATION_ROLLED_BACK` are left out of the list, because they only say that another
record failed.

New module-level helpers:

- `_SOQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")`
- `_soql_quote(value: str) -> str`: raises `SalesforceClientError("key value contains a control character")`
  for any character below `" "`, and returns
  `"'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"`.
- `_write_failures(content: Any) -> list[WriteFailure]`: reads a list of dicts with `errorCode` or
  `statusCode`, `message` and `fields`, all at index 0. Any other shape gives
  `[WriteFailure(0, "UNKNOWN", [], "")]`.
- `_composite_failures(results: list[dict]) -> list[WriteFailure]`: for the composite response
  `[{"id", "success", "errors": [...]}]`, one failure per error of each result that is not
  `success`, at that result's index.

New `SalesforceClient` methods. Each runs through `self._call(_run)`, so an expired session is
refreshed and retried once. That is safe, because Salesforce refused the request before running
it. Nothing else is retried (ADR 0105).

Inside each write's `_run`, catch `simple_salesforce.exceptions.SalesforceMalformedRequest`,
`SalesforceRefusedRequest`, `SalesforceResourceNotFound` and `SalesforceGeneralError`, and raise
`SalesforceWriteError(_write_failures(exc.content), total)`. Import those exception classes inside
`_run`, as `_build_sf` imports `Salesforce`. Do not catch `SalesforceExpiredSession`.

- `describe_fields(object_type: str) -> list[SalesforceField]`
  - Validates `object_type`, then calls `getattr(sf, object_type).describe()` and maps each entry
    of `["fields"]`: `name`, `label`, `type`, `createable`, `updateable`, `nillable`, and
    `externalId` → `external_id`.
  - Missing keys default to `""` or `False`.
- `create_record(object_type: str, fields: dict[str, Any]) -> str`
  - Calls `sf.restful(f"sobjects/{object_type}/", method="POST", json=fields)` and returns
    `result["id"]`.
  - When the result is not a dict with `success` true, raise `SalesforceWriteError` from
    `_write_failures(result.get("errors"))` with `total=1`.
- `update_record(object_type: str, record_id: str, fields: dict[str, Any]) -> None`
  - Validates both arguments, then calls
    `sf.restful(f"sobjects/{object_type}/{record_id}", method="PATCH", json=fields)`.
- `create_records(object_type: str, records: list[dict[str, Any]]) -> list[str]`
  - Requires `1 <= len(records) <= 200`, else `SalesforceClientError`.
  - Calls `sf.restful("composite/sobjects", method="POST", json={"allOrNone": True, "records":
    [{"attributes": {"type": object_type}, **r} for r in records]})`.
  - Any result that is not `success` raises `SalesforceWriteError(_composite_failures(results),
    len(records))`. Otherwise returns the ids in order.
- `update_records(object_type: str, records: list[dict[str, Any]]) -> None`
  - Each record must carry a valid `"Id"`. Otherwise this works like `create_records`, but with
    `method="PATCH"`.
- `find_records(object_type: str, key_field: str, keys: list[str], fields: list[str]) -> list[dict[str, Any]]`
  - Validates `object_type`, `key_field` and every name in `fields` with
    `_SOQL_IDENTIFIER_RE`, raising `SalesforceClientError` on a mismatch.
  - Requires `1 <= len(keys) <= 200`.
  - When `key_field == "Id"`, every key must match `_SALESFORCE_ID_RE` and is quoted as
    `'<id>'`. Every other key goes through `_soql_quote`.
  - Builds `SELECT <Id, key_field, *fields, de-duplicated case-insensitively in that order> FROM
    <object_type> WHERE <key_field> IN (<quoted, comma-joined>)`.
  - Runs `sf.query_all(soql)` and returns `records` with `attributes` removed.
  - The f-string query gets `# nosec B608  # identifiers match _SOQL_IDENTIFIER_RE and values are quoted by _soql_quote`.
- Every method logs object type and counts only, never a value, for example
  `logger.info("create_records %s: %d record(s)", object_type, len(records))`.

### D4. Connector behaviour

Common to all four tools:

1. Validate `object_type` with `salesforce_client._validate_object_type_name`. A
   `SalesforceClientError` is re-raised as `ValueError(str(exc))`. Do this through a new connector
   helper `_check_object_type(object_type: str) -> str`.
2. Parse the JSON with D2. All of this happens before any client call, so a malformed call costs
   no approval and no API call.
3. Run `index = field_index(await self._fetch(self._sf.describe_fields, object_type))` and resolve
   the fields with D2.
4. Call `gated_call(connector=self.name, tool="<tool>", ..., gate="popup", ...)`. Keep the keyword
   order `tool=` before `gate=` on purpose: the alignment and write-effects tests find the tool by
   the regex `tool="X",.*?gate="popup"`. Pass `filtered_data=None`, `my_email=self.my_email` and
   `table_only=True`.
5. Pass `raw_data` holding names and counts only, never values:
   `{"object_type", "fields": sorted names}` for a single write, and
   `{"object_type", "records": n, "fields": sorted union of names}` for a batch.
6. Pass `args` with `"object_type"`, which the `approved_object_types` matcher reads, and
   `"sha256": payload_sha256(<the exact write payload>)`, which binds the approval to the payload
   (ADR 0073). Also pass `"record_id"` on update_record and `"key_field"` on update_records.
7. After approval, run the client write through `self._fetch`. A `SalesforceWriteError` arrives as
   `RuntimeError` with D3's message.

Per tool:

- **`salesforce_create_record`**
  - `tool_name="Create Salesforce Record"` and `summary=f"Create {object_type} record"`.
  - `preview={"Object type": object_type, "Fields": str(len(resolved))}`.
  - `preview_tables=[{"headers": ["Field", "Label", "Value"], "rows": [[name, index[name.lower()].label, cell_text(v)] ...]}]`.
  - `details_text`: one `f"{name}: {cell_text(v, None)}"` line per field, in input order.
  - Payload hashed: `{"object_type": object_type, "fields": resolved}`.
- **`salesforce_update_record`**
  1. Validate `record_id` with `_validate_salesforce_id`; a `SalesforceClientError` becomes
     `ValueError`.
  2. Resolve the fields with mode `"update"`.
  3. Run `current = await self._fetch(self._sf.get_record, object_type, record_id)`.
  4. `changes = [(name, current.fields.get(name), new) for name, new in resolved.items() if values_differ(new, current.fields.get(name), index[name.lower()].type)]`.
  5. If `changes` is empty, raise
     `ValueError("salesforce_update_record: every field already has the value given; nothing to change")`.
  6. Gate the call:
     - `tool_name="Update Salesforce Record"`
     - `summary=f"Update {object_type}: {name}"`, where `name` is
       `current.fields.get("Name") or record_id`
     - `preview={"Object type", "Record ID", "Name": str(name), "Fields changed": str(len(changes))}`
     - `preview_tables=[{"headers": ["Field", "Current", "New"], "rows": [[name, cell_text(old), cell_text(new)] ...]}]`
     - `details_text`: one line per change,
       `f"{name}: {cell_text(old, None)} → {cell_text(new, None)}"`
  7. Write only the changed fields.
  8. Payload hashed: `{"object_type", "record_id", "fields": {changed}}`.
- **`salesforce_create_records`**
  - Run `resolve_fields` for each record with `where=f"records[{i}]"`.
  - `tool_name="Create Salesforce Records"` and
    `summary=f"Create {n} {object_type} record(s)"`.
  - `preview={"Object type", "Records": str(n)}`.
  - The table's headers are `["#"]` plus the union of field names in first-seen order. Each row is
    `[str(i + 1)]` plus `cell_text` of each value, with `""` where a record lacks that field.
  - `details_text`: a `f"Record {i + 1}"` line per record, followed by that record's
    `f"  {name}: {cell_text(v, None)}"` lines.
  - Payload hashed: `{"object_type", "records": [resolved...]}`.
- **`salesforce_update_records`**
  1. `key = resolve_key_field(key_field, index, object_type)` and
     `is_id = key.type == "id"`.
  2. Find each record's key entry case-insensitively.
     - Missing: `ValueError(f"records[{i}]: has no {key.name} value")`.
     - Get the text with `key_text(...)`.
     - When `is_id`, check it with `_validate_salesforce_id`; a failure becomes
       `ValueError(f"records[{i}]: {key.name} must be a 15- or 18-character Salesforce id")`.
     - Resolve the remaining fields with mode `"update"`. A record with no field besides the key
       raises `ValueError(f"records[{i}]: names no field to change")`.
     - A `key_norm` seen before raises
       `ValueError(f"records[{i}]: {key.name} {key_value!r} is listed twice")`.
  3. Run `rows = await self._fetch(self._sf.find_records, object_type, key.name, keys, sorted(union of field names))`.
     Group the rows by `key_norm(str(row[key.name]), is_id)`.
     - A key with two or more rows raises
       `ValueError(f"{key.name} {key_value!r} matches {k} {object_type} records; it must match one")`.
     - Unmatched keys raise
       `ValueError(f"No {object_type} record has {key.name} " + ", ".join(repr(k) for k in first 10) + (f" and {n - 10} more" if n > 10 else ""))`.
  4. For each record, keep the changed fields, compared with `values_differ` as on update_record.
     Records with none count as `unchanged`.
     - If every record is unchanged, raise
       `ValueError("salesforce_update_records: every record already has the values given; nothing to change")`.
  5. Gate the call:
     - `tool_name="Update Salesforce Records"`
     - `summary=f"Update {u} {object_type} record(s)"`
     - `preview={"Object type", "Matched by": key.name, "Records to update": str(u), "Unchanged records": str(m), "Fields changed": str(total changed cells)}`
     - `preview_tables=[{"headers": ["Record", "Field", "Current", "New"], "rows": ...}]`, with the
       key value as "Record"
     - `details_text`: one line per change,
       `f"{key_value} {name}: {old} → {new}"`, using `cell_text(..., None)`
  6. Write `[{"Id": row["Id"], **changed}]` in input order with `update_records`.
  7. Return the ids of the written records.
  8. Payload hashed: `{"object_type", "key_field": key.name, "records": <the write list>}`.

### D5. Gate and policy registration

- **`auto_accept.TOOL_TO_OPERATION`:**
  - `"salesforce_create_record": "salesforce.create_record"`
  - `"salesforce_create_records": "salesforce.create_records"`
  - `"salesforce_update_record": "salesforce.update_record"`
  - `"salesforce_update_records": "salesforce.update_records"`

  Each tool gets its own key, so that a rule can later leave the batch tools out with one `excludes`
  entry, without changing any key.
- **`auto_accept.TOOL_TO_GATE`:** all four are `"popup"`.
- **`registry.TOOL_TO_VERB`:** the creates are `Verb.ADD` and the updates are `Verb.UPDATE`.
- **`write_effects.EFFECT_BY_TOOL`** gets these exact sentences:
  - create_record: "A new record of that object type is created in Salesforce with the fields
    shown. PrivacyFence changes no other record; your org's own automation, such as flows and
    triggers, may still react."
  - create_records: "New records of that object type are created in Salesforce with the fields
    shown, all or none. PrivacyFence changes no other record; your org's own automation may still
    react."
  - update_record: "The fields shown change to their new values on that record. PrivacyFence
    changes no other field or record; your org's own automation may still react."
  - update_records: "The fields shown change to their new values on the records shown, all or
    none. PrivacyFence changes no other field or record; your org's own automation may still
    react."
- **`gate._TOOL_LAYOUT`:** all four are `WIDE`.
- **`policy/propose.py`:** the `approved_object_types` entry's verbs become
  `(Verb.READ, Verb.SEARCH, Verb.ADD, Verb.UPDATE)`. `excludes` stays
  `("salesforce.run_report",)`.

  `_salesforce_object_types` already reads `object_type` from args, so no change is needed there.

  Update the comment above the Salesforce block to say that the object-type scope also covers
  creating and updating records of that type, including the batch tools.
- **`docs/approvals-and-policy.md`:** the Salesforce object type row's condition column becomes
  "every requested or written object type is one of these".

### D6. QA lifecycle

Add `lifecycle_salesforce(manifest)` to `scripts/qa_fixture_recorder.py` and register it in
`LIFECYCLE_CHECKS` under `"salesforce"`.

- Object type: `cfg.get("lifecycle_object_type", "Task")`.
- Name field: `cfg.get("lifecycle_name_field", "Subject")`.
- Note field: `cfg.get("lifecycle_note_field", "Description")`.

Steps, all on the client:

1. `create_record` with name `f"{LIFECYCLE_TAG} salesforce {suffix}"` and note
   `"created by qa_fixture_recorder.py --lifecycle"`, then `get_record` and check the name.
2. `update_record` with note `"updated"`, then `get_record` and check it.
3. `create_records` with two records named `... {suffix} a` and `... b`, then `find_records`
   (key `Id`) and check 2 rows with the right names.
4. `update_records` setting both notes to `"batch updated"`, then `find_records` and check.
5. Run `describe_fields` and check that the name field is `createable`.

It never deletes (`cleanup_ok=None`), as `lifecycle_grist` does. The created records collect under
the `LIFECYCLE_TAG` subject, and someone deletes them by hand now and then.

Add the three keys, commented, to `tests/fixtures/qa_environment.yaml.example`'s `salesforce:`
block. The code defaults apply, so the runner's real `qa_environment.yaml` needs no change.

### D7. Things deliberately not built

These go into the ADRs as rejected alternatives:

- **CSV-file bulk tools (`salesforce_import_csv`).** The maintainer chose inline JSON only. A batch
  of at most 200 records keeps a card readable and avoids chunked, partly-failed writes.
- **Bulk API 2.0.** It is asynchronous ingest jobs without a per-record result before commit. A
  card could not say the job is all-or-none, and polling adds a long-running call.
- **Native upsert (`PATCH /sobjects/{type}/{extIdField}/{value}`) and a `create_missing` option on
  `salesforce_update_records`.** Upsert cannot tell beforehand which records it would create. A
  rule allowing only *update* on an object type would then auto-accept creates. Unmatched keys are
  refused instead, and the caller uses `salesforce_create_records` for them.
- **Delete.** A delete can only be previewed as ids. A mistaken create or update can be fixed in
  Salesforce, and Salesforce keeps field history where the org turns it on.
- **Atomic multi-object batches.** One object type per call keeps the describe check and the rule
  scope to one object type.

## ADRs

The retirement phase writes these three ADRs. The numbers are provisional: use the next three free
numbers in `docs/adr/` when that phase runs, in this order (see Risks).

- **ADR 0175: Salesforce writes create and update one record or a batch of up to 200, all or
  none, and never delete.**
  - Covers D1 and D7.
  - Rejected: CSV-file tools, Bulk API 2.0, native upsert and `create_missing`, delete, and
    multi-object batches.
- **ADR 0176: A Salesforce write is checked against the object's describe and its current values
  before the card.**
  - Covers D2 and D4: field, type and nullability checks before any approval.
  - Updates read the current values and show only the fields that change; a call that changes
    nothing is refused.
  - Batch updates match by `Id` or an External ID field, and a key that matches no record or
    several records refuses the call.
  - Pre-approval messages never carry a value read from Salesforce.
  - After approval, Salesforce's own error messages are returned, cut to 200 characters, because
    the caller needs them to fix a validation-rule failure.
- **ADR 0177: The Salesforce object-type rule also covers creating and updating records of that
  type.**
  - Covers D5: `approved_object_types` gains `add` and `update`, offered from the card.
  - Each tool has its own operation key, so the batch tools can be left out later.
  - Existing rules are not widened (ADR 0166).

## Manual steps

There is no `manual_before`. The live checks run on the self-hosted runner through
`connector-live-check.yml`, and the lifecycle check's settings have code defaults.

There is one `manual_after`, because CI cannot see a real approval card in a real Salesforce org.
Try one single-record update and one batch create from an AI client against your Salesforce
sandbox, and confirm what the cards show. The steps are in the
[manual steps page](https://claude.ai/artifact/KrHGUXsa4AEMY6Du8VPzag).

## Risks and open questions

- **ADR numbers.** `feature/jira-assets-connector` adds ADRs numbered 0171-0173, which collide with
  `main`. When it is renumbered, it will likely take 0175-0177. The retirement phase takes the next
  free numbers on the feature branch when it runs. Whichever PR merges second renumbers its ADRs
  and their index rows.
- **Tool totals.** The 144 on the website is `main`'s current total. Phases p3 and p4 each **add
  2** to whatever total the files hold, and do not set an absolute number. If
  `tests/unit/test_website_connectors_page.py` still fails after that, stop with `status=blocked`:
  another connector's count moved under you.
- **`simple-salesforce` error shapes.** D3 assumes `.content` is the parsed JSON list. If the
  installed version gives bytes or text for a 400 in the client tests' mocks of `restful`,
  `_write_failures` must still return `[WriteFailure(0, "UNKNOWN", [], "")]` and never raise.
  Assert this in a test.
- **The composite API needs API version 42.0 or later.** `simple-salesforce`'s default version is
  far newer. If a lifecycle check reports `NOT_FOUND` on `composite/sobjects`, stop and report it,
  rather than lowering anything.
- **QA user permissions.** The QA Salesforce user must be able to create and edit Tasks. Standard
  profiles can. If `lifecycle_salesforce` fails with `INSUFFICIENT_ACCESS`, the p5 worker reports
  it as a `manual_after` finding for the maintainer and does not change the code.
- **Datetime comparison.** Salesforce returns `2026-10-14T10:00:00.000+0000`. If `values_differ`
  ever reports a change for the same instant, the card would show a no-op change. The D2 tests
  pin this case.

## Implementation manifest

```yaml
plan_slug: salesforce-write-tools
feature_branch: feature/salesforce-write-tools
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/KrHGUXsa4AEMY6Du8VPzag
manual_steps_source: docs/salesforce-write-tools-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-real-cards
    title: Check a single-record update card and a batch create card against a real Salesforce sandbox
    why: CI drives the gate with a spy; only a person can confirm the real cards show Current/New values, the field labels and the "Always allow" offer for the object type in a real org
verify_after_merge:
  - python3 -m pytest tests/unit/test_salesforce_client.py tests/unit/test_salesforce_writes.py tests/unit/connectors/test_salesforce_connector.py tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_auto_accept.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_systemic_gate_invariants.py -q
final_checks:
  - docs/salesforce-write-tools-plan.md and docs/salesforce-write-tools-plan-manual-steps.html are deleted and nothing links to them (grep -rn "salesforce-write-tools-plan" . --include=*.md --include=*.html --include=*.py finds nothing)
  - The three ADRs from the plan's ADRs section exist in docs/adr/, have Status Accepted, and are rows in docs/adr/README.md
  - CHANGELOG.md has the Salesforce write-tools entry under "## [Unreleased]" and no new version heading
  - grep -rn -i "read-only" website/connectors/salesforce/index.html README.md website/compare/claude-connectors/index.html | grep -i salesforce finds nothing
phases:
  - id: p1-client-writes
    title: Salesforce client - describe, single and composite writes, find by key
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/salesforce_client.py
      - tests/unit/test_salesforce_client.py
    brief: |
      Read docs/salesforce-write-tools-plan.md, "Design" D3, before starting; it is the spec.
      Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/coding-and-testing-guidelines.md (§1.4, §1.8, §2.7).
      1. In src/privacyfence/salesforce_client.py, add the dataclasses SalesforceField and
         WriteFailure and the exception SalesforceWriteError(SalesforceClientError) exactly as in
         D3, next to the existing dataclasses (after ReportPage). Implement __init__ to store
         `failures` and `total` and build the message D3 specifies.
      2. Add the module helpers _SOQL_IDENTIFIER_RE, _soql_quote, _write_failures and
         _composite_failures under a new "# ---- #" banner "Record writes" placed before
         build_authorize_url.
      3. Add SalesforceClient methods describe_fields, create_record, update_record,
         create_records, update_records and find_records, after search(). Each uses self._call.
         Writes catch only the four simple_salesforce exceptions D3 names (imported inside _run)
         and raise SalesforceWriteError; SalesforceExpiredSession must stay uncaught so _call
         refreshes. Log object type and counts only.
      4. Tests in tests/unit/test_salesforce_client.py, using the existing with_fake_sf(client, sf)
         helper and a MagicMock sf. Add classes:
         - TestDescribeFields: maps a describe payload (name, label, type, createable, updateable,
           nillable, externalId) to SalesforceField; a missing key defaults to "" / False;
           rejects "Account/../x" as an object type.
         - TestCreateRecord: POSTs to "sobjects/Account/" with json=fields and returns the id;
           a SalesforceMalformedRequest whose .content is
           [{"errorCode": "REQUIRED_FIELD_MISSING", "message": "Required fields are missing: [Name]", "fields": ["Name"]}]
           raises SalesforceWriteError whose str() is exactly
           "Salesforce did not save the record: records[0]: REQUIRED_FIELD_MISSING (Name): Required fields are missing: [Name]";
           .content of b"not json" gives one UNKNOWN failure and does not raise anything else;
           an expired session (SalesforceExpiredSession, as the existing refresh tests raise it)
           is refreshed and retried once.
         - TestUpdateRecord: PATCHes "sobjects/Account/001000000000001AAA" with only the given
           fields; an invalid record id raises SalesforceClientError before any sf call.
         - TestCompositeWrites: create_records sends allOrNone True and attributes.type on every
           record and returns ids in order; a result list where records[1] fails with
           DUPLICATE_VALUE and records[0] with ALL_OR_NONE_OPERATION_ROLLED_BACK raises with a
           message naming records[1] only and starting "Salesforce saved none of the 2 records: ";
           12 failures list 10 then "; and 2 more"; 0 and 201 records raise SalesforceClientError;
           update_records rejects a record without a valid "Id".
         - TestFindRecords: by Id builds `SELECT Id, Name FROM Account WHERE Id IN ('001000000000001AAA')`
           (assert the exact string passed to sf.query_all); by an external id the value
           "O'Brien\\x" is quoted as 'O\'Brien\\\\x' in the SOQL; a key with "\n" raises
           SalesforceClientError; a field name "Name; DROP" raises; the key field is not repeated
           when also listed in fields (case-insensitive); attributes are stripped from results.
      5. Run `ruff check .`, `python3 -m pytest tests/unit/test_salesforce_client.py -q`,
         `bandit -c pyproject.toml -r src` and `python3 -m mypy src/privacyfence`.
      Stop condition: if simple_salesforce.exceptions lacks any of the four exception names D3
      lists, stop with status=blocked naming the installed version.
    acceptance:
      - python3 -m pytest tests/unit/test_salesforce_client.py -q passes
      - python3 -m pytest tests/unit/test_salesforce_client.py -q --cov=privacyfence.salesforce_client --cov-branch --cov-report=term-missing shows no missing line in the new methods and helpers
      - bandit -c pyproject.toml -r src reports no issue
      - python3 -m mypy src/privacyfence passes
      - grep -n "class SalesforceWriteError(SalesforceClientError)" src/privacyfence/salesforce_client.py matches

  - id: p2-write-validation
    title: salesforce_writes.py - pure parsing, field checks, diffing and card text
    depends_on: [p1-client-writes]
    complexity: M
    touches:
      - src/privacyfence/salesforce_writes.py
      - tests/unit/test_salesforce_writes.py
    brief: |
      Read docs/salesforce-write-tools-plan.md, "Design" D2, before starting; it is the spec
      (constants, function signatures and every error string).
      1. Create src/privacyfence/salesforce_writes.py with a module docstring saying it holds the
         pure checks the Salesforce write tools run before their approval card, and that no error
         message carries a value read from Salesforce. `from __future__ import annotations` next.
         Import SalesforceField from .salesforce_client.
      2. Implement the constants and the functions parse_fields_object, parse_records,
         field_index, resolve_fields, resolve_key_field, key_text, key_norm, values_differ,
         cell_text and payload_sha256 exactly as D2 specifies, with the shared _check_object
         helper.
      3. Create tests/unit/test_salesforce_writes.py (module docstring naming the module and the
         invariant "no error message contains a value read from Salesforce"; pytestmark unit).
         Classes and the cases each must cover:
         - TestParse: invalid JSON, a list where an object is expected, an empty object, a nested
           value, over MAX_PAYLOAD_CHARS, 0 and 201 records, records[2] being a string — each
           with its exact D2 message.
         - TestResolveFields: unknown field; same field twice in different case; not createable
           on create but fine on update; not updateable on update; type "address" refused;
           null on create refused; null on update allowed when nillable and refused when not;
           every WRITABLE_TYPES kind accepted with a good value and refused with a bad one
           (true for an int field, "5" for a double, "14/10/2026" for a date, "2026-10-14 10:00"
           for a datetime); keys come back canonical ("industry" -> "Industry").
         - TestKeyField: Id accepted; an externalId string field accepted; a non-external field
           and an externalId of type double refused; unknown name refused; key_text rejects
           "", 256 characters and "a\tb"; key_norm("001000000000001AAA", True) ==
           key_norm("001000000000001", True); key_norm("ABC", False) == key_norm("abc", False).
         - TestValuesDiffer: None vs "" equal; 3 vs 3.0 equal for double; True vs "true" handled
           as bool; "2026-10-14T10:00:00.000+0000" vs "2026-10-14T10:00:00Z" equal;
           "2026-10-14T10:00:00Z" vs "2026-10-14T11:00:00Z" differ; "a;b" vs "b;a" equal for
           multipicklist; an unparseable datetime falls back to string comparison.
         - TestCellText: (empty), true/false, the 200-character cut with "…", limit=None.
         - TestNoValueInMessages: build a describe index and call resolve_fields with a value
           "SECRET-VALUE-123" in each failing shape; assert "SECRET-VALUE-123" is in no message.
         - payload_sha256 is stable for key order and differs for a changed value.
      4. Run `ruff check .`, `python3 -m pytest tests/unit/test_salesforce_writes.py -q` and
         `python3 -m mypy src/privacyfence`.
    acceptance:
      - python3 -m pytest tests/unit/test_salesforce_writes.py -q passes
      - python3 -m pytest tests/unit/test_salesforce_writes.py -q --cov=privacyfence.salesforce_writes --cov-branch --cov-report=term-missing reports 100%
      - python3 -m mypy src/privacyfence passes
      - ruff check . passes

  - id: p3-single-record-tools
    title: salesforce_create_record and salesforce_update_record, registered in every gate table
    depends_on: [p2-write-validation]
    complexity: M
    touches:
      - src/privacyfence/connectors/salesforce.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - tests/unit/connectors/test_salesforce_connector.py
      - tests/unit/policy/test_registry.py
      - tests/unit/policy/test_propose.py
      - tests/unit/test_auto_accept.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - docs/approvals-and-policy.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read docs/salesforce-write-tools-plan.md "Design" D1, D4 and D5 first; they are the spec
      (descriptions, param texts, card fields, error strings, table entries, effect sentences).
      Copy the shape of grist_add_records / grist_update_records in src/privacyfence/connectors/grist.py:1018-1121.
      1. connectors/salesforce.py: import from ..salesforce_writes the D2 functions you use and
         _validate_object_type_name/_validate_salesforce_id from ..salesforce_client. Add the
         ToolSpecs salesforce_create_record and salesforce_update_record after
         salesforce_search's spec, with read_only=False and the D1 texts. Add both to call().
         Add a "# ---- #" banner "Popup gate (writes)" and the methods _create_record and
         _update_record per D4, plus the helper _check_object_type. Keep `tool="..."` before
         `gate="popup"` in each gated_call.
      2. auto_accept.py: add the two tools to TOOL_TO_OPERATION (salesforce.create_record,
         salesforce.update_record) and TOOL_TO_GATE ("popup"), next to the Salesforce rows.
      3. policy/registry.py: TOOL_TO_VERB gets salesforce_create_record: Verb.ADD and
         salesforce_update_record: Verb.UPDATE.
      4. write_effects.py: the two D5 sentences for these tools.
      5. gate.py: add both to _TOOL_LAYOUT as WIDE.
      6. policy/propose.py: approved_object_types verbs become (Verb.READ, Verb.SEARCH,
         Verb.ADD, Verb.UPDATE); update the Salesforce comment as D5 says.
      7. Tests:
         - tests/unit/connectors/test_salesforce_connector.py: add the two tools to
           SALESFORCE_SIBLINGS ("salesforce_create_record": ("salesforce_get_record",),
           "salesforce_update_record": ("salesforce_get_record",) — the batch siblings are added
           in p4). Add TestCreateRecord and TestUpdateRecord using gated_call_spy and a client
           MagicMock whose describe_fields returns a list of SalesforceField and get_record returns
           a SalesforceRecord. Cover: gate "popup"; preview has only the D4 keys and no field
           value; preview_tables headers; args carry object_type and sha256 (and record_id);
           raw_data holds no value; a malformed fields JSON and an unknown field raise ValueError
           with no describe/gate call for the JSON case and no gate call for the field case;
           update with no change raises the D4 "nothing to change" error and never gates; update
           sends only the changed fields to client.update_record; SalesforceWriteError from the
           client surfaces as RuntimeError with the same text; results match D1.
           Extend the existing assert_all_tools_leave_an_audit_trail call with arg_overrides for
           the two tools (valid fields JSON, a valid record_id) and make its client return a
           describe list and a record so both reach the gate.
         - tests/unit/policy/test_registry.py: add the two rows to _VERB_MATRIX and change the
           asserted length from 93 to 95.
         - tests/unit/policy/test_propose.py: add both tools to the per-tool context list at
           lines 79-82 with args {"object_type": "Account", ...}; add a test that
           propose.operations_for(<approved_object_types entry>, Verb.ADD) contains
           "salesforce.create_record" and, for Verb.UPDATE, "salesforce.update_record".
         - tests/unit/test_auto_accept.py: add both tools to any table spot-check that lists
           Salesforce tools.
      8. Regenerate: `python3 scripts/generate_tools_reference.py` and
         `python3 scripts/generate_always_allow_reference.py`; commit both outputs.
      9. website/connectors/index.html: on the Salesforce card set data-tools and the printed
         tools count +2 and data-popup and "need approval" count +2 (5→7, 0→2 on today's main);
         change the "Needs your approval" <dd> to "Creating or updating a record." and the
         connector-note to "Writes create or update records, never delete, each behind a card.";
         add 2 to the "<N> tools" total in the h1. website/how-it-works/index.html: add 2 to
         "<N> connector tools".
      10. docs/approvals-and-policy.md: the Salesforce object type row's last column per D5.
      11. Run `ruff check .`, the verify_after_merge pytest command from the plan, and
          `python3 -m mypy src/privacyfence`.
      Stop condition: if tests/unit/test_website_connectors_page.py or
      test_readme_manifest_alignment.py still fails after step 9 for a connector other than
      Salesforce, stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_salesforce_connector.py -q passes
      - python3 -m pytest tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_auto_accept.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py tests/unit/web/test_tool_schema_portability.py -q passes
      - grep -n 'salesforce_create_record.*popup' src/privacyfence/auto_accept.py matches
      - python3 -m mypy src/privacyfence passes

  - id: p4-batch-tools
    title: salesforce_create_records and salesforce_update_records
    depends_on: [p3-single-record-tools]
    complexity: M
    touches:
      - src/privacyfence/connectors/salesforce.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - tests/unit/connectors/test_salesforce_connector.py
      - tests/unit/policy/test_registry.py
      - tests/unit/policy/test_propose.py
      - tests/unit/test_auto_accept.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read docs/salesforce-write-tools-plan.md "Design" D1, D4 (the two batch tools) and D5
      first. Follow the pattern p3 added for the single-record tools (git log --grep "Plan-Phase: p3"
      on this branch shows the commit).
      1. connectors/salesforce.py: ToolSpecs salesforce_create_records and
         salesforce_update_records (D1 texts; key_field is ToolParam("key_field", "str",
         required=False, default="Id", ...)), dispatch in call(), and methods _create_records and
         _update_records exactly per D4, in the "Popup gate (writes)" group.
      2. auto_accept.py: TOOL_TO_OPERATION (salesforce.create_records, salesforce.update_records)
         and TOOL_TO_GATE ("popup"). registry.py: Verb.ADD / Verb.UPDATE. write_effects.py: the two
         D5 batch sentences. gate.py: both WIDE. propose.py needs no change (verbs already there).
      3. Tests in tests/unit/connectors/test_salesforce_connector.py:
         - SALESFORCE_SIBLINGS: "salesforce_create_records": ("salesforce_create_record",),
           "salesforce_update_records": ("salesforce_update_record", "salesforce_search"), and add
           "salesforce_create_records" to salesforce_create_record's tuple and
           "salesforce_update_records" to salesforce_update_record's.
         - TestCreateRecords: card counts and table ("#" column, union headers, "" for a missing
           field); records[1] with an unknown field is refused naming records[1]; 201 records
           refused before describe; client.create_records receives the resolved records in order;
           result {object_type, created, ids}.
         - TestUpdateRecords: by Id (15-char input matches an 18-char row); by an external id
           field with case-different input; key_field not External ID refused; duplicate key
           refused; a key matching two rows refused; 12 unmatched keys list 10 and "and 2 more";
           a record with only the key refused; unchanged records counted and not written; all
           unchanged refused without a gate call; preview keys exactly the D4 set; args carry
           object_type, key_field and sha256; the error text never contains a current value read
           from find_records (use "SECRET-CURRENT" as a current value and assert it is absent from
           every raised message).
         - Extend the audit-trail call's arg_overrides for both tools.
         - tests/unit/policy/test_registry.py: two rows, length 95 → 97.
           tests/unit/policy/test_propose.py: both tools in the context list; operations_for for
           Verb.ADD/UPDATE also contain "salesforce.create_records"/"salesforce.update_records".
           tests/unit/test_auto_accept.py: add to any Salesforce spot-check.
      4. Regenerate docs/tools-reference.md and docs/always-allow-rules-reference.md with the two
         generator scripts.
      5. website/connectors/index.html: Salesforce card tools +2 and popup +2 (7→9, 2→4 if p3
         left 7 and 2), printed counts to match, h1 total +2; "Needs your approval" <dd> becomes
         "Creating or updating a record, or up to 200 at once."; website/how-it-works/index.html
         total +2.
      6. Run `ruff check .`, the plan's verify_after_merge pytest command and
         `python3 -m mypy src/privacyfence`.
      Stop condition: if p3's single-record methods are not in connectors/salesforce.py, stop with
      status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_salesforce_connector.py -q passes
      - python3 -m pytest tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_auto_accept.py tests/unit/test_systemic_gate_invariants.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py tests/unit/web/test_tool_schema_portability.py -q passes
      - grep -c 'salesforce_create_record\|salesforce_update_record' src/privacyfence/policy/registry.py prints 4
      - python3 -m mypy src/privacyfence passes

  - id: p5-qa-lifecycle
    title: Live lifecycle check for the Salesforce writes
    depends_on: [p1-client-writes]
    complexity: S
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/qa_environment.yaml.example
      - docs/connector-qa.md
    brief: |
      Read docs/salesforce-write-tools-plan.md "Design" D6 first.
      1. scripts/qa_fixture_recorder.py: add lifecycle_salesforce(manifest) after lifecycle_peek
         (alphabetical order of the existing lifecycle_* functions), modelled on lifecycle_grist
         (scripts/qa_fixture_recorder.py:2462-2510): build the client the way check_salesforce
         does, run the D6 steps, return LifecycleResult("salesforce", ok, note,
         cleanup_ok=None) with note "create, get, update, batch create, find, batch update all
         verified" on success. Docstring states it never deletes and why. Register it in
         LIFECYCLE_CHECKS in alphabetical position.
      2. tests/fixtures/qa_environment.yaml.example: under salesforce:, add commented
         lifecycle_object_type: Task, lifecycle_name_field: Subject, lifecycle_note_field:
         Description, with a comment that they are the --lifecycle check's defaults.
      3. tests/unit/test_qa_fixture_recorder.py: a test that drives lifecycle_salesforce with a
         MagicMock client (patch the builder the function uses) through success, and one where
         update_record raises, asserting ok False and the note carries the error; and that
         "salesforce" is in LIFECYCLE_CHECKS.
      4. docs/connector-qa.md: add Salesforce to the --lifecycle list (around line 385) and the
         manifest-keys table row (around line 342), and a short paragraph in "Salesforce checks"
         (around 641-650) saying the lifecycle check creates and updates Tasks tagged with the
         lifecycle tag and never deletes them, so they are deleted by hand now and then.
      5. Run `ruff check .` and `python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q`.
      6. Push the phase branch, then dispatch connector-live-check.yml (no inputs) against it with
         the GitHub MCP actions_run_trigger tool; record the run URL and the lifecycle report's
         Salesforce row in your phase report. A red Salesforce lifecycle row from
         INSUFFICIENT_ACCESS is a finding for the maintainer, not a code change: report it and
         finish. Any other red Salesforce row: stop with status=blocked and paste the row.
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes
      - grep -n 'lifecycle_salesforce,' scripts/qa_fixture_recorder.py matches
      - the phase report contains the connector-live-check.yml run URL and its Salesforce lifecycle row

  - id: p6-docs-and-copy
    title: User docs, website copy and changelog for Salesforce writes
    depends_on: [p4-batch-tools]
    complexity: S
    touches:
      - website/connectors/salesforce/index.html
      - website/compare/claude-connectors/index.html
      - README.md
      - docs/salesforce-setup.md
      - CHANGELOG.md
    brief: |
      Read docs/salesforce-write-tools-plan.md "Goal" and "Current state" (the read-only claims
      list) first. Match the website pages' existing voice: short plain sentences.
      1. website/connectors/salesforce/index.html:
         - <title>: "Salesforce for AI assistants (MCP), reviewed — PrivacyFence".
         - meta description: "Let AI assistants look up, search and report on Salesforce records,
           and create or update them, through PrivacyFence: every read reviewed, every write
           approved."
         - og:description: "Salesforce for AI assistants: records, search and reports reviewed
           before release; creating and updating records only with your approval, never deleting."
         - lede (line 30): "PrivacyFence lets an AI assistant read Salesforce records, search and
           reports, and create or update records. Every record and report result is shown to you
           before the assistant gets it, and every change waits for your approval. Nothing
           deletes."
         - h2 (line 36): "Reviewed reads, approved writes."
         - "Needs your approval" card (line 50): "Creating a record or updating one, or up to 200
           of one object type at once, all or none. The card shows each field's current and new
           value. The assistant can only change what your own Salesforce user can change."
      2. website/compare/claude-connectors/index.html:78: "Salesforce (read-only)" becomes
         "Salesforce".
      3. README.md:75: "| Salesforce | Read records, search, run reports; create and update records |".
      4. docs/salesforce-setup.md: add a section "## Creating and updating records" after the
         section that lists the OAuth scopes: the existing api scope already covers writes, so no
         one signs in again; what the user may change is decided by their own Salesforce profile
         and permission sets; every write shows an approval card and nothing deletes; an
         "Always allow" rule for an object type can now include create and update, and a rule
         saved earlier keeps only the verbs it had (Settings → Auto-accept offers "Update rule"
         only for verbs it already has).
      5. CHANGELOG.md under "## [Unreleased]" (Added): "Salesforce: create and update records,
         one at a time or up to 200 of one object type in one approval, all or none. Updates show
         each field's current and new value; nothing deletes. An object-type "Always allow" rule
         can include create and update."
      6. Run `python3 -m pytest tests/unit/test_website_connectors_page.py tests/unit/test_website_connector_pages.py -q`
         and `ruff check .`. If the repo has a website link/build test (grep tests/unit for
         build_site), run it too.
    acceptance:
      - grep -n -i "read-only" website/connectors/salesforce/index.html finds nothing
      - grep -n "Salesforce (read-only)" website/compare/claude-connectors/index.html finds nothing
      - grep -n "## Creating and updating records" docs/salesforce-setup.md matches
      - python3 -m pytest tests/unit/test_website_connectors_page.py tests/unit/test_website_connector_pages.py -q passes

  - id: p7-retire-plan
    title: ADRs, reference-doc check, delete the plan
    depends_on: [p4-batch-tools, p5-qa-lifecycle, p6-docs-and-copy]
    complexity: S
    touches:
      - docs/adr/*
      - docs/salesforce-write-tools-plan.md
      - docs/salesforce-write-tools-plan-manual-steps.html
      - docs/tools-reference.md
    brief: |
      Read docs/adr/README.md (template, index format, the rule that ADRs never link a plan
      document) and the plan's "Design" D7 and "ADRs" sections.
      1. Find the next three free ADR numbers in docs/adr/ (ls docs/adr | sort). Write the three
         ADRs from the plan's ADRs section in that order, using the template in docs/adr/README.md,
         Status "Accepted — <today>. Implemented.". Context/Decision/Alternatives/Consequences
         come from the plan's Goal, D1-D7 and Risks; Verification names the test classes added in
         p1-p5; Related links src/privacyfence/salesforce_client.py, salesforce_writes.py,
         connectors/salesforce.py and ADRs 0073, 0105, 0145, 0148, 0166, 0171. No link to the plan.
      2. Add the three rows to docs/adr/README.md's index table in number order.
      3. Delete docs/salesforce-write-tools-plan.md and
         docs/salesforce-write-tools-plan-manual-steps.html.
      4. `grep -rn "salesforce-write-tools-plan" .` must find nothing; fix any link it finds.
      5. Re-run `python3 scripts/generate_tools_reference.py`; if it changes docs/tools-reference.md,
         commit that.
      6. Run `ruff check .` and `python3 -m pytest tests/unit -q` (includes
         tests/unit/test_code_no_history.py and any ADR index test).
    acceptance:
      - test ! -e docs/salesforce-write-tools-plan.md && test ! -e docs/salesforce-write-tools-plan-manual-steps.html
      - grep -rn "salesforce-write-tools-plan" . --include=*.md --include=*.html --include=*.py finds nothing
      - grep -c "Salesforce" docs/adr/README.md is at least 4 (0133 plus the three new rows)
      - python3 -m pytest tests/unit -q passes
```
