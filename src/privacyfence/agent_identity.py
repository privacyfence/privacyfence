"""Which AI system made a request -- the identity model behind ADR 0006 and ADR 0035.

This module is the data model only: nothing here reads a handshake, an OAuth client or an
override. Callers resolve an ``AgentIdentity`` from whatever signal they have, enter
``agent_scope`` around the request's dispatch (beside ``principal_scope``), and everything
downstream -- ``audit_log.AuditLogger`` stamping an entry, above all -- reads it back through
``current_agent()``.

Invariants this module is where the rest of the tree gets them from (ADR 0006, ADR 0035):

- **A claimed identity never changes an outcome.** Only ``AgentSource.OVERRIDE`` and
  ``AgentSource.OAUTH_CLIENT`` are attested (``is_attested()``); nothing may key on ``agent_id``
  otherwise.
- **Provenance is never upgraded.** ``identify()`` records exactly the source it is given. A
  registry match names the display name; it never turns a claim into an attestation.
- **Unknown says so.** No usable signal gives ``UNKNOWN_AGENT`` -- every field ``""`` -- never a
  default name and never "Claude".
- **Caller-supplied strings are data.** Every one goes through ``sanitize_client_string()``
  before it is stored; rendering still escapes it. ``clientInfo.icons`` and ``website_url`` are
  never read here at all.
"""

from __future__ import annotations

import contextvars
import unicodedata
from dataclasses import dataclass
from enum import Enum

# ADR 0006 Invariant 4: the cap every caller-supplied identity string is cut to.
MAX_CLIENT_STRING_LENGTH = 64

# What an unmatched client renders as, followed by its (sanitized, escaped) claimed name --
# ADR 0035 decision 2. The rendering itself belongs to the approval card; the wording lives here
# so the card and any other surface cannot drift apart on it.
UNRECOGNISED_LABEL = "Unrecognised AI system"

# Prefix of an unmatched client's agent_id: "unknown:<sanitized name>".
UNKNOWN_ID_PREFIX = "unknown:"


class AgentSource(str, Enum):
    """Which signal produced an ``AgentIdentity``, ranked strongest first (ADR 0006 decision 2):
    the first one present wins. ``NONE`` is the empty string, so it serializes into the audit log
    as ``""`` -- "no usable signal", which renders as unknown."""

    OVERRIDE = "override"
    OAUTH_CLIENT = "oauth_client"
    CLIENT_INFO = "client_info"
    ENDPOINT = "endpoint"
    NONE = ""

    def is_attested(self) -> bool:
        """True only for a signal the caller could not have chosen (ADR 0006 decision 3)."""
        return self in (AgentSource.OVERRIDE, AgentSource.OAUTH_CLIENT)


@dataclass(frozen=True)
class AgentIdentity:
    id: str
    name: str
    version: str
    source: AgentSource

    def is_attested(self) -> bool:
        return self.source.is_attested()


UNKNOWN_AGENT = AgentIdentity(id="", name="", version="", source=AgentSource.NONE)


def _is_stripped_char(ch: str) -> bool:
    # Cc: C0/C1 controls (newlines, escapes, NUL). Cf: format characters, which is where every
    # bidi control lives (U+200E/200F, U+202A-202E, U+2066-2069, U+061C) along with zero-width
    # characters that would let two different-looking names compare equal on screen. Zl/Zp: the
    # line and paragraph separators, which break a one-line label as surely as a newline does.
    return unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")


