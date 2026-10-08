# ADR 0125: The org-mode plugin contract is reserved in protocol 1 and rejected in local mode

## Status

Accepted — 2026-10-07. The reservation is implemented: `src/privacyfence/plugins/protocol.py`,
`src/privacyfence/plugins/manifest.py`, `docs/plugin-protocol/protocol.schema.json`. The org-mode
host is not built.

## Context

The plugin framework is built for local mode first, where the owner is the one principal. An
organization deployment has many principals, roles and service sign-ins that a plugin would need to
see, and an administrator, not each user, would manage plugins. Building that now is out of scope,
but a protocol that has to add fields to existing messages later breaks the plugins written in
between.

## Decision

Protocol 1 reserves the org-mode fields and gives them a defined local-mode behavior:

- `PrincipalContext.roles` and the `source.call` `credential` parameter are part of the message
  shapes. In local mode the daemon never sends `roles`, and a `source.call` that carries a
  `credential` is refused with `org_only_field`.
- The manifest's `service_credentials` key parses, and `true` is a manifest error in local mode.
- `initialize` carries `mode` (`local` or `org`) and a list of principals, which in local mode holds
  exactly the local principal, and `principal.removed` is defined in the protocol and the SDK. Local
  mode never sends it, because the local principal cannot be removed.
- Every request a plugin sends names a principal by id, and local mode answers `unknown_principal`
  to any id but `local`.

Org mode does not start the plugin host, the plugin Settings actions are allowed in local mode
only, and the plugin page routes are not mounted. An org-mode install therefore has no plugins.

## Alternatives considered

- **Leave the fields out and add them in a later major version.** Rejected. A major version is a
  different contract, and every installed plugin would stop starting.
- **Build org mode with the first version.** Rejected. It needs a decision on who manages plugins
  centrally and how service sign-ins reach them, which no consumer needs yet.
- **Accept the fields in local mode and ignore them.** Rejected. A plugin that depends on a role or
  a credential must fail loudly where there are none.

## Consequences

- A plugin written for protocol 1 can already be written to handle several principals and org-only
  fields, and will work unchanged when an org-mode host exists.
- Until then a plugin's `roles` and `credential` code paths cannot be exercised against the daemon.
- Whether an org-mode host needs another major version is decided when it is built.

## Verification

`tests/unit/plugins/test_protocol.py` (`org_only_field` in local mode),
`tests/unit/plugins/test_manifest.py` (`service_credentials`), `tests/unit/plugins/test_source_ops.py`
(`credential` refused) and `tests/unit/web/test_org_settings_scope.py` (the plugin actions are local
mode only).

## Related

- [The tracking issue](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md)
- [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
