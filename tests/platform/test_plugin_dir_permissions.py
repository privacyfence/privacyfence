"""The plugins' administrator-only check, against a real filesystem.

The unit tests fake ``os.stat`` and the DACL reader; this asks the real operating system. A file
the test itself just created is, by construction, one a non-administrator can rewrite, so the
check has to refuse it on every platform: through ownership and mode bits on POSIX, through the
inherited user-writable ACL of the temp directory on Windows (ADR 0058, ADR 0121). The one case
expected to pass is a plugin an elevated process installs under ``%ProgramFiles%``.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid

import pytest

from privacyfence import privilege_separation
from privacyfence.plugins import trust

pytestmark = pytest.mark.platform


def _make_plugin(tmp_path):
    plugin_dir = tmp_path / "today"
    plugin_dir.mkdir()
    exe = plugin_dir / "today-plugin.exe"
    exe.write_bytes(b"not really a program")
    return plugin_dir, exe


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership; Windows is the ACL case below")
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root owns what root creates")
def test_posix_refuses_a_file_this_user_owns(tmp_path):
    plugin_dir, exe = _make_plugin(tmp_path)

    problem = privilege_separation.admin_only_write_problem(exe)

    assert problem == f"{exe} is owned by uid {os.geteuid()}, not root"
    assert trust.admin_only_problem(plugin_dir, exe) == problem


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs exist only on Windows")
def test_windows_refuses_a_file_in_a_user_writable_directory(tmp_path):
    plugin_dir, exe = _make_plugin(tmp_path)

    problem = privilege_separation.admin_only_write_problem(exe)

    assert problem is not None
    assert "is writable by" in problem
    assert trust.admin_only_problem(plugin_dir, exe) == problem


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs exist only on Windows")
def test_windows_reads_inherit_only_entries_on_program_files():
    # Program Files carries inherit-only entries (CREATOR OWNER among them) on every standard
    # install; the ancestor rule can only ignore them if the real reader reports the flag.
    from privacyfence import windows_acl

    aces = windows_acl.read_dacl(trust.plugins_dir().parent)

    assert aces is not None
    assert any(ace.inherit_only for ace in aces)
    assert not all(ace.inherit_only for ace in aces)


def _is_elevated() -> bool:
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs exist only on Windows")
def test_windows_accepts_a_plugin_installed_under_program_files():
    # A folder created under %ProgramFiles% inherits CREATOR OWNER full control as an
    # inherit-only entry; the plugin directory must not count it, and nothing else on the way
    # may grant a non-administrator write.
    from privacyfence import windows_acl

    if not _is_elevated():
        pytest.skip("creating a folder under %ProgramFiles% needs an elevated process")
    plugins = trust.plugins_dir().parent / f"PrivacyFence Plugin Test {uuid.uuid4().hex}"
    try:
        plugin_dir = plugins / "today"
        (plugin_dir / "bin").mkdir(parents=True)
        exe = plugin_dir / "bin" / "today-plugin.exe"
        exe.write_bytes(b"not really a program")
        owner = windows_acl.read_owner(plugin_dir)
        if owner is None or not windows_acl.is_trusted(owner):
            pytest.skip(f"this process creates folders owned by {owner}, not by Administrators")

        assert any(ace.inherit_only for ace in windows_acl.read_dacl(plugin_dir))
        assert privilege_separation.admin_only_write_problem(plugin_dir) is not None
        assert trust.admin_only_problem(plugin_dir, exe) is None
    finally:
        shutil.rmtree(plugins, ignore_errors=True)
