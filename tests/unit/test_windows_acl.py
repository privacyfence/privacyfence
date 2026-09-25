"""#428 Phase 4 (B5c): the access-mask arithmetic every Windows layout
decision rests on.

``windows_acl``'s audit functions are the Windows half of what POSIX
expressed as three mode literals, and they are only as good as the bit
tests underneath them -- "does this ACE let its holder *list* the directory"
is the entire difference between ``0711`` and ``0755``, and it is one
``&`` against one constant. That constant being wrong would not fail
loudly anywhere: the audit would simply stop reporting a data directory
every account on the machine can enumerate.

So this file tests the arithmetic directly, on every platform, rather than
only through the layout checks that consume it.
``tests/unit/test_privilege_separation.py``'s ``TestWindowsLayoutAudit``
covers the layout decisions built on top, and
``tests/platform/test_windows_acls.py`` covers the one thing neither can:
that a directory ``icacls`` really has provisioned reads back through
``read_dacl()`` the way all of this assumes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from privacyfence import windows_acl

ACCOUNT = "NT SERVICE\\PrivacyFence"
GROUP = "PrivacyFenceUsers"
PATH = Path("C:/ProgramData/PrivacyFence")


class TestAccessMasks:
    def test_traverse_only_is_not_read(self):
        # The whole of the root's "0711": icacls (X) grants FILE_EXECUTE and
        # SYNCHRONIZE and nothing else, so a process can walk through the
        # directory to reach handoff\ without being able to enumerate what
        # else is in there.
        ace = windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_TRAVERSE_ONLY)

        assert ace.grants_traverse() is True
        assert ace.grants_read() is False
        assert ace.grants_write() is False

    def test_read_execute_is_read(self):
        ace = windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_GENERIC_READ_EXECUTE)

        assert ace.grants_read() is True
        assert ace.grants_traverse() is True
        assert ace.grants_write() is False

    def test_full_access_is_everything(self):
        ace = windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS)

        assert ace.grants_read() is True
        assert ace.grants_write() is True
        assert ace.grants_traverse() is True

    @pytest.mark.parametrize(
        "mask",
        [
            windows_acl.FILE_WRITE_DATA,
            windows_acl.FILE_APPEND_DATA,
            windows_acl.FILE_DELETE_CHILD,
            windows_acl.DELETE,
            # Not "write" in the everyday sense, and exactly why they are in
            # the mask: either one lets its holder grant itself every other
            # right it does not already have.
            windows_acl.WRITE_DAC,
            windows_acl.WRITE_OWNER,
            windows_acl.GENERIC_WRITE,
            windows_acl.GENERIC_ALL,
        ],
    )
    def test_every_way_of_writing_counts_as_writing(self, mask):
        assert windows_acl.Ace("MACHINE\\alice", mask).grants_write() is True

    def test_generic_read_counts_as_reading(self):
        # A hand-written ACE (or one from an older tool) can carry the
        # generic bits rather than the mapped specific ones; the file system
        # maps them at access-check time, so an audit that only looked at
        # FILE_READ_DATA would call such an ACE harmless.
        assert windows_acl.Ace("BUILTIN\\Users", windows_acl.GENERIC_READ).grants_read() is True

    def test_a_deny_ace_grants_nothing(self):
        ace = windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_ALL_ACCESS, allowed=False)

        assert ace.grants_read() is False
        assert ace.grants_write() is False
        assert ace.grants_anything() is False

    def test_read_attributes_alone_is_not_access(self):
        # Every ACE Windows writes carries these; treating them as access
        # would report every directory on the machine as a defect.
        ace = windows_acl.Ace(
            "BUILTIN\\Users", windows_acl.FILE_READ_ATTRIBUTES | windows_acl.READ_CONTROL
        )

        assert ace.grants_anything() is False


class TestTrusteeMatching:
    def test_account_names_are_case_insensitive(self):
        # LookupAccountSid is free to return either spelling depending on how
        # the SID was registered, and comparing with == would make the audit
        # report a correctly provisioned install as broken.
        assert windows_acl.trustee_matches("NT Service\\privacyfence", ACCOUNT) is True

    def test_a_bare_expectation_matches_on_the_account_part(self):
        # A local group's domain is the machine's own name, which no code
        # here can spell in advance -- so the group is named bare and matched
        # against whatever domain the ACE carries.
        assert windows_acl.trustee_matches(f"WIN-ABC123\\{GROUP}", GROUP) is True

    def test_a_qualified_expectation_has_to_match_in_full(self):
        # The important direction: a *local* account someone created called
        # "PrivacyFence" must never satisfy an expectation of
        # NT SERVICE\PrivacyFence, or the audit would accept a layout whose
        # authority directory belongs to an ordinary account.
        assert windows_acl.trustee_matches("WIN-ABC123\\PrivacyFence", ACCOUNT) is False

    def test_forward_slashes_normalize(self):
        assert windows_acl.trustee_matches("NT SERVICE/PrivacyFence", ACCOUNT) is True

    @pytest.mark.parametrize(
        "trustee",
        ["NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "NT SERVICE\\TrustedInstaller"],
    )
    def test_system_and_administrators_are_trusted(self, trustee):
        # TrustedInstaller belongs alongside SYSTEM/Administrators here: it
        # is what *grants* %ProgramFiles% its default write access, not an
        # untrusted principal holding it -- see TRUSTED_TRUSTEES' own
        # docstring.
        assert windows_acl.is_trusted(trustee) is True

    @pytest.mark.parametrize(
        "trustee",
        ["BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users", "Everyone", "CREATOR OWNER"],
    )
    def test_nothing_else_is(self, trustee):
        # CREATOR OWNER in particular: it is inherited from %ProgramData% and
        # its presence means `icacls /inheritance:r` did not take, which is
        # the one thing the whole provisioning sequence depends on.
        assert windows_acl.is_trusted(trustee) is False


class TestRootProblems:
    PROVISIONED = [
        windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS),
        windows_acl.Ace("NT AUTHORITY\\SYSTEM", windows_acl.FILE_ALL_ACCESS),
        windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_TRAVERSE_ONLY),
    ]

    def test_the_provisioned_root_is_clean(self):
        assert windows_acl.root_problems(PATH, self.PROVISIONED, service_account=ACCOUNT) == []

    def test_a_listable_root_is_reported(self):
        aces = [*self.PROVISIONED[:2], windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_GENERIC_READ_EXECUTE)]

        problems = windows_acl.root_problems(PATH, aces, service_account=ACCOUNT)

        assert len(problems) == 1
        assert "list its contents" in problems[0]
        # Named without repr's backslash doubling: this string is read by a
        # human who may paste the account name into a command.
        assert "'BUILTIN\\Users'" in problems[0]

    def test_a_writable_root_is_reported_even_without_read(self):
        aces = [*self.PROVISIONED, windows_acl.Ace("MACHINE\\alice", windows_acl.FILE_WRITE_DATA)]

        problems = windows_acl.root_problems(PATH, aces, service_account=ACCOUNT)

        assert len(problems) == 1
        assert "write access" in problems[0]

    def test_a_root_the_service_account_cannot_write_is_reported(self):
        # The "this install is not actually separated" case from the other
        # direction: the ACL is tight, and the daemon is locked out of its
        # own data directory.
        aces = [windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_TRAVERSE_ONLY)]

        problems = windows_acl.root_problems(PATH, aces, service_account=ACCOUNT)

        assert any("no write access" in problem for problem in problems)


class TestAuthorityProblems:
    def test_the_service_account_alone_is_clean(self):
        aces = [
            windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS),
            windows_acl.Ace("BUILTIN\\Administrators", windows_acl.FILE_ALL_ACCESS),
        ]

        assert windows_acl.authority_problems(PATH, aces, service_account=ACCOUNT) == []

    def test_even_read_access_is_a_defect(self):
        # Unlike the root, this does not distinguish read from write:
        # settings.yaml tells an agent exactly which approvals it can already
        # grant itself, so reading it is not harmless.
        aces = [
            windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS),
            windows_acl.Ace("MACHINE\\alice", windows_acl.FILE_GENERIC_READ_EXECUTE),
        ]

        problems = windows_acl.authority_problems(PATH, aces, service_account=ACCOUNT)

        assert len(problems) == 1
        assert "not actually in effect" in problems[0]

    def test_even_traverse_access_is_a_defect(self):
        aces = [
            windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS),
            windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_TRAVERSE_ONLY),
        ]

        assert len(windows_acl.authority_problems(PATH, aces, service_account=ACCOUNT)) == 1


class TestHandoffProblems:
    PROVISIONED = [
        windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS),
        windows_acl.Ace(f"MACHINE\\{GROUP}", windows_acl.FILE_GENERIC_READ_EXECUTE),
    ]

    def _problems(self, aces):
        return windows_acl.handoff_problems(
            PATH, aces, service_account=ACCOUNT, service_group=GROUP
        )

    def test_the_provisioned_handoff_is_clean(self):
        assert self._problems(self.PROVISIONED) == []

    def test_a_group_that_cannot_read_is_reported(self):
        # This is the check that catches a *broken* install rather than an
        # insecure one, and it is worth having for exactly that reason: the
        # symptom is "daemon not running" reported against a daemon that is.
        problems = self._problems([windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS)])

        assert len(problems) == 1
        assert "cannot read mcp_url, web_base_url" in problems[0]

    def test_a_writable_group_is_reported(self):
        aces = [
            windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS),
            windows_acl.Ace(f"MACHINE\\{GROUP}", windows_acl.FILE_ALL_ACCESS),
        ]

        problems = self._problems(aces)

        assert len(problems) == 1
        assert "never to rewrite it" in problems[0]

    def test_a_third_principal_is_reported(self):
        aces = [*self.PROVISIONED, windows_acl.Ace("Everyone", windows_acl.FILE_GENERIC_READ_EXECUTE)]

        problems = self._problems(aces)

        assert len(problems) == 1
        assert "'Everyone'" in problems[0]


class TestImageProblems:
    def test_an_administrator_owned_install_is_clean(self):
        aces = [
            windows_acl.Ace("BUILTIN\\Administrators", windows_acl.FILE_ALL_ACCESS),
            windows_acl.Ace("NT AUTHORITY\\SYSTEM", windows_acl.FILE_ALL_ACCESS),
            windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_GENERIC_READ_EXECUTE),
        ]

        assert windows_acl.image_problems(PATH, aces, service_account=ACCOUNT) == []

    def test_a_user_writable_install_is_reported(self):
        # #407: a service runs whatever binPath names, so this is the agent
        # being handed a way to run its own code as the service account.
        aces = [windows_acl.Ace("MACHINE\\alice", windows_acl.FILE_ALL_ACCESS)]

        problems = windows_acl.image_problems(PATH, aces, service_account=ACCOUNT)

        assert len(problems) == 1
        assert "run code as that account" in problems[0]
        assert "only administrators can write" in problems[0]

    def test_reading_the_install_is_fine(self):
        # Every account on the machine can read %ProgramFiles%, and nothing
        # about privilege separation changes that -- only writing matters.
        aces = [windows_acl.Ace("BUILTIN\\Users", windows_acl.FILE_GENERIC_READ_EXECUTE)]

        assert windows_acl.image_problems(PATH, aces, service_account=ACCOUNT) == []

    def test_trustedinstaller_write_is_not_reported(self):
        # The regression this guards: %ProgramFiles% is owned by, and
        # inherits a full-control grant to, NT SERVICE\TrustedInstaller by
        # default on a stock Windows install -- that is what makes
        # %ProgramFiles% write-protected from an ordinary Administrator
        # token in the first place, not a gap in the protection. Before
        # TrustedInstaller was added to TRUSTED_TRUSTEES, this exact ACE
        # made every install into the installer's own default location fail
        # Assert-ImageProtected/image_problems.
        aces = [windows_acl.Ace("NT SERVICE\\TrustedInstaller", windows_acl.FILE_ALL_ACCESS)]

        assert windows_acl.image_problems(PATH, aces, service_account=ACCOUNT) == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the point of these is the no-pywin32 fallback; on Windows the real calls answer",
)
class TestWin32LookupsAreBestEffort:
    """Every pywin32 call in this module answers None rather than raising,
    including on a machine that has no pywin32 at all -- these run on Linux,
    which is the strongest form of that claim. What the real calls return
    where there *is* a Win32 to ask is
    ``tests/platform/test_windows_acls.py``'s subject."""

    def test_read_dacl_answers_none(self, tmp_path):
        assert windows_acl.read_dacl(tmp_path) is None

    def test_has_null_dacl_answers_false(self, tmp_path):
        assert windows_acl.has_null_dacl(tmp_path) is False

    def test_lookup_account_sid_answers_none(self):
        assert windows_acl.lookup_account_sid(ACCOUNT) is None

    def test_read_owner_answers_none(self, tmp_path):
        assert windows_acl.read_owner(tmp_path) is None


