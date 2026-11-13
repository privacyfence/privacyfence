"""The upstream MCP operator file and the admin-config domain grants."""
from __future__ import annotations

import copy
import json
import logging
import os

import pytest

from privacyfence import upstream_mcp_config as cfg
from privacyfence.org_mode import ConfigurationError

PREFIX = "PRIVACYFENCE_UPSTREAM_MCP_FILE: "


def _doc(**upstream):
    server = {
        "id": "kh",
        "label": "Knowledge hub",
        "url": "https://kh.example.internal/mcp",
        "audience": "https://kh.example.internal",
        "tools": {
            "search": {"gate": "auto", "verb": "search"},
            "get_doc": {"gate": "review", "verb": "read"},
            "propose_change": {"gate": "popup", "verb": "add", "effect": "Adds a change proposal."},
        },
    }
    server.update(upstream)
    return {"version": 1, "upstreams": [server]}


def _parse(data):
    return cfg.parse_upstreams(data, source=cfg.ENV_VAR)


def _refused(data, path, secret=None):
    with pytest.raises(ConfigurationError) as info:
        _parse(data)
    message = str(info.value)
    assert message.startswith(PREFIX)
    assert path in message
    if secret is not None:
        assert secret not in message
    return message


class TestValidFile:
    def test_parses(self):
        (server,) = _parse(_doc())
        assert (server.id, server.label, server.audience) == ("kh", "Knowledge hub", "https://kh.example.internal")
        assert [t.name for t in server.tools] == ["search", "get_doc", "propose_change"]
        tool = server.tools[2]
        assert (tool.exposed_name, tool.operation, tool.gate, tool.verb) == ("kh_propose_change", "kh.propose_change", "popup", "add")
        assert tool.effect == "Adds a change proposal."
        assert server.tools[0].effect == ""

    def test_load_from_file(self, tmp_path):
        path = tmp_path / "up.json"
        path.write_text(json.dumps(_doc()), encoding="utf-8")
        assert cfg.load_upstreams(path)[0].id == "kh"

    def test_load_unreadable_and_bad_json(self, tmp_path):
        with pytest.raises(ConfigurationError, match="could not be read"):
            cfg.load_upstreams(tmp_path / "missing.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{nope SECRETVALUE", encoding="utf-8")
        with pytest.raises(ConfigurationError) as info:
            cfg.load_upstreams(bad)
        assert "not valid JSON" in str(info.value) and "SECRETVALUE" not in str(info.value)

    def test_no_upstreams_is_fine(self):
        assert _parse({"version": 1, "upstreams": []}) == ()


