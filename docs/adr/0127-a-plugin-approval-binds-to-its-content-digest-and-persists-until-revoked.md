# ADR 0127: A plugin approval binds to its content digest and persists until revoked

## Status

Accepted — 2026-10-08. Implemented: `src/privacyfence/plugins/approvals.py`,
`src/privacyfence/approvals.py` (`PendingApproval.frame_src`), `src/privacyfence/dialog_window_html.py`
(`build_plugin_approval_html`), `src/privacyfence/web/csp.py`, `src/privacyfence/web/server.py`,
`src/privacyfence/web/routes_approvals.py`, `src/privacyfence/web/routes_plugins.py`,
`src/privacyfence/plugins/pages.py`, `src/privacyfence/plugins/host.py`,
`src/privacyfence/settings_controller.py`.

## Context

A plugin that runs code, applies a template or maps fields on a human's behalf needs a human to
approve that thing once, not once per call. [`confirm.request`](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)
asks a question and forgets the answer. What the plugin needs is an approval that names the thing,
survives restarts, ends only when a human takes it back, and stops covering the thing the moment
its content changes. The plugin also wants to show the human its own rendering of the thing (a
syntax-highlighted script, a layout), which means showing a plugin page inside the approval card.
The page is the plugin's, so nothing on it may be mistaken for what PrivacyFence says is being
approved.
Requested in [the design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618).

## Decision

**What is approved.** A plugin asks with `approval.request` for `(plugin, principal, kind,
subject_id, digest)`, where `digest` is `sha256:` and 64 hex digits of the content. The approval
binds to exactly that tuple. The same subject with a different digest is a new request. An approved
tuple is stored in `plugin-approvals.json` and stays valid until a human revokes it in Settings.
`approval.check` answers `approved`, `revoked` or `unknown`; a revoke notifies a running plugin with
`approval.revoked`. Purging or uninstalling a plugin deletes its approvals. A request for something
already approved opens no card.

**Never automatic.** The card is a confirmation without an operation key, so no rule can accept it
(as in ADR 0122). It is refused while any AI session is unattended. Step-up is on unless the plugin
turns it off for that request. Confirmation limits apply, counted separately from `confirm.request`.
Only approvals are stored: a denial or an expiry leaves nothing behind.

**What the card shows.** PrivacyFence's own fields come first and sit outside any frame: the plugin
(its display name and its installed name, because the display name is the plugin's to choose), the
kind, the subject and the full digest. The plugin's preview blocks follow, then optionally a frame
with the plugin's own page, in a sandbox that allows scripts only. PrivacyFence cannot check that
the digest matches what the preview or the page shows. The digest is what binds; the human is
trusting the plugin to show the content it hashed, the same trust that ADR 0121 places in every
enabled plugin.

**Where framing is loosened, and nowhere else.** Both defences against framing stay on, and each is
relaxed for one response. The card's own response gets `frame-src 'self'`, and only when the card
has a page to show. A plugin page response gets `frame-ancestors 'self'` and
`X-Frame-Options: SAMEORIGIN` only when its query names a `pf_approval` that is a pending approval
of that same plugin framing exactly that page. Any other `pf_approval` is ignored. The query reaches
the plugin unchanged, so the page can show the right subject.

## Alternatives considered

- **Allow framing of every plugin page.** Rejected. Any page, including one a plugin or another
  site could trick a user into opening, could then be embedded in PrivacyFence's own pages.
- **Render the plugin's HTML inside the card document.** Rejected. It would run plugin script in
  the origin that holds the session cookie.
- **Fold `confirm.request` into it.** Rejected. A confirmation is a one-shot answer and an approval
  is a stored, revocable fact about content; one shape cannot say both without a flag that changes
  what a card means.
- **Approve a subject without a digest.** Rejected. An edited script would stay approved.

## Consequences

- A plugin can rely on a stored approval across restarts and must check it before it acts, and
  stop when it hears `approval.revoked`.
- Revoking is non-sensitive, since it only takes trust away (ADR 0070).
- A malicious plugin can show one thing and hash another. The card tells the human what is bound,
  and the residual risk is stated in `docs/security-and-compliance.md`.

## Verification

`tests/unit/plugins/test_approvals.py` (the store, the service and the card),
`tests/unit/web/test_server.py` and `tests/unit/web/test_routes_plugins.py` (the two headers and
when they apply), `tests/integration/test_plugin_approval_frame_browser.py` (the frame loads in a real
browser with the flags set and not without), `tests/integration/test_plugin_approvals.py` (a real
plugin through request, card, check and revoke).

## Related

- [The design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618)
- [ADR 0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md)
- [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
- [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)
- [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md)