class TestOwnerRights:
    """``OWNER RIGHTS`` (S-1-3-4) is not a principal — it grants whoever owns
    the object — and a real ``platform-windows`` run is what put this class
    here: an ACE Windows had materialized on a directory under a user
    profile, which ``icacls /inheritance:r`` does not remove, made a
    correctly provisioned root fail its own audit.

    Reading it as a literal trustee calls that root broken. Ignoring it
    calls a *human-owned* root fine, which is the bypass this whole phase
    exists to close. So it is resolved against the real owner, and these are
    the two directions that has to come out right in.
    """

    OWNER_RIGHTS_FULL = windows_acl.Ace(
        windows_acl.OWNER_RIGHTS_TRUSTEE, windows_acl.FILE_ALL_ACCESS
    )

    def test_an_administrator_owned_root_accepts_it(self):
        aces = [windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS), self.OWNER_RIGHTS_FULL]

        assert windows_acl.root_problems(
            PATH, aces, service_account=ACCOUNT, owner="BUILTIN\\Administrators",
        ) == []

    def test_a_service_account_owned_root_accepts_it(self):
        aces = [windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS), self.OWNER_RIGHTS_FULL]

        assert windows_acl.root_problems(
            PATH, aces, service_account=ACCOUNT, owner=ACCOUNT,
        ) == []

    def test_a_human_owned_root_reports_it(self):
        # The case that makes this worth resolving rather than trusting:
        # identical DACL, and every byte of protection in it is void.
        aces = [windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS), self.OWNER_RIGHTS_FULL]

        problems = windows_acl.root_problems(
            PATH, aces, service_account=ACCOUNT, owner="MACHINE\\alice",
        )

        assert len(problems) == 1
        assert "list its contents" in problems[0]
        # Names both the ACE to remove and who it currently reaches: one
        # without the other sends the reader looking for an account that
        # does not exist, or for an ACE they cannot find.
        assert "'OWNER RIGHTS'" in problems[0]
        assert "'MACHINE\\alice'" in problems[0]

    def test_an_unknown_owner_is_treated_as_untrusted(self):
        # read_owner() answering None must fail loud rather than silent: a
        # reported grant that turns out harmless costs a log line, an
        # accepted one that turns out to be a bypass costs the feature.
        aces = [windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS), self.OWNER_RIGHTS_FULL]

        assert len(windows_acl.root_problems(PATH, aces, service_account=ACCOUNT)) == 1

    def test_it_resolves_in_the_authority_check_too(self):
        aces = [windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS), self.OWNER_RIGHTS_FULL]

        assert windows_acl.authority_problems(
            PATH, aces, service_account=ACCOUNT, owner="BUILTIN\\Administrators",
        ) == []
        assert len(windows_acl.authority_problems(
            PATH, aces, service_account=ACCOUNT, owner="MACHINE\\alice",
        )) == 1

    def test_it_can_satisfy_the_handoff_groups_read_grant(self):
        # The other direction, and the reason effective_trustee() is applied
        # to the "is anything *missing*" checks and not only to the "is
        # anything extra" ones: an OWNER RIGHTS ACE on a group-owned handoff
        # directory really is the group's read grant.
        aces = [
            windows_acl.Ace(ACCOUNT, windows_acl.FILE_ALL_ACCESS),
            windows_acl.Ace(
                windows_acl.OWNER_RIGHTS_TRUSTEE, windows_acl.FILE_GENERIC_READ_EXECUTE
            ),
        ]

        assert windows_acl.handoff_problems(
            PATH, aces, service_account=ACCOUNT, service_group=GROUP, owner=f"MACHINE\\{GROUP}",
        ) == []


