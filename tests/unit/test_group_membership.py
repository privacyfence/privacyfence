"""Group membership store, resolvers and approvals (plan 3.5 and 3.7)."""
from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from privacyfence import group_membership as gm
from privacyfence.group_membership import (
    MembershipStore,
    SnapshotResolver,
    StaticFileResolver,
)

NOW = 1_000_000.0
MGMT = "mgmt@company"
EVERYONE = "everyone@company"
MAP = {MGMT: frozenset({"mgmt"}), EVERYONE: frozenset({"all"})}


@pytest.fixture
def store(tmp_path):
    return MembershipStore(tmp_path / gm.FILENAME)


def snap(store, groups, *, now=NOW, mapping=MAP, max_additions=20):
    return store.apply_snapshot(groups, group_audiences=mapping, now=now, max_additions=max_additions)


class TestSnapshot:
    def test_bootstrap_exempt_from_cap(self, store):
        people = [f"p{i}@x.example" for i in range(30)]
        result = snap(store, {EVERYONE: people}, max_additions=5)
        assert result.status == "applied"
        assert result.added == 30
        assert store.updated_at == NOW

    def test_bootstrap_additions_do_not_count_toward_the_window(self, store):
        snap(store, {EVERYONE: [f"p{i}@x.example" for i in range(30)]}, max_additions=5)
        result = snap(store, {EVERYONE: [f"p{i}@x.example" for i in range(32)]}, now=NOW + 60, max_additions=5)
        assert result.status == "applied"
        assert result.added == 2

    def test_addition_to_all_group_applies_at_once(self, store):
        snap(store, {EVERYONE: ["a@x.example"]})
        result = snap(store, {EVERYONE: ["a@x.example", "B@X.example"]}, now=NOW + 1)
        assert (result.status, result.added, result.parked) == ("applied", 1, 0)
        assert SnapshotResolver(store).audiences_for("b@x.example", MAP) == {"all"}

    def test_addition_to_restricted_group_is_parked_until_approved(self, store):
        snap(store, {EVERYONE: ["a@x.example"]})
        result = snap(store, {MGMT: ["m@x.example"]}, now=NOW + 1)
        assert (result.added, result.parked) == (1, 1)
        resolver = SnapshotResolver(store)
        assert resolver.audiences_for("m@x.example", MAP) == frozenset()
        assert store.pending(MAP) == [gm.Pending(MGMT, "mgmt", ("m@x.example",))]
        assert store.approve(MGMT, "mgmt", "m@x.example", group_audiences=MAP, now=NOW + 2) == 1
        assert resolver.audiences_for("m@x.example", MAP) == {"mgmt"}
        assert store.pending(MAP) == []

    def test_removal_applies_at_once_and_drops_the_approval(self, store):
        snap(store, {MGMT: ["m@x.example"]})
        store.approve(MGMT, "mgmt", None, group_audiences=MAP, now=NOW)
        resolver = SnapshotResolver(store)
        assert resolver.audiences_for("m@x.example", MAP) == {"mgmt"}
        result = snap(store, {MGMT: []}, now=NOW + 1)
        assert result.removed == 1
        assert resolver.audiences_for("m@x.example", MAP) == frozenset()
        assert not store.has_approval("m@x.example", MGMT, "mgmt")

    def test_missing_group_removes_every_member(self, store):
        snap(store, {MGMT: ["a@x.example", "b@x.example"]})
        result = snap(store, {}, now=NOW + 1)
        assert result.removed == 2
        assert store.members_of("a@x.example") == frozenset()

    def test_over_cap_within_24h_rejects_the_whole_snapshot(self, store):
        snap(store, {EVERYONE: ["a@x.example"]})
        snap(store, {EVERYONE: ["a@x.example", "b@x.example"]}, now=NOW + 10, max_additions=2)
        before = json.loads(store._path.read_text())
        result = snap(
            store,
            {EVERYONE: ["a@x.example", "b@x.example", "c@x.example", "d@x.example"], MGMT: ["z@x.example"]},
            now=NOW + 20,
            max_additions=2,
        )
        assert (result.status, result.reason) == ("rejected", "too_many_additions")
        after = json.loads(store._path.read_text())
        assert {k: v for k, v in after.items() if k != "last_rejected"} == {
            k: v for k, v in before.items() if k != "last_rejected"
        }
        assert store.last_rejected() == {"reason": "too_many_additions", "at": NOW + 20}

    def test_window_expires_after_24h(self, store):
        snap(store, {EVERYONE: ["a@x.example"]})
        snap(store, {EVERYONE: ["a@x.example", "b@x.example", "c@x.example"]}, now=NOW + 10, max_additions=2)
        later = NOW + 10 + gm.WINDOW_SECONDS + 1
        result = snap(
            store, {EVERYONE: ["a@x.example", "b@x.example", "c@x.example", "d@x.example", "e@x.example"]},
            now=later, max_additions=2,
        )
        assert result.status == "applied"

    def test_unmapped_group_is_ignored_and_not_stored(self, store):
        result = snap(store, {EVERYONE: ["a@x.example"], "other@company": ["z@x.example"]})
        assert result.ignored_groups == ("other@company",)
        assert "other@company" not in json.loads(store._path.read_text())["members"]

    def test_mapping_a_group_later_shows_it_as_additions(self, store):
        groups = {EVERYONE: ["a@x.example"], "finance@company": ["f@x.example"]}
        snap(store, groups)
        mapped = {**MAP, "finance@company": frozenset({"finance"})}
        result = snap(store, groups, now=NOW + 1, mapping=mapped)
        assert result.added == 1
        assert result.ignored_groups == ()
        assert store.pending(mapped) == [gm.Pending("finance@company", "finance", ("f@x.example",))]

    def test_pending_person_absent_from_next_snapshot_is_no_longer_pending(self, store):
        snap(store, {MGMT: ["a@x.example", "b@x.example"]})
        snap(store, {MGMT: ["a@x.example"]}, now=NOW + 1)
        assert store.pending(MAP) == [gm.Pending(MGMT, "mgmt", ("a@x.example",))]

    def test_approval_does_not_survive_leaving_and_rejoining(self, store):
        snap(store, {MGMT: ["a@x.example"]})
        store.approve(MGMT, "mgmt", "a@x.example", group_audiences=MAP, now=NOW)
        snap(store, {MGMT: []}, now=NOW + 1)
        snap(store, {MGMT: ["a@x.example"]}, now=NOW + 2)
        assert SnapshotResolver(store).audiences_for("a@x.example", MAP) == frozenset()
        assert store.pending(MAP) == [gm.Pending(MGMT, "mgmt", ("a@x.example",))]

    def test_approval_for_one_audience_does_not_grant_a_later_one(self, store):
        snap(store, {MGMT: ["a@x.example"]})
        store.approve(MGMT, "mgmt", "a@x.example", group_audiences=MAP, now=NOW)
        wider = {MGMT: frozenset({"mgmt", "finance"})}
        assert SnapshotResolver(store).audiences_for("a@x.example", wider) == {"mgmt"}
        assert store.pending(wider) == [gm.Pending(MGMT, "finance", ("a@x.example",))]

    def test_more_than_500_pending_rejects(self, store):
        snap(store, {EVERYONE: ["a@x.example"]})
        people = [f"p{i}@x.example" for i in range(gm.MAX_PENDING + 1)]
        result = snap(store, {MGMT: people}, now=NOW + 1, max_additions=10_000)
        assert (result.status, result.reason) == ("rejected", "too_many_pending")
        assert store.pending(MAP) == []

    @pytest.mark.parametrize(
        "groups",
        [
            {MGMT: ["no-at-sign"]},
            {MGMT: ["a@b@c"]},
            {MGMT: ["a b@x.example"]},
            {MGMT: "a@x.example"},
            {MGMT: [None]},
            ["not", "a", "mapping"],
            {f"g{i}@company": [] for i in range(gm.MAX_GROUPS + 1)},
        ],
    )
    def test_malformed_input_is_invalid(self, store, groups):
        result = snap(store, groups)
        assert (result.status, result.reason) == ("rejected", "invalid_snapshot")
        assert store.updated_at is None

    def test_too_many_entries_is_invalid(self, store, monkeypatch):
        monkeypatch.setattr(gm, "MAX_ENTRIES", 3)
        result = snap(store, {MGMT: [f"p{i}@x.example" for i in range(4)]})
        assert result.reason == "invalid_snapshot"

    def test_rejected_first_snapshot_keeps_the_bootstrap_exemption(self, store):
        snap(store, {MGMT: ["bad"]})
        assert snap(store, {EVERYONE: [f"p{i}@x.example" for i in range(30)]}, max_additions=1).status == "applied"

    def test_state_persists_in_a_0600_file_and_reloads(self, store, tmp_path):
        snap(store, {MGMT: ["a@x.example"]})
        assert stat.S_IMODE(os.stat(store._path).st_mode) == 0o600
        reloaded = MembershipStore(store._path)
        assert reloaded.members_of("A@x.example") == {MGMT}
        assert reloaded.updated_at == NOW


