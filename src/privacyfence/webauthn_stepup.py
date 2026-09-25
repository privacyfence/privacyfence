"""WebAuthn step-up: user-verified passkey proof (Face ID / Touch ID /
Windows Hello, a security key with a PIN or biometric, or a phone over the
hybrid flow) that a human -- not merely a possessed, stolen session cookie
-- is the one approving a gated *write*, in org mode and local mode alike
-- see step_up_config.py's own module docstring for why local mode gets
this at all, and why it means something there only on a privilege-
separated install (ADR 0003). The threat it closes: a borrowed or stolen
unlocked phone with a live session becomes a remote approval instrument for
live write actions, and the control that actually closes that is a
step-up check on the approval itself.

Built on the ``webauthn`` package (py_webauthn), not hand-rolled -- the same
reasoning ADR 0009 gives for the MCP SDK applies here (and to PyJWT):
parsing CBOR attestation objects and verifying COSE signatures is
security-critical, spec-governed work with a maintained implementation
already available; owning that by hand buys nothing.

Two ceremonies, both delegated straight to the library after this module
resolves *who* (``Principal``) and *what RP*
(``step_up_config.StepUpConfig.rp_id``/``rp_name``) the call is for:

- **Registration** (``begin_registration``/``finish_registration``) --
  enrolling a new passkey, from web/routes_security.py's own ``/security``
  page.
- **Assertion** (``begin_assertion``/``verify_assertion``) -- proving
  possession of an already-enrolled one, from web/routes_approvals.py's
  decide endpoint.

Five things this module exists to get right, not just the happy path:

- **User verification is checked, not just the signature -- against a
  cooperating authenticator.** ``require_user_verification=True`` on both
  verify calls, so a credential that reports having proved only *presence*
  (no biometric/PIN) is rejected outright rather than silently accepted as
  "good enough". What that check reads is the ``UV`` bit in the
  authenticator's own ``authData``, which is a claim the authenticator
  makes about itself: a real authenticator sets it only after a
  biometric or PIN, and a process that is not one sets it to 1 because
  nothing signs the *absence* of a human. Registration here uses ``none``
  attestation (below), so there is also no attestation statement tying the
  key to a genuine authenticator model to fall back on. Treat this flag as
  "this authenticator says a human was verified", not as proof that one
  was -- the same structural reason attachment is not constrained
  (below). The control that makes it *mean* something against a local
  adversary is not this flag but the enrollment gate (web/routes_security.py's ``register_options``): a
  key nothing attests to is only as good as the proof demanded before it
  got into the store in the first place.
- **Any authenticator attachment is accepted, because none could be
  enforced.** Registration sets no ``authenticator_attachment``, so a
  browser offers a built-in authenticator, a roaming security key, or a
  phone over the hybrid (QR code) flow alike. Asking for ``platform`` would
  only have been a request to a cooperating browser: WebAuthn's signed
  payload carries no attachment claim to re-verify server-side (the
  browser-reported ``authenticatorAttachment`` field on the credential is
  informational only), so it bought no assurance -- while making enrollment
  impossible on a machine with no built-in authenticator, which is most
  Linux desktops. User verification, above, is the property that matters
  and stays required. See ADR 0055. ``exclude_credentials`` (below, from
  ``list_credentials``) is client-side-enforced in the same way and worth
  naming as such: it stops a *browser* offering to re-enroll an
  authenticator this principal already has, and stops nothing else.
- **The RP ID must be a real registrable domain.** Local mode's own server
  is bound to ``localhost`` for exactly this reason (ADR 0010);
  ``StepUpConfig.rp_id`` here is org mode's own version of that constraint
  -- an IP address or a non-HTTPS origin fails the ceremony at the browser
  level, not here.
- **The challenge is bound to a specific decision, not just "a human
  tapped something."** The challenge is a server nonce bound to the
  approval_id and a hash of the decision payload, verified server-side.
  ``decision_fingerprint``/``StepUpChallengeStore`` below are
  that binding -- see their own docstrings.
- **Sign-count regression is logged, not silently ignored** -- see
  ``verify_assertion``'s own note on why it's a warning, not a hard
  failure, for this authenticator class.

Two more concerns, both still scoped to *this*
principal's own credential-store directory and, like everything else
here, free of any dependency on audit_log.py -- callers (web/
routes_security.py, daemon_main.py) record the actual audit entries,
this module only tracks the state an entry needs to be written from:

- **A one-time recovery code** (``generate_recovery_code``/
  ``consume_recovery_code``) -- with no IdP there is no remote reset in
  local mode, and privilege separation takes ``config/settings.yaml`` off
  the agent's uid, so losing the only enrolled
  authenticator (a new machine, a wiped TPM) needs a sanctioned way back
  in that isn't "edit the config file from a shell" -- the very door the
  agent this feature defends against would also use. Generated once,
  shown once, never recoverable again, stored only as a salted hash.

  *Where* it is shown once is mode-dependent. Org mode hands it to the
  browser in the same response that generates it
  (``generate_recovery_code``). Local mode
  does not: a packaged local-mode install mints the code
  (``mint_recovery_code``), has the companion put it in front of the human
  on their own desktop (web/control_channel.py's ``SHOW RECOVERY``), and
  only then makes it live (``store_recovery_code``) -- so a credential-
  store reset token is never a value a process that merely holds a
  ``pf_session`` can read out of an HTTP response body, and a code nobody
  was shown never becomes the one code on file. The companion can also
  issue a replacement later, which is the only way this is ever shown
  twice: a new one, with the old invalidated.
- **Requirement enable/disable tracking**
  (``observe_step_up_requirement``/``step_up_disabled_notice``) -- daemon
  startup compares the freshly loaded ``step_up.require_passkey`` against
  what was last seen, which catches a change made by a config file edit
  plus a restart. The one UI path (settings_controller.py's
  ``enable_step_up``, see step_up_config.py's own ``LiveStepUpConfig``
  docstring) only turns this *on*, and it calls this same function itself
  right away rather than waiting for the next startup, so an enable is
  observed -- and audited -- the moment it happens. Turning it back *off* is a
  config-file-plus-restart operation with no UI of its own, deliberately:
  that asymmetry is what keeps ``step_up_disabled_notice``'s "treat this
  install as compromised" banner trustworthy -- a disable this module ever
  observes did not come from a human clicking a button in their own
  browser.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import paths
from .principal import Principal
from .secure_files import atomic_write_json

logger = logging.getLogger(__name__)

CREDENTIALS_FILE_NAME = "webauthn_credentials.json"

# The binding window -- generous enough to cover a real biometric prompt
# (including the delay a password-manager hand-off such as 1Password adds)
# but short enough that a leaked/logged challenge is useless well before
# the pending approval itself would expire.
STEP_UP_CHALLENGE_TTL_SECONDS = 2 * 60
_REGISTRATION_CHALLENGE_TTL_SECONDS = 5 * 60


class WebAuthnError(Exception):
    """Raised by finish_registration()/verify_assertion() on any ceremony
    failure -- an unverifiable signature, a challenge/RP-ID/origin
    mismatch, user verification not satisfied, or an unknown credential.
    Callers (web/routes_security.py, web/routes_approvals.py) turn this
    into a plain 401/400, never a stack trace reaching the browser."""


@dataclass
class WebAuthnCredential:
    """One enrolled passkey. ``credential_id``/``public_key`` are stored
    base64url-encoded (JSON has no byte-string type); everything else is
    exactly what ``VerifiedRegistration`` hands back, kept for the
    lifetime of the credential rather than re-derived."""

    credential_id: str
    public_key: str
    sign_count: int
    device_type: str  # "single_device" | "multi_device" -- the BE flag
    backed_up: bool  # the BS flag -- a synced (not device-bound) passkey
    label: str = "Passkey"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "public_key": self.public_key,
            "sign_count": self.sign_count,
            "device_type": self.device_type,
            "backed_up": self.backed_up,
            "label": self.label,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "WebAuthnCredential":
        return WebAuthnCredential(
            credential_id=str(raw["credential_id"]),
            public_key=str(raw["public_key"]),
            sign_count=int(raw.get("sign_count", 0)),
            device_type=str(raw.get("device_type", "single_device")),
            backed_up=bool(raw.get("backed_up", False)),
            label=str(raw.get("label", "Passkey")),
            created_at=float(raw.get("created_at", 0.0)),
        )


# --------------------------------------------------------------------- #
# Credential storage -- one 0600 JSON file per principal, same posture as
# every OAuth token file in this codebase (see slack_client.
# save_token_record's own comment). Lives under paths.authority_dir(), not
# paths.user_dir() directly: the agent must not be able to write the
# credential store a passkey is checked against, and authority_dir() is
# the root privilege separation makes service-owned (ADR 0003).
# --------------------------------------------------------------------- #

def _credentials_path(principal: Principal) -> Path:
    return paths.authority_dir(principal) / CREDENTIALS_FILE_NAME


def list_credentials(principal: Principal) -> list[WebAuthnCredential]:
    path = _credentials_path(principal)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read WebAuthn credentials for %s -- treating as none enrolled", principal.id)
        return []
    return [WebAuthnCredential.from_dict(item) for item in raw.get("credentials", []) if isinstance(item, dict)]


def has_credentials(principal: Principal) -> bool:
    return bool(list_credentials(principal))


def _save_credentials(principal: Principal, creds: list[WebAuthnCredential]) -> None:
    path = _credentials_path(principal)
    atomic_write_json(path, {"credentials": [c.to_dict() for c in creds]})


def add_credential(principal: Principal, credential: WebAuthnCredential) -> None:
    creds = [c for c in list_credentials(principal) if c.credential_id != credential.credential_id]
    creds.append(credential)
    _save_credentials(principal, creds)


def remove_credential(principal: Principal, credential_id: str) -> bool:
    creds = list_credentials(principal)
    remaining = [c for c in creds if c.credential_id != credential_id]
    if len(remaining) == len(creds):
        return False
    _save_credentials(principal, remaining)
    return True


def _update_sign_count(principal: Principal, credential_id: str, new_count: int) -> None:
    creds = list_credentials(principal)
    for c in creds:
        if c.credential_id == credential_id:
            c.sign_count = new_count
    _save_credentials(principal, creds)


# --------------------------------------------------------------------- #
# Registration ceremony -- enrolling a new passkey.
# --------------------------------------------------------------------- #

@dataclass
class _PendingRegistration:
    challenge: bytes
    authorized: bool = False
    created_at: float = field(default_factory=time.time)


class RegistrationChallengeStore:
    """One in-flight enrollment ceremony per principal at a time -- same
    "a daemon restart invalidates it, start over" posture as web/
    routes_connect.py's own _TelegramAuthStore.

    ``authorized`` is how the
    two halves of a gated enrollment stay one ceremony: web/
    routes_security.py's ``register_options`` is where the gate actually
    runs -- a fresh assertion when this principal already has a credential,
    a companion confirmation when it has none -- and it records the verdict
    here, on the challenge it just issued, so ``register_verify`` can check
    that the ceremony it is being asked to complete is the one that passed
    rather than re-deciding (and re-prompting) for itself. ``pop`` returns
    the whole entry for that reason, the same shape ``StepUpChallengeStore.
    pop`` already returns for the decide-time ceremony.
    """

    def __init__(self, ttl: float = _REGISTRATION_CHALLENGE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingRegistration] = {}

    def put(self, principal_id: str, challenge: bytes, *, authorized: bool = False) -> None:
        with self._lock:
            self._pending[principal_id] = _PendingRegistration(challenge=challenge, authorized=authorized)

    def pop(self, principal_id: str) -> _PendingRegistration | None:
        with self._lock:
            entry = self._pending.pop(principal_id, None)
        if entry is None or (time.time() - entry.created_at) > self._ttl:
            return None
        return entry


def begin_registration(principal: Principal, *, rp_id: str, rp_name: str) -> tuple[str, bytes]:
    """Returns ``(options_json, challenge)`` -- ``options_json`` goes
    straight to the browser (``navigator.credentials.create()``);
    ``challenge`` is what the caller must hand to
    ``RegistrationChallengeStore.put()`` to verify against later."""
    existing = list_credentials(principal)
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_name=principal.email or principal.display_name or principal.id,
        user_id=principal.id.encode("utf-8"),
        user_display_name=principal.display_name or principal.email or principal.id,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing
        ],
    )
    return webauthn.options_to_json(options), options.challenge


def finish_registration(
    principal: Principal,
    credential_json: dict[str, Any],
    *,
    expected_challenge: bytes,
    rp_id: str,
    origin: str,
    label: str = "Passkey",
) -> WebAuthnCredential:
    try:
        verified = webauthn.verify_registration_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 -- the library's own exception hierarchy isn't public API to pin to
        raise WebAuthnError(f"Registration could not be verified: {exc}") from exc
    credential = WebAuthnCredential(
        credential_id=bytes_to_base64url(verified.credential_id),
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
        label=label or "Passkey",
    )
    add_credential(principal, credential)
    return credential


# --------------------------------------------------------------------- #
# Assertion ceremony -- proving possession of an already-enrolled passkey,
# for a specific gated decision.
# --------------------------------------------------------------------- #

def begin_assertion(principal: Principal, *, rp_id: str) -> tuple[str, bytes] | None:
    """``None`` when this principal has no enrolled credential -- the
    caller (web/routes_approvals.py) falls back to offering the IdP
    step-up/re-auth path instead (OIDC re-auth is the fallback for a user
    with no passkey enrolled)."""
    creds = list_credentials(principal)
    if not creds:
        return None
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in creds],
    )
    return webauthn.options_to_json(options), options.challenge


def verify_assertion(
    principal: Principal, credential_json: dict[str, Any], *, expected_challenge: bytes, rp_id: str, origin: str,
) -> None:
    """Raises WebAuthnError on any failure; returns (updating the stored
    sign count as a side effect) on success."""
    cred_id = credential_json.get("id") if isinstance(credential_json, dict) else None
    stored = next((c for c in list_credentials(principal) if c.credential_id == cred_id), None)
    if stored is None:
        raise WebAuthnError("Unknown WebAuthn credential")
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 -- see finish_registration's own note
        raise WebAuthnError(f"Step-up could not be verified: {exc}") from exc
    # Clone-detection: many platform authenticators always report a sign
    # count of 0 (informational only for those -- both sides being 0 is
    # normal, not suspicious), but a *nonzero* count that fails to advance
    # is the classic signal a credential's private key material was cloned
    # rather than used from the one real authenticator. Logged, not a hard
    # failure: the assertion signature itself already verified, and
    # treating this as fatal would lock a legitimate user out of their own
    # passkey on a spec-compliant authenticator that simply doesn't
    # increment (a real, common case, not hypothetical).
    if (stored.sign_count != 0 or verified.new_sign_count != 0) and verified.new_sign_count <= stored.sign_count:
        logger.warning(
            "WebAuthn sign count did not advance for principal %s, credential %s -- "
            "possible cloned credential", principal.id, stored.credential_id,
        )
    _update_sign_count(principal, stored.credential_id, verified.new_sign_count)


# --------------------------------------------------------------------- #
# Decision binding -- a server nonce bound to the approval_id and a hash of
# the decision payload, verified server-side. web/routes_approvals.py's decide endpoint is the one
# caller of both halves below.
# --------------------------------------------------------------------- #

def decision_fingerprint(*, approval_id: str, principal_id: str, result: str, choice: int | None) -> str:
    """A stand-in for "this exact decision" -- not a secret, just a
    collision-resistant tag over (approval, principal, decision) so a
    step-up ceremony started for one decision can't be replayed to
    authorize a *different* decision on the same approval (e.g. a
    WebAuthn assertion obtained while approving gets silently reused to
    authorize a deny, or a different ``choice`` index)."""
    payload = f"{approval_id}|{principal_id}|{result}|{'' if choice is None else choice}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def batch_decision_fingerprint(*, principal_id: str, items: list[tuple[str, str]]) -> str:
    """The approval binder's own binding: the
    role ``decision_fingerprint`` plays for one decision, over a whole
    submitted *set*. ``items`` is ``(approval_id, result)`` pairs -- sorted
    here before hashing, so resubmitting the identical set in a different
    order (a batch has no meaningful order) fingerprints identically, while
    adding, dropping, or flipping the result of any single item does not.
    Binds the whole submitted set, deny items included, not just the ones
    that actually needed step-up -- so an assertion obtained for {A, B}
    cannot be replayed to authorize {A, B, C}, nor to flip B's own result,
    even though only A needed a passkey at all."""
    canonical = "|".join(f"{approval_id}:{result}" for approval_id, result in sorted(items))
    payload = f"{principal_id}|{canonical}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _PendingStepUp:
    challenge: bytes
    fingerprint: str
    created_at: float = field(default_factory=time.time)


class StepUpChallengeStore:
    """Server-side binding for one in-flight decide-time WebAuthn ceremony.
    Single-use (``pop``, not a read) and short-lived; keyed by
    ``(principal_id, approval_id)`` since only one step-up ceremony is ever
    meaningfully in flight for a given approval at a time -- a second
    ``options`` request for the same approval simply overwrites the first
    rather than needing its own separate slot."""

    def __init__(self, ttl: float = STEP_UP_CHALLENGE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _PendingStepUp] = {}

    def put(self, principal_id: str, approval_id: str, *, challenge: bytes, fingerprint: str) -> None:
        with self._lock:
            self._sweep_expired_locked()
            self._pending[(principal_id, approval_id)] = _PendingStepUp(challenge=challenge, fingerprint=fingerprint)

    def pop(self, principal_id: str, approval_id: str) -> _PendingStepUp | None:
        with self._lock:
            entry = self._pending.pop((principal_id, approval_id), None)
        if entry is None or (time.time() - entry.created_at) > self._ttl:
            return None
        return entry

    def _sweep_expired_locked(self) -> None:
        """Evict entries past ``self._ttl`` -- called with ``self._lock``
        already held. ``pop()`` only ever discards the one key it was asked
        for, on read; the second half of the store's own key space
        (``batch:<batch_id>``) is chosen by the request body rather
        than bounded by ``max_pending`` the way ``approval_id`` is, so
        without a sweep here nothing ever evicts an entry nobody comes back
        to pop."""
        now = time.time()
        expired = [key for key, entry in self._pending.items() if (now - entry.created_at) > self._ttl]
        for key in expired:
            del self._pending[key]


def is_step_up_required(*, gate_kind: str, pii_detected: bool, scope: str) -> bool:
    """Step-up covers writes, or writes plus PII-flagged reads, or
    (``"writes_and_reads"``) every gated read too, for an install that
    wants them covered rather than trusting pii_detector.py to have flagged the
    ones worth confirming. ``scope`` is
    ``step_up_config.StepUpConfig.scope`` -- kept as a bare string parameter
    here (rather than importing ``step_up_config.StepUpScope``) so this
    module has no dependency on step_up_config.py at all; that type's
    string literals are the whole of it.

    A ``gate_kind`` that is neither (``""`` -- approvals.PendingApproval's
    own bare confirm dialog) needs no step-up under any scope, including the
    widest: a confirm is a second step *inside* a decision the caller's own
    card already gated, never a release of its own."""
    if gate_kind == "popup":
        return True
    if gate_kind != "review":
        return False
    if scope == "writes_and_reads":
        return True
    return scope == "writes_and_pii_reads" and pii_detected


# --------------------------------------------------------------------- #
# Recovery code -- one per principal, salted-hash storage
# alongside the credential file itself under authority_dir(), same 0600
# posture. Exactly one *unused* code exists for a principal at a time:
# generating a new one (web/routes_security.py's register_verify, whenever
# none is currently unused) overwrites any previous one outright, and
# consuming the current one marks it spent rather than deleting it, so the
# "audited when spent" record survives the code's own consumption --
# has_recovery_code() (an *unused* one exists) is what actually decides
# whether the next enrollment generates a fresh one.
# --------------------------------------------------------------------- #

RECOVERY_CODE_FILE_NAME = "webauthn_recovery_code.json"

# Four groups of 4 uppercase hex characters ("A1B2-C3D4-E5F6-1789") -- long
# enough (64 bits of entropy) to make guessing infeasible while still
# something a human can type back in by hand if they ever have to, unlike
# a raw token_urlsafe blob.
_RECOVERY_CODE_GROUPS = 4
_RECOVERY_CODE_GROUP_CHARS = 4


def _recovery_code_path(principal: Principal) -> Path:
    return paths.authority_dir(principal) / RECOVERY_CODE_FILE_NAME


def _load_recovery_code(principal: Principal) -> dict[str, Any]:
    path = _recovery_code_path(principal)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read recovery code state for %s -- treating as none set", principal.id)
        return {}
    return raw if isinstance(raw, dict) else {}


def has_recovery_code(principal: Principal) -> bool:
    """True iff an *unused* recovery code currently exists for this
    principal -- a spent one (``used_at`` set) doesn't count, so the next
    successful enrollment generates a fresh one."""
    raw = _load_recovery_code(principal)
    return bool(raw) and not raw.get("used_at")


def mint_recovery_code() -> str:
    """A fresh recovery code's plaintext, stored nowhere -- the half of
    ``generate_recovery_code`` below that has no side effect.

    Split out for plan item 1.3's delivery order: local mode hands the code
    to the companion to put on screen (web/control_channel.py's ``SHOW
    RECOVERY``), and a code that reached nobody must not become the one
    live code on file -- ``has_recovery_code`` would then be True forever
    for a value no human has, and no later enrollment would issue another.
    So the caller mints, delivers, and only then calls
    ``store_recovery_code``. Nothing but the ordering changes: the stored
    shape and the verification path are exactly what they were."""
    return "-".join(
        secrets.token_hex(_RECOVERY_CODE_GROUP_CHARS // 2).upper() for _ in range(_RECOVERY_CODE_GROUPS)
    )


def store_recovery_code(principal: Principal, code: str) -> None:
    """Makes ``code`` this principal's one live recovery code, keeping only
    its salted SHA-256 hash. Overwrites (invalidates) any code already on
    file, used or not -- there is only ever one live code per principal."""
    salt = secrets.token_bytes(16)
    digest = hashlib.sha256(salt + code.encode("utf-8")).hexdigest()
    atomic_write_json(_recovery_code_path(principal), {
        "salt": salt.hex(), "digest": digest, "created_at": time.time(), "used_at": None,
    })


def generate_recovery_code(principal: Principal) -> str:
    """Mint and store in one step, returning the plaintext once -- the
    caller must surface it to the human in this same response; it is never
    retrievable again. What org mode's enrollment still does, since it has
    no companion to hand a code to and the browser that reached ``/security``
    is IdP-authenticated (docs/security-and-compliance.md says so in the
    same terms). Local mode uses the two halves above instead."""
    raw_code = mint_recovery_code()
    store_recovery_code(principal, raw_code)
    return raw_code


def consume_recovery_code(principal: Principal, code: str) -> bool:
    """Verifies ``code`` (whitespace-insensitive, case-insensitive -- a
    human retyping it may not match the on-screen formatting exactly)
    against the stored salted hash. On success, marks the code used
    (single-use: the file is kept, not deleted, so a spent code still
    proves it once existed and was spent -- ``has_recovery_code`` is what
    treats it as gone) and returns True; any failure (no code on file,
    already used, mismatch) returns False without revealing which."""
    raw = _load_recovery_code(principal)
    if not raw or raw.get("used_at"):
        return False
    try:
        salt = bytes.fromhex(str(raw.get("salt", "")))
    except ValueError:
        return False
    expected = str(raw.get("digest", ""))
    candidate = hashlib.sha256(salt + code.strip().upper().encode("utf-8")).hexdigest()
    if not hmac.compare_digest(candidate, expected):
        return False
    raw["used_at"] = time.time()
    atomic_write_json(_recovery_code_path(principal), raw)
    return True


# --------------------------------------------------------------------- #
# Requirement enable/disable tracking -- see module
# docstring. One small state file per principal, alongside the credential
# and recovery-code files.
# --------------------------------------------------------------------- #

STEP_UP_STATE_FILE_NAME = "step_up_state.json"


@dataclass(frozen=True)
class StepUpRequirementChange:
    """Returned by ``observe_step_up_requirement`` when this startup's
    ``(enabled, require_passkey)`` pair differs from what was last
    observed for this principal -- the caller (daemon_main.py) turns this
    into the actual audit entry."""

    was_required: bool
    is_required: bool


def _step_up_state_path(principal: Principal) -> Path:
    return paths.authority_dir(principal) / STEP_UP_STATE_FILE_NAME


def _load_step_up_state(principal: Principal) -> dict[str, Any]:
    path = _step_up_state_path(principal)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read step-up requirement state for %s -- treating as unset", principal.id)
        return {}
    return raw if isinstance(raw, dict) else {}


def observe_step_up_requirement(
    principal: Principal, *, enabled: bool, require_passkey: bool,
) -> StepUpRequirementChange | None:
    """Called once per daemon startup with the just-loaded ``step_up.
    enabled``/``require_passkey`` pair. "Required" is ``enabled and
    require_passkey`` together, not either flag alone: with ``enabled``
    False the whole step-up check is skipped regardless of
    ``require_passkey`` (web/routes_approvals.py's decide(), web/
    routes_settings.py's ``_needs_step_up``), so that pairing is the only
    one that actually changes what gets enforced.

    Returns ``None`` on an ordinary startup where nothing changed since
    the last one (the common case) -- otherwise a ``StepUpRequirementChange``
    for the caller to audit-log. A transition into *not* required also
    latches a persistent notice (see ``step_up_disabled_notice``) that
    outlives this one startup; a transition back into required clears it.
    """
    state = _load_step_up_state(principal)
    was_required = bool(state.get("required", False))
    is_required = bool(enabled and require_passkey)
    changed = was_required != is_required
    state["required"] = is_required
    if is_required:
        state["disabled_notice"] = False
    elif changed:
        state["disabled_notice"] = True
    atomic_write_json(_step_up_state_path(principal), state)
    if not changed:
        return None
    return StepUpRequirementChange(was_required=was_required, is_required=is_required)


def step_up_disabled_notice(principal: Principal) -> str | None:
    """An already-safe-to-embed HTML fragment (no user input, nothing to
    escape) for web_shell.wrap()'s persistent banner, or ``None`` when no
    disable transition is currently latched -- see
    ``observe_step_up_requirement``'s own docstring for when that's set
    and cleared."""
    if not _load_step_up_state(principal).get("disabled_notice", False):
        return None
    return (
        "Passkey requirement was turned off. If you didn't do this, treat this install as "
        "compromised. To restore it, set <code>step_up.require_passkey: true</code> in "
        "<code>config/settings.yaml</code> and restart PrivacyFence."
    )


__all__ = [
    "STEP_UP_CHALLENGE_TTL_SECONDS",
    "RegistrationChallengeStore",
    "StepUpChallengeStore",
    "StepUpRequirementChange",
    "WebAuthnCredential",
    "WebAuthnError",
    "add_credential",
    "batch_decision_fingerprint",
    "begin_assertion",
    "begin_registration",
    "consume_recovery_code",
    "decision_fingerprint",
    "finish_registration",
    "generate_recovery_code",
    "has_credentials",
    "has_recovery_code",
    "mint_recovery_code",
    "is_step_up_required",
    "list_credentials",
    "observe_step_up_requirement",
    "remove_credential",
    "step_up_disabled_notice",
    "store_recovery_code",
    "verify_assertion",
]
