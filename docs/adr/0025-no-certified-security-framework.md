# ADR 0025: no certified security framework, BCP or SLA; customers treat the gap as a risk acceptance

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-08-26 in `a2336a49`, which
added the vendor-risk section to `docs/security-and-compliance.md`). In force. The section's
current text is `docs/security-and-compliance.md`, "Vendor risk criteria".

## Context

An approval review of PrivacyFence against a standard vendor-risk questionnaire found that the
project has no certified information security management framework, no Business Continuity Plan
and no formal risk-response process or SLA. `a2336a49`'s commit message records the review's
conclusion: "risk approval is essential even though the tool itself does not create additional
Information Security Risk."

The deployment model decides what could be certified at all. Local mode runs entirely on the
employee's device. Org mode runs on a server the organization provisions and controls. There is
no PrivacyFence-operated infrastructure, no multi-tenant service, and no PrivacyFence API in the
connector data path (`docs/security-and-compliance.md`, "Deployment model"). The project is
maintained by a single person (`SECURITY.md`, "Supported versions").

## Decision

PrivacyFence does not pursue, and does not claim, any of the three:

- **Certified information security framework** (e.g. ISO 27001): none. The docs say such
  certifications "attest to controls around *operated* infrastructure, which doesn't exist here."
- **Business Continuity Plan**: none. There is no operated service whose outage could disrupt a
  deployment; installed copies keep running if the maintainer becomes unreachable, and the open
  source code lets an organization audit, fork or maintain a pinned version itself.
- **Risk-response process or SLA**: none. Vulnerability reports are handled best-effort, with no
  committed response time (`SECURITY.md`, "Acknowledgement and triage").

The project documents this as a structural consequence of the deployment model, "not an
oversight". It tells evaluating organizations to treat the absence as "a risk-acceptance
decision, not a security gap":

- approve PrivacyFence through a risk-acceptance or exception process, not a standard
  vendor-security sign-off;
- pin deployments to a specific reviewed release rather than auto-updating;
- assign an internal owner to track new releases and patch or roll back if a report doesn't land
  in time.

## Alternatives considered

The sources name no alternative that was weighed and rejected (for example, pursuing a
certification or offering a paid support contract). What they record is the reasoning for why
the three criteria do not apply: certification and BCP assume operated infrastructure or an
operated service that does not exist here. The original section added that a vendor with a full
ISMS and an SLA but "a hosted backend in the request path is a *different*, and in some respects
larger, attack surface" (`git show a2336a49:docs/security-and-compliance.md`, §9).

## Consequences

- PrivacyFence cannot pass a standard vendor-security sign-off unchanged. Each adopting
  organization carries the approval effort through its own exception process.
- Patch timing is not guaranteed. Organizations that need a guaranteed turnaround must plan for
  it themselves (pinned releases and an internal owner) rather than assume it.
- The technical risk profile is stated as unchanged by this posture: no vendor infrastructure in
  the data path, no new data processor, human-in-the-loop enforcement on sensitive calls, and a
  local audit trail.
- Documentation must not imply certification. The same file states that it "is a technical
  control reference, not a certification statement" and that the repository documentation "should
  not be read as claiming certification by itself" ("Compliance positioning").
- If PrivacyFence ever operates infrastructure in the data path (a hosted service), the premise
  of this ADR no longer holds and the posture needs a new ADR.

## Verification

- `docs/security-and-compliance.md`: "Deployment model", "Vendor risk criteria", and "Compliance
  positioning", plus the opening statement at the top of the file.
- `SECURITY.md`: "Supported versions" (no support contract, no committed patch SLA) and
  "Acknowledgement and triage" (best-effort), both linking back to "Vendor risk criteria".

## Related

- `a2336a49` (2026-08-26): the original §9 of `docs/security-and-compliance.md`, with a table per
  criterion, a list of compensating controls and a matching FAQ row.
- `6de7f7cd` and `b72da459`: later rewrites of `docs/security-and-compliance.md` that shortened
  the section to its current form.