class TestApprovals:
    def test_approve_all_pending_is_per_group_and_audience(self, store):
        mapping = {MGMT: frozenset({"mgmt"}), "ops@company": frozenset({"mgmt"})}
        snap(store, {MGMT: ["a@x.example", "b@x.example"], "ops@company": ["c@x.example"]}, mapping=mapping)
        assert store.approve(MGMT, "mgmt", group_audiences=mapping, now=NOW) == 2
        assert store.pending(mapping) == [gm.Pending("ops@company", "mgmt", ("c@x.example",))]

    def test_decline_removes_from_pending_and_is_forgotten_on_leaving(self, store):
        snap(store, {MGMT: ["a@x.example"]})
        assert store.decline(MGMT, "mgmt", "a@x.example", group_audiences=MAP, now=NOW) == 1
        assert store.pending(MAP) == []
        assert SnapshotResolver(store).audiences_for("a@x.example", MAP) == frozenset()
        snap(store, {MGMT: []}, now=NOW + 1)
        snap(store, {MGMT: ["a@x.example"]}, now=NOW + 2)
        assert store.pending(MAP) == [gm.Pending(MGMT, "mgmt", ("a@x.example",))]

    def test_approve_after_decline_flips_it(self, store):
        snap(store, {MGMT: ["a@x.example"]})
        store.decline(MGMT, "mgmt", "a@x.example", group_audiences=MAP, now=NOW)
        assert store.approve(MGMT, "mgmt", "a@x.example", group_audiences=MAP, now=NOW) == 1
        assert SnapshotResolver(store).audiences_for("a@x.example", MAP) == {"mgmt"}

    def test_only_current_members_and_named_audiences(self, store):
        snap(store, {MGMT: ["a@x.example"]})
        assert store.approve(MGMT, "mgmt", "stranger@x.example", group_audiences=MAP, now=NOW) == 0
        assert store.approve(MGMT, "finance", "a@x.example", group_audiences=MAP, now=NOW) == 0
        assert store.approve(EVERYONE, "all", "a@x.example", group_audiences=MAP, now=NOW) == 0
        assert store.approve(MGMT, "mgmt", "a@x.example", group_audiences=MAP, now=NOW) == 1
        assert store.approve(MGMT, "mgmt", "a@x.example", group_audiences=MAP, now=NOW) == 0

    def test_resolver_requires_the_current_map(self, store):
        snap(store, {MGMT: ["a@x.example"]})
        store.approve(MGMT, "mgmt", None, group_audiences=MAP, now=NOW)
        assert SnapshotResolver(store).audiences_for("a@x.example", {}) == frozenset()


