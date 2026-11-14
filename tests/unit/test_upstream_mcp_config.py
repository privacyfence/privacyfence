"""The upstream MCP operator file and the admin-config group-to-audience maps."""
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


# ----- group maps ----- #


GROUPS = {
    "kh": {
        "groups": {
            "everyone@company.example": ["general"],
            "hr@company.example": ["hr", "general"],
            "ceo@company.example": ["hr", "finance"],
        }
    }
}
IDS = frozenset({"kh"})


def _groups():
    return cfg.parse_groups(copy.deepcopy(GROUPS), IDS)


class TestParseGroups:
    def test_maps_groups_to_audience_sets(self):
        assert _groups() == {
            "kh": {
                "everyone@company.example": frozenset({"general"}),
                "hr@company.example": frozenset({"hr", "general"}),
                "ceo@company.example": frozenset({"hr", "finance"}),
            }
        }

    def test_undeclared_upstream_is_ignored_with_one_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = cfg.parse_groups({"zz": {"groups": "junk"}}, IDS)
        assert result == {}
        assert "undeclared upstream" in caplog.text

    def test_section_must_be_an_object(self):
        with pytest.raises(ConfigurationError, match="upstream_mcp: must be an object"):
            cfg.parse_groups([], IDS)

    @pytest.mark.parametrize(
        ("section", "path"),
        [
            ({"kh": []}, "upstream_mcp.kh: must be an object"),
            ({"kh": {"zzz": 1}}, "upstream_mcp.kh.zzz"),
            ({"kh": {"groups": []}}, "upstream_mcp.kh.groups"),
            ({"kh": {"groups": {"NoAt": ["a"]}}}, "upstream_mcp.kh.groups"),
            ({"kh": {"groups": {"Upper@Company.example": ["a"]}}}, "upstream_mcp.kh.groups"),
            ({"kh": {"groups": {"a@b.example": "hr"}}}, "upstream_mcp.kh.groups.a@b.example"),
            ({"kh": {"groups": {"a@b.example": ["ok", "Bad Name"]}}}, "upstream_mcp.kh.groups.a@b.example[1]"),
            ({"kh": {"groups": {"a@b.example": [3]}}}, "upstream_mcp.kh.groups.a@b.example[0]"),
            ({"kh": {"groups": {"a@b.example": [f"a{n}" for n in range(21)]}}}, "at most 20 audiences"),
            ({"kh": {"groups": {f"g{n}@b.example": [] for n in range(201)}}}, "at most 200 groups"),
        ],
    )
    def test_invalid(self, section, path):
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_groups(section, IDS)
        assert path in str(info.value)
        assert str(info.value).startswith("admin_config.json: ")

    def test_grants_are_refused(self):
        with pytest.raises(ConfigurationError, match=r"upstream_mcp\.kh\.grants: grants were replaced by groups"):
            cfg.parse_groups({"kh": {"grants": [{"match": "*", "domains": ["general"]}]}}, IDS)

    def test_entry_without_groups_key_is_empty(self):
        assert cfg.parse_groups({"kh": {}}, IDS) == {"kh": {}}


# ----- store ----- #


