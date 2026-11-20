# Deviations: drive-folder-tree-rule

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-scope-and-grants

- worker: Worker reported no deviations; it could not run tld, so the orchestrator ran the roles check (none required).
- review: Diff review: inside the brief and touches; ListingGrants stores an emptied entry map until the next read prunes it, which is harmless.

## p2-connector-listing-grants

- worker: Worker reported no deviations; tld was unavailable to it, so the orchestrator ran the roles check (none required).
- review: Diff review: matches plan section 3.4, inside touches, no protected paths.

## p3-docs-adr-retire

- pending

## Final review

- pending

## Finishing

- pending
