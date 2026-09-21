"""Tests for scripts/check_graphical_session_coverage.py (privacyfence/privacyfence#374).

Imported by file path (importlib), same pattern as tests/unit/test_release_stats.py -- scripts/
isn't part of the installed ``privacyfence`` distribution.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_graphical_session_coverage.py"
_spec = importlib.util.spec_from_file_location("check_graphical_session_coverage", _SCRIPT_PATH)
check_graphical_session_coverage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_graphical_session_coverage)


def _run(*, conclusion="success", head_sha="abc123", html_url="https://example.invalid/run/1"):
    return {"conclusion": conclusion, "head_sha": head_sha, "html_url": html_url}


class TestEvaluate:
    def test_no_run_at_all_warns(self):
        message = check_graphical_session_coverage.evaluate("linux-graphical-session.yml", None, False)

        assert message is not None
        assert "no completed run at all" in message
        assert "#374" in message

    def test_run_not_an_ancestor_warns(self):
        run = _run()

        message = check_graphical_session_coverage.evaluate("windows-graphical-session.yml", run, False)

        assert message is not None
        assert "not an ancestor" in message
        assert run["html_url"] in message

    def test_reachable_but_failed_run_warns(self):
        run = _run(conclusion="failure")

        message = check_graphical_session_coverage.evaluate("linux-graphical-session.yml", run, True)

        assert message is not None
        assert "did not succeed" in message
        assert "conclusion=failure" in message

    def test_reachable_cancelled_run_warns(self):
        run = _run(conclusion="cancelled")

        message = check_graphical_session_coverage.evaluate("linux-graphical-session.yml", run, True)

        assert message is not None
        assert "conclusion=cancelled" in message

    def test_reachable_successful_run_is_fine(self):
        run = _run(conclusion="success")

        message = check_graphical_session_coverage.evaluate("linux-graphical-session.yml", run, True)

        assert message is None


class TestCheckAll:
    def test_collects_a_warning_per_failing_workflow(self, monkeypatch):
        runs = {
            "linux-graphical-session.yml": _run(conclusion="success", head_sha="good"),
            "windows-graphical-session.yml": _run(conclusion="failure", head_sha="bad"),
            "macos-graphical-session.yml": _run(conclusion="success", head_sha="good"),
        }

        def fake_fetch(repo, workflow, branch, token):
            assert repo == "privacyfence/privacyfence"
            assert branch == "main"
            assert token == "test-token"
            return runs[workflow]

        monkeypatch.setattr(check_graphical_session_coverage, "resolve_release_branch", lambda commit: "main")
        monkeypatch.setattr(check_graphical_session_coverage, "fetch_latest_completed_run", fake_fetch)
        monkeypatch.setattr(check_graphical_session_coverage, "is_ancestor", lambda sha, commit: True)

        warnings = check_graphical_session_coverage.check_all(
            "privacyfence/privacyfence", "deadbeef", "test-token"
        )

        assert len(warnings) == 1
        assert "windows-graphical-session.yml" in warnings[0]

    def test_queries_the_resolved_release_branch_not_main(self, monkeypatch):
        seen_branches = []

        def fake_fetch(repo, workflow, branch, token):
            seen_branches.append(branch)
            return _run(conclusion="success")

        monkeypatch.setattr(
            check_graphical_session_coverage, "resolve_release_branch", lambda commit: "releases/4.1-dev"
        )
        monkeypatch.setattr(check_graphical_session_coverage, "fetch_latest_completed_run", fake_fetch)
        monkeypatch.setattr(check_graphical_session_coverage, "is_ancestor", lambda sha, commit: True)

        check_graphical_session_coverage.check_all("privacyfence/privacyfence", "deadbeef", "test-token")

        assert seen_branches == ["releases/4.1-dev"] * len(check_graphical_session_coverage.WORKFLOWS)

    def test_no_warnings_when_all_workflows_are_green_and_reachable(self, monkeypatch):
        monkeypatch.setattr(check_graphical_session_coverage, "resolve_release_branch", lambda commit: "main")
        monkeypatch.setattr(
            check_graphical_session_coverage,
            "fetch_latest_completed_run",
            lambda repo, workflow, branch, token: _run(conclusion="success"),
        )
        monkeypatch.setattr(check_graphical_session_coverage, "is_ancestor", lambda sha, commit: True)

        warnings = check_graphical_session_coverage.check_all(
            "privacyfence/privacyfence", "deadbeef", "test-token"
        )

        assert warnings == []

    def test_fetch_error_is_reported_not_raised(self, monkeypatch):
        import urllib.error

        def fake_fetch(repo, workflow, branch, token):
            raise urllib.error.URLError("boom")

        monkeypatch.setattr(check_graphical_session_coverage, "resolve_release_branch", lambda commit: "main")
        monkeypatch.setattr(check_graphical_session_coverage, "fetch_latest_completed_run", fake_fetch)

        warnings = check_graphical_session_coverage.check_all(
            "privacyfence/privacyfence", "deadbeef", "test-token"
        )

        assert len(warnings) == len(check_graphical_session_coverage.WORKFLOWS)
        assert all("could not check" in message for message in warnings)

    def test_never_calls_is_ancestor_when_there_is_no_run(self, monkeypatch):
        monkeypatch.setattr(check_graphical_session_coverage, "resolve_release_branch", lambda commit: "main")
        monkeypatch.setattr(
            check_graphical_session_coverage,
            "fetch_latest_completed_run",
            lambda repo, workflow, branch, token: None,
        )

        def fail_if_called(sha, commit):
            raise AssertionError("is_ancestor should not run without a fetched run")

        monkeypatch.setattr(check_graphical_session_coverage, "is_ancestor", fail_if_called)

        warnings = check_graphical_session_coverage.check_all(
            "privacyfence/privacyfence", "deadbeef", "test-token"
        )

        assert len(warnings) == len(check_graphical_session_coverage.WORKFLOWS)


class TestResolveReleaseBranch:
    def test_defaults_to_main_when_no_releases_branch_contains_the_commit(self, monkeypatch):
        def fake_run(cmd, check, capture_output, text):
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")

        monkeypatch.setattr(check_graphical_session_coverage.subprocess, "run", fake_run)

        assert check_graphical_session_coverage.resolve_release_branch("deadbeef") == "main"

    def test_prefers_the_releases_branch_that_contains_the_commit(self, monkeypatch):
        def fake_run(cmd, check, capture_output, text):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="origin/main\norigin/releases/4.1-dev\n", stderr=""
            )

        monkeypatch.setattr(check_graphical_session_coverage.subprocess, "run", fake_run)

        assert check_graphical_session_coverage.resolve_release_branch("deadbeef") == "releases/4.1-dev"

    def test_uses_git_branch_contains_with_the_given_commit(self, monkeypatch):
        seen_cmd = []

        def fake_run(cmd, check, capture_output, text):
            seen_cmd.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(check_graphical_session_coverage.subprocess, "run", fake_run)

        check_graphical_session_coverage.resolve_release_branch("deadbeef")

        assert seen_cmd == [["git", "branch", "-r", "--contains", "deadbeef", "--format=%(refname:short)"]]


class TestIsAncestor:
    def test_true_for_head_commit_itself(self):
        import subprocess

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()

        assert check_graphical_session_coverage.is_ancestor(head, head) is True

    def test_false_for_unrelated_sha(self):
        assert check_graphical_session_coverage.is_ancestor("0" * 40, "HEAD") is False


class TestMain:
    def test_requires_a_token(self, monkeypatch, capsys):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        exit_code = check_graphical_session_coverage.main(
            ["--repo", "privacyfence/privacyfence", "--commit", "deadbeef"]
        )

        assert exit_code == 2
        assert "GH_TOKEN" in capsys.readouterr().err

    def test_exits_zero_with_warnings_when_no_channel_given(self, monkeypatch, capsys):
        # No --channel at all (the flag's own default, "") -- same as this script's behavior
        # before privacyfence/privacyfence#374's option 3 landed.
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setattr(
            check_graphical_session_coverage,
            "check_all",
            lambda repo, commit, token: ["something is stale"],
        )

        exit_code = check_graphical_session_coverage.main(
            ["--repo", "privacyfence/privacyfence", "--commit", "deadbeef"]
        )

        assert exit_code == 0
        assert "::warning::something is stale" in capsys.readouterr().out

    def test_exits_zero_with_warnings_on_a_pre_release_channel(self, monkeypatch, capsys):
        # Option 3's whole point: pre-release tags stay ungated -- a flake there is cheap.
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setattr(
            check_graphical_session_coverage,
            "check_all",
            lambda repo, commit, token: ["something is stale"],
        )

        exit_code = check_graphical_session_coverage.main(
            ["--repo", "privacyfence/privacyfence", "--commit", "deadbeef", "--channel", "alpha"]
        )

        assert exit_code == 0
        assert "::warning::something is stale" in capsys.readouterr().out

    def test_fails_with_warnings_on_the_stable_channel(self, monkeypatch, capsys):
        # Option 3: a stable tag is the one case a coverage gap actually blocks the release.
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setattr(
            check_graphical_session_coverage,
            "check_all",
            lambda repo, commit, token: ["something is stale"],
        )

        exit_code = check_graphical_session_coverage.main(
            ["--repo", "privacyfence/privacyfence", "--commit", "deadbeef", "--channel", "stable"]
        )

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "::warning::something is stale" in out
        assert "::error::" in out
        assert "#374" in out

    def test_prints_a_clean_summary_with_no_warnings(self, monkeypatch, capsys):
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setattr(check_graphical_session_coverage, "check_all", lambda repo, commit, token: [])

        exit_code = check_graphical_session_coverage.main(
            ["--repo", "privacyfence/privacyfence", "--commit", "deadbeef"]
        )

        assert exit_code == 0
        assert "is green and reachable" in capsys.readouterr().out

    def test_stable_channel_exits_zero_when_nothing_is_stale(self, monkeypatch, capsys):
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setattr(check_graphical_session_coverage, "check_all", lambda repo, commit, token: [])

        exit_code = check_graphical_session_coverage.main(
            ["--repo", "privacyfence/privacyfence", "--commit", "deadbeef", "--channel", "stable"]
        )

        assert exit_code == 0
        assert "is green and reachable" in capsys.readouterr().out
