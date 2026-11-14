"""The operator's upstream MCP file and the admin-config group-to-audience maps.

An operator can attach other MCP servers ("upstreams") to an organization server. They are declared
in a JSON file named by ``PRIVACYFENCE_UPSTREAM_MCP_FILE`` and read once at startup. Each upstream has
an ``id``, a ``label``, a ``url``, an ``audience`` and a table of declared tools. Every declared tool
carries a ``gate`` (``auto``, ``review`` or ``popup``) and a ``verb``. A read verb (``read``,
``download``, ``search``) takes ``auto`` or ``review``. Every other verb takes ``popup`` and must
describe its ``effect``, so an ungated write cannot be declared. Any problem raises
``org_mode.ConfigurationError``, which refuses the start; the message names the JSON path of the bad
value and never quotes the value. An upstream id may not collide with a built-in connector name.

The ``upstream_mcp`` section of ``admin_config.json`` maps *groups* to *audiences*, per upstream
(``groups``: ``{"mgmt@company": ["mgmt"]}``). ``GroupMapStore`` re-reads the file when its modification
time changes, so an edit applies to the next call without a restart. Who is in a group is decided by a
``group_membership.MembershipResolver``, not by this module. The operator file also takes the top-level
keys ``claim`` and ``membership`` (``parse_settings``).

See ADR 0285 (upstream MCP servers as a connector) and ADR 0289 (audiences come from groups).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from . import group_membership
from .org_mode import CONNECTOR_ORG_SECTION, ConfigurationError
from .policy.registry import Verb

logger = logging.getLogger(__name__)

ENV_VAR = "PRIVACYFENCE_UPSTREAM_MCP_FILE"
ENV_VAR_INLINE = "PRIVACYFENCE_UPSTREAM_MCP"
READ_VERBS = frozenset({"read", "download", "search"})
BUILTIN_NAMES = frozenset(CONNECTOR_ORG_SECTION) | {"privacyfence", "upstream_mcp"}

_VERSION = 1
_MAX_UPSTREAMS = 8
_MAX_TOOLS = 50
_MAX_GROUPS = 200
_MAX_AUDIENCES_PER_GROUP = 20
_MAX_BYTES = 256 * 1024
_ID_RE = re.compile(r"[a-z][a-z0-9]{1,15}")
_TOOL_RE = re.compile(r"[A-Za-z0-9_-]{1,48}")
_SINGLE_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_GATES = ("auto", "review", "popup")
_VERBS = frozenset(verb.value for verb in Verb)
_UPSTREAM_KEYS = frozenset({
    "id", "label", "url", "audience", "tools", "grants",
    "identity_claim", "claim_lifetime_seconds", "allowed_principal_types",
})
_IDENTITY_CLAIM_MODES = ("required", "off")
_PRINCIPAL_TYPES = ("claude_client", "n8n_workflow")
_TOP_LEVEL_KEYS = frozenset({"version", "upstreams", "claim", "membership"})
_CLAIM_KEYS = frozenset({"key_overlap_hours"})
_MEMBERSHIP_KEYS = frozenset({"source", "file", "max_additions"})
_TOOL_KEYS = frozenset({"gate", "verb", "effect"})
_CONFIG_FILE = "admin_config.json"
_CONFIG_SECTION = "upstream_mcp"


@dataclass(frozen=True)
class DeclaredTool:
    name: str  # upstream tool name, e.g. "search"
    exposed_name: str  # "<id>_<name>", e.g. "kh_search"
    operation: str  # "<id>.<name>", e.g. "kh.search"
    gate: str  # "auto" | "review" | "popup"
    verb: str  # a Verb value
    effect: str  # "" unless popup


@dataclass(frozen=True)
class UpstreamServer:
    id: str
    label: str
    url: str
    audience: str
    tools: tuple[DeclaredTool, ...]  # in file order
    identity_claim: str = "required"  # "required" | "off"
    claim_lifetime_seconds: int = 60  # 5..60
    allowed_principal_types: tuple[str, ...] = _PRINCIPAL_TYPES
    grants: tuple[object, ...] = ()  # the removed operator "grants" key: parsed only so the daemon can warn; never used


@dataclass(frozen=True)
class ClaimSettings:
    key_overlap_hours: int = 24  # 1..720


@dataclass(frozen=True)
class MembershipSettings:
    source: str = "snapshot"  # "snapshot" | "static"
    file: str | None = None  # static only
    max_additions: int = 20  # 1..1000


# ----- #
# ----- The operator file
# ----- #


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in text)


def _http_host_ok(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return _SINGLE_LABEL_RE.fullmatch(host) is not None


def _url_ok(url: str) -> bool:
    if "#" in url or any(char.isspace() for char in url):
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
    return parts.scheme == "http" and _http_host_ok(host)


class _RepeatedKey(yaml.constructor.ConstructorError):
    """A YAML mapping repeats a key."""


class _RepeatedJsonKey(ValueError):
    """A JSON object repeats a key."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a repeated mapping key (a repeated tool name would keep the last gate)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
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
        base = f"upstreams[{index}]"
        server = _parse_one(raw, base, seen, fail, source)
        seen.add(server.id)
        result.append(server)
    return tuple(result)


