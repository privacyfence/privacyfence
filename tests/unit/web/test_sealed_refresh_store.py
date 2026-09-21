"""Tests for web/sealed_refresh_store.py -- the on-disk half of org mode's
OAuth refresh tokens (#402).

The property that matters most here isn't "a record round-trips" but what the
file is worth *without* the token: every test that touches the raw JSON checks
that the sealed half stays sealed, because that is the whole argument for
persisting these at all (see the module's own docstring).
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from privacyfence.web import sealed_refresh_store as srs

TOKEN = "refresh-token-value"
FAR_FUTURE = 30 * 24 * 60 * 60


def _record(**overrides) -> srs.SealedRefreshRecord:
    fields = {
        "client_id": "claude", "scopes": ["mcp"], "subject": "user-1",
        "email": "ana@example.com", "display_name": "Ana", "is_admin": False,
        "issued_at": 1_000.0,
    }
    return srs.SealedRefreshRecord(**{**fields, **overrides})


def _store(tmp_path, name="oauth_refresh.json") -> srs.SealedRefreshStore:
    return srs.SealedRefreshStore(tmp_path / name)


def _put(store, token=TOKEN, *, principal_id="user-1", expires_in=FAR_FUTURE, **overrides) -> None:
    store.put(
        token, principal_id=principal_id, chain_expires_at=time.time() + expires_in,
        record=_record(**overrides),
    )


class TestRoundTrip:
    def test_a_stored_record_comes_back_for_the_token_that_sealed_it(self, tmp_path):
        store = _store(tmp_path)
        _put(store, is_admin=True, scopes=["mcp", "admin"])
        assert store.get(TOKEN) == _record(is_admin=True, scopes=["mcp", "admin"])

    def test_it_survives_reopening_the_file(self, tmp_path):
        _put(_store(tmp_path))
        reopened = _store(tmp_path)
        assert reopened.restored_count == 1
        assert reopened.get(TOKEN) == _record()

    def test_an_unknown_token_is_not_an_error(self, tmp_path):
        store = _store(tmp_path)
        _put(store)
        assert store.get("some-other-token") is None

    def test_record_count_tracks_what_is_held(self, tmp_path):
        store = _store(tmp_path)
        assert store.record_count == 0
        _put(store, "a")
        _put(store, "b")
        assert store.record_count == 2


class TestWhatTheFileGivesAway:
    """The file is the threat model. These are the tests that would fail if a
    future change started writing the sealed half in the clear."""

    def test_the_token_and_its_claims_appear_nowhere_in_the_file(self, tmp_path):
        store = _store(tmp_path)
        # The display name carries a space on purpose, and the assertion below
        # uses it in full. This checks containment against the whole file --
        # the sealed ciphertext included, which is the point: a leak would be
        # just as real inside that field as beside it. But the ciphertext is
        # base64, drawn from [A-Za-z0-9+/=], so a sentinel built only from
        # those characters can turn up in it by chance. A three-character one
        # does: `display_name="Ana"` collided in 42 of 40,000 sealed files
        # here (0.1%, ~1 CI run in 1140), and took a release-blocking PR red
        # once. Any character outside that alphabet makes a coincidental match
        # impossible rather than merely unlikely, which is why the token
        # ("-") and email ("@", ".") sentinels have never been affected.
        _put(store, email="ana@example.com", display_name="Ana Example")
        raw = (tmp_path / "oauth_refresh.json").read_text(encoding="utf-8")
        assert TOKEN not in raw
        assert "ana@example.com" not in raw
        assert "Ana Example" not in raw
        assert "claude" not in raw

    def test_only_the_principal_id_and_chain_expiry_are_in_the_clear(self, tmp_path):
        store = _store(tmp_path)
        _put(store, principal_id="user-1")
        [entry] = json.loads((tmp_path / "oauth_refresh.json").read_text(encoding="utf-8")).values()
        assert set(entry) == {"principal_id", "chain_expires_at", "nonce", "ciphertext"}
        assert entry["principal_id"] == "user-1"

    def test_a_forged_token_cannot_be_made_to_match_a_stored_record(self, tmp_path):
        store = _store(tmp_path)
        _put(store)
        # The lookup key is sha256(token), so an attacker holding the file
        # can enumerate hashes but cannot produce a token for one.
        [lookup_id] = json.loads((tmp_path / "oauth_refresh.json").read_text(encoding="utf-8"))
        assert store.get(lookup_id) is None


class TestExpiry:
    def test_a_chain_lapsing_while_the_store_is_open_is_dropped_on_next_use(self, tmp_path):
        # Backdating the held entry rather than storing an already-lapsed one:
        # put() prunes on the way in, so the only way to reach get()'s own
        # expiry check is for the chain to lapse after it was stored. Same
        # white-box idiom test_oauth_provider.py uses for SEC-12.
        store = _store(tmp_path)
        _put(store)
        store._entries[srs._lookup_id(TOKEN)].chain_expires_at = 1

        assert store.get(TOKEN) is None
        assert store.record_count == 0
        assert _store(tmp_path).restored_count == 0

    def test_a_lapsed_chain_does_not_survive_reopening(self, tmp_path):
        store = _store(tmp_path)
        _put(store, "live")
        _put(store, "also-live")
        store._entries[srs._lookup_id("live")].chain_expires_at = 1
        store._save_locked()

        assert _store(tmp_path).restored_count == 1

    def test_reopening_restores_every_live_chain_in_the_file(self, tmp_path):
        store = _store(tmp_path)
        _put(store, "one", principal_id="ana")
        _put(store, "two", principal_id="bo")

        reopened = _store(tmp_path)
        assert reopened.restored_count == 2
        assert reopened.get("one") is not None
        assert reopened.get("two") is not None

    def test_putting_prunes_whatever_has_lapsed_since(self, tmp_path):
        store = _store(tmp_path)
        _put(store, "lapsed", expires_in=-1)
        _put(store, "fresh")
        assert store.record_count == 1
        assert store.get("fresh") is not None


class TestCap:
    def test_over_the_cap_the_chains_closest_to_expiry_go_first(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(srs, "_MAX_RECORDS", 2)
        store = _store(tmp_path)
        _put(store, "soonest", expires_in=10)
        _put(store, "middle", expires_in=20)
        with caplog.at_level("WARNING"):
            _put(store, "latest", expires_in=30)
        assert store.record_count == 2
        assert store.get("soonest") is None
        assert store.get("middle") is not None
        assert store.get("latest") is not None
        assert "cap" in caplog.text

    def test_at_the_cap_nothing_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(srs, "_MAX_RECORDS", 2)
        store = _store(tmp_path)
        _put(store, "a", expires_in=10)
        _put(store, "b", expires_in=20)
        assert store.record_count == 2


class TestDiscard:
    def test_discarding_removes_it_from_memory_and_disk(self, tmp_path):
        store = _store(tmp_path)
        _put(store)
        store.discard(TOKEN)
        assert store.get(TOKEN) is None
        assert _store(tmp_path).restored_count == 0

    def test_discarding_something_never_stored_is_a_no_op(self, tmp_path):
        store = _store(tmp_path)
        _put(store)
        before = (tmp_path / "oauth_refresh.json").read_bytes()
        store.discard("never-stored")
        assert (tmp_path / "oauth_refresh.json").read_bytes() == before

    def test_discard_all_for_removes_only_that_principals_chains(self, tmp_path):
        store = _store(tmp_path)
        _put(store, "ana-1", principal_id="ana")
        _put(store, "ana-2", principal_id="ana")
        _put(store, "bo-1", principal_id="bo")
        assert store.discard_all_for("ana") == 2
        assert store.get("ana-1") is None and store.get("ana-2") is None
        assert store.get("bo-1") is not None

    def test_discard_all_for_an_unknown_principal_touches_nothing(self, tmp_path):
        store = _store(tmp_path)
        _put(store)
        before = (tmp_path / "oauth_refresh.json").read_bytes()
        assert store.discard_all_for("nobody") == 0
        assert (tmp_path / "oauth_refresh.json").read_bytes() == before


class TestUnreadableStore:
    """Losing this file costs a sign-in, which is exactly the behavior that
    predates it -- so every damaged-file path starts empty rather than
    refusing to serve."""

    @pytest.mark.parametrize("content", [
        "not json at all",
        '["a list, not an object"]',
        '{"abc": {"principal_id": "u", "chain_expires_at": 1}}',        # missing nonce/ciphertext
        '{"abc": {"principal_id": "u", "chain_expires_at": "soon", "nonce": "", "ciphertext": ""}}',
        '{"abc": {"principal_id": "u", "chain_expires_at": 99999999999, "nonce": "!!", "ciphertext": "!!"}}',
        '{"abc": "not an object either"}',
    ])
    def test_a_damaged_file_starts_empty(self, tmp_path, caplog, content):
        (tmp_path / "oauth_refresh.json").write_text(content, encoding="utf-8")
        with caplog.at_level("WARNING"):
            store = _store(tmp_path)
        assert store.restored_count == 0
        assert "unreadable" in caplog.text

    def test_a_record_that_will_not_decrypt_is_discarded(self, tmp_path, caplog):
        store = _store(tmp_path)
        _put(store)
        path = tmp_path / "oauth_refresh.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        [entry] = raw.values()
        entry["ciphertext"] = base64.b64encode(b"not the ciphertext that was here").decode("ascii")
        path.write_text(json.dumps(raw), encoding="utf-8")

        reopened = _store(tmp_path)
        with caplog.at_level("WARNING"):
            assert reopened.get(TOKEN) is None
        assert reopened.record_count == 0
        assert "would not decrypt" in caplog.text

    def test_a_store_that_cannot_be_written_still_serves_this_process(self, tmp_path, monkeypatch, caplog):
        store = _store(tmp_path)

        def refuse(*_args, **_kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(srs, "atomic_write_json", refuse)
        with caplog.at_level("WARNING"):
            _put(store)
        assert store.get(TOKEN) == _record()
        assert "Could not persist" in caplog.text
