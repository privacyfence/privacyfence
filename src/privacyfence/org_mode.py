"""``mode: local`` vs. ``mode: org`` -- the one setting every other
difference between the two operating modes follows from. Lives in
``org_bundle.json`` (alongside the org server, TLS and IdP settings), not
settings.yaml: it's
an install-wide decision, not a per-user preference, and org_bundle.json is
already the file daemon_main.py reads before it knows anything about a
principal at all.

Absent entirely, ``mode`` resolves to ``"local"`` -- an existing install's
org_bundle.json (today only ever carrying Google/Slack/Salesforce/
Atlassian app registrations, per daemon_main.py's own module docstring)
keeps meaning exactly what it already means, with no migration.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from .grist_auth import GristClientError, normalize_server_url
from .peek_client import PeekClientError, normalize_base_url

logger = logging.getLogger(__name__)

Mode = Literal["local", "org"]

DEFAULT_MODE: Mode = "local"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

MODE_ENV = "PRIVACYFENCE_MODE"
BIND_HOST_ENV = "PRIVACYFENCE_BIND_HOST"
PORT_ENV = "PRIVACYFENCE_PORT"
ISSUER_URL_ENV = "PRIVACYFENCE_ISSUER_URL"
TRUSTED_PROXIES_ENV = "PRIVACYFENCE_TRUSTED_PROXIES"

IDP_ISSUER_ENV = "PRIVACYFENCE_IDP_ISSUER"
IDP_CLIENT_ID_ENV = "PRIVACYFENCE_IDP_CLIENT_ID"
IDP_CLIENT_SECRET_ENV = "PRIVACYFENCE_IDP_CLIENT_SECRET"  # nosec B105  # a variable name, not a credential
IDP_CLIENT_SECRET_FILE_ENV = "PRIVACYFENCE_IDP_CLIENT_SECRET_FILE"  # nosec B105  # a variable name, not a credential
IDP_ADMIN_GROUP_CLAIM_ENV = "PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM"
IDP_ADMIN_GROUP_VALUES_ENV = "PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES"
IDP_STEP_UP_ACR_VALUES_ENV = "PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES"
IDP_TRUST_UNVERIFIED_EMAIL_ENV = "PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL"
ADMIN_EMAILS_ENV = "PRIVACYFENCE_ADMIN_EMAILS"
_IDP_ENV_PREFIX = "PRIVACYFENCE_IDP_"

# Hostnames a single-user install may be served at: it has one administrator who signs in with a
# setup code, which is only acceptable when nothing but this computer can reach it.
SINGLE_USER_HOSTS = ("localhost", "127.0.0.1")

# Shown in the daemon log, privacyfence_status, every desktop page and (once) the companion.
LOCAL_MODE_DEPRECATION = (
    "Desktop mode is deprecated and will be removed in PrivacyFence 8. PrivacyFence's built-in "
    "sign-in for Google, Slack, Salesforce, Atlassian and Miro works only in organization mode "
    "and the Docker image. Move to an organization server, or try the Docker image on this "
    "computer: https://privacyfence.eu/docs/moving-to-organization-mode/"
)


class ConfigurationError(ValueError):
    """Raised for organization configuration that is present but broken --
    unreadable, malformed JSON, a non-object top level, an invalid
    ``mode``, or (in org mode) missing/incomplete required sections --
    rather than genuinely absent. daemon_main.py's ``main()``
    never catches this specifically: it's a ``ValueError`` subclass, so it
    falls into the same "print and refuse to start" path every other
    startup configuration error already takes, deliberately -- there is no
    silent fallback to local mode for a *broken* config, only for a
    missing one (see load_org_config's own docstring for why that
    distinction matters)."""


def _env(name: str) -> str:
    """An environment value, stripped; a variable that is set but empty counts as unset, so a
    container that passes ``-e NAME=`` behaves like one that does not pass it."""
    return os.environ.get(name, "").strip()


def resolve_mode(org_config: dict[str, Any]) -> Mode:
    """The bundle's ``mode``, else ``PRIVACYFENCE_MODE``, else ``"local"``.

    A bundle and the variable that disagree refuse the start rather than letting either win
    silently: the person who set the variable would otherwise get the other mode without a word.
    """
    env_mode = _env(MODE_ENV)
    if env_mode and env_mode not in ("local", "org"):
        raise ConfigurationError(f"{MODE_ENV} must be \"local\" or \"org\", got {env_mode}")
    if "mode" in org_config:
        mode = org_config["mode"]
        if mode not in ("local", "org"):
            raise ConfigurationError(f"org_bundle.json's \"mode\" must be \"local\" or \"org\", got {mode!r}")
        if env_mode and env_mode != mode:
            raise ConfigurationError(
                f"org_config.json says \"mode\": \"{mode}\" but {MODE_ENV} says \"{env_mode}\"; "
                "remove one of them"
            )
        return mode
    return env_mode or DEFAULT_MODE  # type: ignore[return-value]  # validated against both literals above


def _idp_env_names_set() -> bool:
    """Whether any variable that configures the identity provider is set."""
    return bool(_env(ADMIN_EMAILS_ENV)) or any(
        name.startswith(_IDP_ENV_PREFIX) and value.strip() for name, value in os.environ.items()
    )


def _idp_present(org_config: dict[str, Any]) -> bool:
    """Whether an identity provider is configured, by the bundle or by the environment.

    Reads no secret, writes no log line and does no network discovery, so the mode checks that run
    before the server is built can call it freely."""
    return bool(org_config.get("idp")) or bool(_env(IDP_ISSUER_ENV) and _env(IDP_CLIENT_ID_ENV))


def is_single_user(org_config: dict[str, Any]) -> bool:
    """Organization mode with no identity provider: one administrator, on this computer only."""
    return resolve_mode(org_config) == "org" and not _idp_present(org_config)


_env_ignored_warned = False


def _comma_list(name: str) -> list[str]:
    return [part.strip() for part in _env(name).split(",") if part.strip()]


def _client_secret_from_env() -> str:
    inline = os.environ.get(IDP_CLIENT_SECRET_ENV, "")
    path = _env(IDP_CLIENT_SECRET_FILE_ENV)
    if inline and path:
        raise ConfigurationError(
            f"Set {IDP_CLIENT_SECRET_ENV} or {IDP_CLIENT_SECRET_FILE_ENV}, not both."
        )
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().rstrip("\r\n")
        except (OSError, UnicodeDecodeError):
            # The reason is dropped on purpose: a decode error quotes the bytes it choked on.
            raise ConfigurationError(f"{IDP_CLIENT_SECRET_FILE_ENV} could not be read.") from None
    return inline


def provisioning_key(key_env: str) -> str | None:
    """The Peek or Grist provisioning key, from ``key_env`` or from the file ``key_env_FILE`` names.

    ``None`` when neither is set. Setting both is an error, and so is a file that cannot be
    read; the message names the variable and never the file's content or the key."""
    file_env = f"{key_env}_FILE"
    inline = _env(key_env)
    path = _env(file_env)
    if inline and path:
        raise ConfigurationError(f"Set {key_env} or {file_env}, not both.")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                key = handle.read().rstrip("\r\n").strip()
        except (OSError, UnicodeDecodeError):
            # The reason is dropped on purpose: a decode error quotes the bytes it choked on.
            raise ConfigurationError(f"{file_env} could not be read.") from None
        return key or None
    return inline or None


