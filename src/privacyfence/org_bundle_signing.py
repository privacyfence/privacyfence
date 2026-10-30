"""Ed25519 signing/verification for organization config bundles
(org_bundle.json), plus the plain-hash helper daemon_main.py's startup
logging uses. ADR 0016 records why integrity has these two layers.

Threat model this closes: org_bundle.json carries the org's IdP client
secret, the Google/Slack/Salesforce/Atlassian app registrations, and (in
org mode) this daemon's own OAuth 2.1 authorization-server trust
configuration (``idp.issuer``, ``server.issuer_url``). Before this,
anything that can write to this file -- a compromised backup, a
misconfigured file share the bundle is distributed over, brief physical/
admin access to the machine -- could silently redirect this daemon's
whole trust root, and a tampered-but-still-valid-JSON replacement was
indistinguishable from the real thing (refusing a malformed org-mode
config at startup only catches malformed/unreadable files, not a
well-formed-but-hostile one).

Design -- trust-on-first-use (TOFU), the same model SSH host keys use:
each organization generates an Ed25519 keypair once
(``scripts/build_org_bundle.py --generate-signing-key``) and signs every
bundle it builds with it (``--sign-key``). The *first* signed bundle an
install ever sees has its embedded public key trusted on that basis
alone and pinned to disk (``pinned_public_key_path()``); every bundle
after that -- installed via PrivacyFence Settings' "Install/Update
Organization Config..." (``settings_controller.install_org_config_
bytes``) or dropped onto disk by hand and picked up at the next daemon
start (``daemon_main.load_org_config``) -- must verify against that
pinned key or is rejected outright. That closes the obvious downgrade
this would otherwise still allow (an attacker replacing a signed bundle
with an unsigned or self-signed one) without requiring any out-of-band
key distribution beyond the bundle itself. A signed bundle may also carry
a ``serial``; the highest one an install has accepted is pinned beside the
key (``pinned_serial_path()``) and a bundle with a lower serial is refused
as a rollback. Deleting that one file is how an administrator deliberately
goes back to an older bundle.

An install that never adopts signing (no ``--sign-key`` ever used) never
pins a key and keeps working exactly as before -- signing is opt-in for
local mode. It's mandatory for ``mode: org`` (see ``daemon_main.
load_org_config``'s and ``settings_controller.install_org_config_bytes``'s
own enforcement of that), since org mode is the surface this phase is
hardening towards enterprise-production readiness, not every existing
local install.

This module intentionally takes each caller's own ``org_dir`` as a
parameter rather than importing ``paths.org_dir`` itself -- both
``daemon_main.py`` and ``settings_controller.py`` already import ``org_dir``
into their own module namespace and their tests monkeypatch it there
(see e.g. ``test_daemon_main.py``'s ``monkeypatch.setattr(daemon_main,
"org_dir", ...)``); importing a second, independent reference to
``paths.org_dir`` here would silently bypass that and point the pinned-
key file at a different (real) location during tests.

Ed25519 (``cryptography.hazmat.primitives.asymmetric.ed25519``) rather
than RSA: fixed-size 32-byte keys/64-byte signatures make embedding both
directly in the JSON bundle (base64) simple, and it's the same
"security-critical, spec-governed, don't hand-roll it" primitive class
``pyproject.toml``'s own dependency comments already invoke for JWT/
WebAuthn verification elsewhere in this codebase -- ``cryptography`` is
already a runtime dependency there, so this adds nothing new to what's
installed.

``scripts/build_org_bundle.py`` does NOT import this module (see its own
module docstring: it's meant to be runnable standalone, without a
PrivacyFence install) -- it re-implements the same canonicalization/
signing logic against ``cryptography`` directly. The two MUST stay
byte-for-byte identical or a bundle signed by the script will fail to
verify here; ``_canonical_payload_bytes`` below is deliberately simple
(sorted-key compact JSON) to make that easy to keep in sync.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from datetime import datetime, timezone

from .org_mode import ConfigurationError
from .paths import LEGACY_ORG_CONFIG_FILENAME, ORG_BUNDLE_FILENAME
from .secure_files import atomic_write_bytes, atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

# Bundle fields written by scripts/build_org_bundle.py --sign-key, read
# here. Both are standard-alphabet base64 of the raw fixed-size Ed25519
# bytes -- 32 for the public key, 64 for the signature.
SIGNATURE_FIELD = "signature"
SIGNING_PUBLIC_KEY_FIELD = "signing_public_key"
SERIAL_FIELD = "serial"

# Lives alongside org_bundle.json itself (in the caller's own org_dir),
# not inside org_bundle.json -- the whole point is that once a key is
# pinned, this file's trust doesn't come from anything the bundle itself
# claims.
PINNED_PUBKEY_FILENAME = "org_bundle_signing_pubkey.txt"
# The highest bundle serial this install has accepted, beside the key pin.
PINNED_SERIAL_FILENAME = "org_bundle_serial.txt"
# The 6.x names. 7.x copies them once (adopt_legacy_files) and never writes,
# renames or deletes them, so a downgrade finds its own pins unchanged.
LEGACY_PINNED_PUBKEY_FILENAME = "org_config_signing_pubkey.txt"
LEGACY_PINNED_SERIAL_FILENAME = "org_config_serial.txt"
ADOPTED_RECORD_FILENAME = "org_bundle_adopted.json"


def sha256_hex(raw: bytes) -> str:
    """A hash any admin can compare by eye/script against
    a known-good value, independent of whether the bundle is signed at
    all -- see daemon_main.py's startup logging."""
    return hashlib.sha256(raw).hexdigest()


