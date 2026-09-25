# Security policy

## Reporting a vulnerability

Please do not open a public GitHub issue for a suspected vulnerability. Report it privately:

1. **GitHub private vulnerability reporting** (preferred): on this repository's
   [Security tab](https://github.com/privacyfence/privacyfence/security), choose **Report a
   vulnerability**. This opens a private advisory visible only to you and the maintainer.
2. **Email:** info@privacyfence.eu, if you would rather not use a GitHub account.

Please include:

- what an attacker could do, and who is exposed (a local-mode user, an organization deployment, a
  third party);
- steps to reproduce or a proof of concept;
- the affected version (shown in the app and its logs) or commit;
- the deployment mode (local or organization) and any connector or setting involved;
- whether you believe the issue is already public or exploited;
- whether you would like to be credited.

**In scope:** the code in this repository (the daemon, the web UI, the companion app, the `.mcpb`
shim, organization mode) and its packaging and release pipeline. **Out of scope** (as `NOTICE`
sets out): content AI models generate while using PrivacyFence, data returned by connected
services, and vulnerabilities in those services themselves (Google, Slack, Salesforce, Atlassian,
Telegram); report those to the vendor.

## What happens next

PrivacyFence is maintained by one person, with no support contract and no committed response time.
Reports are handled best-effort. That is a deliberate position, explained in
[What PrivacyFence does not claim](docs/security-and-compliance.md#what-privacyfence-does-not-claim).
If you have not heard back after a reasonable interval, a follow-up on the same thread is welcome.

1. **Triage:** the maintainer confirms the issue and which versions and modes it affects.
2. **Fix:** the fix ships in a new tagged release.
3. **Coordinated disclosure:** please keep the issue private until a fix is released or you and the
   maintainer agree on a date. There is no fixed embargo period.
4. **Advisory:** the maintainer may publish a GitHub Security Advisory naming the fixed version,
   with credit if you want it.

## Supported versions

Only the latest release receives security fixes; older releases are not patched. Pre-releases
(`a`, `b` and `rc` versions) are in scope too. Report an issue even if you found it on an older
release. If your organization needs guaranteed fixes for a pinned version, plan for that
internally. Releases are listed on the
[Releases page](https://github.com/privacyfence/privacyfence/releases).

For how PrivacyFence protects data and where each protection stops, see
[Security and compliance](docs/security-and-compliance.md).