def _parse_one(raw: object, base: str, seen: set[str], fail, source: str) -> UpstreamServer:
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
    label = raw.get("label")
    if not isinstance(label, str) or not 1 <= len(label) <= 60 or _has_control(label):
        raise fail(f"{base}.label", "must be 1-60 characters with no control characters")
    url = raw.get("url")
    if not isinstance(url, str) or not _url_ok(url):
        raise fail(
            f"{base}.url",
            "must be an https:// address (http:// only for localhost, a loopback address or a "
            "single-word host name) with no user name or fragment",
        )
    audience = raw.get("audience")
    if (
        not isinstance(audience, str)
        or not 1 <= len(audience) <= 200
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in audience)
    ):
        raise fail(f"{base}.audience", "must be 1-200 characters with no whitespace")
    tools = _parse_tools(raw.get("tools"), upstream_id, f"{base}.tools", fail)
    claim_mode = raw.get("identity_claim", "required")
    if not isinstance(claim_mode, str) or claim_mode not in _IDENTITY_CLAIM_MODES:
        raise fail(f"{base}.identity_claim", "must be required or off")
    lifetime = raw.get("claim_lifetime_seconds", 60)
    if type(lifetime) is not int or not 5 <= lifetime <= 60:
        raise fail(f"{base}.claim_lifetime_seconds", "must be a whole number from 5 to 60")
    types = raw.get("allowed_principal_types", list(_PRINCIPAL_TYPES))
    if (
        not isinstance(types, list)
        or not types
        or any(not isinstance(item, str) or item not in _PRINCIPAL_TYPES for item in types)
    ):
        raise fail(f"{base}.allowed_principal_types", "must be a non-empty list of claude_client and n8n_workflow")
    grants = raw.get("grants", [])
    if not isinstance(grants, list):
        raise fail(f"{base}.grants", "must be a list")
    return UpstreamServer(
        id=upstream_id, label=label, url=url, audience=audience, tools=tools,
        identity_claim=claim_mode, claim_lifetime_seconds=lifetime,
        allowed_principal_types=tuple(sorted(set(types))), grants=tuple(grants),
    )