class TestOwnerProblems:
    """The check this module shipped without, because its own docstring
    claimed Windows had no ownership question to ask.

    It has a sharper one than POSIX: an object's owner holds ``WRITE_DAC``
    implicitly, so an owner outside this design does not merely have what
    the ACL grants — they can grant themselves the rest, with no elevation.
    And ``enable`` *moves* the data directory out of ``%LOCALAPPDATA%``,
    which preserves ownership, so without an explicit ``/setowner`` the
    separated root is owned by exactly the account being excluded.
    """

    def test_an_administrator_owned_root_is_clean(self):
        assert windows_acl.owner_problems(
            PATH, "BUILTIN\\Administrators", service_account=ACCOUNT
        ) == []

    def test_a_service_account_owned_root_is_clean(self):
        assert windows_acl.owner_problems(PATH, ACCOUNT, service_account=ACCOUNT) == []

    def test_a_human_owned_root_is_reported(self):
        problems = windows_acl.owner_problems(PATH, "MACHINE\\alice", service_account=ACCOUNT)

        assert len(problems) == 1
        assert "can rewrite its access-control list at will" in problems[0]
        assert "'MACHINE\\alice'" in problems[0]

    def test_an_unreadable_owner_is_skipped_rather_than_reported(self):
        # Best-effort, same posture as every other probe here: the process
        # running the audit may legitimately not be able to see a path.
        assert windows_acl.owner_problems(PATH, None, service_account=ACCOUNT) == []
