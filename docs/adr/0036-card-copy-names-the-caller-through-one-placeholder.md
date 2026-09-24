# ADR 0036: card copy names the caller through one placeholder, and connectors never learn who is asking

## Status

Accepted (recorded retroactively on 2026-09-24; decided on 2026-09-24 in #648 and #654).

## Context

The approval card named its caller "Claude" in fixed copy: "What will be provided to Claude", "Why
Claude is doing this", "What Claude already knows", and `_DISCLOSURE_BLOCK`'s "None — not disclosed
to Claude". [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) asked for that
copy to be templated before detection landed. Part of it is not written by
`approval_window_html.py` at all: the Gmail, Drive, Slack and Confluence connectors return
`new_info` rows such as "Content returned to Claude", built where the connector runs the tool.

Two questions had to be settled. How does the caller's name reach strings that a connector writes?
And which name goes into a sentence that reads as a statement of fact, when the identity may be
only a claim?

## Decision

1. **Every card string that names the caller is written with one placeholder,
   `approval_window_html.AGENT_PLACEHOLDER` (`"{agent}"`).** This covers the card's own copy and
   the connectors' `new_info` rows. A connector writes the placeholder and is never told which AI
   system is asking. `card_builder._disclosure_rows` fills the connector rows, and the card
   template fills its own copy.
2. **The fill is `approval_window_html.fill_agent_placeholder`, a plain `str.replace`.** It is
   never `str.format`: the text can carry other braces, and the name is caller data. The fill is
   left unescaped, and every renderer escapes the result exactly once.
3. **The value filled in is `agent_label.AgentLabel.subject`.** For an attested identity that is
   the registry display name. For a claimed or unknown identity it is the neutral
   `NEUTRAL_SUBJECT`, "the AI system". There is no "Claude" default: a card built with no identity
   also says "the AI system". A claimed name appears on the card once, in the header, marked as a
   claim ("Says it is ChatGPT", *Not verified*). It is never borrowed as the subject of the
   card's other sentences.

## Alternatives considered

- **Pass the resolved name into each connector.** Every connector would gain an identity
  parameter it has no use for. A connector that can see who is asking can also branch on it, and
  [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) decision 3 forbids
  keying any outcome on a claimed identity. A connector that never receives the name cannot
  break that rule.
- **Fill with `str.format`.** Content-derived row text can contain braces, and the result would
  then depend on what those braces happen to name. `str.replace` substitutes one literal token
  and nothing else.
- **Escape at fill time.** Every renderer already escapes row text, so escaping here as well
  would show a name containing `&` or `<` double-escaped.
- **Keep "Claude" as the default subject.** That is ADR 0006 Invariant 3's failure case: an
  unattributed request named after one vendor.
- **Use the claimed name as the subject** ("What will be provided to ChatGPT"). That sentence
  reads as fact on the one screen where borrowed trust does the most damage. ADR 0006 decision 4
  requires that the claimed tier not present itself the way the attested tier does. The
  maintainer signed off on the neutral subject in #654 (gate G3).

## Consequences

- Connector unit tests that read raw `new_info` expect the literal placeholder. Rendered-card
  tests see the filled text.
- A new connector row that names the caller must use the placeholder. A hardcoded "Claude" would
  be wrong on every card that is not an attested Claude request.
- In local mode almost every identity is claimed, so most cards say "the AI system" throughout
  and name the AI system only in the header.

## Verification

- `src/privacyfence/approval_window_html.py` (`AGENT_PLACEHOLDER`, `fill_agent_placeholder`),
  `src/privacyfence/card_builder.py` (`_disclosure_rows`, `build_card_html`),
  `src/privacyfence/agent_label.py` (`label_for`, `NEUTRAL_SUBJECT`).
- `tests/unit/test_card_builder.py`: `test_no_identity_uses_the_neutral_subject_never_claude`,
  `test_an_attested_name_changes_every_in_scope_string`, `test_a_claimed_name_is_never_the_subject`,
  `test_unattributed_request_renders_unknown_not_blank_not_claude`.
- The connector tests for Gmail, Drive and Confluence assert the raw placeholder in `new_info`.

## Related

- [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) decisions 3 and 4,
  [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
  decision 2.
- #648 (the placeholder and the connector rows), #654 (the tiered subject, and removal of the
  "Claude" default).
- Issues #579, #580.
