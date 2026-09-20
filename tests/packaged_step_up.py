"""Enrolling a passkey, and deciding with one, against a *real* packaged
daemon -- what the packaged-artifact smoke tests have to do since
``default_local_step_up()`` made ``step_up.enabled`` and
``step_up.require_passkey`` both default to on for the DMG/``.pkg``, the
Windows installer and the ``.deb``.

On those builds, and only those, a fresh install with nothing enrolled
refuses to release anything: web/routes_approvals.py's ``decide`` answers
``passkey_enrollment_required`` (403) to a sensitive confirm and
``step_up_required`` (428) to an approving result in scope, exactly as
``StepUpConfig.local_enrollment_banner()``'s own docstring says it will.
That is the shipped behaviour, so a smoke test proving the shipped artifact
works end to end has to walk the path a real user walks: enroll at
``/security`` first, then approve with an assertion. Turning the setting off
for the test would prove the artifact works in a configuration nobody ships.

Two things this needs that an in-process test does not:

- **An authenticator.** ``tests/software_authenticator.py``, unchanged --
  see its own docstring on why nothing PrivacyFence verifies distinguishes it
  from a platform authenticator, and why that is the threat model rather
  than a hole to plug here.
- **A companion.** A first enrollment is gated on the companion's own
  ``CONFIRM ENROLL`` dialog (web/routes_security.py's ``_enrollment_gate``),
  and the recovery code minted right after it is delivered through the same
  channel (``SHOW RECOVERY``). A headless CI runner has no companion, so the
  caller passes one in: ``companion`` is a context manager standing up a
  stand-in on the companion's own address for the duration of the ceremony.
  Its POSIX implementation is tests/control_channel_client.py's
  ``companion_stand_in_script()``, run under ``sudo`` by whichever packaged
  module is calling -- see that section for why standing in is not a bypass.

Nothing here imports ``privacyfence``: these are HTTP round trips against a
frozen binary, in the wire shapes ``PF_WEBAUTHN_JS`` itself posts.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import httpx

# step_up_config.py's DEFAULT_LOCAL_RP_ID -- local mode has no issuer_url to
# derive one from, and never writes an rp_id into a fresh settings.yaml, so
# this is what a packaged install actually runs with. Spelled out rather than
# imported, same reasoning as every other constant the packaged modules
# duplicate: the test should fail if the app's value moves, not move with it.
RP_ID = "localhost"

# A number, not a name: web/routes_approvals.py's ``decide`` answers 428 with
# assertion options for exactly one reason, and both gates it guards (a
# sensitive confirm, an approving result in scope) use the same shape.
_STEP_UP_STATUS = 428


def _challenge_bytes(options: dict[str, Any]) -> bytes:
    """The ``challenge`` out of a ``webauthn_options`` payload, in the
    ``bytes`` form the authenticator signs over -- the decode a browser does
    inside ``PF_WEBAUTHN_JS``'s own ``pfB64uToBuf``."""
    from webauthn.helpers import base64url_to_bytes

    return base64url_to_bytes(options["challenge"])


def new_authenticator():
    """Imported here rather than at module scope so importing this module
    costs nothing on a platform whose packaged test is going to skip."""
    from tests.software_authenticator import SoftwareAuthenticator

    return SoftwareAuthenticator()


async def enroll_passkey(
    web_client: httpx.AsyncClient,
    session_id: str,
    *,
    origin: str,
    companion: Callable[[], contextlib.AbstractContextManager[Any]],
    label: str = "Packaged smoke test passkey",
):
    """Enrolls one passkey through the two real ``/api/security/webauthn/
    register/*`` round trips and returns the authenticator holding it.

    ``companion`` is entered around the *first* round trip only: that is the
    one that raises the enrollment dialog, and the recovery-code delivery
    that follows the second happens while it is still up, so both reach a
    listener. A recovery code that reaches nobody is not fatal --
    ``register_verify`` reports ``recovery_shown_by_companion: false`` and
    carries on (see its own comment) -- but an install that silently never
    issued one is not the state a real first enrollment leaves behind, so
    this asserts it was delivered."""
    authenticator = new_authenticator()
    with companion():
        options_resp = await web_client.post(
            "/api/security/webauthn/register/options", json={"csrf": session_id},
        )
        assert options_resp.status_code == 200, options_resp.text
        options = options_resp.json()["options"]

        credential = authenticator.register(
            challenge=_challenge_bytes(options), rp_id=RP_ID, origin=origin,
        )
        verify_resp = await web_client.post(
            "/api/security/webauthn/register/verify",
            json={"csrf": session_id, "credential": credential, "label": label},
        )
        assert verify_resp.status_code == 200, verify_resp.text
        enrolled = verify_resp.json()
    assert enrolled["status"] == "ok", enrolled
    assert enrolled.get("recovery_shown_by_companion") is True, (
        f"the first enrollment issued no recovery code: {enrolled}"
    )
    return authenticator


async def decide_with_step_up(
    web_client: httpx.AsyncClient,
    session_id: str,
    approval_id: str,
    *,
    result: str,
    authenticator,
    origin: str,
    choice: Any = None,
) -> httpx.Response:
    """POSTs the real decide route and answers its step-up challenge if it
    raises one -- what a browser does between ``pfWebauthnGet()`` and its
    retry, and the only way an approving decision goes through on a packaged
    install now.

    The retry repeats ``result``/``choice`` byte for byte because the
    challenge is fingerprinted over them (``webauthn_stepup.
    decision_fingerprint``): a retry that decided anything else would be
    refused, which is the point of the fingerprint."""
    body: dict[str, Any] = {"result": result, "csrf": session_id}
    if choice is not None:
        body["choice"] = choice
    response = await web_client.post(f"/api/approvals/{approval_id}/decide", json=body)
    if response.status_code != _STEP_UP_STATUS:
        return response
    options = response.json()["webauthn_options"]
    body["webauthn_assertion"] = authenticator.assert_(
        challenge=_challenge_bytes(options), rp_id=RP_ID, origin=origin,
    )
    return await web_client.post(f"/api/approvals/{approval_id}/decide", json=body)