def sanitize_client_string(value: object) -> str:
    """ADR 0006 Invariant 4, for ``clientInfo.name``/``title``/``version`` and a DCR
    ``client_name`` alike: control, bidi and other format characters stripped, surrounding
    whitespace trimmed, then cut to ``MAX_CLIENT_STRING_LENGTH``. Anything that is not a string
    (a mis-shaped handshake) is treated as absent and gives ``""``, never an exception."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if not _is_stripped_char(ch)).strip()
    return cleaned[:MAX_CLIENT_STRING_LENGTH].strip()


@dataclass(frozen=True)
class RegistryEntry:
    agent_id: str
    display_name: str
    client_names: tuple[str, ...]


# ADR 0035 decision 2's initial registry. Matching is exact, case-insensitive equality on the
# sanitized clientInfo.name -- never prefix, substring or regex, so a name that merely contains
# "claude" is not Claude. Entries marked "guess" in that ADR are corrected from a real handshake
# when one is seen; an unrecognised name is never promoted to an entry by default.
REGISTRY: tuple[RegistryEntry, ...] = (
    RegistryEntry("claude-code", "Claude Code", ("claude-code",)),
    RegistryEntry("claude", "Claude", ("claude-ai",)),
    RegistryEntry("chatgpt", "ChatGPT", ("openai-mcp",)),
    RegistryEntry("gemini-cli", "Gemini CLI", ("gemini-cli-mcp-client",)),
    RegistryEntry("cursor", "Cursor", ("cursor-vscode",)),
)

_BY_CLIENT_NAME: dict[str, RegistryEntry] = {
    client_name.casefold(): entry for entry in REGISTRY for client_name in entry.client_names
}


_BY_AGENT_ID: dict[str, RegistryEntry] = {entry.agent_id: entry for entry in REGISTRY}


def lookup(client_name: str) -> RegistryEntry | None:
    """The registry entry whose match list contains ``client_name`` exactly (case-insensitively,
    after sanitizing), or None."""
    name = sanitize_client_string(client_name)
    if not name:
        return None
    return _BY_CLIENT_NAME.get(name.casefold())


def entry_for_id(agent_id: object) -> RegistryEntry | None:
    """The registry entry whose ``agent_id`` is exactly ``agent_id``, or None -- what an org pin
    or a local override names (ADR 0035 decision 3, ADR 0006 option D)."""
    if not isinstance(agent_id, str):
        return None
    return _BY_AGENT_ID.get(agent_id)


def identify_registry_id(agent_id: str, version: object, source: AgentSource) -> AgentIdentity | None:
    """The identity for a registry ``agent_id`` an admin pin or a local override named, recording
    ``source`` unchanged -- the caller decides whether that signal is attested, exactly as for
    ``identify``. None when ``agent_id`` is not a registry entry, so a stale or mistyped mapping
    falls through to the next signal rather than inventing a name."""
    entry = entry_for_id(agent_id)
    if entry is None or source is AgentSource.NONE:
        return None
    return AgentIdentity(
        id=entry.agent_id, name=entry.display_name, version=sanitize_client_string(version), source=source,
    )


def identify(client_name: object, version: object, source: AgentSource) -> AgentIdentity:
    """Resolve a claimed client name through the registry, recording ``source`` unchanged.

    - No usable name gives ``UNKNOWN_AGENT`` (``agent_source: ""``), whatever ``source`` was.
    - A registry match gives that entry's ``agent_id`` and display name.
    - Anything else gives ``agent_id="unknown:<sanitized name>"`` with the sanitized name as
      ``name``; it renders as ``UNRECOGNISED_LABEL`` plus that name (ADR 0035 decision 2).
    """
    name = sanitize_client_string(client_name)
    if not name or source is AgentSource.NONE:
        return UNKNOWN_AGENT
    clean_version = sanitize_client_string(version)
    entry = _BY_CLIENT_NAME.get(name.casefold())
    if entry is not None:
        return AgentIdentity(id=entry.agent_id, name=entry.display_name, version=clean_version, source=source)
    return AgentIdentity(id=UNKNOWN_ID_PREFIX + name, name=name, version=clean_version, source=source)


# Default is UNKNOWN_AGENT: any code running outside a request's dispatch (startup, a background
# sweep, every test that never enters a scope) attributes to no one, which is Invariant 3.
_agent_ctx: contextvars.ContextVar[AgentIdentity] = contextvars.ContextVar(
    "privacyfence_agent", default=UNKNOWN_AGENT
)


def current_agent() -> AgentIdentity:
    return _agent_ctx.get()


class agent_scope:  # noqa: N801 (context-manager-style name, like principal_scope/reason_scope)
    """Run the wrapped code attributed to ``agent`` -- entered once around a request's dispatch,
    beside ``principal.principal_scope``, the same way that and ``gate.reason_scope`` are.

    Also entered around audit rows about an *earlier* request (``gate``'s expiry sweep), with the
    identity that request's ``PendingApproval`` captured at creation, so a sweep running inside an
    unrelated call never attributes someone else's approval to that call's agent."""

    def __init__(self, agent: AgentIdentity) -> None:
        self._agent = agent
        self._token: contextvars.Token[AgentIdentity] | None = None

    def __enter__(self) -> agent_scope:
        self._token = _agent_ctx.set(self._agent)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _agent_ctx.reset(self._token)