class TestRefusals:
    def test_top_level_not_object(self):
        _refused([], "$")

    @pytest.mark.parametrize("version", [2, True, "1", None])
    def test_version(self, version):
        data = _doc()
        data["version"] = version
        _refused(data, "version")

    def test_unknown_top_level_key(self):
        data = _doc()
        data["extraKey"] = 1
        _refused(data, "extraKey")

    def test_upstreams_not_list(self):
        _refused({"version": 1, "upstreams": {}}, "upstreams")

    def test_too_many_upstreams(self):
        servers = [{"id": f"srv{n}"} for n in range(9)]
        _refused({"version": 1, "upstreams": servers}, "upstreams")

    def test_upstream_not_object(self):
        _refused({"version": 1, "upstreams": ["x"]}, "upstreams[0]")

    def test_unknown_upstream_key(self):
        data = _doc(bogus=1)
        _refused(data, "upstreams[0].bogus")

    @pytest.mark.parametrize("bad", ["K", "k", "1ab", "a_b", "a" * 17, 5, ""])
    def test_id_shape(self, bad):
        message = _refused(_doc(id=bad), "upstreams[0].id")
        if isinstance(bad, str) and bad:
            assert bad not in message or len(bad) == 1

    def test_id_not_unique(self):
        data = _doc()
        data["upstreams"].append(copy.deepcopy(data["upstreams"][0]))
        _refused(data, "upstreams[1].id")

    @pytest.mark.parametrize("bad", ["slack", "privacyfence", "apps", "upstream"])
    def test_id_collides(self, bad):
        message = _refused(_doc(id=bad), "upstreams[0].id")
        assert f'upstream id "{bad}" collides with the built-in connector' in message

    @pytest.mark.parametrize("ok", ["slac", "slackx", "kh"])
    def test_id_no_collision(self, ok):
        assert _parse(_doc(id=ok))[0].id == ok

    @pytest.mark.parametrize("bad", ["", "x" * 61, "a\nb", 3])
    def test_label(self, bad):
        _refused(_doc(label=bad), "upstreams[0].label")

    @pytest.mark.parametrize(
        "bad",
        [
            "ftp://kh.example.com/mcp",
            "http://kh.example.com/mcp",
            "https://user:pw@kh.example.com/mcp",
            "https://kh.example.com/mcp#frag",
            "https://kh.example.com:notaport/mcp",
            "https:///mcp",
            "https://kh .example.com/mcp",
            "http://2130706433/mcp",
            "http://10.0.0.5/mcp",
            7,
        ],
    )
    def test_url_refused(self, bad):
        message = _refused(_doc(url=bad), "upstreams[0].url")
        if isinstance(bad, str):
            assert bad not in message

    @pytest.mark.parametrize(
        "ok",
        [
            "https://kh.example.com/mcp",
            "http://localhost:8080/mcp",
            "http://127.0.0.1:8080/mcp",
            "http://[::1]:8080/mcp",
            "http://knowledge-hub:8080/mcp",
        ],
    )
    def test_url_allowed(self, ok):
        assert _parse(_doc(url=ok))[0].url == ok

    def test_http_remote_host_refused(self):
        _refused(_doc(url="http://kh.example.com/mcp"), "upstreams[0].url")

    @pytest.mark.parametrize("bad", ["", "a b", "x" * 201, "a\tb", None])
    def test_audience(self, bad):
        _refused(_doc(audience=bad), "upstreams[0].audience")

    def test_tools_not_object(self):
        _refused(_doc(tools=[]), "upstreams[0].tools")

    def test_too_many_tools(self):
        tools = {f"t{n}": {"gate": "auto", "verb": "read"} for n in range(51)}
        _refused(_doc(tools=tools), "upstreams[0].tools")

    def test_tool_name(self):
        _refused(_doc(tools={"bad name": {"gate": "auto", "verb": "read"}}), "upstreams[0].tools.bad name")

    def test_exposed_name_too_long(self):
        name = "t" * 48
        _refused(_doc(id="abcdefghijklmnop", tools={name: {"gate": "auto", "verb": "read"}}), f"tools.{name}")

    def test_tool_not_object(self):
        _refused(_doc(tools={"search": "x"}), "upstreams[0].tools.search")

    def test_unknown_tool_key(self):
        _refused(_doc(tools={"search": {"gate": "auto", "verb": "read", "zzz": 1}}), "tools.search.zzz")

    @pytest.mark.parametrize("gate", ["", "none", 5, None])
    def test_gate_value(self, gate):
        _refused(_doc(tools={"search": {"gate": gate, "verb": "read"}}), "upstreams[0].tools.search.gate")

    @pytest.mark.parametrize("verb", ["", "write", 5, None])
    def test_verb_value(self, verb):
        _refused(_doc(tools={"search": {"gate": "auto", "verb": verb}}), "upstreams[0].tools.search.verb")


