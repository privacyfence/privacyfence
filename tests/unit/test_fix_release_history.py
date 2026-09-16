"""Tests for scripts/fix_release_history.py.

Imported by file path (importlib) -- same pattern as tests/unit/test_update_branch_protection.py,
since scripts/ isn't part of the installed `privacyfence` distribution. Every GitHub API call is
mocked; these tests never make a real network request and never require GITHUB_TOKEN to be set
for anything other than the "missing token" case. `git grep` runs for real against a temporary
repo, not this checkout, so the tree-reference check is exercised without depending on this repo's
actual history.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "fix_release_history.py"
_spec = importlib.util.spec_from_file_location("fix_release_history", _SCRIPT_PATH)
frh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frh)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests")


def _fake_response(*, status_code=200, json_body=None, links=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body if json_body is not None else {}
    response.links = links or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = frh.requests.HTTPError(f"{status_code} error")
    else:
        response.raise_for_status.return_value = None
    return response


def _release(tag_name, *, rid=1, body="", prerelease=True):
    return {"id": rid, "tag_name": tag_name, "body": body, "prerelease": prerelease}


@pytest.fixture
def empty_tree(tmp_path, monkeypatch):
    """A throwaway git repo with nothing in it, so tree_references() has something real to grep
    against without touching this checkout's own history."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setattr(frh, "REPO_ROOT", tmp_path)
    return tmp_path