def _canonical_payload_bytes(bundle: dict[str, Any]) -> bytes:
    """Deterministic bytes to sign/verify: every field except
    ``signature`` itself (which obviously can't be part of what it
    signs), sorted-key compact JSON so the exact same logical bundle
    always canonicalizes identically regardless of how it was
    constructed or re-serialized. ``signing_public_key`` IS included --
    an attacker can't pair their own key with their own signature and
    have it verify against the *pinned* key (verification below always
    checks the pinned key, never a key the bundle merely claims), and
    folding the embedded key into what's signed also means that key
    can't be tampered with independent of the signature at the one
    moment it IS trusted on its own say-so -- first-pin (see
    ``verify_and_maybe_pin``).
    """
    payload = {k: v for k, v in bundle.items() if k != SIGNATURE_FIELD}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_against(bundle: dict[str, Any], public_key_bytes: bytes) -> bool:
    signature_b64 = bundle.get(SIGNATURE_FIELD)
    if not isinstance(signature_b64, str) or not signature_b64:
        return False
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, _canonical_payload_bytes(bundle))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def adopted_record_path(org_dir: Path) -> Path:
    return org_dir / ADOPTED_RECORD_FILENAME


def load_adopted_record(org_dir: Path) -> dict[str, Any]:
    """The ``org_bundle_adopted.json`` record, or ``{}`` when it is absent
    or unreadable."""
    try:
        data = json.loads(adopted_record_path(org_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _copy_unchanged(source: Path, dest: Path) -> bytes:
    try:
        raw = source.read_bytes()
        atomic_write_bytes(dest, raw)
    except OSError as exc:
        raise ConfigurationError(f"Could not copy {source} to {dest} for PrivacyFence 7: {exc}") from exc
    return raw


def adopt_legacy_files(org_dir: Path) -> bool:
    """Copies the 6.x bundle and pin files to their 7.x names (ADR 0016's
    pins move too, so a newer serial accepted under 7.x never makes a
    downgraded 6.x refuse its own older ``org_config.json``).

    Returns True when the bundle was copied. The legacy files are never
    modified or deleted. A failed copy raises ``ConfigurationError``, so the
    daemon refuses to start rather than run with half a configuration.

    The bundle is copied only while ``org_bundle.json`` does not exist and the adopted record
    does not say ``retired``. Each
    pin is copied, independently of the bundle, when its new name is missing
    -- but only until ``org_bundle_adopted.json`` records ``pins_adopted``,
    so that deleting the new-name pin (the documented key rotation) never
    brings the old key back.
    """
    bundle = org_dir / ORG_BUNDLE_FILENAME
    legacy_bundle = org_dir / LEGACY_ORG_CONFIG_FILENAME
    record = load_adopted_record(org_dir)

    copied_pin = False
    if not record.get("pins_adopted"):
        for legacy_name, new_name in (
            (LEGACY_PINNED_PUBKEY_FILENAME, PINNED_PUBKEY_FILENAME),
            (LEGACY_PINNED_SERIAL_FILENAME, PINNED_SERIAL_FILENAME),
        ):
            legacy_pin, new_pin = org_dir / legacy_name, org_dir / new_name
            if legacy_pin.exists() and not new_pin.exists():
                _copy_unchanged(legacy_pin, new_pin)
                copied_pin = True

    adopted_bundle = False
    # A retired bundle stays retired: 6.x's untouched org_config.json is not copied back.
    if not bundle.exists() and legacy_bundle.exists() and not record.get("retired"):
        raw = _copy_unchanged(legacy_bundle, bundle)
        record.update({
            "from": LEGACY_ORG_CONFIG_FILENAME,
            "sha256": sha256_hex(raw),
            "at": datetime.now(timezone.utc).isoformat(),
        })
        adopted_bundle = True
        logger.warning(
            "Copied org/org_config.json to org/org_bundle.json for PrivacyFence 7. The original is "
            "kept unchanged so that you can go back to 6.x."
        )

    if (adopted_bundle or copied_pin) and not record.get("pins_adopted"):
        record.setdefault("from", "")
        record.setdefault("sha256", "")
        record.setdefault("at", datetime.now(timezone.utc).isoformat())
        record["pins_adopted"] = True
    if adopted_bundle or copied_pin:
        try:
            atomic_write_json(adopted_record_path(org_dir), record, indent=2)
        except OSError as exc:
            raise ConfigurationError(f"Could not write {adopted_record_path(org_dir)}: {exc}") from exc
    return adopted_bundle


def pinned_public_key_path(org_dir: Path) -> Path:
    return org_dir / PINNED_PUBKEY_FILENAME


def load_pinned_public_key(org_dir: Path) -> bytes | None:
    path = pinned_public_key_path(org_dir)
    if not path.exists():
        return None
    try:
        return base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)
    except (OSError, ValueError):
        return None


def _pin_public_key(org_dir: Path, raw_public_key: bytes) -> None:
    """Writes the pinned-key file, atomically and at 0600 from the moment
    it exists -- see secure_files.py's module docstring."""
    path = pinned_public_key_path(org_dir)
    atomic_write_text(path, base64.b64encode(raw_public_key).decode("ascii") + "\n")


def pinned_serial_path(org_dir: Path) -> Path:
    return org_dir / PINNED_SERIAL_FILENAME


def load_pinned_serial(org_dir: Path) -> int | None:
    """None when no serial has been pinned; raises ValueError when the
    file exists but does not hold a positive integer (fail closed)."""
    path = pinned_serial_path(org_dir)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not text.isascii() or not text.isdigit() or int(text) < 1:
        raise ValueError(f"{path} does not hold a positive integer")
    return int(text)


@dataclass(frozen=True)
class BundleTrust:
    ok: bool
    detail: str
    signed: bool = False
    newly_pinned: bool = False
    serial: int | None = None
    # Which sentence a refusal message ends with: "key" (delete the key
    # pin), "serial" (delete the serial pin) or "none".
    remedy: str = "key"


def _serial_refusal(bundle: dict[str, Any], org_dir: Path) -> BundleTrust | None:
    """The serial check, run once the signature has verified. None means
    the serial is acceptable."""
    serial = bundle.get(SERIAL_FIELD)
    if SERIAL_FIELD in bundle and (isinstance(serial, bool) or not isinstance(serial, int) or serial < 1):
        return BundleTrust(ok=False, remedy="none", detail="serial is not a positive integer")
    try:
        pinned = load_pinned_serial(org_dir)
    except ValueError:
        return BundleTrust(ok=False, remedy="serial", detail="the pinned serial file is unreadable")
    if pinned is None:
        return None
    if serial is None:
        return BundleTrust(
            ok=False,
            remedy="serial",
            detail=f"this bundle carries no serial, but this install has already accepted serial {pinned}",
        )
    if serial < pinned:
        return BundleTrust(
            ok=False,
            remedy="serial",
            detail=f"serial {serial} is older than serial {pinned}, the newest this install has accepted (rollback refused)",
        )
    return None


def record_accepted_serial(trust: BundleTrust, org_dir: Path) -> None:
    """Raises the serial pin to ``trust.serial`` after a caller has
    accepted the bundle. Never raises: a failed write is logged and the
    caller carries on."""
    if not trust.ok or trust.serial is None:
        return
    try:
        pinned = load_pinned_serial(org_dir)
        if pinned is None or pinned < trust.serial:
            atomic_write_text(pinned_serial_path(org_dir), f"{trust.serial}\n")
    except (OSError, ValueError) as exc:
        logger.warning("Could not record the accepted organization config serial: %s", exc)


def would_pin_new_key(bundle: dict[str, Any], org_dir: Path) -> bool:
    """A pure, disk-mutation-free duplicate of verify_and_maybe_pin's own
    "first signed bundle this install has ever seen" branch condition --
    True exactly when calling that function on the same arguments would
    pin a new signing key as a side effect.

    settings_controller.install_org_config_bytes's web caller (routes_settings.py's
    org_config_upload) wants to ask a human for an explicit confirmation
    before that TOFU pin happens, rather than letting it happen silently
    as a side effect of an upload verify_and_maybe_pin would otherwise
    perform unconditionally. daemon_main.load_org_config's own call
    (hand-editing config on disk, not over HTTP) keeps pinning
    unconditionally -- placing a file there already required the kind of
    access an HTTP request from an agent does not have.

    Deliberately a second copy of the condition rather than a shared
    helper verify_and_maybe_pin also calls: this module already accepts
    that kind of duplication for the sake of the two functions staying
    independently readable (see the module docstring's own note on
    scripts/build_org_bundle.py's canonicalization copy) -- keep this in
    exact lockstep with verify_and_maybe_pin's pinning branch below, or a
    caller ends up asking for confirmation a pin never follows, or
    skipping it for one that happens anyway.
    """
    if load_pinned_public_key(org_dir) is not None:
        return False
    has_signature_fields = bool(bundle.get(SIGNATURE_FIELD)) or bool(bundle.get(SIGNING_PUBLIC_KEY_FIELD))
    if not has_signature_fields:
        return False
    embedded_key_b64 = bundle.get(SIGNING_PUBLIC_KEY_FIELD)
    if not isinstance(embedded_key_b64, str) or not embedded_key_b64:
        return False
    try:
        embedded_key = base64.b64decode(embedded_key_b64, validate=True)
    except ValueError:
        return False
    return len(embedded_key) == 32 and _verify_against(bundle, embedded_key)


def verify_and_maybe_pin(bundle: dict[str, Any], org_dir: Path) -> BundleTrust:
    """The one trust decision every org_bundle.json load or install goes
    through. See module docstring for the TOFU model.

    Mutates disk state (pins a new key) only in the "first signed bundle
    this install has ever seen" case below -- every other path is a pure
    check. Callers (daemon_main.load_org_config, settings_controller.
    install_org_config_bytes) both call this on every load/install, so
    the very first signed bundle wins the pin regardless of which of the
    two documented install methods (web upload vs. hand-editing the
    file) it arrived through.
    """
    has_signature_fields = bool(bundle.get(SIGNATURE_FIELD)) or bool(bundle.get(SIGNING_PUBLIC_KEY_FIELD))
    pinned = load_pinned_public_key(org_dir)

    if pinned is not None:
        if _verify_against(bundle, pinned):
            refusal = _serial_refusal(bundle, org_dir)
            if refusal is not None:
                return refusal
            return BundleTrust(
                ok=True,
                detail="signature verified against the pinned signing key",
                signed=True,
                serial=bundle.get(SERIAL_FIELD),
            )
        if has_signature_fields:
            return BundleTrust(
                ok=False,
                detail="signature does not verify against the previously trusted (pinned) signing key",
            )
        return BundleTrust(
            ok=False,
            detail="a signing key is already trusted for this install, but this bundle is unsigned",
        )

    if not has_signature_fields:
        return BundleTrust(ok=True, detail="unsigned (no signing key trusted yet)", signed=False)

    embedded_key_b64 = bundle.get(SIGNING_PUBLIC_KEY_FIELD)
    if not isinstance(embedded_key_b64, str) or not embedded_key_b64:
        return BundleTrust(ok=False, detail="carries a signature but no signing_public_key")
    try:
        embedded_key = base64.b64decode(embedded_key_b64, validate=True)
    except ValueError:
        return BundleTrust(ok=False, detail="signing_public_key is not valid base64")
    if len(embedded_key) != 32 or not _verify_against(bundle, embedded_key):
        return BundleTrust(ok=False, detail="signature does not verify against its own embedded signing_public_key")

    refusal = _serial_refusal(bundle, org_dir)
    if refusal is not None:
        return refusal

    _pin_public_key(org_dir, embedded_key)
    return BundleTrust(
        ok=True,
        detail="first signed bundle seen for this install -- its signing key is now pinned (TOFU)",
        signed=True,
        newly_pinned=True,
        serial=bundle.get(SERIAL_FIELD),
    )


# ---------------------------------------------------------------------------- #
# Signing side -- used by tests exercising the verification path above
# against a real signature (scripts/build_org_bundle.py has its own,
# deliberately independent copy of this for actual bundle production --
# see module docstring).
# ---------------------------------------------------------------------------- #

def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def public_key_b64(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign_bundle(bundle: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Returns a copy of ``bundle`` with ``signing_public_key``/
    ``signature`` added (replacing any that were already present). Any
    change to the bundle after this invalidates the signature, by
    design."""
    signed = dict(bundle)
    signed.pop(SIGNATURE_FIELD, None)
    signed[SIGNING_PUBLIC_KEY_FIELD] = public_key_b64(private_key.public_key())
    signature = private_key.sign(_canonical_payload_bytes(signed))
    signed[SIGNATURE_FIELD] = base64.b64encode(signature).decode("ascii")
    return signed


__all__ = [
    "ADOPTED_RECORD_FILENAME",
    "LEGACY_PINNED_PUBKEY_FILENAME",
    "LEGACY_PINNED_SERIAL_FILENAME",
    "PINNED_PUBKEY_FILENAME",
    "PINNED_SERIAL_FILENAME",
    "SIGNATURE_FIELD",
    "SIGNING_PUBLIC_KEY_FIELD",
    "BundleTrust",
    "adopt_legacy_files",
    "adopted_record_path",
    "generate_keypair",
    "load_adopted_record",
    "load_pinned_public_key",
    "load_pinned_serial",
    "pinned_public_key_path",
    "pinned_serial_path",
    "public_key_b64",
    "record_accepted_serial",
    "sha256_hex",
    "sign_bundle",
    "verify_and_maybe_pin",
    "would_pin_new_key",
]
