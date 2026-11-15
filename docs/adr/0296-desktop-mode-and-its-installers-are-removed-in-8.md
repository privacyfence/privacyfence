# ADR 0296: Desktop mode and its installers are removed in 8.x

## Status

Accepted — 2026-11-14. Supersedes [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md), [ADR 0003](0003-separated-installs-only.md), [ADR 0261](0261-local-mode-is-deprecated-in-7-and-removed-in-8.md). Implemented.

## Context

[ADR 0261](0261-local-mode-is-deprecated-in-7-and-removed-in-8.md) deprecated desktop (local) mode in 7.x and fixed the direction; removal was left to a separate decision. Built-in OAuth apps, the relay, the Admin page and single-user mode all serve organization mode, and desktop mode cannot use built-in apps. Two products in one tree cost more with every feature.

## Decision

- PrivacyFence 8 has one mode: organization mode, which includes single-user mode on localhost.
- It is installed only as the organization Docker image. There is no desktop install, no bare-host systemd setup and no `pip install privacyfence`.
- The macOS, Windows and Linux installers, the companion app, the privilege-separated install, the Claude Desktop `.mcpb` extension and its shim are removed, together with every local-mode code path and the desktop command-line flags.
- Old installers stay on their GitHub Releases. A person on a desktop install keeps running 7.x or follows the guide "Moving to organization mode".

## Alternatives considered

- **Packaging only in 7.x.** Rejected: leaves two products' code in one tree.
- **Keeping pip local mode.** Rejected: it keeps all of the local-mode code, its trust boundary and its platform tests alive.
- **Shipping a `.deb` that runs organization mode.** Rejected: it keeps an installer, a systemd unit and a platform leg for no reader the Docker image does not already serve.

## Consequences

- Desktop users must move to organization mode or stay on 7.x; the start-up checks that say so are in the next two ADRs.
- The local file bridge, the loopback sign-in and the privilege-separation code have no remaining caller and are deleted with their tests.
- Organization behaviour that depended on privilege separation takes its organization value.

## Verification

The behaviour is covered by the tests named in the ADRs that follow; the deleted code and its tests do not exist.

## Related

ADR 0002, 0003, 0261, 0297, 0298, 0299, 0304.