class TestListReleases:
    def test_follows_pagination_link(self, monkeypatch):
        calls = []

        def _get(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return _fake_response(json_body=[_release("v1")], links={"next": {"url": "page2"}})
            return _fake_response(json_body=[_release("v2")])

        monkeypatch.setattr(frh.requests, "get", _get)
        releases = frh.list_releases("o", "r")
        assert [r["tag_name"] for r in releases] == ["v1", "v2"]
        assert calls == [f"{frh.API_ROOT}/repos/o/r/releases?per_page=100", "page2"]

    def test_raises_on_error_status(self, monkeypatch):
        monkeypatch.setattr(frh.requests, "get", lambda *a, **kw: _fake_response(status_code=500))
        with pytest.raises(frh.requests.HTTPError):
            frh.list_releases("o", "r")


class TestFindReleaseByTag:
    def test_finds_matching_tag(self):
        releases = [_release("v1"), _release("v2", rid=2)]
        assert frh.find_release_by_tag(releases, "v2")["id"] == 2

    def test_returns_none_when_absent(self):
        assert frh.find_release_by_tag([_release("v1")], "v4.0.0-apha2") is None


class TestReleasesLinkingOldFork:
    def test_matches_body_containing_old_fork(self):
        releases = [
            _release("v1", body=f"see https://github.com/{frh.OLD_FORK_SLUG}/pull/1"),
            _release("v2", body="see https://github.com/privacyfence/privacyfence/pull/2"),
        ]
        assert [r["tag_name"] for r in frh.releases_linking_old_fork(releases)] == ["v1"]

    def test_tolerates_missing_body(self):
        assert frh.releases_linking_old_fork([{"id": 1, "tag_name": "v1", "body": None}]) == []


class TestRewriteBody:
    def test_replaces_old_fork_with_new_org(self):
        body = f"Full Changelog: https://github.com/{frh.OLD_FORK_SLUG}/compare/v1...v2"
        assert frh.OLD_FORK_SLUG not in frh.rewrite_body(body)
        assert frh.NEW_ORG_SLUG in frh.rewrite_body(body)


class TestMisflaggedPrereleases:
    @pytest.mark.parametrize(
        "tag",
        ["v4.0.0-alpha1", "v4.0.0a11", "v4.0.0b1", "v4.0.0-beta3", "v4.2.0rc1"],
    )
    def test_flags_prerelease_shaped_tag_marked_stable(self, tag):
        releases = [_release(tag, prerelease=False)]
        assert [r["tag_name"] for r in frh.misflagged_prereleases(releases)] == [tag]

    def test_ignores_correctly_flagged_prerelease(self):
        releases = [_release("v4.0.0-alpha1", prerelease=True)]
        assert frh.misflagged_prereleases(releases) == []

    def test_ignores_a_genuine_stable_release(self):
        releases = [_release("v3.4.7", prerelease=False)]
        assert frh.misflagged_prereleases(releases) == []

    def test_does_not_flag_the_typo_tag(self):
        """v4.0.0-apha2 doesn't match the alpha/beta/rc pattern at all -- it's handled as the
        literal TYPO_TAG, not folded into this generic check."""
        releases = [_release("v4.0.0-apha2", prerelease=False)]
        assert frh.misflagged_prereleases(releases) == []


class TestTreeReferences:
    def test_no_matches_returns_empty(self, empty_tree):
        (empty_tree / "README.md").write_text("nothing interesting here\n")
        subprocess.run(["git", "add", "."], cwd=empty_tree, check=True)
        assert frh.tree_references(frh.TYPO_TAG, root=empty_tree) == []

    def test_finds_a_tracked_reference(self, empty_tree):
        (empty_tree / "notes.md").write_text(f"see {frh.TYPO_TAG}\n")
        subprocess.run(["git", "add", "."], cwd=empty_tree, check=True)
        assert frh.tree_references(frh.TYPO_TAG, root=empty_tree) == ["notes.md"]

    def test_ignores_an_untracked_file(self, empty_tree):
        (empty_tree / "scratch.md").write_text(f"see {frh.TYPO_TAG}\n")
        assert frh.tree_references(frh.TYPO_TAG, root=empty_tree) == []


class TestDeleteReleaseAndTag:
    def test_deletes_release_then_tag_ref(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            frh.requests, "delete", lambda url, **kw: calls.append(url) or _fake_response(status_code=204)
        )
        frh.delete_release_and_tag("o", "r", _release(frh.TYPO_TAG, rid=42))
        assert calls == [
            f"{frh.API_ROOT}/repos/o/r/releases/42",
            f"{frh.API_ROOT}/repos/o/r/git/refs/tags/{frh.TYPO_TAG}",
        ]

    def test_tolerates_an_already_deleted_tag_ref(self, monkeypatch):
        def _delete(url, **kw):
            if "/git/refs/" in url:
                return _fake_response(status_code=404)
            return _fake_response(status_code=204)

        monkeypatch.setattr(frh.requests, "delete", _delete)
        frh.delete_release_and_tag("o", "r", _release(frh.TYPO_TAG, rid=42))  # must not raise

    def test_raises_if_the_release_delete_fails(self, monkeypatch):
        monkeypatch.setattr(frh.requests, "delete", lambda *a, **kw: _fake_response(status_code=500))
        with pytest.raises(frh.requests.HTTPError):
            frh.delete_release_and_tag("o", "r", _release(frh.TYPO_TAG, rid=42))


class TestUpdateReleaseBody:
    def test_patches_the_body(self, monkeypatch):
        captured = {}

        def _patch(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return _fake_response()

        monkeypatch.setattr(frh.requests, "patch", _patch)
        frh.update_release_body("o", "r", 7, "new body")
        assert captured == {"url": f"{frh.API_ROOT}/repos/o/r/releases/7", "json": {"body": "new body"}}


class TestSetPrerelease:
    def test_patches_the_flag(self, monkeypatch):
        captured = {}

        def _patch(url, **kwargs):
            captured["json"] = kwargs["json"]
            return _fake_response()

        monkeypatch.setattr(frh.requests, "patch", _patch)
        frh.set_prerelease("o", "r", 7, prerelease=True)
        assert captured["json"] == {"prerelease": True}


class TestMainShow:
    def test_reports_a_clean_state(self, monkeypatch, capsys):
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: [_release("v1")])
        assert frh.main(["show"]) == 0
        out = capsys.readouterr().out
        assert "not found (already deleted)" in out
        assert f"no release bodies link {frh.OLD_FORK_SLUG}" in out
        assert "no releases are mis-flagged" in out

    def test_reports_the_typo_release(self, monkeypatch, capsys):
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: [_release(frh.TYPO_TAG, rid=99)])
        monkeypatch.setattr(frh, "tree_references", lambda *a, **kw: [])
        assert frh.main(["show"]) == 0
        assert f"typo release: {frh.TYPO_TAG} (id 99)" in capsys.readouterr().out

    def test_reports_a_tree_reference_blocking_deletion(self, monkeypatch, capsys):
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: [_release(frh.TYPO_TAG, rid=99)])
        monkeypatch.setattr(frh, "tree_references", lambda *a, **kw: ["docs/notes.md"])
        assert frh.main(["show"]) == 0
        assert "refuses to delete: referenced in docs/notes.md" in capsys.readouterr().out

    def test_reports_fork_linked_and_misflagged_releases(self, monkeypatch, capsys):
        releases = [
            _release("v4.0.0a12", body=f"see {frh.OLD_FORK_SLUG}"),
            _release("v4.0.0a13", prerelease=False),
        ]
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: releases)
        assert frh.main(["show"]) == 0
        out = capsys.readouterr().out
        assert "v4.0.0a12" in out
        assert "v4.0.0a13" in out


