"""The Ed25519 claim key set and its rotation (plan 3.4)."""
from __future__ import annotations

import json
import os
import stat
import sys
import threading

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from privacyfence import upstream_claim_keys as ucw
from privacyfence.upstream_claim_keys import JWKS_CACHE_SECONDS, KeySet, KeySetError

OVERLAP = 3600


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def path(tmp_path):
    return tmp_path / ucw.FILENAME


@pytest.fixture
def keys(path, clock) -> KeySet:
    made = KeySet(path, overlap_seconds=OVERLAP, clock=clock)
    made.ensure()
    return made


def _stored(path) -> list[dict]:
    return json.loads(path.read_text())["keys"]


def _public(entry: dict) -> Ed25519PublicKey:
    import base64

    raw = base64.urlsafe_b64decode(entry["x"] + "=" * (-len(entry["x"]) % 4))
    return Ed25519PublicKey.from_public_bytes(raw)


def _verify(token: str, entry: dict) -> dict:
    return jwt.decode(token, _public(entry), algorithms=["EdDSA"], options={"verify_exp": False})


def test_first_start_creates_one_key_in_a_0600_file(keys, path):
    stored = _stored(path)
    assert len(stored) == 1
    assert stored[0]["signs_from"] == stored[0]["created_at"]
    assert stored[0]["retired_at"] is None
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_jwks_has_one_ed25519_entry_that_verifies_a_signed_token(keys):
    jwks = keys.jwks()
    assert len(jwks["keys"]) == 1
    entry = jwks["keys"][0]
    assert {k: entry[k] for k in ("kty", "crv", "use", "alg")} == {
        "kty": "OKP", "crv": "Ed25519", "use": "sig", "alg": "EdDSA",
    }
    assert len(entry["kid"]) == 16
    token = keys.sign({"sub": "alice"})
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "EdDSA", "kid": entry["kid"], "typ": "pf-identity-claim+jwt"}
    assert _verify(token, entry) == {"sub": "alice"}


def test_ensure_twice_keeps_the_same_key(keys, path):
    before = _stored(path)
    keys.ensure()
    assert _stored(path) == before


def test_rotate_publishes_the_new_key_at_once_but_signs_with_the_old_one_until_signs_from(keys, clock):
    old = keys.signing_key()
    new = keys.rotate()
    assert new.signs_from == clock.now + JWKS_CACHE_SECONDS
    assert {k["kid"] for k in keys.jwks()["keys"]} == {old.kid, new.kid}
    assert keys.signing_key().kid == old.kid
    clock.now += JWKS_CACHE_SECONDS - 1
    assert jwt.get_unverified_header(keys.sign({}))["kid"] == old.kid
    clock.now += 1
    assert keys.signing_key().kid == new.kid
    assert jwt.get_unverified_header(keys.sign({}))["kid"] == new.kid


def test_overlap_keeps_both_kids_in_the_jwks_then_only_the_new_one(keys, clock):
    old = keys.signing_key()
    new = keys.rotate()
    retired_at = new.signs_from
    clock.now = retired_at + OVERLAP - 1
    assert {k["kid"] for k in keys.jwks()["keys"]} == {old.kid, new.kid}
    clock.now = retired_at + OVERLAP
    assert [k["kid"] for k in keys.jwks()["keys"]] == [new.kid]


def test_a_retired_keys_seed_survives_until_retired_at_and_is_dropped_after(keys, clock, path):
    old = keys.signing_key()
    new = keys.rotate()
    by_kid = {k["kid"]: k for k in _stored(path)}
    assert by_kid[old.kid]["seed"] is not None
    assert by_kid[old.kid]["retired_at"] == new.signs_from
    clock.now = new.signs_from - 1
    keys.prune()
    assert {k["kid"]: k for k in _stored(path)}[old.kid]["seed"] is not None
    clock.now = new.signs_from
    keys.prune()
    by_kid = {k["kid"]: k for k in _stored(path)}
    assert by_kid[old.kid]["seed"] is None
    assert by_kid[new.kid]["seed"] is not None
    # Dropped for good: the retired key is still published but cannot sign.
    assert keys.signing_key().kid == new.kid


