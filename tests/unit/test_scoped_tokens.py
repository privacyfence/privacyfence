"""Tests for scoped_tokens.py: the store, the policy and the request scope (ADR 0164)."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from privacyfence import auto_accept, paths, scoped_tokens
from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.scoped_tokens import (
    MAX_TOKENS_PER_PRINCIPAL,
    REVOKED_SCOPE,
    STORE_FILE_NAME,
    TOKEN_PREFIX,
    ScopedTokenError,
    ScopedTokenPolicy,
    ScopedTokenStore,
)

ENABLED = ScopedTokenPolicy(enabled=True, max_days=365)
NOW = 1_000_000.0
TOOL = "gmail_list_messages"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        auto_accept, "get_policy_v2_store_rules", lambda: [SimpleNamespace(id="rule-a"), SimpleNamespace(id="rule-b")]
    )


def _store(*principals: Principal) -> ScopedTokenStore:
    return ScopedTokenStore(lambda: list(principals or [LOCAL_PRINCIPAL]))


def _create(store, principal=LOCAL_PRINCIPAL, **overrides):
    args = dict(label="sales-sync", days=90, rule_ids=["rule-a"], tools=[TOOL], policy=ENABLED, now=NOW)
    args.update(overrides)
    return store.create(principal, **args)


def _file(tmp_path):
    return tmp_path / "authority" / STORE_FILE_NAME


class TestCreateValidation:
    def test_disabled(self):
        with pytest.raises(ScopedTokenError) as err:
            _create(_store(), policy=ScopedTokenPolicy(False, 90))
        assert str(err.value) == (
            "Scoped tokens are not enabled on this install. "
            "Your administrator turns them on in the organization config bundle."
        )

    @pytest.mark.parametrize("label", ["", " x", "-x", "x" * 61, "a/b"])
    def test_bad_label(self, label):
        with pytest.raises(ScopedTokenError) as err:
            _create(_store(), label=label)
        assert str(err.value) == (
            "A token name is 1 to 60 letters, digits, spaces, dots, dashes or underscores, "
            "starting with a letter or digit."
        )

    def test_duplicate_label(self):
        store = _store()
        _create(store)
        with pytest.raises(ScopedTokenError, match="^You already have a token with that name.$"):
            _create(store)

    @pytest.mark.parametrize("days", [0, 366])
    def test_days_out_of_range(self, days):
        with pytest.raises(ScopedTokenError) as err:
            _create(_store(), days=days)
        assert str(err.value) == "A token lasts 1 to 365 days."

    def test_days_capped_by_policy(self):
        with pytest.raises(ScopedTokenError, match="^A token lasts 1 to 30 days.$"):
            _create(_store(), days=31, policy=ScopedTokenPolicy(True, 30))

    def test_no_rule_and_no_tool(self):
        with pytest.raises(ScopedTokenError, match="^Choose at least one tool for the token.$"):
            _create(_store(), rule_ids=[], tools=[])

    def test_tools_without_rules_are_allowed(self):
        token, _ = _create(_store(), rule_ids=[])
        assert token.rule_ids == frozenset()

    def test_unknown_tool(self):
        with pytest.raises(ScopedTokenError, match="^nope is not a PrivacyFence tool.$"):
            _create(_store(), tools=["nope"])

    def test_unknown_rule(self):
        with pytest.raises(ScopedTokenError) as err:
            _create(_store(), rule_ids=["rule-gone"])
        assert str(err.value) == "One of the chosen rules no longer exists; reload the page."

    def test_too_many_tokens(self):
        store = _store()
        for i in range(MAX_TOKENS_PER_PRINCIPAL):
            _create(store, label=f"t{i}")
        with pytest.raises(ScopedTokenError) as err:
            _create(store, label="one-more")
        assert str(err.value) == f"You have {MAX_TOKENS_PER_PRINCIPAL} tokens already; revoke one first."


class TestStore:
    def test_create_returns_a_prefixed_secret_and_stores_only_its_digest(self, tmp_path):
        token, secret = _create(_store())
        assert secret.startswith(TOKEN_PREFIX)
        assert token.id.startswith("st-") and len(token.id) == 15
        raw = _file(tmp_path).read_text(encoding="utf-8")
        assert secret not in raw
        assert json.loads(raw)["tokens"][0]["digest"] == token.digest

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes do not exist on Windows")
    def test_file_is_mode_600(self, tmp_path):
        _create(_store())
        assert _file(tmp_path).stat().st_mode & 0o777 == 0o600

    def test_find_returns_the_token_and_none_for_another_secret(self):
        store = _store()
        token, secret = _create(store)
        assert store.find(secret, NOW) == token
        assert store.find(TOKEN_PREFIX + "other", NOW) is None

    def test_find_ignores_secrets_without_the_prefix(self):
        store = _store()
        _create(store)
        assert store.find("abc", NOW) is None

    def test_find_returns_none_once_expired(self):
        store = _store()
        token, secret = _create(store, days=1)
        assert store.find(secret, token.expires_at - 1) == token
        assert store.find(secret, token.expires_at) is None

    def test_find_without_now_uses_the_clock(self):
        store = _store()
        _, secret = _create(store, now=None)
        assert store.find(secret) is not None

    def test_revoke_and_revoke_all(self):
        store = _store()
        first, first_secret = _create(store, label="a")
        _, second_secret = _create(store, label="b")
        _, third_secret = _create(store, label="c")
        assert store.revoke(LOCAL_PRINCIPAL, first.id) is True
        assert store.revoke(LOCAL_PRINCIPAL, first.id) is False
        assert store.find(first_secret, NOW) is None
        assert store.find(second_secret, NOW) is not None
        assert store.revoke_all("local") == 2
        assert store.revoke_all("local") == 0
        assert store.find(third_secret, NOW) is None
        assert store.tokens_for(LOCAL_PRINCIPAL) == []

    def test_revoke_all_for_a_principal_the_store_has_not_seen(self, tmp_path):
        other = Principal(id="alice", email="a@example.com", display_name="Alice")
        _create(_store(other), other)
        fresh = _store()
        assert fresh.revoke_all("alice") == 1
        assert fresh.revoke_all("a/../b") == 0

    def test_a_revocation_written_by_another_store_is_seen_on_next_find(self):
        first, second = _store(), _store()
        token, secret = _create(first)
        second.load()
        assert first.find(secret, NOW) == token
        assert second.revoke(LOCAL_PRINCIPAL, token.id) is True
        assert first.find(secret, NOW) is None
        assert first.get("local", token.id) is None

    def test_load_after_restart_finds_the_token(self):
        token, secret = _create(_store())
        restarted = _store()
        restarted.load()
        assert restarted.find(secret, NOW) == token
        assert restarted.get("local", token.id) == token
        assert restarted.get("nobody", token.id) is None

    def test_org_principal_claims_round_trip(self):
        alice = Principal(id="alice", email="a@example.com", display_name="Alice")
        token, _ = _create(_store(alice), alice)
        restarted = _store(alice)
        restarted.load()
        (loaded,) = restarted.tokens_for(alice)
        assert (loaded.principal_id, loaded.principal_email, loaded.principal_display_name) == (
            "alice", "a@example.com", "Alice",
        )
        assert loaded == token

    def test_create_stores_allow_personal_data_and_defaults_to_false(self):
        store = _store()
        plain, _ = _create(store, label="plain")
        flagged, _ = _create(store, label="flagged", allow_personal_data=True)
        assert plain.allow_personal_data is False
        assert flagged.allow_personal_data is True
        assert REVOKED_SCOPE.allow_personal_data is False

    def test_allow_personal_data_round_trips_and_is_written_only_when_true(self, tmp_path):
        store = _store()
        plain, _ = _create(store, label="plain")
        flagged, _ = _create(store, label="flagged", allow_personal_data=True)
        entries = {e["label"]: e for e in json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"]}
        assert "allow_personal_data" not in entries["plain"]
        assert entries["flagged"]["allow_personal_data"] is True
        restarted = _store()
        restarted.load()
        assert restarted.get("local", plain.id) == plain
        assert restarted.get("local", flagged.id) == flagged

    def test_old_file_without_the_key_loads_as_false(self, tmp_path):
        token, _ = _create(_store(), allow_personal_data=True)
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        del data["tokens"][0]["allow_personal_data"]
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")
        restarted = _store()
        restarted.load()
        assert restarted.get("local", token.id).allow_personal_data is False

    def test_a_non_true_value_in_the_file_loads_as_false(self, tmp_path):
        token, _ = _create(_store(), allow_personal_data=True)
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"][0]["allow_personal_data"] = "yes"
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")
        restarted = _store()
        restarted.load()
        assert restarted.get("local", token.id).allow_personal_data is False

    def test_malformed_file_is_skipped_not_raised(self, tmp_path, caplog):
        _create(_store())
        _file(tmp_path).write_text("{not json", encoding="utf-8")
        store = _store()
        store.load()
        assert store.tokens_for(LOCAL_PRINCIPAL) == []
        _file(tmp_path).write_text(json.dumps({"tokens": 5}), encoding="utf-8")
        os.utime(_file(tmp_path), ns=(1, 2))
        assert store.tokens_for(LOCAL_PRINCIPAL) == []
        assert "unreadable scoped token file" in caplog.text

    def test_malformed_entry_is_skipped_not_raised(self, tmp_path, caplog):
        good, secret = _create(_store(), label="good")
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"].insert(0, {"id": "st-bad"})
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")
        store = _store()
        store.load()
        assert store.find(secret, NOW) == good
        assert [t.id for t in store.tokens_for(LOCAL_PRINCIPAL)] == [good.id]
        assert "malformed scoped token entry" in caplog.text

    def test_default_store_is_empty_and_settable(self):
        assert scoped_tokens.get_store().find(TOKEN_PREFIX + "x") is None
        store = _store()
        scoped_tokens.set_store(store)
        assert scoped_tokens.get_store() is store


class TestPolicy:
    def test_policy_local_is_always_enabled_365(self):
        policy = scoped_tokens.policy_from_org_config({"scoped_tokens": {"enabled": False, "max_days": 5}}, org_mode=False)
        assert policy == ScopedTokenPolicy(True, 365)

    def test_policy_org_defaults_off_90_and_clamps_bad_max_days(self, caplog):
        from_cfg = scoped_tokens.policy_from_org_config
        assert from_cfg({}, org_mode=True) == ScopedTokenPolicy(False, 90)
        assert from_cfg({"scoped_tokens": "yes"}, org_mode=True) == ScopedTokenPolicy(False, 90)
        assert from_cfg({"scoped_tokens": {"enabled": True}}, org_mode=True) == ScopedTokenPolicy(True, 90)
        assert from_cfg({"scoped_tokens": {"enabled": "true"}}, org_mode=True).enabled is False
        assert from_cfg({"scoped_tokens": {"enabled": True, "max_days": 30}}, org_mode=True) == ScopedTokenPolicy(True, 30)
        for bad in (0, 366, "30", 1.5, True, None):
            assert from_cfg({"scoped_tokens": {"enabled": True, "max_days": bad}}, org_mode=True).max_days == 90
        assert "scoped_tokens.max_days" in caplog.text


class TestToken:
    def test_days_left_and_expired(self):
        token, _ = _create(_store(), days=2)
        assert token.days_left(NOW) == 2
        assert token.days_left(NOW + 86400 + 1) == 1
        assert token.days_left(token.expires_at + 10) == 0
        assert token.expired(token.expires_at) is True
        assert token.expired(token.expires_at - 1) is False

    def test_revoked_scope_allows_nothing(self):
        assert REVOKED_SCOPE.rule_ids == frozenset() and REVOKED_SCOPE.tools == frozenset()
        assert REVOKED_SCOPE.expired(NOW)


def test_token_scope_sets_and_resets_the_context():
    token, _ = _create(_store())
    assert scoped_tokens.current_token_scope() is None
    with scoped_tokens.token_scope(token):
        assert scoped_tokens.current_token_scope() is token
        with scoped_tokens.token_scope(None):
            assert scoped_tokens.current_token_scope() is None
        assert scoped_tokens.current_token_scope() is token
    assert scoped_tokens.current_token_scope() is None
    scoped_tokens.token_scope(token).__exit__(None, None, None)  # exit without enter is a no-op
