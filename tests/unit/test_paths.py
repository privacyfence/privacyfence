"""Tests for paths.py: dev vs. bundled-.app path resolution.

A wrong answer here means credentials/config/logs end up in the wrong
place after packaging (e.g. an .app writing into its own read-only bundle
instead of ~/.privacyfence), so both branches of every function are
covered explicitly.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from privacyfence import paths
from privacyfence.principal import Principal, principal_scope


class TestIsBundled:
    def test_false_when_neither_attribute_set(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert paths.is_bundled() is False

    def test_false_when_frozen_but_no_meipass(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert paths.is_bundled() is False

    def test_false_when_meipass_but_not_frozen(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "/some/bundle", raising=False)
        assert paths.is_bundled() is False

    def test_true_when_both_set(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "/some/bundle", raising=False)
        assert paths.is_bundled() is True


class TestIsInstalledPackage:
    def test_false_for_a_source_checkout_path(self, monkeypatch, tmp_path):
        fake_module_file = tmp_path / "src" / "privacyfence" / "paths.py"
        fake_module_file.parent.mkdir(parents=True)
        monkeypatch.setattr(paths, "__file__", str(fake_module_file))

        assert paths._is_installed_package() is False

    @pytest.mark.parametrize("packages_dir", ["site-packages", "dist-packages"])
    def test_true_when_file_lives_under_a_packages_directory(self, monkeypatch, tmp_path, packages_dir):
        fake_module_file = tmp_path / "lib" / "python3.13" / packages_dir / "privacyfence" / "paths.py"
        fake_module_file.parent.mkdir(parents=True)
        monkeypatch.setattr(paths, "__file__", str(fake_module_file))

        assert paths._is_installed_package() is True


class TestIsWindows:
    def test_true_when_os_name_is_nt(self, monkeypatch):
        monkeypatch.setattr(paths.os, "name", "nt")
        assert paths.is_windows() is True

    def test_false_when_os_name_is_posix(self, monkeypatch):
        monkeypatch.setattr(paths.os, "name", "posix")
        assert paths.is_windows() is False


class TestDataDir:
    def test_dev_mode_resolves_to_project_root_relative_to_this_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        fake_module_file = tmp_path / "src" / "privacyfence" / "paths.py"
        fake_module_file.parent.mkdir(parents=True)
        monkeypatch.setattr(paths, "__file__", str(fake_module_file))

        result = paths.data_dir()

        assert result == tmp_path
        assert result.is_dir()  # mkdir(parents=True, exist_ok=True) was called

    def test_bundled_mode_resolves_under_home_and_creates_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        # Exercises the POSIX branch specifically -- real Windows CI has a
        # real os.name of "nt", which would otherwise take data_dir() down
        # windows_data_dir()'s branch instead and ignore the Path.home()
        # mock below.
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = paths.data_dir()

        assert result == tmp_path / ".privacyfence"
        assert result.is_dir()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_created_at_0700_not_the_process_umask(self, monkeypatch, tmp_path):
        """SEC-09: this directory holds every credential/token file this
        install has, so its own permissions matter regardless of what an
        individual file's chmod does."""
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = paths.data_dir()

        assert stat.S_IMODE(result.stat().st_mode) == 0o700

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_re_tightens_a_pre_existing_directory_with_looser_permissions(self, monkeypatch, tmp_path):
        """A pre-SEC-09 install's data_dir() may already exist at whatever
        the umask left it with (e.g. a shared 0755) -- every subsequent
        resolution must tighten it, not just the first one that creates
        it."""
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        loose = tmp_path / ".privacyfence"
        loose.mkdir()
        loose.chmod(0o755)

        result = paths.data_dir()

        assert stat.S_IMODE(result.stat().st_mode) == 0o700

    def test_bundled_mode_on_windows_resolves_under_local_appdata(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(paths, "is_windows", lambda: True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

        result = paths.data_dir()

        assert result == tmp_path / "AppData" / "Local" / "PrivacyFence"
        assert result.is_dir()
        # Not the POSIX dotfile name -- see windows_data_dir()'s docstring.
        assert not (tmp_path / ".privacyfence").exists()

    def test_bundled_mode_on_windows_falls_back_to_home_when_localappdata_unset(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(paths, "is_windows", lambda: True)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = paths.data_dir()

        assert result == tmp_path / "AppData" / "Local" / "PrivacyFence"
        assert result.is_dir()

    def test_installed_package_resolves_under_home_and_creates_it(self, monkeypatch, tmp_path):
        # A real (non-editable) `pip install privacyfence` -- unbundled
        # (is_bundled() False, no PyInstaller involved) but still not a
        # source checkout, so this must not fall through to the dev-mode
        # branch above and land inside site-packages itself.
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        # Same reasoning as test_bundled_mode_resolves_under_home_and_
        # creates_it above -- this exercises the POSIX branch specifically.
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        fake_module_file = tmp_path / "lib" / "python3.13" / "site-packages" / "privacyfence" / "paths.py"
        fake_module_file.parent.mkdir(parents=True)
        monkeypatch.setattr(paths, "__file__", str(fake_module_file))
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)

        result = paths.data_dir()

        assert result == home / ".privacyfence"
        assert result.is_dir()


class TestOrgDir:
    def test_is_a_subdirectory_of_data_dir_and_gets_created(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.org_dir()

        assert result == tmp_path / "org"
        assert result.is_dir()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_created_at_0700(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.org_dir()

        assert stat.S_IMODE(result.stat().st_mode) == 0o700


class TestSafePrincipalId:
    """P7, org_identity.py's principal_from_claims: an OIDC `sub` claim is
    opaque per spec and may not be filesystem-safe."""

    @pytest.mark.parametrize("safe_id", ["alice", "alice@example.com", "a1b2-c3_d4.e5"])
    def test_already_safe_ids_pass_through_unchanged(self, safe_id):
        assert paths.safe_principal_id(safe_id) == safe_id

    @pytest.mark.parametrize("unsafe_id", ["cn=alice,dc=example,dc=com", "../etc", "a/b", ".."])
    def test_unsafe_ids_are_hashed(self, unsafe_id):
        result = paths.safe_principal_id(unsafe_id)
        assert result != unsafe_id
        assert result.startswith("idp-")
        assert paths._is_safe_principal_id(result)

    def test_hashing_is_deterministic(self):
        assert paths.safe_principal_id("cn=alice") == paths.safe_principal_id("cn=alice")

    def test_different_unsafe_ids_hash_differently(self):
        assert paths.safe_principal_id("cn=alice") != paths.safe_principal_id("cn=bob")


class TestUserDir:
    """Per-principal storage layout."""

    def test_local_principal_is_data_dir_itself(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        assert paths.user_dir(Principal(id="local")) == tmp_path
        # Not a users/local/ subdirectory -- an existing single-user install
        # needs no migration.
        assert not (tmp_path / "users").exists()

    def test_defaults_to_current_principal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        with principal_scope(Principal(id="local")):
            assert paths.user_dir() == tmp_path

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_other_principal_gets_a_users_subdirectory_and_it_is_created(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.user_dir(Principal(id="alice@example.com"))

        assert result == tmp_path / "users" / "alice@example.com"
        assert result.is_dir()
        assert stat.S_IMODE(result.stat().st_mode) == 0o700

    def test_two_principals_get_different_directories(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        alice = paths.user_dir(Principal(id="alice"))
        bob = paths.user_dir(Principal(id="bob"))

        assert alice != bob
        assert alice == tmp_path / "users" / "alice"
        assert bob == tmp_path / "users" / "bob"

    @pytest.mark.parametrize("bad_id", ["../etc", "a/b", "..", ".", ""])
    def test_rejects_unsafe_principal_ids(self, monkeypatch, tmp_path, bad_id):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        with pytest.raises(ValueError):
            paths.user_dir(Principal(id=bad_id))


class TestAuthorityDir:
    """#428 Phase 1: the human-authority root -- web_token, settings.yaml,
    webauthn credentials, and the audit log -- split out of user_dir(),
    which stays reachable by the agent for mcp_token and connector state."""

    def test_local_principal_is_a_subdirectory_of_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.authority_dir(Principal(id="local"))

        assert result == tmp_path / "authority"
        assert result.is_dir()

    def test_other_principal_is_a_subdirectory_of_their_own_user_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.authority_dir(Principal(id="alice"))

        assert result == tmp_path / "users" / "alice" / "authority"
        assert result.is_dir()

    def test_defaults_to_current_principal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        with principal_scope(Principal(id="local")):
            assert paths.authority_dir() == tmp_path / "authority"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap, the now-removed windows-linux-support-plan.md's Track B3)",
    )
    def test_created_at_0700(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.authority_dir()

        assert stat.S_IMODE(result.stat().st_mode) == 0o700

    @pytest.mark.parametrize("legacy_relative", [
        Path("config") / "settings.yaml",
        "webauthn_credentials.json",
        "web_token",
        "web_token_version",
    ])
    def test_migrates_a_legacy_file_on_first_use(self, monkeypatch, tmp_path, legacy_relative):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        legacy_path = tmp_path / legacy_relative
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("pre-4.1 content", encoding="utf-8")

        result = paths.authority_dir()

        assert not legacy_path.exists()
        assert (result / legacy_relative).read_text(encoding="utf-8") == "pre-4.1 content"

    def test_does_not_migrate_the_audit_log(self, monkeypatch, tmp_path):
        # Regression test: authority_dir()/authority_root() with no
        # migrate_audit_log=True must never touch logs/audit. It once did
        # unconditionally, which broke org mode's audit trail -- any call
        # resolving an org principal's settings.yaml (which does go through
        # authority_dir()) silently relocated a directory that principal's
        # own audit logger (audit_log.py's _fallback_log_dir(), never
        # redirected by #428) was still actively writing to at its old,
        # unmigrated path -- see privacyfence/privacyfence#440's CI failure.
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        legacy_audit = tmp_path / "logs" / "audit"
        legacy_audit.mkdir(parents=True)
        (legacy_audit / "2026-W01.jsonl").write_text('{"decision": "approved"}\n', encoding="utf-8")

        result = paths.authority_dir()

        assert legacy_audit.exists()
        assert not (result / "logs").exists()

    def test_a_missing_legacy_file_is_not_fabricated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.authority_dir()

        assert not (result / "web_token").exists()

    def test_does_not_overwrite_an_already_migrated_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        (tmp_path / "web_token").write_text("legacy", encoding="utf-8")
        first = paths.authority_dir()
        (first / "web_token").write_text("current", encoding="utf-8")
        # A legacy file re-appearing (e.g. something else wrote to the old
        # path after migration already ran) must never clobber real current
        # state on a second lookup.
        (tmp_path / "web_token").write_text("stale-legacy", encoding="utf-8")

        second = paths.authority_dir()

        assert (second / "web_token").read_text(encoding="utf-8") == "current"

    def test_a_failed_migration_is_logged_not_raised(self, monkeypatch, tmp_path, caplog):
        # e.g. a permissions error mid-upgrade -- the directory must still
        # come back usable rather than taking the whole startup down over
        # one file that couldn't be moved.
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        (tmp_path / "web_token").write_text("legacy", encoding="utf-8")

        def _raise_rename(self, target):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "rename", _raise_rename)

        with caplog.at_level("WARNING"):
            result = paths.authority_dir()

        assert result.is_dir()
        assert not (result / "web_token").exists()
        assert "web_token" in caplog.text


class TestAuthorityRoot:
    """authority_root() is authority_dir()'s own underlying primitive, taking
    an explicit root rather than a principal -- daemon_main.py's local-
    principal path resolution uses it directly, anchored on its own
    PROJECT_ROOT/data_dir() module-level references rather than calling
    user_dir()/authority_dir() itself, so that tests which monkeypatch those
    two names to sandbox a run keep doing so correctly (see that module's
    _resolve_authority_path())."""

    def test_is_an_authority_subdirectory_of_the_given_root(self, tmp_path):
        result = paths.authority_root(tmp_path)

        assert result == tmp_path / "authority"
        assert result.is_dir()

    def test_migrates_legacy_files_relative_to_the_given_root(self, tmp_path):
        (tmp_path / "web_token").write_text("secret", encoding="utf-8")

        result = paths.authority_root(tmp_path)

        assert not (tmp_path / "web_token").exists()
        assert (result / "web_token").read_text(encoding="utf-8") == "secret"

    def test_does_not_migrate_the_audit_log_by_default(self, tmp_path):
        legacy_audit = tmp_path / "logs" / "audit"
        legacy_audit.mkdir(parents=True)
        (legacy_audit / "2026-W01.jsonl").write_text('{"decision": "approved"}\n', encoding="utf-8")

        result = paths.authority_root(tmp_path)

        assert legacy_audit.exists()
        assert not (result / "logs").exists()

    def test_migrates_the_audit_log_when_asked(self, tmp_path):
        legacy_audit = tmp_path / "logs" / "audit"
        legacy_audit.mkdir(parents=True)
        (legacy_audit / "2026-W01.jsonl").write_text('{"decision": "approved"}\n', encoding="utf-8")

        result = paths.authority_root(tmp_path, migrate_audit_log=True)

        assert not legacy_audit.exists()
        migrated = result / "logs" / "audit" / "2026-W01.jsonl"
        assert migrated.read_text(encoding="utf-8") == '{"decision": "approved"}\n'

    def test_a_second_call_without_migrate_audit_log_does_not_undo_it(self, tmp_path):
        # Once daemon_main.py's own call has migrated the audit log, a later
        # read-only accessor (settings_controller.py's export/snapshot
        # helpers) calling authority_root() with the default False must see
        # the already-migrated directory, not have it treated as absent.
        legacy_audit = tmp_path / "logs" / "audit"
        legacy_audit.mkdir(parents=True)
        (legacy_audit / "2026-W01.jsonl").write_text('{"decision": "approved"}\n', encoding="utf-8")
        migrated_path = paths.authority_root(tmp_path, migrate_audit_log=True) / "logs" / "audit" / "2026-W01.jsonl"

        result = paths.authority_root(tmp_path)

        assert result / "logs" / "audit" / "2026-W01.jsonl" == migrated_path
        assert migrated_path.read_text(encoding="utf-8") == '{"decision": "approved"}\n'

    def test_authority_dir_and_authority_root_agree_for_the_local_principal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        assert paths.authority_dir(Principal(id="local")) == paths.authority_root(tmp_path)


class TestDownloadsDir:
    """The per-principal downloads directory used for staged delivery."""

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_is_a_downloads_subdirectory_of_user_dir_and_is_created(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.downloads_dir(Principal(id="alice"))

        assert result == tmp_path / "users" / "alice" / "downloads"
        assert result.is_dir()
        assert stat.S_IMODE(result.stat().st_mode) == 0o700

    def test_local_principal_gets_downloads_under_data_dir_itself(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        assert paths.downloads_dir(Principal(id="local")) == tmp_path / "downloads"

    def test_defaults_to_current_principal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        with principal_scope(Principal(id="bob")):
            assert paths.downloads_dir() == tmp_path / "users" / "bob" / "downloads"


class TestAllDownloadsDirs:
    """Enumeration used by DownloadStagingStore.__init__ to find ciphertext
    orphaned by a daemon restart -- see that class's docstring. Existence-
    only: unlike downloads_dir()/user_dir(), it must never create a
    directory, or every call would provision empty staging dirs for
    principals that never staged anything."""

    def test_empty_data_dir_yields_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        assert paths.all_downloads_dirs() == []
        # Confirms the "existence-only" claim above.
        assert not (tmp_path / "downloads").exists()
        assert not (tmp_path / "users").exists()

    def test_finds_the_local_principals_downloads_dir_once_provisioned(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        paths.downloads_dir(Principal(id="local"))

        assert paths.all_downloads_dirs() == [tmp_path / "downloads"]

    def test_finds_each_provisioned_org_principals_downloads_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        paths.downloads_dir(Principal(id="alice"))
        paths.downloads_dir(Principal(id="bob"))

        assert paths.all_downloads_dirs() == [
            tmp_path / "users" / "alice" / "downloads",
            tmp_path / "users" / "bob" / "downloads",
        ]

    def test_skips_a_users_entry_that_never_provisioned_a_downloads_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        paths.user_dir(Principal(id="carol"))  # config/etc. but no download ever staged

        assert paths.all_downloads_dirs() == []

    def test_skips_a_users_entry_that_isnt_a_safe_principal_id(self, monkeypatch, tmp_path):
        # user_dir()/downloads_dir() could never have produced this
        # directory name themselves (the character class rejects the
        # space) -- reachable only by something else writing directly
        # under users/, which all_downloads_dirs() must not trust.
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        stray = tmp_path / "users" / "not a safe id" / "downloads"
        stray.mkdir(parents=True)

        assert paths.all_downloads_dirs() == []

    def test_skips_a_file_sitting_directly_under_users(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        users_root = tmp_path / "users"
        users_root.mkdir()
        (users_root / "not-a-directory").write_text("stray file")

        assert paths.all_downloads_dirs() == []


class TestBundleMacosDir:
    def test_none_when_not_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        assert paths.bundle_macos_dir() is None

    def test_parent_of_executable_when_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(sys, "executable", "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app")

        result = paths.bundle_macos_dir()

        assert result == Path("/Applications/PrivacyFenceApp.app/Contents/MacOS")


class TestAppBundlePath:
    def test_none_when_not_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        assert paths.app_bundle_path() is None

    def test_app_bundle_root_when_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(sys, "executable", "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app")

        result = paths.app_bundle_path()

        assert result == Path("/Applications/PrivacyFenceApp.app")
