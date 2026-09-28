# ADR 0088: local mode shows every requester as "Undetected"

## Status

Accepted — 2026-09-27, decided by the maintainer.
Amends [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
decision 2 and [ADR 0037](0037-a-local-override-is-a-relabel-and-never-attests.md) for what a
local install displays. What is recorded is unchanged.

## Context

[ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) established that local mode
has one MCP token shared by every AI system the OS user runs, so nothing a local install receives
can tell one caller from another. [ADR 0037](0037-a-local-override-is-a-relabel-and-never-attests.md)
removed the last attested source there. Since then, every local-mode label has been a claim:
"Says it is Claude Code" with a **Not verified** badge, or "Unrecognised AI system" with the name
the caller sent.

On a local install that distinction carries no information a user can act on. The badge can never
become **Verified**, so it is on every card, and the claimed name it qualifies is chosen by the
caller. Showing that name, even qualified, still puts the caller's words in the header of a
security decision.

## Decision

- **On a local install, every approval card, approval-list row and Audit Log row names the
  requester as "Undetected"**. There is no claimed name, no "Says it is …", no "Unrecognised AI
  system", no "?" glyph and no **Not verified** badge or note. This is `agent_label.TIER_UNDETECTED`,
  returned by `agent_label.label_for()` for every identity while `agent_label.is_local_mode()`.
- **The mode is install-wide**, set once at daemon startup from `org_mode.resolve_mode()`
  (`agent_label.set_local_mode`). The default is local, matching `org_mode.DEFAULT_MODE`.
- **Organization mode is unchanged**: Verified for a pinned client, Not verified for a claim,
  Unrecognised for no match.
- **The audit log still records the claim.** `agent_id`, `agent_name`, `agent_version` and
  `agent_source` are written exactly as before; only the display changes.
- **Card copy is unchanged.** It already said "the AI system" for anything not attested
  ([ADR 0036](0036-card-copy-names-the-caller-through-one-placeholder.md)).

## Consequences

- A local `agent_overrides:` entry no longer changes anything a human sees on the card, the list or
  the Audit Log page. It still changes the recorded `agent_id`/`agent_name`.
- If per-credential local tokens are ever added (ADR 0037's forward path), local mode would have
  something that can be verified, and a new ADR would decide whether to show names again.

## Rejected

- **Keeping the claimed name with a "Not verified" badge in local mode.** A badge that is on every
  card tells the user nothing, and it still displays text the caller chose.
- **"Unrecognised AI system" for everything.** That implies PrivacyFence tried to recognise the
  caller and failed. "Undetected" says that it has no way to detect the caller.

## Verification

- `src/privacyfence/agent_label.py` (`label_for`, `set_local_mode`), `src/privacyfence/daemon_main.py`
- Renderers: `approval_window_html._agent_html`, `approval_list_html._agent_html` and its JS mirror
  `agentHtml`, `settings_window_html`'s `auditAgentHtml`
- Tests: `tests/unit/test_agent_label.py::TestLocalMode`, and the local-mode cases in
  `tests/unit/test_card_builder.py` and `tests/unit/test_approval_list_html.py`
