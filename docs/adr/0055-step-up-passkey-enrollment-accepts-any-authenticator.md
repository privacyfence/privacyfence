# ADR 0055: Step-up passkey enrollment accepts any authenticator

## Status

Accepted — 2026-09-25. Implemented in `src/privacyfence/webauthn_stepup.py`'s
`begin_registration`.

## Context

Passkey registration asked the browser for a built-in (platform) authenticator only:
`authenticator_attachment=PLATFORM` in `begin_registration`'s `AuthenticatorSelectionCriteria`.
A compliant browser then offers Touch ID, Windows Hello or an Android fingerprint, and never a
USB/NFC security key or a phone over the hybrid (QR code) flow.

Most Linux desktop browsers have no platform authenticator, and whether Chrome or Firefox offer
one depends on version and setup. Passkey step-up is on by default on packaged installs, the
`.deb` included (`require_passkey: true`, scope `writes_and_pii_reads`), and every approving
decision is refused until a passkey is enrolled. A Linux user could therefore be left with no way
to enroll the passkey that approving a write asks for
([#728](https://github.com/privacyfence/privacyfence/issues/728)).

The attachment preference bought little in exchange. As `webauthn_stepup.py`'s module docstring
already said, platform attachment is only *requested*: WebAuthn's signed payload carries no
attachment claim, the browser-reported `authenticatorAttachment` field is informational, and the
server cannot verify after the fact how a credential is attached. A process that is not a
cooperating browser ignores the request, which is exactly the adversary the enrollment gate
(`web/routes_security.py`'s `register_options`) exists for.

## Decision

Step-up passkey enrollment accepts any authenticator, on every platform: `begin_registration`
sets no `authenticator_attachment`, so the browser may offer a platform authenticator, a roaming
security key, or a phone over the hybrid flow. The rest of the registration options are
unchanged: `user_verification` stays `REQUIRED` (and both verify calls keep
`require_user_verification=True`), `resident_key` stays `PREFERRED`, attestation stays `none`,
and the enrollment gate is untouched.

User verification (a PIN, biometric or device unlock on the authenticator) is the property that
separates a human from a possessed session, and it is required whatever the attachment.

## Alternatives considered

- **Relax attachment on Linux only, keep `PLATFORM` on macOS and Windows.** Platform-conditional
  trust-boundary behavior, for no assurance gain on the other two: the requirement cannot be
  verified there either. It would also lock out a macOS or Windows user whose only authenticator is
  a security key, and add a per-platform branch to test and document.
- **Keep `PLATFORM` and document a Linux fallback.** No working fallback exists without a code
  change: a browser honoring the request offers nothing on a machine with no platform
  authenticator, and the only other way through, turning `require_passkey` off, removes step-up.

## Consequences

- A Linux desktop user can enroll with a security key or a phone, so the packaged `.deb`'s default
  step-up is usable there.
- A roaming authenticator can now be enrolled on purpose. A security key or phone can be carried
  away from the machine; that is the same possession-plus-verification model as any passkey, and
  a stolen key still needs its PIN or biometric.
- Nothing a server-side check relied on changes: the attachment was never verified, so no
  guarantee is lost that was actually held.
- The `/security` page's `NotAllowedError` explanation no longer assumes a built-in authenticator
  is the only option; with none available it suggests a security key or a phone.

## Verification

- `tests/unit/test_webauthn_stepup.py`'s
  `test_any_attachment_is_offered_but_user_verification_is_required`: the registration options
  carry no `authenticatorAttachment`, `userVerification` is `required` and `residentKey` is
  `preferred`.
- `docs/release-testing.md`, Debian/Ubuntu human check "Passkey enrollment": enrollment on the
  `.deb` in Chrome and Firefox, with a security key and with a phone.

## Related

- [#728](https://github.com/privacyfence/privacyfence/issues/728) — the issue and the options it
  weighed.
- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md) — what step-up gates.
- [ADR 0018](0018-linux-ships-a-self-contained-deb-built-with-pyinstaller.md) — the `.deb`.
