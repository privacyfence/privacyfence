"""The version 2 upstream MCP operator file."""
from __future__ import annotations

import json

import pytest

from privacyfence import paths
from privacyfence import upstream_mcp_config as cfg
from privacyfence.org_mode import ConfigurationError

PREFIX = "PRIVACYFENCE_UPSTREAM_MCP_FILE: "
V1_TEXT = (
    "this release reads version 2 only; version 1 files (tools, identity claim, audiences) "
    "are no longer supported, see docs/upstream-mcp.md."
)


def _server(**upstream):
    server = {"id": "canva", "label": "Canva", "url": "https://mcp.canva.com/mcp"}
    server.update(upstream)
    return server


def _doc(**upstream):
    return {"version": 2, "upstreams": [_server(**upstream)]}


def _parse(data):
    return cfg.parse_upstreams(data, source=cfg.ENV_VAR)


def _refused(data, expected):
    with pytest.raises(ConfigurationError) as info:
        _parse(data)
    assert str(info.value) == PREFIX + expected


class TestValidFile:
    def test_defaults(self):
        (server,) = _parse(_doc())
        assert (server.id, server.label, server.url) == ("canva", "Canva", "https://mcp.canva.com/mcp")
        assert server.allowed_principal_types == ("claude_client", "n8n_workflow")
        assert server.oauth == cfg.UpstreamOAuthSettings()
        assert server.oauth.scopes == ()
        assert (server.oauth.client_id, server.oauth.client_secret_env, server.oauth.issuer) == (None, None, None)

    def test_every_key(self):
        oauth = {
            "scopes": ["a:read", "b"], "client_id": "abc", "client_secret_env": "CANVA_SECRET",
            "issuer": "https://auth.example.com/tenant",
        }
        (server,) = _parse(_doc(oauth=oauth, allowed_principal_types=["n8n_workflow"]))
        assert server.oauth == cfg.UpstreamOAuthSettings(
            scopes=("a:read", "b"), client_id="abc", client_secret_env="CANVA_SECRET",
            issuer="https://auth.example.com/tenant",
        )
        assert server.allowed_principal_types == ("n8n_workflow",)

    def test_principal_types_are_sorted_and_deduplicated(self):
        (server,) = _parse(_doc(allowed_principal_types=["n8n_workflow", "claude_client", "n8n_workflow"]))
        assert server.allowed_principal_types == ("claude_client", "n8n_workflow")

    def test_empty_upstream_list(self):
        assert _parse({"version": 2, "upstreams": []}) == ()

    def test_eight_upstreams(self):
        data = {"version": 2, "upstreams": [_server(id=f"srv{i}") for i in range(8)]}
        assert len(_parse(data)) == 8

    def test_load_json_file(self, tmp_path):
        path = tmp_path / "up.json"
        path.write_text(json.dumps(_doc()), encoding="utf-8")
        assert cfg.load_upstreams(path)[0].id == "canva"

    @pytest.mark.parametrize("name", ["up.yaml", "up.YML"])
    def test_load_yaml_file(self, tmp_path, name):
        path = tmp_path / name
        path.write_text(
            "version: 2\nupstreams:\n  - id: framer\n    label: Framer\n    url: https://mcp.framer.com/mcp\n",
            encoding="utf-8",
        )
        assert cfg.load_upstreams(path)[0].id == "framer"

    def test_inline_yaml_and_json(self):
        yaml_text = "version: 2\nupstreams:\n  - {id: canva, label: Canva, url: 'https://mcp.canva.com/mcp'}\n"
        assert cfg.parse_inline(yaml_text)[0].id == "canva"
        assert cfg.parse_inline(json.dumps(_doc()))[0].id == "canva"


class TestLoadDeclared:
    def test_none_when_unset(self, monkeypatch):
        monkeypatch.delenv(cfg.ENV_VAR, raising=False)
        monkeypatch.delenv(cfg.ENV_VAR_INLINE, raising=False)
        assert cfg.load_declared() is None

    def test_file_variable(self, monkeypatch, tmp_path):
        path = tmp_path / "up.json"
        path.write_text(json.dumps(_doc()), encoding="utf-8")
        monkeypatch.setenv(cfg.ENV_VAR, str(path))
        monkeypatch.delenv(cfg.ENV_VAR_INLINE, raising=False)
        assert cfg.load_declared()[0].id == "canva"

    def test_inline_variable(self, monkeypatch):
        monkeypatch.delenv(cfg.ENV_VAR, raising=False)
        monkeypatch.setenv(cfg.ENV_VAR_INLINE, json.dumps(_doc()))
        assert cfg.load_declared()[0].id == "canva"

    def test_both_set_is_refused(self, monkeypatch):
        monkeypatch.setenv(cfg.ENV_VAR, "/nowhere.json")
        monkeypatch.setenv(cfg.ENV_VAR_INLINE, "version: 2")
        with pytest.raises(ConfigurationError) as info:
            cfg.load_declared()
        assert str(info.value) == f"Set {cfg.ENV_VAR} or {cfg.ENV_VAR_INLINE}, not both."


