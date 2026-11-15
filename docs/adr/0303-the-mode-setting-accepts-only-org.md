# ADR 0303: The mode setting accepts only org

## Status

Accepted — 2026-11-14. Relates to [ADR 0260](0260-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md). Implemented.

## Context

[ADR 0260](0260-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md) lets organization mode start from the environment, and the Dockerfile and existing compose files set `PRIVACYFENCE_MODE=org`. With desktop mode removed, the setting has one valid value, but a person may still carry `local` from a desktop install.

## Decision

- No mode set means organization mode.
- `PRIVACYFENCE_MODE=org` and a bundle `"mode": "org"` are accepted silently.
- `local`, from either source, stops the start with this text, where `{source}` is `PRIVACYFENCE_MODE` or `org_bundle.json's "mode"`:

> {source} says "local": desktop mode is not part of PrivacyFence 8. Remove it (organization mode is the only mode), or keep using PrivacyFence 7 for a desktop install: https://privacyfence.eu/docs/moving-to-organization-mode/

- Any other value stops the start with `{source} must be "org".`
- A bundle and the variable that disagree are still refused.
- Documents whose `source.mode` is `"local"` are still imported by personal settings: that is the documented desktop-to-organization move.

## Alternatives considered

- **Keeping `local` as a recognised mode name that exits with a message from the mode resolver.** Rejected: the resolver and every `mode=` parameter would have to live on; refusing at configuration load lets the plumbing go.
- **Ignoring the setting.** Rejected: a person who set `local` expects desktop behaviour and must be told it is gone.

## Consequences

- One validation runs when the bundle is loaded and once at start; the mode type, its default and the `mode=` parameters are deleted.
- Single-user mode is "no identity provider configured".

## Verification

`tests/unit/test_org_mode.py`.

## Related

ADR 0260, 0301, 0302.
