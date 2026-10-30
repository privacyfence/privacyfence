"""The organization server's side of the PrivacyFence sign-in relay.

A service offered with a built-in app has one redirect URI registered with its provider, at
``oauth.privacyfence.eu``. The relay receives the provider's redirect and sends the browser on to
the organization server that started the sign-in. It will only do that for a server that proved
it controls its own host name: the server keeps a random secret in ``org/relay_token.txt``,
publishes its SHA-256 at ``/.well-known/privacyfence-relay``, and sends the secret to the relay,
which fetches the hash and compares (``register``). Only the holder of the secret can register the
host or change its registration; the published hash alone proves nothing.

This module keeps that registration alive. ``start_registration_loop`` runs a daemon thread that
registers when a service needs the relay and re-registers daily, backing off after a failure.
``status`` is what the connect routes read before they send a person to a provider, so a sign-in
the relay would refuse never starts.

The secret is sent only to the relay and is never served or logged; the hash is public by design.

With PrivacyFence's built-in Google sign-in, the relay also needs the server to prove each Google
Workspace domain it signs people in from: a DNS TXT record at ``_privacyfence-relay.<domain>``
whose value is bound to this server's host (``dns_record_value``). ``register`` sends those
domains, and the set the relay found verified is kept in ``org/relay_status.json``: sign-in admits
an account only from a domain in it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from . import oauth_apps, org_mode, paths
from .secure_files import atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

TOKEN_LENGTH = 43
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^[a-z]{2,63}$")
_PORT_RE = re.compile(r"^[1-9][0-9]{0,4}$")
_LOOPBACK_RE = re.compile(r"^(localhost|127\.0\.0\.1)(:([1-9][0-9]{0,4}))?$")
_NOT_PUBLIC_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")

# Error codes the relay's /register answers with; anything else is reported as "HTTP <status>".
_WORKER_CODES = frozenset({
    "invalid_request", "host_not_public", "revoked", "too_soon", "too_many_new_hosts", "proof_failed",
    "unsupported_media_type",
})

_UNREACHABLE = "could not reach oauth.privacyfence.eu"
_NEEDS_HTTPS = "the relay needs an https issuer_url with no path"
_NOT_PUBLIC = "host_not_public"

_REGISTER_TIMEOUT_SECONDS = 10
_RECHECK_SECONDS = 3600
_REREGISTER_AFTER_SECONDS = 86400
_FIRST_BACKOFF_SECONDS = 60
# The relay checks at most this many sign-in domains per server.
MAX_SIGNIN_DOMAINS = 20
DNS_RECORD_PREFIX = "_privacyfence-relay."
# The sign-in domain record format, as the relay computes it. A new format gets a new version tag.
DNS_RECORD_VERSION = "v1"
# Personal Google accounts: never a Workspace domain, and the relay refuses them as sign-in domains.
_FREE_MAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_REPLACED_NOTICE = (
    "org/relay_token.txt was missing or unreadable and has been replaced. The server registers again "
    "with the new secret. With the built-in Google sign-in, every _privacyfence-relay DNS TXT record "
    "must be updated; the lines below give the new values."
)


def relay_token_path() -> Path:
    return paths.org_dir() / "relay_token.txt"


def relay_status_path() -> Path:
    return paths.org_dir() / "relay_status.json"


@dataclass
class _State:
    last_ok_at: float | None = None
    last_error: str = ""
    needed: bool = False
    failures: int = 0
    loaded: bool = False
    replaced_logged: bool = False
    # The sign-in domains the relay found proven in DNS at the last successful registration.
    signin_domains_verified: tuple[str, ...] = ()
    # The sign-in domains the relay refused outright (a public suffix or a shared mail domain).
    signin_domains_refused: tuple[str, ...] = ()
    skipped_logged: bool = False


_lock = threading.Lock()
_token_lock = threading.Lock()
_state = _State()
_wake = threading.Event()


def reset_for_tests() -> None:
    global _state
    with _lock:
        _state = _State()
    _wake.clear()


# --------------------------------------------------------------------------- #
# Host rules -- the relay's Worker implements the same ones.
# --------------------------------------------------------------------------- #

def valid_relay_host(host: str) -> bool:
    if len(host) > 253:
        return False
    name = host
    if ":" in host:
        name, _, port = host.rpartition(":")
        if not _PORT_RE.match(port) or int(port) > 65535:
            return False
    labels = name.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        return False
    return bool(_TLD_RE.match(labels[-1]))


def public_host(host: str) -> bool:
    if not valid_relay_host(host):
        return False
    name = host.rpartition(":")[0] if ":" in host else host
    return name != "localhost" and not name.endswith(_NOT_PUBLIC_SUFFIXES)


def loopback_host(host: str) -> bool:
    match = _LOOPBACK_RE.match(host)
    return match is not None and (match.group(3) is None or int(match.group(3)) <= 65535)


def issuer_host(issuer_url: str) -> str:
    return urlsplit(issuer_url).netloc.lower()


# --------------------------------------------------------------------------- #
# The well-known token and the persisted status
# --------------------------------------------------------------------------- #

def _read_persisted() -> dict[str, Any]:
    try:
        value = json.loads(relay_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _persisted_last_ok(persisted: dict[str, Any]) -> float | None:
    value = persisted.get("last_ok_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _persisted_domains(persisted: dict[str, Any], key: str) -> tuple[str, ...]:
    value = persisted.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(sorted({domain for domain in value if isinstance(domain, str) and _signin_domain_ok(domain)}))


def _read_persisted_last_ok() -> float | None:
    return _persisted_last_ok(_read_persisted())


def relay_secret() -> str:
    """The secret behind this server's relay registration, created on first need and then kept."""
    with _token_lock:
        path = relay_token_path()
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = None
        if existing is not None and _TOKEN_RE.match(existing):
            return existing
        last_ok = _read_persisted_last_ok()
        if last_ok is not None:
            with _lock:
                first = not _state.replaced_logged
                _state.replaced_logged = True
                # Every DNS record is bound to the old secret's hash, so none of them proves a
                # domain any more: sign-in waits until the relay checks the new records.
                _load_persisted()
                _state.signin_domains_verified = ()
                refused = _state.signin_domains_refused
            _persist_last_ok(last_ok, (), refused)
            if first:
                logger.error(_REPLACED_NOTICE)
        token = secrets.token_urlsafe(32)
        atomic_write_text(path, token)
        return token


