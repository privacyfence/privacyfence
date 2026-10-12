# Plan: a read-only Jira Assets connector

## Goal

Jira Service Management **Assets** (the CMDB formerly called Insight) holds structured objects,
such as contracts, accounts and hardware assets, linked to each other by reference attributes and
to Jira tickets. Today an agent can read Jira issues and Confluence pages through PrivacyFence but
cannot see Assets at all. After this plan, a new connector, `assets` (shown as **Jira Assets**),
lets an agent:

- browse the object schemas, object types and attribute definitions without an approval card;
- search objects with AQL and read one object with every attribute value, after review;
- follow the links from an object (contract → account → assets, inbound and outbound) to a
  bounded depth in one reviewed call;
- list the Jira tickets connected to an object, after review;
- export an AQL result set to a CSV or JSON file after one review.

It never writes to Assets. It reuses the existing Atlassian sign-in. The extra Atlassian
permissions are requested only when Assets is switched on, so nothing changes for Jira or
Confluence users who never turn it on.

No tracking issue: the request came from the maintainer in a planning session.

## Current state

- **Atlassian OAuth.** `src/privacyfence/atlassian_oauth.py:40-66` defines `DEFAULT_SCOPES`:
  classic Jira scopes plus granular Confluence ones and `offline_access`. `build_authorize_url`
  (:83-102) always sends `scopes or DEFAULT_SCOPES` with `prompt=consent`. Its three callers rely
  on the default:
  - `settings_controller._authenticate_atlassian`, which calls `atlassian_authorize_interactive`
    at `settings_controller.py:1376-1381`. That name is only an import alias of
    `atlassian_oauth.authorize_interactive` (`settings_controller.py:91`, `daemon_main.py:148`),
    which already takes `scopes` and forwards it (`atlassian_oauth.py:176-189`);
  - `daemon_main.run_atlassian_oauth` (`daemon_main.py:~1855`);
  - `web/routes_connect._build_authorize_url` (`routes_connect.py:375-380`).

  `resolve_resource_and_save` (:137-169) stores `{access_token, refresh_token, cloud_id, site_url,
  account_email}` and does not store which scopes were granted. The accessible-resources response
  does list each resource's `scopes`. Mixing classic and granular scopes can return the same site
  twice, once per scope family (see `daemon_main._cli_pick_atlassian_resource`, :1814-1850).
- **Token refresh.** Both `JiraClient._try_refresh` (`jira_client.py:250-285`) and
  `ConfluenceClient._try_refresh` (`confluence_client.py:264-299`) rewrite the shared token file
  as a fixed five-key dict. Any new key in the record is dropped on the first refresh.
- **No Assets code.** Nothing in the repository mentions Assets, CMDB, Insight or AQL. The
  vendored `atlassian-python-api` has an `AssetsCloud` class, but it assumes basic-auth access and
  is not used here.
- **Connector enablement.** `daemon_main.build_connectors` builds every connector whose
  `connectors.<name>.enabled` is not false: `enabled()` defaults to `True` at
  `daemon_main.py:1304-1305`. `settings_controller._connectors_state` makes the same default at
  `settings_controller.py:2005`. A new connector is therefore on by default unless the code says
  otherwise.
- **The Jira connector** (`src/privacyfence/connectors/jira.py`) is the model to follow:
  - `ToolSpec`/`ToolParam` declarations are at :336-610, and an `if tool == …` dispatch is at
    :616-643.
  - Auto tools write their own audit entry with `_auto_audit` (:1138-1157) and fetch through
    `_fetch` (:1131-1136), which re-raises `JiraClientError` as `RuntimeError`.
  - Review reads call `gate.gated_call(..., gate="review", pii_scan_text=…, preview_tables=…)`
    (`_get_issue` :802-904, `_search_issues_with_fields` :700-729).
  - The export is `jira_export_issues` (:399-421; ADR 0159).
- **Gate and policy tables** that each tool must be added to:
  - `auto_accept.TOOL_TO_OPERATION` (:84; Jira rows :155-161), `TOOL_TO_GATE` (:197; Jira rows
    :294-306) and `PERSONAL_DATA_RULE_TOOLS` (:359-366);
  - `policy/registry.TOOL_TO_VERB` (:145; Jira rows :211-217);
  - `policy/scopes.py` (Jira selectors :273-305, `SCOPE_SELECTORS` :472-487, `NEW_SCOPE_SELECTORS`
    :584);
  - `policy/propose.py` (`_jira_project_keys` :168, `_jira_result_project_keys` :179,
    `PROPOSABLE_SCOPES` :336-343);
  - `policy/catalogue.py` (`VALUE_HINTS` :89-108, `PERSONAL_DATA_GROUPS` :113);
  - `policy/resource_registry.py` (`_resolve_jira_project` :75, the Jira `GrantResourceType`
    :224-237);
  - `gate._TOOL_LAYOUT` (:258-299).
- **Guardrail tests a new connector module trips.** These are the files Peek's connector commit
  `99b771a8` touched:
  - `tests/unit/test_website_connector_pages.py:27-41` (`CONNECTORS` must equal the modules in
    `connectors/`);
  - `tests/unit/test_website_connectors_page.py:55-60` (the totals, plus `len(REFERENCE) == 13`
    and "Thirteen connectors");
  - `tests/unit/connectors/test_readme_manifest_alignment.py:23-41` (`CONNECTOR_CLASSES`);
  - `scripts/generate_tools_reference.py:44-75` (`CONNECTOR_TITLES`, `CONNECTOR_SHORT`; a missing
    entry makes `render()` raise), which regenerates `docs/tools-reference.md`, checked by
    `test_docs_tools_reference.py`;
  - `scripts/pyinstaller_common.py:83-84`, checked by `test_pyinstaller_hidden_imports.py`;
  - `tests/helpers.py:75` `assert_tool_definitions_complete` (ADR 0115), run for every connector by
    `tests/unit/web/test_tool_schema_portability.py:50`;
  - `tests/unit/test_systemic_gate_invariants.py` and `tests/unit/test_connector_tool_annotations.py`,
    which discover connectors automatically; the first also requires `pii_scan_text=` on every
    `gate="review"` `gated_call` (`TestPiiScanTextOnEveryReviewGatedTool`, :225);
  - `tests/unit/test_generate_always_allow_reference.py`, which fails when
    `docs/always-allow-rules-reference.md` is not regenerated after any review or popup tool is
    added (`scripts/generate_always_allow_reference.py`, no per-connector title map);
  - `tests/unit/test_docs_configuration_reference.py:129-145`, which requires every
    `build_org_bundle.py` option to be documented in `docs/configuration-reference.md`;
  - `tests/unit/policy/test_scopes.py:315-328`, which requires every `SCOPE_SELECTORS` entry to
    have a v1 counterpart in `tests/unit/policy/_v1_reference.py` and pins the count at 48
    (precedent: commit 8facf006 added `approved_project_keys_all_results` that way).
- **Settings toggle and example config.** `enable_connector`/`disable_connector` write an explicit
  `connectors.<name>.enabled` bool through `_set_connector_enabled` (`settings_controller.py:1157-1176`).
  `src/privacyfence/resources/settings.yaml.example:118-124` has only a commented `connectors:`
  example saying "all enabled by default".
- **QA recorder.** `scripts/qa_fixture_recorder.py`'s `CONNECTOR_CHECKS` (:1992) and
  `EXPECTED_FIXTURES` (:2019) must have equal keys (an import-time assert at :2041).
  `tests/unit/test_qa_fixture_recorder.py:2099-2130` (`TestFixturePresence`) fails unless every
  listed fixture file exists under `tests/fixtures/live/<connector>/`. `RawCapture` (:599-632)
  wraps a client's `_request(fn, *a, **kw)` and records what it returns.

## Design

### 3.1 Names

| Thing | Value |
|---|---|
| Connector key / `name` | `assets` |
| Display label | `Jira Assets` |
| Client module | `src/privacyfence/assets_client.py`, class `AssetsClient`, error `AssetsClientError(Exception)` |
| Connector module | `src/privacyfence/connectors/assets.py`, class `AssetsConnector` |
| Operation key (every reviewed tool) | `assets.read_object` |
| Scope type | `assets.schema` (IDENTITY) |
| Predicates | `approved_schema_ids` (READ), `approved_schema_ids_all_results` (SEARCH) |
| Grant config key | `schemas` |
| Feature branch | `feature/jira-assets-connector` |

### 3.2 Permissions: requested only when Assets is enabled

Add these to `atlassian_oauth.py`:

```python
ASSETS_SCOPES: list[str] = [
    "read:cmdb-schema:jira", "read:cmdb-type:jira", "read:cmdb-attribute:jira",
    "read:cmdb-object:jira",
    # The Assets workspace id comes from Jira Service Management's
    # /rest/servicedeskapi/assets/workspace, which is behind a JSM scope.
    "read:servicedesk-request",
]

def scopes_for(*, assets: bool) -> list[str]:
    return DEFAULT_SCOPES + ASSETS_SCOPES if assets else list(DEFAULT_SCOPES)

def assets_enabled(config: dict[str, Any], org_config: dict[str, Any]) -> bool:
    """Assets is off unless switched on. An explicit connectors.assets.enabled wins;
    otherwise the org bundle's atlassian.assets flag is the default."""
    explicit = ((config.get("connectors") or {}).get("assets") or {}).get("enabled")
    if explicit is not None:
        return bool(explicit)
    return bool((org_config.get("atlassian") or {}).get("assets", False))
```