class TestResolvers:
    def test_empty_email_gets_nothing(self, store, tmp_path):
        snap(store, {EVERYONE: ["a@x.example"]})
        assert SnapshotResolver(store).audiences_for("", MAP) == frozenset()
        static = tmp_path / "static.json"
        static.write_text(json.dumps({"version": 1, "groups": {EVERYONE: [""]}}))
        assert StaticFileResolver(static).audiences_for("", MAP) == frozenset()

    def test_static_grants_every_audience_without_approval(self, tmp_path):
        path = tmp_path / "static.json"
        path.write_text(json.dumps({"version": 1, "groups": {MGMT: ["A@x.example"], EVERYONE: ["a@x.example"]}}))
        resolver = StaticFileResolver(path)
        assert resolver.audiences_for("a@x.example", MAP) == {"mgmt", "all"}
        assert resolver.audiences_for("b@x.example", MAP) == frozenset()

    def test_static_reloads_when_the_file_changes(self, tmp_path):
        path = tmp_path / "static.json"
        path.write_text(json.dumps({"version": 1, "groups": {MGMT: ["a@x.example"]}}))
        resolver = StaticFileResolver(path)
        assert resolver.audiences_for("b@x.example", MAP) == frozenset()
        path.write_text(json.dumps({"version": 1, "groups": {MGMT: ["a@x.example", "b@x.example"]}}))
        os.utime(path, ns=(1, 2_000_000_000))
        assert resolver.audiences_for("b@x.example", MAP) == {"mgmt"}

    def test_static_keeps_last_good_data_on_a_malformed_file(self, tmp_path, caplog):
        path = tmp_path / "static.json"
        path.write_text(json.dumps({"version": 1, "groups": {MGMT: ["a@x.example"]}}))
        resolver = StaticFileResolver(path)
        assert resolver.audiences_for("a@x.example", MAP) == {"mgmt"}
        path.write_text("{not json")
        os.utime(path, ns=(1, 3_000_000_000))
        with caplog.at_level(logging.WARNING):
            assert resolver.audiences_for("a@x.example", MAP) == {"mgmt"}
        assert "malformed" in caplog.text

    def test_static_missing_file_grants_nothing(self, tmp_path):
        assert StaticFileResolver(tmp_path / "nope.json").audiences_for("a@x.example", MAP) == frozenset()