def well_known_proof() -> str:
    """What ``/.well-known/privacyfence-relay`` serves: the hex SHA-256 of the secret."""
    return hashlib.sha256(relay_secret().encode("ascii")).hexdigest()


def _load_persisted() -> None:
    # Called with _lock held.
    if _state.loaded:
        return
    _state.loaded = True
    persisted = _read_persisted()
    _state.last_ok_at = _persisted_last_ok(persisted)
    _state.signin_domains_verified = _persisted_domains(persisted, "signin_domains_verified")
    _state.signin_domains_refused = _persisted_domains(persisted, "signin_domains_refused")


def _persist_last_ok(
    when: float,
    signin_domains_verified: tuple[str, ...] | list[str] = (),
    signin_domains_refused: tuple[str, ...] | list[str] = (),
) -> None:
    try:
        atomic_write_json(
            relay_status_path(),
            {
                "last_ok_at": when,
                "signin_domains_verified": sorted(signin_domains_verified),
                "signin_domains_refused": sorted(signin_domains_refused),
            },
        )
    except OSError as exc:
        logger.warning("Could not save the relay registration status: %s", exc)


def status() -> dict[str, Any]:
    with _lock:
        _load_persisted()
        return {
            "last_ok_at": _state.last_ok_at,
            "last_error": _state.last_error,
            "needed": _state.needed,
            "signin_domains_verified": list(_state.signin_domains_verified),
            "signin_domains_refused": list(_state.signin_domains_refused),
        }


def verified_signin_domains() -> frozenset[str]:
    """The Google Workspace domains the relay last found proven in DNS for this server."""
    with _lock:
        _load_persisted()
        return frozenset(_state.signin_domains_verified)


def request_check() -> None:
    _wake.set()


# --------------------------------------------------------------------------- #
# Sign-in domains for the built-in Google sign-in
# --------------------------------------------------------------------------- #

def _signin_domain_ok(domain: str) -> bool:
    """A name worth sending as a sign-in domain: a public host name with no port that is not a
    free-mail domain. A malformed name would make the relay refuse the whole registration, which
    would stop the connectors too, so the server never sends one. A public suffix (``co.uk``,
    ``github.io``) passes here, since the server has no copy of the Public Suffix List; the relay
    refuses that one domain on its own and the registration still succeeds."""
    return ":" not in domain and public_host(domain) and domain not in _FREE_MAIL_DOMAINS


