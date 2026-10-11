# ADR 0151: PrivacyFence signs in to Peek with Peek's own device sign-in and keeps the token per principal

## Status

Accepted — 2026-10-11. Implemented.

## Context

Peek's API takes a bearer token. Peek offers a device sign-in for its own command-line tool
(`/api/cli/login/start` and `/api/cli/login/poll`): the client gets a short code, the person
approves it in Peek in a browser, and the client receives a token once. PrivacyFence needs a token
for each person without that person handling one.

## Decision

PrivacyFence uses Peek's device sign-in: from **Settings > Connectors** in local mode, from
`/connect` in organization mode, and from `--peek-login` on the command line. The token is saved per
principal in `credentials/peek_token.json` together with the server it belongs to, written with
`secure_files.atomic_write_json` (mode 0600). It is never in `settings.yaml`, the settings snapshot,
a log line, an error message or the audit log; the device code is held in memory only until the
sign-in ends.

## Alternatives considered

- **Pasting a token.** On Peek servers that sign people in through SSO only an admin can mint a
  token, and a typed token is easy to leak into chat, shell history or a screenshot.
- **Reading the Peek command-line tool's own config file.** It is another program's credential
  store, it may be absent, and organization mode has no such file on the server.

## Consequences

Connecting needs a browser that can reach Peek, and the person's approval in Peek, not in
PrivacyFence. Revoking the token in Peek ends PrivacyFence's access; the connector then asks to
connect again. One more place writes a token file, so the token-write-site list grows by one.

## Verification

- `tests/unit/test_peek_client.py`: the sign-in start and poll parsing, the credential file round trip
  and its mode, and that a device code or token never appears in a `repr`.
- `tests/unit/test_systemic_gate_invariants.py`: the token write sites are exactly the listed ones.
- `tests/unit/test_settings_controller.py`, `tests/unit/web/test_routes_connect.py` and
  `tests/unit/test_daemon_main.py`: the three sign-in paths, and that the snapshot carries no code or
  token.

## Related

- [`peek_client.py`](../../src/privacyfence/peek_client.py), [`connectors/peek.py`](../../src/privacyfence/connectors/peek.py)
- ADR [0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md) (live QA credentials stay on the runner), ADR [0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md), ADR [0072](0072-org-mode-persists-only-sealed-refresh-tokens.md) (org mode keeps per-person credentials)
- ADR [0152](0152-which-peek-server-is-used-and-a-token-stays-with-it.md)