def parse_settings(data: object, *, source: str = ENV_VAR) -> tuple[ClaimSettings, MembershipSettings]:
    """The top-level ``claim`` and ``membership`` settings of the operator file (defaults when absent)."""

    def fail(path: str, problem: str) -> ConfigurationError:
        return ConfigurationError(f"{source}: {path}: {problem}")

    if not isinstance(data, dict):
        raise fail("$", "must be a JSON object")
    claim = data.get("claim", {})
    if not isinstance(claim, dict):
        raise fail("claim", "must be an object")
    for key in claim:
        if key not in _CLAIM_KEYS:
            raise fail(f"claim.{key}", "is not a known key")
    overlap = claim.get("key_overlap_hours", 24)
    if type(overlap) is not int or not 1 <= overlap <= 720:
        raise fail("claim.key_overlap_hours", "must be a whole number from 1 to 720")
    membership = data.get("membership", {})
    if not isinstance(membership, dict):
        raise fail("membership", "must be an object")
    for key in membership:
        if key not in _MEMBERSHIP_KEYS:
            raise fail(f"membership.{key}", "is not a known key")
    mode = membership.get("source", "snapshot")
    if not isinstance(mode, str) or mode not in ("snapshot", "static"):
        raise fail("membership.source", "must be snapshot or static")
    file = membership.get("file")
    if mode == "static":
        if not isinstance(file, str) or not file.strip() or _has_control(file):
            raise fail("membership.file", "is required for source static: a path")
    elif "file" in membership:
        raise fail("membership.file", "is only allowed with source static")
    max_additions = membership.get("max_additions", group_membership.DEFAULT_MAX_ADDITIONS)
    if type(max_additions) is not int or not 1 <= max_additions <= 1000:
        raise fail("membership.max_additions", "must be a whole number from 1 to 1000")
    return (
        ClaimSettings(key_overlap_hours=overlap),
        MembershipSettings(source=mode, file=file if mode == "static" else None, max_additions=max_additions),
    )


def load_settings(path: Path | None = None) -> tuple[ClaimSettings, MembershipSettings]:
    """``parse_settings`` for the declaration at ``path`` (the file variable) or, with no path, whichever
    of the file variable and the inline variable is set; the defaults when neither is."""
    if path is not None:
        return parse_settings(_read_file(path))
    file_value = os.environ.get(ENV_VAR, "").strip()
    inline = os.environ.get(ENV_VAR_INLINE, "").strip()
    if file_value and inline:
        raise ConfigurationError(f"Set {ENV_VAR} or {ENV_VAR_INLINE}, not both.")
    if file_value:
        return parse_settings(_read_file(Path(file_value)))
    if inline:
        data = _parse_text(
            os.environ[ENV_VAR_INLINE], as_yaml=True, source=ENV_VAR_INLINE, noun="text", invalid="YAML or JSON",
        )
        return parse_settings(data, source=ENV_VAR_INLINE)
    return ClaimSettings(), MembershipSettings()


def _parse_tools(raw: object, upstream_id: str, base: str, fail) -> tuple[DeclaredTool, ...]:
    if not isinstance(raw, dict):
        raise fail(base, "must be an object")
    if len(raw) > _MAX_TOOLS:
        raise fail(base, f"may declare at most {_MAX_TOOLS} tools")
    tools: list[DeclaredTool] = []
    for name, spec in raw.items():
        path = f"{base}.{name}"
        if not isinstance(name, str) or not _TOOL_RE.fullmatch(name):
            raise fail(path, "tool name must be 1-48 letters, digits, underscores or hyphens")
        exposed = f"{upstream_id}_{name}"
        if len(exposed) > 64:
            raise fail(path, "exposed tool name is longer than 64 characters")
        if not isinstance(spec, dict):
            raise fail(path, "must be an object")
        for key in spec:
            if key not in _TOOL_KEYS:
                raise fail(f"{path}.{key}", "is not a known key")
        gate = spec.get("gate")
        if not isinstance(gate, str) or gate not in _GATES:
            raise fail(f"{path}.gate", "must be auto, review or popup")
        verb = spec.get("verb")
        if not isinstance(verb, str) or verb not in _VERBS:
            raise fail(f"{path}.verb", "must be one of the policy verbs")
        effect = ""
        if verb in READ_VERBS:
            if gate == "popup":
                raise fail(f"{path}.gate", "a read verb takes gate auto or review")
            if "effect" in spec:
                raise fail(f"{path}.effect", "a read verb takes no effect")
        else:
            if gate != "popup":
                raise fail(f"{path}.gate", "a verb that changes something takes gate popup")
            declared = spec.get("effect")
            if not isinstance(declared, str) or not 1 <= len(declared) <= 200 or not declared.endswith("."):
                raise fail(f"{path}.effect", "is required: 1-200 characters, ending in a full stop")
            effect = declared
        tools.append(
            DeclaredTool(
                name=name,
                exposed_name=exposed,
                operation=f"{upstream_id}.{name}",
                gate=gate,
                verb=verb,
                effect=effect,
            )
        )
    return tuple(tools)