class TestFileProblems:
    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigurationError, match="the file could not be read"):
            cfg.load_upstreams(tmp_path / "absent.json")

    def test_not_utf8(self, tmp_path):
        path = tmp_path / "up.json"
        path.write_bytes(b"\xff\xfe\x00")
        with pytest.raises(ConfigurationError, match="the file could not be read"):
            cfg.load_upstreams(path)

    def test_file_over_256_kib(self, tmp_path):
        path = tmp_path / "up.json"
        path.write_bytes(b" " * (256 * 1024 + 1))
        with pytest.raises(ConfigurationError) as info:
            cfg.load_upstreams(path)
        assert str(info.value) == f"{cfg.ENV_VAR}: the file is larger than 256 KiB."

    def test_inline_over_256_kib(self):
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_inline("#" * (256 * 1024 + 1))
        assert str(info.value) == f"{cfg.ENV_VAR_INLINE}: the text is larger than 256 KiB."

    def test_invalid_json_and_yaml(self, tmp_path):
        path = tmp_path / "up.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="the file is not valid JSON"):
            cfg.load_upstreams(path)
        path = tmp_path / "up.yaml"
        path.write_text("a: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="the file is not valid YAML"):
            cfg.load_upstreams(path)
        with pytest.raises(ConfigurationError, match="the text is not valid YAML or JSON"):
            cfg.parse_inline("a: [unclosed")

    def test_repeated_json_key(self, tmp_path):
        path = tmp_path / "up.json"
        path.write_text('{"version": 2, "version": 2, "upstreams": []}', encoding="utf-8")
        with pytest.raises(ConfigurationError, match="the file repeats a key"):
            cfg.load_upstreams(path)

    def test_repeated_yaml_key(self):
        text = "version: 2\nupstreams:\n  - id: a1\n    id: b2\n"
        with pytest.raises(ConfigurationError, match="the text repeats a key"):
            cfg.parse_inline(text)

    def test_yaml_merge_key_and_unhashable_key_are_not_repeats(self):
        merged = "base: &b {id: canva}\nversion: 2\nupstreams: []\nextra:\n  <<: *b\n  x: 1\n"
        with pytest.raises(ConfigurationError, match="is not a known key"):
            cfg.parse_inline(merged)
        with pytest.raises(ConfigurationError, match="the text is not valid YAML or JSON"):
            cfg.parse_inline("? [a, b]\n: 1\n? [a, b]\n: 2\n")

    def test_no_message_quotes_the_text(self):
        with pytest.raises(ConfigurationError) as info:
            cfg.parse_inline("secret-value: [unclosed")
        assert "secret-value" not in str(info.value)


class TestTopLevel:
    def test_not_an_object(self):
        _refused([], "$: must be a JSON object")

    def test_version_1(self):
        _refused({"version": 1, "upstreams": []}, "version: " + V1_TEXT)

    @pytest.mark.parametrize("version", [3, 0, "2", 2.0, True, None])
    def test_other_versions(self, version):
        _refused({"version": version, "upstreams": []}, "version: must be 2")

    def test_missing_version(self):
        _refused({"upstreams": []}, "version: must be 2")

    @pytest.mark.parametrize("key", ["claim", "membership", "tools", "audience"])
    def test_unknown_top_level_key(self, key):
        _refused({"version": 2, "upstreams": [], key: {}}, f"{key}: is not a known key")

    def test_upstreams_must_be_a_list(self):
        _refused({"version": 2, "upstreams": {}}, "upstreams: must be a list")
        _refused({"version": 2}, "upstreams: must be a list")

    def test_more_than_eight_upstreams(self):
        data = {"version": 2, "upstreams": [_server(id=f"srv{i}") for i in range(9)]}
        _refused(data, "upstreams: may list at most 8 upstreams")

    def test_an_upstream_must_be_an_object(self):
        _refused({"version": 2, "upstreams": ["x"]}, "upstreams[0]: must be an object")

    def test_error_names_the_index(self):
        data = {"version": 2, "upstreams": [_server(), _server(id="framer", label="")]}
        _refused(data, "upstreams[1].label: must be 1-60 characters with no control characters")


class TestUpstreamKeys:
    @pytest.mark.parametrize(
        "key", ["tools", "audience", "identity_claim", "claim_lifetime_seconds", "audiences_tool", "grants"]
    )
    def test_removed_keys_are_unknown(self, key):
        _refused(_doc(**{key: "x"}), f"upstreams[0].{key}: is not a known key")

    @pytest.mark.parametrize("upstream_id", ["a", "A1", "1ab", "ab_c", "abcdefghijklmnopq", "", 5, None])
    def test_bad_id(self, upstream_id):
        _refused(
            _doc(id=upstream_id),
            "upstreams[0].id: must be 2-16 lowercase letters or digits, starting with a letter",
        )

    def test_missing_id(self):
        data = {"version": 2, "upstreams": [{"label": "x", "url": "https://a.example.com"}]}
        _refused(data, "upstreams[0].id: must be 2-16 lowercase letters or digits, starting with a letter")

    def test_duplicate_id(self):
        data = {"version": 2, "upstreams": [_server(), _server()]}
        _refused(data, "upstreams[1].id: is not unique")

    @pytest.mark.parametrize("name", ["gmail", "slack", "miro", "drive", "privacyfence"])
    def test_id_collides_with_a_builtin_connector(self, name):
        _refused(_doc(id=name), f'upstreams[0].id: upstream id "{name}" collides with the built-in connector "{name}"')

    @pytest.mark.parametrize("name", ["docs", "sheets"])
    def test_id_collides_with_a_drive_alias(self, name):
        _refused(_doc(id=name), f'upstreams[0].id: upstream id "{name}" collides with the built-in connector "drive"')

    @pytest.mark.parametrize("label", ["", "x" * 61, "a\nb", "a\x00b", 5, None])
    def test_bad_label(self, label):
        _refused(_doc(label=label), "upstreams[0].label: must be 1-60 characters with no control characters")

    def test_label_limits_are_accepted(self):
        assert _parse(_doc(label="x"))[0].label == "x"
        assert _parse(_doc(label="x" * 60))[0].label == "x" * 60

    @pytest.mark.parametrize(
        "url",
        [
            "http://mcp.example.com/mcp", "http://example/mcp", "ftp://example.com", "mcp.example.com",
            "https://user@example.com/mcp", "https://user:pw@example.com/mcp", "https://example.com/mcp#frag",
            "https://example.com/m cp", "https://example.com:notaport/mcp", "https:///mcp", "http://10.0.0.5/mcp",
            "", 5, None,
        ],
    )
    def test_bad_url(self, url):
        _refused(
            _doc(url=url),
            "upstreams[0].url: must be an https:// address (http:// only for localhost or a loopback address) "
            "with no user name or fragment",
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://mcp.example.com/mcp?x=1", "https://10.0.0.5/mcp", "http://localhost:8080/mcp",
            "http://127.0.0.1/mcp", "http://[::1]:9000/mcp",
        ],
    )
    def test_good_url(self, url):
        assert _parse(_doc(url=url))[0].url == url

    @pytest.mark.parametrize(
        "types",
        [[], "claude_client", ["bot"], ["claude_client", 5], None, {"a": 1}],
    )
    def test_bad_principal_types(self, types):
        _refused(
            _doc(allowed_principal_types=types),
            "upstreams[0].allowed_principal_types: must be a non-empty list of claude_client and n8n_workflow",
        )


