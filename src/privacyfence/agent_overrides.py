"""Local mode's ``agent_overrides:`` section -- ADR 0006 option D, the ``override`` source.

``settings.yaml`` may map a claimed ``clientInfo.name`` to a registry ``agent_id``::

    agent_overrides:
      my-internal-wrapper: claude-code

A matching call is then attributed to that registry entry. Whether it is *attested*
(``agent_source: "override"``) depends on one thing only: whether the agent could have written
the mapping itself. That holds only on a privilege-separated install whose ``settings.yaml`` is
the one under ``authority_dir()`` -- owned by the service account, out of the agent's reach
(``paths.authority_dir``). Anywhere else the file is as writable by the agent as by the human, so
an attested source read from it would be the agent attesting itself -- Invariant 2, "provenance
is never upgraded" -- and the mapping only relabels: the source stays ``client_info``.

``overrides_are_attested`` is that condition, in one place, so it is tested directly.

The section is read once at startup, like the rest of ``settings.yaml``'s web wiring; changing it
takes a daemon restart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import privilege_separation
from .agent_identity import AgentIdentity, AgentSource, entry_for_id, identify_registry_id, sanitize_client_string

logger = logging.getLogger(__name__)

SECTION = "agent_overrides"


def overrides_are_attested(config_path: str | Path, authority_root: Path) -> bool:
    """True only when ``config_path`` is out of the agent's reach: the install is privilege-
    separated *and* the file resolves inside ``authority_root`` (the service-owned
    ``authority_dir()``). Either condition alone is not enough -- an unseparated install's
    authority directory is the agent's own uid, and a separated install started with a
    ``--config`` somewhere else reads a file nothing vouches for."""
    if not privilege_separation.is_enabled():
        return False
    try:
        resolved = Path(config_path).resolve()
        root = authority_root.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved.is_relative_to(root)


@dataclass(frozen=True)
class AgentOverrides:
    # Casefolded sanitized claimed name -> registry agent_id.
    mapping: Mapping[str, str] = field(default_factory=dict)
    attested: bool = False

    def resolve(self, client_name: object, version: object) -> AgentIdentity | None:
        """The identity an override gives a call claiming ``client_name``, or None when no
        override matches. Exact, case-insensitive match on the sanitized name, the same rule as
        the registry's own (``agent_identity.lookup``)."""
        name = sanitize_client_string(client_name)
        if not name:
            return None
        agent_id = self.mapping.get(name.casefold())
        if agent_id is None:
            return None
        source = AgentSource.OVERRIDE if self.attested else AgentSource.CLIENT_INFO
        return identify_registry_id(agent_id, version, source)


def from_config(config: Mapping[str, object], *, attested: bool) -> AgentOverrides | None:
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
    if not attested:
        logger.info(
            "settings.yaml: %s applies as a relabel only -- this install is not privilege-separated, "
            "so the file is within the AI system's reach and cannot attest who it is", SECTION,
        )
    return AgentOverrides(mapping=mapping, attested=attested)