def _write(path, section, mtime_ns):
    path.write_text(json.dumps({"version": 1, "upstream_mcp": section}), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


class TestGroupMapStore:
    def test_missing_file_means_no_groups(self, tmp_path):
        store = cfg.GroupMapStore(tmp_path / "admin_config.json", IDS)
        assert store.group_audiences("kh") == {}

    def test_missing_key_means_no_groups(self, tmp_path):
        path = tmp_path / "admin_config.json"
        path.write_text('{"version": 1}', encoding="utf-8")
        assert cfg.GroupMapStore(path, IDS).group_audiences("kh") == {}

    def test_reread_on_mtime_change(self, tmp_path):
        path = tmp_path / "admin_config.json"
        _write(path, GROUPS, 1_000_000_000)
        store = cfg.GroupMapStore(path, IDS)
        assert store.group_audiences("kh")["everyone@company.example"] == frozenset({"general"})
        _write(path, {"kh": {"groups": {"everyone@company.example": ["other"]}}}, 2_000_000_000)
        assert store.group_audiences("kh") == {"everyone@company.example": frozenset({"other"})}

    def test_other_upstream_has_no_groups(self, tmp_path):
        path = tmp_path / "admin_config.json"
        _write(path, GROUPS, 1_000_000_000)
        assert cfg.GroupMapStore(path, frozenset({"kh", "wiki"})).group_audiences("wiki") == {}

    def test_bad_edit_keeps_the_last_good_maps_and_logs_once(self, tmp_path, caplog):
        path = tmp_path / "admin_config.json"
        _write(path, GROUPS, 1_000_000_000)
        store = cfg.GroupMapStore(path, IDS)
        _write(path, {"kh": {"groups": {"a@b.example": ["Bad Name"]}}}, 2_000_000_000)
        with caplog.at_level(logging.ERROR):
            assert "everyone@company.example" in store.group_audiences("kh")
            assert "everyone@company.example" in store.group_audiences("kh")
        assert caplog.text.count("keeping the last good group maps") == 1
        _write(path, {"kh": {"groups": {"fixed@b.example": ["fixed"]}}}, 3_000_000_000)
        assert store.group_audiences("kh") == {"fixed@b.example": frozenset({"fixed"})}

    def test_a_grants_key_in_the_file_is_a_bad_section_naming_groups(self, tmp_path):
        path = tmp_path / "admin_config.json"
        _write(path, {"kh": {"grants": []}}, 1_000_000_000)
        with pytest.raises(ConfigurationError, match=r"upstream_mcp\.kh\.grants: grants were replaced by groups"):
            cfg.GroupMapStore(path, IDS)

    def test_bad_section_names_the_groups_path(self, tmp_path):
        path = tmp_path / "admin_config.json"
        _write(path, {"kh": {"groups": {"a@b.example": "hr"}}}, 1_000_000_000)
        with pytest.raises(ConfigurationError, match=r"upstream_mcp\.kh\.groups\.a@b\.example"):
            cfg.GroupMapStore(path, IDS)

    def test_unreadable_and_malformed_files_refuse_the_first_load(self, tmp_path):
        path = tmp_path / "admin_config.json"
        path.write_text("{nope", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not valid JSON"):
            cfg.GroupMapStore(path, IDS)
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="must hold a JSON object"):
            cfg.GroupMapStore(path, IDS)
        directory = tmp_path / "dir"
        directory.mkdir()
        with pytest.raises(ConfigurationError, match="could not be read"):
            cfg.GroupMapStore(directory, IDS)

    def test_undeclared_upstream_warns_once(self, tmp_path, caplog):
        path = tmp_path / "admin_config.json"
        _write(path, {"zz": {"groups": {}}}, 1_000_000_000)
        with caplog.at_level(logging.WARNING):
            store = cfg.GroupMapStore(path, IDS)
            _write(path, {"zz": {"groups": {}}}, 2_000_000_000)
            store.group_audiences("kh")
        assert caplog.text.count("undeclared upstream") == 1


# ----- #
# ----- YAML, inline text, size, repeated keys and settings (ADR 0289)
# ----- #

INLINE = "PRIVACYFENCE_UPSTREAM_MCP"
YAML_DOC = """\
version: 1
upstreams:
  - id: kh
    label: Knowledge hub
    url: https://kh.example.internal/mcp
    audience: https://kh.example.internal
    tools:
      search: {gate: auto, verb: search}
      get_doc: {gate: review, verb: read}
      propose_change: {gate: popup, verb: add, effect: Adds a change proposal.}
"""


def _file(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _refused_file(path):
    with pytest.raises(ConfigurationError) as info:
        cfg.load_upstreams(path)
    return str(info.value)


class TestYamlFile:
    def test_yaml_yml_and_json_load_the_same_upstreams(self, tmp_path):
        expected = _parse(_doc())
        assert cfg.load_upstreams(_file(tmp_path, "u.yaml", YAML_DOC)) == expected
        assert cfg.load_upstreams(_file(tmp_path, "u.YML", YAML_DOC)) == expected
        assert cfg.load_upstreams(_file(tmp_path, "u.json", json.dumps(_doc()))) == expected

    def test_json_text_in_a_yaml_file_loads(self, tmp_path):
        assert cfg.load_upstreams(_file(tmp_path, "u.yaml", json.dumps(_doc())))[0].id == "kh"

    def test_invalid_yaml_does_not_quote_the_file(self, tmp_path):
        message = _refused_file(_file(tmp_path, "u.yaml", "a: [MARKER\n  b: }"))
        assert message == PREFIX + "the file is not valid YAML."

    def test_repeated_tool_name_is_refused_in_yaml_and_json(self, tmp_path):
        yaml_text = YAML_DOC + "      search: {gate: review, verb: read}\n"
        message = _refused_file(_file(tmp_path, "u.yaml", yaml_text))
        assert message == PREFIX + "the file repeats a key."
        json_text = '{"version": 1, "upstreams": [], "version": 1}'
        assert _refused_file(_file(tmp_path, "u.json", json_text)) == PREFIX + "the file repeats a key."

    def test_int_tool_key_is_refused_without_a_type_error(self, tmp_path):
        text = YAML_DOC.replace("      search:", "      1:")
        message = _refused_file(_file(tmp_path, "u.yaml", text))
        assert "tool name must be 1-48 letters" in message

    def test_unquoted_boolean_id_is_refused_at_the_id(self, tmp_path):
        message = _refused_file(_file(tmp_path, "u.yaml", YAML_DOC.replace("id: kh", "id: no")))
        assert "upstreams[0].id" in message

    def test_deeply_nested_text_is_refused_without_recursion_error(self, tmp_path):
        deep = "[" * 100_000
        assert _refused_file(_file(tmp_path, "u.yaml", deep)) == PREFIX + "the file is not valid YAML."
        assert _refused_file(_file(tmp_path, "u.json", deep)) == PREFIX + "the file is not valid JSON."

    def test_merge_key_override_is_accepted(self, tmp_path):
        text = YAML_DOC.replace(
            "      search: {gate: auto, verb: search}\n",
            "      search: {gate: auto, verb: search}\n"
            "      base_read: &base {gate: auto, verb: read}\n"
            "      other: {<<: *base, gate: review}\n",
        )
        tools = {t.name: t for t in cfg.load_upstreams(_file(tmp_path, "u.yaml", text))[0].tools}
        assert (tools["base_read"].gate, tools["other"].gate) == ("auto", "review")

    def test_unhashable_key_is_not_reported_as_repeated(self, tmp_path):
        message = _refused_file(_file(tmp_path, "u.yaml", "? [a]\n: 1\n? [a]\n: 2\n"))
        assert message == PREFIX + "the file is not valid YAML."

    def test_size_limit(self, tmp_path):
        base = json.dumps(_doc())
        ok = base + " " * (262144 - len(base))
        assert cfg.load_upstreams(_file(tmp_path, "ok.json", ok))[0].id == "kh"
        too_big = _file(tmp_path, "big.json", ok + " ")
        assert _refused_file(too_big) == PREFIX + "the file is larger than 256 KiB."


class TestInline:
    def test_yaml_and_one_line_json(self):
        assert cfg.parse_inline(YAML_DOC) == _parse(_doc())
        assert cfg.parse_inline(json.dumps(_doc())) == _parse(_doc())

    def test_errors_carry_the_inline_prefix(self):
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_inline("a: [MARKER")
        assert str(info.value) == INLINE + ": the text is not valid YAML or JSON."
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_inline("a: 1\na: 2\n")
        assert str(info.value) == INLINE + ": the text repeats a key."
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_inline("x" * 262145)
        assert str(info.value) == INLINE + ": the text is larger than 256 KiB."
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_inline("version: 2\nupstreams: []\n")
        assert str(info.value).startswith(INLINE + ": version")


class TestLoadDeclared:
    def test_neither_and_blank_is_none(self, monkeypatch):
        monkeypatch.delenv(cfg.ENV_VAR, raising=False)
        monkeypatch.delenv(cfg.ENV_VAR_INLINE, raising=False)
        assert cfg.load_declared() is None
        monkeypatch.setenv(cfg.ENV_VAR, "  ")
        monkeypatch.setenv(cfg.ENV_VAR_INLINE, "\n")
        assert cfg.load_declared() is None

    def test_file_or_inline(self, monkeypatch, tmp_path):
        monkeypatch.delenv(cfg.ENV_VAR_INLINE, raising=False)
        monkeypatch.setenv(cfg.ENV_VAR, str(_file(tmp_path, "u.yaml", YAML_DOC)))
        assert cfg.load_declared() == _parse(_doc())
        monkeypatch.delenv(cfg.ENV_VAR)
        monkeypatch.setenv(cfg.ENV_VAR_INLINE, YAML_DOC)
        assert cfg.load_declared() == _parse(_doc())

    def test_both_are_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv(cfg.ENV_VAR, str(_file(tmp_path, "u.yaml", YAML_DOC)))
        monkeypatch.setenv(cfg.ENV_VAR_INLINE, YAML_DOC)
        with pytest.raises(ConfigurationError) as info:
            cfg.load_declared()
        assert str(info.value) == "Set PRIVACYFENCE_UPSTREAM_MCP_FILE or PRIVACYFENCE_UPSTREAM_MCP, not both."


class TestOperatorClaimKeys:
    def test_defaults(self):
        (server,) = _parse(_doc())
        assert server.identity_claim == "required"
        assert server.claim_lifetime_seconds == 60
        assert server.allowed_principal_types == ("claude_client", "n8n_workflow")
        assert server.grants == ()

    def test_valid_values(self):
        (server,) = _parse(_doc(identity_claim="off", claim_lifetime_seconds=5, allowed_principal_types=["n8n_workflow"]))
        assert server.identity_claim == "off"
        assert server.claim_lifetime_seconds == 5
        assert server.allowed_principal_types == ("n8n_workflow",)

    @pytest.mark.parametrize("value", ["on", "", 1, None, True])
    def test_identity_claim_refused(self, value):
        _refused(_doc(identity_claim=value), "upstreams[0].identity_claim")

    @pytest.mark.parametrize("value", [4, 61, 0, -1, "60", 5.5, True, None])
    def test_lifetime_refused(self, value):
        _refused(_doc(claim_lifetime_seconds=value), "upstreams[0].claim_lifetime_seconds")

    @pytest.mark.parametrize("value", [[], "claude_client", ["human"], ["service"], [3], None, ["claude_client", "x"]])
    def test_principal_types_refused(self, value):
        _refused(_doc(allowed_principal_types=value), "upstreams[0].allowed_principal_types")

    def test_removed_grants_key_is_flagged_not_seeded(self):
        (server,) = _parse(_doc(grants=[{"match": "*", "domains": ["general"]}]))
        assert len(server.grants) == 1
        _refused(_doc(grants={"match": "*"}), "upstreams[0].grants: must be a list")


class TestSettings:
    def test_defaults(self):
        assert cfg.parse_settings({"version": 1}) == (cfg.ClaimSettings(), cfg.MembershipSettings())

    def test_values(self):
        claim, membership = cfg.parse_settings({
            "claim": {"key_overlap_hours": 720},
            "membership": {"source": "static", "file": "/etc/pf/members.json", "max_additions": 1000},
        })
        assert claim == cfg.ClaimSettings(720)
        assert membership == cfg.MembershipSettings("static", "/etc/pf/members.json", 1000)

    def test_parse_upstreams_accepts_the_top_level_keys(self):
        data = _doc()
        data["claim"] = {"key_overlap_hours": 1}
        data["membership"] = {"source": "snapshot"}
        assert len(_parse(data)) == 1

    @pytest.mark.parametrize(
        ("data", "path"),
        [
            ([], "$"),
            ({"claim": []}, "claim:"),
            ({"claim": {"zzz": 1}}, "claim.zzz"),
            ({"claim": {"key_overlap_hours": 0}}, "claim.key_overlap_hours"),
            ({"claim": {"key_overlap_hours": 721}}, "claim.key_overlap_hours"),
            ({"claim": {"key_overlap_hours": "24"}}, "claim.key_overlap_hours"),
            ({"membership": []}, "membership:"),
            ({"membership": {"zzz": 1}}, "membership.zzz"),
            ({"membership": {"source": "ldap"}}, "membership.source"),
            ({"membership": {"source": "static"}}, "membership.file"),
            ({"membership": {"source": "static", "file": ""}}, "membership.file"),
            ({"membership": {"source": "snapshot", "file": "/x"}}, "membership.file"),
            ({"membership": {"max_additions": 0}}, "membership.max_additions"),
            ({"membership": {"max_additions": 1001}}, "membership.max_additions"),
            ({"membership": {"max_additions": True}}, "membership.max_additions"),
        ],
    )
    def test_refused(self, data, path):
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_settings(data)
        assert path in str(info.value)

    def test_bad_settings_also_refuse_parse_upstreams_keys(self):
        data = _doc()
        data["zzz"] = 1
        _refused(data, "zzz")

    def test_load_settings_from_a_path(self, tmp_path):
        path = tmp_path / "up.json"
        data = _doc()
        data["claim"] = {"key_overlap_hours": 48}
        path.write_text(json.dumps(data), encoding="utf-8")
        assert cfg.load_settings(path)[0] == cfg.ClaimSettings(48)

    def test_load_settings_from_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.delenv(cfg.ENV_VAR, raising=False)
        monkeypatch.delenv(cfg.ENV_VAR_INLINE, raising=False)
        assert cfg.load_settings() == (cfg.ClaimSettings(), cfg.MembershipSettings())
        data = _doc()
        data["membership"] = {"max_additions": 7}
        monkeypatch.setenv(cfg.ENV_VAR_INLINE, json.dumps(data))
        assert cfg.load_settings()[1].max_additions == 7
        path = tmp_path / "up.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setenv(cfg.ENV_VAR, str(path))
        with pytest.raises(ConfigurationError, match="not both"):
            cfg.load_settings()
