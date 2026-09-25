"""A WebAuthn authenticator in software, for the tests that have to be the
adversary the enrollment gate exists to stop.

What it establishes, and what every test using it is really asserting, is
that **nothing PrivacyFence verifies distinguishes this from a real platform
authenticator**:

- Registration uses ``fmt: "none"`` -- no attestation statement, so there is
  no signed claim about the authenticator's make or model to check. That is
  what ``webauthn_stepup.begin_registration()`` asks for, and what every
  browser passkey this product is meant to be used with also produces.
- ``authData``'s ``UV`` bit is set to 1 here by assignment. On a real platform
  authenticator that bit follows a biometric or a PIN; nothing signs its
  *absence*, so ``require_user_verification=True`` (which reads exactly this
  bit) is satisfied by a key that has never been near a human. See
  webauthn_stepup.py's own "five things" list, which says so.
- ``exclude_credentials`` is sent in the registration options and ignored
  here, because only a cooperating browser enforces it and this is not one.
  Registration asks for no particular attachment, and none could be checked.

So this class cannot be "fixed" and is not a bug in py_webauthn -- it is the
shape of the threat model, which is why the control that actually binds is a
gate on *enrolling*, not a stronger check at verification time.

Keyed on ES256 (COSE alg ``-7``), the algorithm
``generate_registration_options``' own defaults put first for a platform
authenticator. Nothing here imports from ``privacyfence``: the point is to
speak the wire format the library parses, not to reuse the code under test.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

# authData flag bits (WebAuthn L2 §6.1). UP/UV are the two this helper sets on
# every ceremony; AT is registration-only, since only an attestation carries
# attested credential data.
FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04
FLAG_BACKUP_ELIGIBLE = 0x08
FLAG_BACKED_UP = 0x10
FLAG_ATTESTED_CREDENTIAL_DATA = 0x40

# A synthetic authenticator has no real AAGUID, and with fmt "none" it is
# required to be all-zero anyway -- which is also what a real platform
# authenticator's "none" attestation reports, so this is not a tell.
_ZERO_AAGUID = b"\x00" * 16


class SoftwareAuthenticator:
    """One synthetic passkey: an ES256 key pair, a credential id, and the two
    ceremonies' worth of wire format built around them.

    ``user_verified`` is a constructor argument rather than a hardcoded True
    so a test can assert on *both* directions -- that a UV bit of 1 is
    accepted from something that never verified a user (the finding), and that
    a UV bit of 0 is still rejected (the check does do the one thing it can).
    """

    def __init__(self, *, user_verified: bool = True, backed_up: bool = False) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.user_verified = user_verified
        self.backed_up = backed_up
        self.sign_count = 0

    # -- identity ---------------------------------------------------------- #

    @property
    def credential_id_b64u(self) -> str:
        """The form both ``WebAuthnCredential.credential_id`` and a
        credential's own JSON ``id`` field use."""
        return bytes_to_base64url(self.credential_id)

    # -- building blocks --------------------------------------------------- #

    def _cose_public_key(self) -> bytes:
        numbers = self._key.public_key().public_numbers()
        return cbor2.dumps({
            1: 2,    # kty: EC2
            3: -7,   # alg: ES256
            -1: 1,   # crv: P-256
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        })

    def _flags(self, *, attested: bool) -> int:
        flags = FLAG_USER_PRESENT
        if self.user_verified:
            flags |= FLAG_USER_VERIFIED
        if self.backed_up:
            flags |= FLAG_BACKUP_ELIGIBLE | FLAG_BACKED_UP
        if attested:
            flags |= FLAG_ATTESTED_CREDENTIAL_DATA
        return flags

    def _auth_data(self, rp_id: str, *, attested: bool) -> bytes:
        auth_data = (
            hashlib.sha256(rp_id.encode("utf-8")).digest()
            + bytes([self._flags(attested=attested)])
            + struct.pack(">I", self.sign_count)
        )
        if attested:
            auth_data += (
                _ZERO_AAGUID
                + struct.pack(">H", len(self.credential_id))
                + self.credential_id
                + self._cose_public_key()
            )
        return auth_data

    @staticmethod
    def _client_data(ceremony: str, challenge: bytes, origin: str) -> bytes:
        # Field order and spelling as a browser writes them. The library
        # re-reads the challenge out of this JSON rather than trusting the
        # caller, so this is the half that actually has to match.
        return json.dumps({
            "type": ceremony,
            "challenge": bytes_to_base64url(challenge),
            "origin": origin,
            "crossOrigin": False,
        }, separators=(",", ":")).encode("utf-8")

    # -- ceremonies -------------------------------------------------------- #

    def register(self, *, challenge: bytes, rp_id: str, origin: str) -> dict:
        """What ``navigator.credentials.create()`` would hand back -- the
        exact JSON shape ``PF_WEBAUTHN_JS``'s ``pfWebauthnCreate`` posts to
        ``/api/security/webauthn/register/verify``."""
        client_data = self._client_data("webauthn.create", challenge, origin)
        attestation_object = cbor2.dumps({
            "fmt": "none",
            "attStmt": {},
            "authData": self._auth_data(rp_id, attested=True),
        })
        return {
            "id": self.credential_id_b64u,
            "rawId": self.credential_id_b64u,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation_object),
            },
        }

    def assert_(self, *, challenge: bytes, rp_id: str, origin: str, user_handle: str | None = None) -> dict:
        """What ``navigator.credentials.get()`` would hand back --
        ``pfWebauthnGet``'s own shape. The signature is over
        ``authData || SHA-256(clientDataJSON)``, which is the one part of any
        of this that is real cryptography rather than a self-report.

        Increments ``sign_count`` first, so a second assertion from the same
        instance never looks like the replay ``verify_assertion()`` warns
        about.
        """
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", challenge, origin)
        auth_data = self._auth_data(rp_id, attested=False)
        signature = self._key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": self.credential_id_b64u,
            "rawId": self.credential_id_b64u,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(user_handle.encode("utf-8")) if user_handle else None,
            },
        }