def _builtin_signin_section(org_config: dict[str, Any]) -> dict[str, Any] | None:
    """The ``idp`` section when this server signs people in with PrivacyFence's built-in Google
    client through the relay, else ``None``. Startup has already refused a broken one."""
    try:
        section = org_mode.idp_section(org_config)
    except org_mode.ConfigurationError:
        return None
    if isinstance(section, dict) and section.get("builtin") is True and section.get("relay") is True:
        return section
    return None


def signin_domains(org_config: dict[str, Any]) -> list[str] | None:
    """The domains this server signs people in from: those of the admins' addresses and the
    allowed domains, lower-cased, de-duplicated and sorted. ``None`` without the built-in sign-in,
    which proves no domain."""
    section = _builtin_signin_section(org_config)
    if section is None:
        return None
    candidates: set[str] = set()
    for email in section.get("admin_emails") or ():
        if isinstance(email, str) and email.isascii():
            local, sep, domain = email.strip().lower().partition("@")
            if local and sep and domain:
                candidates.add(domain)
    candidates.update(org_mode.AuthzPolicyConfig.from_org_config(org_config).allowed_domains)
    domains = sorted(domain for domain in candidates if _signin_domain_ok(domain))
    skipped = sorted(candidates - set(domains)) + domains[MAX_SIGNIN_DOMAINS:]
    if skipped:
        with _lock:
            first = not _state.skipped_logged
            _state.skipped_logged = True
        if first:
            logger.warning(
                "Built-in Google sign-in: nobody from %s can sign in; a sign-in domain must be a "
                "Google Workspace domain, at most %d of them.", ", ".join(skipped), MAX_SIGNIN_DOMAINS,
            )
    return domains[:MAX_SIGNIN_DOMAINS]


def dns_record_value(host: str) -> str:
    """The TXT value that lets ``host`` sign people in from a domain: ``pf-relay=v1:`` and the hex
    SHA-256 of the UTF-8 string ``v1|<host>|<well-known proof>``. ``host`` is the issuer URL's host,
    lower-case, with its port if it has one. Binding the host means the record vouches for this
    server only: another server cannot reuse it to claim the domain."""
    material = f"{DNS_RECORD_VERSION}|{host}|{well_known_proof()}"
    return f"pf-relay={DNS_RECORD_VERSION}:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _config_host(org_config: dict[str, Any]) -> str | None:
    try:
        issuer_url = org_mode.ServerConfig.from_org_config(org_config).issuer_url
    except (org_mode.ConfigurationError, ValueError):
        return None
    host = issuer_host(issuer_url)
    return None if loopback_host(host) or not public_host(host) else host


def missing_dns_records(org_config: dict[str, Any]) -> list[str]:
    """One line for each sign-in domain the relay has not found proven, saying what to publish,
    and one for each it refused as a sign-in domain."""
    domains = signin_domains(org_config)
    host = _config_host(org_config)
    if not domains or host is None:
        return []
    # The secret first: replacing a lost one also forgets the domains its old records proved.
    value = dns_record_value(host)
    verified = verified_signin_domains()
    with _lock:
        refused = set(_state.signin_domains_refused)
    lines = []
    for domain in domains:
        if domain in verified:
            continue
        if domain in refused:
            lines.append(
                f"Built-in Google sign-in: {domain} is a public suffix or a shared mail domain, which the "
                f"relay never accepts as a sign-in domain. Nobody from {domain} can sign in."
            )
        else:
            lines.append(
                f"Built-in Google sign-in: add the DNS TXT record {DNS_RECORD_PREFIX}{domain} with the value "
                f"{value}. Until then nobody from {domain} can sign in."
            )
    return lines


def log_missing_dns_records(org_config: dict[str, Any]) -> None:
    for line in missing_dns_records(org_config):
        logger.warning("%s", line)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def register(issuer_url: str, signin_domains: list[str] | None = None) -> tuple[bool, str]:
    ok, detail, _verified, _refused = _register(issuer_url, signin_domains)
    return ok, detail


