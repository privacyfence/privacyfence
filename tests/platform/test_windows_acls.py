"""#428 Phase 4 (B5c): NTFS ACLs, against a real filesystem.

Every other test of this feature works on a model. ``windows_acl``'s audit
functions take a list of dataclasses, ``TestWindowsLayoutAudit`` hands
``audit_layout()`` a synthetic DACL, and the installer contract compares two
files' constants -- all of which run on this repo's Ubuntu CI and none of
which can answer the question the whole Windows layout rests on: **does an
``icacls`` grant read back the way this code assumes it does?**

That question has four parts, and each one is a decision made elsewhere in
B5c that would be silently wrong if the assumption underneath it were:

1. ``icacls /grant "<who>":(X)`` really is traverse-without-listing -- the
   Windows spelling of POSIX ``0711``, and the reason the separated root can
   be reachable without being enumerable.
2. ``icacls /inheritance:r`` really does sever what ``%ProgramData%``
   inherits down -- the single load-bearing line in
   ``scripts/windows_privilege_separation.ps1``.
3. A file *created* in an ACL'd directory inherits that directory's grants
   -- which is what lets ``privilege_separation.write_handoff_file()`` stay
   a plain atomic write on Windows, with no ACL code of its own.
4. A file *moved* into one does **not** -- which is the entire reason that
   script runs ``icacls /reset`` over ``handoff/`` after the migration, and
   the reason ``ensure_handoff_file_mode()`` is a no-op there rather than a
   chmod.

And one thing that is not about ACLs at all but only shows up on this
platform: ``secure_mkdir(..., foreign_owner_ok=True)`` has to stay *silent*
here, since the processes that resolve a shared directory most often are
exactly the ones that cannot chmod it.

None of this needs Administrator: every ACL below is set on a directory this
process just created and therefore already owns. What it cannot cover is the
real service account -- creating one means creating the service -- so the
current user stands in for it, which is exactly the substitution that makes
these runnable on the ``platform-windows`` job on every PR rather than only
by hand on a real install.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from privacyfence import secure_files, windows_acl

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs exist only on Windows"),
]

# Well-known SIDs, for the same reason the installer uses them: "BUILTIN\
# Users" is spelled differently on a localized Windows and icacls would
# reject the name.
SID_USERS = "*S-1-5-32-545"
SID_SYSTEM = "*S-1-5-18"


def icacls(*args: str) -> None:
    """Run icacls and fail the test with its own output on a non-zero exit.

    Deliberately the real tool rather than pywin32's ``SetNamedSecurityInfo``:
    what is under test here is the behavior of the exact commands
    ``scripts/windows_privilege_separation.ps1`` runs, so reproducing them
    through a different API would prove something about that API instead.
    """
    completed = subprocess.run(
        ["icacls.exe", *args], capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, (
        f"icacls {' '.join(args)} failed ({completed.returncode}):\n"
        f"{completed.stdout}\n{completed.stderr}"
    )


@pytest.fixture
def me() -> str:
    """This process's own account, as the ACL reader itself names it -- so a
    comparison against a trustee read back off disk is comparing two answers
    from the same source rather than an environment variable against a SID
    lookup."""
    return windows_acl.current_account_name()


@pytest.fixture
def provisioned_root(tmp_path, me):
    """``tmp_path``, ACL'd exactly the way the installer ACLs the separated
    root -- with this process standing in for the service account."""
    root = tmp_path / "PrivacyFence"
    root.mkdir()
    icacls(str(root), "/inheritance:r", "/q")
    icacls(str(root), "/grant:r", f"{me}:(OI)(CI)(F)", f"{SID_SYSTEM}:(OI)(CI)(F)", "/q")
    icacls(str(root), "/grant", f"{SID_USERS}:(X)", "/q")
    return root


class TestCurrentAccountName:
    def test_answers_this_process_rather_than_the_environment(self, me):
        # %USERNAME% is what this replaced, and the replacement is the whole
        # reason check_runtime_identity() can recognize a virtual service
        # account at all -- a service running as NT SERVICE\PrivacyFence gets
        # an environment block naming the *machine* account instead. For an
        # ordinary interactive process the two still agree on the account
        # part, which is what makes that comparable here at all.
        import os

        assert "\\" in me, f"expected DOMAIN\\Name, got {me!r}"
        assert me.rsplit("\\", 1)[-1].casefold() == os.environ["USERNAME"].casefold()


class TestReadDacl:
    def test_reads_back_what_icacls_wrote(self, provisioned_root, me):
        aces = windows_acl.read_dacl(provisioned_root)

        assert aces is not None
        trustees = {windows_acl.normalize_trustee(ace.trustee) for ace in aces}
        assert windows_acl.normalize_trustee(me) in trustees

    def test_inheritance_r_really_severs_program_data_style_inheritance(self, tmp_path, me):
        # Assumption 2. A directory created under an inheriting parent picks
        # up that parent's inheritable ACEs; /inheritance:r is what stops it,
        # and if it ever stopped working the separated root would be readable
        # by every account on the machine with nothing in the install output
        # to say so.
        parent = tmp_path / "parent"
        parent.mkdir()
        icacls(str(parent), "/grant", f"{SID_USERS}:(OI)(CI)(RX)", "/q")
        child = parent / "child"
        child.mkdir()

        before = windows_acl.read_dacl(child)
        assert before is not None
        assert any(ace.inherited and ace.grants_read() for ace in before), (
            "the fixture is wrong: the child inherited nothing to sever"
        )

        icacls(str(child), "/inheritance:r", "/q")
        icacls(str(child), "/grant:r", f"{me}:(OI)(CI)(F)", "/q")

        after = windows_acl.read_dacl(child)
        assert after is not None
        assert not any(ace.inherited for ace in after)

    def test_a_traverse_only_grant_is_not_a_read_grant(self, provisioned_root):
        # Assumption 1, read straight off a real DACL: (X) has to come back
        # with FILE_EXECUTE and without FILE_READ_DATA, or "traversable but
        # not listable" is a claim this repo cannot make.
        aces = windows_acl.read_dacl(provisioned_root)

        users = [
            ace for ace in aces
            if windows_acl.trustee_matches(ace.trustee, "Users") and ace.allowed
        ]
        assert users, f"no Users ACE on {provisioned_root}: {aces}"
        assert all(ace.grants_traverse() for ace in users)
        assert not any(ace.grants_read() for ace in users)

    def test_the_audit_accepts_a_root_the_installer_would_have_written(
        self, provisioned_root, me,
    ):
        # The end-to-end claim of this file: a real directory, ACL'd by the
        # real commands, passing the real audit.
        assert windows_acl.root_problems(
            provisioned_root, windows_acl.read_dacl(provisioned_root), service_account=me,
        ) == []

    def test_the_audit_reports_a_root_that_kept_its_inheritance(self, tmp_path, me):
        parent = tmp_path / "parent"
        parent.mkdir()
        icacls(str(parent), "/grant", f"{SID_USERS}:(OI)(CI)(RX)", "/q")
        root = parent / "PrivacyFence"
        root.mkdir()
        icacls(str(root), "/grant", f"{me}:(OI)(CI)(F)", "/q")

        problems = windows_acl.root_problems(
            root, windows_acl.read_dacl(root), service_account=me,
        )

        assert any("list its contents" in problem for problem in problems)


class TestSharedDirectoriesSkipTheChmod:
    """``secure_mkdir(..., foreign_owner_ok=True)`` has to stay silent on
    Windows, and this is the only place that can prove it.

    On a separated install the companion and the MCP client resolve
    ``paths.handoff_dir()`` constantly while holding read access to it and
    nothing more. ``os.chmod`` there fails, and since SEC-09 that failure is
    a ``warning`` rather than a swallowed ``debug`` line -- so getting this
    wrong does not break anything, it just fires a permissions warning on
    every path resolution in the two processes that are behaving correctly,
    which is precisely how a reader learns to ignore the warnings SEC-09
    added the logging for.
    """

    def test_windows_is_never_treated_as_the_owner(self):
        assert secure_files._is_owned_by_this_process(Path("C:/Windows")) is False

    def test_a_shared_directory_resolves_without_a_warning(self, tmp_path, caplog):
        shared = tmp_path / "handoff"
        shared.mkdir()

        with caplog.at_level(logging.WARNING, logger="privacyfence.secure_files"):
            result = secure_files.secure_mkdir(shared, 0o2770, foreign_owner_ok=True)

        assert result == shared
        assert caplog.records == []

    def test_an_ordinary_directory_still_gets_the_call(self, tmp_path):
        # The skip is scoped to foreign_owner_ok: every other caller keeps
        # the same behavior it had before #428 Phase 4, which on Windows is
        # "attempt it, and it does nothing" rather than "do not attempt it".
        ordinary = tmp_path / "credentials"

        assert secure_files.secure_mkdir(ordinary).is_dir()


class TestHandoffInheritance:
    """Assumptions 3 and 4, which together decide how much ACL code the
    Python side needs: none for a write, and a one-time ``/reset`` in the
    installer for the migration."""

    @pytest.fixture
    def handoff(self, provisioned_root, me):
        handoff = provisioned_root / "handoff"
        handoff.mkdir()
        icacls(str(handoff), "/inheritance:r", "/q")
        icacls(
            str(handoff), "/grant:r",
            f"{me}:(OI)(CI)(F)", f"{SID_SYSTEM}:(OI)(CI)(F)", f"{SID_USERS}:(OI)(CI)(RX)", "/q",
        )
        return handoff

    def test_a_created_file_inherits_the_directorys_grants(self, handoff):
        # Why write_handoff_file() is a plain atomic write on Windows: the
        # agent's mcp_token becomes readable by the handoff group because the
        # directory says so, not because anything sets an ACL on the file.
        token = handoff / "mcp_token"
        token.write_text("deadbeef", encoding="utf-8")

        aces = windows_acl.read_dacl(token)

        assert aces is not None
        assert any(
            windows_acl.trustee_matches(ace.trustee, "Users") and ace.grants_read()
            for ace in aces
        ), f"a file created in the handoff directory did not inherit its grants: {aces}"

    def test_a_moved_file_keeps_the_acl_it_arrived_with(self, tmp_path, handoff, me):
        # And why the installer runs `icacls /reset` over handoff\ after the
        # migration, rather than trusting the move: a rename preserves the
        # source file's security descriptor. mcp_token is the one handoff
        # file that is reused across restarts rather than rewritten, so
        # without that reset a migrated install's agent could never read its
        # own credential again -- the exact failure the POSIX scripts' own
        # `find -exec chmod 640` exists to prevent.
        elsewhere = tmp_path / "LocalAppData"
        elsewhere.mkdir()
        token = elsewhere / "mcp_token"
        token.write_text("deadbeef", encoding="utf-8")
        icacls(str(token), "/inheritance:r", "/q")
        icacls(str(token), "/grant:r", f"{me}:(F)", "/q")

        moved = handoff / "mcp_token"
        token.rename(moved)

        kept = windows_acl.read_dacl(moved)
        assert kept is not None
        assert not any(
            windows_acl.trustee_matches(ace.trustee, "Users") and ace.grants_read()
            for ace in kept
        ), "a moved file picked up the destination's ACL; the installer's icacls /reset is then dead code"

        icacls(str(moved), "/reset", "/q")

        after_reset = windows_acl.read_dacl(moved)
        assert after_reset is not None
        assert any(
            windows_acl.trustee_matches(ace.trustee, "Users") and ace.grants_read()
            for ace in after_reset
        ), "icacls /reset did not make the file re-inherit the handoff directory's grants"

    def test_the_audit_accepts_a_handoff_the_installer_would_have_written(self, handoff, me):
        assert windows_acl.handoff_problems(
            handoff, windows_acl.read_dacl(handoff), service_account=me, service_group="Users",
        ) == []
