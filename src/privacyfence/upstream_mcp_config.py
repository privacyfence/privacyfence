"""The operator's upstream MCP file (version 2): hosted MCP servers that sign each person in with OAuth.

An operator declares each hosted MCP server ("upstream") in a YAML or JSON file named by
``PRIVACYFENCE_UPSTREAM_MCP_FILE``, or inline in ``PRIVACYFENCE_UPSTREAM_MCP``, and it is read once at
startup. An upstream has an ``id``, a ``label``, a ``url``, optional ``allowed_principal_types`` and
optional ``oauth`` settings (``scopes``, ``client_id``, ``client_secret_env``, ``issuer``). The file no
longer declares tools: an administrator reviews them on the Admin page. A version 1 file (the removed
claim-based feature) is refused at start.

Any problem raises ``org_mode.ConfigurationError``, which refuses the start; the message names the path
of the bad value (``<source>: upstreams[<i>].<key>: <problem>``) and never quotes the value. An upstream
id may not collide with a built-in connector name.

``leftovers`` names what the removed claim-based feature left on disk; the server only warns, it never
reads or deletes those files.

See ADR 0329 (hosted MCP servers sign in each person with their own OAuth).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from . import paths
from .org_mode import CONNECTOR_ORG_SECTION, ConfigurationError

ENV_VAR = "PRIVACYFENCE_UPSTREAM_MCP_FILE"
ENV_VAR_INLINE = "PRIVACYFENCE_UPSTREAM_MCP"
BUILTIN_NAMES = frozenset(CONNECTOR_ORG_SECTION) | {"privacyfence", "upstream_mcp"}
# The policy maps these operation prefixes to Drive, so an upstream may not use them.
_ALIASED_BUILTINS = {"docs": "drive", "sheets": "drive"}

_VERSION = 2
_MAX_UPSTREAMS = 8
_MAX_BYTES = 256 * 1024
_ID_RE = re.compile(r"[a-z][a-z0-9]{1,15}")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_PRINCIPAL_TYPES = ("claude_client", "n8n_workflow")
_TOP_LEVEL_KEYS = frozenset({"version", "upstreams"})
_UPSTREAM_KEYS = frozenset({"id", "label", "url", "allowed_principal_types", "oauth"})
_OAUTH_KEYS = frozenset({"scopes", "client_id", "client_secret_env", "issuer"})
_VERSION_1_MESSAGE = (
    "this release reads version 2 only; version 1 files (tools, identity claim, audiences) "
    "are no longer supported, see docs/upstream-mcp.md."
)


@dataclass(frozen=True)
class UpstreamOAuthSettings:
    scopes: tuple[str, ...] = ()
    client_id: str | None = None
    client_secret_env: str | None = None
    issuer: str | None = None


@dataclass(frozen=True)
class UpstreamServer:
    id: str
    label: str
    url: str
    oauth: UpstreamOAuthSettings
    allowed_principal_types: tuple[str, ...] = _PRINCIPAL_TYPES


# ----- #
# ----- The operator file
# ----- #


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in text)


def _bad_token(text: str) -> bool:
    return _has_control(text) or any(char.isspace() for char in text)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _address_ok(url: str, *, query_ok: bool = True) -> bool:
    """True for an ``https://`` address (``http://`` only for localhost or a loopback address) with no
    user name or fragment, and, when asked, no query."""
    if "#" in url or any(char.isspace() for char in url) or (not query_ok and "?" in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # reading the port validates it
    except ValueError:
        return False
    if not host or parts.username is not None or parts.password is not None:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and _is_loopback_host(host)


class _RepeatedKey(yaml.constructor.ConstructorError):
    """A YAML mapping repeats a key."""


class _RepeatedJsonKey(ValueError):
    """A JSON object repeats a key."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a repeated mapping key (a repeated key would silently keep the last value)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            try:
                hash(key)
            except TypeError:
                continue  # super() refuses an unhashable key
            if key in seen:
                raise _RepeatedKey(None, None, "repeated key", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _RepeatedJsonKey("repeated key")
    return dict(pairs)


def _parse_text(text: str, *, as_yaml: bool, source: str, noun: str, invalid: str) -> object:
    """Parse ``text`` as YAML or JSON. No message quotes any part of the text."""
    if len(text.encode("utf-8")) > _MAX_BYTES:
        raise ConfigurationError(f"{source}: the {noun} is larger than 256 KiB.")
    try:
        if as_yaml:
            loader = _UniqueKeyLoader(text)
            try:
                return loader.get_single_data()
            finally:
                loader.dispose()
        return json.loads(text, object_pairs_hook=_json_pairs)
    except (_RepeatedKey, _RepeatedJsonKey):
        raise ConfigurationError(f"{source}: the {noun} repeats a key.") from None
    except (yaml.YAMLError, ValueError, RecursionError):
        raise ConfigurationError(f"{source}: the {noun} is not valid {invalid}.") from None


def _read_file(path: Path) -> object:
    """The parsed content of the operator file (YAML for ``.yaml``/``.yml``, JSON otherwise)."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ConfigurationError(f"{ENV_VAR}: the file is larger than 256 KiB.")
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        # The reason is dropped on purpose: a decode error quotes the bytes it choked on.
        raise ConfigurationError(f"{ENV_VAR}: the file could not be read.") from None
    as_yaml = path.name.lower().endswith((".yaml", ".yml"))
    return _parse_text(text, as_yaml=as_yaml, source=ENV_VAR, noun="file", invalid="YAML" if as_yaml else "JSON")


def load_upstreams(path: Path) -> tuple[UpstreamServer, ...]:
    """Read and validate the operator file. Every problem raises ``ConfigurationError``."""
    return parse_upstreams(_read_file(path), source=ENV_VAR)


def parse_inline(text: str) -> tuple[UpstreamServer, ...]:
    """Read and validate the declaration held in ``PRIVACYFENCE_UPSTREAM_MCP`` (YAML or JSON text)."""
    data = _parse_text(text, as_yaml=True, source=ENV_VAR_INLINE, noun="text", invalid="YAML or JSON")
    return parse_upstreams(data, source=ENV_VAR_INLINE)


def load_declared() -> tuple[UpstreamServer, ...] | None:
    """The upstreams declared by the file variable or the inline variable; ``None`` when neither is set."""
    path = os.environ.get(ENV_VAR, "").strip()
    inline = os.environ.get(ENV_VAR_INLINE, "").strip()
    if path and inline:
        raise ConfigurationError(f"Set {ENV_VAR} or {ENV_VAR_INLINE}, not both.")
    if path:
        return load_upstreams(Path(path))
    if inline:
        return parse_inline(os.environ[ENV_VAR_INLINE])
    return None


def parse_upstreams(data: object, *, source: str) -> tuple[UpstreamServer, ...]:
    """The upstreams in ``data``. ``source`` starts every error message (``<source>: <path>: <problem>``)."""

    def fail(path: str, problem: str) -> ConfigurationError:
        return ConfigurationError(f"{source}: {path}: {problem}")

    if not isinstance(data, dict):
        raise fail("$", "must be a JSON object")
    version = data.get("version")
    if type(version) is int and version == 1:
        raise fail("version", _VERSION_1_MESSAGE)
    if type(version) is not int or version != _VERSION:
        raise fail("version", f"must be {_VERSION}")
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            raise fail(str(key), "is not a known key")
    upstreams = data.get("upstreams")
    if not isinstance(upstreams, list):
        raise fail("upstreams", "must be a list")
    if len(upstreams) > _MAX_UPSTREAMS:
        raise fail("upstreams", f"may list at most {_MAX_UPSTREAMS} upstreams")
    result: list[UpstreamServer] = []
    seen: set[str] = set()
    for index, raw in enumerate(upstreams):
        server = _parse_one(raw, f"upstreams[{index}]", seen, fail)
        seen.add(server.id)
        result.append(server)
    return tuple(result)


def _parse_one(raw: object, base: str, seen: set[str], fail) -> UpstreamServer:
    if not isinstance(raw, dict):
        raise fail(base, "must be an object")
    for key in raw:
        if key not in _UPSTREAM_KEYS:
            raise fail(f"{base}.{key}", "is not a known key")
    upstream_id = raw.get("id")
    if not isinstance(upstream_id, str) or not _ID_RE.fullmatch(upstream_id):
        raise fail(f"{base}.id", "must be 2-16 lowercase letters or digits, starting with a letter")
    if upstream_id in seen:
        raise fail(f"{base}.id", "is not unique")
    for name in sorted(BUILTIN_NAMES):
        if (upstream_id + "_").startswith(name + "_") or (name + "_").startswith(upstream_id + "_"):
            raise fail(f"{base}.id", f'upstream id "{upstream_id}" collides with the built-in connector "{name}"')
    if upstream_id in _ALIASED_BUILTINS:
        raise fail(
            f"{base}.id",
            f'upstream id "{upstream_id}" collides with the built-in connector "{_ALIASED_BUILTINS[upstream_id]}"',
        )
    label = raw.get("label")
    if not isinstance(label, str) or not 1 <= len(label) <= 60 or _has_control(label):
        raise fail(f"{base}.label", "must be 1-60 characters with no control characters")
    url = raw.get("url")
    if not isinstance(url, str) or not _address_ok(url):
        raise fail(
            f"{base}.url",
            "must be an https:// address (http:// only for localhost or a loopback address) "
            "with no user name or fragment",
        )
    types = raw.get("allowed_principal_types", list(_PRINCIPAL_TYPES))
    if (
        not isinstance(types, list)
        or not types
        or any(not isinstance(item, str) or item not in _PRINCIPAL_TYPES for item in types)
    ):
        raise fail(f"{base}.allowed_principal_types", "must be a non-empty list of claude_client and n8n_workflow")
    oauth = _parse_oauth(raw.get("oauth", {}), f"{base}.oauth", fail)
    return UpstreamServer(
        id=upstream_id, label=label, url=url, oauth=oauth,
        allowed_principal_types=tuple(sorted(set(types))),
    )


def _parse_oauth(raw: object, base: str, fail) -> UpstreamOAuthSettings:
    if not isinstance(raw, dict):
        raise fail(base, "must be an object")
    for key in raw:
        if key not in _OAUTH_KEYS:
            raise fail(f"{base}.{key}", "is not a known key")
    scopes: tuple[str, ...] = ()
    if "scopes" in raw:
        listed = raw["scopes"]
        if (
            not isinstance(listed, list)
            or not 1 <= len(listed) <= 20
            or any(not isinstance(item, str) or not 1 <= len(item) <= 100 or _bad_token(item) for item in listed)
        ):
            raise fail(f"{base}.scopes", "must be a list of 1-20 scopes, each 1-100 characters with no whitespace")
        scopes = tuple(listed)
    client_id: str | None = None
    if "client_id" in raw:
        client_id = raw["client_id"]
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 200 or _bad_token(client_id):
            raise fail(f"{base}.client_id", "must be 1-200 characters with no whitespace")
    secret_env: str | None = None
    if "client_secret_env" in raw:
        secret_env = raw["client_secret_env"]
        if not isinstance(secret_env, str) or not _ENV_NAME_RE.fullmatch(secret_env):
            raise fail(
                f"{base}.client_secret_env",
                "must be an environment variable name in capital letters, digits and underscores",
            )
        if client_id is None:
            raise fail(f"{base}.client_secret_env", "needs client_id")
    issuer: str | None = None
    if "issuer" in raw:
        issuer = raw["issuer"]
        if not isinstance(issuer, str) or not _address_ok(issuer, query_ok=False):
            raise fail(f"{base}.issuer", "must be an https:// address with no user name, query or fragment")
    return UpstreamOAuthSettings(scopes=scopes, client_id=client_id, client_secret_env=secret_env, issuer=issuer)


# ----- #
# ----- What the removed claim-based feature left behind
# ----- #


def leftovers(admin_config: dict | None = None) -> list[str]:
    """One warning text for each thing the removed claim-based upstream feature left on disk.

    The server only logs these; it never reads or deletes the files. ``admin_config`` is the loaded
    ``admin_config.json`` (or ``None``)."""
    found: list[str] = []
    org = paths.org_dir()
    prefix = "Left over from the removed claim-based upstream feature: "
    if (org / "upstream_claim_keys.json").exists():
        found.append(f"{prefix}org/upstream_claim_keys.json (signing keys; delete it)")
    if (org / "group_membership.json").exists():
        found.append(f"{prefix}org/group_membership.json (delete it)")
    if admin_config is not None and "upstream_mcp" in admin_config:
        found.append("admin_config.json holds an upstream_mcp section that is no longer used")
    return found
