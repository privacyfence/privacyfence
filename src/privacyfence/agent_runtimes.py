"""What is runtime-neutral about delivering a credential to an agent runtime: the n8n
configuration, the ``deliver_to`` request an AI client may make, and the target it names.

The only runtime is n8n; ``n8n_api`` holds the code that talks to it. The n8n address comes
from the server's environment only, and the API key is read per use and never stored,
logged or put in an error (ADR 0278-0282).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

from . import org_mode
from .n8n_api import N8nApi

logger = logging.getLogger(__name__)

N8N_URL_ENV = "PRIVACYFENCE_N8N_URL"
N8N_API_KEY_ENV = "PRIVACYFENCE_N8N_API_KEY"  # the variable's name, never a key; secret-scan:allow
RUNTIMES = ("n8n",)

DELIVERY_UNAVAILABLE = (
    "Delivery to n8n is not set up on this PrivacyFence server. "
    "Your administrator sets it up under Admin → Agent runtimes."
)
DELIVERY_MESSAGE = (
    "Give the person this link. They review the token in PrivacyFence Settings, can narrow it, "
    "and confirm it themselves. When they do, PrivacyFence creates an n8n credential holding the token; "
    "call privacyfence_runtime_credential_status with this proposal_id to get the credential's id for "
    "the MCP Client node. Anyone who can edit workflows in that n8n can use the credential, so keep its "
    "tools and rules narrow. Calls that would need an approval are refused at once. Tools in "
    "uncovered_tools have no rule, so the token's calls to them will be denied."
)

_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9]{1,36}")
_WORKFLOW_ID_RE = re.compile(r"[A-Za-z0-9]{1,36}")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_REFUSED_HOSTS = ("metadata.google.internal",)


@dataclass(frozen=True)
class N8nRuntimeConfig:
    enabled: bool
    base_url: str
    project_id: str
    mcp_url: str

    @classmethod
    def from_org_config(
        cls, org_config: dict[str, Any], *, environ: Mapping[str, str] | None = None
    ) -> N8nRuntimeConfig:
        env = os.environ if environ is None else environ
        section = org_config.get("agent_runtimes", {})
        n8n = section.get("n8n", {}) if isinstance(section, dict) else None
        if not isinstance(n8n, dict):
            raise org_mode.ConfigurationError("agent_runtimes must be an object holding an n8n object.")
        enabled = n8n.get("enabled", False)
        if not isinstance(enabled, bool):
            raise org_mode.ConfigurationError("agent_runtimes.n8n.enabled must be true or false.")
        project_id = n8n.get("project_id", "")
        if not isinstance(project_id, str) or (project_id and not _PROJECT_ID_RE.fullmatch(project_id)):
            raise org_mode.ConfigurationError(
                "agent_runtimes.n8n.project_id must be empty or 1 to 36 letters and digits."
            )
        mcp_url = n8n.get("mcp_url", "")
        if not isinstance(mcp_url, str) or (mcp_url and not _valid_mcp_url(mcp_url)):
            raise org_mode.ConfigurationError(
                "agent_runtimes.n8n.mcp_url must be empty, an https:// URL, or http:// on localhost."
            )
        raw_url = env.get(N8N_URL_ENV, "").strip()
        return cls(
            enabled=enabled,
            base_url=normalize_n8n_url(raw_url) if raw_url else "",
            project_id=project_id,
            mcp_url=mcp_url,
        )


@dataclass(frozen=True)
class DeliveryTarget:
    runtime: str
    workflow_id: str


def _split(raw: str) -> SplitResult | None:
    """The parts of ``raw`` when it is a URL with a host, no userinfo and no fragment; else None."""
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError for a bad port
    except ValueError:
        return None
    if not parts.hostname or "@" in parts.netloc or parts.fragment or "#" in raw:
        return None
    return parts


def _valid_mcp_url(raw: str) -> bool:
    parts = _split(raw)
    if parts is None:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


def normalize_n8n_url(raw: str) -> str:
    """The n8n base URL with a trailing ``/`` removed, or a ``ConfigurationError``."""
    bad = org_mode.ConfigurationError(
        f"{N8N_URL_ENV} must be an https:// address without credentials, query or fragment, "
        "and not a link-local, unspecified or multicast address."
    )
    raw = raw.strip()
    parts = _split(raw)
    if parts is None or parts.scheme != "https" or "?" in raw:
        raise bad
    host = parts.hostname or ""
    if host.lower().rstrip(".") in _REFUSED_HOSTS:
        raise bad
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise bad
    return raw.rstrip("/")


def endpoint(url: str) -> tuple[str, str, int]:
    """``(scheme, lower-case host, port)`` of ``url``; the port defaults to 443 or 80."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def api_key() -> str | None:
    """The n8n API key, read per use. Lets ``ConfigurationError`` through."""
    return org_mode.provisioning_key(N8N_API_KEY_ENV)


def _key_or_none() -> str | None:
    try:
        return api_key()
    except org_mode.ConfigurationError as exc:
        logger.warning("%s", exc)
        return None


def delivery_available(config: N8nRuntimeConfig | None) -> bool:
    return bool(config and config.enabled and config.base_url and _key_or_none())


def parse_deliver_to(raw: object) -> DeliveryTarget:
    if not isinstance(raw, dict):
        raise ValueError("deliver_to is {runtime: 'n8n', workflow_id}.")
    if not set(raw) <= {"runtime", "workflow_id"}:
        raise ValueError("deliver_to takes only runtime and workflow_id.")
    if raw.get("runtime") != "n8n":
        raise ValueError("The only runtime PrivacyFence delivers to is n8n.")
    workflow_id = raw.get("workflow_id", "")
    if not isinstance(workflow_id, str) or (workflow_id and not _WORKFLOW_ID_RE.fullmatch(workflow_id)):
        raise ValueError("workflow_id is the n8n workflow's id: 1 to 36 letters and digits.")
    return DeliveryTarget(runtime="n8n", workflow_id=workflow_id)


def credential_name(label: str) -> str:
    return f"PrivacyFence {label}"


def mcp_url_for(config: N8nRuntimeConfig, base_url: str) -> str:
    return config.mcp_url or base_url.rstrip("/") + "/mcp"


def api_for(config: N8nRuntimeConfig) -> N8nApi | None:
    key = _key_or_none()
    if not config.base_url or not key:
        return None
    return N8nApi(config.base_url, key)
