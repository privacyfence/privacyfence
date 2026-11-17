# ADR 0324: Apps Script can create an empty project without a card

## Status

Accepted — 2026-11-17. Implemented: src/privacyfence/apps_script_client.py (create_project), src/privacyfence/connectors/apps_script.py, src/privacyfence/auto_accept.py.
Amends [ADR 0075](0075-apps-script-gets-no-run-tool.md).

## Context

The Apps Script connector exposed exactly four tools (ADR 0075): list, read and write the source of
existing standalone projects, and read the result of a run. It could not create a project, so the
user had to make an empty one in script.google.com before an AI client could write any code into it.

Creating something empty that discloses nothing already has a precedent: `drive_create_blank_file`
and `drive_sheets_create` are `auto`. Google's `projects.create` needs only the `script.projects`
scope, which the connector already holds.

## Decision

- Add `apps_script_create_project(title)`, which calls `projects.create` with only a title and
  returns the new project in the same shape as `apps_script_list_projects`.
- Its gate is `auto` (a `TOOL_TO_GATE` row and nothing else), and every call writes an audit entry.
- Standalone projects only: the client never sends a `parentId`.
- No new OAuth scope and no re-authorization; `SCOPES` is unchanged.
- Writing code into the project stays the popup-gated `apps_script_write_content`.
- There is still no tool that runs a script (ADR 0075).

## Alternatives considered

- **A popup-gated create that also takes the initial files.** It merges two decisions into one card
  and duplicates the card of `apps_script_write_content`. Keeping create empty and automatic means
  the card that matters, the code, is shown once and in one place.
- **A popup-gated empty create.** An empty project discloses nothing and runs nothing. Every other
  empty create (`drive_create_blank_file`, `drive_sheets_create`) is `auto`, and a card for it would
  teach people to click through cards.
- **A `parent_id` parameter for container-bound scripts** (attached to a Doc, Sheet, Form or
  Slides). Binding code to someone's document is a bigger change: `apps_script_list_projects`
  cannot list such scripts, and the project would sit inside a file the user did not pick. Left out;
  a later issue can propose it.

## Consequences

- An AI client can start a new script end to end, except running it.
- A project is created in the user's Drive with no card; it is visible in the audit log.
- An unwanted project has to be deleted by the user in Drive.
- ADR 0075's "exactly four tools" is now five; the rest of 0075 (no run tool) stands.

## Verification

- The `apps_script_create_project` row in `TOOL_TO_GATE` (`src/privacyfence/auto_accept.py`).
- The tests added in `tests/unit/test_apps_script_client.py` (`TestCreateProject`) and
  `tests/unit/connectors/test_apps_script_connector.py` (`TestCreateProject`).
- The counts in `tests/unit/test_website_connectors_page.py`.

## Related

[#1151](https://github.com/privacyfence/privacyfence/issues/1151), [ADR 0075](0075-apps-script-gets-no-run-tool.md), [ADR 0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md).
