# ADR 0106: A Google not-found or not-shared answer reaches the AI client as a fixed message naming the connected account; every other Google error stays generic

## Status

Accepted — 2026-09-29. Implements [issue 799](https://github.com/privacyfence/privacyfence/issues/799).

## Context

When a Google API call returned 404 (a file not shared with the connected account, a deleted
message, a wrong ID), the agent saw only `Tool call failed. See the PrivacyFence log for details.`
Each connector's `_fetch` re-raises its `*ClientError` as a bare `RuntimeError`, which
`safe_errors.public_message()` deliberately maps to that generic text, because the `*ClientError`
wraps Google's own response text (third-party text `safe_errors` never forwards). The agent, and
the user reading its reply, could not tell "share this file with the connected account" from a real
outage.

Two facts bound the fix. About a dozen connector call sites are best-effort `except RuntimeError:`
fallbacks (thumbnail preview, parent-folder name lookup, tab-title lookup, `_note_own_write`) that
must keep catching these failures. And 403 is not one answer: it is also Google's response to rate
limits, a missing OAuth scope, domain policy and quota.

## Decision

A Google 404, a Calendar 410 with reason `deleted`, and the three 403 shapes in
`src/privacyfence/google_errors.py` (`insufficientFilePermissions`, `requiredAccessLevel`, and the
v4-style `PERMISSION_DENIED` with message "The caller does not have permission") reach the agent as
a fixed PrivacyFence-written message naming the connected account; every other Google error stays
generic.

- `google_errors.unavailable_error()` classifies off the original `HttpError` in the `*ClientError`'s
  `__cause__` chain, and returns a `GoogleResourceUnavailableError`, a named `RuntimeError`
  subclass that `public_message()` trusts.
- Each of the six Google connectors' `_fetch` calls it once and raises its result instead of the
  bare `RuntimeError` when it is not `None`.
- The message names the account only when `my_email` looks like an address. Apps Script now gets
  `my_email` from `check_connection()`; Tasks does not (its `check_connection()` returns no address).

## Alternatives considered

- **A `ValueError` subclass, as the issue suggested** — it would escape the connectors' best-effort
  `except RuntimeError:` fallbacks, turning an optional lookup's 404 into a failed tool call.
- **Classifying inside each `*_client.py`'s `raise ...ClientError(...) from exc` sites** — one
  classification point per connector, off the `__cause__` chain, covers every call without touching
  about 150 raise sites.
- **Passing Google's own `message` text through** — it is third-party text, which `safe_errors`
  never forwards; the messages are fully ours.
- **Treating every 403 as "no permission"** — 403 also means rate limit, missing scope, domain
  policy and quota; calling those "not shared" would send the user in the wrong direction.
- **Leaving out the account's address** — the agent can already read it (Drive `owners`, Calendar
  organizer, Gmail `From` on sent mail), it is the identity the user signed in with, and it is the
  one fact that tells the user which account to share with. It goes only into these messages.

## Consequences

- The connected account's address now reaches the agent in these error messages, only when it looks
  like an address.
- A 404 on a secondary lookup, such as a destination folder, is still worded "this file".
- Contacts and Tasks messages carry no address until their `check_connection()` returns one.
- The ID is not in the message: `_fetch` doesn't know which argument was the ID and the caller has it.

## Verification

`tests/unit/test_google_errors.py` (classification, exact templates, no Google text in the message,
non-address accounts left out), `tests/unit/test_safe_errors.py`, each Google connector's
`TestGoogleUnavailableErrors`, and `tests/unit/test_daemon_main.py` for the `my_email` wiring.

## Related

Issue #799; `src/privacyfence/safe_errors.py`; `src/privacyfence/google_errors.py`.
