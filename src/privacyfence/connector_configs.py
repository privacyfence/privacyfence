"""Signed connector configs: an organization's own OAuth app for one provider.

An organization server reads no bundle for its apps. Each provider's own app is a small signed
text file an administrator uploads, stored unchanged at ``org/connector_configs/<provider>.txt``
and verified again at every start. The format is text rather than JSON so that Python and a
browser produce exactly the same bytes to sign::

    privacyfence-connector-config v1
    provider: slack
    serial: 3
    client_id: 1234.5678
    client_secret: ...
    signed_at: 2026-11-02T10:00:00Z
    key: <base64 Ed25519 public key>
    signature: <base64 Ed25519 signature>

The signed message is every line before ``signature:``, each ending in a line feed. Fields come
in exactly one order; Salesforce adds an optional ``login_url`` and Grist adds
``auth_server_url`` and ``server_url`` before ``signed_at``.

Trust works like the bundle's (``org_bundle_signing``): the first key uploaded is pinned, but only
when the caller confirmed its fingerprint, and a different key is refused afterwards. Each
provider also keeps the highest serial ever accepted, in a file removal leaves alone, so an older
file can never be replayed.

An upgraded server's legacy bundle carried the same credentials. They are copied once, unsigned
(the bundle's own verified signature covered them), to ``<provider>.json`` beside the signed
files, and a later upload replaces them.

Nothing here puts a client secret into a message, a log line or a repr: messages name the
provider and the field, never a value.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import org_bundle_signing
from .org_mode import ConfigurationError
from .paths import org_dir
from .secure_files import atomic_write_bytes, atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

PROVIDERS = ("google", "slack", "salesforce", "atlassian", "miro", "grist")

HEADER = "privacyfence-connector-config v1"
MAX_FILE_BYTES = 8 * 1024
MAX_VALUE_CHARS = 512
MAX_SERIAL_DIGITS = 15

PINNED_KEY_FILENAME = "connector_config_signing_pubkey.txt"
SERIALS_FILENAME = "connector_config_serials.json"
STORE_DIRNAME = "connector_configs"

LEGACY_SOURCE = "legacy-bundle"
UPLOADED_SOURCE = "connector-config"

# Marks in org_bundle_adopted.json: the legacy credentials were imported, and the connector pin
# was seeded from the bundle's pinned key. Each happens once, so removing a config or rotating
# the key by deleting the pin file is never undone by the next start.
_IMPORT_DONE = "connector_import_done"
_PIN_SEEDED = "connector_pin_seeded"

LEGACY_BUNDLE_WARNING = (
    "org/org_bundle.json is read only for its server and identity-provider settings until "
    "PrivacyFence 8. Move them to privacyfence.env (privacyfence-app --print-env-from-bundle), "
    "then run privacyfence-app --retire-bundle."
)
NEW_BUNDLE_REFUSAL = (
    "Organization servers no longer take a new org_bundle.json; set privacyfence.env and upload "
    "connector configs on the Admin page."
)

# (field name, required), in file order, between ``serial`` and ``signed_at``.
_LAYOUT: dict[str, tuple[tuple[str, bool], ...]] = {
    "google": (("client_id", True), ("client_secret", True)),
    "slack": (("client_id", True), ("client_secret", True)),
    "salesforce": (("consumer_key", True), ("consumer_secret", True), ("login_url", False)),
    "atlassian": (("client_id", True), ("client_secret", True)),
    "miro": (("client_id", True), ("client_secret", True)),
    "grist": (
        ("client_id", True), ("client_secret", True), ("auth_server_url", True), ("server_url", True),
    ),
}
# Fields a legacy bundle section may carry beyond the signed layout.
_LEGACY_EXTRA = {"google": ("auth_uri", "token_uri"), "slack": ("user_scopes",)}

_SIGNED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SIGNED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ConnectorConfigError(ConfigurationError):
    """A connector config that is malformed, untrusted or stale. The message is safe to show."""


class ConfirmationRequired(ConnectorConfigError):
    """The first connector config for this server: its key is pinned only once the caller
    confirms ``fingerprint``."""

    def __init__(self, fingerprint: str) -> None:
        super().__init__(
            f"This is the first connector config for this server. Check that the signing key's "
            f"fingerprint is {fingerprint} before trusting it."
        )
        self.fingerprint = fingerprint


@dataclass(frozen=True)
class ParsedConfig:
    provider: str
    serial: int
    signed_at: str
    key: bytes
    signature: bytes
    # The credential and URL fields, in file order; holds the client secret, so never shown.
    fields: dict[str, str] = field(repr=False)
    message: bytes = field(repr=False)


@dataclass(frozen=True)
class Accepted:
    provider: str
    serial: int
    signed_at: str
    fingerprint: str
    newly_pinned: bool


@dataclass(frozen=True)
class LoadedConfig:
    """One provider's app as the server uses it. ``fields`` holds the client secret."""

    provider: str
    source: str
    serial: int | None
    signed_at: str
    fingerprint: str
    fields: dict[str, Any] = field(repr=False)

    @property
    def client_id(self) -> str:
        return str(self.fields["consumer_key" if self.provider == "salesforce" else "client_id"])

    def section(self) -> dict[str, Any]:
        """A copy of the fields, in the shape the connectors read from the org config."""
        return json.loads(json.dumps(self.fields))