class TestVerbGateEffect:
    @pytest.mark.parametrize("verb", sorted(cfg.READ_VERBS))
    def test_read_verb_with_popup_refused(self, verb):
        _refused(_doc(tools={"t": {"gate": "popup", "verb": verb}}), "tools.t.gate")

    def test_read_verb_with_effect_refused(self):
        _refused(_doc(tools={"t": {"gate": "auto", "verb": "read", "effect": "Reads."}}), "tools.t.effect")

    @pytest.mark.parametrize("verb", ["add", "update", "restructure", "move", "send", "delete"])
    @pytest.mark.parametrize("gate", ["auto", "review"])
    def test_write_verb_needs_popup(self, verb, gate):
        _refused(_doc(tools={"t": {"gate": gate, "verb": verb, "effect": "Does it."}}), "tools.t.gate")

    @pytest.mark.parametrize("effect", [None, "", "No full stop", "x" * 200 + ".", 4])
    def test_write_verb_needs_effect(self, effect):
        tool = {"gate": "popup", "verb": "update"}
        if effect is not None:
            tool["effect"] = effect
        message = _refused(_doc(tools={"t": tool}), "tools.t.effect")
        if isinstance(effect, str) and effect:
            assert effect not in message

    def test_every_read_verb_allowed_with_auto_and_review(self):
        for verb in cfg.READ_VERBS:
            for gate in ("auto", "review"):
                assert _parse(_doc(tools={"t": {"gate": gate, "verb": verb}}))[0].tools[0].gate == gate


# ----- grants ----- #


GRANTS = {
    "kh": {
        "grants": [
            {"match": "*", "domains": ["general"]},
            {"match": "@HR.example.com", "domains": ["hr"]},
            {"match": "CEO@example.com", "domains": ["hr", "finance"]},
        ]
    }
}
IDS = frozenset({"kh"})


def _grants():
    return cfg.parse_grants(copy.deepcopy(GRANTS), IDS)


class TestDomainsFor:
    def test_star_matches_everyone_verified(self):
        assert cfg.domains_for(_grants(), "kh", "x@other.com", True) == ("general",)

    def test_domain_match_is_case_insensitive_and_sorted_union(self):
        assert cfg.domains_for(_grants(), "kh", "Bob@hr.EXAMPLE.com", True) == ("general", "hr")

    def test_exact_email_case_insensitive(self):
        assert cfg.domains_for(_grants(), "kh", " ceo@EXAMPLE.com ", True) == ("finance", "general", "hr")

    def test_unverified_gives_nothing(self):
        assert cfg.domains_for(_grants(), "kh", "ceo@example.com", False) == ()

    def test_empty_email_gives_nothing(self):
        assert cfg.domains_for(_grants(), "kh", "", True) == ()

    def test_unknown_upstream_gives_nothing(self):
        assert cfg.domains_for(_grants(), "other", "ceo@example.com", True) == ()


