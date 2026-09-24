"""Org mode's admin pins: which registered OAuth client is which AI system (ADR 0035 decision 3).

A DCR ``client_name`` is whatever the registering caller chose, so on its own it is a claim
(``agent_source: "client_info"``). An admin who has looked at a registration can pin its
``client_id`` to a registry ``agent_id``; a tool call whose access token carries a pinned
``client_id`` is then attributed with ``agent_source: "oauth_client"`` -- the attested tier, the
only one a future rule may key on (ADR 0006 decision 3). The pin is made by a human, behind
step-up, never by anything the caller supplied.

The pins live in their own file under ``org_dir()``, beside ``oauth_clients.json``, not in
``org_config.json``: DCR ``client_id``s are minted at runtime, so a bundle prepared and signed
ahead of time has nothing stable to name.

A pin names a registration, not a name. It never moves to another ``client_id``, including a
re-registration under the same ``client_name``. When the provider's TTL prune removes a pinned
registration, the pin stays on disk but is inert -- no token can carry that ``client_id`` again,
since the server mints every ``client_id`` itself -- and the Settings surface lists it as stale so
an admin can remove it. It never silently transfers.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..agent_identity import entry_for_id
from ..secure_files import atomic_write_json

logger = logging.getLogger(__name__)

PINS_FILE_NAME = "agent_pins.json"


@dataclass(frozen=True)
class AgentPin:
    agent_id: str
    pinned_at: float
    pinned_by: str


class AgentPinStore:
    """``client_id`` -> ``AgentPin``, persisted atomically to ``path`` on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._pins: dict[str, AgentPin] = self._load()

    def _load(self) -> dict[str, AgentPin]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            pins: dict[str, AgentPin] = {}
            for client_id, data in raw.items():
                # A pin naming something the registry no longer knows is dropped on load rather
                # than kept: it could only ever resolve to nothing (identify_registry_id), and an
                # admin re-pinning it sees the current registry, not a dead id.
                agent_id = str(data["agent_id"])
                if entry_for_id(agent_id) is None:
                    logger.warning("Ignoring the pin for client %s: %r is not a known AI system", client_id, agent_id)
                    continue
                pins[str(client_id)] = AgentPin(
                    agent_id=agent_id, pinned_at=float(data["pinned_at"]), pinned_by=str(data["pinned_by"]),
                )
            return pins
        except Exception as exc:
            # Fail closed to "no pins": every client falls back to the claimed tier, which is
            # the safe direction -- never to an attestation nobody can see the source of.
            logger.warning("Could not read %s: %s -- starting with no AI-system pins", self._path, exc)
            return {}

    def _save_locked(self) -> None:
        raw = {
            client_id: {"agent_id": pin.agent_id, "pinned_at": pin.pinned_at, "pinned_by": pin.pinned_by}
            for client_id, pin in self._pins.items()
        }
        atomic_write_json(self._path, raw, indent=2, sort_keys=True)

    def pinned_agent_id(self, client_id: str) -> str | None:
        """The registry ``agent_id`` pinned to ``client_id``, or None. Read on every tool call."""
        with self._lock:
            pin = self._pins.get(client_id)
            return pin.agent_id if pin is not None else None

    def pins(self) -> dict[str, AgentPin]:
        with self._lock:
            return dict(self._pins)

    def pin(self, client_id: str, agent_id: str, *, pinned_by: str) -> None:
        """Pin ``client_id`` to ``agent_id``. Raises ``ValueError`` for an ``agent_id`` the
        registry does not know -- validation the route relies on, not a hint."""
        if entry_for_id(agent_id) is None:
            raise ValueError(f"{agent_id!r} is not a known AI system")
        with self._lock:
            self._pins[client_id] = AgentPin(agent_id=agent_id, pinned_at=time.time(), pinned_by=pinned_by)
            self._save_locked()

    def unpin(self, client_id: str) -> bool:
        """Remove ``client_id``'s pin. Returns whether there was one."""
        with self._lock:
            if self._pins.pop(client_id, None) is None:
                return False
            self._save_locked()
            return True


def pins_file_path() -> Path:
    from ..paths import org_dir

    return org_dir() / PINS_FILE_NAME
