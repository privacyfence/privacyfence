"""Tests for scoped_tokens.py: the store, the policy and the request scope (ADR 0164)."""
from __future__ import annotations

import dataclasses
import json
import os
import sys

import pytest

from privacyfence import auto_accept, paths, scoped_tokens
from privacyfence.policy import scope_rows
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import Verb
from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.scoped_tokens import (
    ACTIVITY_FILE_NAME,
    MAX_TOKENS_PER_PRINCIPAL,
    REVOKED_SCOPE,
    STORE_FILE_NAME,
    TOKEN_PREFIX,
    ScopedTokenError,
    ScopedTokenPolicy,
    ScopedTokenStore,
    TokenActivity,
)

def _row(rule_id, predicate, operations, value=None):
    """The one row ``rows_for_rules`` makes of a single rule (a custom row for these predicates)."""
    (row,) = scope_rows.rows_for_rules(
        [PolicyRule(id=rule_id, predicate=predicate, value=value, operations=frozenset(operations))], rule_id,
    )
    return row


# The person's Auto-accept rows, as ``auto_accept.get_policy_rows`` gives them.
ROWS = [
    _row("rule-a", "pred-a", {"op.read", "op.write"}),
    _row("rule-b", "pred-b", {"op.read"}),
]
ENABLED = ScopedTokenPolicy(enabled=True, max_days=365)
NOW = 1_000_000.0
TOOL = "gmail_list_messages"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(auto_accept, "get_policy_rows", lambda: list(ROWS))


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

    def test_tools_with_no_rules_are_allowed(self):
        token, _ = _create(_store(), rule_ids=[])
        assert token.rules == ()

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
        for attr in ("digest", "id", "expires_at", "rules", "tools", "allow_personal_data"):
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


class TestAllowPersonalDataReads:
    def test_flag_round_trips_through_the_file(self, tmp_path):
        store = _store()
        plain, _ = _create(store, label="plain")
        flagged, _ = _create(store, label="flagged", allow_personal_data_reads=True)
        entries = {e["label"]: e for e in json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"]}
        assert "allow_personal_data_reads" not in entries["plain"]
        assert entries["flagged"]["allow_personal_data_reads"] is True
        restarted = _store()
        restarted.load()
        assert restarted.get("local", plain.id) == plain
        assert restarted.get("local", flagged.id) == flagged
        assert restarted.get("local", flagged.id).allow_personal_data_reads is True

    def test_a_file_without_the_key_loads_false(self, tmp_path):
        token, _ = _create(_store(), allow_personal_data_reads=True)
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        del data["tokens"][0]["allow_personal_data_reads"]
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")
        restarted = _store()
        restarted.load()
        assert restarted.get("local", token.id).allow_personal_data_reads is False

    def test_create_stores_the_flag(self):
        store = _store()
        plain, _ = _create(store, label="plain")
        flagged, _ = _create(store, label="flagged", allow_personal_data_reads=True)
        assert plain.allow_personal_data_reads is False
        assert flagged.allow_personal_data_reads is True
        assert REVOKED_SCOPE.allow_personal_data_reads is False

    def test_the_two_flags_are_independent(self):
        store = _store()
        exports, _ = _create(store, label="exports", allow_personal_data=True)
        reads, _ = _create(store, label="reads", allow_personal_data_reads=True)
        assert (exports.allow_personal_data, exports.allow_personal_data_reads) == (True, False)
        assert (reads.allow_personal_data, reads.allow_personal_data_reads) == (False, True)

    def test_conversion_keeps_the_flag(self, tmp_path):
        _write_entries(tmp_path, [_legacy("reads", allow_personal_data_reads=True)])

        scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE])

        assert _tokens(tmp_path)["reads"]["allow_personal_data_reads"] is True


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
        assert REVOKED_SCOPE.rules == () and REVOKED_SCOPE.tools == frozenset()
        assert REVOKED_SCOPE.compiled_rules() == []
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


MENTIONS_PAIR = ["slack.mentions_of_me", "slack.read_messages"]


