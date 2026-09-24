"""Local mode's ``agent_overrides:`` section -- ADR 0006 option D, as a relabel only.

``settings.yaml`` may map a claimed ``clientInfo.name`` to a registry ``agent_id``::

    agent_overrides:
      my-internal-wrapper: claude-code

A matching call is then *named* after that registry entry, but always recorded as
``agent_source: "client_info"`` -- claimed, on every install, separated or not. The mapping is
selected by the name the caller sends, and local mode has one MCP token per OS user shared by
every AI system on it, so any of them can send a mapped name. Recording the match as ``override``
would turn that caller-supplied string into an attested source (ADR 0006 Verification 2,
ADR 0035; ADR 0037). Local mode has no attested source until a credential identifies the client
by itself (per-credential local tokens -- future work).

The section is read once at startup, like the rest of ``settings.yaml``'s web wiring; changing it
takes a daemon restart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from .agent_identity import AgentIdentity, AgentSource, entry_for_id, identify_registry_id, sanitize_client_string

logger = logging.getLogger(__name__)

SECTION = "agent_overrides"


@dataclass(frozen=True)
class AgentOverrides:
    # Casefolded sanitized claimed name -> registry agent_id.
    mapping: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, client_name: object, version: object) -> AgentIdentity | None:
        """The identity an override gives a call claiming ``client_name``, or None when no
        override matches. Exact, case-insensitive match on the sanitized name, the same rule as
        the registry's own (``agent_identity.lookup``). The match is a claim -- the selector is
        the caller's own name -- so it records ``client_info``, never ``override``."""
        name = sanitize_client_string(client_name)
        if not name:
            return None
        agent_id = self.mapping.get(name.casefold())
        if agent_id is None:
            return None
        return identify_registry_id(agent_id, version, AgentSource.CLIENT_INFO)


def from_config(config: Mapping[str, object]) -> AgentOverrides | None:
    """Parse ``config``'s ``agent_overrides:`` section, or None when there is none. An entry
    naming an unknown registry id, or not a string pair, is skipped with a warning rather than
    refusing to start: an override can only ever add a label, so dropping one is the safe
    direction."""
    raw = config.get(SECTION)
    if not raw:
        return None
    if not isinstance(raw, Mapping):
        logger.warning("settings.yaml: %s must be a mapping of client name to AI system id; ignoring it", SECTION)
        return None
    mapping: dict[str, str] = {}
    for claimed, agent_id in raw.items():
        name = sanitize_client_string(claimed)
        if not name or not isinstance(agent_id, str) or entry_for_id(agent_id) is None:
            logger.warning("settings.yaml: ignoring %s entry %r -> %r", SECTION, claimed, agent_id)
            continue
        mapping[name.casefold()] = agent_id
    if not mapping:
        return None
    return AgentOverrides(mapping=mapping)
