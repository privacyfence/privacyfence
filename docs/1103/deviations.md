# Deviations: upstream-identity-claim

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-claim-keys

- worker: Worker interpreted: revoking a pending key un-retires the signing key waiting to hand over; revoke refuses when no other key can sign now; the CLI uses the default 24 h overlap until phase 9/10 wires claim.key_overlap_hours.
- worker: Worker reported blocked only because the tld plugin is absent in its container; no roles are required for its files, so I accepted the head after scope check and the unit suite.

## p2-membership-store

- worker: Worker interpreted: parked counts new (email, group, restricted audience) triples; bootstrap additions are left out of the 24 h cap; an unmapped group is dropped like a removal; approve/decline return 0 for the all audience.
- worker: Worker reported blocked because the tld plugin is absent in its container; no roles are required for its files, so I accepted the head after my own review and the unit suite.

## p3-audit-fields

- pending

## p4-client-id-context

- pending

## p5-claim-core

- pending

## p6-verifier

- pending

## p7-reference-server-both

- pending

## p8-audience-rename

- pending

## p9-groups-config

- pending

## p10-claim-switch

- pending

## p11-forward-audit

- pending

## p12-reference-server-final

- pending

## p13-membership-tool

- pending

## p14-token-admin-only

- pending

## p15-admin-approvals

- pending

## p16-contract-and-examples

- pending

## p17-retire

- pending

## Final review

- pending

## Finishing

- pending