def _stamp(tmp_path):
    st = _file(tmp_path).stat()
    return st.st_mtime_ns, st.st_ino


def _tokens(tmp_path):
    return {t["label"]: t for t in json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"]}


# A rule of the person's that a legacy token names by id.
_RULE = PolicyRule(
    id="r-1", predicate="gmail.anything", value=None, operations=frozenset({"gmail.list_messages"}),
)


def _legacy(label, rule_ids=("r-1",), secret="", **extra):
    """A token entry as a version before ADR 0236 wrote it: rule ids, no rules."""
    return {
        "id": f"st-{label}", "label": label, "digest": scoped_tokens._digest(secret or label),
        "principal_email": "", "principal_display_name": "", "created_at": NOW, "expires_at": NOW + 86400,
        "rule_ids": list(rule_ids), "tools": [TOOL], **extra,
    }


def _write_entries(tmp_path, entries, version=1):
    _file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _file(tmp_path).write_text(json.dumps({"version": version, "tokens": entries}), encoding="utf-8")


class TestTokenRows:
    """A token stores its own rows, in the auto_accept: row format (ADR 0236)."""

    def test_a_created_token_entry_has_rows_and_no_rule_ids(self, tmp_path):
        token, _ = _create(_store(), rule_ids=["rule-a"])

        (entry,) = json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"]

        assert entry["rules"] == [scope_rows.row_to_dict(ROWS[0])]
        assert not set(scoped_tokens._LEGACY_KEYS) & set(entry)
        assert token.rules == (ROWS[0],)

    def test_a_copied_row_drops_its_former_ids(self, monkeypatch):
        renamed = dataclasses.replace(ROWS[0], former_ids=("old-id",))
        monkeypatch.setattr(auto_accept, "get_policy_rows", lambda: [renamed])

        token, _ = _create(_store(), rule_ids=["rule-a"])

        assert token.rules == (dataclasses.replace(renamed, former_ids=()),)

    def test_the_file_has_version_2(self, tmp_path):
        _create(_store())

        assert json.loads(_file(tmp_path).read_text(encoding="utf-8"))["version"] == 2

    def test_a_token_without_rules_does_not_read_the_auto_accept_rows(self, monkeypatch):
        def unavailable():
            raise AssertionError("read the Auto-accept rows")

        monkeypatch.setattr(auto_accept, "get_policy_rows", unavailable)

        token, _ = _create(_store(), rule_ids=[])

        assert token.rules == ()

    def test_a_row_that_cannot_be_read_is_dropped_and_the_token_still_loads(self, tmp_path, caplog):
        token, secret = _create(_store(), rule_ids=["rule-a", "rule-b"])
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"][0]["rules"][0]["scope"] = "retired.scope"
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")

        restarted = _store()
        restarted.load()

        assert restarted.find(secret, NOW).rules == (ROWS[1],)
        assert f"Scoped token {token.id}: skipping a rule that cannot be read" in caplog.text

    def test_a_rules_value_that_is_not_a_list_skips_the_token(self, tmp_path):
        token, _ = _create(_store())
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"][0]["rules"] = {"rule-a": True}
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")

        restarted = _store()
        restarted.load()

        assert restarted.get("local", token.id) is None

    def test_a_legacy_entry_loads_with_no_rules_and_round_trips(self, tmp_path, caplog):
        entry = _legacy("old", rule_limits={"r-1": [["gmail.anything", "gmail.list_messages"]]})
        _write_entries(tmp_path, [entry])
        store_ = _store()
        store_.load()
        (token,) = store_.tokens_for(LOCAL_PRINCIPAL)

        assert token.rules == () and token.compiled_rules() == []
        assert token.legacy_raw == entry
        assert "st-old was created before tokens held their own rules" in caplog.text
        assert scoped_tokens._to_json(token) == entry
        store_.rename(LOCAL_PRINCIPAL, token.id, "renamed")
        assert _tokens(tmp_path)["renamed"] == {**entry, "label": "renamed"}

    def test_a_legacy_entry_with_rule_ids_that_are_not_a_list_is_skipped(self, tmp_path):
        _write_entries(tmp_path, [{**_legacy("old"), "rule_ids": "r-1"}])
        store_ = _store()
        store_.load()

        assert store_.tokens_for(LOCAL_PRINCIPAL) == []

    def test_compiled_rules_of_a_copied_row_equal_its_compile_row(self):
        row = scope_rows.ScopeRow(
            id="r-folder", scope="drive.folder", value=["F1"], verbs=frozenset({Verb.READ}),
            operations=scope_rows.implied_operations("drive.folder", [Verb.READ]),
        )
        token, _ = _create(_store(), rule_ids=[])

        assert dataclasses.replace(token, rules=(row,)).compiled_rules() == scope_rows.compile_row(row)


class TestOwnLegacyRules:
    """Converting legacy tokens at start-up: each gets its own copy of the rules it named (ADR 0236)."""

    def test_the_file_variant_converts_a_legacy_entry_and_is_idempotent(self, tmp_path):
        _write_entries(tmp_path, [_legacy("old", rule_ids=["r-1", "r-gone"])])

        assert scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE]) == []

        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        assert data["version"] == 2
        (entry,) = data["tokens"]
        assert entry["rules"] == [scope_rows.row_to_dict(r) for r in scope_rows.rows_for_rules([_RULE], "r-1")]
        assert not set(scoped_tokens._LEGACY_KEYS) & set(entry)
        stamp = _stamp(tmp_path)
        assert scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE]) == []
        assert _stamp(tmp_path) == stamp

    def test_the_conversion_message_reaches_log(self, tmp_path):
        _write_entries(tmp_path, [_legacy("old", rule_ids=["r-1", "r-gone"])])
        messages = []

        scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE], log=messages.append)

        assert messages == [
            "Scoped token 'old' now holds its own copy of 1 rule(s); 1 rule(s) it named no longer existed",
        ]

    def test_unreadable_entries_are_kept_and_reported(self, tmp_path):
        _write_entries(tmp_path, [{"id": "broken"}, "nope", _legacy("old")])
        messages = []

        scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE], log=messages.append)

        tokens = json.loads(_file(tmp_path).read_text(encoding="utf-8"))["tokens"]
        assert tokens[:2] == [{"id": "broken"}, "nope"]
        assert "rules" in tokens[2]
        unreadable = f"Keeping an unreadable scoped token entry as it is ({_file(tmp_path)})"
        assert messages[:2] == [unreadable, unreadable]

    @pytest.mark.parametrize("limits", ["x", {"r-1": "x"}, {"r-1": [["only-one"]]}, {"r-1": [["p", 1]]}])
    def test_a_file_with_only_unreadable_legacy_entries_is_not_rewritten(self, tmp_path, limits):
        _write_entries(tmp_path, [_legacy("old", rule_limits=limits)])
        stamp = _stamp(tmp_path)
        messages = []

        assert scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE], log=messages.append) == []

        assert _stamp(tmp_path) == stamp
        assert messages == [f"Keeping an unreadable scoped token entry as it is ({_file(tmp_path)})"]

    def test_a_current_entry_is_kept_as_it_is(self, tmp_path):
        _create(_store(), label="new", rule_ids=["rule-a"])
        current = _tokens(tmp_path)["new"]
        data = json.loads(_file(tmp_path).read_text(encoding="utf-8"))
        data["tokens"].append(_legacy("old"))
        _file(tmp_path).write_text(json.dumps(data), encoding="utf-8")

        scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE])

        assert _tokens(tmp_path)["new"] == current

    def test_a_missing_or_unreadable_file_returns_nothing(self, tmp_path):
        path = tmp_path / "nowhere" / STORE_FILE_NAME
        assert scoped_tokens.own_rules_in_file(path, lambda: [_RULE]) == []
        assert not path.exists()
        path = tmp_path / STORE_FILE_NAME
        path.write_text("{not json", encoding="utf-8")
        assert scoped_tokens.own_rules_in_file(path, lambda: [_RULE]) == []
        assert path.read_text(encoding="utf-8") == "{not json"

    def test_compiled_is_not_called_when_no_token_is_legacy(self, tmp_path):
        _create(_store())
        stamp = _stamp(tmp_path)

        def never():
            raise AssertionError("compiled was called")

        assert scoped_tokens.own_rules_in_file(_file(tmp_path), never) == []
        assert _store().own_legacy_rules(LOCAL_PRINCIPAL, never) == []
        assert _stamp(tmp_path) == stamp

    def test_newly_flagged_labels_are_returned(self, tmp_path):
        _write_entries(tmp_path, [
            _legacy("plain", rule_ids=["r-2"]), _legacy("pii"), _legacy("already", allow_personal_data=True),
        ])

        labels = scoped_tokens.own_rules_in_file(_file(tmp_path), lambda: [_RULE], frozenset({"r-1"}))

        assert labels == ["pii"]
        tokens = _tokens(tmp_path)
        assert tokens["pii"]["allow_personal_data"] is True
        assert "allow_personal_data" not in tokens["plain"]

    def test_the_store_variant_converts_and_the_token_auto_accepts_through_its_own_row(self, tmp_path):
        from privacyfence import gate

        _write_entries(tmp_path, [_legacy("old", secret=TOKEN_PREFIX + "s3cret")])
        store_ = _store()
        store_.load()
        current, _ = _create(store_, label="current", rule_ids=["rule-b"])
        assert store_.find(TOKEN_PREFIX + "s3cret", NOW).rules == ()
        messages = []

        labels = store_.own_legacy_rules(LOCAL_PRINCIPAL, lambda: [_RULE], frozenset({"r-1"}), log=messages.append)

        assert labels == ["old"]
        assert messages == [
            "Scoped token 'old' now holds its own copy of 1 rule(s); 0 rule(s) it named no longer existed",
        ]
        token = store_.find(TOKEN_PREFIX + "s3cret", NOW)
        assert token.legacy_raw is None and token.allow_personal_data is True
        assert gate._rules_in_scope(token) == [_RULE]
        assert json.loads(_file(tmp_path).read_text(encoding="utf-8"))["version"] == 2
        assert store_.get("local", current.id) == current  # a current token is kept as it is
        assert store_.own_legacy_rules(LOCAL_PRINCIPAL, lambda: [_RULE]) == []  # idempotent

    def test_the_store_variant_refreshes_and_holds_the_lock(self, tmp_path):
        one, two = _store(), _store()  # two handles on one file, like two processes
        one.load()
        _write_entries(tmp_path, [_legacy("old")])
        seen = []

        def compiled():
            seen.append(one._lock.locked())
            return [_RULE]

        one.own_legacy_rules(LOCAL_PRINCIPAL, compiled)

        assert seen == [True]
        (token,) = two.tokens_for(LOCAL_PRINCIPAL)
        assert token.compiled_rules() == [_RULE]

    def test_the_store_variant_keeps_a_token_it_cannot_convert(self, tmp_path):
        _write_entries(tmp_path, [_legacy("old", rule_limits={"r-1": [["gmail.anything", "gmail.list_messages"]]})])
        store_ = _store()
        store_.load()
        (token,) = store_.tokens_for(LOCAL_PRINCIPAL)
        _write_entries(tmp_path, [_legacy("old", rule_limits="broken")])  # unreadable limits
        store_._tokens["local"] = [dataclasses.replace(token, legacy_raw=_legacy("old", rule_limits="broken"))]
        store_._stamps["local"] = _stamp(tmp_path)
        stamp = _stamp(tmp_path)
        messages = []

        assert store_.own_legacy_rules(LOCAL_PRINCIPAL, lambda: [_RULE], log=messages.append) == []

        assert messages == ["Keeping an unreadable scoped token entry as it is (local)"]
        assert _stamp(tmp_path) == stamp
        assert store_.tokens_for(LOCAL_PRINCIPAL)[0].legacy_raw is not None

    def test_the_module_function_uses_the_live_store_when_set(self, tmp_path, monkeypatch):
        _write_entries(tmp_path, [_legacy("old")])
        scoped_tokens.set_store(None)
        assert scoped_tokens.own_legacy_rules(LOCAL_PRINCIPAL, lambda: [_RULE]) == []  # no store: edits the file
        assert "rules" in _tokens(tmp_path)["old"]

        live = _store()
        scoped_tokens.set_store(live)
        try:
            monkeypatch.setattr(live, "own_legacy_rules", lambda p, c, ids, *, log: ["via-store"])
            assert scoped_tokens.own_legacy_rules(LOCAL_PRINCIPAL, lambda: [_RULE]) == ["via-store"]
        finally:
            scoped_tokens.set_store(None)

    def test_a_principal_with_no_token_file_writes_nothing(self, tmp_path):
        alice = Principal(id="alice")

        assert scoped_tokens.own_legacy_rules(alice, lambda: [_RULE]) == []
        assert _store(alice).own_legacy_rules(alice, lambda: [_RULE]) == []
        assert not (paths.authority_dir(alice) / STORE_FILE_NAME).exists()