def test_a_key_past_its_overlap_is_pruned_from_the_file(keys, clock, path):
    old = keys.signing_key()
    new = keys.rotate()
    clock.now = new.signs_from + OVERLAP
    keys.prune()
    assert [k["kid"] for k in _stored(path)] == [new.kid]
    assert old.kid not in {k["kid"] for k in _stored(path)}


def test_loading_a_file_with_an_expired_seed_prunes_it(path, clock):
    first = KeySet(path, overlap_seconds=OVERLAP, clock=clock)
    first.ensure()
    old = first.signing_key()
    new = first.rotate()
    clock.now = new.signs_from + 1
    fresh = KeySet(path, overlap_seconds=OVERLAP, clock=clock)
    assert fresh.signing_key().kid == new.kid
    assert {k["kid"]: k for k in _stored(path)}[old.kid]["seed"] is None


def test_revoke_removes_a_key_from_the_jwks_at_once(keys):
    old = keys.signing_key()
    new = keys.rotate()
    assert keys.revoke(new.kid) is True
    assert [k["kid"] for k in keys.jwks()["keys"]] == [old.kid]
    assert keys.revoke("nope") is False


def test_revoking_the_pending_key_lets_the_signing_key_carry_on(keys, clock):
    old = keys.signing_key()
    new = keys.rotate()
    keys.revoke(new.kid)
    clock.now = new.signs_from + 10
    assert keys.signing_key().kid == old.kid


def test_revoke_refuses_the_only_key_that_can_sign(keys, clock):
    only = keys.signing_key()
    with pytest.raises(KeySetError):
        keys.revoke(only.kid)
    # A key that has not started signing yet does not count as one that can.
    keys.rotate()
    with pytest.raises(KeySetError):
        keys.revoke(only.kid)
    assert keys.signing_key().kid == only.kid


def test_revoking_the_old_key_after_the_new_one_signs_works(keys, clock):
    old = keys.signing_key()
    new = keys.rotate()
    clock.now = new.signs_from
    assert keys.revoke(old.kid) is True
    assert keys.signing_key().kid == new.kid


def test_a_malformed_file_after_the_first_load_keeps_the_last_good_set(keys, path, caplog):
    kid = keys.signing_key().kid
    path.write_text("{ not json")
    with caplog.at_level("WARNING"):
        assert keys.signing_key().kid == kid
    assert "keeping the last good key set" in caplog.text
    assert [k["kid"] for k in keys.jwks()["keys"]] == [kid]


def test_a_malformed_file_on_first_load_raises(path, clock):
    path.write_text("{ not json")
    with pytest.raises(KeySetError):
        KeySet(path, clock=clock).signing_key()


def test_a_missing_file_on_first_load_raises(path, clock):
    with pytest.raises(KeySetError):
        KeySet(path, clock=clock).signing_key()


def test_a_second_keyset_on_the_same_file_sees_a_rotation(keys, path, clock):
    other = KeySet(path, overlap_seconds=OVERLAP, clock=clock)
    old = other.signing_key()
    new = keys.rotate()
    assert {k["kid"] for k in other.jwks()["keys"]} == {old.kid, new.kid}
    clock.now = new.signs_from
    assert other.signing_key().kid == new.kid


def test_two_threads_rotating_leave_both_new_keys_in_the_file(keys, path, clock):
    other = KeySet(path, overlap_seconds=OVERLAP, clock=clock)
    other.signing_key()
    created: list = []
    barrier = threading.Barrier(2)

    def _rotate(target: KeySet) -> None:
        barrier.wait()
        created.append(target.rotate())

    threads = [threading.Thread(target=_rotate, args=(t,)) for t in (keys, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(created) == 2
    stored = {k["kid"] for k in _stored(path)}
    assert {k.kid for k in created} <= stored
    assert len(stored) == 3


def test_sign_with_no_usable_key_raises(keys, clock):
    new = keys.rotate()
    # Hand-edit: retire the signing key with no successor ready.
    data = json.loads(keys._path.read_text())
    for entry in data["keys"]:
        entry["retired_at"] = clock.now - 1
    keys._path.write_text(json.dumps(data))
    with pytest.raises(KeySetError, match="No upstream claim signing key is usable."):
        keys.signing_key()
    assert new.kid
