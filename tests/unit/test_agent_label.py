"""agent_label.py -- how an AgentIdentity is shown on the approval card and list."""
from __future__ import annotations

import pytest

from privacyfence.agent_identity import (
    REGISTRY,
    SERVER_PLACEHOLDER,
    UNKNOWN_AGENT,
    UNRECOGNISED_LABEL,
    AgentIdentity,
    AgentSource,
    identify,
)
from privacyfence.agent_label import (
    NEUTRAL_SUBJECT,
    TIER_ATTESTED,
    TIER_CLAIMED,
    TIER_UNKNOWN,
    label_for,
    unknown_agent_label,
)

ATTESTED_SOURCES = [s for s in AgentSource if s.is_attested()]
CLAIMED_SOURCES = [s for s in AgentSource if s.value and not s.is_attested()]


class TestTiers:
    @pytest.mark.parametrize("source", ATTESTED_SOURCES)
    def test_attested_registry_match_gets_name_subject_and_mark(self, source):
        label = label_for(identify("claude-code", "2.0", source))
        assert label.tier == TIER_ATTESTED
        assert label.headline == "Claude Code"
        assert label.subject == "Claude Code"
        assert label.icon_id == "claude-code"
        assert label.claim == ""
        assert label.text == "Claude Code"

    @pytest.mark.parametrize("source", CLAIMED_SOURCES)
    def test_claimed_registry_match_is_a_claim_with_no_mark(self, source):
        label = label_for(identify("ChatGPT", "", source))
        assert label.tier == TIER_CLAIMED
        assert label.headline == "Says it is ChatGPT"
        assert label.subject == NEUTRAL_SUBJECT
        assert label.icon_id == ""

    def test_no_signal_is_unknown(self):
        UNKNOWN_AGENT_LABEL = unknown_agent_label()
        assert UNKNOWN_AGENT_LABEL == label_for(UNKNOWN_AGENT)
        assert UNKNOWN_AGENT_LABEL.tier == TIER_UNKNOWN
        assert UNKNOWN_AGENT_LABEL.headline == UNRECOGNISED_LABEL
        assert UNKNOWN_AGENT_LABEL.text == UNRECOGNISED_LABEL
        assert UNKNOWN_AGENT_LABEL.subject == NEUTRAL_SUBJECT
        assert "Claude" not in UNKNOWN_AGENT_LABEL.text

    @pytest.mark.parametrize("source", [*ATTESTED_SOURCES, *CLAIMED_SOURCES])
    def test_unmatched_name_is_unknown_carrying_the_claim(self, source):
        label = label_for(identify("claude-exfil", "", source))
        assert label.tier == TIER_UNKNOWN
        assert label.claim == "claude-exfil"
        assert label.text == f"{UNRECOGNISED_LABEL} “claude-exfil”"
        assert label.icon_id == ""

    def test_a_registry_id_with_no_source_is_not_trusted(self):
        # identify() never builds this, but a hand-made identity with a
        # registry id and no source must still render as unknown.
        label = label_for(AgentIdentity(id="claude", name="Claude", version="", source=AgentSource.NONE))
        assert label.tier == TIER_UNKNOWN
        assert label.claim == ""

    def test_every_registry_entry_resolves_in_both_tiers(self):
        for entry in REGISTRY:
            name = entry.client_names[0] if entry.client_names else entry.name_templates[0].replace(SERVER_PLACEHOLDER, "pf")
            assert label_for(identify(name, "", AgentSource.OVERRIDE)).icon_id == entry.agent_id
            assert label_for(identify(name, "", AgentSource.CLIENT_INFO)).icon_id == ""

    def test_to_dict_is_the_list_payload(self):
        assert label_for(identify("Claude", "", AgentSource.OAUTH_CLIENT)).to_dict() == {
            "tier": "attested", "headline": "Claude", "claim": "", "icon_id": "claude",
        }


class TestScopedToken:
    """ADR 0164: a scoped token's calls are an override-attested identity named after the token."""

    TOKEN = AgentIdentity(id="scoped-token:st-1", name="sales-sync", version="", source=AgentSource.OVERRIDE)

    def test_named_after_the_token(self):
        label = label_for(self.TOKEN)
        assert (label.tier, label.headline, label.icon_id) == (TIER_ATTESTED, "sales-sync", "")

    def test_a_claimed_scoped_id_is_not_attested(self):
        claimed = AgentIdentity(id="scoped-token:x", name="sales-sync", version="", source=AgentSource.CLIENT_INFO)
        assert label_for(claimed).tier == TIER_UNKNOWN
