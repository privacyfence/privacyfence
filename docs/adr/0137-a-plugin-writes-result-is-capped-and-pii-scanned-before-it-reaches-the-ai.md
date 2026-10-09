# ADR 0137: A plugin write's result is capped at 2,048 bytes and PII-scanned before it reaches the AI

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/plugins/connector.py` (`_screen_write_result`),
`src/privacyfence/plugins/constants.py` (`WRITE_RESULT_MAX_BYTES`),
`plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py`.
Amends [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md).

## Context

A read-only plugin tool releases the payload its card showed, so the human saw what the AI gets.
A write's `result` is different: it is returned to the AI client after the action ran, with no card
showing it. Up to 100,000 bytes could pass that way unscanned, which made it a second path for a
plugin's own data to reach the AI, next to the output files that
[ADR 0130](0130-plugin-outputs-are-a-folder-that-privacyfence-reads-through-its-own-tools.md)
gates behind a card and a PII check. A write result is meant to be an acknowledgement; the largest
in the repository is about 70 bytes.

## Decision

The result of a non-read-only plugin tool, with any `approval_id` merged in, reaches the AI only if
its JSON is at most 2,048 bytes and the local PII detector finds nothing; otherwise the AI gets a
fixed sentence saying the action ran and its result was withheld. The withheld result is
`{"withheld": true, "message": ...}`, plus `approval_id` only when PrivacyFence issued that id to
this plugin (one of its confirmation or approval cards, or a stored approval of it); any other
`approval_id` is dropped. The check covers popup and `auto` writes alike. It logs only the size or
the PII categories, never the content. With PII detection off, only the cap applies.

## Alternatives considered

- **A second card after execute.** Rejected. It doubles the cards per write.
- **No result at all.** Rejected. It breaks the hand-back of the confirmation and approval ids.
- **Raising an error.** Rejected. It invites a retry of a write that already ran.

## Consequences

- A plugin that returned large write results now hands them to the AI through an output file.
- With PII detection off, only the cap applies.
- The SDK still accepts up to 100,000 bytes, so a plugin's own tests do not catch the cap unless
  they use `PluginTestHost`, which applies it.
- A withheld result keeps `approval_id` only when PrivacyFence issued that id to this plugin, so the
  field cannot carry data past the cap.

## Verification

`tests/unit/plugins/test_connector.py`, `tests/unit/plugin_sdk/test_testhost.py`.

## Related

- [Issue 846](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)
- [ADR 0130](0130-plugin-outputs-are-a-folder-that-privacyfence-reads-through-its-own-tools.md)