SCOPES_PROBLEM = "must be a list of 1-20 scopes, each 1-100 characters with no whitespace"
CLIENT_ID_PROBLEM = "must be 1-200 characters with no whitespace"
ENV_PROBLEM = "must be an environment variable name in capital letters, digits and underscores"
ISSUER_PROBLEM = "must be an https:// address with no user name, query or fragment"


class TestOAuth:
    @pytest.mark.parametrize("oauth", [[], "x", 5, None])
    def test_oauth_must_be_an_object(self, oauth):
        _refused(_doc(oauth=oauth), "upstreams[0].oauth: must be an object")

    def test_empty_oauth_is_fine(self):
        assert _parse(_doc(oauth={}))[0].oauth == cfg.UpstreamOAuthSettings()

    def test_unknown_oauth_key(self):
        _refused(_doc(oauth={"audience": "x"}), "upstreams[0].oauth.audience: is not a known key")

    @pytest.mark.parametrize(
        "scopes",
        [[], "a b", ["a"] * 21, [""], ["x" * 101], ["a b"], ["a\tb"], ["a\x00"], ["a", 5], None, {}],
    )
    def test_bad_scopes(self, scopes):
        _refused(_doc(oauth={"scopes": scopes}), f"upstreams[0].oauth.scopes: {SCOPES_PROBLEM}")

    def test_scope_limits_are_accepted(self):
        assert _parse(_doc(oauth={"scopes": ["a"] * 20}))[0].oauth.scopes == ("a",) * 20
        assert _parse(_doc(oauth={"scopes": ["x" * 100]}))[0].oauth.scopes == ("x" * 100,)

    @pytest.mark.parametrize("client_id", ["", "x" * 201, "a b", "a\nb", 5, None])
    def test_bad_client_id(self, client_id):
        _refused(_doc(oauth={"client_id": client_id}), f"upstreams[0].oauth.client_id: {CLIENT_ID_PROBLEM}")

    def test_client_id_limits_are_accepted(self):
        assert _parse(_doc(oauth={"client_id": "x" * 200}))[0].oauth.client_id == "x" * 200

    @pytest.mark.parametrize("name", ["", "lower", "1ABC", "A B", "A" * 65, "A-B", 5, None])
    def test_bad_client_secret_env(self, name):
        _refused(
            _doc(oauth={"client_id": "abc", "client_secret_env": name}),
            f"upstreams[0].oauth.client_secret_env: {ENV_PROBLEM}",
        )

    def test_client_secret_env_needs_client_id(self):
        _refused(_doc(oauth={"client_secret_env": "SECRET"}), "upstreams[0].oauth.client_secret_env: needs client_id")

    def test_client_secret_env_limits_are_accepted(self):
        name = "A" + "0" * 63
        oauth = {"client_id": "abc", "client_secret_env": name}
        assert _parse(_doc(oauth=oauth))[0].oauth.client_secret_env == name

    @pytest.mark.parametrize(
        "issuer",
        [
            "http://auth.example.com", "auth.example.com", "https://user@auth.example.com",
            "https://auth.example.com/?x=1", "https://auth.example.com?", "https://auth.example.com/#f",
            "ftp://auth.example.com", "", 5, None,
        ],
    )
    def test_bad_issuer(self, issuer):
        _refused(_doc(oauth={"issuer": issuer}), f"upstreams[0].oauth.issuer: {ISSUER_PROBLEM}")

    @pytest.mark.parametrize(
        "issuer", ["https://auth.example.com", "https://auth.example.com/tenant/1", "http://localhost:9000"]
    )
    def test_good_issuer(self, issuer):
        assert _parse(_doc(oauth={"issuer": issuer}))[0].oauth.issuer == issuer


