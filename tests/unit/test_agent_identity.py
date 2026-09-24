"""agent_identity.py: the identity model, the Invariant 4 sanitiser, the ADR 0035 registry, and
the agent_scope/current_agent contextvar."""
from __future__ import annotations

import asyncio

import pytest

from privacyfence.agent_identity import (
    MAX_CLIENT_STRING_LENGTH,
    REGISTRY,
    UNKNOWN_AGENT,
    UNKNOWN_ID_PREFIX,
    AgentIdentity,
    AgentSource,
    agent_scope,
    current_agent,
    identify,
    lookup,
    sanitize_client_string,
)


class TestAgentSource:
    @pytest.mark.parametrize("source", [AgentSource.OVERRIDE, AgentSource.OAUTH_CLIENT])
    def test_attested_sources(self, source):
        assert source.is_attested() is True

    @pytest.mark.parametrize("source", [AgentSource.CLIENT_INFO, AgentSource.ENDPOINT, AgentSource.NONE])
    def test_claimed_and_absent_sources_are_not_attested(self, source):
        assert source.is_attested() is False

    def test_none_serializes_as_empty_string(self):
        assert AgentSource.NONE.value == ""

    def test_identity_delegates_attestation_to_its_source(self):
        assert AgentIdentity("chatgpt", "ChatGPT", "", AgentSource.OAUTH_CLIENT).is_attested() is True
        assert AgentIdentity("chatgpt", "ChatGPT", "", AgentSource.CLIENT_INFO).is_attested() is False

    def test_identity_is_frozen(self):
        with pytest.raises(AttributeError):
            UNKNOWN_AGENT.id = "claude"  # type: ignore[misc]


class TestSanitizer:
    def test_plain_name_unchanged(self):
        assert sanitize_client_string("claude-code") == "claude-code"

    def test_length_capped(self):
        assert sanitize_client_string("x" * 500) == "x" * MAX_CLIENT_STRING_LENGTH

    def test_cap_applies_after_stripping(self):
        # Stripped characters do not count toward the cap.
        assert sanitize_client_string("‮" * 10 + "y" * MAX_CLIENT_STRING_LENGTH) == "y" * MAX_CLIENT_STRING_LENGTH

    @pytest.mark.parametrize("control", ["\x00", "\n", "\r", "\t", "\x1b", "\x7f", "\x85"])
    def test_control_characters_stripped(self, control):
        assert sanitize_client_string(f"open{control}ai") == "openai"

    @pytest.mark.parametrize(
        "bidi",
        ["‎", "‏", "‪", "‫", "‬", "‭", "‮",
         "⁦", "⁧", "⁨", "⁩", "؜"],
    )
    def test_bidi_controls_stripped(self, bidi):
        assert sanitize_client_string(f"cla{bidi}ude") == "claude"

    def test_zero_width_and_line_separators_stripped(self):
        assert sanitize_client_string("cl​au de ") == "claude"

    def test_surrounding_whitespace_trimmed(self):
        assert sanitize_client_string("  cursor-vscode \n") == "cursor-vscode"

    def test_non_ascii_letters_kept(self):
        assert sanitize_client_string("Ügyfél") == "Ügyfél"

    @pytest.mark.parametrize("value", [None, 42, ["claude-code"], {"name": "x"}, b"claude-code"])
    def test_non_string_is_absent(self, value):
        assert sanitize_client_string(value) == ""


class TestRegistry:
    @pytest.mark.parametrize(
        ("client_name", "agent_id", "display"),
        [
            ("claude-code", "claude-code", "Claude Code"),
            ("claude-ai", "claude", "Claude"),
            ("openai-mcp", "chatgpt", "ChatGPT"),
            ("gemini-cli-mcp-client", "gemini-cli", "Gemini CLI"),
            ("cursor-vscode", "cursor", "Cursor"),
        ],
    )
    def test_adr_0035_entries(self, client_name, agent_id, display):
        entry = lookup(client_name)
        assert entry is not None
        assert (entry.agent_id, entry.display_name) == (agent_id, display)

    def test_match_is_case_insensitive(self):
        entry = lookup("Claude-Code")
        assert entry is not None and entry.agent_id == "claude-code"

    @pytest.mark.parametrize("near_miss", ["claude-code-x", "claude", "my-claude-code", "claude-cod", "claude code"])
    def test_match_is_exact_not_prefix_or_substring(self, near_miss):
        assert lookup(near_miss) is None

    def test_match_ignores_smuggled_bidi(self):
        entry = lookup("claude‮-code")
        assert entry is not None and entry.agent_id == "claude-code"

    def test_empty_name_matches_nothing(self):
        assert lookup("") is None
        assert lookup("‮\n") is None

    def test_agent_ids_are_unique(self):
        ids = [e.agent_id for e in REGISTRY]
        assert len(ids) == len(set(ids))