- `build_authorize_url` and `authorize_interactive` (imported elsewhere as
  `atlassian_authorize_interactive`) already take `scopes`. Each of the three callers imports
  `from .atlassian_oauth import assets_enabled, scopes_for` (`..atlassian_oauth` in `web/`) and
  passes `scopes=scopes_for(assets=assets_enabled(<config>, org_config))`:
  - Settings: `cfg = self._load_config()` in `_authenticate_atlassian`;
  - the CLI: `def run_atlassian_oauth(org_config: dict[str, Any], config: dict[str, Any] | None = None) -> int`,
    using `config or {}`; `main()` (`daemon_main.py:2400`) calls `run_atlassian_oauth(org_config, config)`
    (`config` is loaded at :2374);
  - org-mode `/connect` has only the bundle, so it passes `{}` as config.
- `resolve_resource_and_save` adds a sixth key to the token record. Its value is the union of the
  `scopes` of every accessible resource whose `id` equals the chosen `cloud_id`:
  `"scopes": sorted({s for r in resources if r.get("id") == cloud_id for s in r.get("scopes") or []})`.
- `JiraClient._try_refresh` and `ConfluenceClient._try_refresh` add
  `"scopes": self._config.get("scopes", [])` to the dict they save, so a refresh keeps the key.
- `scripts/build_org_bundle.py` gets `--atlassian-assets` (`action="store_true"`, in the
  "Atlassian (Jira + Confluence)" argument group, help text: "Request Jira Assets permissions at
  Atlassian sign-in (the app must have the read:cmdb-* scopes)"). When it is given, the bundle's
  `atlassian` section gets `"assets": true`. Giving it without the client id and secret is a
  `SystemExit("--atlassian-assets needs --atlassian-client-id and --atlassian-client-secret.")`.
- `assets_client.py` defines `ASSETS_REQUIRED_SCOPE = "read:cmdb-object:jira"`. A token record
  without it in `scopes` (every token made before this
  change, and every token made while Assets was off) makes `AssetsClient` raise
  `AssetsClientError(NEEDS_ASSETS_PERMISSION)`, with
  `NEEDS_ASSETS_PERMISSION = "Jira Assets needs an extra Atlassian permission. Use Authenticate… on Jira Assets in PrivacyFence Settings."`.
  The message contains "Use Authenticate…", so `daemon_main._classify_connector_failure`
  (:1236-1266) reports `not_authenticated` and Settings offers the button.

Rejected:

- **Adding the scopes to `DEFAULT_SCOPES`.** Every Jira and Confluence user would have to
  reconnect. Worse, an organisation whose own Atlassian app lacks the `read:cmdb-*` scopes would
  find every Atlassian sign-in failing at the consent screen.
- **A separate API-token credential.** It would be a second Atlassian secret on disk, with a
  different setup path from Jira and Confluence.
- **Inferring the scopes from a 401.** Atlassian's 401 for a missing scope looks the same as one
  for an expired token, and the existing clients treat 401 as "refresh".

### 3.3 Client (`assets_client.py`)

Plain `requests`, not atlassian-python-api: `self._session = requests.Session()` with the Bearer
header; `_send(method, url, params=None, json=None)` calls
`self._session.request(method, url, params=params, json=json, timeout=30)`; `_try_refresh` updates
`self._session.headers`. Tokens are written only through `atlassian_oauth.save_token_file`, so
`TOKEN_WRITE_SITES` in `test_systemic_gate_invariants.py` stays unchanged.

- `AssetsClient(config: dict[str, Any], token_file: str | None = None)`. `config` is the same
  merged `atlassian_config` the Jira client gets, so it has `access_token`, `refresh_token`,
  `cloud_id`, `client_id`, `client_secret`, `scopes`, `account_email` and `site_url`.
- The constructor raises `AssetsClientError("Jira Assets is not authenticated. Use Authenticate… in PrivacyFence Settings.")`
  when `access_token` or `cloud_id` is missing, and `AssetsClientError(NEEDS_ASSETS_PERMISSION)`
  when `ASSETS_REQUIRED_SCOPE` is not in `config.get("scopes") or []`.
- `self._jira_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"`.
- The workspace id is resolved lazily and cached on the instance:
  1. `GET {_jira_url}/rest/servicedeskapi/assets/workspace` returns `{"values": [{"workspaceId": "…"}]}`.
  2. The first value's `workspaceId` is used.
  3. If there is none, raise `AssetsClientError("This Atlassian site has no Assets workspace (Jira Service Management Assets is not enabled).")`.
- `self._api_url` is `f"{_jira_url}/jsm/assets/workspace/{workspace_id}/v1"`.
- `_try_refresh()` is a copy of `JiraClient._try_refresh`. It logs `"Assets token refresh failed: %s"`
  and `"Assets access token refreshed"`, and carries `scopes` (ADR 0116: clients share no code).
- `_request(fn, *args, **kwargs)` makes one refresh-and-retry when `atlassian_oauth.is_unauthorized(exc)`
  is true. It returns whatever `fn` returns. Every HTTP call goes through
  `self._request(self._send, method, url, params=…, json=…)`, where `_send` calls
  `raise_for_status()` and returns `response.json()`. `RawCapture` then records decoded JSON.
- `check_connection() -> None` resolves the workspace id. Nothing else.
- Input checks raise `AssetsClientError`:
  - an object, object type or schema id must match `^[0-9]{1,12}$`
    (`"<what> must be a numeric Assets id"`);
  - AQL must be non-empty and at most 4000 characters (`"aql must be 1-4000 characters"`).
- Constants: `MAX_PAGES = 10`, `AQL_PAGE_SIZE = 50`, `SEARCH_MAX = 100`,
  `EXPORT_MAX_OBJECTS = 10_000`, `EXPORT_PAGE_SIZE = 100`, `RELATED_MAX_OBJECTS = 200`,
  `RELATED_MAX_DEPTH = 3`, `ID_BATCH = 50`.

Dataclasses (frozen):

```python
@dataclass(frozen=True)
class AssetsSchema:            # GET objectschema/list (paged: startAt, maxResults; values, isLast)
    id: str; name: str; key: str; description: str; object_count: int; object_type_count: int

@dataclass(frozen=True)
class AssetsObjectType:        # GET objectschema/{id}/objecttypes/flat (a bare list)
    id: str; name: str; schema_id: str; parent_id: str; description: str; object_count: int

@dataclass(frozen=True)
class AssetsAttributeDef:      # GET objecttype/{id}/attributes (a bare list)
    id: str; name: str; kind: str; value_type: str
    reference_type_id: str; reference_type_name: str; reference_kind: str
    min_cardinality: int; max_cardinality: int; is_label: bool; is_system: bool

@dataclass(frozen=True)
class AssetsReference:
    id: str; key: str; label: str

@dataclass(frozen=True)
class AssetsObject:
    id: str; key: str; label: str; type_id: str; type_name: str; schema_id: str
    created: str; updated: str
    attributes: dict[str, list[str]]                  # attribute name -> display values
    references: dict[str, list[AssetsReference]]      # reference attributes only
    def to_dict(self) -> dict[str, Any]: ...          # see "Object output" below

@dataclass(frozen=True)
class AssetsEdge:
    source_id: str; target_id: str; attribute: str

@dataclass(frozen=True)
class AssetsGraph:
    root_id: str; objects: list[AssetsObject]; edges: list[AssetsEdge]; truncated: bool

@dataclass(frozen=True)
class AssetsTicket:            # GET objectconnectedtickets/{objectId}/tickets -> {"tickets": [...]}
    key: str; summary: str; status: str; issue_type: str; priority: str
```

`kind` maps the attribute's integer `type` as follows: 0 `default`, 1 `reference`, 2 `user`,
3 `confluence`, 4 `group`, 5 `version`, 6 `project`, 7 `status`, anything else `other`.

- `value_type` is `defaultType.name` for kind `default`, and `""` otherwise.
- `reference_type_id`/`reference_type_name` come from `referenceObjectTypeId`/`referenceObjectType.name`.
- `reference_kind` is `referenceType.name`.
- `min_cardinality`/`max_cardinality` come from `minimumCardinality`/`maximumCardinality`.
- `is_label` is `label`, and `is_system` is `system`.
- Missing keys read as `""`, `0` or `False`.

**Parsing an object** (`_parse_object(raw, names)`):

- `id`, `objectKey` → `key`, `label`, `objectType.id`/`.name`, `objectType.objectSchemaId` →
  `schema_id`, `created`, `updated`.
- For each entry of `raw["attributes"]`, the attribute name is the first of these that exists:
  1. `entry["objectTypeAttribute"]["name"]`;
  2. `names[entry["objectTypeAttributeId"]]`, where `names` is built from the response-level
     `objectTypeAttributes` list of an AQL page;
  3. the name from `self.list_attributes(type_id)`, cached per instance per type id.
- Each item of `entry["objectAttributeValues"]`:
  - with `referencedObject` → an `AssetsReference(id, objectKey, label)` under `references[name]`,
    **and** `"<label> (<key>)"` under `attributes[name]`;
  - with `user` → `mask_emails(user.get("displayName") or "", user.get("key") or "") or UNKNOWN_USER_LABEL`,
    both imported from `privacyfence.atlassian_users` (`mask_emails(name, account_id)` is at
    `atlassian_users.py:61`; Assets' user object carries the account id in `key`); an email
    address is never kept;
  - with `status` → `status["name"]`;
  - with `group` → `group["name"]`;
  - otherwise `displayValue` if present and non-empty, else `str(value)`.
- An attribute with no values is left out.

**Object output** (`AssetsObject.to_dict()`, what tools return):

```json
{"id": "123", "key": "PFQA-123", "label": "ACME support contract", "type": "Contract",
 "type_id": "7", "schema_id": "2", "created": "…", "updated": "…",
 "attributes": {"Name": "ACME support contract", "Tags": ["a", "b"]},
 "references": {"Account": [{"id": "45", "key": "PFQA-45", "label": "ACME Ltd"}]}}
```

A one-element value list collapses to a scalar in `attributes`. `references` always holds lists.

**Methods** (each wraps failures as `AssetsClientError(f"<method> failed: {exc}")` and re-raises
its own `AssetsClientError` unchanged):

| Method | Request | Notes |
|---|---|---|
| `list_schemas(max_results=100) -> list[AssetsSchema]` | `GET objectschema/list?startAt&maxResults` | ADR 0116 loop: stop at `isLast`, an empty page, `max_results` or `MAX_PAGES` |
| `get_schema(schema_id) -> AssetsSchema` | `GET objectschema/{id}` | used by the grant-name resolver |
| `list_object_types(schema_id) -> list[AssetsObjectType]` | `GET objectschema/{id}/objecttypes/flat` | |
| `list_attributes(type_id) -> list[AssetsAttributeDef]` | `GET objecttype/{id}/attributes` | cached per instance |
| `search_objects(aql, max_results=25) -> list[AssetsObject]` | `POST object/aql?startAt&maxResults&includeAttributes=true`, body `{"qlQuery": aql}` | clamp to 1..`SEARCH_MAX`; pages of `min(remaining, AQL_PAGE_SIZE)`; stop at `isLast` true, an empty `values` or `MAX_PAGES`; do not trust `hasMoreResults` or `total` |
| `get_object(object_id) -> AssetsObject` | `GET object/{id}` and then `GET object/{id}/attributes` | the second call's list is the `attributes` passed to `_parse_object` |
| `get_objects(ids) -> list[AssetsObject]` | AQL `objectId IN (1,2,…)` in batches of `ID_BATCH` | order follows `ids` |
| `related_objects(object_id, direction="both", depth=1, max_objects=50) -> AssetsGraph` | see 3.5 | |
| `connected_tickets(object_id) -> list[AssetsTicket]` | `GET objectconnectedtickets/{id}/tickets` | `key`, `title` → summary, `status.name`, `type.name`, `priority.name` |
| `export_objects(aql) -> list[AssetsObject]` | the AQL loop with `EXPORT_PAGE_SIZE` and no `MAX_PAGES` | raises `AssetsClientError("The AQL query matches more than 10,000 objects; narrow it and export in parts.")` once a 10,001st object is seen |

### 3.4 Tools

Every tool sets `read_only=True` and has a required `reason` param, worded exactly as in Jira.
Descriptions follow ADR 0115:

- the first sentence is the summary;
- then one `Returns` sentence;
- then one sentence per sibling;
- then `Auto-approved.` or `Requires user approval.`;
- every parameter except `reason` has a description of at least 20 characters;
- the whole description is at most 1024 characters.

The sibling map for `TestToolDefinitions` is `ASSETS_SIBLINGS` (all names carry the `assets_`
prefix; each phase adds only its own rows):

- p3: `list_schemas → (list_object_types,)`, `list_object_types → (list_schemas, list_attributes)`,
  `list_attributes → (list_object_types,)`;
- p4: `list_attributes` also gets `search_objects`; `search_objects → (get_object,)`,
  `get_object → (search_objects,)`;
- p6: `get_object` also gets `get_related_objects`; `get_related_objects → (get_object,)`,
  `get_connected_tickets → (get_object,)`;
- p7: `search_objects` also gets `export_objects`; `export_objects → (search_objects,)`.

| Tool | Gate | Verb | Params (name type default) | Returns |
|---|---|---|---|---|
| `assets_list_schemas` | auto | — | `max_results int 50` (1-100) | list of `{id, name, key, description, object_count, object_type_count}` |
| `assets_list_object_types` | auto | — | `schema_id str` | list of `{id, name, parent_id, description, object_count}` |
| `assets_list_attributes` | auto | — | `object_type_id str` | list of `{id, name, kind, value_type, reference_type_id, reference_type_name, reference_kind, min_cardinality, max_cardinality, is_label}` (no `is_system` rows: system attributes are left out) |
| `assets_search_objects` | review | SEARCH | `aql str`, `max_results int 25` (1-100) | list of object dicts |
| `assets_get_object` | review | READ | `object_id str` | one object dict |
| `assets_get_related_objects` | review | SEARCH | `object_id str`, `direction str "both"` (`outbound`, `inbound` or `both`), `depth int 1` (1-3), `max_objects int 50` (1-200) | `{root_id, objects: [object dicts], edges: [{from, to, attribute}], truncated}` |
| `assets_get_connected_tickets` | review | READ | `object_id str` | `{object: {id, key, label, type}, tickets: [{key, summary, status, issue_type, priority}]}` |
| `assets_export_objects` | review | SEARCH | `aql str`, `destination_dir str`, `file_name str ""` (empty means `assets-objects.csv`) | `{rows, columns, sha256, path, name, size_bytes, delivery}`, as `jira_export_issues` returns it |

The summaries (first sentences) are fixed:

- `assets_list_schemas`: "List the Jira Assets object schemas on the connected Atlassian site."
- `assets_list_object_types`: "List the object types in one Jira Assets schema."
- `assets_list_attributes`: "List the attribute definitions of one Jira Assets object type, including which attributes reference other object types."
- `assets_search_objects`: "Search Jira Assets objects with an AQL query and return them with their attribute values."
- `assets_get_object`: "Read one Jira Assets object with every attribute value and its outbound references."
- `assets_get_related_objects`: "Read the Jira Assets objects linked to one object, following references outbound, inbound or both, up to three hops."
- `assets_get_connected_tickets`: "List the Jira tickets connected to one Jira Assets object."
- `assets_export_objects`: "Write every Jira Assets object an AQL query matches, up to 10,000, to a CSV or JSON file on the user's computer, in one approval."

**Review cards** (all `gate="review"`):

| Tool | `tool_name` | `summary` | `raw_data` | Card |
|---|---|---|---|---|
| `assets_search_objects` | `Search Jira Assets` | `f"AQL: {aql[:120]}"` | the `list[AssetsObject]` | `preview={"Query": aql[:200], "Objects": str(n)}`, one table (headers Key, Label, Type, then attribute names in first-seen order up to 8; cells cut to 120 chars), `table_only=True`, `pii_scan_text` holds every attribute value joined by newlines |
| `assets_get_object` | `Read Jira Assets Object` | `f"{key}: {label}"` | the `AssetsObject` | `preview={"Key", "Label", "Type", "Schema": schema_id}`, a two-column table Attribute/Value, `pii_scan_text` as above |
| `assets_get_related_objects` | `Read Linked Jira Assets Objects` | `f"{root key}: {n} linked objects"` | `graph.objects` (root included) | `preview={"Root", "Direction", "Depth", "Objects": str(n)}`, a table per object type (Key, Label, then up to 8 attributes), then an edges table (From, Attribute, To); `pii_scan_text` holds every attribute value of every object in `graph.objects`, joined by newlines |
| `assets_get_connected_tickets` | `Read Jira Assets Object Tickets` | `f"{key}: {n} tickets"` | `[the AssetsObject]` (the object is fetched first so the schema rule can match) | `preview={"Object": key, "Tickets": str(n)}`, table Key/Summary/Status/Type/Priority, `pii_scan_text` holds the summaries |
| `assets_export_objects` | `Export Jira Assets Objects` | as `jira_export_issues` | `[_ExportedObject(o.id, o.key, o.schema_id) for o in objects]`, where `_ExportedObject` is a private frozen dataclass in connectors/assets.py, as `_ExportedIssue` is (connectors/jira.py:274); no attribute value (ADR 0159) | as `jira_export_issues`, through `export_files.card_details` and `sample_table` |

Every review card passes `sender="Jira Assets"`. `args` is the tool's own arguments without
`reason`, as a dict. `details_text` is `"\n".join` of
`f"{key} — {label}: " + "; ".join(f"{name}: {value}" ...)` per object. No `new_info`, except on the
export, which copies Jira's `new_info`. `preview` dicts carry metadata only (§2.7). Every reviewed tool is `WIDE` in `gate._TOOL_LAYOUT`.
An AQL that matches nothing returns `[]` from `assets_search_objects` with a card, as
`jira_search_issues_with_fields` does. `assets_export_objects` writes no file and returns
`rows: 0` without a card, as `jira_export_issues` does.

Export columns are `id, key, label, type`, then every attribute name in first-seen order. A
multi-value cell is its values joined with `"; "`. CSV or JSON is picked from the `file_name`
suffix through the same `export_files` helpers `jira_export_issues` uses:
`export_files.export_file_name(file_name, "assets-objects")`, and
`refuse_org_without_staging` names `"assets_search_objects"` as the alternative tool.

### 3.5 Linked-object traversal

`related_objects(object_id, direction, depth, max_objects)`:

1. Validate: `direction` is one of `outbound`, `inbound` or `both`, otherwise
   `AssetsClientError("direction must be outbound, inbound or both")`. `depth` is clamped to
   1..3 and `max_objects` to 1..200.
2. `root = get_object(object_id)`. `seen = {root.id: root}`, `frontier = [root]`, `edges = []`,
   `truncated = False`.
3. For each level up to `depth`:
   - **outbound**: for each frontier object, for each `(attribute, refs)` in `references`, for
     each ref, add `AssetsEdge(obj.id, ref.id, attribute)`. Ids not in `seen` are fetched with
     `get_objects` (batched AQL `objectId IN (…)`).
   - **inbound**: one AQL per `ID_BATCH` frontier ids,
     `object HAVING outboundReferences(objectId IN (<ids>))`, through the search loop with a limit
     of the remaining budget. For each returned object, every reference it holds to a frontier id
     becomes `AssetsEdge(returned.id, frontier_id, attribute)`.
   - New objects go into `seen` in discovery order until `len(seen) == max_objects`. After that,
     set `truncated = True`, drop edges to unseen ids, and stop.
   - The next frontier is the newly added objects.
4. Deduplicate edges by `(source_id, target_id, attribute)`. Return
   `AssetsGraph(root.id, list(seen.values()), edges, truncated)`.

Rejected:

- **An unbounded walk.** One call could pull the whole CMDB past a single card.
- **One card per hop.** The agent would need several approvals for one question, and each card
  would show less context than the whole graph does.

### 3.6 Policy

- `TOOL_TO_OPERATION`: the five reviewed tools map to `assets.read_object`.
- `TOOL_TO_GATE`: as in the table in 3.4.
- `PERSONAL_DATA_RULE_TOOLS` gets `assets_export_objects` (ADR 0157).
- `TOOL_TO_VERB`: as in the table.
- Selectors in `policy/scopes.py`, both `scope_type="assets.schema"`, `ScopeKind.IDENTITY`, value
  source FETCHED (they read `raw_data`, as `approved_project_keys_all_results` does):
  - `approved_schema_ids`: `raw_data` is one `AssetsObject`, or a one-element list of them; it
    matches when its `schema_id` is in the rule's values;
  - `approved_schema_ids_all_results`: matches when `raw_data` is a non-empty list and every
    item's `schema_id` is in the rule's values.
  - Add both to `SCOPE_SELECTORS`, after `approved_project_keys_all_results` (scopes.py:476-479),
    with `resolves_from=ResolvesFrom.FETCHED`. Each reads `getattr(item, "schema_id", "")` from
    `ctx.raw_data`, which is one object or a list of them. Not `NEW_SCOPE_SELECTORS`:
    `propose._candidate_value` (propose.py:515) and `test_propose.py:188-190` consult only
    `SCOPE_SELECTORS`.
  - `tests/unit/policy/_v1_reference.py` gets both names in `DATA_DEPENDENT_RULES` and
    `_rule_approved_schema_ids` / `_rule_approved_schema_ids_all_results` with the same bodies;
    `test_scopes.py` gets `FIXTURES` for both (match, mismatch, empty `raw_data`, empty value) and
    its count goes from 48 to 50 (:328).
- `policy/propose.py` gets two value extractors. `_assets_schema_ids(ctx)` returns the schema id
  of `ctx.raw_data` (an object, or the first of a list). `_assets_result_schema_ids(ctx)` returns
  the sorted distinct schema ids of every item. It also gets two `PROPOSABLE_SCOPES` entries:
  - `_scope("approved_schema_ids", "assets.schema", "assets", (Verb.READ,), _assets_schema_ids, "this schema")`
  - `_scope("approved_schema_ids_all_results", "assets.schema", "assets", (Verb.SEARCH,), _assets_result_schema_ids, "this schema")`
- `policy/catalogue.py` (and add `"assets.schema"` to the hard-coded set in
  `test_catalogue.py::test_personal_data_label_only_on_the_listed_groups`, :106-108):
  `VALUE_HINTS["approved_schema_ids"] = "2, 5"`,
  `VALUE_HINTS["approved_schema_ids_all_results"] = "2, 5"` and
  `PERSONAL_DATA_GROUPS["assets.schema"] = "Exports may include personal data"`.
- `policy/resource_registry.py`:
  - `_resolve_assets_schema(client, resource_id) -> str | None`:
    `try: return client.get_schema(resource_id).name or None` / `except Exception: return None`
    (resource_registry.py imports no client module, :136-138);
  - add `GrantResourceType(connector="assets", config_key="schemas", capabilities={"read": GrantCapability("Read auto-accept", (("assets.read_object", "approved_schema_ids"), ("assets.read_object", "approved_schema_ids_all_results")))}, resolver=_resolve_assets_schema)`,
    following the Jira entry's field names and order.
- Schema, type and attribute listings are `auto` with a connector-written `_auto_audit`, on the
  precedent of `jira_list_fields` (ADR 0135).

### 3.7 Wiring

- `daemon_main.build_connectors` (which has `org_config` as a parameter, :1285) gets a block
  after Confluence's, guarded by `assets_enabled(config, org_config)` and not by
  `enabled("assets")`. Inside its `try`:
  `if not atlassian_org.get("client_id"): raise AssetsClientError("Atlassian organization config not installed")`,
  then `if not atlassian_token: raise AssetsClientError("Jira Assets is not authenticated. Use Authenticate… in PrivacyFence Settings.")`
  (as Jira's pre-checks at :1516-1519 do), then
  `AssetsClient(config=atlassian_config, token_file=_resolve_path(TOKEN_FILES["atlassian"]))`,
  `check_connection()`, `AssetsConnector(client)`, `my_email = atlassian_token.get("account_email", "")`
  and `download_mode`, `download_config` and `download_base_url` exactly as the Jira block sets
  them. It catches `(AssetsClientError, FileNotFoundError)` and records
  `failures["assets"] = _classify_connector_failure(exc)`.
- `settings_controller.py`:
  - `ALL_CONNECTORS` gets `"assets"` after `"confluence"`;
  - `_CONNECTOR_LABEL_OVERRIDES["assets"] = "Jira Assets"`;
  - `ORG_CONFIG_SERVICE["assets"] = "atlassian"`;
  - `_connectors_state` uses `assets_enabled(cfg, org_config)` for `"assets"`;
  - the authenticate dispatch (:1239-1240) sends `"assets"` to `_authenticate_atlassian`, which
    adds and discards `"assets"` in `_busy_connectors` beside jira and confluence.

  Enable and Disable already write an explicit `connectors.assets.enabled`; they need no change.
- `src/privacyfence/resources/settings.yaml.example` (:118-124): the comment's first line becomes
  `# Enable/disable individual connectors (all enabled by default except Jira Assets, which is off`
  `# unless switched on here or by the organization bundle's atlassian.assets; this section is`
  `# otherwise only needed to turn one off).`, and a commented line is added under
  `#   telegram: { enabled: false }`:
  `#   assets: { enabled: true }   # Jira Assets (read-only). Needs the Atlassian app's read:cmdb-* scopes; after switching it on, use Authenticate… once.`
  It stays commented, so `test_docs_configuration_reference.py` reads it as the documented
  `connectors.<name>.enabled`.
- Org-mode `/connect` gets no new card. The Atlassian cards request the Assets scopes when the
  bundle says `"assets": true` (3.2).
- `scripts/generate_tools_reference.py`: `"assets": "Jira Assets"` inserted immediately after
  `"confluence"` in both `CONNECTOR_TITLES` and `CONNECTOR_SHORT` (the summary order follows them,
  and `test_one_card_per_connector` compares orders). Regenerate with
  `python3 scripts/generate_tools_reference.py` (no arguments).
- `scripts/pyinstaller_common.py` gets `"privacyfence.connectors.assets"`.
- Website and README:
  - `website/connectors/jira-confluence/index.html` gets an Assets section;
  - the README Connectors table gets a `Jira Assets` row;
  - `tests/unit/test_website_connector_pages.py`
    `CONNECTORS["assets"] = ("/connectors/jira-confluence/", "atlassian-setup", "Jira Assets")`;
  - `/connectors/` gets a new card immediately after the Confluence card, `id="jira-assets"`
    (the tools-reference anchor of "Jira Assets");
  - the totals copy changes from "Thirteen connectors" to "Fourteen connectors", and
    `website/compare/mcp-gateways/index.html:62` from "thirteen connectors" to "fourteen connectors";
  - `website/_partials/other-connectors.html` does not change: its one "Jira and Confluence" link
    already points to the shared page;
  - `test_website_connectors_page.py` changes from `len(REFERENCE) == 13` to `== 14`.

Out of scope: the connector lists in `mcpb/manifest.json.tmpl:5-6`, `debian/control:27` and
`scripts/build_site.py:1119` (Grist and Peek are not there either), and a connector icon (optional
per guidelines §3; grist, peek and apps_script have none).

### 3.8 QA recorder

- `check_assets(record, manifest)` in `scripts/qa_fixture_recorder.py` follows `check_jira`. It
  reads the `assets:` block of `qa_environment.yaml`:
  - `schema_key: PFQA`;
  - `seed_object_key: ""`;
  - `seed_object_label: "PrivacyFence QA seed contract [QATEST]"`.
- It records three fixtures:
  - `list_schemas.json`, filtered to the QA schema;
  - `get_object.json`, `{"object": calls[0], "attributes": calls[1]}` from `RawCapture.calls`,
    with `client.check_connection()` called before entering `RawCapture` so the lazy workspace
    lookup is not among the calls;
  - `aql_search.json`, from `objectSchemaId = <id>` with max 5.
- `check_assets` is written and unit-tested in phase p9, but it is **not** added to
  `CONNECTOR_CHECKS` or `EXPECTED_FIXTURES` there. `TestFixturePresence` would fail until a live
  recording exists, and a recording needs the runner's Atlassian token re-granted with the Assets
  scopes, which only the maintainer can do after the code exists (`manual_after` ma3). The "Manual steps" section
  says how registration and recording happen then.

### 3.9 Documentation

- `docs/atlassian-setup.md` gets a "Jira Assets" section covering:
  - the five extra scopes and where to add them in the developer console;
  - switching Assets on in Settings and re-authenticating;
  - the `--atlassian-assets` bundle flag;
  - a troubleshooting row for "needs an extra Atlassian permission";
  - a troubleshooting row for "no Assets workspace".
- `docs/configuration-reference.md` documents `connectors.assets.enabled` (default `false`) and
  the bundle's `atlassian.assets`.
- `docs/connector-qa.md` gets "Seed: Jira Assets":
  - a schema with key `PFQA` holding types Account, Contract and Asset;
  - Contract references Account, and Asset references Contract;
  - two accounts, two contracts and three assets, all tagged `[QATEST]`;
  - the seed contract `PrivacyFence QA seed contract [QATEST]`, linked to one `PFQA` Jira issue.
- `CHANGELOG.md` under `## [Unreleased]`: "Jira Assets connector (read-only, off by default):
  browse schemas and object types, search objects with AQL, read an object with its linked
  objects and connected Jira tickets, and export objects to CSV or JSON. Switching it on asks for
  one extra Atlassian consent."

## ADRs

The next free number is 0171. If a branch merged first has taken it, renumber at retirement.

- **ADR 0171: Jira Assets is a separate read-only connector whose Atlassian scopes are requested
  only when it is switched on.** It defaults to off, with the bundle flag `atlassian.assets` as
  the default. Granted scopes are recorded from accessible-resources, and a token without them
  asks for Authenticate…. Rejected: adding the scopes to `DEFAULT_SCOPES`, an API token, and
  detecting from a 401.
- **ADR 0172: Assets schemas, object types and attribute definitions are auto-approved; reading
  objects is reviewed under `assets.read_object` and can be auto-accepted per object schema.**
  The rules are `approved_schema_ids` for READ and `approved_schema_ids_all_results` for SEARCH.
  This covers search, get, connected tickets, traversal and the export. It cites ADRs 0135, 0134
  and 0159.
- **ADR 0173: Linked objects are read in one bounded, reviewed traversal.** The bounds are depth
  3 and 200 objects. The card shows every object and edge, and the schema rule must cover every
  returned object. Rejected: an unbounded walk, and one card per hop.

## Manual steps

Only **after** implementation. No phase needs anything from the user. The step-by-step page is
[`docs/jira-assets-connector-plan-manual-steps.html`](jira-assets-connector-plan-manual-steps.html)
(published at https://claude.ai/artifact/AeRXKvbmAqCzaiV497tcRH).

1. **ma1:** Add the five Assets scopes to the Atlassian OAuth app the QA bundle uses, and to the
   app your real bundle uses.
2. **ma2:** Seed the QA site's Assets workspace and fill in the `assets:` block of
   `qa_environment.yaml` on the runner.
3. **ma3:** On the self-hosted runner, switch Assets on in the runner's settings and re-run
   `scripts/qa_authenticate_connectors.py --only atlassian` from the feature branch.
4. **ma4:** Tell the PR session that ma3 is done. That session then:
   - adds `"assets": check_assets` to `CONNECTOR_CHECKS` and
     `"assets": ("list_schemas.json", "get_object.json", "aql_search.json")` to `EXPECTED_FIXTURES`;
   - dispatches `qa-record-fixture.yml` with `connector=assets` against the PR branch, pulls,
     and reviews the fixture diff for identifying content;
   - adds `TestLiveFixtureParsing` to `tests/unit/test_assets_client.py`, replaying
     `tests/fixtures/live/assets/*.json` through the real parsers;
   - dispatches `connector-live-check.yml` and links the run in the PR.

   These are dispatches from the `ci.dispatchable` table, not manual steps. They are listed here
   because they cannot run before ma3.
5. **ma5:** In Claude, connected to your real site, ask for a contract's account and assets. You
   should see one review card that lists every linked object, and an answer that follows the
   links.

## Risks and open questions

Each item below names what a worker would observe if a brief is wrong. When that happens, the
worker stops with `status=blocked`.

- **Assets API shapes are from Atlassian's reference and community reports, not from a live
  recording.** This applies to field names in `objectschema/list`, `objecttypes/flat`, the
  `objectconnectedtickets` response, and the AQL `isLast` flag.
  - Phases test against hand-written fixtures in `tests/fixtures/assets/` that follow 3.3 exactly.
  - ma4's recording is the real check. A mismatch found then is fixed on the PR branch with the
    recorded fixture as the test input.
  - Workers must not invent extra fields.
- **Workspace discovery scope.** `read:servicedesk-request` is the best-documented scope for
  `/rest/servicedeskapi/assets/workspace` under OAuth.
  - If ma4's live check gets a 401 "scope does not match" on that call, the fix is the scope list
    in `ASSETS_SCOPES` and the docs table, followed by ma1 and ma3 again.
- **AQL `outboundReferences()` syntax on Cloud.** If the inbound query is rejected live, inbound
  traversal falls back to `GET object/{id}/referenceinfo` per frontier object. That change goes
  on the PR branch, not into a phase.
- **Granted-scope source.** If ma4 shows `read:cmdb-object:jira` missing from the accessible
  resources' `scopes`, every token would stay at `NEEDS_ASSETS_PERMISSION`. The PR-branch fix is to
  record the token response's space-separated `scope` field instead: add a `granted_scope: str = ""`
  parameter to `resolve_resource_and_save`, passed `response.get("scope", "")` from
  `authorize_interactive` and from `routes_connect.py:430`.
- **Duplicate accessible resources.** If `resolve_resource_and_save` does not see the duplicate
  per-scope-family entries that 3.2 assumes, the union is still correct: one entry gives the same
  result.
- **Stop conditions per phase:**
  - p3 stops if `tool_specs()` cannot be called with `AssetsConnector(None)`; `connector_catalog`
    requires that.
  - p5 stops if adding the two selectors needs a change to `ScopeSelector` itself.
- **Website copy.** The `/connectors/` card markup must match the regex in
  `test_website_connectors_page.py`. Copy the Peek card and change only its id, name and numbers.

## Implementation manifest

```yaml
plan_slug: jira-assets-connector
feature_branch: feature/jira-assets-connector
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/AeRXKvbmAqCzaiV497tcRH
manual_steps_source: docs/jira-assets-connector-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-oauth-app-scopes
    title: Add the five Jira Assets scopes to the Atlassian OAuth app(s) at developer.atlassian.com
    why: Without them, an authorize request that asks for Assets fails at Atlassian's consent screen, so ma3 and real use cannot work.
  - id: ma2-seed-qa-assets
    title: Seed the QA site's Assets workspace (schema PFQA with Account, Contract, Asset) and fill the runner's qa_environment.yaml assets block
    why: check_assets records against these [QATEST] objects; without them the live recording has nothing safe to record.
  - id: ma3-reauth-runner
    title: On the self-hosted runner, enable Assets and re-run qa_authenticate_connectors.py --only atlassian from feature/jira-assets-connector
    why: The runner's Atlassian token predates the Assets scopes, so every live Assets call would report "needs an extra Atlassian permission".
  - id: ma4-record-and-live-check
    title: Tell the PR session ma3 is done; it registers check_assets, dispatches qa-record-fixture.yml (connector=assets) and connector-live-check.yml, and reviews the fixtures
    why: The §2.7 live-connector row and the real API shapes can only be checked against a live site, and only after ma3.
  - id: ma5-real-site-try
    title: In Claude, ask about one real contract's account and assets and check the single review card lists every linked object
    why: Confirms the traversal and card are usable on the user's real, linked data, which no fixture covers.
verify_after_merge:
  - "python3 -m pytest tests/unit/test_assets_client.py tests/unit/connectors/test_assets_connector.py tests/unit/test_atlassian_oauth.py -q"
  - "python3 -m pytest tests/unit/policy tests/unit/test_gate.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/web/test_tool_schema_portability.py -q"
final_checks:
  - "docs/jira-assets-connector-plan.md and docs/jira-assets-connector-plan-manual-steps.html are deleted and nothing links to them (grep -rn \"jira-assets-connector-plan\" . --include=*.md --include=*.html --include=*.py finds nothing)"
  - "ADRs 0171, 0172 and 0173 (or their renumbered successors) exist, are Accepted, and are listed in docs/adr/README.md"
  - "CHANGELOG.md has the Jira Assets line under \"## [Unreleased]\" and no new version heading"
  - "Every assets_* tool is in auto_accept.TOOL_TO_GATE, and every reviewed one in TOOL_TO_OPERATION and policy/registry.TOOL_TO_VERB"
  - "The PR body lists ma1-ma5 as unchecked items with the artifact link"
phases:
  - id: p1-oauth-scopes
    title: Opt-in Assets scopes in the shared Atlassian sign-in, and record granted scopes on the token
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/atlassian_oauth.py
      - src/privacyfence/jira_client.py
      - src/privacyfence/confluence_client.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/web/routes_connect.py
      - scripts/build_org_bundle.py
      - docs/configuration-reference.md
      - tests/unit/test_atlassian_oauth.py
      - tests/unit/test_jira_client.py
      - tests/unit/test_confluence_client.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_routes_connect.py
      - tests/unit/test_build_org_bundle.py
    brief: |
      Read docs/jira-assets-connector-plan.md "Current state" and 3.2 first. Do not add an assets
      connector, Settings row or daemon block here (phase p8 does).
      1. In src/privacyfence/atlassian_oauth.py, add ASSETS_SCOPES,
         scopes_for(*, assets: bool) and assets_enabled(config, org_config) exactly as in plan
         3.2, after DEFAULT_SCOPES. Leave DEFAULT_SCOPES unchanged.
      2. In resolve_resource_and_save, add the "scopes" key to token_record (the sorted union
         over every resource whose "id" equals cloud_id), as in plan 3.2.
      3. In JiraClient._try_refresh (jira_client.py:276-283) and ConfluenceClient._try_refresh
         (confluence_client.py, the save_token_file dict), add
         "scopes": self._config.get("scopes", []) to the saved dict.
      4. Pass scopes=scopes_for(assets=assets_enabled(cfg, org_config)) at the three authorize
         call sites, importing `from .atlassian_oauth import assets_enabled, scopes_for`
         (`..atlassian_oauth` in web/), exactly as plan 3.2's callers bullet says:
         - settings_controller._authenticate_atlassian (:1376-1381): cfg = self._load_config();
         - daemon_main.run_atlassian_oauth: the new signature
           `(org_config, config: dict[str, Any] | None = None)`, using `config or {}`; main()
           (:2400) passes `config`;
         - web/routes_connect._build_authorize_url (:375-380): assets_enabled({}, org_config).
         atlassian_authorize_interactive is an alias of authorize_interactive, which already
         forwards `scopes`; do not change its signature.
      5. In scripts/build_org_bundle.py, add --atlassian-assets as in plan 3.2 (the argument
         group, the "assets": true key, and the SystemExit text). In
         docs/configuration-reference.md's bundle-options table, after the
         --atlassian-client-id row (:247), add:
         `--atlassian-assets` | off | `atlassian.assets` | Request the Jira Assets scopes at
         Atlassian sign-in; needs `--atlassian-client-id`/`--atlassian-client-secret`.
      6. Tests:
         - tests/unit/test_atlassian_oauth.py: scopes_for both ways; assets_enabled with explicit
           true, explicit false, bundle flag only, and neither; the token record's scopes union
           over two resources with the same id and one with a different id; build_authorize_url
           with scopes_for(assets=True) contains "read:cmdb-object:jira".
         - test_jira_client.py and test_confluence_client.py: a refresh keeps "scopes". The
           exact-dict assertions at test_atlassian_oauth.py:309-312, test_jira_client.py:355-357
           and test_confluence_client.py:300-302 gain the "scopes" key ([] for the refresh
           tests, the resources' scope union for authorize_interactive).
         - test_daemon_main.py:3333: the lambda becomes `lambda org_config, *a: calls.append(1) or 0`;
           the one-argument calls at :2480-2512 keep working through the default.
         - test_settings_controller.py, test_daemon_main.py and web/test_routes_connect.py: the
           authorize URL or call carries the Assets scopes only when assets_enabled is true.
         - tests/unit/test_build_org_bundle.py: the flag sets "assets": true, and the flag without client id/secret exits with the
           exact message.
      No changelog line in this phase (p10 writes it).
    acceptance:
      - "python3 -m pytest tests/unit/test_atlassian_oauth.py tests/unit/test_jira_client.py tests/unit/test_confluence_client.py -q passes"
      - "python3 -m pytest tests/unit/test_settings_controller.py tests/unit/test_daemon_main.py tests/unit/web/test_routes_connect.py tests/unit/test_build_org_bundle.py tests/unit/test_docs_configuration_reference.py -q passes"
      - "grep -n \"def assets_enabled\" src/privacyfence/atlassian_oauth.py matches"
      - "grep -c '\"scopes\": self._config.get(\"scopes\", \\[\\])' src/privacyfence/jira_client.py src/privacyfence/confluence_client.py shows 1 for each file"
      - "ruff check . passes, and python3 -m mypy src/privacyfence passes"

  - id: p2-assets-client
    title: AssetsClient with schema, type, attribute, object, AQL, connected-ticket and traversal reads
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/assets_client.py
      - tests/unit/test_assets_client.py
      - tests/fixtures/assets/*.json
    brief: |
      Read plan sections 3.3 and 3.5. They are the full spec: names, constants, dataclasses,
      parsing rules and error texts. Copy JiraClient's constructor, _try_refresh and _request
      shape (jira_client.py:215-294), but use plain requests (no atlassian-python-api).
      `scopes` comes from config; do not import anything p1 adds. Hard-code
      ASSETS_REQUIRED_SCOPE = "read:cmdb-object:jira" and NEEDS_ASSETS_PERMISSION as module
      constants in assets_client.py. Phase p8 does not move them.
      1. Write the hand-made fixtures under tests/fixtures/assets/:
         workspace.json, objectschema_list_page1.json and _page2.json (isLast false, then true),
         objecttypes_flat.json, objecttype_attributes_contract.json, object_contract.json,
         object_contract_attributes.json, aql_page.json (with a response-level
         objectTypeAttributes list), connected_tickets.json. Use the field names in plan 3.3
         only. The content is synthetic: "[QATEST]" labels, example.com, no real names.
      2. Write src/privacyfence/assets_client.py: the module docstring (two sentences: what it
         reads, that it never writes); the constants; AssetsClientError; the dataclasses;
         _parse_object; the methods from the 3.3 table; related_objects per 3.5.
      3. Write tests/unit/test_assets_client.py with requests mocked through monkeypatch, as
         test_jira_client.py does. Cover:
         - constructor errors (no token; no scope -> NEEDS_ASSETS_PERMISSION text);
         - workspace discovery (found, none, and cached: one HTTP call for two reads);
         - 401 then a successful refresh and retry, with the saved token keeping scopes;
         - paging stops at isLast, at an empty page, at max_results and at MAX_PAGES;
         - search clamps max_results to 100;
         - every attribute-value kind in _parse_object, including user email masking and each
           of the three name-lookup fallbacks;
         - id and AQL validation messages;
         - export over 10,000 raises the exact text;
         - related_objects for outbound depth 1, inbound depth 1, both at depth 2, truncation at
           max_objects, edge dedupe, and a bad direction;
         - connected_tickets parsing;
         - _request returns the decoded JSON, so RawCapture can record it.
      4. Coverage of assets_client.py must be 100% branch coverage (the coverage floor).
    acceptance:
      - "python3 -m pytest tests/unit/test_assets_client.py -q passes"
      - "python3 -m pytest tests/unit/test_assets_client.py -q --cov=privacyfence.assets_client --cov-branch --cov-report=term-missing reports 100%"
      - "grep -n \"class AssetsClientError(Exception)\" src/privacyfence/assets_client.py matches"
      - "ruff check . and python3 -m mypy src/privacyfence pass"

  - id: p3-connector-schema-tools
    title: AssetsConnector with the three auto-approved schema tools, registries, tools reference, README and website
    depends_on: [p2-assets-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/assets.py
      - src/privacyfence/auto_accept.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - scripts/pyinstaller_common.py
      - README.md
      - docs/README.md
      - website/connectors/index.html
      - website/connectors/jira-confluence/index.html
      - website/how-it-works/index.html
      - website/compare/mcp-gateways/index.html
      - tests/unit/connectors/test_assets_connector.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_website_connectors_page.py
    brief: |
      Read plan sections 3.1, 3.4 (the three auto rows and their summaries) and 3.7 (the
      website, README and generator bullets). The pattern commit is Peek's 99b771a8 (git show
      --stat 99b771a8); copy its file set and shapes.
      1. Create src/privacyfence/connectors/assets.py: class AssetsConnector with name "assets",
         `def __init__(self, client: AssetsClient) -> None` storing it in `self._assets`, a
         `@property def client(self) -> AssetsClient: return self._assets` (Settings resolves
         schema names through it, settings_controller.py:802-807), and my_email, download_mode,
         download_config and download_base_url attributes, all as in JiraConnector
         (connectors/jira.py:317-327); tool_specs() returning only assets_list_schemas,
         assets_list_object_types and assets_list_attributes, and call() dispatching them.
         tool_specs() must work on AssetsConnector(None) (connector_catalog.py:39 passes None
         under a type: ignore). STOP with status=blocked if connector_catalog needs anything more.
      2. Implement the three tools as Jira's auto tools are (_fetch with AssetsClientError ->
         RuntimeError; _auto_audit; see connectors/jira.py:649-694 and :1131-1157). Return the
         dict shapes from plan 3.4. assets_list_attributes leaves out is_system rows. Clamp
         max_results to 1..100.
      3. Add the three tools as "auto" to auto_accept.TOOL_TO_GATE, next to the Jira rows.
      4. In scripts/generate_tools_reference.py, insert "assets": "Jira Assets" immediately
         after "confluence" in both CONNECTOR_TITLES and CONNECTOR_SHORT. Run
         `python3 scripts/generate_tools_reference.py` (no arguments) and commit the output.
      5. Add "privacyfence.connectors.assets" to scripts/pyinstaller_common.py next to the
         confluence entry.
      6. README.md Connectors table: add a "Jira Assets" row after Confluence, worded like it.
         docs/README.md: add the connector next to Jira/Confluence.
      7. Website:
         - add a /connectors/ card for Jira Assets immediately after the Confluence card,
           copied from the Peek card, with id="jira-assets" and the counts from the
           regenerated tools-reference summary row;
         - update every total ("N tools", "N connector tools"), change "Thirteen connectors"
           to "Fourteen connectors", and change "thirteen connectors" to "fourteen connectors"
           in website/compare/mcp-gateways/index.html:62;
         - add a short "Jira Assets" section to website/connectors/jira-confluence/index.html
           (read-only, off by default, schema reads auto, object reads reviewed);
         - leave website/_partials/other-connectors.html unchanged.
      8. Tests:
         - tests/unit/test_website_connector_pages.py: CONNECTORS["assets"] as in plan 3.7.
         - tests/unit/test_website_connectors_page.py: 13 -> 14 and "Fourteen connectors".
         - test_readme_manifest_alignment.py: add AssetsConnector to CONNECTOR_CLASSES.
         - New tests/unit/connectors/test_assets_connector.py: TestDispatch (an unknown tool
           raises ValueError); one test per auto tool (shape, clamp, audit entry with
           decision="auto_accepted", and gated_call patched to fail so it is never called);
           RuntimeError on AssetsClientError; TestToolDefinitions with the p3 rows of
           ASSETS_SIBLINGS from plan 3.4, as JIRA_SIBLINGS does
           (test_jira_connector.py:113-127); assert_all_tools_leave_an_audit_trail.
    acceptance:
      - "python3 -m pytest tests/unit/connectors/test_assets_connector.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes"
      - "python3 -m pytest tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_docs_tools_reference.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/web/test_tool_schema_portability.py -q passes"
      - "python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/test_connector_tool_annotations.py -q passes"
      - "grep -n \"Fourteen connectors\" website/connectors/index.html matches"
      - "ruff check . passes"

  - id: p4-object-reads
    title: Reviewed assets_search_objects and assets_get_object
    depends_on: [p3-connector-schema-tools]
    complexity: M
    touches:
      - src/privacyfence/connectors/assets.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - tests/unit/connectors/test_assets_connector.py
      - tests/unit/test_gate.py
      - tests/unit/policy/test_registry.py
    brief: |
      Read plan 3.4 (the two rows, the summaries and the review-card table) and 3.6 (the
      operation and verb). No auto-accept rule exists yet (p5 adds it), so cards show no "always
      allow" button in this phase. That is expected.
      1. Add the two ToolSpecs and their dispatch to connectors/assets.py. Implement them with
         gate.gated_call(gate="review", ...) as _search_issues_with_fields
         (connectors/jira.py:700-729) and _get_issue (:802-904) do: preview, preview_tables,
         table_only, pii_scan_text, my_email and args as plan 3.4 says. raw_data is the parsed
         object(s); filtered_data is the to_dict() output.
      2. auto_accept: TOOL_TO_OPERATION gets both -> "assets.read_object"; TOOL_TO_GATE gets
         both -> "review".
      3. policy/registry.TOOL_TO_VERB: assets_get_object -> Verb.READ,
         assets_search_objects -> Verb.SEARCH.
      4. gate._TOOL_LAYOUT: both WIDE.
      5. Add the p4 rows of ASSETS_SIBLINGS (plan 3.4). Run
         `python3 scripts/generate_tools_reference.py` and
         `python3 scripts/generate_always_allow_reference.py`, commit both outputs, and update
         the /connectors/ card counts and totals.
      6. Tests in test_assets_connector.py, as test_jira_connector.py's TestGetIssue and
         TestSearchIssuesWithFields do (§2.8: gate path, whether the client was called, the
         result, the decision, the audit entry):
         - approve and deny for each tool;
         - the preview dict holds only metadata keys;
         - the PII scan text holds the attribute values;
         - an empty search still shows a card;
         - AssetsClientError -> RuntimeError;
         - TestFieldCompleteness (as in test_jira_connector.py:1582): run
           tests/fixtures/assets/object_contract.json + object_contract_attributes.json through
           the real _parse_object and the real assets_get_object card building, checked with
           assert_no_placeholder_fields.
         Add the two tools to any parametrised list in test_gate.py or
         policy/test_registry.py that enumerates reviewed tools and fails without them.
    acceptance:
      - "python3 -m pytest tests/unit/connectors/test_assets_connector.py tests/unit/test_gate.py tests/unit/policy/test_registry.py -q passes"
      - "python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_website_connectors_page.py -q passes"
      - "grep -n '\"assets_get_object\": *\"assets.read_object\"' src/privacyfence/auto_accept.py matches"
      - "ruff check . passes"

  - id: p5-schema-rules
    title: Per-schema auto-accept rules (approved_schema_ids, approved_schema_ids_all_results)
    depends_on: [p4-object-reads]
    complexity: M
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/resource_registry.py
      - docs/always-allow-rules-reference.md
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/_v1_reference.py
      - tests/unit/policy/test_propose.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_resource_registry.py
      - tests/unit/test_generate_always_allow_reference.py
      - tests/unit/connectors/test_assets_connector.py
    brief: |
      Read plan 3.6. Copy the Jira project entries: scopes.py:273-305 and :472-487,
      propose.py:168-191 and :336-343, catalogue.py:89-113, and resource_registry.py:75 and
      :224-237.
      1. scopes.py: add the two selectors to SCOPE_SELECTORS after
         approved_project_keys_all_results (:476-479), with resolves_from=ResolvesFrom.FETCHED,
         reading .schema_id from raw_data, as plan 3.6 says. In tests/unit/policy/_v1_reference.py
         add both names to DATA_DEPENDENT_RULES and add _rule_approved_schema_ids /
         _rule_approved_schema_ids_all_results with the same bodies (precedent: git show
         8facf006). STOP with status=blocked if this needs a change to ScopeSelector itself.
      2. propose.py: add _assets_schema_ids, _assets_result_schema_ids and the two
         PROPOSABLE_SCOPES entries, verbatim from plan 3.6.
      3. catalogue.py: add the VALUE_HINTS and PERSONAL_DATA_GROUPS entries from plan 3.6.
      4. resource_registry.py: add _resolve_assets_schema and the GrantResourceType from plan
         3.6.
      5. Run `python3 scripts/generate_always_allow_reference.py` and commit the output.
      6. Tests:
         - test_scopes.py: FIXTURES entries for both (match, mismatch, empty raw_data, empty
           value), and the count at :328 goes from 48 to 50;
         - propose: the button label "this schema" appears for both tools, with the right
           values;
         - test_catalogue.py: add "assets.schema" to the expected set in
           test_personal_data_label_only_on_the_listed_groups (:106-108);
         - resource name resolution, including None when get_schema raises;
         - in test_assets_connector.py, an end-to-end auto-accept: a stored approved_schema_ids
           rule for schema "2" lets assets_get_object of a schema-2 object through without a
           card, and a schema-3 object still shows one; the same for
           approved_schema_ids_all_results with a mixed-schema search.
    acceptance:
      - "python3 -m pytest tests/unit/policy -q passes"
      - "python3 -m pytest tests/unit/connectors/test_assets_connector.py tests/unit/test_generate_always_allow_reference.py -q passes"
      - "grep -n '\"approved_schema_ids_all_results\"' src/privacyfence/policy/propose.py matches"
      - "ruff check . and python3 -m mypy src/privacyfence pass"

  - id: p6-related-and-tickets
    title: Reviewed assets_get_related_objects and assets_get_connected_tickets
    depends_on: [p5-schema-rules]
    complexity: M
    touches:
      - src/privacyfence/connectors/assets.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - tests/unit/connectors/test_assets_connector.py
    brief: |
      Read plan 3.4 (the two rows and their card rows) and 3.5.
      1. Add both ToolSpecs, their dispatch and their gated implementations to
         connectors/assets.py:
         - assets_get_related_objects calls client.related_objects and returns
           {root_id, objects: [to_dict()], edges: [{"from", "to", "attribute"}], truncated};
         - assets_get_connected_tickets calls client.get_object first, then
           client.connected_tickets, and passes [object] as raw_data;
         - pii_scan_text of both is as plan 3.4's card table says;
         - the description of assets_get_related_objects states the bounds (depth 1-3, up to
           200 objects) and that truncated true means the limit was hit.
      2. auto_accept: both -> "assets.read_object" and "review". registry: related -> SEARCH,
         connected tickets -> READ. gate._TOOL_LAYOUT: both WIDE.
      3. Add the p6 rows of ASSETS_SIBLINGS (plan 3.4). Run both generators
         (generate_tools_reference.py, generate_always_allow_reference.py), commit the output,
         and update the /connectors/ card counts and totals.
      4. Tests:
         - approve and deny for each tool;
         - the card has one table per object type plus the edges table;
         - truncated passes through;
         - the schema rule (from p5) auto-accepts the traversal only when every returned
           object's schema is approved;
         - the connected-tickets card shows the ticket table and auto-accepts under
           approved_schema_ids;
         - the preview holds metadata only;
         - AssetsClientError -> RuntimeError.
    acceptance:
      - "python3 -m pytest tests/unit/connectors/test_assets_connector.py tests/unit/test_systemic_gate_invariants.py tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_website_connectors_page.py -q passes"
      - "grep -n '\"assets_get_related_objects\": *\"review\"' src/privacyfence/auto_accept.py matches"
      - "ruff check . passes"

  - id: p7-export
    title: assets_export_objects to CSV or JSON after one review
    depends_on: [p6-related-and-tickets]
    complexity: M
    touches:
      - src/privacyfence/connectors/assets.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - tests/unit/connectors/test_assets_connector.py
      - tests/unit/test_gate.py
      - tests/unit/policy/test_propose.py
    brief: |
      Read plan 3.4 (the export row, its card row and the export-columns paragraph) and ADR
      0159. The pattern is jira_export_issues: its ToolSpec at connectors/jira.py:399-421, its
      implementation (grep "_export_issues" in connectors/jira.py) and commit be27441f
      (git show --stat be27441f).
      1. Add the ToolSpec, worded like Jira's, with params aql, destination_dir (reuse the
         _DESTINATION_DIR text pattern) and file_name (default via
         export_files.export_file_name(file_name, "assets-objects")). Copy the implementation:
         - client.export_objects(aql);
         - an empty result returns rows 0 with no card and no file;
         - otherwise build the columns and rows per plan 3.4, then gated_call with
           raw_data=[_ExportedObject(o.id, o.key, o.schema_id) for o in objects] (a private
           frozen dataclass, as _ExportedIssue at connectors/jira.py:274), export_files.card_details / sample_table /
           scan_text, and export_files.render;
         - org delivery exactly as Jira's (refuse_org_without_staging naming
           "assets_search_objects" as the alternative, saved_to, export_summary).
      2. auto_accept: "assets.read_object", "review", and add to PERSONAL_DATA_RULE_TOOLS.
         registry: Verb.SEARCH. gate._TOOL_LAYOUT: WIDE.
      3. Add the p7 rows of ASSETS_SIBLINGS (plan 3.4). Run both generators, commit the
         output, and update the card counts and totals.
      4. Tests, copied from Jira's TestExportIssues:
         - CSV and JSON content;
         - multi-value cells joined with "; ";
         - the empty result (no card, no file);
         - the over-10,000 error reaches the agent as RuntimeError;
         - raw_data holds no attribute value;
         - the schema all-results rule auto-accepts;
         - a rule with allow_personal_data is needed when PII is found;
         - org-mode delivery.
         Extend test_gate.py / policy/test_propose.py only where a parametrised list of export
         tools or PERSONAL_DATA_RULE_TOOLS fails without it.
    acceptance:
      - "python3 -m pytest tests/unit/connectors/test_assets_connector.py tests/unit/test_gate.py tests/unit/policy/test_propose.py -q passes"
      - "python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_website_connectors_page.py -q passes"
      - "grep -n '\"assets_export_objects\"' src/privacyfence/auto_accept.py shows the TOOL_TO_OPERATION, TOOL_TO_GATE and PERSONAL_DATA_RULE_TOOLS lines"
      - "ruff check . passes"

  - id: p8-daemon-and-settings
    title: Build the Assets connector in the daemon and show it in Settings, off by default
    depends_on: [p1-oauth-scopes, p3-connector-schema-tools]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/resources/settings.yaml.example
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
    brief: |
      Read plan 3.7's daemon_main and settings_controller bullets. This phase runs in parallel
      with p4-p7 and must not touch connectors/assets.py.
      1. daemon_main.build_connectors: add the Assets block after Confluence's (:1537-1558)
         exactly as plan 3.7's daemon_main bullet spells it out (guard, the two pre-check
         raises, construction, my_email, download_*, the except clause). Import AssetsClient,
         AssetsClientError and AssetsConnector beside Jira's imports, and assets_enabled from
         .atlassian_oauth (p1 already imports it there).
      2. settings_controller.py: make the ALL_CONNECTORS, _CONNECTOR_LABEL_OVERRIDES,
         ORG_CONFIG_SERVICE, _connectors_state, authenticate-dispatch and
         _authenticate_atlassian busy-set changes from plan 3.7. Enable/Disable need no change.
      3. resources/settings.yaml.example: make the comment change and add the commented
         assets line exactly as plan 3.7 says.
      4. Tests:
         - daemon: assets is not built by default; it is built when connectors.assets.enabled
           is true or the bundle flag is set; a token without the scope records failures["assets"]
           == "not_authenticated"; explicit enabled false beats the bundle flag.
         - settings: the Jira Assets row is present with label "Jira Assets", disabled by
           default; toggling it on writes enabled: true; Authenticate on "assets" runs the
           Atlassian flow with the Assets scopes and marks jira, confluence and assets busy.
         Update any test that asserts the full ALL_CONNECTORS list or a connector-row count.
    acceptance:
      - "python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/test_docs_configuration_reference.py -q passes"
      - "grep -n '\"assets\"' src/privacyfence/settings_controller.py shows the ALL_CONNECTORS, label override and ORG_CONFIG_SERVICE entries"
      - "ruff check . and python3 -m mypy src/privacyfence pass"

  - id: p9-qa-recorder
    title: check_assets in the QA fixture recorder (written and unit-tested, not yet registered)
    depends_on: [p7-export, p8-daemon-and-settings]
    complexity: S
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/qa_environment.yaml.example
      - docs/connector-qa.md
    brief: |
      Read plan 3.8 and 3.9's connector-qa bullet. Copy check_jira (scripts/qa_fixture_recorder.py:957
      onward) and its _build_jira_client helper.
      1. Add _build_assets_client() and check_assets(record, manifest) recording the three
         fixtures named in plan 3.8 with RawCapture (call client.check_connection() before
         entering it; get_object.json is {"object": calls[0], "attributes": calls[1]}), through redact() and
         deidentify_structural_fields() as check_jira does. Filter list_schemas to the QA
         schema key. Do NOT add "assets" to CONNECTOR_CHECKS or EXPECTED_FIXTURES; put a
         one-line comment above check_assets saying it is registered once its live fixtures
         are recorded.
      2. tests/fixtures/qa_environment.yaml.example: add the assets: block from plan 3.8, with
         a "docs/connector-qa.md, "Seed: Jira Assets"" comment as the other blocks have.
      3. docs/connector-qa.md: add "### Seed: Jira Assets" after "Seed: Jira", with the
         checklist from plan 3.9 and an optional-rule table row (approved_schema_ids,
         [<PFQA schema id>], assets.read_object).
      4. tests/unit/test_qa_fixture_recorder.py: test check_assets against a fake
         AssetsClient (as the existing check_* tests fake their clients): ok and not-found
         paths, and that record mode filters the schema list to the QA key.
    acceptance:
      - "python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes"
      - "grep -n \"def check_assets\" scripts/qa_fixture_recorder.py matches"
      - "python3 -c \"import sys; sys.path.insert(0, 'scripts'); import qa_fixture_recorder as r; assert 'assets' not in r.CONNECTOR_CHECKS\" exits 0"
      - "ruff check . passes"

  - id: p10-retire-plan
    title: ADRs, setup and configuration docs, changelog, delete the plan
    depends_on: [p9-qa-recorder]
    complexity: S
    touches:
      - docs/adr/0171-*.md
      - docs/adr/0172-*.md
      - docs/adr/0173-*.md
      - docs/adr/README.md
      - docs/atlassian-setup.md
      - docs/configuration-reference.md
      - CHANGELOG.md
      - docs/jira-assets-connector-plan.md
      - docs/jira-assets-connector-plan-manual-steps.html
    brief: |
      Read plan 3.2, 3.5, 3.6, 3.9 and the "ADRs" section, and docs/adr/README.md (template, index
      format, and the rule that ADRs link to code and commits, never to a plan document).
      1. ls docs/adr. If 0171-0173 are free, use them. Otherwise take the next free numbers and
         use them consistently.
      2. Write the three ADRs from the plan's "ADRs" section, using the README's template:
         - Status "Accepted — <today's date>. Implemented: <files>";
         - Context, Decision, Alternatives considered (the "Rejected" lists in 3.2 and 3.5),
           Consequences, Verification (the test classes added by p1-p8), and Related (ADRs 0115,
           0116, 0134, 0135, 0157, 0159).
         Add them to the index in docs/adr/README.md.
      3. docs/atlassian-setup.md: add the "Jira Assets" section from plan 3.9, including the
         scope table rows and the two troubleshooting rows with their exact error texts.
         docs/configuration-reference.md: document connectors.assets.enabled (default false)
         and the bundle's atlassian.assets.
      4. CHANGELOG.md: add the line from plan 3.9 under "## [Unreleased]". Open no version
         heading.
      5. git rm docs/jira-assets-connector-plan.md docs/jira-assets-connector-plan-manual-steps.html.
         Then grep -rn "jira-assets-connector-plan" . (excluding .git) must find nothing.
    acceptance:
      - "ls docs/adr | grep -c -E \"^017[1-9]-\" is at least 3, and each new file contains \"Accepted\""
      - "grep -n \"Jira Assets\" CHANGELOG.md matches a line above the first \"## [\" version heading after \"## [Unreleased]\""
      - "test ! -e docs/jira-assets-connector-plan.md"
      - "grep -rn \"jira-assets-connector-plan\" --include=*.md --include=*.html --include=*.py . finds nothing"
      - "python3 -m pytest tests/unit -q passes"
```