class TestMainApply:
    def test_dry_run_makes_no_writes(self, monkeypatch, capsys):
        releases = [
            _release(frh.TYPO_TAG, rid=99),
            _release("v4.0.0a12", rid=1, body=f"see {frh.OLD_FORK_SLUG}"),
            _release("v4.0.0a13", rid=2, prerelease=False),
        ]
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: releases)
        monkeypatch.setattr(frh, "tree_references", lambda *a, **kw: [])

        def _fail_if_called(*a, **kw):
            raise AssertionError("no mutating call should run in --dry-run mode")

        monkeypatch.setattr(frh, "delete_release_and_tag", _fail_if_called)
        monkeypatch.setattr(frh, "update_release_body", _fail_if_called)
        monkeypatch.setattr(frh, "set_prerelease", _fail_if_called)

        assert frh.main(["apply", "--dry-run"]) == 0
        assert "(dry run -- not applied)" in capsys.readouterr().out

    def test_already_clean_skips_every_mutating_call(self, monkeypatch, capsys):
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: [_release("v1")])

        def _fail_if_called(*a, **kw):
            raise AssertionError("nothing should be mutated when already clean")

        monkeypatch.setattr(frh, "delete_release_and_tag", _fail_if_called)
        monkeypatch.setattr(frh, "update_release_body", _fail_if_called)
        monkeypatch.setattr(frh, "set_prerelease", _fail_if_called)

        assert frh.main(["apply"]) == 0
        assert "already clean" in capsys.readouterr().out

    def test_refuses_to_delete_a_referenced_typo_tag(self, monkeypatch):
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: [_release(frh.TYPO_TAG, rid=99)])
        monkeypatch.setattr(frh, "tree_references", lambda *a, **kw: ["docs/notes.md"])

        def _fail_if_called(*a, **kw):
            raise AssertionError("must not delete while a tree reference exists")

        monkeypatch.setattr(frh, "delete_release_and_tag", _fail_if_called)
        assert frh.main(["apply"]) == 1

    def test_force_flag_overrides_the_tree_reference_refusal(self, monkeypatch):
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: [_release(frh.TYPO_TAG, rid=99)])
        monkeypatch.setattr(frh, "tree_references", lambda *a, **kw: ["docs/notes.md"])
        seen = {}
        monkeypatch.setattr(frh, "delete_release_and_tag", lambda o, r, release: seen.setdefault("deleted", release))
        assert frh.main(["apply", "--force-delete-typo-tag"]) == 0
        assert seen["deleted"]["tag_name"] == frh.TYPO_TAG

    def test_applies_all_three_fixes(self, monkeypatch):
        releases = [
            _release(frh.TYPO_TAG, rid=99),
            _release("v4.0.0a12", rid=1, body=f"see {frh.OLD_FORK_SLUG}"),
            _release("v4.0.0a13", rid=2, prerelease=False),
        ]
        monkeypatch.setattr(frh, "list_releases", lambda *a, **kw: releases)
        monkeypatch.setattr(frh, "tree_references", lambda *a, **kw: [])
        seen = {"deleted": None, "bodies": [], "prerelease": []}
        monkeypatch.setattr(frh, "delete_release_and_tag", lambda o, r, release: seen.__setitem__("deleted", release))
        monkeypatch.setattr(
            frh, "update_release_body", lambda o, r, rid, body: seen["bodies"].append((rid, body))
        )
        monkeypatch.setattr(
            frh, "set_prerelease", lambda o, r, rid, *, prerelease: seen["prerelease"].append((rid, prerelease))
        )

        assert frh.main(["apply"]) == 0
        assert seen["deleted"]["tag_name"] == frh.TYPO_TAG
        assert seen["bodies"] == [(1, f"see {frh.NEW_ORG_SLUG}")]
        assert seen["prerelease"] == [(2, True)]


class TestHeaders:
    def test_missing_token_is_a_clear_failure(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="GITHUB_TOKEN is required"):
            frh._headers()
