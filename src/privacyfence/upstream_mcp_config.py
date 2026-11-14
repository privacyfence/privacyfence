"""The operator's upstream MCP file and the admin-config domain grants.

An operator can attach other MCP servers ("upstreams") to an organization server. They are declared
in a JSON file named by ``PRIVACYFENCE_UPSTREAM_MCP_FILE`` and read once at startup. Each upstream has
an ``id``, a ``label``, a ``url``, an ``audience`` and a table of declared tools. Every declared tool
carries a ``gate`` (``auto``, ``review`` or ``popup``) and a ``verb``. A read verb (``read``,
``download``, ``search``) takes ``auto`` or ``review``. Every other verb takes ``popup`` and must
describe its ``effect``, so an ungated write cannot be declared. Any problem raises
``org_mode.ConfigurationError``, which refuses the start; the message names the JSON path of the bad
value and never quotes the value. An upstream id may not collide with a built-in connector name.

The ``upstream_mcp`` section of ``admin_config.json`` grants *domains* to people, per upstream:
``*`` (any verified person), ``@<domain>`` or an exact email address. ``GrantStore`` re-reads the file
when its modification time changes, so an edit applies to the next call without a restart. A person
whose email is not verified, or is empty, gets no domains.

See ADR 0285 (upstream MCP servers as a connector) and ADR 0287 (domain grants).
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
_MAX_GRANTS = 200
_MAX_BYTES = 256 * 1024
_ID_RE = re.compile(r"[a-z][a-z0-9]{1,15}")
_TOOL_RE = re.compile(r"[A-Za-z0-9_-]{1,48}")
_DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_SINGLE_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_GATES = ("auto", "review", "popup")
_VERBS = frozenset(verb.value for verb in Verb)
_UPSTREAM_KEYS = frozenset({"id", "label", "url", "audience", "tools", "grants"})
_TOOL_KEYS = frozenset({"gate", "verb", "effect"})
_GRANT_FILE = "admin_config.json"
_GRANT_SECTION = "upstream_mcp"


@dataclass(frozen=True)
class DeclaredTool:
    name: str  # upstream tool name, e.g. "search"
    exposed_name: str  # "<id>_<name>", e.g. "kh_search"
    operation: str  # "<id>.<name>", e.g. "kh.search"
    gate: str  # "auto" | "review" | "popup"
    verb: str  # a Verb value
    effect: str  # "" unless popup


@dataclass(frozen=True)
class Grant:
    match: str  # "*", "@domain" or an email, lower-case
    domains: tuple[str, ...]  # sorted


@dataclass(frozen=True)
class UpstreamServer:
    id: str
    label: str
    url: str
    audience: str
    tools: tuple[DeclaredTool, ...]  # in file order
    grants: tuple[Grant, ...] = ()  # seeded into admin_config.json once (ADR 0288)


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


def load_upstreams(path: Path) -> tuple[UpstreamServer, ...]:
    """Read and validate the operator file (YAML for ``.yaml``/``.yml``, JSON otherwise).
    Every problem raises ``ConfigurationError``."""
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
    data = _parse_text(text, as_yaml=as_yaml, source=ENV_VAR, noun="file", invalid="YAML" if as_yaml else "JSON")
    return parse_upstreams(data, source=ENV_VAR)


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
        if key not in ("version", "upstreams"):
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
    grants = _parse_declared_grants(raw.get("grants", []), base, source, fail)
    return UpstreamServer(id=upstream_id, label=label, url=url, audience=audience, tools=tools, grants=grants)


def _parse_declared_grants(raw: object, base: str, source: str, fail) -> tuple[Grant, ...]:
    if not isinstance(raw, list):
        raise fail(f"{base}.grants", "must be a list")
    if len(raw) > _MAX_GRANTS:
        raise fail(f"{base}.grants", f"may list at most {_MAX_GRANTS} grants")
    return tuple(_parse_grant(item, f"{source}: {base}.grants[{n}]") for n, item in enumerate(raw))


def seed_payload(upstreams: tuple[UpstreamServer, ...]) -> dict[str, dict[str, Any]]:
    """The ``upstream_mcp`` section entries to seed into admin_config.json; upstreams without grants are left out."""
    return {
        upstream.id: {"grants": [{"match": g.match, "domains": list(g.domains)} for g in upstream.grants]}
        for upstream in upstreams
        if upstream.grants
    }


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
# ----- Domain grants
# ----- #


def _grant_match_ok(match: str) -> bool:
    if match == "*":
        return True
    if any(char.isspace() for char in match) or _has_control(match):
        return False
    if match.startswith("@"):
        return len(match) > 1 and "@" not in match[1:]
    local, at, domain = match.partition("@")
    return bool(local) and at == "@" and bool(domain) and "@" not in domain


def _parse_grants(section: object, upstream_ids: frozenset[str], warned: set[str]) -> dict[str, tuple[Grant, ...]]:
    base = f"{_GRANT_FILE}: {_GRANT_SECTION}"
    if not isinstance(section, dict):
        raise ConfigurationError(f"{base}: must be an object")
    result: dict[str, tuple[Grant, ...]] = {}
    for upstream_id, entry in section.items():
        if upstream_id not in upstream_ids:
            if upstream_id not in warned:
                warned.add(upstream_id)
                logger.warning("%s: grants for the undeclared upstream %r are ignored.", base, upstream_id)
            continue
        where = f"{base}.{upstream_id}"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{where}: must be an object")
        for key in entry:
            if key != "grants":
                raise ConfigurationError(f"{where}.{key}: is not a known key")
        raw_grants = entry.get("grants", [])
        if not isinstance(raw_grants, list):
            raise ConfigurationError(f"{where}.grants: must be a list")
        if len(raw_grants) > _MAX_GRANTS:
            raise ConfigurationError(f"{where}.grants: may hold at most {_MAX_GRANTS} grants")
        result[upstream_id] = tuple(_parse_grant(raw, f"{where}.grants[{n}]") for n, raw in enumerate(raw_grants))
    return result


def _parse_grant(raw: object, where: str) -> Grant:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{where}: must be an object")
    for key in raw:
        if key not in ("match", "domains"):
            raise ConfigurationError(f"{where}.{key}: is not a known key")
    match = raw.get("match")
    if not isinstance(match, str) or not _grant_match_ok(match):
        raise ConfigurationError(f"{where}.match: must be *, @<domain> or an email address")
    domains = raw.get("domains")
    if not isinstance(domains, list):
        raise ConfigurationError(f"{where}.domains: must be a list")
    for position, domain in enumerate(domains):
        if not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain):
            raise ConfigurationError(
                f"{where}.domains[{position}]: must be 1-63 lowercase letters, digits, underscores or hyphens"
            )
    return Grant(match=match.lower(), domains=tuple(sorted(set(domains))))


def parse_grants(section: object, upstream_ids: frozenset[str]) -> dict[str, tuple[Grant, ...]]:
    """The grants in the ``upstream_mcp`` section of the admin config, keyed by upstream id.

    An id that is not in ``upstream_ids`` is logged as a warning and ignored. An invalid section
    raises ``ConfigurationError`` naming ``upstream_mcp.<id>.grants[<n>]``."""
    return _parse_grants(section, upstream_ids, set())


def domains_for(
    grants: dict[str, tuple[Grant, ...]], upstream_id: str, email: str, email_verified: bool
) -> tuple[str, ...]:
    """The sorted union of the domains of every grant matching ``email``; ``()`` unless verified."""
    address = email.strip().lower()
    if not email_verified or not address:
        return ()
    domain = "@" + address.rpartition("@")[2]
    found: set[str] = set()
    for grant in grants.get(upstream_id, ()):
        if grant.match in ("*", address, domain):
            found.update(grant.domains)
    return tuple(sorted(found))


class GrantStore:
    """The grants in ``admin_config.json``, re-read when the file's modification time changes."""

    def __init__(self, path: Path, upstream_ids: frozenset[str]) -> None:
        self._path = path
        self._upstream_ids = upstream_ids
        self._lock = threading.Lock()
        self._warned: set[str] = set()
        self._grants: dict[str, tuple[Grant, ...]] = {}
        self._mtime: int | None = None
        self._mtime = self._stat()
        self._grants = self._read()  # the first load raises on a bad file

    def _stat(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _read(self) -> dict[str, tuple[Grant, ...]]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError):
            raise ConfigurationError(f"{_GRANT_FILE}: the file could not be read.") from None
        try:
            raw = json.loads(text)
        except ValueError:
            raise ConfigurationError(f"{_GRANT_FILE}: the file is not valid JSON.") from None
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{_GRANT_FILE}: must hold a JSON object")
        if _GRANT_SECTION not in raw:
            return {}
        return _parse_grants(raw[_GRANT_SECTION], self._upstream_ids, self._warned)

    def domains_for(self, upstream_id: str, email: str, email_verified: bool) -> tuple[str, ...]:
        with self._lock:
            mtime = self._stat()
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    self._grants = self._read()
                except ConfigurationError as error:
                    logger.error("%s; keeping the last good grants.", error)
            grants = self._grants
        return domains_for(grants, upstream_id, email, email_verified)