def default_org_dir() -> Path:
    return org_dir()


def fingerprint(public_key: bytes) -> str:
    """``SHA256:`` and the unpadded base64 of the key's SHA-256, as OpenSSH shows a key."""
    digest = hashlib.sha256(public_key).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def store_dir(org: Path) -> Path:
    return org / STORE_DIRNAME


def stored_path(org: Path, provider: str) -> Path:
    return store_dir(org) / f"{provider}.txt"


def legacy_path(org: Path, provider: str) -> Path:
    return store_dir(org) / f"{provider}.json"


def pinned_key_path(org: Path) -> Path:
    return org / PINNED_KEY_FILENAME


def serials_path(org: Path) -> Path:
    return org / SERIALS_FILENAME


# ---------------------------------------------------------------------------- #
# parse and verify
# ---------------------------------------------------------------------------- #

def _b64_exact(value: str, size: int, name: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != size or base64.b64encode(raw).decode("ascii") != value:
        raise ConnectorConfigError(f"{name} is not the base64 of {size} bytes.")
    return raw


def parse(raw: bytes) -> ParsedConfig:
    """The file's fields, or ``ConnectorConfigError``. Checks the layout only; ``verify`` checks
    the signature."""
    if len(raw) > MAX_FILE_BYTES:
        raise ConnectorConfigError(f"A connector config is at most {MAX_FILE_BYTES // 1024} KB.")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ConnectorConfigError("A connector config holds printable ASCII only.") from None
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in text.replace("\n", "")):
        raise ConnectorConfigError("A connector config holds printable ASCII only, with no control character.")
    if not text.endswith("\n"):
        raise ConnectorConfigError("A connector config ends with a line feed after the signature line.")
    lines = text[:-1].split("\n")
    if lines[0] != HEADER:
        raise ConnectorConfigError(f"The first line must be {HEADER}.")
    if len(lines) < 2 or not lines[1].startswith("provider: ") or lines[1][len("provider: "):] not in PROVIDERS:
        raise ConnectorConfigError(f"Line 2 must be provider: and one of {', '.join(PROVIDERS)}.")
    provider = lines[1][len("provider: "):]

    layout = (("serial", True), *_LAYOUT[provider], ("signed_at", True), ("key", True), ("signature", True))
    values: dict[str, str] = {}
    index = 2
    for name, required in layout:
        prefix = f"{name}: "
        if index < len(lines) and lines[index].startswith(prefix):
            value = lines[index][len(prefix):]
            if not value or value != value.strip() or len(value) > MAX_VALUE_CHARS:
                raise ConnectorConfigError(
                    f"Line {index + 1} ({name}) must be 1 to {MAX_VALUE_CHARS} characters, "
                    "with no space at either end."
                )
            values[name] = value
            index += 1
        elif required:
            raise ConnectorConfigError(f"Line {index + 1} must be {name}: followed by its value.")
    if index != len(lines):
        raise ConnectorConfigError(f"Line {index + 1} is not expected here.")

    serial = values["serial"]
    if not serial.isdigit() or serial.startswith("0") or len(serial) > MAX_SERIAL_DIGITS:
        raise ConnectorConfigError("serial must be a positive whole number without leading zeros.")
    signed_at = values["signed_at"]
    try:
        if not _SIGNED_AT.fullmatch(signed_at):
            raise ValueError(signed_at)
        datetime.strptime(signed_at, _SIGNED_AT_FORMAT)
    except ValueError:
        raise ConnectorConfigError("signed_at must be a UTC time such as 2026-11-02T10:00:00Z.") from None
    key = _b64_exact(values["key"], 32, "key")
    signature = _b64_exact(values["signature"], 64, "signature")

    signed_lines = lines[:-1]
    return ParsedConfig(
        provider=provider,
        serial=int(serial),
        signed_at=signed_at,
        key=key,
        signature=signature,
        fields={name: values[name] for name, _ in _LAYOUT[provider] if name in values},
        message=("\n".join(signed_lines) + "\n").encode("ascii"),
    )


