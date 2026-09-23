# ADR 0015: org-config bundle integrity has two independent layers: a startup hash log and signing

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-04 in
`docs/security-remediation-plan.md`, added in `dd7bccd9` and deleted in `ba1ec76e` on 2026-09-10.
Read it with `git show ba1ec76e^:docs/security-remediation-plan.md`, section "How this differs from
the review's own §15 roadmap" item 1, and Phase 1 rows 1.1 and 1.1b). Implemented. **Both steps
landed together** in one commit, `04cb47db` (2026-09-07, "Phase 1.1(a+b)"), so the sequencing the
plan argued for never happened as a gap in time. What survives is the layering: the hash log was
kept as an independent record next to signing, not treated as scaffolding.

## Context

The 2026-09-04 technical code review (finding SEC-05) found that a tampered `org_config.json` was
undetectable. The bundle carries the organization's IdP client secret, the connectors' app
registrations and, in org mode, the daemon's own authorization-server trust configuration
(`idp.issuer`, `server.issuer_url`). SEC-04 already rejected malformed files. A well-formed
replacement that carried someone else's credentials passed, because nothing distinguished it from
the real bundle.

The review asked for signing. It also offered an explicit "minimum viable fallback": log a bundle
hash at every startup so that tampering is at least detectable. The plan judged full signing a
real subproject (key management, `scripts/build_org_bundle.py` changes, and a verification path
that must itself fail closed).

## Decision

Bundle integrity has two independent layers:

1. **Detect (every install).** At every daemon startup,
   `daemon_main.log_org_config_bundle_hash()` records the SHA-256, size and signed/unsigned state
   of the installed `org_config.json` to the application log and to the audit trail (decision
   `org_config_startup`). This applies whether or not the install has adopted signing.
2. **Prevent (signing).** `src/privacyfence/org_bundle_signing.py` verifies an Ed25519 signature
   using trust-on-first-use key pinning. The first signed bundle an install sees pins its embedded
   public key. After that, every bundle loaded by `daemon_main.load_org_config()` or installed
   through `settings_controller.install_org_config_bytes()` must verify against the pinned key. A
   downgrade to unsigned is rejected as well. A failed check raises `ConfigurationError`, and the
   daemon refuses to start. `"mode": "org"` requires a signed bundle. For local mode, signing is
   opt-in.

The plan framed this as a sequencing decision: ship step 1 first so that the detectability gain
did not wait on a crypto and key-distribution design, and track step 2 separately as Phase 1b.
It flagged the split as a judgment call and offered to go straight to signing instead.

## Alternatives considered

- **Go straight to full signing, with no interim hash log.** The plan offered this as an equally
  acceptable choice ("swap for full signing if preferred"). In practice both were built in the
  same change, and the hash log was kept as a record for installs that never sign, and as a second,
  independent record for installs that do (`log_org_config_bundle_hash`'s docstring).
- **A public key pinned out of band.** Plan row 1.1b described verifying "against a pinned public
  key" without saying how the key is distributed. The implementation uses trust-on-first-use from
  the bundle itself, "without requiring any out-of-band key distribution" (`org_bundle_signing.py`
  module docstring). The sources do not record whether a separately distributed key was weighed
  and rejected.

## Consequences

- A hash log detects tampering but does not prevent it. It helps only someone who compares
  startup hashes across restarts, or who forwards the audit log to a place where that can be done.
- Trust-on-first-use trusts whatever signed bundle arrives first. Rotating a key requires an
  administrator to delete the pinned key file (`org_config_signing_pubkey.txt`), as described in
  `docs/org-mode-setup-guide.md`.
- A local-mode install that never signs gets detection only.
- `scripts/build_org_bundle.py` carries its own copy of the canonicalization and signing logic so
  that it runs without a PrivacyFence install. The two copies must stay byte-identical.
  `tests/unit/test_build_org_bundle.py` cross-checks them.

## Verification

- `src/privacyfence/daemon_main.py`: `log_org_config_bundle_hash()`, and `load_org_config()`'s call
  to `org_bundle_signing.verify_and_maybe_pin()` and its org-mode unsigned check.
- `tests/unit/test_daemon_main.py::TestLogOrgConfigBundleHash`, plus the `load_org_config` tests in
  the same file for pinning, tampering, key replacement, downgrade and unsigned org mode.
- `tests/unit/test_org_bundle_signing.py` and `tests/unit/test_build_org_bundle.py`.

## Related

- `git show ba1ec76e^:docs/security-remediation-plan.md`, the source plan (SEC-05, rows 1.1/1.1b).
- Commit `04cb47db`, where both layers were implemented.
- Commit `151d6994` (self-approval review Phase 3) made the Settings upload ask for explicit
  confirmation before a first trust-on-first-use pin, instead of pinning as a side effect. See
  [ADR 0013](0013-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md).
