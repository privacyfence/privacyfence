"""write_effects.py -- the write card's own consequence sentence.

The coverage test below is the point of this module having a table at all:
a write tool that reaches the gate without an entry renders a card that
shows a payload and a reason and never says what approving it does. That is
the state this file exists to make unreachable, so it is asserted against
the connectors themselves rather than against a hand-maintained list.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from privacyfence.write_effects import EFFECT_BY_TOOL, effect_for

_CONNECTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "privacyfence" / "connectors"


def _write_gated_tools() -> set[str]:
    """Every ``tool=`` that reaches ``gated_call(..., gate="popup")``.

    Read off the connector sources rather than a list kept here, so adding
    a write tool cannot also silently add an exemption from this check --
    the same reason approvals.PendingApproval.is_batchable()'s own coverage
    test derives its set instead of restating it.
    """
    tools: set[str] = set()
    for path in sorted(_CONNECTORS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"gated_call\((.*?)\n\s*\)", source, re.S):
            block = match.group(1)
            if 'gate="popup"' not in block:
                continue
            tool = re.search(r'tool="([^"]+)"', block)
            if tool:
                tools.add(tool.group(1))
    return tools


class TestCoverage:
    def test_the_scan_finds_the_write_tools_at_all(self):
        # Guards the guard: a regex that silently matched nothing would
        # make every assertion below vacuously true.
        found = _write_gated_tools()
        assert len(found) > 40
        assert "slack_send_message" in found
        assert "gmail_add_label" in found

    def test_every_write_gated_tool_has_an_effect_sentence(self):
        missing = sorted(_write_gated_tools() - set(EFFECT_BY_TOOL))
        assert not missing, (
            "these write tools would render a card that never says what approving it does: "
            f"{missing}"
        )

    def test_no_entry_describes_a_tool_that_no_longer_reaches_the_write_gate(self):
        stale = sorted(set(EFFECT_BY_TOOL) - _write_gated_tools())
        assert not stale, f"effect sentences for tools that are no longer write-gated: {stale}"


class TestSentences:
    @pytest.mark.parametrize("tool", sorted(EFFECT_BY_TOOL))
    def test_reads_as_a_finished_sentence(self, tool):
        sentence = EFFECT_BY_TOOL[tool]
        assert sentence == sentence.strip()
        assert sentence[0].isupper(), sentence
        assert sentence.endswith("."), sentence

    def test_the_irreversible_ones_say_so_plainly(self):
        # The whole reason this row exists. If these ever soften, the card
        # stops distinguishing "add a label" from "post to a channel".
        assert "cannot be unsent" in EFFECT_BY_TOOL["slack_send_message"]
        assert "cannot be unsent" in EFFECT_BY_TOOL["telegram_send_message"]

    def test_a_harmless_one_says_what_does_not_happen(self):
        # Equally load-bearing in the other direction: without the second
        # half, a label add reads as open-ended as a send.
        assert EFFECT_BY_TOOL["gmail_add_label"] == (
            "A label is added. Nothing is sent, moved or deleted."
        )

    def test_drafts_are_explicit_that_nothing_is_sent(self):
        for tool, sentence in EFFECT_BY_TOOL.items():
            if "draft" in tool:
                assert "Nothing is sent." in sentence, tool


class TestEffectFor:
    def test_returns_the_sentence_for_a_known_tool(self):
        assert effect_for("gmail_add_label") == EFFECT_BY_TOOL["gmail_add_label"]

    def test_returns_empty_for_an_unknown_tool(self):
        # Deliberately not a generic fallback -- see effect_for's docstring:
        # a vague sentence would occupy the row that exists to be specific.
        assert effect_for("some_tool_nobody_has_written_copy_for") == ""

    def test_returns_empty_for_no_tool(self):
        assert effect_for("") == ""
