"""Tests for scoped_tokens.py: the store, the policy and the request scope (ADR 0164)."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from privacyfence import auto_accept, paths, scoped_tokens
from privacyfence.policy import scope_rows, store
from privacyfence.policy.engine import PolicyRule
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

RULES = [
    SimpleNamespace(id="rule-a", predicate="pred-a", operations=frozenset({"op.read", "op.write"})),
    SimpleNamespace(id="rule-b", predicate="pred-b", operations=frozenset({"op.read"})),
    SimpleNamespace(id="rule-b", predicate="pred-b2", operations=frozenset({"op.search"})),
]
ENABLED = ScopedTokenPolicy(enabled=True, max_days=365)
NOW = 1_000_000.0
TOOL = "gmail_list_messages"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(auto_accept, "get_policy_v2_store_rules", lambda: list(RULES))


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

    def test_rename_changes_only_the_label(self):
        store = _store()
        token, secret = _create(store, label="a")
        old, renamed = store.rename(LOCAL_PRINCIPAL, token.id, "b")
        assert renamed.label == "b"
        found = store.find(secret, NOW)
        assert found == renamed
        for attr in ("digest", "id", "expires_at", "rule_ids", "tools", "allow_personal_data", "rule_limits"):
            assert getattr(found, attr) == getattr(token, attr)
        assert _store().tokens_for(LOCAL_PRINCIPAL)[0].label == "b"

    def test_rename_returns_the_old_label(self):
        store = _store()
        token, _ = _create(store, label="a")
        assert store.rename(LOCAL_PRINCIPAL, token.id, "b")[0] == "a"

    def test_rename_to_a_taken_label_is_refused(self):
        store = _store()
        _create(store, label="a")
        other, _ = _create(store, label="b")
        with pytest.raises(ScopedTokenError) as err:
            store.rename(LOCAL_PRINCIPAL, other.id, "a")
        assert str(err.value) == "You already have a token with that name."

    def test_rename_to_its_own_label_is_a_no_op(self, monkeypatch):
        store = _store()
        token, _ = _create(store, label="a")
        monkeypatch.setattr(store, "_write", lambda p: pytest.fail("wrote"))
        assert store.rename(LOCAL_PRINCIPAL, token.id, "a") == ("a", token)

    def test_rename_rejects_an_invalid_label(self):
        store = _store()
        token, _ = _create(store, label="a")
        with pytest.raises(ScopedTokenError, match="A token name is 1 to 60 letters"):
            store.rename(LOCAL_PRINCIPAL, token.id, "-bad")

    def test_rename_of_an_unknown_token_returns_none(self):
        assert _store().rename(LOCAL_PRINCIPAL, "st-missing", "x") is None

    def test_rename_keeps_an_expired_token_renamable(self):
        store = _store()
        token, secret = _create(store, label="a", days=1)
        assert store.rename(LOCAL_PRINCIPAL, token.id, "b")[1].label == "b"
        assert store.find(secret, token.expires_at) is None

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


class TestRuleLimits:
    def test_create_records_limits_for_every_rule(self):
        token, _ = _create(_store(), rule_ids=["rule-a", "rule-b"])
        assert token.rule_limits == {
            "rule-a": {("pred-a", "op.read"), ("pred-a", "op.write")},
            "rule-b": {("pred-b", "op.read"), ("pred-b2", "op.search")},
        }

    def test_rule_limits_round_trip(self, tmp_path):
        token, _ = _create(_store(), rule_ids=["rule-a"])
        raw = json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"][0]
        assert raw["rule_limits"] == {"rule-a": [["pred-a", "op.read"], ["pred-a", "op.write"]]}
        restarted = _store()
        restarted.load()
        assert restarted.get("local", token.id).rule_limits == token.rule_limits

    @pytest.mark.parametrize("bad", ["x", {"rule-a": "x"}, {"rule-a": [["only-one"]]}, {"rule-a": [["p", 1]]}, {"rule-a": [{}]}])
    def test_malformed_rule_limits_skips_the_token(self, tmp_path, bad):
        token, secret = _create(_store())
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"][0]["rule_limits"] = bad
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")
        restarted = _store()
        restarted.load()
        assert restarted.get("local", token.id) is None
        assert restarted.find(secret, now=NOW) is None


def _raw(rule_id, predicate, operations, flagged=False):
    data = store.rule_to_dict(PolicyRule(
        id=rule_id, predicate=predicate, value=["F1"], operations=frozenset(operations),
    ))
    if flagged:
        data["allow_personal_data"] = True
    return data


NARROW = _raw("r-narrow", "approved_folder", ["drive.download_file"])
FLAGGED = _raw("r-flagged", "approved_folder", ["drive.read_file_contents", "drive.download_file"], flagged=True)


def _known(monkeypatch, *ids):
    monkeypatch.setattr(
        auto_accept, "get_policy_v2_store_rules",
        lambda: [SimpleNamespace(id=i, predicate="approved_folder", operations=frozenset({"x"})) for i in ids],
    )


def _tokens(tmp_path):
    return {t["label"]: t for t in json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"]}


class TestMigrateTokenFile:
    def test_migrate_token_file_replaces_old_ids(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-narrow", "r-flagged", "r-other")
        store_ = _store()
        _create(store_, label="narrow", rule_ids=["r-narrow", "r-other"])
        migration = scope_rows.migrate_v2([NARROW, FLAGGED])
        (row,) = migration.rows

        labels = scoped_tokens.migrate_token_file(_file(tmp_path), migration)

        assert labels == []
        entry = _tokens(tmp_path)["narrow"]
        assert entry["rule_ids"] == sorted([row.id, "r-other"])
        assert entry["rule_limits"][row.id] == [["approved_folder", "drive.download_file"]]
        assert "allow_personal_data" not in entry

    def test_migrate_token_file_is_idempotent(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-narrow", "r-flagged")
        _create(_store(), label="a", rule_ids=["r-narrow"])
        _create(_store(), label="b", rule_ids=["r-flagged"])
        migration = scope_rows.migrate_v2([NARROW, FLAGGED])

        first = scoped_tokens.migrate_token_file(_file(tmp_path), migration)
        written = _file(tmp_path).read_bytes()
        second = scoped_tokens.migrate_token_file(_file(tmp_path), migration)

        assert first == ["b"]
        assert second == []
        assert _file(tmp_path).read_bytes() == written

    def test_partial_reference_records_narrow_limits(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-narrow", "r-flagged")
        _create(_store(), label="narrow", rule_ids=["r-narrow"])
        _create(_store(), label="both", rule_ids=["r-narrow", "r-flagged"])
        migration = scope_rows.migrate_v2([NARROW, FLAGGED])
        (row,) = migration.rows

        scoped_tokens.migrate_token_file(_file(tmp_path), migration)

        tokens = _tokens(tmp_path)
        assert tokens["narrow"]["rule_limits"] == {row.id: [["approved_folder", "drive.download_file"]]}
        assert tokens["both"]["rule_limits"] == {row.id: [
            ["approved_folder", "drive.download_file"], ["approved_folder", "drive.read_file_contents"],
        ]}
        assert row.verbs and len(scope_rows.pairs_of(migration.rows, row.id)) >= 2

    def test_custom_row_reference_gets_its_own_limit(self, tmp_path, monkeypatch):
        odd = _raw("r-odd", "approved_folder", ["drive.write_file"])
        fine = _raw("r-fine", "approved_folder", ["drive.read_file_contents"])
        _known(monkeypatch, "r-odd")
        _create(_store(), label="odd", rule_ids=["r-odd"])
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        del data["tokens"][0]["rule_limits"]  # a token written before limits existed
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")
        migration = scope_rows.migrate_v2([odd, fine])

        scoped_tokens.migrate_token_file(_file(tmp_path), migration)

        entry = _tokens(tmp_path)["odd"]
        assert entry["rule_ids"] == ["r-odd"]
        assert entry["rule_limits"] == {"r-odd": [["approved_folder", "drive.write_file"]]}

    def test_flag_transfer_returns_labels(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-narrow", "r-flagged")
        _create(_store(), label="plain", rule_ids=["r-narrow"])
        _create(_store(), label="pii", rule_ids=["r-flagged"])
        _create(_store(), label="already", rule_ids=["r-flagged"], allow_personal_data=True)

        labels = scoped_tokens.migrate_token_file(_file(tmp_path), scope_rows.migrate_v2([NARROW, FLAGGED]))

        assert labels == ["pii"]
        tokens = _tokens(tmp_path)
        assert tokens["pii"]["allow_personal_data"] is True
        assert "allow_personal_data" not in tokens["plain"]

    def test_missing_file_is_left_alone(self, tmp_path):
        path = tmp_path / "nowhere" / STORE_FILE_NAME
        assert scoped_tokens.migrate_token_file(path, scope_rows.migrate_v2([NARROW])) == []
        assert not path.exists()
        path = tmp_path / STORE_FILE_NAME
        path.write_text("{not json", encoding="utf-8")
        assert scoped_tokens.migrate_token_file(path, scope_rows.migrate_v2([NARROW])) == []
        assert path.read_text(encoding="utf-8") == "{not json"

    def test_malformed_entry_is_kept_untouched(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-narrow")
        _create(_store(), label="ok", rule_ids=["r-narrow"])
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"].append({"id": "broken"})
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")

        scoped_tokens.migrate_token_file(_file(tmp_path), scope_rows.migrate_v2([NARROW]))

        assert json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"][1] == {"id": "broken"}

    def test_apply_migration_holds_the_lock_and_refreshes(self, tmp_path, monkeypatch):
        a1 = _raw("r-a1", "approved_folder", ["drive.read_file_contents"])
        a2 = _raw("r-a2", "approved_folder", ["drive.download_file"], flagged=True)
        b = _raw("r-b", "approved_folder", ["drive.read_file_contents"])
        b["value"] = ["F2"]
        m1, m2 = scope_rows.migrate_v2([a1, a2]), scope_rows.migrate_v2([b])
        _known(monkeypatch, "r-a1", "r-b", "r-c")
        one, two = _store(), _store()  # two handles on one file, like two processes
        _create(one, label="t1", rule_ids=["r-a1"])
        _create(two, label="t2", rule_ids=["r-b"])  # reads t1 from disk, writes both

        assert one.apply_migration(LOCAL_PRINCIPAL, m1) == []  # refreshes: sees t2 too
        _create(one, label="t3", rule_ids=["r-c"])
        assert two.apply_migration(LOCAL_PRINCIPAL, m2) == []
        assert two.apply_migration(LOCAL_PRINCIPAL, m2) == []  # idempotent through the store

        fresh = _store()
        fresh.load()
        by_label = {t.label: t for t in fresh.tokens_for(LOCAL_PRINCIPAL)}
        assert set(by_label) == {"t1", "t2", "t3"}
        assert by_label["t1"].rule_ids == {m1.rows[0].id}
        assert by_label["t2"].rule_ids == {m2.rows[0].id}
        assert by_label["t3"].rule_ids == {"r-c"}

    def test_migrate_tokens_uses_the_live_store_when_set(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-narrow")
        live = _store()
        scoped_tokens.set_store(None)
        assert scoped_tokens.current_store() is None
        _create(live, label="a", rule_ids=["r-narrow"])
        migration = scope_rows.migrate_v2([NARROW])
        scoped_tokens.migrate_tokens(LOCAL_PRINCIPAL, migration)  # no store: edits the file
        assert _tokens(tmp_path)["a"]["rule_ids"] == [migration.rows[0].id]

        scoped_tokens.set_store(live)
        try:
            assert scoped_tokens.current_store() is live
            monkeypatch.setattr(live, "apply_migration", lambda p, m: ["via-store"])
            assert scoped_tokens.migrate_tokens(LOCAL_PRINCIPAL, migration) == ["via-store"]
        finally:
            scoped_tokens.set_store(None)


def _add_limit(tmp_path, label, rule_id, pairs):
    data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
    for entry in data["tokens"]:
        if entry["label"] == label:
            entry.setdefault("rule_limits", {})[rule_id] = pairs
    _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")


def _stamp(tmp_path):
    st = _file(tmp_path).stat()
    return st.st_mtime_ns, st.st_ino


class TestForgetRules:
    def test_drops_the_id_and_its_limit_and_keeps_the_rest(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1", "r-2")
        store_ = _store()
        _, secret = _create(
            store_, label="job", rule_ids=["r-1", "r-2"], tools=[TOOL, "gmail_get_message"],
            allow_personal_data=True,
        )
        _add_limit(tmp_path, "job", "r-1", [["approved_folder", "drive.download_file"]])
        _add_limit(tmp_path, "job", "r-2", [["approved_folder", "drive.write_file"]])
        store_.load()

        labels = store_.forget_rules(LOCAL_PRINCIPAL, ["r-1"])

        assert labels == ["job"]
        token = store_.find(secret, NOW)
        assert token is not None
        assert token.rule_ids == {"r-2"}
        assert token.rule_limits == {"r-2": frozenset({("approved_folder", "drive.write_file")})}
        assert token.tools == {TOOL, "gmail_get_message"}
        assert token.allow_personal_data is True
        assert _tokens(tmp_path)["job"]["rule_ids"] == ["r-2"]

    def test_returns_only_the_labels_that_changed_in_file_order(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1", "r-2")
        store_ = _store()
        _create(store_, label="b", rule_ids=["r-1"])
        _create(store_, label="plain", rule_ids=["r-2"])
        _create(store_, label="a", rule_ids=["r-1", "r-2"])

        assert store_.forget_rules(LOCAL_PRINCIPAL, ["r-1"]) == ["b", "a"]

    def test_no_write_when_nothing_matches(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1")
        store_ = _store()
        _create(store_, label="job", rule_ids=["r-1"])
        before = _stamp(tmp_path)

        assert store_.forget_rules(LOCAL_PRINCIPAL, ["r-unknown"]) == []
        assert _stamp(tmp_path) == before

    def test_a_token_left_with_nothing_stays(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1")
        store_ = _store()
        token, secret = _create(store_, label="job", rule_ids=["r-1"], tools=[])

        assert store_.forget_rules(LOCAL_PRINCIPAL, ["r-1"]) == ["job"]
        kept = store_.find(secret, NOW)
        assert kept is not None and kept.id == token.id
        assert kept.rule_ids == frozenset() and kept.tools == frozenset()

    def test_the_change_survives_a_fresh_load(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1", "r-2")
        store_ = _store()
        _create(store_, label="job", rule_ids=["r-1", "r-2"])
        store_.forget_rules(LOCAL_PRINCIPAL, ["r-1"])

        fresh = _store()
        fresh.load()
        (token,) = fresh.tokens_for(LOCAL_PRINCIPAL)
        assert token.rule_ids == {"r-2"}

    def test_refreshes_before_changing(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1")
        one, two = _store(), _store()
        _create(one, label="t1", rule_ids=["r-1"])
        _create(two, label="t2", rule_ids=["r-1"])  # a write one has not seen yet

        assert one.forget_rules(LOCAL_PRINCIPAL, ["r-1"]) == ["t1", "t2"]

    def test_file_variant_keeps_an_unreadable_entry_and_changes_the_rest(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1")
        _create(_store(), label="job", rule_ids=["r-1"])
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"].append({"id": "broken"})
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")

        assert scoped_tokens.forget_rules_in_file(_file(tmp_path), ["r-1"]) == ["job"]

        written = json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"]
        assert written[0]["rule_ids"] == []
        assert written[1] == {"id": "broken"}

    def test_file_variant_writes_nothing_when_nothing_matches(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1")
        _create(_store(), label="job", rule_ids=["r-1"])
        before = _stamp(tmp_path)

        assert scoped_tokens.forget_rules_in_file(_file(tmp_path), ["r-other"]) == []
        assert _stamp(tmp_path) == before

    def test_file_variant_leaves_a_missing_or_unreadable_file_alone(self, tmp_path):
        missing = tmp_path / "nowhere" / STORE_FILE_NAME
        assert scoped_tokens.forget_rules_in_file(missing, ["r-1"]) == []
        assert not missing.exists()
        broken = tmp_path / STORE_FILE_NAME
        broken.write_text("{not json", encoding="utf-8")
        assert scoped_tokens.forget_rules_in_file(broken, ["r-1"]) == []
        assert broken.read_text(encoding="utf-8") == "{not json"

    def test_module_function_uses_the_file_when_no_store_is_set(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1")
        _create(_store(), label="job", rule_ids=["r-1"])
        scoped_tokens.set_store(None)

        def _boom(*_a, **_k):
            raise AssertionError("the store must not be used")

        monkeypatch.setattr(ScopedTokenStore, "forget_rules", _boom)

        assert scoped_tokens.forget_rules(LOCAL_PRINCIPAL, ["r-1"]) == ["job"]
        assert _tokens(tmp_path)["job"]["rule_ids"] == []

    def test_module_function_uses_the_live_store_when_set(self, tmp_path, monkeypatch):
        _known(monkeypatch, "r-1")
        live = _store()
        _create(live, label="job", rule_ids=["r-1"])

        def _boom(*_a, **_k):
            raise AssertionError("the file must not be edited behind the store")

        monkeypatch.setattr(scoped_tokens, "forget_rules_in_file", _boom)
        scoped_tokens.set_store(live)
        try:
            assert scoped_tokens.forget_rules(LOCAL_PRINCIPAL, ["r-1"]) == ["job"]
        finally:
            scoped_tokens.set_store(None)
        assert _tokens(tmp_path)["job"]["rule_ids"] == []

    def test_module_function_with_no_ids_touches_nothing(self, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("nothing to do")

        monkeypatch.setattr(ScopedTokenStore, "forget_rules", _boom)
        monkeypatch.setattr(scoped_tokens, "forget_rules_in_file", _boom)
        scoped_tokens.set_store(_store())
        try:
            assert scoped_tokens.forget_rules(LOCAL_PRINCIPAL, []) == []
        finally:
            scoped_tokens.set_store(None)