class TestOwnedEntry:
    """``owned_entry``: a legacy token entry becomes one holding its own copy of its rules (ADR 0236)."""

    @staticmethod
    def _rule(rule_id="r-1", predicate="approved_folder", operations=("drive.read_file_contents",), value=("F1",)):
        return PolicyRule(
            id=rule_id, predicate=predicate, value=list(value), operations=frozenset(operations),
        )

    @staticmethod
    def _raw(rule_ids, **extra):
        return {"id": "st-1", "label": "job", "rule_ids": rule_ids, "tools": [], **extra}

    def test_an_entry_with_rules_gives_none(self):
        assert scoped_tokens.owned_entry({"id": "st-1", "rules": []}, [self._rule()]) is None

    def test_rule_ids_are_copied_as_rows_with_their_own_ids(self):
        row = scope_rows.ScopeRow(
            id="r-1", scope="drive.folder", value=["F1"], verbs=frozenset({Verb.READ}),
            operations=scope_rows.implied_operations("drive.folder", [Verb.READ]),
        )
        compiled = scope_rows.compile_row(row)

        owned = scoped_tokens.owned_entry(self._raw(["r-1"]), compiled)

        assert owned.copied == 1 and owned.missing == 0 and owned.newly_flagged is False
        assert owned.entry["rules"] == [scope_rows.row_to_dict(row)]

    def test_a_rule_limit_narrows_the_copy_to_its_pairs(self):
        rule = self._rule(operations=("drive.read_file_contents", "drive.download_file"))
        raw = self._raw(
            ["r-1"], rule_limits={"r-1": [["approved_folder", "drive.download_file"]]}, limits_split_verbs=True,
        )

        owned = scoped_tokens.owned_entry(raw, [rule])

        rows = [scope_rows.row_from_dict(d) for d in owned.entry["rules"]]
        assert {op for r in scope_rows.compile_rows(rows) for op in r.operations} == {"drive.download_file"}

    def test_an_id_with_no_compiled_rule_is_missing_and_adds_no_row(self):
        owned = scoped_tokens.owned_entry(self._raw(["r-gone", "r-1"]), [self._rule()])

        assert (owned.copied, owned.missing) == (1, 1)
        assert {d["id"] for d in owned.entry["rules"]} == {"r-1"}

    def test_a_limit_that_leaves_nothing_counts_as_missing(self):
        raw = self._raw(["r-1"], rule_limits={"r-1": [["approved_folder", "drive.other"]]}, limits_split_verbs=True)

        owned = scoped_tokens.owned_entry(raw, [self._rule()])

        assert (owned.copied, owned.missing) == (0, 1)
        assert owned.entry["rules"] == []

    def test_a_token_without_limits_split_verbs_gets_the_limit_successor(self):
        rule = PolicyRule(
            id="m1", predicate="slack.mentions_of_me_all_results", value=None,
            operations=frozenset({"slack.read_messages"}),
        )
        raw = self._raw(["m1"], rule_limits={"m1": [list(MENTIONS_PAIR)]})

        old = scoped_tokens.owned_entry(raw, [rule])
        split = scoped_tokens.owned_entry({**raw, "limits_split_verbs": True}, [rule])

        assert old.copied == 1
        assert split.copied == 0 and split.missing == 1

    def test_personal_data_ids_set_the_flag_and_newly_flagged(self):
        owned = scoped_tokens.owned_entry(self._raw(["r-1"]), [self._rule()], frozenset({"r-1"}))

        assert owned.newly_flagged is True
        assert owned.entry["allow_personal_data"] is True

    def test_an_entry_that_already_has_the_flag_is_not_newly_flagged(self):
        raw = self._raw(["r-1"], allow_personal_data=True)

        owned = scoped_tokens.owned_entry(raw, [self._rule()], frozenset({"r-1"}))

        assert owned.newly_flagged is False
        assert owned.entry["allow_personal_data"] is True

    def test_the_output_entry_has_none_of_the_legacy_keys(self):
        raw = self._raw(["r-1"], rule_limits={}, limits_split_verbs=True)

        owned = scoped_tokens.owned_entry(raw, [self._rule()])

        assert not set(scoped_tokens._LEGACY_KEYS) & set(owned.entry)
        assert owned.entry["label"] == "job"

    @pytest.mark.parametrize("raw", ["nope", {"id": "st-1"}, {"id": "st-1", "rule_ids": "r-1"}, {"rule_ids": [1]}])
    def test_an_unreadable_entry_raises(self, raw):
        with pytest.raises((KeyError, TypeError, ValueError)):
            scoped_tokens.owned_entry(raw, [self._rule()])


