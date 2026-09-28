# ADR 0103: The companion copies the AI client token to the clipboard, with no passkey; Settings reaches it only through the companion

## Status

Accepted — 2026-09-28. Implements [issue 795](https://github.com/privacyfence/privacyfence/issues/795)
except its ChatGPT desktop steps, which follow with ChatGPT's own setup doc.
Amends [ADR 0008](0008-one-principal-per-os-user.md) D3, which names only the `.mcpb` shim and
`privacyfence-app --print-mcp-token` as ways to get the MCP token.

## Context

An AI client that connects to `/mcp` directly in local mode (Claude Code, and any other client that
speaks Streamable HTTP with a custom header) needs the MCP URL and the caller's own bearer token.
Until now the token came only from a terminal command, `privacyfence-app --print-mcp-token`, whose
path differs on every platform and which prints nothing visible on Windows unless its output is
captured. Settings said nothing about connecting a client at all.

Two facts bound what a better path may do:

- **The token is not secret from this OS user.** Anything running as the user, the agent included,
  gets it without any confirmation by sending `MINT MCP` over the control channel
  (`web/control_channel.py`, ADR 0008 D3). The kernel identifies the account, not the process.
- **The token cannot approve anything.** `/mcp` and the browser surface are audience-separated
  ([ADR 0061](0061-the-mcp-token-and-the-browser-session-are-audience-separated.md)): the token is
  refused on `/approvals`, `/settings` and `/security`.

And one posture constrains where it may travel: under
[ADR 0003](0003-separated-installs-only.md)'s 2026-09-19 Out-of-scope amendment, a packaged install
puts no secret in an HTTP response a `pf_session` holder can read. The one-time recovery code was
moved to the companion's desktop dialog for that reason.

## Decision

1. **The companion menu gets "Copy AI Client Token"** (macOS menu bar, Windows tray, and a
   `CopyMcpToken` Desktop Action on Linux, `--action=copy-mcp-token`). It asks the daemon for this
   account's token with `MINT MCP`, puts it on the native clipboard (pbcopy; `win32clipboard`;
   wl-copy, xclip or xsel, whichever the running display server has, at absolute paths), and says
   so: "Treat it like a password." No confirmation dialog.
2. **Settings gets a "Connect an AI client" section** (nav: **AI clients**), in local mode with an
   `/mcp` endpoint only. It shows the MCP URL, the `Authorization: Bearer <token>` header, the
   `claude mcp add` command, the generic Streamable HTTP steps and links to the setup docs.
3. **Its Copy token button never returns the token.** `POST /api/settings/mcp_token/copy` has the
   daemon mint the owner's token and send it to the owner's companion as `COPY MCP <token>`; the
   companion copies it and shows the same notice. The response carries only the settings snapshot,
   or an error naming why the companion could not do it. The route has the ordinary settings
   mutation checks (session, CSRF, Origin) and no more: no passkey, and no human session.
4. **Rotate token is a sensitive action.** `POST /api/settings/mcp_token/rotate` uses `ROTATE MCP`'s
   backing (the old token is unregistered before the new one is registered), then delivers the new
   token the way Copy token does. It needs an explicit confirmation, a human session where the
   install requires one, and the passkey step-up whenever `step_up.require_passkey` is on, like the
   other bespoke sensitive route, the organization bundle upload.
5. **`COPY MCP` is held to one shape.** The companion accepts only a value matching the token
   alphabet and length, puts it on the clipboard and nowhere else (not in the notice, a log line or
   the reply), and on a separated install accepts the command from the daemon's service account
   only, like every companion command except a page `SHOW`.

## Alternatives considered

- **Settings → Copy token → passkey → the token in the response, copied by the page.** Rejected:
  it puts the secret in an HTTP response, against ADR 0003's amendment, and the passkey protects
  nothing, because a same-user process that could read that response can send `MINT MCP` itself.
- **A passkey (or a confirmation dialog) before copying.** Rejected: friction without protection,
  for the same reason. The token also cannot reach the Settings or Approvals routes (ADR 0061).
- **Show the token on the page, masked, with a reveal button.** Rejected: the same HTTP-response
  problem, plus the token on screen.
- **Rotate without a passkey, like copy.** Rejected: copying hands out something the caller could
  already get; rotating takes something away from every connected client at once, which an agent
  holding an unattested session should not be able to do on its own say-so.
- **Per-client setup steps for clients whose instructions have not been run.** Left out: the page
  lists Claude Code and the generic Streamable HTTP steps only. A client's steps join when its setup
  doc does ([ADR 0100](0100-unverified-client-setup-instructions-never-merge-to-main.md)).

## Consequences

- Connecting Claude Code, or any client with a custom-header form, needs no terminal command for
  the token. `--print-mcp-token` stays for scripts.
- Copy token needs a running companion. Without one it says so and names starting the companion;
  a rotation that reached no companion has still happened, and the message says to copy the new
  token from the companion's menu.
- The clipboard is readable by other programs running as the user. That exposes nothing
  `MINT MCP` does not already give them. PrivacyFence does not clear the clipboard afterwards.
- On a Linux desktop with none of wl-copy, xclip or xsel the copy fails with a message naming them;
  none is a dependency (ADR 0002 decision 4's Linux budget).

## Verification

- `src/privacyfence/web/control_channel.py`: `copy_mcp_token_to_clipboard`, `_copy_to_clipboard`,
  `COPY MCP` in `_handle_companion_request`, `send_mcp_token`; tests in
  `tests/unit/web/test_control_channel.py` (`TestCopyMcpCommand`, `TestClipboardWriters`, and
  `TestCompanionChannelPeerVerification` for the daemon-only rule).
- `src/privacyfence/companion.py`: `ACTION_COPY_MCP_TOKEN`, `_copy_mcp_token`;
  `resources/linux/privacyfence-companion.desktop`; tests in `tests/unit/test_companion.py`.
- `src/privacyfence/web/routes_settings.py`: the two routes, `/api/settings/mcp_token/rotate` in
  `_BESPOKE_SENSITIVE_ROUTE_PATHS` and `/api/settings/mcp_token/copy` in
  `_BESPOKE_EXEMPT_ROUTE_PATHS`; tests in `tests/unit/web/test_routes_settings.py`
  (`TestAiClientTokenRoutes`, `TestBespokeRoutesAreClassified`).
- `src/privacyfence/web/server.py`: `copy_local_mcp_token`; `tests/unit/web/test_server.py`
  (`TestAiClientTokenDelivery`).

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md): the companion and its menu.
- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md): step-up on sensitive
  settings writes.
- [ADR 0062](0062-only-a-companion-attested-session-may-approve.md): what makes a session human.