def _answered(answer: Any, key: str, sent: set[str]) -> tuple[str, ...]:
    reported = answer.get(key) if isinstance(answer, dict) else None
    return tuple(sorted({
        domain for domain in (reported if isinstance(reported, list) else ())
        if isinstance(domain, str) and domain in sent
    }))


def _register(
    issuer_url: str, signin_domains: list[str] | None,
) -> tuple[bool, str, tuple[str, ...], tuple[str, ...]]:
    """``register``, plus the sign-in domains the relay answered as verified and as refused (only
    ones sent)."""
    parts = urlsplit(issuer_url)
    host = parts.netloc.lower()
    plain = parts.path in ("", "/") and not parts.query and not parts.fragment
    if not plain or not (parts.scheme == "https" or (parts.scheme == "http" and loopback_host(host))):
        return False, _NEEDS_HTTPS, (), ()
    if loopback_host(host) or not public_host(host):
        return False, _NOT_PUBLIC, (), ()
    body: dict[str, Any] = {"host": host, "secret": relay_secret()}
    if signin_domains is not None:
        body["signin_domains"] = list(signin_domains)
    try:
        response = requests.post(
            f"{oauth_apps.RELAY_BASE_URL}/register",
            json=body,
            timeout=_REGISTER_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        return False, _UNREACHABLE, (), ()
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if response.status_code == 200:
        sent = set(signin_domains or ())
        return (
            True, "",
            _answered(answer, "signin_domains_verified", sent),
            _answered(answer, "signin_domains_refused", sent),
        )
    code = answer.get("error") if isinstance(answer, dict) else None
    if isinstance(code, str) and code in _WORKER_CODES:
        return False, code, (), ()
    return False, f"HTTP {response.status_code}", (), ()


def _tick(issuer_url: str, org_config: dict[str, Any], now: float) -> float:
    """One check; returns the seconds to wait before the next.

    A failed attempt never clears ``last_ok_at``: the relay keeps accepting a server for 35 days
    after its last proof, so a busy relay (``too_soon``, ``too_many_new_hosts``) or a network
    error must not close sign-ins that still work.

    With the built-in Google sign-in the relay is needed from the first start, and a sign-in
    domain not yet proven is checked again at every pass, so a record the administrator has just
    published takes effect within the hour."""
    sections = oauth_apps.snapshot(org_config)
    domains = signin_domains(org_config)
    needed = not loopback_host(issuer_host(issuer_url)) and (
        domains is not None
        or any(isinstance(section, dict) and oauth_apps.uses_relay(section) for section in sections.values())
    )
    with _lock:
        _load_persisted()
        _state.needed = needed
        fresh = (
            _state.last_ok_at is not None
            and now - _state.last_ok_at < _REREGISTER_AFTER_SECONDS
            and not _state.last_error
            and set(domains or ()) - set(_state.signin_domains_refused) <= set(_state.signin_domains_verified)
        )
    if not needed or fresh:
        return _RECHECK_SECONDS
    ok, detail, verified, refused = _register(issuer_url, domains)
    with _lock:
        if ok:
            _state.last_ok_at = now
            _state.last_error = ""
            _state.failures = 0
            _state.signin_domains_verified = verified
            _state.signin_domains_refused = refused
            wait = _RECHECK_SECONDS
        else:
            _state.last_error = detail
            _state.failures += 1
            wait = min(_FIRST_BACKOFF_SECONDS * 2 ** (_state.failures - 1), _RECHECK_SECONDS)
    if ok:
        _persist_last_ok(now, verified, refused)
        logger.info("Registered with the PrivacyFence sign-in relay")
        log_missing_dns_records(org_config)
    else:
        logger.warning("Registration with the PrivacyFence sign-in relay failed: %s", detail)
    return float(wait)


def start_registration_loop(issuer_url: str, org_config: dict[str, Any], stop: threading.Event) -> threading.Thread:
    def run() -> None:
        while not stop.is_set():
            _wake.clear()
            try:
                wait = _tick(issuer_url, org_config, time.time())
            except Exception:  # noqa: BLE001 -- the loop must outlive one bad pass
                logger.exception("Relay registration check failed")
                wait = float(_FIRST_BACKOFF_SECONDS)
            _wake.wait(wait)

    thread = threading.Thread(target=run, name="relay-registration", daemon=True)
    thread.start()
    return thread
