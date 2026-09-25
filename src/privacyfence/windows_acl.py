"""NTFS ACLs -- the net-new half of #428 Phase 4's Windows phase (B5c).

On macOS and Linux, Phase 4 (B5a/B5b) expressed its whole layout in POSIX
permission bits: ``0711`` on the root, ``0700`` on ``authority/``, ``3770``
on ``handoff/``. Windows has none of those. ``secure_files.secure_mkdir``'s
``chmod`` there is the documented no-op its own docstring describes, and
what protected the data directory until now was not a permission at all --
it was ``%LOCALAPPDATA%`` sitting inside the user's own profile, which is
exactly the protection privilege separation has to give up when the data
directory moves to ``%ProgramData%`` so a service account can own it.

So this module is the Windows half of the boundary, and it is a *different*
primitive rather than a translation of the same one:

=================  ==============================================  ==========================
POSIX (B5a/B5b)    Windows (B5c)                                   What it means
=================  ==============================================  ==========================
root ``0711``      ``Users:(X)``, no ``FILE_READ_DATA``            traverse, never enumerate
authority ``0700`` service account only                            policy/passkeys/audit key
handoff ``3770``   service account full, service group read        the two-account handoff
=================  ==============================================  ==========================

One asymmetry is deliberate and is not an oversight: ``handoff/`` is
group-*readable* here, not group-writable, where POSIX had to give the group
``rwx``. On POSIX the companion creates its own ``companion.sock`` in that
directory and the daemon has to be able to connect to it, and ``connect(2)``
on a socket node needs write permission. Windows' companion channel is a
named pipe in the ``\\\\.\\pipe\\`` namespace (``web/control_channel.py``),
not a file, so nothing in the user's session ever needs to create anything
under ``handoff/`` -- only to read ``mcp_url``, ``web_base_url`` and the
other discovery files. Granting less is free here, so it is granted.

## Two kinds of function

**Pure**, taking a list of ``Ace``: ``root_problems()``,
``authority_problems()``, ``handoff_problems()``. These are the whole audit,
they run on any platform, and they are what this repo's Ubuntu CI can
actually test -- a synthetic DACL is just a list of dataclasses.

**Impure**, touching pywin32: ``read_dacl()``, ``current_account_name()``,
``lookup_account_sid()``. Thin by construction, each one a single Win32 call
plus a SID-to-name lookup, and every one of them returns ``None`` rather
than raising on a platform or a path where the question has no answer.
``pywin32`` is not a new dependency -- ``web/control_channel.py``'s named
pipes already need it, and it arrives transitively via ``mcp`` regardless.

## Ownership is part of the answer, not a separate question

An object's owner implicitly holds ``WRITE_DAC`` on Windows, whatever its
DACL says -- so every grant below is advisory against whoever owns the
directory, and a well-formed ACL on a human-owned root is separation that
can be undone with one command and no elevation. ``read_owner()`` and
``owner_problems()`` cover that, and it matters here specifically because
``enable`` *moves* the data directory out of ``%LOCALAPPDATA%`` and a move
preserves ownership. The related trap is ``OWNER RIGHTS`` (see that
constant), an ACE that grants the owner rather than any fixed principal.

## What "trusted" means here

``SYSTEM`` and ``Administrators`` are ignored by every check below, which is
a claim worth making explicitly rather than by omission: they are the
service manager and the account that provisioned the install, and issue
#428's own "Honest limits" already concedes that a local Administrator
defeats the whole design by taking ownership. Reporting them would report
the design as a defect on every startup, exactly as auditing ``handoff/``
against a flat ``0700`` would have on POSIX (see ``daemon_main.py``'s
SEC-09 check). Everything else -- the logged-in user, ``Users``,
``Authenticated Users``, ``Everyone``, a stale ``CREATOR OWNER`` ACE that
means ``icacls /inheritance:r`` did not take -- is reported.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Win32 access-mask bits (winnt.h). Spelled out here rather than imported
# from ``ntsecuritycon`` so the pure half of this module -- every audit
# function below, and therefore every test of one -- imports on Linux and
# macOS too. The values are ABI, not configuration; they have not changed
# since Windows NT 3.1.
FILE_READ_DATA = 0x0001  # on a directory: list its entries
FILE_WRITE_DATA = 0x0002  # on a directory: create a file in it
FILE_APPEND_DATA = 0x0004  # on a directory: create a subdirectory in it
FILE_READ_EA = 0x0008
FILE_WRITE_EA = 0x0010
FILE_EXECUTE = 0x0020  # on a directory: traverse it -- the 0711 "x" bit
FILE_DELETE_CHILD = 0x0040
FILE_READ_ATTRIBUTES = 0x0080
FILE_WRITE_ATTRIBUTES = 0x0100
DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
SYNCHRONIZE = 0x00100000
GENERIC_ALL = 0x10000000
GENERIC_EXECUTE = 0x20000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000

FILE_ALL_ACCESS = 0x1F01FF
# What ``icacls <dir> /grant "<who>":(RX)`` produces, and what the group
# needs on ``handoff/``: read plus traverse, no write of any kind.
FILE_GENERIC_READ_EXECUTE = 0x1200A9
# What ``icacls <dir> /grant "<who>":(X)`` produces: traverse only, which is
# the whole of what the root grants and the exact Windows spelling of
# "``0711``: traversable by anyone, listable by no one".
FILE_TRAVERSE_ONLY = 0x100020

_READ_BITS = FILE_READ_DATA | GENERIC_READ | GENERIC_ALL
_WRITE_BITS = (
    FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES
    | FILE_DELETE_CHILD | DELETE | WRITE_DAC | WRITE_OWNER | GENERIC_WRITE | GENERIC_ALL
)
_TRAVERSE_BITS = FILE_EXECUTE | GENERIC_EXECUTE | GENERIC_ALL

# Never reported by the checks below -- see this module's own docstring for
# why each one is here rather than merely unmentioned.
#
# NT SERVICE\TrustedInstaller belongs here for the same reason SYSTEM and
# Administrators do: it is what *grants* write access to a protected system
# folder, not an instance of an untrusted one holding it. TrustedInstaller,
# not Administrators, owns %ProgramFiles% by default and is the one
# principal with unconditional write there -- that is what stops an
# ordinary Administrator token from touching it without first taking
# ownership, i.e. it is this design's guarantee working as intended, not a
# hole in it. Without this, image_problems() (and the .ps1's own
# Assert-ImageProtected, which mirrors this list under its own name) flagged
# every install into the installer's own default location as unprotected --
# "... is writable by 'NT SERVICE\\TrustedInstaller'" -- which is the one
# layout the installer's own error message tells the user to keep.
TRUSTED_TRUSTEES = ("NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "NT SERVICE\\TrustedInstaller")

# S-1-3-4. Not a principal at all: an ACE naming it grants its rights to
# whoever currently *owns* the object, whoever that turns out to be. Windows
# puts one on directories under a user profile, it survives
# ``icacls /inheritance:r`` (it is not an inherited ACE -- the file system
# materializes it at creation), and a real ``platform-windows`` run is what
# established both of those facts rather than any documentation.
#
# It therefore cannot be judged on its own: ``OWNER RIGHTS:(F)`` is harmless
# on a directory an administrator owns and a complete bypass on one the
# logged-in human owns. Every check below resolves it against the object's
# real owner before deciding -- see ``effective_trustee()`` -- and
# ``owner_problems()`` is what makes sure that owner is someone this design
# can accept in the first place.
OWNER_RIGHTS_TRUSTEE = "OWNER RIGHTS"


@dataclass(frozen=True)
class Ace:
    """One access-control entry, reduced to the four things any check here
    asks about. Deliberately not a wrapper around a pywin32 ACE object: the
    audit functions below have to be constructible from a test on a machine
    that has no such object, and the real reader (``read_dacl()``) is what
    flattens one into this."""

    trustee: str
    mask: int
    allowed: bool = True
    inherited: bool = False

    def grants_read(self) -> bool:
        """True when this ACE would let its trustee *enumerate* a directory
        -- the distinction the root's ``0711`` equivalent turns on, where
        traversing is allowed and listing is not."""
        return self.allowed and bool(self.mask & _READ_BITS)

    def grants_write(self) -> bool:
        return self.allowed and bool(self.mask & _WRITE_BITS)

    def grants_traverse(self) -> bool:
        return self.allowed and bool(self.mask & _TRAVERSE_BITS)

    def grants_anything(self) -> bool:
        return self.allowed and bool(self.mask & (_READ_BITS | _WRITE_BITS | _TRAVERSE_BITS))


def normalize_trustee(trustee: str) -> str:
    """Windows account names are case-insensitive, and the same account
    reaches these checks spelled three ways depending on who wrote it down
    -- ``NT SERVICE\\PrivacyFence`` from the marker file, ``NT
    Service\\privacyfence`` from ``LookupAccountSid``, ``PrivacyFence`` from
    a hand-edited ``icacls`` line. Compare them folded, and treat a bare
    name as matching a qualified one only through
    ``trustee_matches()`` below."""
    return trustee.strip().replace("/", "\\").casefold()


def trustee_matches(ace_trustee: str, expected: str) -> bool:
    """``expected`` may be qualified (``NT SERVICE\\PrivacyFence``) or bare
    (``PrivacyFenceUsers``, a local group whose domain is the machine name
    and therefore not something this code can spell in advance). A bare
    expectation matches on the account part alone; a qualified one has to
    match in full, so ``NT SERVICE\\PrivacyFence`` is never satisfied by a
    *local* account someone created with the same name."""
    ace_trustee = normalize_trustee(ace_trustee)
    expected = normalize_trustee(expected)
    if "\\" in expected:
        return ace_trustee == expected
    return ace_trustee.rsplit("\\", 1)[-1] == expected


def is_trusted(trustee: str) -> bool:
    return any(trustee_matches(trustee, known) for known in TRUSTED_TRUSTEES)


def effective_trustee(trustee: str, owner: str | None) -> str:
    """Who an ACE actually grants to.

    Itself, for every ordinary trustee. For ``OWNER RIGHTS`` (see that
    constant) it is the object's current owner, so the same ACE is read as
    ``BUILTIN\\Administrators`` on an administrator-owned directory and as
    ``MACHINE\\alice`` on one alice owns -- which is exactly the difference
    between "fine" and "privilege separation is not in effect".

    An unknown owner (``read_owner()`` could not answer) leaves the trustee
    as the literal ``OWNER RIGHTS``, which no check treats as trusted. That
    is the deliberate direction to fail in: reporting a grant that may turn
    out to be harmless costs a confusing log line, and silently accepting
    one that may be a bypass costs the whole feature.
    """
    if owner is not None and trustee_matches(trustee, OWNER_RIGHTS_TRUSTEE):
        return owner
    return trustee


def describe_ace(ace: Ace, owner: str | None) -> str:
    """How an ACE is named in a finding. Identical to its trustee, except
    for an ``OWNER RIGHTS`` ACE, where naming only the literal trustee would
    send a reader looking for an account that does not exist -- and naming
    only the owner would hide which ACE to actually remove."""
    resolved = effective_trustee(ace.trustee, owner)
    if resolved == ace.trustee:
        return f"'{ace.trustee}'"
    return f"'{ace.trustee}' (which grants this object's owner, '{resolved}')"


def _unexpected(
    aces: list[Ace], allowed_trustees: tuple[str, ...], owner: str | None = None,
) -> list[Ace]:
    return [
        ace for ace in aces
        if ace.grants_anything()
        and not is_trusted(effective_trustee(ace.trustee, owner))
        and not any(
            trustee_matches(effective_trustee(ace.trustee, owner), expected)
            for expected in allowed_trustees
        )
    ]


def root_problems(
    path: Path, aces: list[Ace], *, service_account: str, owner: str | None = None,
) -> list[str]:
    """The Windows reading of ``0711``: anyone may traverse the root to
    reach ``handoff/``, nobody but the service account may enumerate what is
    in it.

    An ACE that grants a non-service principal ``FILE_READ_DATA`` is the one
    real defect here, and it is also the likeliest one to exist by accident:
    ``%ProgramData%`` grants ``Users`` read-and-execute by inheritance, so a
    directory created under it *without* ``icacls /inheritance:r`` is
    readable by every account on the machine before anyone touches it. This
    is the check that notices that the installer's very first step did not
    happen.
    """
    problems = []
    for ace in aces:
        resolved = effective_trustee(ace.trustee, owner)
        if is_trusted(resolved) or trustee_matches(resolved, service_account):
            continue
        if ace.grants_read():
            problems.append(
                f"{path} grants {describe_ace(ace, owner)} permission to list its contents "
                f"(mask {ace.mask:#010x}) -- the separated root is meant to be traversable "
                "but not enumerable, so everything under it is discoverable by any account "
                "on this machine."
            )
        elif ace.grants_write():
            problems.append(
                f"{path} grants {describe_ace(ace, owner)} write access (mask "
                f"{ace.mask:#010x}) -- only the '{service_account}' account should be able to "
                "change anything under the separated root."
            )
    if not any(
        trustee_matches(effective_trustee(ace.trustee, owner), service_account)
        and ace.grants_write()
        for ace in aces
    ):
        problems.append(
            f"{path} grants '{service_account}' no write access -- the daemon cannot own its "
            "own data directory, so this install is not actually separated."
        )
    return problems


def authority_problems(
    path: Path, aces: list[Ace], *, service_account: str, owner: str | None = None,
) -> list[str]:
    """The Windows reading of ``0700``, and the check that actually matters:
    ``authority/`` holds the policy the agent may not edit, the WebAuthn
    store #426 depends on being unforgeable, and the audit log's HMAC key.
    Any ACE at all for anything but the service account defeats the whole
    phase, so unlike the root this does not distinguish read from write --
    reading ``settings.yaml`` is not harmless, it tells an agent exactly
    which approvals it can already grant itself.
    """
    return [
        f"{path} grants {describe_ace(ace, owner)} access (mask {ace.mask:#010x}) -- the "
        f"human-authority files are meant to be reachable only by the '{service_account}' "
        "account, so the privilege separation this install advertises is not actually in "
        "effect."
        for ace in _unexpected(aces, (service_account,), owner)
    ]


def handoff_problems(
    path: Path,
    aces: list[Ace],
    *,
    service_account: str,
    service_group: str,
    owner: str | None = None,
) -> list[str]:
    """``handoff/`` is deliberately not a boundary (ADR 0002 decision 6), so
    this checks that it is *open enough* as much as that it is not open too
    far: the ``.mcpb`` shim has to be able to read ``mcp_url`` there, and
    the companion ``web_base_url``, or a separated install is simply broken
    in a way no error message would explain. (The MCP token itself is not
    there on a separated install: it lives under the authority directory
    and clients mint it over the control channel.)

    Write is the one thing the group must not have. Nothing in the user's
    session creates anything here on Windows -- both control channels are
    named pipes rather than socket files (see this module's docstring) -- so
    a group ACE carrying ``FILE_WRITE_DATA`` would let the agent replace the
    discovery files the companion and the MCPB shim read, for nothing gained.
    """
    problems = [
        f"{path} grants {describe_ace(ace, owner)} access (mask {ace.mask:#010x}) -- the "
        f"handoff directory is shared between the '{service_account}' account and the "
        f"'{service_group}' group only."
        for ace in _unexpected(aces, (service_account, service_group), owner)
    ]
    for ace in aces:
        if trustee_matches(effective_trustee(ace.trustee, owner), service_group) and ace.grants_write():
            problems.append(
                f"{path} grants the '{service_group}' group write access (mask "
                f"{ace.mask:#010x}) -- it needs to read what the daemon publishes there, never "
                "to rewrite it."
            )
    if not any(
        trustee_matches(effective_trustee(ace.trustee, owner), service_group)
        and ace.grants_read()
        for ace in aces
    ):
        problems.append(
            f"{path} grants the '{service_group}' group no read access -- the companion app "
            "and the MCP extension cannot read mcp_url, web_base_url or the other discovery "
            "files, so the daemon will look like it is not running."
        )
    return problems


def owner_problems(path: Path, owner: str | None, *, service_account: str) -> list[str]:
    """The Windows counterpart of ``privilege_separation._authority_owner_
    problem()``, and the check this module shipped without because its own
    docstring claimed Windows had no ownership question to ask. It does, and
    it is sharper than the POSIX one.

    **An object's owner always implicitly holds ``WRITE_DAC``**, whatever
    the DACL says. So an owner outside this design does not merely have
    whatever the ACL grants them -- they can grant themselves the rest, with
    no elevation and nothing to stop them. Every ``icacls`` line the
    installer writes is advisory against the owner.

    That is not theoretical here, and it is the reason this check exists:
    ``enable`` *moves* the data directory out of ``%LOCALAPPDATA%``, and a
    move preserves ownership -- so without an explicit ``/setowner`` the
    separated root ends up owned by the very human account the separation is
    supposed to exclude, with a perfect-looking ACL on top of it.
    ``scripts/windows_privilege_separation.ps1`` sets the owner to
    ``Administrators``; this is what notices when that did not happen, or was
    undone later.

    An owner that cannot be read is not reported -- same best-effort posture
    as every other probe here.
    """
    if owner is None or is_trusted(owner) or trustee_matches(owner, service_account):
        return []
    return [
        f"{path} is owned by '{owner}', not by '{service_account}' or an administrator -- an "
        "object's owner can rewrite its access-control list at will, so every permission this "
        "install relies on is one command away from being undone by the account it is meant to "
        "exclude."
    ]


def image_problems(path: Path, aces: list[Ace], *, service_account: str) -> list[str]:
    """The Windows half of this weakness: a service runs whatever its
    ``binPath`` names, so an image the logged-in user can rewrite is not
    privilege separation -- it is a way for the agent to execute its own
    code *as the service account*, which is strictly worse than the
    unseparated install it replaced. ``privilege_separation.
    _posix_image_problems()`` is the POSIX half, added by B1 once it turned
    out ``/Applications`` does not put a drag-installed ``.app`` somewhere
    root owns the way ``/opt/privacyfence`` (dpkg-owned) does -- ADR 0002
    §5a asserted otherwise and was wrong.

    §5a's own answer to this on Windows was a second install tier: an
    elevated per-machine install under ``%ProgramFiles%`` that could be
    separated, and a non-elevated per-user one under
    ``%LOCALAPPDATA%\\Programs`` (#407) that could not. ADR 0003 decision 4
    withdraws the tier rather than the requirement -- ``installer/
    privacyfence.iss`` is ``PrivilegesRequired=admin`` and runs ``enable``
    itself, so every shipped install lands somewhere only administrators
    can write.

    That makes this a check on what happened to an install *afterwards*
    rather than on how it was made, which is why it was always checked and
    not documented: ``scripts/windows_privilege_separation.ps1`` refuses to
    enable against a user-writable image, and ``privilege_separation.
    audit_layout()`` re-checks it on every start in case the install was
    later replaced in place.
    """
    return [
        f"{path} is writable by '{ace.trustee}' (mask {ace.mask:#010x}) -- the daemon runs this "
        f"image as '{service_account}', so anything that can rewrite it can run code as that "
        "account. A privilege-separated install has to live somewhere only administrators can "
        "write, which is where the PrivacyFence installer puts one."
        for ace in aces
        if ace.grants_write() and not is_trusted(ace.trustee)
        and not trustee_matches(ace.trustee, service_account)
    ]


# --------------------------------------------------------------------------- #
# The impure half: three Win32 lookups, each one best-effort.
# --------------------------------------------------------------------------- #

def describe_sid(sid) -> str:  # noqa: ANN001 -- a pywin32 PySID, no type stub
    """``DOMAIN\\Name`` for a SID that resolves, and the SID's own string
    form (``S-1-5-21-...``) for one that doesn't -- an account deleted after
    the ACE was written, or a domain controller that isn't reachable. Never
    raises: every caller here is an audit or a log line, and "I could not
    name this trustee" has to still produce a finding that names *something*
    rather than no finding at all."""
    import win32security

    try:
        name, domain, _type = win32security.LookupAccountSid(None, sid)
    except Exception:  # pragma: no cover -- unresolvable SID, machine-dependent
        try:
            return win32security.ConvertSidToStringSid(sid)
        except Exception:
            return "<unknown>"
    return f"{domain}\\{name}" if domain else name


def read_owner(path: Path) -> str | None:
    """``path``'s owning principal as ``DOMAIN\\Name``, or None where that
    cannot be read (not Windows, no such path, no permission). Feeds both
    ``owner_problems()`` and every ``OWNER RIGHTS`` resolution above."""
    try:
        import win32security

        descriptor = win32security.GetFileSecurity(
            str(path), win32security.OWNER_SECURITY_INFORMATION
        )
        sid = descriptor.GetSecurityDescriptorOwner()
    except Exception as exc:
        logger.debug("Could not read the owner of %s: %s", path, exc)
        return None
    return None if sid is None else describe_sid(sid)


def read_dacl(path: Path) -> list[Ace] | None:
    """Every ACE on ``path``'s discretionary ACL, or None when the question
    has no answer: not Windows, the path doesn't exist, or this process
    cannot read the security descriptor.

    A *NULL* DACL -- which grants everyone everything -- also returns None
    rather than an empty list, and that difference matters: an empty list
    means "nobody has access", a NULL DACL means the opposite. Callers treat
    None as "could not check", the same best-effort posture every other
    permission check in this codebase takes (``secure_files.audit_directory_
    permissions`` skips a path it cannot ``stat``), so a NULL DACL is
    reported by ``has_null_dacl()`` below instead.

    The import is inside the ``try`` on purpose. pywin32 is required on
    Windows and arrives transitively besides, but ``audit_layout()`` calls
    this on every daemon start -- and a startup check that *crashes* on a
    build missing an optional-looking import would be a worse outcome than
    one that reports "could not check", which is what every other branch
    here already does.
    """
    try:
        import win32security


        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
    except Exception as exc:
        logger.debug("Could not read the DACL of %s: %s", path, exc)
        return None
    if dacl is None:
        return None
    aces = []
    for index in range(dacl.GetAceCount()):
        (ace_type, ace_flags), mask, sid = dacl.GetAce(index)
        aces.append(
            Ace(
                trustee=describe_sid(sid),
                mask=mask,
                allowed=ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE,
                inherited=bool(ace_flags & win32security.INHERITED_ACE),
            )
        )
    return aces


def has_null_dacl(path: Path) -> bool:
    """True only for the one case ``read_dacl()`` cannot express: a security
    descriptor that has a DACL field set to NULL, which grants every account
    on the machine full control. Rare, and never something this repo's own
    installer produces -- worth one explicit check rather than silently
    reading as "could not check"."""
    try:
        import win32security

        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
    except Exception:
        return False
    return descriptor.GetSecurityDescriptorDacl() is None


def current_account_name() -> str:
    """This process's own account, as ``DOMAIN\\Name``.

    ``%USERNAME%`` is not usable for this, which is the whole reason this
    function exists: a service running under a *virtual* account
    (``NT SERVICE\\PrivacyFence``) gets an environment block whose
    ``USERNAME`` is the machine's own name with a ``$``, not the account the
    token actually holds. ``privilege_separation.check_runtime_identity()``
    compares this against the marker's ``service_account``, and a wrong
    answer there means a daemon that refuses to start for the right reason
    with the wrong explanation.
    """
    import win32api
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
    return describe_sid(sid)


def lookup_account_sid(name: str):  # noqa: ANN201 -- a pywin32 PySID, no type stub
    """The SID for ``name`` (``NT SERVICE\\PrivacyFence``,
    ``PrivacyFenceUsers``), or None if no such account exists on this
    machine -- which is the ordinary case on an unseparated install, where
    neither has been created. ``web/control_channel.py`` uses this to build
    the named pipes' security descriptors and simply omits an ACE it cannot
    resolve."""
    try:
        import win32security

        sid, _domain, _type = win32security.LookupAccountName(None, name)
    except Exception as exc:
        logger.debug("Could not resolve the account %r: %s", name, exc)
        return None
    return sid


__all__ = [
    "Ace",
    "FILE_ALL_ACCESS",
    "FILE_GENERIC_READ_EXECUTE",
    "FILE_TRAVERSE_ONLY",
    "OWNER_RIGHTS_TRUSTEE",
    "TRUSTED_TRUSTEES",
    "authority_problems",
    "current_account_name",
    "describe_ace",
    "describe_sid",
    "effective_trustee",
    "handoff_problems",
    "has_null_dacl",
    "image_problems",
    "is_trusted",
    "lookup_account_sid",
    "normalize_trustee",
    "owner_problems",
    "read_dacl",
    "read_owner",
    "root_problems",
    "trustee_matches",
]