# ----- #
# ----- Group-to-audience maps
# ----- #


def _parse_groups(section: object, upstream_ids: frozenset[str], warned: set[str]) -> dict[str, dict[str, frozenset[str]]]:
    base = f"{_CONFIG_FILE}: {_CONFIG_SECTION}"
    if not isinstance(section, dict):
        raise ConfigurationError(f"{base}: must be an object")
    result: dict[str, dict[str, frozenset[str]]] = {}
    for upstream_id, entry in section.items():
        if upstream_id not in upstream_ids:
            if upstream_id not in warned:
                warned.add(upstream_id)
                logger.warning("%s: groups for the undeclared upstream %r are ignored.", base, upstream_id)
            continue
        where = f"{base}.{upstream_id}"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{where}: must be an object")
        for key in entry:
            if key == "grants":
                raise ConfigurationError(f"{where}.grants: grants were replaced by groups")
            if key != "groups":
                raise ConfigurationError(f"{where}.{key}: is not a known key")
        raw_groups = entry.get("groups", {})
        if not isinstance(raw_groups, dict):
            raise ConfigurationError(f"{where}.groups: must be an object")
        if len(raw_groups) > _MAX_GROUPS:
            raise ConfigurationError(f"{where}.groups: may hold at most {_MAX_GROUPS} groups")
        groups: dict[str, frozenset[str]] = {}
        for name, audiences in raw_groups.items():
            if not isinstance(name, str) or not group_membership.GROUP_NAME_RE.fullmatch(name):
                raise ConfigurationError(f"{where}.groups: a group name must be a lowercase address like mgmt@company")
            item = f"{where}.groups.{name}"
            if not isinstance(audiences, list):
                raise ConfigurationError(f"{item}: must be a list of audience names")
            if len(audiences) > _MAX_AUDIENCES_PER_GROUP:
                raise ConfigurationError(f"{item}: may name at most {_MAX_AUDIENCES_PER_GROUP} audiences")
            for position, audience in enumerate(audiences):
                if not isinstance(audience, str) or not group_membership.AUDIENCE_RE.fullmatch(audience):
                    raise ConfigurationError(
                        f"{item}[{position}]: must be 1-63 lowercase letters, digits, underscores or hyphens"
                    )
            groups[name] = frozenset(audiences)
        result[upstream_id] = groups
    return result


def parse_groups(section: object, upstream_ids: frozenset[str]) -> dict[str, dict[str, frozenset[str]]]:
    """The group maps in the ``upstream_mcp`` section of the admin config, keyed by upstream id.

    An id that is not in ``upstream_ids`` is logged as a warning and ignored. An invalid section
    raises ``ConfigurationError`` naming ``upstream_mcp.<id>.groups``."""
    return _parse_groups(section, upstream_ids, set())


class GroupMapStore:
    """The group-to-audience maps in ``admin_config.json``, re-read when the file's modification time changes."""

    def __init__(self, path: Path, upstream_ids: frozenset[str]) -> None:
        self._path = path
        self._upstream_ids = upstream_ids
        self._lock = threading.Lock()
        self._warned: set[str] = set()
        self._groups: dict[str, dict[str, frozenset[str]]] = {}
        self._mtime: int | None = None
        self._mtime = self._stat()
        self._groups = self._read()  # the first load raises on a bad file

    def _stat(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _read(self) -> dict[str, dict[str, frozenset[str]]]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError):
            raise ConfigurationError(f"{_CONFIG_FILE}: the file could not be read.") from None
        try:
            raw = json.loads(text)
        except ValueError:
            raise ConfigurationError(f"{_CONFIG_FILE}: the file is not valid JSON.") from None
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{_CONFIG_FILE}: must hold a JSON object")
        if _CONFIG_SECTION not in raw:
            return {}
        return _parse_groups(raw[_CONFIG_SECTION], self._upstream_ids, self._warned)

    def group_audiences(self, upstream_id: str) -> dict[str, frozenset[str]]:
        with self._lock:
            mtime = self._stat()
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    self._groups = self._read()
                except ConfigurationError as error:
                    logger.error("%s; keeping the last good group maps.", error)
            return dict(self._groups.get(upstream_id, {}))