def idp_section(org_config: dict[str, Any]) -> dict[str, Any] | None:
    """The ``idp`` section the identity-provider settings are read from.

    A non-empty bundle section wins, unchanged. Otherwise a section built from the
    ``PRIVACYFENCE_IDP_*`` variables and ``PRIVACYFENCE_ADMIN_EMAILS``, when the issuer and the
    client id are both set; a partial environment is an error rather than single-user mode, so a
    typo never turns a real server into one. ``None`` when nothing configures an IdP."""
    global _env_ignored_warned
    bundle_idp = org_config.get("idp")
    if bundle_idp:
        if _idp_env_names_set() and not _env_ignored_warned:
            _env_ignored_warned = True
            logger.warning(
                "PRIVACYFENCE_IDP_* and PRIVACYFENCE_ADMIN_EMAILS are ignored: org_bundle.json has an "
                "\"idp\" section."
            )
        return bundle_idp
    issuer, client_id = _env(IDP_ISSUER_ENV), _env(IDP_CLIENT_ID_ENV)
    if not issuer and not client_id:
        if _idp_env_names_set():
            raise ConfigurationError(f"Set both {IDP_ISSUER_ENV} and {IDP_CLIENT_ID_ENV}, or neither.")
        return None
    if not issuer or not client_id:
        raise ConfigurationError(f"Set both {IDP_ISSUER_ENV} and {IDP_CLIENT_ID_ENV}, or neither.")
    section: dict[str, Any] = {"issuer": issuer, "client_id": client_id}
    secret = _client_secret_from_env()
    if secret:
        section["client_secret"] = secret
    if _env(IDP_ADMIN_GROUP_CLAIM_ENV):
        section["admin_group_claim"] = _env(IDP_ADMIN_GROUP_CLAIM_ENV)
    for key, name in (
        ("admin_group_values", IDP_ADMIN_GROUP_VALUES_ENV),
        ("step_up_acr_values", IDP_STEP_UP_ACR_VALUES_ENV),
        ("admin_emails", ADMIN_EMAILS_ENV),
    ):
        values = _comma_list(name)
        if values:
            section[key] = values
    trust = _env(IDP_TRUST_UNVERIFIED_EMAIL_ENV)
    if trust:
        if trust not in ("true", "false"):
            raise ConfigurationError(f"{IDP_TRUST_UNVERIFIED_EMAIL_ENV} must be \"true\" or \"false\".")
        section["trust_unverified_email"] = trust == "true"
    return section


def is_loopback_issuer(issuer_url: str) -> bool:
    """Whether ``issuer_url`` is plain http at a name only this computer answers to."""
    parsed = urlsplit(issuer_url)
    return parsed.scheme == "http" and parsed.hostname in SINGLE_USER_HOSTS