class TestIdentify:
    def test_known_client(self):
        agent = identify("claude-code", "2.1.0", AgentSource.CLIENT_INFO)
        assert agent == AgentIdentity("claude-code", "Claude Code", "2.1.0", AgentSource.CLIENT_INFO)

    def test_source_recorded_unchanged_never_upgraded(self):
        # A registry match names the system; it does not attest it.
        assert identify("openai-mcp", "", AgentSource.CLIENT_INFO).source is AgentSource.CLIENT_INFO
        assert identify("openai-mcp", "", AgentSource.OAUTH_CLIENT).source is AgentSource.OAUTH_CLIENT

    def test_unmatched_client_is_unknown_prefixed_with_its_claim(self):
        agent = identify("claude-exfil", "1.0", AgentSource.CLIENT_INFO)
        assert agent.id == UNKNOWN_ID_PREFIX + "claude-exfil"
        assert agent.name == "claude-exfil"
        assert agent.version == "1.0"
        assert agent.source is AgentSource.CLIENT_INFO

    def test_unmatched_name_and_version_are_sanitized(self):
        agent = identify("evil‮\n" + "z" * 100, "1\x00.0", AgentSource.CLIENT_INFO)
        assert agent.name == "evil" + "z" * (MAX_CLIENT_STRING_LENGTH - 4)
        assert agent.id == UNKNOWN_ID_PREFIX + agent.name
        assert agent.version == "1.0"

    @pytest.mark.parametrize("name", [None, "", "   ", "‮", 7])
    def test_no_usable_name_is_unknown_with_empty_source(self, name):
        assert identify(name, "1.0", AgentSource.CLIENT_INFO) == UNKNOWN_AGENT

    def test_no_source_is_unknown(self):
        assert identify("claude-code", "1.0", AgentSource.NONE) == UNKNOWN_AGENT

    def test_unknown_agent_is_all_empty_never_claude(self):
        assert (UNKNOWN_AGENT.id, UNKNOWN_AGENT.name, UNKNOWN_AGENT.version) == ("", "", "")
        assert UNKNOWN_AGENT.source is AgentSource.NONE


class TestAgentScope:
    def test_default_is_unknown(self):
        assert current_agent() == UNKNOWN_AGENT

    def test_scope_sets_and_restores(self):
        x = identify("claude-code", "", AgentSource.CLIENT_INFO)
        y = identify("openai-mcp", "", AgentSource.CLIENT_INFO)
        with agent_scope(x) as scope:
            assert isinstance(scope, agent_scope)
            assert current_agent() == x
            with agent_scope(y):
                assert current_agent() == y
            assert current_agent() == x
        assert current_agent() == UNKNOWN_AGENT

    def test_exit_without_enter_is_a_no_op(self):
        agent_scope(UNKNOWN_AGENT).__exit__(None, None, None)
        assert current_agent() == UNKNOWN_AGENT

    async def test_concurrent_tasks_do_not_see_each_others_agent(self):
        x = identify("claude-code", "", AgentSource.CLIENT_INFO)
        y = identify("openai-mcp", "", AgentSource.CLIENT_INFO)
        seen: dict[str, AgentIdentity] = {}

        async def run(label: str, agent: AgentIdentity) -> None:
            with agent_scope(agent):
                await asyncio.sleep(0)
                seen[label] = current_agent()

        await asyncio.gather(run("x", x), run("y", y))
        assert seen == {"x": x, "y": y}