class TestSingleton:
    def test_set_and_get_store(self, tmp_path):
        custom = MembershipStore(tmp_path / gm.FILENAME)
        gm.set_store(custom)
        assert gm.get_store() is custom
        assert gm.current_store() is custom
        gm.set_store(None)
        assert gm.current_store() is None

    def test_name_patterns(self):
        assert gm.GROUP_NAME_RE.fullmatch("mgmt@company")
        assert not gm.GROUP_NAME_RE.fullmatch("Mgmt@company")
        assert gm.AUDIENCE_RE.fullmatch("mgmt-2")
        assert not gm.AUDIENCE_RE.fullmatch("-x")


class TestFileHandling:
    def test_malformed_file_keeps_last_good_state(self, store, caplog):
        snap(store, {MGMT: ["a@x.example"]})
        store._path.write_text("{broken")
        os.utime(store._path, ns=(1, 5_000_000_000))
        with caplog.at_level(logging.WARNING):
            assert store.members_of("a@x.example") == {MGMT}
        assert "unreadable" in caplog.text

    @pytest.mark.parametrize(
        "raw",
        [
            [],
            {"members": []},
            {"members": {"g": "x"}},
            {"approvals": {}},
            {"approvals": [["a", "b"]]},
            {"history": [1]},
        ],
    )
    def test_invalid_shapes_are_unreadable(self, tmp_path, raw):
        path = tmp_path / gm.FILENAME
        path.write_text(json.dumps(raw))
        assert MembershipStore(path).members_of("a@x.example") == frozenset()

    def test_another_writer_is_seen(self, store, tmp_path):
        other = MembershipStore(store._path)
        snap(other, {MGMT: ["a@x.example"]})
        assert store.members_of("a@x.example") == {MGMT}
        store._path.unlink()
        assert store.members_of("a@x.example") == frozenset()

    def test_blank_email_has_no_groups(self, store):
        assert store.members_of("  ") == frozenset()