def verify(parsed: ParsedConfig, public_key: bytes) -> bool:
    """Whether ``parsed`` carries a valid Ed25519 signature by ``public_key`` over its message."""
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(parsed.signature, parsed.message)
    except (InvalidSignature, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------- #
# the pin and the serials
# ---------------------------------------------------------------------------- #

def load_pin(org: Path) -> bytes | None:
    """The pinned signing key, or None when none is pinned yet. A pin file that exists but cannot
    be read fails closed: treating it as absent would let the next upload pin any key."""
    path = pinned_key_path(org)
    if not path.exists():
        return None
    try:
        return _b64_exact(path.read_text(encoding="ascii").strip(), 32, "The pinned key")
    except (OSError, ValueError):
        raise ConnectorConfigError(
            f"The pinned connector config signing key at {path} cannot be read. Fix or delete it."
        ) from None


def _write_record(org: Path, record: dict[str, Any]) -> None:
    try:
        atomic_write_json(org_bundle_signing.adopted_record_path(org), record, indent=2)
    except OSError as exc:
        raise ConnectorConfigError(f"Could not write {org_bundle_signing.adopted_record_path(org)}: {exc}") from exc


def _seed_pin_from_bundle(org: Path) -> bytes | None:
    """On a server upgraded with a signed bundle, the bundle's pinned key becomes this pin, once,
    so the organization keeps its key. Deleting the pin file afterwards rotates the key."""
    record = org_bundle_signing.load_adopted_record(org)
    if not record.get("sha256") or record.get(_PIN_SEEDED):
        return None
    bundle_pin = org_bundle_signing.load_pinned_public_key(org)
    if bundle_pin is None or len(bundle_pin) != 32:
        return None
    atomic_write_text(pinned_key_path(org), base64.b64encode(bundle_pin).decode("ascii") + "\n")
    record[_PIN_SEEDED] = True
    _write_record(org, record)
    return bundle_pin


def _current_pin(org: Path) -> bytes | None:
    pin = load_pin(org)
    return pin if pin is not None else _seed_pin_from_bundle(org)


def load_serials(org: Path) -> dict[str, int]:
    """The highest serial accepted per provider. Fails closed on a file it cannot trust."""
    path = serials_path(org)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or any(
        key not in PROVIDERS or isinstance(value, bool) or not isinstance(value, int) or value < 1
        for key, value in data.items()
    ):
        raise ConnectorConfigError(f"The connector config serials at {path} cannot be read. Fix or delete it.")
    return data


# ---------------------------------------------------------------------------- #
# accept, load, remove
# ---------------------------------------------------------------------------- #

def accept(org: Path, raw: bytes, *, confirmed_fingerprint: str | None = None) -> Accepted:
    """Verify ``raw`` and store it unchanged, or raise ``ConnectorConfigError``.

    With no key pinned yet the file's own key is trusted on first use, but only when
    ``confirmed_fingerprint`` equals that key's fingerprint; otherwise ``ConfirmationRequired``
    carries the fingerprint to show. Nothing is written unless every check passes."""
    parsed = parse(raw)
    pin = _current_pin(org)
    signer = fingerprint(parsed.key)
    if pin is None:
        if not verify(parsed, parsed.key):
            raise ConnectorConfigError("The signature does not verify against the file's own key.")
    else:
        if parsed.key != pin:
            raise ConnectorConfigError(
                f"This file is signed with another key ({signer}) than the one this server trusts "
                f"({fingerprint(pin)}). To change the key, delete {pinned_key_path(org)} on the server."
            )
        if not verify(parsed, pin):
            raise ConnectorConfigError("The signature does not verify against the key this server trusts.")
    serials = load_serials(org)
    highest = serials.get(parsed.provider, 0)
    if parsed.serial <= highest:
        raise ConnectorConfigError(
            f"serial {parsed.serial} is not higher than {highest}, the highest accepted for {parsed.provider}."
        )
    if pin is None:
        if confirmed_fingerprint != signer:
            raise ConfirmationRequired(signer)
        atomic_write_text(pinned_key_path(org), base64.b64encode(parsed.key).decode("ascii") + "\n")
    # The serial goes first: a crash between the two writes burns a serial instead of leaving a
    # file whose serial could be replayed.
    atomic_write_json(serials_path(org), {**serials, parsed.provider: parsed.serial}, indent=2, sort_keys=True)
    atomic_write_bytes(stored_path(org, parsed.provider), raw)
    legacy_path(org, parsed.provider).unlink(missing_ok=True)
    return Accepted(parsed.provider, parsed.serial, parsed.signed_at, signer, newly_pinned=pin is None)


def _stored_error(provider: str, problem: str) -> ConnectorConfigError:
    return ConnectorConfigError(
        f"The stored {provider} connector config {problem} -- refusing to start. Upload it again "
        f"on Admin → Services, or delete org/{STORE_DIRNAME}/{provider}.txt."
    )


def _load_signed(org: Path, provider: str, pin: bytes | None, serials: dict[str, int]) -> LoadedConfig | None:
    """The verified config, or ``None`` when the file was signed under a key this server no longer
    trusts (no key is pinned, or another one is): that is what rotating the key leaves behind, so
    the service stays on its built-in app until a new signed file is uploaded."""
    path = stored_path(org, provider)
    try:
        parsed = parse(path.read_bytes())
    except OSError:
        raise _stored_error(provider, "cannot be read") from None
    except ConnectorConfigError as exc:
        raise _stored_error(provider, f"is malformed ({exc})") from None
    if parsed.provider != provider:
        raise _stored_error(provider, f"is a {parsed.provider} config")
    if pin is None or parsed.key != pin:
        logger.warning(
            "The stored %s connector config was signed under a key this server does not trust "
            "(none is pinned, or another one is), so it is not used; upload a new one on Admin → Services.",
            provider,
        )
        return None
    if not verify(parsed, pin):
        raise _stored_error(provider, "no longer verifies against the trusted signing key")
    if parsed.serial < serials.get(provider, 0):
        raise _stored_error(provider, "is older than the newest one accepted (rollback)")
    return LoadedConfig(
        provider=provider, source=UPLOADED_SOURCE, serial=parsed.serial, signed_at=parsed.signed_at,
        fingerprint=fingerprint(pin), fields=dict(parsed.fields),
    )


def _load_imported(org: Path, provider: str) -> LoadedConfig:
    required = [name for name, needed in _LAYOUT[provider] if needed]
    try:
        data = json.loads(legacy_path(org, provider).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    fields = data.get("fields") if isinstance(data, dict) else None
    if (
        not isinstance(fields, dict)
        or data.get("source") != LEGACY_SOURCE
        or any(not isinstance(fields.get(name), str) or not fields[name] for name in required)
    ):
        raise ConnectorConfigError(
            f"The imported {provider} connector config is unreadable -- refusing to start. Upload it "
            f"again on Admin → Services, or delete org/{STORE_DIRNAME}/{provider}.json."
        )
    return LoadedConfig(
        provider=provider, source=LEGACY_SOURCE, serial=None, signed_at="", fingerprint="", fields=fields,
    )


def load_with_stale(org: Path) -> tuple[dict[str, LoadedConfig], set[str]]:
    """``load_all`` and the providers whose stored signed file is not used because its key is not
    the pinned one (the state key rotation leaves until each file is uploaded again)."""
    found: dict[str, LoadedConfig] = {}
    stale: set[str] = set()
    signed = [provider for provider in PROVIDERS if stored_path(org, provider).exists()]
    pin = _current_pin(org) if signed else None
    serials = load_serials(org) if signed else {}
    for provider in PROVIDERS:
        if provider in signed:
            loaded = _load_signed(org, provider, pin, serials)
            if loaded is None:
                stale.add(provider)
            else:
                found[provider] = loaded
        elif legacy_path(org, provider).exists():
            found[provider] = _load_imported(org, provider)
    return found, stale


def load_all(org: Path) -> dict[str, LoadedConfig]:
    """Every stored connector config, verified again against the pin. A signed file that is
    malformed, tampered with under the pinned key or rolled back, or an unreadable imported one,
    raises ``ConnectorConfigError`` naming the provider. A signed file under a key that is not the
    pinned one is skipped (see ``load_with_stale``). A signed file wins over an imported one for the
    same provider."""
    return load_with_stale(org)[0]


def remove(org: Path, provider: str) -> bool:
    """Delete ``provider``'s stored config. The serials file is kept, so a removed config's
    serial can never be used again. Returns whether anything was stored."""
    if provider not in PROVIDERS:
        raise ConnectorConfigError(f"{provider!r} is not a provider with a connector config.")
    removed = False
    for path in (stored_path(org, provider), legacy_path(org, provider)):
        if path.exists():
            path.unlink()
            removed = True
    return removed


def import_from_legacy_bundle(org: Path, bundle: dict[str, Any], bundle_sha256: str) -> list[str]:
    """Copy each credential section of the adopted legacy bundle to ``<provider>.json``, once.

    ``bundle_sha256`` must be the adopted bundle's hash, as ``org_bundle_adopted.json`` records
    it; any other bundle imports nothing. A provider that already has a stored config keeps it.
    Returns the providers imported."""
    record = org_bundle_signing.load_adopted_record(org)
    if not bundle_sha256 or record.get("sha256") != bundle_sha256 or record.get(_IMPORT_DONE):
        return []
    imported: list[str] = []
    for provider in PROVIDERS:
        section = bundle.get(provider)
        if not isinstance(section, dict):
            continue
        names = [name for name, _ in _LAYOUT[provider]] + list(_LEGACY_EXTRA.get(provider, ()))
        fields = {name: section[name] for name in names if name in section}
        required = [name for name, needed in _LAYOUT[provider] if needed]
        if any(not isinstance(fields.get(name), str) or not fields[name] for name in required):
            continue
        if stored_path(org, provider).exists() or legacy_path(org, provider).exists():
            continue
        atomic_write_json(
            legacy_path(org, provider),
            {"source": LEGACY_SOURCE, "provider": provider, "sha256": bundle_sha256, "fields": fields},
            indent=2,
        )
        imported.append(provider)
    _seed_pin_from_bundle(org)
    record = org_bundle_signing.load_adopted_record(org)
    record[_IMPORT_DONE] = True
    _write_record(org, record)
    if imported:
        logger.info("Imported the legacy bundle's app credentials as connector configs: %s", ", ".join(imported))
    return imported


__all__ = [
    "LEGACY_BUNDLE_WARNING",
    "NEW_BUNDLE_REFUSAL",
    "PROVIDERS",
    "Accepted",
    "ConfirmationRequired",
    "ConnectorConfigError",
    "LoadedConfig",
    "ParsedConfig",
    "accept",
    "default_org_dir",
    "fingerprint",
    "import_from_legacy_bundle",
    "load_all",
    "load_pin",
    "load_with_stale",
    "load_serials",
    "parse",
    "remove",
    "verify",
]
