# ADR 0189: Assets schemas, object types and attribute definitions are auto-approved; reading objects is reviewed under `assets.read_object` and can be auto-accepted per object schema

## Status

Accepted — 2026-10-13. Implemented: `src/privacyfence/connectors/assets.py`,
`src/privacyfence/policy/scopes.py`, `src/privacyfence/policy/propose.py`,
`src/privacyfence/policy/catalogue.py`, `src/privacyfence/policy/resource_registry.py`.

## Context

The Assets structure (schemas, object types, attribute definitions) describes what the CMDB holds
but not the objects themselves. Objects carry the data: contract terms, account names, owners. The
connector needs a gate that matches how Jira handles its own structure and issue reads.

## Decision

- **Structure is auto-approved.** `assets_list_schemas`, `assets_list_object_types` and
  `assets_list_attributes` write a connector `_auto_audit`, on the precedent of
  `jira_list_fields` (ADR 0135).
- **Every object read is reviewed under one operation, `assets.read_object`:**
  `assets_search_objects`, `assets_get_object`, `assets_get_related_objects`,
  `assets_get_connected_tickets` and `assets_export_objects`.
- **Two scope predicates on the new `assets.schema` scope type**, matching on the schema id of
  the objects the call returned: `approved_schema_ids` for READ, and
  `approved_schema_ids_all_results` for SEARCH, which matches only when every returned object is in
  an approved schema. The export reaches its rule through the search verb and holds no attribute
  value in `raw_data` (ADR 0159), and `assets_export_objects` is in
  `auto_accept.PERSONAL_DATA_RULE_TOOLS`, so a scoped token with `allow_personal_data`
  ([0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md) on `main`) lets an export
  through when personal data is found, and no other call does.
- **A grant type `assets`** with config key `schemas` lets a user name schemas by id; the
  resolver shows the schema name.

## Alternatives considered

- **Review the structure listings too** — they hold no object data, and a card per listing would
  teach users to click through.
- **One rule on the object type** — objects are fetched by id and by AQL across types, so the
  schema is the unit every returned object carries.

## Consequences

- A rule naming a schema auto-accepts reads and AQL searches only inside it; a search that returns
  an object from any other schema still asks.

## Verification

`tests/unit/connectors/test_assets_connector.py` (`TestSchemaAutoAccept`, `TestExportAutoAccept`,
`TestAuditTrail`, `TestToolDefinitions`), `tests/unit/policy/test_scopes.py`,
`tests/unit/policy/test_propose.py`, `tests/unit/policy/test_catalogue.py`,
`tests/unit/policy/test_resource_registry.py`.

## Related

ADRs [0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md),
[0116](0116-list-tools-page-inside-the-client-and-keep-their-return-shape.md),
[0134](0134-extra-jira-fields-go-through-a-reviewed-search.md),
[0135](0135-the-jira-field-list-is-auto-approved.md),
[0157](0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md),
[0159](0159-jira-export-issues-is-covered-by-the-project-search-rule.md).
