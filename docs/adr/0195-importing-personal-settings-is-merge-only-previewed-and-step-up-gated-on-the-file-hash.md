# ADR 0195: Importing personal settings is merge-only, previewed, and step-up gated on the file hash

## Status

Accepted — 2026-10-16. Implemented: `src/privacyfence/personal_settings.py` (`plan_import`,
`apply_import`), `src/privacyfence/web/personal_settings_routes.py`,
`src/privacyfence/web/personal_settings_shim.py`.

## Context

The file from [ADR 0194](0194-personal-settings-are-exported-as-a-file-of-auto-accept-rows-and-the-step-up-level.md)
comes from outside and is unsigned, so a stale or hostile file must not be able to do more than the
person could do by hand on the Auto-accept page. Importing rules widens what is auto-accepted,
which is a sensitive settings write ([ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md)),
and org-mode settings go through a shared action dispatcher
([ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md)). A stored rule is one per
scope ([ADR 0172](0172-one-stored-auto-accept-rule-per-scope.md)).

## Decision

- **Import only adds.** It merges rows through the same path as a rule added in Settings. It never
  removes a rule, never narrows one and never lowers a step-up level (the stored level can only
  widen the configured minimum).
- **The person sees a preview first.** Every row is listed as *add*, *extend* (adds tools to a rule
  already there), *present* or *skip with a reason*, plus the step-up line and warnings. The
  preview changes nothing.
- **The plan is computed by simulating the merge**, never from the ids in the file. At confirmation
  the file is uploaded again and planned against the rows as they are then, so a stale preview
  cannot be applied.
- **The confirmation is step-up gated on the SHA-256 of the file that was previewed.** When step-up
  is in force a passkey assertion made for one file is refused for another.
- **Limits on the file:** at most 256 KiB and 500 rows, bounded value shapes, and the preview is
  rendered as text only.
- **Import is per person.** It acts on the signed-in person's own settings; an administrator cannot
  import for someone else.

## Alternatives considered

- **Replace mode ("make my rules equal the file")** — it can delete or narrow rules, which makes a
  hostile or stale file more powerful than any single Settings action.
- **Keeping the uploaded file on the server between preview and confirm** — a stateful two-request
  flow; re-uploading and re-planning is simpler and cannot apply a stale preview.
- **Trusting the preview's own result at confirm time** — the rows may have changed in between.

## Consequences

- A hostile file can only add rules, and only after the person has read the preview and, where
  step-up is in force, approved it with a passkey.
- Custom rules (rows with no catalogue scope) are neither exported nor imported. A custom row
  carries its own predicate and operations, nothing in Settings can create one, and a hostile file
  could use one (for example predicate `always_allow` with send and delete operations) to
  auto-accept far more than its preview sentence says. Scoped rows are safe because compiling them
  intersects their operations with the catalogue.
- Rules whose tools this version does not know, or that it cannot compile, are skipped with a
  reason rather than half-applied.
- Rule ids in a file are ignored; the id shown in the preview is the one the row will really have.

## Verification

`tests/unit/test_personal_settings.py`, `tests/unit/web/test_personal_settings_routes.py`,
`tests/unit/web/test_routes_org_settings.py::TestPersonalSettingsRoutes`,
`tests/integration/test_settings_personal_transfer_browser.py`.

## Related

[ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md),
[ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md),
[ADR 0172](0172-one-stored-auto-accept-rule-per-scope.md),
[ADR 0194](0194-personal-settings-are-exported-as-a-file-of-auto-accept-rows-and-the-step-up-level.md).
