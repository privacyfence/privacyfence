# ADR 0131: A plugin's child processes run under its account, unsupervised, and the plugin confines them

## Status

Accepted — 2026-10-08. Implemented: documentation only (`docs/plugins.md`,
`docs/security-and-compliance.md`); no code enforces or supervises child processes.
Amends [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md).

## Context

A plugin that runs a user's script, a converter or a query engine starts a child process. ADR 0121
makes a plugin trusted code but does not say what a child of it is. A plugin author needs to know
whether PrivacyFence expects them, and a reviewer what the enable decision covers.
Requested in [the design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618).

## Decision

A plugin may start child processes. They run under the service account with the plugin's
environment and hold the same trust as the plugin; enabling a plugin approves what it can start. PrivacyFence
does not supervise them: it does not restart them, count their crashes or limit them. Stopping the
plugin kills its process group on POSIX and only the plugin's own process on Windows, as before, so
on Windows a child can outlive it. The plugin is responsible for confining its children (no
network, read-only inputs, one scratch folder, a time limit) and for stopping them.

## Alternatives considered

- **Forbid or sandbox children.** Rejected. Protocol 1 has no sandbox (ADR 0121), and one that
  covered children only would suggest a plugin itself is confined.
- **Supervise children like the plugin.** Rejected. The daemon cannot tell which child belongs to
  which call, and it would take on responsibility for processes it did not choose.

## Consequences

- The residual risk in `docs/security-and-compliance.md` covers children: a child can read what the
  service account can read.
- A plugin that runs code a human approved (ADR 0127) should confine that code.

## Verification

None beyond the documents; this decision adds no code. `tests/unit/test_docs_references_exist.py`
keeps the links in those documents valid.

## Related

- [The design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618)
- [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
- [ADR 0127](0127-a-plugin-approval-binds-to-its-content-digest-and-persists-until-revoked.md)