class TestLeftovers:
    @pytest.fixture(autouse=True)
    def _data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        self.org = tmp_path / "org"
        self.org.mkdir()

    def test_nothing_left(self):
        assert cfg.leftovers() == []
        assert cfg.leftovers({}) == []
        assert cfg.leftovers({"other": {}}) == []

    def test_claim_keys(self):
        (self.org / "upstream_claim_keys.json").write_text("{}", encoding="utf-8")
        assert cfg.leftovers() == [
            "Left over from the removed claim-based upstream feature: "
            "org/upstream_claim_keys.json (signing keys; delete it)"
        ]

    def test_group_membership(self):
        (self.org / "group_membership.json").write_text("{}", encoding="utf-8")
        assert cfg.leftovers() == [
            "Left over from the removed claim-based upstream feature: org/group_membership.json (delete it)"
        ]

    def test_admin_config_section(self):
        assert cfg.leftovers({"upstream_mcp": {}}) == [
            "admin_config.json holds an upstream_mcp section that is no longer used"
        ]

    def test_all_three(self):
        (self.org / "upstream_claim_keys.json").write_text("{}", encoding="utf-8")
        (self.org / "group_membership.json").write_text("{}", encoding="utf-8")
        found = cfg.leftovers({"upstream_mcp": {"kh": {}}})
        assert len(found) == 3
        assert found[2].startswith("admin_config.json holds")
