"""ListingGrants: what listings under a folder-tree rule's folder remember, in memory only."""

from __future__ import annotations

from privacyfence import drive_client, drive_listing_grants
from privacyfence.drive_listing_grants import GRANT_TTL_SECONDS, MAX_GRANTED_PER_ROOT, ListingGrants

ROOT = "R" * 12
OTHER_ROOT = "S" * 12
A = "A" * 12
B = "B" * 12
A1 = "C" * 12


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _grants():
    clock = Clock()
    return ListingGrants(clock), clock


def test_listing_the_root_grants_its_child_folders():
    grants, _ = _grants()
    grants.note_listing("k", [ROOT], ROOT, [A, B])
    assert grants.granted("k", [ROOT]) == {ROOT: sorted([A, B])}


def test_listing_an_unrelated_folder_grants_nothing():
    grants, _ = _grants()
    grants.note_listing("k", [ROOT], "Z" * 12, [A])
    assert grants.granted("k", [ROOT]) == {}


def test_a_grandchild_is_granted_after_its_parent_was_listed():
    grants, _ = _grants()
    grants.note_listing("k", [ROOT], ROOT, [A])
    grants.note_listing("k", [ROOT], A, [A1])
    assert grants.granted("k", [ROOT]) == {ROOT: sorted([A, A1])}


def test_a_grant_expires_after_the_ttl():
    grants, clock = _grants()
    grants.note_listing("k", [ROOT], ROOT, [A])
    clock.now = GRANT_TTL_SECONDS - 1
    assert grants.granted("k", [ROOT]) == {ROOT: [A]}
    clock.now = GRANT_TTL_SECONDS
    assert grants.granted("k", [ROOT]) == {}


def test_listing_the_root_again_refreshes_the_grant():
    grants, clock = _grants()
    grants.note_listing("k", [ROOT], ROOT, [A])
    clock.now = 3000
    grants.note_listing("k", [ROOT], ROOT, [A])
    clock.now = 3601
    assert grants.granted("k", [ROOT]) == {ROOT: [A]}


def test_a_child_never_outlives_the_grant_of_the_folder_listed_to_reach_it():
    grants, clock = _grants()
    grants.note_listing("k", [ROOT], ROOT, [A])
    clock.now = 3500
    grants.note_listing("k", [ROOT], A, [A1])
    clock.now = 3601
    assert grants.granted("k", [ROOT]) == {}


def test_a_relisted_child_does_not_outlive_its_parent():
    grants, clock = _grants()
    grants.note_listing("k", [ROOT], ROOT, [A])
    clock.now = 100
    grants.note_listing("k", [ROOT], A, [A1])
    clock.now = 3500
    grants.note_listing("k", [ROOT], A, [A1])
    clock.now = 3599
    assert grants.granted("k", [ROOT]) == {ROOT: sorted([A, A1])}
    clock.now = GRANT_TTL_SECONDS
    assert grants.granted("k", [ROOT]) == {}


def test_a_root_holds_at_most_the_cap_of_folders():
    grants, _ = _grants()
    children = [f"folder{i:08d}" for i in range(MAX_GRANTED_PER_ROOT + 1)]
    grants.note_listing("k", [ROOT], ROOT, children)
    granted = grants.granted("k", [ROOT])[ROOT]
    assert len(granted) == MAX_GRANTED_PER_ROOT
    assert children[-1] not in granted


def test_a_child_id_that_is_not_a_folder_id_is_ignored():
    grants, _ = _grants()
    grants.note_listing("k", [ROOT], ROOT, ["short", "not an id!" * 2, A])
    assert grants.granted("k", [ROOT]) == {ROOT: [A]}


def test_two_keys_do_not_see_each_others_grants():
    grants, _ = _grants()
    grants.note_listing("k1", [ROOT], ROOT, [A])
    assert grants.granted("k2", [ROOT]) == {}
    assert grants.granted("k1", [ROOT]) == {ROOT: [A]}


def test_two_roots_do_not_see_each_others_grants():
    grants, _ = _grants()
    grants.note_listing("k", [ROOT, OTHER_ROOT], ROOT, [A])
    assert grants.granted("k", [ROOT, OTHER_ROOT]) == {ROOT: [A]}
    assert grants.granted("k", [OTHER_ROOT]) == {}


def test_granted_omits_a_root_with_no_live_folder():
    grants, _ = _grants()
    assert grants.granted("k", [ROOT]) == {}


def test_a_listed_id_with_query_syntax_grants_nothing():
    grants, _ = _grants()
    grants.note_listing("k", [ROOT], f"{ROOT}' in parents or 'x", [A])
    assert grants.granted("k", [ROOT]) == {}


def test_constants_match_the_drive_client():
    assert drive_listing_grants.FOLDER_MIME == drive_client._FOLDER_MIME
    assert drive_listing_grants._ID_RE.pattern == drive_client._FOLDER_ID_RE.pattern