def _activity_file(tmp_path):
    return tmp_path / "authority" / ACTIVITY_FILE_NAME


def _stored_activity(tmp_path):
    return json.loads(_activity_file(tmp_path).read_text())


class TestTokenActivity:
    def test_first_note_use_writes_the_file(self, tmp_path):
        store = _store()
        token, _ = _create(store)
        store.note_use(token, now=NOW)
        assert _stored_activity(tmp_path) == {
            "version": 1,
            "tokens": {token.id: {"last_used_at": NOW, "last_denied_at": None, "last_denied_tool": ""}},
        }

    def test_second_use_within_a_minute_does_not_rewrite(self, tmp_path):
        store = _store()
        token, _ = _create(store)
        store.note_use(token, now=NOW)
        store.note_use(token, now=NOW + 30)
        assert _stored_activity(tmp_path)["tokens"][token.id]["last_used_at"] == NOW
        assert store.activity_for(LOCAL_PRINCIPAL)[token.id].last_used_at == NOW + 30

    def test_use_after_a_minute_rewrites(self, tmp_path):
        store = _store()
        token, _ = _create(store)
        store.note_use(token, now=NOW)
        store.note_use(token, now=NOW + 61)
        assert _stored_activity(tmp_path)["tokens"][token.id]["last_used_at"] == NOW + 61

    def test_denial_within_a_minute_stays_in_memory(self, tmp_path):
        store = _store()
        token, _ = _create(store)
        store.note_use(token, now=NOW)
        store.note_denial(token, "drive_list_files", now=NOW + 5)
        assert _stored_activity(tmp_path)["tokens"][token.id]["last_denied_at"] is None
        assert store.activity_for(LOCAL_PRINCIPAL)[token.id] == TokenActivity(NOW, NOW + 5, "drive_list_files")

    def test_denied_tool_travels_with_the_denial_time(self):
        store = _store()
        token, _ = _create(store)
        store.note_denial(token, "drive_list_files", now=NOW)
        store.note_denial(token, "gmail_send", now=NOW + 1)
        assert store.activity_for(LOCAL_PRINCIPAL)[token.id] == TokenActivity(None, NOW + 1, "gmail_send")

    def test_first_write_after_a_restart_keeps_other_tokens_stored_values(self, tmp_path):
        first = _store()
        token_a, _ = _create(first, label="a")
        token_b, _ = _create(first, label="b")
        first.note_denial(token_b, "drive_list_files", now=NOW)
        restarted = _store()
        restarted.note_use(token_a, now=NOW + 500)
        stored = _stored_activity(tmp_path)["tokens"]
        assert stored[token_b.id] == {
            "last_used_at": None, "last_denied_at": NOW, "last_denied_tool": "drive_list_files",
        }
        assert stored[token_a.id]["last_used_at"] == NOW + 500

    def test_loading_keeps_the_newer_value_of_file_and_memory(self, tmp_path):
        first = _store()
        token, _ = _create(first)
        first.note_denial(token, "stored_tool", now=NOW + 90)
        first.note_use(token, now=NOW + 100)
        other = _store()
        memory = {token.id: TokenActivity(NOW + 200, NOW + 10, "memory_tool")}
        other._activity[LOCAL_PRINCIPAL.id] = memory
        other._load_activity(LOCAL_PRINCIPAL)
        assert memory[token.id] == TokenActivity(NOW + 200, NOW + 90, "stored_tool")

    def test_loading_keeps_a_newer_denial_held_in_memory(self, tmp_path):
        first = _store()
        token, _ = _create(first)
        first.note_denial(token, "stored_tool", now=NOW + 10)
        other = _store()
        memory = {token.id: TokenActivity(None, NOW + 90, "memory_tool")}
        other._activity[LOCAL_PRINCIPAL.id] = memory
        other._load_activity(LOCAL_PRINCIPAL)
        assert memory[token.id] == TokenActivity(None, NOW + 90, "memory_tool")

    def test_activity_for_omits_tokens_that_are_gone(self):
        store = _store()
        token, _ = _create(store)
        kept, _ = _create(store, label="kept")
        store.note_use(token, now=NOW)
        store.note_use(kept, now=NOW)
        store.revoke(LOCAL_PRINCIPAL, token.id)
        assert set(store.activity_for(LOCAL_PRINCIPAL)) == {kept.id}

    def test_revoked_scope_is_never_recorded(self, tmp_path):
        store = _store()
        store.note_use(REVOKED_SCOPE, now=NOW)
        store.note_denial(REVOKED_SCOPE, "gmail_send", now=NOW)
        assert not _activity_file(tmp_path).exists()

    def test_a_write_failure_is_logged_not_raised(self, monkeypatch, caplog):
        store = _store()
        token, _ = _create(store)

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(scoped_tokens.secure_files, "atomic_write_json", boom)
        store.note_use(token, now=NOW)
        assert f"Could not record scoped token activity for {token.id}" in caplog.text
        assert store.activity_for(LOCAL_PRINCIPAL)[token.id].last_used_at == NOW

    def test_a_corrupt_file_reads_as_empty(self, tmp_path, caplog):
        store = _store()
        token, _ = _create(store)
        _activity_file(tmp_path).write_text("{not json")
        assert store.activity_for(LOCAL_PRINCIPAL) == {}
        store.note_use(token, now=NOW)
        assert "Skipping an unreadable scoped token activity file" in caplog.text
        assert store.activity_for(LOCAL_PRINCIPAL)[token.id] == TokenActivity(NOW)

    @pytest.mark.parametrize("entry", [
        {"last_used_at": "yesterday", "last_denied_at": None, "last_denied_tool": ""},
        {"last_used_at": None, "last_denied_at": None, "last_denied_tool": 5},
    ])
    def test_an_entry_of_the_wrong_type_reads_as_empty(self, tmp_path, entry):
        store = _store()
        token, _ = _create(store)
        _activity_file(tmp_path).write_text(json.dumps({"version": 1, "tokens": {token.id: entry}}))
        assert store.activity_for(LOCAL_PRINCIPAL) == {}

    def test_a_principal_that_cannot_name_a_directory_is_logged_not_raised(self, caplog):
        store = _store()
        token = scoped_tokens.replace(_create(store)[0], principal_id="../x")
        store.note_use(token, now=NOW)
        assert f"Could not record scoped token activity for {token.id}" in caplog.text
