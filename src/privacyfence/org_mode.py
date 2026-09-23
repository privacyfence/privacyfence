"""``mode: local`` vs. ``mode: org`` (P7, docs/https-connector-refactor-
plan.md §4's operating-modes table) -- the one setting the rest of the
table's rows follow from. Lives in ``org_config.json`` (§4: "org_config.json
| as today | as today, plus server/TLS/IdP config"), not settings.yaml: it's
an install-wide decision, not a per-user preference, and org_config.json is
already the file daemon_main.py reads before it knows anything about a
principal at all.

Absent entirely, ``mode`` resolves to ``"local"`` -- an existing install's
org_config.json (today only ever carrying Google/Slack/Salesforce/
Atlassian app registrations, per daemon_main.py's own module docstring)
keeps meaning exactly what it already means, with no migration.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

Mode = Literal["local", "org"]

DEFAULT_MODE: Mode = "local"
DEFAULT_BIND_HOST = "localhost"
DEFAULT_PORT = 8765


class ConfigurationError(ValueError):
    """Raised for organization configuration that is present but broken --
    unreadable, malformed JSON, a non-object top level, an invalid
    ``mode``, or (in org mode) missing/incomplete required sections --
    rather than genuinely absent (SEC-04). daemon_main.py's ``main()``
    never catches this specifically: it's a ``ValueError`` subclass, so it
    falls into the same "print and refuse to start" path every other
    startup configuration error already takes, deliberately -- there is no
    silent fallback to local mode for a *broken* config, only for a
    missing one (see load_org_config's own docstring for why that
    distinction matters)."""


def resolve_mode(org_config: dict[str, Any]) -> Mode:
    mode = org_config.get("mode", DEFAULT_MODE)
    if mode not in ("local", "org"):
        raise ConfigurationError(f"org_config.json's \"mode\" must be \"local\" or \"org\", got {mode!r}")
    return mode


@dataclass(frozen=True)
class ServerConfig:
    """§10.2's transport decision, made concrete per install. Local mode's
    defaults here (``bind_host="localhost"``, no TLS, no trusted proxies)
    are exactly D1's decision -- loopback plain HTTP -- so an install that
    never sets ``mode: org`` never has a reason to look at this class at
    all; ``daemon_main.py`` only calls ``from_org_config`` when
    ``resolve_mode`` says org.
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
    # §10.2: "X-Forwarded-For / X-Forwarded-Proto are honored only when an
    # explicit trusted_proxies list is configured, never by default."
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
        # Stripped before anything derives from it: web/server.py builds
        # the Host allowlist from urlsplit(issuer_url).hostname, which
        # keeps a stray trailing space *inside* the hostname
        # ("pf.acme.example.com "), while routes_mcp.py's pydantic
        # AnyHttpUrl normalizes the same string and accepts it. The daemon
        # then starts cleanly and rejects every single request with
        # "Invalid Host header" -- a message pointing at Host headers and
        # reverse proxies rather than at the one config value at fault.
        issuer_url = str(raw.get("issuer_url") or "").strip()
        if not issuer_url:
            raise ConfigurationError("org mode requires org_config.json's \"server\".\"issuer_url\"")
        # Fail here, naming the key, rather than several frames later
        # inside mount_org_oauth's AnyHttpUrl() -- and, for a scheme that
        # parses but carries no hostname, rather than not at all (the
        # Host allowlist silently gains nothing and every request 400s).
        parsed = urlsplit(issuer_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigurationError(
                "org_config.json's \"server\".\"issuer_url\" must be an absolute http(s) URL "
                f"with a hostname (e.g. https://pf.acme.example.com), got {issuer_url!r}"
            )
        return ServerConfig(
            bind_host=raw.get("bind_host", DEFAULT_BIND_HOST),
            port=int(raw.get("port", DEFAULT_PORT)),
            issuer_url=issuer_url,
            cert_file=tls.get("cert_file", ""),
            key_file=tls.get("key_file", ""),
            trusted_proxies=tuple(raw.get("trusted_proxies") or ()),
        )


# Deliberately *larger* than connectors/drive.py's/connectors/gmail.py's/
# connectors/confluence.py's own pre-approval prefetch caps (5MB),
# reflecting that in org mode, inline delivery is the primary transport
# for drive_download_file/gmail_download_attachment/confluence_download_
# attachment, not a small-file convenience. The real ceiling here is
# practical MCP Streamable HTTP response size and base64's ~33%
# inflation, not a privacy argument for staying small.
DEFAULT_INLINE_MAX_BYTES = 8_000_000

# 5 minutes -- see download_staging.DEFAULT_TTL_SECONDS's own docstring for
# why this is short: staging means an encrypted-but-real copy of the file
# sits on the server's disk for this long.
DEFAULT_LINK_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class DownloadDeliveryConfig:
    """How org mode delivers file bytes for ``drive_download_file``/
    ``gmail_download_attachment``/``confluence_download_attachment`` to a
    principal who has no shell on the daemon's own machine: inline in the
    MCP tool result by default, or staged behind a short-lived, single-
    claim link when the file is too large to return inline (see ADR 0017
    for the rationale and the inline/staging split). Lives in
    ``org_config.json``'s ``download_delivery`` section, org-mode-only for
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
    # Phase 4 (local-mode-fixes-plan.md, ADR 0007's "Clients without the
    # bridge" section): every staged-link download in org mode is reached
    # by an MCP client -- an agent, not a human with a browser -- so the
    # default is the capability link (/mcp-files/fetch/<token>, no bearer
    # header or session cookie needed: the token in the URL is the
    # credential). Setting this False keeps the older cookie-authenticated
    # browser link (/downloads/{token}) instead -- for an org that wants a
    # human to be the one who actually opens a staged download, not the
    # agent fetching it directly on their behalf.
    agent_links: bool = True

    def staged_link_path(self, token: bytes) -> str:
        """The URL path a staged download's ``download_url`` should use,
        given this config's own ``agent_links`` choice: the Phase 4
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

    def fits_inline(self, size_bytes: int) -> bool:
        """Whether a file this size should be delivered inline (base64, in
        the tool result) rather than staged behind a one-time link.
        ``inline_max_bytes == 0`` (the "force every download through a
        staged link, unconditionally" knob) always returns False here --
        even for an empty (0-byte) file -- rather than the arithmetically
        tempting but wrong ``0 <= 0``."""
        return self.inline_max_bytes > 0 and size_bytes <= self.inline_max_bytes

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "DownloadDeliveryConfig":
        raw = org_config.get("download_delivery")
        raw = raw if isinstance(raw, dict) else {}
        inline_max_bytes = int(raw.get("inline_max_bytes", DEFAULT_INLINE_MAX_BYTES))
        if inline_max_bytes < 0:
            raise ConfigurationError(
                "org_config.json's \"download_delivery\".\"inline_max_bytes\" must be >= 0 "
                f"(0 forces every download through a staged link), got {inline_max_bytes}"
            )
        link_ttl_seconds = float(raw.get("link_ttl_seconds", DEFAULT_LINK_TTL_SECONDS))
        if link_ttl_seconds <= 0:
            raise ConfigurationError(
                "org_config.json's \"download_delivery\".\"link_ttl_seconds\" must be > 0, "
                f"got {link_ttl_seconds}"
            )
        return DownloadDeliveryConfig(
            inline_max_bytes=inline_max_bytes,
            link_ttl_seconds=link_ttl_seconds,
            allow_disk_staging=bool(raw.get("allow_disk_staging", True)),
            agent_links=bool(raw.get("agent_links", True)),
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
    """SEC-23: org
    mode's centralized audit-log forwarding destination. Lives in
    ``org_config.json``'s ``audit_forwarding`` section, org-mode-only like
    ``ServerConfig``/``DownloadDeliveryConfig`` above -- local mode never
    looks at this at all (there is no "centralize" to speak of for a single
    employee's own machine); see daemon_main.py's ``run_app`` for the
    ``resolve_mode(org_config) == "org"`` gate.

    Forwarding is *additional* visibility, never a replacement for the
    local audit log ``audit_log.py``'s ``AuditLogger`` always writes --
    that JSONL file (with its own append-integrity hash chain, SEC-23's
    other half, always on regardless of this config) stays the
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
    # org_config.json, so a leaked or mis-shared bundle doesn't also leak
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
                f"org_config.json's \"audit_forwarding\".\"kind\" must be \"syslog\" or \"http\", "
                f"got {kind!r}"
            )
        syslog_raw = raw.get("syslog")
        syslog_raw = syslog_raw if isinstance(syslog_raw, dict) else {}
        http_raw = raw.get("http")
        http_raw = http_raw if isinstance(http_raw, dict) else {}

        syslog_protocol = syslog_raw.get("protocol", DEFAULT_SYSLOG_PROTOCOL)
        if syslog_protocol not in ("udp", "tcp"):
            raise ConfigurationError(
                f"org_config.json's \"audit_forwarding\".\"syslog\".\"protocol\" must be \"udp\" "
                f"or \"tcp\", got {syslog_protocol!r}"
            )
        http_url = str(http_raw.get("url", "") or "")
        if enabled and kind == "http" and http_url and not http_url.startswith("https://"):
            raise ConfigurationError(
                "org_config.json's \"audit_forwarding\".\"http\".\"url\" must use https:// -- "
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
    """SEC-22: an
    optional PrivacyFence-level allowlist layered *on top of* the IdP's own
    authentication, not a replacement for it -- the IdP has already decided
    who this human is by the time anything here runs (org_identity.py's
    ``check_authz_policy`` is only ever called after ``principal_from_
    claims`` has a real ``Principal`` in hand); this decides whether
    PrivacyFence itself is willing to admit them.

    Exists because docs/org-mode-setup-guide.md §4.1 flags this as a real
    gap: for a plain (non-Workspace) Google IdP, the OAuth consent screen's
    own test-user list or verification status is the *only* access control
    most org-mode deployments have -- an IdP-side setting this repo can't
    see or audit, let alone enforce consistently across a different IdP.

    Lives in ``org_config.json``'s ``authz`` section, org-mode-only like
    every other org_mode.py config class. Absent entirely (or an ``authz``
    section with neither list set) means ``enabled`` is ``False`` -- "no
    additional restriction, every IdP-authenticated principal is admitted"
    -- so an existing org-mode install with no ``authz`` section keeps
    working exactly as before this landed, the same additive/opt-in
    posture every other org-mode config in this module already has.
    """

    # Case-folded, leading-"@"-stripped at parse time (see from_org_config)
    # so "acme.com", "Acme.com" and "@acme.com" in org_config.json all mean
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
                "org_config.json's \"authz\".\"required_groups\" is set but \"groups_claim\" is "
                "empty -- PrivacyFence has no ID token claim to read group membership from"
            )
        return AuthzPolicyConfig(
            allowed_domains=allowed_domains, groups_claim=groups_claim, required_groups=required_groups,
        )


__all__ = [
    "AuditForwardingConfig",
    "AuditForwardingKind",
    "AuthzPolicyConfig",
    "ConfigurationError",
    "DEFAULT_AUDIT_FORWARDING_KIND",
    "DEFAULT_INLINE_MAX_BYTES",
    "DEFAULT_LINK_TTL_SECONDS",
    "DEFAULT_MODE",
    "DEFAULT_SYSLOG_PORT",
    "DEFAULT_SYSLOG_PROTOCOL",
    "DownloadDeliveryConfig",
    "Mode",
    "ServerConfig",
    "SyslogProtocol",
    "resolve_mode",
]
