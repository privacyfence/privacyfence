"""How an ``AgentIdentity`` is shown to a human -- ADR 0006 decision 4, ADR 0035 decisions 2 and 4.

``agent_identity`` decides who a request came from and how strongly that is known; this module
decides only what the approval card and the approval list say about it. Both surfaces go through
``label_for()`` so they cannot drift apart on the wording or on which tier gets the brand mark.

Three tiers, and they never look the same:

- **attested** -- a registry entry named by a signal the caller could not choose (``override``,
  ``oauth_client``). The only tier that gets the vendor's brand mark (``icon_id``) and the only one
  whose name the card's copy uses as its subject ("What Claude Code already knows").
- **claimed** -- a registry entry named by a signal the caller chose (``client_info``,
  ``endpoint``). No brand mark; the name is shown as a claim ("Says it is ChatGPT"), marked
  ``NOT_VERIFIED``, and the copy's subject is the neutral ``NEUTRAL_SUBJECT``, never the brand.
- **unknown** -- no usable signal, or a name no registry entry matches exactly.
  ``UNRECOGNISED_LABEL``, with the sanitized raw claim alongside when one was sent. Never blank
  and never "Claude".

Everything here returns raw text. Every renderer escapes it, the same contract
``approval_window_html.fill_agent_placeholder`` documents -- escaping here too would show a name
containing ``&`` double-escaped.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent_identity import REGISTRY, UNKNOWN_AGENT, UNKNOWN_ID_PREFIX, UNRECOGNISED_LABEL, AgentIdentity

TIER_ATTESTED = "attested"
TIER_CLAIMED = "claimed"
TIER_UNKNOWN = "unknown"

# What the card's copy calls a caller whose name is not attested: "What will be provided to the AI
# system", not "... to ChatGPT". A claimed brand is shown once, as a claim, and never borrowed as
# the subject of a sentence that reads as fact.
NEUTRAL_SUBJECT = "the AI system"

# The affordance beside every non-attested label.
NOT_VERIFIED = "Not verified"

_REGISTRY_IDS = frozenset(entry.agent_id for entry in REGISTRY)


@dataclass(frozen=True)
class AgentLabel:
    tier: str
    # The registry display name (attested, claimed) or the sanitized raw claim (unknown, "" when
    # nothing was sent).
    name: str
    # What the card's copy calls the caller -- approval_window_html.AGENT_PLACEHOLDER's fill.
    subject: str
    # The registry agent_id whose bundled mark to show -- attested tier only, "" otherwise.
    icon_id: str

    @property
    def headline(self) -> str:
        """The one-line label: the name, the claim, or ``UNRECOGNISED_LABEL``."""
        if self.tier == TIER_ATTESTED:
            return self.name
        if self.tier == TIER_CLAIMED:
            return f"Says it is {self.name}"
        return UNRECOGNISED_LABEL

    @property
    def claim(self) -> str:
        """The raw claimed name shown after an unknown label, or ""."""
        return self.name if self.tier == TIER_UNKNOWN else ""

    @property
    def text(self) -> str:
        """``headline`` and ``claim`` as one plain string, for a surface with room for one line."""
        return f"{self.headline} “{self.claim}”" if self.claim else self.headline

    def to_dict(self) -> dict[str, str]:
        """The shape the approval list's live re-render reads (``PendingApproval.to_summary_dict``)."""
        return {"tier": self.tier, "headline": self.headline, "claim": self.claim, "icon_id": self.icon_id}


def label_for(agent: AgentIdentity) -> AgentLabel:
    """The tiered label for ``agent``. A registry match is required for the attested and claimed
    tiers: an attested signal naming something the registry does not know still renders as
    unknown, because there is no display name or mark to give it."""
    if agent.id in _REGISTRY_IDS and agent.source.value:
        if agent.is_attested():
            return AgentLabel(tier=TIER_ATTESTED, name=agent.name, subject=agent.name, icon_id=agent.id)
        return AgentLabel(tier=TIER_CLAIMED, name=agent.name, subject=NEUTRAL_SUBJECT, icon_id="")
    claim = agent.name if agent.id.startswith(UNKNOWN_ID_PREFIX) else ""
    return AgentLabel(tier=TIER_UNKNOWN, name=claim, subject=NEUTRAL_SUBJECT, icon_id="")


# What a surface renders when it was handed no identity at all.
UNKNOWN_AGENT_LABEL = label_for(UNKNOWN_AGENT)