class TestParseGrants:
    def test_undeclared_id_warned_and_ignored(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = cfg.parse_grants({"zz": {"grants": "junk"}}, IDS)
        assert result == {}
        assert "zz" in caplog.text

    def test_section_not_object(self):
        with pytest.raises(ConfigurationError, match="upstream_mcp"):
            cfg.parse_grants([], IDS)

    @pytest.mark.parametrize(
        ("section", "path"),
        [
            ({"kh": []}, "upstream_mcp.kh"),
            ({"kh": {"nope": 1}}, "upstream_mcp.kh.nope"),
            ({"kh": {"grants": {}}}, "upstream_mcp.kh.grants"),
            ({"kh": {"grants": [{"match": "*", "domains": []}] * 201}}, "upstream_mcp.kh.grants"),
            ({"kh": {"grants": ["x"]}}, "upstream_mcp.kh.grants[0]"),
            ({"kh": {"grants": [{"match": "*", "domains": [], "zzz": 1}]}}, "upstream_mcp.kh.grants[0].zzz"),
            ({"kh": {"grants": [{"match": "a@b@c", "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
            ({"kh": {"grants": [{"match": "@", "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
            ({"kh": {"grants": [{"match": "@a@b", "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
            ({"kh": {"grants": [{"match": "a b@c.com", "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
            ({"kh": {"grants": [{"match": "nodomain", "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
            ({"kh": {"grants": [{"match": "@@x.com", "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
            ({"kh": {"grants": [{"match": "a@", "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
            ({"kh": {"grants": [{"match": "@c.com", "domains": "hr"}]}}, "upstream_mcp.kh.grants[0].domains"),
            ({"kh": {"grants": [{"match": "*", "domains": ["ok", "Bad Name"]}]}}, "upstream_mcp.kh.grants[0].domains[1]"),
            ({"kh": {"grants": [{"match": "*", "domains": [3]}]}}, "upstream_mcp.kh.grants[0].domains[0]"),
            ({"kh": {"grants": [{"match": 3, "domains": []}]}}, "upstream_mcp.kh.grants[0].match"),
        ],
    )
    def test_invalid(self, section, path):
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_grants(section, IDS)
        assert path in str(info.value)
        assert str(info.value).startswith("admin_config.json: ")

    def test_entry_without_grants_key_is_empty(self):
        assert cfg.parse_grants({"kh": {}}, IDS) == {"kh": ()}


# ----- store ----- #


def _write(path, section, mtime_ns):
    path.write_text(json.dumps({"version": 1, "upstream_mcp": section}), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


class TestGrantStore:
    def test_missing_file_means_no_grants(self, tmp_path):
        store = cfg.GrantStore(tmp_path / "admin_config.json", IDS)
        assert store.domains_for("kh", "a@b.com", True) == ()

    def test_missing_key_means_no_grants(self, tmp_path):
        path = tmp_path / "admin_config.json"
        path.write_text('{"version": 1}', encoding="utf-8")
        assert cfg.GrantStore(path, IDS).domains_for("kh", "a@b.com", True) == ()

    def test_reread_on_mtime_change(self, tmp_path):
        path = tmp_path / "admin_config.json"
        _write(path, GRANTS, 1_000_000_000)
        store = cfg.GrantStore(path, IDS)
        assert store.domains_for("kh", "a@b.com", True) == ("general",)
        _write(path, {"kh": {"grants": [{"match": "*", "domains": ["other"]}]}}, 2_000_000_000)
        assert store.domains_for("kh", "a@b.com", True) == ("other",)

    def test_unchanged_mtime_does_not_reread(self, tmp_path):
        path = tmp_path / "admin_config.json"
        _write(path, GRANTS, 1_000_000_000)
        store = cfg.GrantStore(path, IDS)
        _write(path, {"kh": {"grants": []}}, 1_000_000_000)
        assert store.domains_for("kh", "a@b.com", True) == ("general",)

    def test_bad_reread_keeps_last_good(self, tmp_path, caplog):
        path = tmp_path / "admin_config.json"
        _write(path, GRANTS, 1_000_000_000)
        store = cfg.GrantStore(path, IDS)
        _write(path, {"kh": {"grants": [{"match": "*", "domains": ["Bad Name"]}]}}, 2_000_000_000)
        with caplog.at_level(logging.ERROR):
            assert store.domains_for("kh", "a@b.com", True) == ("general",)
            assert store.domains_for("kh", "a@b.com", True) == ("general",)
        assert caplog.text.count("keeping the last good grants") == 1
        _write(path, {"kh": {"grants": [{"match": "*", "domains": ["fixed"]}]}}, 3_000_000_000)
        assert store.domains_for("kh", "a@b.com", True) == ("fixed",)

    def test_bad_first_load_raises(self, tmp_path):
        path = tmp_path / "admin_config.json"
        _write(path, {"kh": {"grants": ["x"]}}, 1_000_000_000)
        with pytest.raises(ConfigurationError, match=r"upstream_mcp\.kh\.grants\[0\]"):
            cfg.GrantStore(path, IDS)

    def test_unreadable_json_and_non_object(self, tmp_path):
        path = tmp_path / "admin_config.json"
        path.write_text("{nope", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not valid JSON"):
            cfg.GrantStore(path, IDS)
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="JSON object"):
            cfg.GrantStore(path, IDS)

    def test_unreadable_file(self, tmp_path):
        directory = tmp_path / "admin_config.json"
        directory.mkdir()
        with pytest.raises(ConfigurationError, match="could not be read"):
            cfg.GrantStore(directory, IDS)

    def test_undeclared_id_warned_once(self, tmp_path, caplog):
        path = tmp_path / "admin_config.json"
        _write(path, {"zz": {"grants": []}}, 1_000_000_000)
        with caplog.at_level(logging.WARNING):
            store = cfg.GrantStore(path, IDS)
            _write(path, {"zz": {"grants": []}}, 2_000_000_000)
            store.domains_for("kh", "a@b.com", True)
        assert caplog.text.count("undeclared upstream") == 1