def reject_removed_tool_annotations(org_config: dict[str, Any]) -> None:
    """Refuses a bundle that still carries ``mcp.tool_annotations``.

    Only a bundle built by 5.0.0a1's ``build_org_bundle.py --tool-annotations`` can carry it. The
    key chose between truthful tool annotations and advertising every connector tool read-only;
    the second was removed and truthful annotations are the only behaviour (ADR 0089). A bundle
    naming the key -- whichever value -- is refused with ``ConfigurationError`` rather than read
    and ignored, so an administrator who asked for all-read-only never silently gets something
    else: the daemon does not start until the bundle is rebuilt without the flag."""
    section = org_config.get("mcp")
    if isinstance(section, dict) and "tool_annotations" in section:
        raise ConfigurationError(
            "org_bundle.json's \"mcp.tool_annotations\" has been removed: PrivacyFence always tells "
            "AI clients which tools are read-only and which write (ADR 0089). Rebuild the bundle "
            "without --tool-annotations."
        )


@dataclass(frozen=True)
class ServerConfig:
    """Org mode's listener and transport settings, made concrete per install
    (see docs/org-mode-setup-guide.md's "Reverse proxy and TLS"). Only org mode
    reads this class -- ``daemon_main.py`` calls ``from_org_config`` only
    when ``resolve_mode`` says org, and local mode binds its own hardcoded
    ``localhost``. The defaults (``bind_host="127.0.0.1"``, no TLS, no
    trusted proxies) are the same loopback listener
    ``scripts/build_org_bundle.py`` writes when ``--server-bind-host`` isn't
    passed, so a bundle with no ``bind_host`` binds exactly what a bundle
    built with no flags does: reachable only by a reverse proxy on the
    same host, never directly from the network.
    """

    bind_host: str = DEFAULT_BIND_HOST
    port: int = DEFAULT_PORT
    # The externally-reachable origin used to build OAuth/OIDC redirect
    # URIs (web/oauth_provider.py's IdP callback, web/routes_org_
    # identity.py's login callback, and the AS's own issuer_url) -- may
    # differ from bind_host:port when this daemon sits behind a reverse
    # proxy or load balancer.
    issuer_url: str = ""
    cert_file: str = ""
    key_file: str = ""
    # X-Forwarded-For / X-Forwarded-Proto are honored only when an explicit
    # trusted_proxies list is configured, never by default: otherwise any
    # client that can reach the listener could claim to be HTTPS or spoof its
    # own address.
    trusted_proxies: tuple[str, ...] = ()

    @property
    def tls_configured(self) -> bool:
        return bool(self.cert_file and self.key_file)

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "ServerConfig":
        raw = org_config.get("server")
        raw = raw if isinstance(raw, dict) else {}
        tls = raw.get("tls")
        tls = tls if isinstance(tls, dict) else {}
        # Every value the bundle's "server" section leaves unset may come from the environment,
        # which is how the Docker image configures a server that has no bundle. The bundle wins.
        bind_host = raw.get("bind_host") or _env(BIND_HOST_ENV) or DEFAULT_BIND_HOST
        if raw.get("port") is not None:
            port = int(raw["port"])
        elif _env(PORT_ENV):
            port = _port_from_env()
        else:
            port = DEFAULT_PORT
        # Stripped before anything derives from it: web/server.py builds
        # the Host allowlist from urlsplit(issuer_url).hostname, which
        # keeps a stray trailing space *inside* the hostname
        # ("pf.acme.example.com "), while routes_mcp.py's pydantic
        # AnyHttpUrl normalizes the same string and accepts it. The daemon
        # then starts cleanly and rejects every single request with
        # "Invalid Host header" -- a message pointing at Host headers and
        # reverse proxies rather than at the one config value at fault.
        issuer_url = str(raw.get("issuer_url") or "").strip() or _env(ISSUER_URL_ENV)
        if not issuer_url and is_single_user(org_config):
            issuer_url = f"http://localhost:{port}"
        if not issuer_url:
            raise ConfigurationError("org mode requires org_bundle.json's \"server\".\"issuer_url\"")
        # Fail here, naming the key, rather than several frames later
        # inside mount_org_oauth's AnyHttpUrl() -- and, for a scheme that
        # parses but carries no hostname, rather than not at all (the
        # Host allowlist silently gains nothing and every request 400s).
        parsed = urlsplit(issuer_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigurationError(
                "org_bundle.json's \"server\".\"issuer_url\" must be an absolute http(s) URL "
                f"with a hostname (e.g. https://pf.acme.example.com), got {issuer_url!r}"
            )
        trusted_proxies = tuple(raw.get("trusted_proxies") or ()) or tuple(
            part.strip() for part in _env(TRUSTED_PROXIES_ENV).split(",") if part.strip()
        )
        if any(str(proxy).strip() in _WILDCARD_PROXIES for proxy in trusted_proxies):
            raise ConfigurationError("trusted_proxies must name specific proxies, not a wildcard.")
        return ServerConfig(
            bind_host=bind_host,
            port=port,
            issuer_url=issuer_url,
            cert_file=tls.get("cert_file", ""),
            key_file=tls.get("key_file", ""),
            trusted_proxies=trusted_proxies,
        )


# A trusted-proxy entry that would make every client a trusted proxy, so any caller could forge
# X-Forwarded-For / X-Forwarded-Proto.
_WILDCARD_PROXIES = frozenset({"*", "0.0.0.0/0", "::/0"})


def _port_from_env() -> int:
    raw = _env(PORT_ENV)
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{PORT_ENV} must be an integer from 1 to 65535, got {raw!r}")
    return port


# A cap on the *tool result*, not on the file: the length of the JSON text
# web/mcp_tools.to_call_tool_result sends for an inline download, base64
# payload and envelope included (see inline_result_length). The ceiling
# that matters is what the client accepts as one tool result, and the
# clients org mode exists for are the hosted ones: Anthropic documents a
# maximum tool result size of ~150,000 characters for claude.ai and
# Claude Desktop (https://claude.com/docs/connectors/building, "Design
# within the size and timeout limits"). The old 8,000,000-byte default
# was a file size, so a 1.7 MB PDF (~2.3 MB once base64-encoded) went
# inline and claude.ai truncated it (manual QA of 5.0.0a2, see
# https://github.com/privacyfence/privacyfence/issues/46). 100,000 leaves
# a third of that documented, approximate limit as headroom and still
# inlines files up to ~75 KB; anything larger gets a one-time link, which
# works for any client that can reach this server's host -- claude.ai only
# once the host is on its domain allowlist. See ADRs 0092 and 0097.
DEFAULT_INLINE_MAX_BYTES = 100_000


def base64_length(size_bytes: int) -> int:
    """Length of ``base64.b64encode`` output for ``size_bytes`` input bytes
    -- the 4/3 inflation an inline download's content pays, padding
    included."""
    return 4 * ((size_bytes + 2) // 3)


def _inline_envelope(name: str, mime_type: str, size_bytes: int, content_base64: str) -> dict[str, Any]:
    return {
        "delivery": "inline",
        "name": name,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "content_base64": content_base64,
    }


def inline_result_length(name: str, mime_type: str, size_bytes: int) -> int:
    """Length of the tool-result text an inline download of ``size_bytes``
    would produce, computed without encoding anything: the JSON envelope
    with an empty ``content_base64``, plus the base64 length (base64's
    alphabet needs no JSON escaping, so the two simply add). Serialized the
    way web/mcp_tools.to_call_tool_result serializes it."""
    envelope = json.dumps(_inline_envelope(name, mime_type, size_bytes, ""), default=str)
    return len(envelope) + base64_length(size_bytes)

# 5 minutes -- see download_staging.DEFAULT_TTL_SECONDS's own docstring for
# why this is short: staging means an encrypted-but-real copy of the file
# sits on the server's disk for this long.
DEFAULT_LINK_TTL_SECONDS = 300.0


# What a staged download's tool result tells the AI client. Nothing is on
# any disk the client or the human can see: destination_dir was ignored.
LINK_RESULT_NOTE = (
    "Nothing was saved to a file: on an organization-managed install destination_dir is ignored. "
    "The file is only at download_url, a one-time link that expires at expires_at. "
    "Fetch it yourself if you can reach this server; otherwise give the link to the user "
    "to open in their own browser. Don't refer to it as a local path."
)


@dataclass(frozen=True)
class DownloadDeliveryConfig:
    """How org mode delivers file bytes for ``drive_download_file``/
    ``gmail_download_attachment``/``confluence_download_attachment`` to a
    principal who has no shell on the daemon's own machine: inline in the
    MCP tool result when the whole result fits ``inline_max_bytes``, or
    staged behind a short-lived, single-claim link when it does not (ADR
    0092 for the limit's unit and default; see ADR 0017
    for the rationale and the inline/staging split). Lives in
    ``org_bundle.json``'s ``download_delivery`` section, org-mode-only for
    the same reason ``ServerConfig`` is -- local mode never reads this
    class. That doesn't mean local mode still writes straight to
    ``destination_dir`` unchanged: since ADR 0007, a privilege-separated
    local-mode daemon can't write to the user's directories either, so it
    hands the file to the ``.mcpb`` shim instead, or, with no shim in the
    request path, returns a single-use capability link of its own (ADR
    0028) -- a separate delivery path with its own config, not this one.
    """

    inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES
    link_ttl_seconds: float = DEFAULT_LINK_TTL_SECONDS
    # The org-level opt-out (ADR 0017's "Organizations can move the line"
    # paragraph): when False, a file too large for inline delivery is
    # refused outright
    # rather than ever being written -- encrypted or not -- to this
    # server's disk. Default True: encryption-at-rest (download_staging.py)
    # is the primary mitigation, and staging still happens for oversized
    # files by default.
    allow_disk_staging: bool = True
    # ADR 0028 (and ADR 0007's "Clients without the bridge" section): every
    # staged-link download in org mode is reached
    # by an MCP client -- an agent, not a human with a browser -- so the
    # default is the capability link (/mcp-files/fetch/<token>, no bearer
    # header or session cookie needed: the token in the URL is the
    # credential). Setting this False keeps the older cookie-authenticated
    # browser link (/downloads/{token}) instead -- for an org that wants a
    # human to be the one who actually opens a staged download, not the
    # agent fetching it directly on their behalf.
    agent_links: bool = True
    # Whether the export tools (grist_export_records, grist_export_sql,
    # salesforce_export_report, jira_export_issues, assets_export_objects)
    # exist at all. Each stages the full file on this server until the user
    # claims it, so an organization that does not want user data held there
    # turns them off: they are then left out of the tool list and refused if
    # called. Unlike allow_disk_staging, which only refuses at call time.
    allow_exports: bool = True

    def staged_link_path(self, token: bytes) -> str:
        """The URL path a staged download's ``download_url`` should use,
        given this config's own ``agent_links`` choice: the agent-facing
        capability route (no bearer header or session cookie needed -- the
        token in the URL is the credential) by default, or the older
        cookie-authenticated browser route when an org has opted back into
        it. Both routes serve the same ``download_staging.
        DownloadStagingStore`` entry, keyed by the same token -- only which
        HTTP endpoint (and therefore which kind of caller) can claim it
        differs. Used by connectors/drive.py, connectors/gmail.py and
        connectors/confluence.py's own org-mode staged-link delivery, so
        the choice is made in exactly one place rather than three."""
        encoded = base64.urlsafe_b64encode(token).decode("ascii")
        return f"/mcp-files/fetch/{encoded}" if self.agent_links else f"/downloads/{encoded}"

    def link_result(self, name: str, size_bytes: int, download_url: str) -> dict[str, Any]:
        """The tool result for a download staged behind ``download_url``,
        shared by the three connectors that deliver files this way.

        ``note`` says in plain words that nothing was saved to a path. The
        tools' ``destination_dir`` is required (local mode needs it) and
        ignored here, and a remote client that passed ``~/Downloads``
        otherwise goes on to treat that path as the file's location
        (Gemini Enterprise did, trying to upload the "saved" file)."""
        return {
            "delivery": "link",
            "name": name,
            "size_bytes": size_bytes,
            "download_url": download_url,
            "expires_at": datetime.fromtimestamp(time.time() + self.link_ttl_seconds, tz=timezone.utc).isoformat(),
            "note": LINK_RESULT_NOTE,
        }

    def fits_inline(self, size_bytes: int, name: str = "", mime_type: str = "") -> bool:
        """Whether a file this size would be delivered inline (base64, in
        the tool result) rather than staged behind a one-time link: the
        whole tool result, base64 inflation and envelope included, must fit
        ``inline_max_bytes`` (see inline_result_length). Used before the
        bytes are fetched -- for the approval preview, the audit record and
        the staging-disabled early refusal -- so it works from a metadata
        size; inline_result makes the binding decision on the real bytes.
        ``inline_max_bytes == 0`` (the "force every download through a
        staged link, unconditionally" knob) always returns False here --
        even for an empty (0-byte) file."""
        return (
            self.inline_max_bytes > 0
            and inline_result_length(name, mime_type, size_bytes) <= self.inline_max_bytes
        )

    def inline_result(self, name: str, mime_type: str, data: bytes) -> dict[str, Any] | None:
        """The inline tool result for ``data``, or None when it would not
        fit ``inline_max_bytes`` -- the caller then stages a link (or
        refuses, with staging disabled) instead of sending a result the
        client would truncate. Decided on the real byte count, which can
        differ from the metadata size fits_inline was first asked about
        (a Google Workspace export, say); inline_result_length is exact for
        the envelope built here, so the result returned is never over the
        limit."""
        if not self.fits_inline(len(data), name, mime_type):
            return None
        return _inline_envelope(name, mime_type, len(data), base64.b64encode(data).decode("ascii"))

    def over_inline_limit_message(self, subject: str, size_bytes: int) -> str:
        """The staging-disabled refusal, stated in the unit the limit is
        actually in -- a file under ``inline_max_bytes`` can still be over
        it once base64-encoded."""
        return (
            f"{subject} is {size_bytes:,} bytes ({base64_length(size_bytes):,} once base64-encoded "
            f"for the tool result), over this organization's {self.inline_max_bytes:,}-byte "
            "inline-delivery limit, and disk staging is disabled for this organization -- there is "
            "no way to deliver it through this tool. Ask the user for a narrower export or a "
            "different way to share it."
        )

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "DownloadDeliveryConfig":
        raw = org_config.get("download_delivery")
        raw = raw if isinstance(raw, dict) else {}
        inline_max_bytes = int(raw.get("inline_max_bytes", DEFAULT_INLINE_MAX_BYTES))
        if inline_max_bytes < 0:
            raise ConfigurationError(
                "org_bundle.json's \"download_delivery\".\"inline_max_bytes\" must be >= 0 "
                f"(0 forces every download through a staged link), got {inline_max_bytes}"
            )
        link_ttl_seconds = float(raw.get("link_ttl_seconds", DEFAULT_LINK_TTL_SECONDS))
        if link_ttl_seconds <= 0:
            raise ConfigurationError(
                "org_bundle.json's \"download_delivery\".\"link_ttl_seconds\" must be > 0, "
                f"got {link_ttl_seconds}"
            )
        return DownloadDeliveryConfig(
            inline_max_bytes=inline_max_bytes,
            link_ttl_seconds=link_ttl_seconds,
            allow_disk_staging=bool(raw.get("allow_disk_staging", True)),
            agent_links=bool(raw.get("agent_links", True)),
            allow_exports=bool(raw.get("allow_exports", True)),
        )


AuditForwardingKind = Literal["syslog", "http"]
DEFAULT_AUDIT_FORWARDING_KIND: AuditForwardingKind = "syslog"

SyslogProtocol = Literal["udp", "tcp"]
DEFAULT_SYSLOG_PROTOCOL: SyslogProtocol = "tcp"
# IANA's syslog-tls port (RFC 5425), not 514 (RFC 5424's plaintext-only
# default) -- see audit_forwarding.py's own module docstring for why this
# module doesn't speak TLS itself and expects this port to be fronted by a
# TLS-terminating relay when that matters.
DEFAULT_SYSLOG_PORT = 6514


@dataclass(frozen=True)
class AuditForwardingConfig:
    """Org mode's centralized audit-log forwarding destination. Lives in
    ``org_bundle.json``'s ``audit_forwarding`` section, org-mode-only like
    ``ServerConfig``/``DownloadDeliveryConfig`` above -- local mode never
    looks at this at all (there is no "centralize" to speak of for a single
    employee's own machine); see daemon_main.py's ``run_app`` for the
    ``resolve_mode(org_config) == "org"`` gate.

    Forwarding is *additional* visibility, never a replacement for the
    local audit log ``audit_log.py``'s ``AuditLogger`` always writes --
    that JSONL file (with its own append-integrity hash chain, always on
    regardless of this config) stays the
    authoritative record even when forwarding is enabled and even when a
    specific entry fails to forward. See ``audit_forwarding.py`` for what
    each ``kind`` actually sends on the wire.
    """

    enabled: bool = False
    kind: AuditForwardingKind = DEFAULT_AUDIT_FORWARDING_KIND
    syslog_host: str = ""
    syslog_port: int = DEFAULT_SYSLOG_PORT
    syslog_protocol: SyslogProtocol = DEFAULT_SYSLOG_PROTOCOL
    http_url: str = ""
    # Name of an environment variable *this daemon's own process*  reads a
    # bearer token from at forward-send time -- never itself stored in
    # org_bundle.json, so a leaked or mis-shared bundle doesn't also leak
    # the SIEM credential (the same reasoning behind every OAuth client
    # secret in this file being a real secret, just via env var since
    # there's no build-time secrets store equivalent for an IT-run
    # server's own SIEM API key).
    http_bearer_token_env: str = ""

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "AuditForwardingConfig":
        raw = org_config.get("audit_forwarding")
        raw = raw if isinstance(raw, dict) else {}
        enabled = bool(raw.get("enabled", False))
        kind = raw.get("kind", DEFAULT_AUDIT_FORWARDING_KIND)
        if kind not in ("syslog", "http"):
            raise ConfigurationError(
                f"org_bundle.json's \"audit_forwarding\".\"kind\" must be \"syslog\" or \"http\", "
                f"got {kind!r}"
            )
        syslog_raw = raw.get("syslog")
        syslog_raw = syslog_raw if isinstance(syslog_raw, dict) else {}
        http_raw = raw.get("http")
        http_raw = http_raw if isinstance(http_raw, dict) else {}

        syslog_protocol = syslog_raw.get("protocol", DEFAULT_SYSLOG_PROTOCOL)
        if syslog_protocol not in ("udp", "tcp"):
            raise ConfigurationError(
                f"org_bundle.json's \"audit_forwarding\".\"syslog\".\"protocol\" must be \"udp\" "
                f"or \"tcp\", got {syslog_protocol!r}"
            )
        http_url = str(http_raw.get("url", "") or "")
        if enabled and kind == "http" and http_url and not http_url.startswith("https://"):
            raise ConfigurationError(
                "org_bundle.json's \"audit_forwarding\".\"http\".\"url\" must use https:// -- "
                "audit entries are sensitive, and this endpoint is otherwise reached over "
                "plaintext HTTP."
            )
        return AuditForwardingConfig(
            enabled=enabled,
            kind=kind,
            syslog_host=str(syslog_raw.get("host", "") or ""),
            syslog_port=int(syslog_raw.get("port", DEFAULT_SYSLOG_PORT)),
            syslog_protocol=syslog_protocol,
            http_url=http_url,
            http_bearer_token_env=str(http_raw.get("bearer_token_env", "") or ""),
        )


@dataclass(frozen=True)
class AuthzPolicyConfig:
    """An optional PrivacyFence-level allowlist layered *on top of* the IdP's own
    authentication, not a replacement for it -- the IdP has already decided
    who this human is by the time anything here runs (org_identity.py's
    ``check_authz_policy`` is only ever called after ``principal_from_
    claims`` has a real ``Principal`` in hand); this decides whether
    PrivacyFence itself is willing to admit them.

    Exists because, without it, for a plain (non-Workspace) Google IdP the
    OAuth consent screen's own test-user list or verification status is the
    *only* access control most org-mode deployments have -- an IdP-side
    setting this repo can't see or audit, let alone enforce consistently
    across a different IdP. docs/org-mode-setup-guide.md's "Who may sign in"
    documents the flags that populate it.

    Lives in ``org_bundle.json``'s ``authz`` section, org-mode-only like
    every other org_mode.py config class. Absent entirely (or an ``authz``
    section with neither list set) means ``enabled`` is ``False`` -- "no
    additional restriction, every IdP-authenticated principal is admitted"
    -- so an existing org-mode install with no ``authz`` section keeps
    working exactly as before this landed, the same additive/opt-in
    posture every other org-mode config in this module already has.
    """

    # Case-folded, leading-"@"-stripped at parse time (see from_org_config)
    # so "acme.com", "Acme.com" and "@acme.com" in org_bundle.json all mean
    # the same thing -- matched against the domain half of the principal's
    # own (IdP-asserted) email.
    allowed_domains: tuple[str, ...] = ()
    # ID token claim (e.g. "groups") that carries group membership -- kept
    # separate from IdpConfig.admin_group_claim (a different question:
    # "is this human an admin", not "may this human sign in at all") so an
    # org can gate sign-in on group membership without also having to
    # configure -- or share values with -- the admin mapping.
    groups_claim: str = ""
    required_groups: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_domains) or bool(self.required_groups)

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "AuthzPolicyConfig":
        raw = org_config.get("authz")
        raw = raw if isinstance(raw, dict) else {}
        raw_domains = (str(d).strip().lower().lstrip("@") for d in (raw.get("allowed_domains") or ()))
        allowed_domains = tuple(domain for domain in raw_domains if domain)
        required_groups = tuple(str(g) for g in (raw.get("required_groups") or ()) if str(g))
        groups_claim = raw.get("groups_claim", "") or ""
        if required_groups and not groups_claim:
            raise ConfigurationError(
                "org_bundle.json's \"authz\".\"required_groups\" is set but \"groups_claim\" is "
                "empty -- PrivacyFence has no ID token claim to read group membership from"
            )
        return AuthzPolicyConfig(
            allowed_domains=allowed_domains, groups_claim=groups_claim, required_groups=required_groups,
        )


@dataclass(frozen=True)
class WebPushConfig:
    """Whether org mode sends a web push notification when an approval is waiting (ADR 0081).

    Lives in ``org_bundle.json``'s ``web_push`` section and is written by
    ``scripts/build_org_bundle.py --no-web-push``. On by default: push is what lets a phone user
    learn about an approval at all, since mobile browsers suspend background tabs. Turning it off
    is the org-wide switch for an organization that does not want approval metadata, however
    little, to pass through Apple's, Google's or Mozilla's push services. Local mode never reads
    this class and never pushes.
    """

    enabled: bool = True

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "WebPushConfig":
        raw = org_config.get("web_push")
        raw = raw if isinstance(raw, dict) else {}
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(
                f"org_bundle.json's \"web_push\".\"enabled\" must be true or false, got {enabled!r}"
            )
        return WebPushConfig(enabled=enabled)


DEFAULT_GRIST_PROVISIONING_KEY_ENV = "PRIVACYFENCE_GRIST_PROVISIONING_KEY"
DEFAULT_PEEK_PROVISIONING_KEY_ENV = "PRIVACYFENCE_PEEK_PROVISIONING_KEY"
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


@dataclass(frozen=True)
class PeekProvisioningConfig:
    """Whether org mode creates a Peek account for each person at sign-in and connects it for them.

    Lives in ``org_bundle.json``'s ``peek.auto_provision`` section and is written by
    ``scripts/build_org_bundle.py --peek-auto-provision``. Off by default. The provisioning key
    itself is never in the bundle: ``key_env`` names the environment variable the daemon reads it
    from, as ``AuditForwardingConfig.http_bearer_token_env`` does for the SIEM bearer token.
    ``trust_unverified_email`` provisions people whose ID token does not say ``email_verified``,
    for providers that only issue verified, organization-owned addresses. ``base_url`` is the
    normalised ``peek.base_url`` and is set only when enabled. Local mode never provisions.
    """

    enabled: bool = False
    key_env: str = DEFAULT_PEEK_PROVISIONING_KEY_ENV
    trust_unverified_email: bool = False
    base_url: str = ""

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "PeekProvisioningConfig":
        peek = org_config.get("peek")
        peek = peek if isinstance(peek, dict) else {}
        raw = peek.get("auto_provision")
        raw = raw if isinstance(raw, dict) else {}
        flags: dict[str, bool] = {}
        for key in ("enabled", "trust_unverified_email"):
            value = raw.get(key, False)
            if not isinstance(value, bool):
                raise ConfigurationError(
                    f"org_bundle.json's \"peek\".\"auto_provision\".\"{key}\" must be true or false, got {value!r}"
                )
            flags[key] = value
        key_env = raw.get("key_env", DEFAULT_PEEK_PROVISIONING_KEY_ENV)
        if not isinstance(key_env, str) or not _ENV_NAME_RE.fullmatch(key_env):
            raise ConfigurationError(
                "org_bundle.json's \"peek\".\"auto_provision\".\"key_env\" must be an environment variable "
                f"name, got {key_env!r}"
            )
        if not flags["enabled"]:
            return PeekProvisioningConfig(
                key_env=key_env, trust_unverified_email=flags["trust_unverified_email"],
            )
        if resolve_mode(org_config) != "org":
            raise ConfigurationError("org_bundle.json's \"peek\".\"auto_provision\" needs \"mode\": \"org\"")
        if not peek.get("base_url"):
            raise ConfigurationError("org_bundle.json's \"peek\".\"auto_provision\" needs \"peek\".\"base_url\"")
        try:
            base_url = normalize_base_url(str(peek["base_url"]))
        except PeekClientError as exc:
            raise ConfigurationError(f"org_bundle.json's \"peek\".\"base_url\" is not usable: {exc}") from exc
        return PeekProvisioningConfig(
            enabled=True, key_env=key_env, trust_unverified_email=flags["trust_unverified_email"],
            base_url=base_url,
        )


@dataclass(frozen=True)
class GristProvisioningConfig:
    """Whether org mode registers a Grist API key for each person at sign-in through the Grist
    provisioner, a small service next to Grist.

    Lives in ``org_bundle.json``'s ``grist.auto_provision`` section and is written by
    ``scripts/build_org_bundle.py --grist-auto-provision``. The provisioning key itself is never in
    the bundle: the daemon reads it from the environment variable ``key_env`` names, as it does
    for ``http_bearer_token_env``. ``provisioner_url`` and ``server_url`` are normalised and set
    only when enabled. Local mode never provisions.
    """

    enabled: bool = False
    key_env: str = DEFAULT_GRIST_PROVISIONING_KEY_ENV
    trust_unverified_email: bool = False
    provisioner_url: str = ""
    server_url: str = ""

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "GristProvisioningConfig":
        grist = org_config.get("grist")
        grist = grist if isinstance(grist, dict) else {}
        raw = grist.get("auto_provision")
        if not isinstance(raw, dict):
            return GristProvisioningConfig()
        for key in ("enabled", "trust_unverified_email"):
            value = raw.get(key, False)
            if not isinstance(value, bool):
                raise ConfigurationError(
                    f"org_bundle.json's \"grist\".\"auto_provision\".\"{key}\" must be true or false, got {value!r}"
                )
        key_env = raw.get("key_env", DEFAULT_GRIST_PROVISIONING_KEY_ENV)
        if not isinstance(key_env, str) or not _ENV_NAME_RE.fullmatch(key_env):
            raise ConfigurationError(
                f"org_bundle.json's \"grist\".\"auto_provision\".\"key_env\" must be an environment "
                f"variable name, got {key_env!r}"
            )
        if not raw.get("enabled", False):
            return GristProvisioningConfig(enabled=False)
        if resolve_mode(org_config) != "org":
            raise ConfigurationError("org_bundle.json's \"grist\".\"auto_provision\" needs \"mode\": \"org\"")
        server_url = grist.get("server_url") or ""
        if not server_url:
            raise ConfigurationError(
                "org_bundle.json's \"grist\".\"auto_provision\" needs \"grist\".\"server_url\""
            )
        if grist.get("client_id"):
            raise ConfigurationError(
                "org_bundle.json's \"grist\".\"auto_provision\" works only with API keys; "
                "remove the Grist OAuth app or turn auto-provisioning off"
            )
        provisioner_url = raw.get("provisioner_url") or ""
        if not provisioner_url:
            raise ConfigurationError(
                "org_bundle.json's \"grist\".\"auto_provision\" needs \"provisioner_url\""
            )
        try:
            server_url = normalize_server_url(server_url)
        except GristClientError as exc:
            raise ConfigurationError(f"org_bundle.json's \"grist\".\"server_url\" is not usable: {exc}") from exc
        try:
            provisioner_url = normalize_server_url(provisioner_url)
        except GristClientError as exc:
            raise ConfigurationError(
                f"org_bundle.json's \"grist\".\"auto_provision\".\"provisioner_url\" is not usable: {exc}"
            ) from exc
        return GristProvisioningConfig(
            enabled=True, key_env=key_env, trust_unverified_email=raw.get("trust_unverified_email", False),
            provisioner_url=provisioner_url, server_url=server_url,
        )


# Which org_bundle.json section switches each connector on (ADR 0196). A connector with no
# non-empty section is not offered at all, whatever else is in place: Google's six share one
# section, Jira, Confluence and Jira Assets share Atlassian's. Telegram, Peek and Grist carry
# no secret an administrator must supply, so their section can be the bare marker
# ``{"enabled": true}``; Peek and Grist may add the server they are pinned to. Miro's section carries
# the app's ``client_id`` and ``client_secret``.
CONNECTOR_ORG_SECTION: dict[str, str] = {
    "gmail": "google", "drive": "google", "contacts": "google",
    "calendar": "google", "tasks": "google", "apps_script": "google",
    "slack": "slack",
    "jira": "atlassian", "confluence": "atlassian", "assets": "atlassian",
    "salesforce": "salesforce",
    "telegram": "telegram", "grist": "grist", "peek": "peek", "miro": "miro",
}


def connector_enabled_by_org(org_config: dict[str, Any], connector: str) -> bool:
    """True when ``org_config`` carries a non-empty section for ``connector``."""
    return bool(org_config.get(CONNECTOR_ORG_SECTION[connector]))


__all__ = [
    "AuditForwardingConfig",
    "AuditForwardingKind",
    "AuthzPolicyConfig",
    "CONNECTOR_ORG_SECTION",
    "ConfigurationError",
    "DEFAULT_AUDIT_FORWARDING_KIND",
    "DEFAULT_INLINE_MAX_BYTES",
    "DEFAULT_LINK_TTL_SECONDS",
    "DEFAULT_MODE",
    "DEFAULT_GRIST_PROVISIONING_KEY_ENV",
    "DEFAULT_PEEK_PROVISIONING_KEY_ENV",
    "DEFAULT_SYSLOG_PORT",
    "DEFAULT_SYSLOG_PROTOCOL",
    "DownloadDeliveryConfig",
    "Mode",
    "GristProvisioningConfig",
    "PeekProvisioningConfig",
    "connector_enabled_by_org",
    "ServerConfig",
    "SyslogProtocol",
    "WebPushConfig",
    "base64_length",
    "inline_result_length",
    "resolve_mode",
]
