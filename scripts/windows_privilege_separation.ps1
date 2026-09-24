<#
.SYNOPSIS
  #428 Phase 4 (B5c): provision -- or back out of -- running the PrivacyFence
  daemon under its own Windows account.

.DESCRIPTION
  Until this runs, the daemon and the AI agent it exists to govern are the same
  OS user, which is the root cause of all four weaknesses issue #428 describes:
  the agent can read the session-minting channel, rewrite the always-allow rules
  and PII policy that decide what it is allowed to do, forge a WebAuthn
  credential into the store a local passkey would be checked against, and read
  the audit log's HMAC key. One change closes all four -- a dedicated account
  owning those files -- and this script is that change, made reversible.

  macOS and Linux got the same change in their own idioms
  (scripts/macos_privilege_separation.sh, scripts/linux_privilege_separation.sh)
  and this deliberately mirrors them step for step. Three things have no POSIX
  counterpart at all, and they are why Windows was sequenced last:

  1. NTFS ACLs instead of permission bits. secure_files.secure_mkdir's chmod is
     a documented no-op here, so every "mode" in the POSIX layout becomes an
     icacls grant -- see src/privacyfence/windows_acl.py, which is both the
     translation table and the audit that reads it back.
  2. A service host. The Service Control Manager does not just launch a binary,
     it waits to be called back, so the daemon is started as
     `privacyfence-app.exe --windows-service` (src/privacyfence/windows_service.py)
     rather than as the plain executable a LaunchDaemon or a systemd unit runs.
  3. The install location is part of the boundary. A service runs whatever its
     binPath names, so a PrivacyFence the logged-in user can rewrite would let
     the agent run its own code *as the service account*. This script refuses to
     enable against such an install -- see Assert-ImageProtected below. #407's
     non-elevated per-user install tier was the case that made that refusal
     reachable by an ordinary user; ADR 0003 decision 4 withdraws the tier
     rather than the refusal, which stays as the check on every install
     directory this is ever pointed at.

.EXAMPLE
  # From an elevated PowerShell, against a real install:
  powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" enable
  ... status
  ... uninstall                # stop and remove the service; keep the data
  ... uninstall -Purge         # ...and delete the data, marker and group too
  ... enable -ForUser alice   # just the per-user half, for a second account

.NOTES
  `uninstall` is what the Inno Setup uninstaller runs (ADR 0042, following
  ADR 0041's "no upgrade path from earlier layouts"). It stops and
  unregisters the service and the companion task and leaves everything under
  %ProgramData%\PrivacyFence -- data, marker, $ServiceGroup -- where it is,
  so a reinstall picks the data straight back up. `-Purge` (the POSIX
  scripts spell it `--purge`) also deletes that directory and the group.
  Nothing here ever moves data back into a user profile.

  Adding the owner to $ServiceGroup is the only step here that needs to know
  *which human* this install is for, and ADR 0003 decision 3 splits it out
  for that reason: an MDM push or
  a SYSTEM-context install resolves no owner account, and that used to leave
  the whole install unseparated. It no longer does. `enable` with no
  resolvable owner does everything an administrator can do alone and records
  the group membership as pending; `enable -ForUser <name>` closes that half
  later, idempotently, and is what the companion app runs by itself at the
  first real sign-in.

  No longer opt-in. ADR 0003 decision 4 has installer/privacyfence.iss run
  `enable` itself, elevated, as a step of every install -- so on Windows this
  script is normally something a human runs only to look at an install
  (`status`) or to purge one (`uninstall -Purge`), the same way the .deb's postinst
  has run the Linux script since #428 D1. Running `enable` by hand still
  works, and is the documented way to re-provision an install whose service,
  ACLs or companion task have drifted.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('enable', 'uninstall', 'status', 'daemon')]
    [string] $Command,

    # The human account that owns this install. Defaults to whoever is running
    # this script, which is right for the ordinary case (UAC's elevation keeps
    # the same user); pass it explicitly when elevating into a *different*
    # administrator account, or the wrong account would be added to
    # $ServiceGroup.
    [string] $User,

    # enable only: run *just* the per-user half for this account, against an
    # install the machine half has already separated (ADR 0003 decision 3).
    # The POSIX scripts spell it `enable --for-user <name>`. Works for any
    # number of accounts on the same machine, not just this install's first
    # (recorded) owner -- each gets its own isolated PrivacyFence identity,
    # never merged with anyone else's (docs/adr/0008-one-principal-per-os-
    # user.md).
    [string] $ForUser,

    [string] $DaemonExec,
    [string] $CompanionExec,

    # uninstall only: also delete %ProgramData%\PrivacyFence (data and marker)
    # and the $ServiceGroup local group. The POSIX scripts' `uninstall --purge`.
    [switch] $Purge,

    # #428 Phase 2: `daemon`'s own sub-verb -- {status|start|stop|restart|
    # ensure-running}. Position = 1 (the only other positional parameter
    # this script has) is what lets `service_control.py`'s elevated
    # `_windows_runas_argv(script, "daemon $action", transcript)` work
    # exactly the way the POSIX scripts' own `<script> daemon <action>` does,
    # with no `-Command`/`-DaemonSubcommand` names needed on the command
    # line: `<script> daemon start` binds $Command='daemon' and
    # $DaemonSubcommand='start' purely by position, since $User/$ForUser/
    # $DaemonExec/$CompanionExec above carry no Position of their own and so
    # are never candidates for positional binding in the first place.
    # ValidateSet only runs against a value PowerShell actually binds, so
    # leaving this unset for `enable`/`uninstall`/`status` (its default, an
    # empty string) never trips it.
    [Parameter(Position = 1)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'ensure-running')]
    [string] $DaemonSubcommand
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Reading an ACL without Microsoft.PowerShell.Security ──────────────────
#
# This script does not use `Get-Acl`, and deliberately so. Three consecutive
# release builds were lost to that one cmdlet being unavailable inside the
# installer's own `powershell -ExecutionPolicy Bypass -File` invocation on a
# stock GitHub Actions windows-latest (Server 2025) runner, and the module it
# lives in refusing to load by two different routes:
#
#   v4.1.0b1  calling Get-Acl        -> CommandNotFoundException,
#                                       "the 'Get-Acl' command was found in the
#                                       module 'Microsoft.PowerShell.Security',
#                                       but the module could not be loaded"
#   v4.1.0b2  Import-Module ... -ErrorAction Stop
#                                    -> FormatXmlUpdateException,
#                                       "The member 'AuditToString' is already
#                                       present"
#   v4.1.0b3  that same import, with FormatXmlUpdateException swallowed as
#             "already loaded, import redundant"
#                                    -> the import is silently skipped and
#                                       Get-Acl still fails exactly as in b1
#
# b3's swallow was the wrong read of b2: a partially-registered module is not
# a loaded one, so tolerating the duplicate-type-data error moved the failure
# from the import line to the first call site without making anything work.
# Each fix addressed the symptom it had just seen and was overtaken by the
# next shape of the same underlying problem, so this stops depending on that
# module being loadable at all.
#
# `GetAccessControl()` on the FileInfo/DirectoryInfo `Get-Item` already
# returns is plain .NET Framework, reachable from Windows PowerShell 5.1 with
# no module import at all, and `GetOwner`/`GetAccessRules` are real methods on
# the FileSecurity/DirectorySecurity it hands back -- taking the same
# arguments the `.Owner`/`.Access` properties pass for you. So the two helpers
# below need nothing from Microsoft.PowerShell.Security: not the cmdlet, and
# not its type data either. Everything in this script reads an ACL through
# them, which is the whole of the dependency.

function Get-PathOwner {
    <#
      .SYNOPSIS
      The NT account name owning $LiteralPath -- exactly what
      `(Get-Acl $p).Owner` returned, including raising on an owner SID that
      resolves to no account, which is the one behaviour every caller's
      Test-TrustedIdentity comparison was already written against.
    #>
    param([Parameter(Mandatory = $true)][string] $LiteralPath)
    $security = (Get-Item -LiteralPath $LiteralPath -Force).GetAccessControl()
    return $security.GetOwner([System.Security.Principal.NTAccount]).Value
}

function Get-PathAccessRules {
    <#
      .SYNOPSIS
      The access rules on $LiteralPath -- exactly what `(Get-Acl $p).Access`
      returned, down to the arguments: inherited rules included and
      identities resolved to NTAccount names, which is the shape
      Test-RuleGrantsWrite/Test-RuleGrantsRead and every
      `$_.IdentityReference.Value` comparison below are written against.
    #>
    param([Parameter(Mandatory = $true)][string] $LiteralPath)
    $security = (Get-Item -LiteralPath $LiteralPath -Force).GetAccessControl()
    return @($security.GetAccessRules($true, $true, [System.Security.Principal.NTAccount]))
}

# ── Constants. Every one of these is also declared in
#    src/privacyfence/privilege_separation.py, and
#    tests/unit/test_privilege_separation.py asserts the two agree -- this
#    script and that module are the two halves of one contract, and a silent
#    drift between them would leave a daemon looking for its data somewhere
#    the installer never put it. ──────────────────────────────────────────────
$ServiceName = 'PrivacyFence'
# Not a choice: Windows derives a virtual service account's name from the
# service's own. Creating the service with this as its account is what brings
# the account into existence -- there is no net user / useradd step here.
$ServiceAccount = "NT SERVICE\$ServiceName"
# The stand-in for the POSIX service *group*. A virtual service account cannot
# hold secondary group memberships, so unlike macOS/Linux the daemon is not a
# member of this group -- every ACL below names both principals.
$ServiceGroup = 'PrivacyFenceUsers'
$SystemRoot = Join-Path $env:ProgramData 'PrivacyFence'
$MarkerName = 'privilege-separation.json'
$MarkerVersion = 1
$HandoffDirName = 'handoff'
$AuthorityDirName = 'authority'
$CompanionTaskName = 'PrivacyFenceCompanion'

# Well-known SIDs rather than names, everywhere a built-in principal is named.
# "BUILTIN\Users" is "BUILTIN\Utilisateurs" on a French Windows and icacls
# would reject it; the SID is the same string on every install and every
# locale. The same reason installers have used *S-1-5-32-544 for decades.
$SidSystem = '*S-1-5-18'
$SidAdministrators = '*S-1-5-32-544'
$SidUsers = '*S-1-5-32-545'

$DefaultInstallDir = Join-Path $env:ProgramFiles 'PrivacyFence'
$DefaultDaemonExecName = 'privacyfence-app.exe'
$DefaultCompanionExecName = 'PrivacyFenceCompanion.exe'

# This script runs from two places: a source checkout (installer/windows/ sits
# next to scripts/) and a real install, where privacyfence.iss copies it next
# to the application as privilege-separation.ps1 with its template alongside.
# Checking the checkout layout first means a developer's edits to the template
# take effect without reinstalling anything, while a packaged install -- the
# only one most Windows users have -- still finds it.
$RepoRoot = Split-Path -Parent $PSScriptRoot
$CheckoutTemplateDir = Join-Path $RepoRoot 'installer\windows'
if (Test-Path -LiteralPath $CheckoutTemplateDir) {
    $TemplateDir = $CheckoutTemplateDir
} else {
    $TemplateDir = $PSScriptRoot
}

$script:OwnerUser = $User
$script:OwnerSid = $null
# Whether Resolve-Owner actually found a human account, as opposed to leaving
# $OwnerUser at whatever name it started from. The POSIX scripts express the
# same thing by leaving $OWNER_USER empty, which they can because their owner
# only ever comes from $SUDO_USER; here it defaults to the current identity,
# so "resolved" has to be its own flag (ADR 0003 decision 3's machine half is
# the caller that has to be able to tell).
$script:OwnerResolved = $false

# Windows' answer to the POSIX scripts' "root is never the owner" refusal.
# An install provisioned from a SYSTEM context -- an MDM push, a deployment
# tool, a service -- resolves one of these, and adding it to $ServiceGroup
# would hand the handoff directory to every service on the machine while
# recording a marker that claims a human owns this install.
$NonHumanSids = @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')

# Every line carries the wall-clock time it was written. Setup captures
# `enable`'s output into a file and copies it into its own log only after the
# script exits (installer/privacyfence.iss's LogCommandOutput), so every one of
# those lines gets the same Inno timestamp -- this is the only record of where
# inside `enable` an install's time actually went.
function Get-NoteTime { return (Get-Date).ToString('HH:mm:ss.fff') }
function Write-Note { param([string] $Message) Write-Host "[$(Get-NoteTime)] -> $Message" }
function Write-Warn { param([string] $Message) Write-Warning "[$(Get-NoteTime)] $Message" }
function Stop-WithError { param([string] $Message) throw $Message }

function Assert-Windows {
    # $env:OS rather than the $IsWindows automatic variable: that one does not
    # exist in Windows PowerShell 5.1 (only in PowerShell 7+), and reading an
    # undefined variable under Set-StrictMode is itself an error -- so the
    # check meant to produce a clear message would throw an obscure one on
    # exactly the shell every Windows install ships with.
    if ($env:OS -ne 'Windows_NT') {
        Stop-WithError 'this script is Windows-only (macOS is scripts/macos_privilege_separation.sh, Linux scripts/linux_privilege_separation.sh)'
    }
}

function Assert-Administrator {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Stop-WithError 'run this from an elevated PowerShell -- creating a service and rewriting ACLs under %ProgramData% both need administrator rights'
    }
}

function Resolve-Owner {
    param([switch] $Optional)

    if (-not $script:OwnerUser) {
        # GetCurrent().Name is DOMAIN\user; the account part is what
        # Get-LocalUser and the group membership below both want.
        $script:OwnerUser = ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name -split '\\')[-1]
    }
    try {
        $account = New-Object System.Security.Principal.NTAccount($script:OwnerUser)
        $script:OwnerSid = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        if ($Optional) { return }
        Stop-WithError "no such account: $($script:OwnerUser) -- pass -User <name>"
    }
    if ($NonHumanSids -contains $script:OwnerSid) {
        if ($Optional) { return }
        Stop-WithError "-User must be a real sign-in account, not $($script:OwnerUser)"
    }
    $script:OwnerResolved = $true
}

function Get-MarkerOwnerUser {
    # The one field of the marker `status` reads back. ConvertFrom-Json is in
    # Windows PowerShell 5.1, so unlike the POSIX scripts this needs no
    # line-oriented workaround -- but like them it must not throw on a marker
    # a hand-edit has broken, since status is the thing you run to find that
    # out.
    $marker = Join-Path $SystemRoot $MarkerName
    if (-not (Test-Path -LiteralPath $marker)) { return $null }
    try {
        return (Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).owner_user
    } catch {
        return $null
    }
}

function Resolve-Executables {
    if (-not $DaemonExec) {
        # Next to this script first: that is where a real install puts both,
        # and it is right even when PrivacyFence was installed somewhere other
        # than %ProgramFiles%.
        $candidate = Join-Path $PSScriptRoot $DefaultDaemonExecName
        if (Test-Path -LiteralPath $candidate) {
            $script:DaemonExec = $candidate
        } else {
            $script:DaemonExec = Join-Path $DefaultInstallDir $DefaultDaemonExecName
        }
    } else {
        $script:DaemonExec = $DaemonExec
    }
    if (-not $CompanionExec) {
        $candidate = Join-Path (Split-Path -Parent $script:DaemonExec) $DefaultCompanionExecName
        $script:CompanionExec = $candidate
    } else {
        $script:CompanionExec = $CompanionExec
    }
    if (-not (Test-Path -LiteralPath $script:DaemonExec)) {
        Stop-WithError "not found: $($script:DaemonExec) -- pass -DaemonExec for an install somewhere else"
    }
    # The companion is what a human uses once the daemon has no desktop session
    # of its own, and on Windows it is also the only thing that can open a
    # browser for connector OAuth (ADR 0002 decision 5 -- session 0 isolation).
    # A separated install without one is a locked door. Refuse rather than
    # install half of the inversion.
    if (-not (Test-Path -LiteralPath $script:CompanionExec)) {
        Stop-WithError "not found: $($script:CompanionExec) -- a separated install needs the companion app (ADR 0002 decision 2)"
    }
}

function Test-RuleGrantsWrite {
    param([System.Security.AccessControl.FileSystemAccessRule] $Rule)

    if ($Rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { return $false }
    $write = [System.Security.AccessControl.FileSystemRights]::WriteData `
        -bor [System.Security.AccessControl.FileSystemRights]::AppendData `
        -bor [System.Security.AccessControl.FileSystemRights]::Delete `
        -bor [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles `
        -bor [System.Security.AccessControl.FileSystemRights]::ChangePermissions `
        -bor [System.Security.AccessControl.FileSystemRights]::TakeOwnership
    return (($Rule.FileSystemRights -band $write) -ne 0)
}

function Test-RuleGrantsRead {
    param([System.Security.AccessControl.FileSystemAccessRule] $Rule)

    if ($Rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { return $false }
    return (($Rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::ReadData) -ne 0)
}

function Test-TrustedIdentity {
    param([string] $Identity)

    # SYSTEM and Administrators are ignored everywhere in this script, for the
    # reason windows_acl.py's own docstring gives: they are the service manager
    # and the account that provisioned the install, and issue #428's "Honest
    # limits" already concedes that a local Administrator defeats the design by
    # taking ownership. Matched by SID so this holds on a non-English Windows.
    #
    # NT SERVICE\TrustedInstaller (S-1-5-80-956008885-3418522649-1831038044-
    # 1853292631-2271478464 -- an "NT SERVICE" SID, computed the same
    # deterministic way $ServiceAccount's own is, so it is this exact string
    # on every Windows machine) belongs on this list for the same reason: it
    # is what *grants* the write access this check exists to catch, not an
    # instance of it. TrustedInstaller, not Administrators, owns
    # %ProgramFiles% out of the box and is the only principal with
    # unconditional write there -- that is what stops an ordinary
    # Administrator token from touching a protected system folder without an
    # explicit takeover, i.e. it is Windows' version of "only administrators
    # can write here", not a hole in it. Before this was added, every install
    # into the installer's own offered default location failed
    # Assert-ImageProtected with "... is writable by 'NT
    # SERVICE\TrustedInstaller'" -- the separation step refusing the one
    # install layout its own error message tells the user to keep.
    foreach ($wellKnown in @(
        'S-1-5-18', 'S-1-5-32-544', 'S-1-5-32-547',
        'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
    )) {
        try {
            $name = (New-Object System.Security.Principal.SecurityIdentifier($wellKnown)).Translate([System.Security.Principal.NTAccount]).Value
        } catch {
            continue
        }
        if ($Identity -ieq $name) { return $true }
    }
    return $false
}

function Assert-ImageProtected {
    <#
      #407, settled: a service runs whatever binPath names, so an install the
      logged-in user can rewrite turns privilege separation inside out -- the
      agent gains a way to run its own code *as the service account*, which is
      strictly worse than the unseparated install it replaced.

      This used to be a check on which install *tier* had been chosen:
      privacyfence.iss offered a non-elevated per-user install under
      %LOCALAPPDATA%\Programs, and that tier is what this refused. ADR 0003
      decision 4 removed the tier -- Setup is PrivilegesRequired=admin and runs
      this script itself, so a stock install lands under %ProgramFiles% and
      never reaches the refusal below. What is left for it to catch is
      everything else that can put a writable image under a service's binPath:
      a -DaemonExec pointed at a copy somewhere in a profile, an install
      directory whose ACL was relaxed afterwards, a hand-assembled build.

      Checked rather than documented, and checked again at every daemon start
      (privilege_separation.audit_layout -> windows_acl.image_problems) in case
      the install is later replaced in place.
    #>
    $installDir = Split-Path -Parent $script:DaemonExec
    foreach ($target in @($script:DaemonExec, $installDir)) {
        foreach ($rule in (Get-PathAccessRules -LiteralPath $target)) {
            if (-not (Test-RuleGrantsWrite -Rule $rule)) { continue }
            $identity = $rule.IdentityReference.Value
            if (Test-TrustedIdentity -Identity $identity) { continue }
            Stop-WithError @"
refusing to enable: $target is writable by '$identity'.

The daemon would run this image as $ServiceAccount, so anything that can
rewrite it -- including the AI agent this feature exists to contain -- could
run its own code as that account. That is worse than no separation at all.

A separated install has to live somewhere only administrators can write. The
PrivacyFence installer puts one under %ProgramFiles% and runs this script
itself; an install directory under a user profile, or one whose permissions
have been relaxed since, is what this refuses. Re-run the PrivacyFence
installer and accept its elevation prompt, keeping the offered location.
"@
        }
    }
}

# ── Account and group provisioning ───────────────────────────────────────────

function New-ServiceGroup {
    # The machine half: creating the group needs no human, and the ACLs
    # Set-Layout writes name it whether or not anyone is in it yet.
    if (-not (Get-LocalGroup -Name $ServiceGroup -ErrorAction SilentlyContinue)) {
        Write-Note "creating the $ServiceGroup local group"
        # Keep this under 48 characters. New-LocalGroup validates -Description
        # against that limit and *fails* over it -- it does not truncate -- so
        # a longer, more explanatory sentence here aborts the whole install
        # (ParameterArgumentValidationError, "the character length of the 85
        # argument is too long"). That is not hypothetical: it is what the
        # separation step failed on once Get-Acl stopped failing first, since
        # nothing had ever reached this line on a CI runner before. What the
        # group actually grants is documented where it is granted -- see
        # Set-Layout's own handoff ACL and privilege_separation.py's module
        # docstring -- not in 48 characters here.
        New-LocalGroup -Name $ServiceGroup -Description 'May read PrivacyFence''s handoff directory.' | Out-Null
    } else {
        Write-Note "local group $ServiceGroup already exists -- leaving it as it is"
    }
    # Deliberately *not* adding $ServiceAccount to this group: a virtual
    # service account has no group memberships, which is why every ACL below
    # names the account and the group separately.
}

function Add-OwnerToServiceGroup {
    # The per-user half (ADR 0003 decision 3). Idempotent: an owner already in
    # the group is left alone, which is what makes -ForUser safe to re-run at
    # every companion start.
    if (-not (Get-LocalGroupMember -Group $ServiceGroup -Member $script:OwnerUser -ErrorAction SilentlyContinue)) {
        Write-Note "adding $($script:OwnerUser) to $ServiceGroup"
        Add-LocalGroupMember -Group $ServiceGroup -Member $script:OwnerUser
    }
}

# ── ACLs: the Windows half of the layout ─────────────────────────────────────

function Invoke-Native {
    <#
      Every external command this script runs goes through here, for one
      reason that is easy to get wrong and hard to notice: with
      $ErrorActionPreference = 'Stop' (set at the top, and wanted everywhere
      else), a native command writing to stderr can itself become a
      terminating error -- so a `2>&1` capture meant to *report* icacls'
      complaint can instead abort the script at the redirect, before the
      exit-code check that was supposed to decide whether it mattered. Some
      of these commands write to stderr on success (`sc.exe stop` on an
      already-stopped service, `schtasks /delete` on a task that is not
      there), which is exactly the case -IgnoreFailure exists for.

      So: stderr is captured as text, the exit code decides, and the
      preference is restored afterwards.
    #>
    param([string] $FilePath, [string[]] $Arguments, [switch] $IgnoreFailure)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = (& $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0 -and -not $IgnoreFailure) {
        Stop-WithError "$FilePath $($Arguments -join ' ') failed (exit $code): $output"
    }
    return $output
}

function ConvertTo-CommandLineToken {
    <#
      One argument, quoted the way CommandLineToArgvW -- and so every C
      runtime's argv, sc.exe's included -- parses it back out: wrapped in
      quotes, with any quote *inside* it escaped as \" and the backslashes
      that immediately precede a quote, or that end the token, doubled so
      they stay literal instead of escaping the quote next to them.

      Only for command lines this script builds itself, for
      Invoke-NativeCommandLine below. Everything that goes through
      Invoke-Native's argument array must *not* be pre-quoted -- PowerShell
      quotes those itself, and doing it twice is its own bug.
    #>
    param([string] $Value)

    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-NativeCommandLine {
    <#
      Invoke-Native's sibling for the one call whose arguments PowerShell
      cannot carry: the command line is handed to CreateProcess exactly as
      built here, instead of being assembled by PowerShell out of an array.

      That distinction is not academic, and `sc create` is where it bites.
      Its binPath= value has to arrive as the single argument
      `"<path>" --windows-service`, quotes and all, because the SCM runs
      ImagePath as a command line: drop the quotes and
      `C:\Program Files\PrivacyFence\privacyfence-app.exe` becomes
      CreateProcess' guessing game, starting at `C:\Program.exe` -- the
      unquoted-service-path hijack, which is not a thing to ship in the
      script whose entire job is to stop the agent running its own code as
      the service account.

      Windows PowerShell 5.1 cannot pass that argument. Its native-argument
      binder wraps any argument containing an unquoted space in quotes and
      does not escape the quotes already inside it, so that value leaves as
      `""C:\Program Files\...\privacyfence-app.exe" --windows-service"` and
      arrives at sc.exe split in two: binPath= gets `C:\Program`, and
      `Files\PrivacyFence\privacyfence-app.exe --windows-service` becomes an
      option sc.exe has never heard of. That is ERROR_INVALID_COMMAND_LINE,
      exit 1639 and a usage dump -- the fifth defect on this one code path,
      and the first that only fires when the install directory has a space
      in it, which is why every CI run into a scratch directory passed while
      every real install into %ProgramFiles% died.

      Pre-escaping the quotes instead would fix 5.1 and break PowerShell 7,
      whose default $PSNativeCommandArgumentPassing escapes them correctly
      already and would pass our backslashes through as literals. Building
      the command line ourselves is the one spelling that means the same
      thing on both.
    #>
    param([string] $FilePath, [string] $CommandLine, [switch] $IgnoreFailure)

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $CommandLine
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void] $process.Start()
    # Both pipes drained concurrently. Reading one to the end while the child
    # fills the other deadlocks both sides, and sc.exe's usage dump -- the
    # output this most needs to report -- is exactly the kind that fills one.
    $stdoutRead = $process.StandardOutput.ReadToEndAsync()
    $stderrRead = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $code = $process.ExitCode
    $output = (@($stdoutRead.Result, $stderrRead.Result) | Where-Object { $_ }) -join [Environment]::NewLine
    $process.Dispose()
    if ($code -ne 0 -and -not $IgnoreFailure) {
        Stop-WithError "$FilePath $CommandLine failed (exit $code): $output"
    }
    return $output
}

function Invoke-Icacls {
    param([string[]] $Arguments, [switch] $IgnoreFailure)

    $output = Invoke-Native -FilePath 'icacls.exe' -Arguments $Arguments -IgnoreFailure:$IgnoreFailure
    if ($IgnoreFailure -and $LASTEXITCODE -ne 0) {
        Write-Warn "icacls $($Arguments -join ' ') reported a problem: $output"
    }
}

function Set-Layout {
    Write-Note "re-owning $SystemRoot to $ServiceAccount and rewriting its ACLs"
    $authority = Join-Path $SystemRoot $AuthorityDirName
    $handoff = Join-Path $SystemRoot $HandoffDirName
    foreach ($dir in @($authority, $handoff, (Join-Path $SystemRoot 'logs'))) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }

    # Ownership first, and it matters more than any grant below. An object's
    # owner holds WRITE_DAC implicitly on Windows, whatever its ACL says --
    # and %ProgramData% lets any user create a subdirectory, which that user
    # then owns. A PrivacyFence directory created there by the signed-in
    # account before this ran (or left by anything else that is not this
    # script) would otherwise stay owned by the very human account the
    # separation exists to exclude, wearing a perfect-looking ACL that account
    # can rewrite with one command and no elevation.
    #
    # Administrators rather than the service account, for two reasons: it
    # needs no privilege juggling (an elevated shell can always set it),
    # and it denies the daemon WRITE_DAC on its own boundary -- a service
    # that can rewrite the ACL protecting it from the agent is one
    # compromise away from not having one. Administrators can already defeat
    # all of this by taking ownership, which issue #428's "Honest limits"
    # says in as many words, so nothing is given away.
    #
    # It also settles OWNER RIGHTS (S-1-3-4), an ACE Windows materializes on
    # directories under a user profile that grants *whoever owns the object*
    # and survives /inheritance:r. Owned by Administrators it is harmless;
    # owned by the human it is a bypass. windows_acl.py resolves it against
    # the real owner rather than trusting the ACE on its face.
    Invoke-Icacls @($SystemRoot, '/setowner', $SidAdministrators, '/t', '/c', '/q') -IgnoreFailure
    # -IgnoreFailure above, then verified here: /c lets icacls continue past
    # an individual entry deep in the tree it cannot rewrite, which is worth
    # tolerating, but the *root's* own owner is the thing everything below
    # depends on. A silent failure there would leave a layout that looks
    # right in every other respect, so it stops here; Invoke-Enable's own
    # catch is what takes the half-made service and task back down
    # (Undo-PartialEnable).
    $newOwner = Get-PathOwner -LiteralPath $SystemRoot
    if (-not (Test-TrustedIdentity -Identity $newOwner)) {
        Stop-WithError @"
could not take ownership of $SystemRoot -- it is still owned by '$newOwner'.

An object's owner can rewrite its access-control list at will, so leaving it
owned by that account would make every permission below advisory. Re-run
'enable' from a PowerShell started with 'Run as administrator'.
"@
    }

    # The root. /inheritance:r first, and it is the single most load-bearing
    # line in this file: %ProgramData% grants Users read-and-execute by
    # inheritance, so a directory created under it is readable by every account
    # on the machine until that inheritance is severed. Then /grant:r (replace,
    # not add) for the three principals that may see everything, and a
    # separate, *non*-inheriting (X) for Users -- the exact Windows spelling of
    # POSIX 0711: traversable so a user-session process can reach handoff\,
    # never listable, so nothing can enumerate what else is in here.
    Invoke-Icacls @($SystemRoot, '/inheritance:r', '/q')
    Invoke-Icacls @($SystemRoot, '/grant:r', "${ServiceAccount}:(OI)(CI)(F)", "${SidSystem}:(OI)(CI)(F)", "${SidAdministrators}:(OI)(CI)(F)", '/q')
    Invoke-Icacls @($SystemRoot, '/grant', "${SidUsers}:(X)", '/q')

    # authority\: policy the agent may not edit, #426's WebAuthn store, the
    # audit log and its HMAC key. The service account and nothing else -- this
    # is the whole of what Phase 4 claims, on every platform.
    Invoke-Icacls @($authority, '/inheritance:r', '/q')
    Invoke-Icacls @($authority, '/grant:r', "${ServiceAccount}:(OI)(CI)(F)", "${SidSystem}:(OI)(CI)(F)", "${SidAdministrators}:(OI)(CI)(F)", '/q')

    # handoff\: read-only to the group, which is where Windows ends up
    # *tighter* than POSIX rather than looser. The POSIX layout has to give the
    # group rwx because the companion creates its own socket there and
    # connect(2) needs write on the node; here both control channels are named
    # pipes, so nothing in the user's session ever creates anything in this
    # directory -- it only reads mcp_token and the discovery files.
    Invoke-Icacls @($handoff, '/inheritance:r', '/q')
    Invoke-Icacls @($handoff, '/grant:r', "${ServiceAccount}:(OI)(CI)(F)", "${SidSystem}:(OI)(CI)(F)", "${SidAdministrators}:(OI)(CI)(F)", "${ServiceGroup}:(OI)(CI)(RX)", '/q')
    # Files already in handoff\ -- a reinstall's, left by `uninstall` -- are
    # reset to inherit the grants above rather than trusted to still carry
    # them. mcp_token in particular is reused across restarts and would
    # otherwise stay unreadable to the agent if its ACL had drifted. This is
    # the Windows counterpart of the POSIX scripts' find -exec chmod 640.
    Get-ChildItem -Path $handoff -Force -ErrorAction SilentlyContinue | ForEach-Object {
        Invoke-Icacls @($_.FullName, '/reset', '/q')
    }
}

function Write-Marker {
    $marker = Join-Path $SystemRoot $MarkerName
    Write-Note "writing $marker"
    # Empty rather than absent when the machine half ran with no human to add
    # (ADR 0003 decision 3): privilege_separation._parse_marker() requires the
    # key and refuses the whole marker without it, and a refused marker is a
    # startup failure by design -- so "" is the machine-readable spelling of
    # "group membership pending", and is_enabled() stays true for it, because
    # the install *is* separated. $OwnerUser itself still holds whatever
    # identity this ran as, which is not the same thing as an owner.
    #
    # An owner already recorded in the marker is kept, whoever this run
    # resolved. owner_user is what privilege_separation.owner_sid() maps to the
    # install's original principal (ADR 0008), so it names the first human only
    # and is never rewritten: `enable -ForUser <second>` adds that account
    # alongside the owner and must not hand it the owner's data, and a machine
    # half with no owner resolved must not unrecord one. `uninstall -Purge` is
    # the one thing that removes the owner, by removing the marker. See
    # docs/adr/0043-the-recorded-owner-is-never-rewritten.md.
    $recordedOwner = Get-MarkerOwnerUser
    if ($recordedOwner) {
        Write-Note "keeping the owner already recorded in ${marker}: $recordedOwner"
    } else {
        $recordedOwner = $(if ($script:OwnerResolved) { $script:OwnerUser } else { '' })
    }
    $payload = [ordered]@{
        version         = $MarkerVersion
        platform        = 'win32'
        service_account = $ServiceAccount
        service_group   = $ServiceGroup
        owner_user      = [string]$recordedOwner
        enabled_at      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    # -Encoding ascii, not the default: privilege_separation._parse_marker
    # reads this as UTF-8, and Windows PowerShell 5.1's "unicode"/"utf8"
    # defaults would hand it a BOM or UTF-16. Every value here is ASCII.
    $payload | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding ascii

    # Readable by every account on the machine, on purpose and by its own ACE
    # rather than by inheritance -- the root above grants Users traverse only,
    # so without this the one file that tells a user-session process the layout
    # has changed would be the one file it cannot read, and paths.py would
    # resolve the *un*separated directory for the companion and the agent. It
    # holds account and directory names, not secrets.
    Invoke-Icacls @($marker, '/inheritance:r', '/q')
    Invoke-Icacls @($marker, '/grant:r', "${ServiceAccount}:(F)", "${SidSystem}:(F)", "${SidAdministrators}:(F)", "${SidUsers}:(R)", '/q')
}

# ── Service and Scheduled Task wiring ────────────────────────────────────────

function Invoke-Sc {
    param([string[]] $Arguments, [switch] $IgnoreFailure)

    return Invoke-Native -FilePath 'sc.exe' -Arguments $Arguments -IgnoreFailure:$IgnoreFailure
}

function Install-DaemonService {
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Note "the $ServiceName service already exists -- stopping and removing it before re-creating"
        Invoke-Sc @('stop', $ServiceName) -IgnoreFailure | Out-Null
        Invoke-Sc @('delete', $ServiceName) -IgnoreFailure | Out-Null
        # The SCM keeps a deleted service until its last handle closes; a
        # create straight afterwards can fail with "marked for deletion".
        Start-Sleep -Seconds 2
    }
    Write-Note "creating the $ServiceName service, running as $ServiceAccount"
    # obj= "NT SERVICE\PrivacyFence" is what *creates* the virtual account:
    # the SCM materializes it with the service, gives it its own SID, and
    # grants it the "log on as a service" right itself -- none of which is
    # true for an ordinary account, which would need a password stored
    # somewhere and a separate LsaAddAccountRights call.
    #
    # The spaces after each `=` are sc.exe's own (genuinely strange) syntax,
    # not a typo: the separator is "name= value", and "name=value" is parsed
    # as a positional argument instead.
    #
    # No `password=` pair, and it must stay that way. A virtual service
    # account has no password -- omitting the option is how that is said --
    # and passing `'password=', ''` to say it was actively harmful: Windows
    # PowerShell 5.1 *drops* an empty-string argument on its way to a native
    # executable (the bug PSNativeCommandArgumentPassing was added to fix, in
    # 7.1). sc.exe therefore received "password= start= auto", read `start=`
    # as the password's value, and rejected the leftover `auto` with exit
    # 1639 and a usage dump -- which is what a fresh Windows install had been
    # dying on, once the ordering fix let anything reach this line at all.
    #
    # And this one call builds its own command line rather than handing
    # Invoke-Sc an argument array, because binPath='s value is
    # `"<path>" --windows-service` -- an argument with quotes inside it,
    # which Windows PowerShell 5.1 cannot pass without tearing in half at the
    # space in "Program Files". See Invoke-NativeCommandLine, which exists
    # for this line and says exactly what 5.1 does to it.
    Invoke-NativeCommandLine -FilePath 'sc.exe' -CommandLine (@(
        'create', (ConvertTo-CommandLineToken $ServiceName),
        'binPath=', (ConvertTo-CommandLineToken "`"$($script:DaemonExec)`" --windows-service"),
        'obj=', (ConvertTo-CommandLineToken $ServiceAccount),
        'start=', 'auto',
        'DisplayName=', (ConvertTo-CommandLineToken 'PrivacyFence')
    ) -join ' ') | Out-Null
    Invoke-Sc @('description', $ServiceName, 'Runs the PrivacyFence approval daemon under its own account (issue #428 Phase 4).') | Out-Null
    # Crash restart, the thing the Scheduled Task's repeating TimeTrigger was
    # standing in for before there was a service manager involved: three
    # restarts with a widening delay, and the counter resets after a day.
    Invoke-Sc @('failure', $ServiceName, 'reset=', '86400', 'actions=', 'restart/5000/restart/10000/restart/30000') | Out-Null
}

function Start-DaemonService {
    <#
      Split from Install-DaemonService above so Invoke-Enable can create the
      service *before* Set-Layout and start it only after Write-Marker.

      Creating it early is not a preference: `sc create obj= "NT SERVICE\..."`
      is what materializes the virtual account (see Install-DaemonService's own
      comment), and until it exists icacls cannot grant it anything -- by name
      or by SID. Both spellings fail identically with "No mapping between
      account names and security IDs was done" (exit 1332), which is what a
      fresh Windows install had been dying on.

      Starting it late is the other half, and the reason this is a split rather
      than simply moving the whole function up: the daemon must not be running
      while Set-Layout is still moving its data and rewriting the ACLs that
      contain it.
    #>
    Write-Note "starting the $ServiceName service"
    Invoke-Sc @('start', $ServiceName) | Out-Null
}

function Wait-ProcessExit {
    <#
      Waits for one process id to leave the process table, and says whether it
      did. Not "is the service Stopped": a service reports itself stopped from
      inside its own control handler, while the files it had open stay open
      until the kernel tears the process down. Only the process actually being
      gone releases them, and this whole helper exists because something has to
      be moved out from under it immediately afterwards.
    #>
    param([int] $ProcessId, [int] $TimeoutSeconds = 60)

    if ($ProcessId -le 0) { return $true }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Wait-ProcessImageGone {
    <# The same wait, for a process this script did not start and has no pid
       for -- it only asked Task Scheduler to end the task running it. #>
    param([string] $Name, [int] $TimeoutSeconds = 30)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name $Name -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Uninstall-DaemonService {
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
    Write-Note "stopping and removing the $ServiceName service"
    # Take the pid before asking, because `sc delete` below removes the record
    # it comes from. `sc.exe stop` only *asks*: it returns as soon as the SCM
    # has accepted the request, with the service still STOP_PENDING and the
    # daemon still holding every file it had open under $SystemRoot -- which
    # `uninstall -Purge` deletes a few lines later, and which the Inno Setup
    # uninstaller needs released before it can remove the image the service
    # runs. An earlier `disable` that returned here early failed exactly that
    # way: `The process cannot access the file because it is being used by
    # another process`.
    $servicePid = (Get-CimInstance -ClassName Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue).ProcessId
    Invoke-Sc @('stop', $ServiceName) -IgnoreFailure | Out-Null
    if (-not (Wait-ProcessExit -ProcessId $servicePid -TimeoutSeconds 60)) {
        Write-Warn "the $ServiceName service (pid $servicePid) has not exited after 60s -- continuing, but a file it still holds open may make a later step fail"
    }
    Invoke-Sc @('delete', $ServiceName) -IgnoreFailure | Out-Null
}

function Install-CompanionTask {
    $template = Join-Path $TemplateDir 'privacyfence-companion-task.xml.tmpl'
    if (-not (Test-Path -LiteralPath $template)) {
        Stop-WithError "missing template: $template"
    }
    Write-Note "registering the '$CompanionTaskName' scheduled task"
    $xml = (Get-Content -LiteralPath $template -Raw).Replace('__EXEC_PATH__', $script:CompanionExec)
    $xmlFile = Join-Path ([System.IO.Path]::GetTempPath()) 'privacyfence-companion-task.xml'
    # The encoding trap the template's own header comment documents: schtasks
    # hands the file to MSXML as a Unicode stream, and a
    # declaration that contradicts the stream fails with "unable to switch the
    # encoding". The template declares no encoding and is pure ASCII, so this
    # only has to avoid writing a BOM.
    Set-Content -LiteralPath $xmlFile -Value $xml -Encoding ascii
    try {
        Invoke-Native -FilePath 'schtasks.exe' `
            -Arguments @('/create', '/tn', $CompanionTaskName, '/xml', $xmlFile, '/f') | Out-Null
    } finally {
        Remove-Item -LiteralPath $xmlFile -Force -ErrorAction SilentlyContinue
    }
    # Start it now, rather than waiting for the next sign-in: without a
    # companion in this session there is no way into the web UI and no way for
    # a connector OAuth flow to open a browser. Best-effort -- this runs from
    # an elevated shell, and an elevated context is not always able to start
    # something into the interactive session it was launched from.
    Invoke-Native -FilePath 'schtasks.exe' `
        -Arguments @('/run', '/tn', $CompanionTaskName) -IgnoreFailure | Out-Null
}

function Uninstall-CompanionTask {
    if (-not (Get-ScheduledTask -TaskName $CompanionTaskName -ErrorAction SilentlyContinue)) { return }
    Write-Note "removing the '$CompanionTaskName' scheduled task"
    # End the instance before deleting the task, for the same reason
    # Uninstall-DaemonService waits above: deleting a task does not end what it
    # already started, and `enable` starts a companion itself
    # (Install-CompanionTask below). A companion left running holds the
    # separated data directory open just as effectively as the daemon does.
    # `/end` rather than taskkill -- the companion is Task Scheduler's process
    # to end, and asking through the same mechanism that started it leaves no
    # ambiguity about which PrivacyFenceCompanion is being stopped.
    Invoke-Native -FilePath 'schtasks.exe' `
        -Arguments @('/end', '/tn', $CompanionTaskName) -IgnoreFailure | Out-Null
    Invoke-Native -FilePath 'schtasks.exe' `
        -Arguments @('/delete', '/tn', $CompanionTaskName, '/f') -IgnoreFailure | Out-Null
    $companionProcess = [System.IO.Path]::GetFileNameWithoutExtension($DefaultCompanionExecName)
    if (-not (Wait-ProcessImageGone -Name $companionProcess -TimeoutSeconds 30)) {
        Write-Warn "$DefaultCompanionExecName is still running after 30s -- continuing, but a file it still holds open may make a later step fail"
    }
}

# ── Subcommands ──────────────────────────────────────────────────────────────

function Undo-PartialEnable {
    <#
      Take a failed `enable` back down to "not separated": no marker, no
      companion task, no service. Whatever is under $SystemRoot stays there --
      `enable` moves no data, so a rollback has none to move back either (ADR
      0042: nothing restores data into a user profile). A re-run `enable`
      picks it up exactly as a reinstall does.

      This is privacyfence/privacyfence#599's counterpart to the `disable`
      defect fixed in 1d6b13f: an operation that is not atomic and does not
      clean up after itself when it fails midway leaves an install that is
      neither separated nor whole -- here, a marker claiming a layout whose
      service is gone, which paths.py would resolve for nobody.

      Every step is a no-op on the state it was not reached from
      (Uninstall-* return early on what is not there), which is what lets one
      function serve every failure point. Every step is best-effort and none
      of them may throw, because this runs *inside* a catch whose exception
      is about to be re-thrown -- that exception is what says why `enable`
      failed, and a rollback that replaced it with its own would lose the
      only useful thing in the transcript.
    #>
    param([string] $Reason)

    Write-Warn "enable failed ($Reason) -- rolling back"
    try {
        $marker = Join-Path $SystemRoot $MarkerName
        if (Test-Path -LiteralPath $marker) {
            Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
        }
        Uninstall-CompanionTask
        Uninstall-DaemonService
        Write-Note "rolled back -- this install is not separated; anything under $SystemRoot is left where it is"
    } catch {
        Write-Warn @"
the rollback itself failed: $($_.Exception.Message)

Re-run '$PSCommandPath enable' from a PowerShell started with 'Run as
administrator' to finish separating, or '$PSCommandPath uninstall' to take the
service and the companion task down.
"@
    }
}

function Invoke-Enable {
    Assert-Windows
    Assert-Administrator
    # Deliberately the optional resolution, not Resolve-Owner's own throw (ADR
    # 0003 decision 3): with no human account to resolve -- an MDM push, a
    # SYSTEM-context install -- this still separates the machine completely,
    # and records the one step that genuinely needs a human (the group
    # membership) as pending rather than abandoning the whole install to the
    # unseparated layout the way it used to.
    Write-Note 'enable: resolving the owner and checking the install image'
    Resolve-Owner -Optional
    Resolve-Executables
    Assert-ImageProtected

    if (Test-Path -LiteralPath (Join-Path $SystemRoot $MarkerName)) {
        Write-Note 'already separated -- re-running to refresh the account, ACLs, service and task'
    }

    Uninstall-DaemonService
    # And the companion, for the same reason and with the same wait: a
    # re-run `enable` (an upgrade, or decision 6's automatic one against an
    # install whose previous enable half-finished) finds the companion this
    # script's own Install-CompanionTask started still running, and a live
    # companion holds files under $SystemRoot open while Set-Layout is about
    # to rewrite their ACLs. Install-CompanionTask registers and
    # starts it again at the end of this same run, so there is nothing to put
    # back -- the only thing removing it early costs is the seconds it takes
    # to exit.
    Uninstall-CompanionTask

    New-ServiceGroup
    if ($script:OwnerResolved) {
        Add-OwnerToServiceGroup
    } else {
        Write-Note "no owner account resolved -- leaving the $ServiceGroup membership pending"
    }
    # Everything that can leave this install in neither layout, in one block
    # that undoes itself -- privacyfence/privacyfence#599's third half. The two
    # states worth having are "separated" and "not"; an `enable` that stops
    # between them produces neither, and that is not a theoretical shape. The
    # observed one was the daemon's data under %ProgramData% -- a real
    # install's authority directory, audit log and MCP token -- with no marker,
    # no service and no companion task pointing at it, which `paths.py`
    # resolves for nobody.
    #
    # Install-DaemonService goes first because it is what brings
    # NT SERVICE\PrivacyFence into existence, and Set-Layout's grants cannot
    # name an account that does not exist yet. It does not start the service -- Start-DaemonService below
    # does that, once the ACLs are in place. See Start-DaemonService.
    try {
        Install-DaemonService
        # A reinstall finds the directory `uninstall` left and keeps it (ADR
        # 0042); a fresh install starts it empty. Nothing is migrated in from
        # a user profile (ADR 0041).
        New-Item -ItemType Directory -Force -Path $SystemRoot | Out-Null
        Set-Layout
        Write-Marker
        Install-CompanionTask
    } catch {
        Undo-PartialEnable -Reason $_.Exception.Message
        throw
    }
    # Deliberately outside that try: a service that exists but will not start
    # is a separated install with a broken daemon, which `sc query` and the
    # event log can both explain, and rolling the marker and the ACLs back
    # around it would trade a diagnosable problem for a silent one.
    Start-DaemonService
    Write-Note 'enable: done'

    Write-Host @"

OK  PrivacyFence now runs as $ServiceAccount.

  Data directory   $SystemRoot
  Human authority  $SystemRoot\$AuthorityDirName   ($ServiceAccount only)
  Shared handoff   $SystemRoot\$HandoffDirName   (read-only to $ServiceGroup)
  Daemon           sc.exe query $ServiceName
  Companion        the PrivacyFence tray icon, started at sign-in
"@

    if ($script:OwnerResolved) {
        Write-Host @"

  One thing left to do by hand: sign $($script:OwnerUser) out and back in.
  Windows puts group memberships in the logon token, so the session you are in
  right now still does not know it is in $ServiceGroup -- which means the
  companion app and your MCP client cannot read the handoff directory until you
  do. '... status' will tell you when it has taken.
"@
    } else {
        Write-Host @"

  Nobody is in $ServiceGroup yet: this ran with no human account to add, which
  is the ordinary case for an MDM push or a SYSTEM-context install. The install
  is separated regardless -- what is pending is one re-runnable step, which the
  companion app takes by itself at the first real sign-in, or which you can
  take now:

    ... enable -ForUser <name>
"@
    }

    Write-Host @"

  What this does and does not buy you is written down in
  docs/security-and-compliance.md's "Local-mode trust boundary" section. The
  short version: the agent can no longer rewrite your policy, forge a passkey
  or read the audit key -- and an agent that can get Administrator still
  defeats all of it, because Administrator defeats everything.
"@
}

function Invoke-EnableForUser {
    # ADR 0003 decision 3's per-user half, on its own: the two steps of
    # `enable` that need to know which human this install is for. Runs against
    # an install the machine half has already separated, and re-runs
    # harmlessly against one that is already complete -- the group add is
    # idempotent, and the ACLs and marker are rewritten to the same values.
    #
    # Deliberately no Resolve-Executables/Assert-ImageProtected: this installs
    # no service and starts nothing, so a second user can be added to the
    # group without the image having to pass a check that only governs what
    # runs *as* the service account.
    Assert-Windows
    Assert-Administrator
    Resolve-Owner

    if (-not (Test-Path -LiteralPath (Join-Path $SystemRoot $MarkerName))) {
        Stop-WithError "this install is not privilege-separated yet -- run '... enable' first"
    }

    # ADR 0008 ("D2: two identities, not one, per install"): adding another
    # account to $ServiceGroup is the normal, supported way to let more than
    # one human use this install. Each gets its own isolated users\os-<sid>\
    # storage, which the daemon creates for that account's principal on first
    # use -- there is nothing to refuse here and nothing to copy.
    $markerOwner = Get-MarkerOwnerUser
    if ($markerOwner -and $markerOwner -ine $script:OwnerUser) {
        Write-Note "adding $($script:OwnerUser) alongside this install's existing owner $markerOwner -- each gets its own isolated PrivacyFence identity (docs/adr/0008-one-principal-per-os-user.md)"
    }

    Add-OwnerToServiceGroup
    # The layout is re-asserted rather than assumed. The marker records this
    # account as the owner only if it has none yet; Write-Marker never
    # replaces one already recorded.
    Set-Layout
    Write-Marker

    Write-Host @"

OK  $($script:OwnerUser) is now a member of $ServiceGroup.

  One thing left to do by hand: sign $($script:OwnerUser) out and back in.
  Windows puts group memberships in the logon token, so the session you are in
  right now still does not know it is in $ServiceGroup -- which means the
  companion app and your MCP client cannot read the handoff directory until you
  do. '... status' will tell you when it has taken.
"@
}

function Invoke-Uninstall {
    <#
      ADR 0042's remove/purge split, the Windows spelling. What the Inno Setup
      uninstaller runs (installer/privacyfence.iss, CurUninstallStepChanged),
      before it deletes the program files the service runs.

      Without -Purge: stop and unregister the service and the companion task,
      and leave $SystemRoot -- data, marker -- and $ServiceGroup exactly where
      they are, so a reinstall's `enable` picks the data straight back up. The
      NT SERVICE\PrivacyFence virtual account goes with its service; that is
      Windows, not a choice this script makes, and the same service name
      brings the same SID back on reinstall, so the ACLs left on $SystemRoot
      still name the right account.

      With -Purge: also delete $SystemRoot and $ServiceGroup. Nothing ever
      moves data back into a user profile (ADR 0041): the per-user layout is
      not a supported one, which is why `disable` is gone.

      Idempotent and tolerant of every piece already being gone, because the
      uninstaller runs it against whatever an install left -- including one
      whose `enable` failed and rolled back.
    #>
    Assert-Windows
    Assert-Administrator

    Uninstall-CompanionTask
    Uninstall-DaemonService

    if (-not $Purge) {
        Write-Host @"

OK  The $ServiceName service and the '$CompanionTaskName' task are removed.

    Your data is still at $SystemRoot, and the $ServiceGroup group is kept:
    reinstalling PrivacyFence picks both up as they are. To delete them:
      $PSCommandPath uninstall -Purge
    or, once the program files are gone, delete $SystemRoot by hand from an
    elevated shell and run: Remove-LocalGroup -Name $ServiceGroup
"@
        return
    }

    if (Test-Path -LiteralPath $SystemRoot) {
        Write-Note "deleting $SystemRoot"
        # Owned by Administrators with a full-control grant for them
        # (Set-Layout), so an elevated shell can always do this; no
        # take-ownership step is needed.
        Remove-Item -LiteralPath $SystemRoot -Recurse -Force
    }
    if (Get-LocalGroup -Name $ServiceGroup -ErrorAction SilentlyContinue) {
        Write-Note "removing the $ServiceGroup local group"
        Remove-LocalGroup -Name $ServiceGroup
    }
    Write-Host @"

OK  PrivacyFence's service, companion task, data ($SystemRoot) and the
    $ServiceGroup group are all removed.
"@
}

function Invoke-Status {
    Assert-Windows
    Resolve-Owner -Optional

    $marker = Join-Path $SystemRoot $MarkerName
    if (-not (Test-Path -LiteralPath $marker)) {
        Write-Host 'privilege separation: OFF'
        Write-Host "  the daemon and the AI agent run as the same account ($($script:OwnerUser))."
        Write-Host '  Run this script with "enable", from an elevated PowerShell, to change that.'
        return 0
    }

    Write-Host 'privilege separation: ON'
    Write-Host "  marker:          $marker"
    Write-Host "  data directory:  $SystemRoot"
    $problems = 0
    # Distinct from "OFF" above on purpose (ADR 0003 decision 3): this install
    # *is* separated -- the machine half ran -- and what is outstanding is one
    # re-runnable step. Reporting it as not separated would say the daemon and
    # the agent share an account, which is exactly what is no longer true.
    $markerOwner = Get-MarkerOwnerUser
    if ($markerOwner) {
        Write-Host "  owner:           $markerOwner"
    } else {
        Write-Host "  PENDING USER     no owner recorded -- nobody has been added to $ServiceGroup yet."
        Write-Host '                   The companion app closes this at the first sign-in, or:'
        Write-Host '                   ... enable -ForUser <name>'
        $problems = 1
    }
    $authority = Join-Path $SystemRoot $AuthorityDirName
    $handoff = Join-Path $SystemRoot $HandoffDirName

    foreach ($dir in @($SystemRoot, $authority, $handoff)) {
        if (-not (Test-Path -LiteralPath $dir)) {
            Write-Host "  MISSING          $dir"
            $problems = 1
        }
    }

    if (Test-Path -LiteralPath $SystemRoot) {
        # Checked before any grant, and reported first: an owner outside this
        # design can rewrite everything below it, so a clean ACL under the
        # wrong owner is not a clean layout.
        $rootOwner = Get-PathOwner -LiteralPath $SystemRoot
        if ((Test-TrustedIdentity -Identity $rootOwner) -or $rootOwner -ieq $ServiceAccount) {
            Write-Host "  ok               $SystemRoot is owned by $rootOwner"
        } else {
            Write-Host "  WRONG OWNER      $SystemRoot is owned by '$rootOwner' -- an owner can rewrite the ACL at will"
            $problems = 1
        }

        $listable = @(Get-PathAccessRules -LiteralPath $SystemRoot |
            Where-Object { (Test-RuleGrantsRead -Rule $_) -and -not (Test-TrustedIdentity -Identity $_.IdentityReference.Value) -and $_.IdentityReference.Value -ine $ServiceAccount })
        if ($listable.Count -gt 0) {
            Write-Host "  LISTABLE         $SystemRoot can be enumerated by: $(($listable | ForEach-Object { $_.IdentityReference.Value }) -join ', ')"
            $problems = 1
        } else {
            Write-Host "  ok               $SystemRoot is traversable but not listable"
        }
    }

    if (Test-Path -LiteralPath $authority) {
        $exposed = @(Get-PathAccessRules -LiteralPath $authority |
            Where-Object { $_.AccessControlType -eq 'Allow' -and -not (Test-TrustedIdentity -Identity $_.IdentityReference.Value) -and $_.IdentityReference.Value -ine $ServiceAccount })
        if ($exposed.Count -gt 0) {
            Write-Host "  NOT SEPARATED    $authority is reachable by: $(($exposed | ForEach-Object { $_.IdentityReference.Value }) -join ', ')"
            $problems = 1
        } else {
            Write-Host "  ok               $authority is reachable only by $ServiceAccount"
        }
    }

    if (Test-Path -LiteralPath $handoff) {
        $groupRules = @(Get-PathAccessRules -LiteralPath $handoff |
            Where-Object { $_.IdentityReference.Value -like "*\$ServiceGroup" -or $_.IdentityReference.Value -ieq $ServiceGroup })
        if ($groupRules.Count -eq 0) {
            Write-Host "  NO HANDOFF       $handoff grants $ServiceGroup nothing -- your MCP client cannot read mcp_token"
            $problems = 1
        } elseif (@($groupRules | Where-Object { Test-RuleGrantsWrite -Rule $_ }).Count -gt 0) {
            Write-Host "  TOO OPEN         $handoff grants $ServiceGroup write access -- it only needs to read"
            $problems = 1
        } else {
            Write-Host "  ok               $handoff is readable by $ServiceGroup"
        }
    }

    if ($script:OwnerResolved) {
        if (Get-LocalGroupMember -Group $ServiceGroup -Member $script:OwnerUser -ErrorAction SilentlyContinue) {
            Write-Host "  ok               $($script:OwnerUser) is a member of $ServiceGroup"
        } else {
            Write-Host "  NOT A MEMBER     $($script:OwnerUser) is not in $ServiceGroup -- the companion cannot read the handoff directory"
            $problems = 1
        }
        # Distinct from the check above: membership can be recorded and still
        # be absent from an already-running logon token, which is exactly the
        # "sign out and back in" case enable prints about. whoami /groups
        # reports the *token*, which is the thing that actually decides access.
        $inToken = (Invoke-Native -FilePath 'whoami.exe' -Arguments @('/groups') -IgnoreFailure) `
            -match [regex]::Escape($ServiceGroup)
        if ($inToken) {
            Write-Host "  ok               this logon session has picked that membership up"
        } else {
            Write-Host "  PENDING SIGNOUT  this session predates the group change -- sign out and back in"
            $problems = 1
        }
    }

    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Host "  NO SERVICE       the $ServiceName service does not exist"
        $problems = 1
    } elseif ($service.Status -ne 'Running') {
        Write-Host "  NOT RUNNING      the $ServiceName service is $($service.Status) (sc.exe query $ServiceName)"
        $problems = 1
    } else {
        Write-Host "  ok               the $ServiceName service is running"
    }

    if (Get-ScheduledTask -TaskName $CompanionTaskName -ErrorAction SilentlyContinue) {
        Write-Host "  ok               the '$CompanionTaskName' task starts the companion at sign-in"
    } else {
        Write-Host "  NOT INSTALLED    the '$CompanionTaskName' task -- no tray icon, and connector OAuth cannot open a browser"
        $problems = 1
    }

    return $problems
}

# ── Daemon manager (#428 Phase 2) ──────────────────────────────────────────
#
# `daemon {status|start|stop|restart|ensure-running}` is what the companion
# app's tray menu runs -- `status` unprivileged, on every poll, and
# `start`/`stop`/`restart` elevated (src/privacyfence/service_control.py's
# `run_elevated()`, via `privilege_separation._windows_runas_argv`), only
# when a human clicks the corresponding menu item. Built on the same
# Invoke-Sc/Invoke-Native wrappers `enable`/`uninstall`/`status` already use
# above -- deliberately not Get-Service/Start-Service/Stop-Service/
# Restart-Service, which this script otherwise avoids except for the two
# read-only existence checks Invoke-Status already makes.
function Invoke-DaemonStatus {
    <#
      Unprivileged and meant to answer immediately, the same posture
      Invoke-Status has for the read-only audit above. Prints a small
      key=value block, one entry per line -- the same shape scripts/
      macos_privilege_separation.sh's and scripts/linux_privilege_
      separation.sh's own `daemon status` print, so a human or a script
      reading any of the three sees the same keys. daemon_status.py's own
      probe does not read this output at all; it asks sc.exe directly
      (PlatformLayout.daemon_ctl_argv), so nothing here has to match that
      module's parser byte for byte -- see that module's own docstring.
    #>
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Host 'status=NotInstalled'
        Write-Host 'pid='
        Write-Host 'win32_exit_code='
        return 0
    }
    $processId = ''
    $cim = Get-CimInstance -ClassName Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
    if ($cim -and $cim.ProcessId) { $processId = $cim.ProcessId }
    # sc.exe query, not Get-Service, for the exit code: .Status alone (used
    # for the state below) says nothing about *why* a stopped service is
    # stopped, and WIN32_EXIT_CODE/SERVICE_EXIT_CODE are exactly the fields
    # src/privacyfence/daemon_status.py's own _windows_status() reads off a
    # direct `sc.exe query` -- keeping this script's own report of the same
    # two fields is what lets a human cross-check the two without learning a
    # second vocabulary.
    $queryOutput = Invoke-Sc @('query', $ServiceName) -IgnoreFailure
    $win32ExitCode = ''
    foreach ($line in ($queryOutput -split [Environment]::NewLine)) {
        if ($line.Trim() -match '^WIN32_EXIT_CODE\s*:\s*(\d+)') { $win32ExitCode = $Matches[1] }
    }
    Write-Host "status=$($service.Status)"
    Write-Host "pid=$processId"
    Write-Host "win32_exit_code=$win32ExitCode"
    return 0
}

function Invoke-DaemonStart {
    Write-Note "starting the $ServiceName service"
    Invoke-Sc @('start', $ServiceName) | Out-Null
}

function Invoke-DaemonStop {
    # Same pattern as Uninstall-DaemonService above: capture the pid before
    # asking the SCM to stop it, because `sc.exe stop` only asks -- it
    # returns as soon as the SCM accepts the request, with the service still
    # STOP_PENDING and the process itself not yet gone.
    $servicePid = (Get-CimInstance -ClassName Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue).ProcessId
    Invoke-Sc @('stop', $ServiceName) -IgnoreFailure | Out-Null
    if (-not (Wait-ProcessExit -ProcessId $servicePid -TimeoutSeconds 30)) {
        Stop-WithError "the $ServiceName service (pid $servicePid) has not exited 30s after stop -- 'sc.exe query $ServiceName' to see why"
    }
    Write-Note "the $ServiceName service stopped"
}

function Invoke-DaemonRestart {
    # There is no `sc.exe restart` -- stop (and wait for the process to
    # actually exit, not just for the SCM to accept the request) then start,
    # mirroring Uninstall-DaemonService's own Invoke-Sc-stop/Wait-ProcessExit
    # pattern followed by a fresh start here instead of a delete.
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -eq 'Running') {
        Invoke-DaemonStop
    }
    Invoke-DaemonStart
}

function Invoke-DaemonEnsureRunning {
    # Tolerant of "already running", unlike a plain Start: the companion's
    # own Start-button click and `enable`'s own daemon start are both
    # supposed to be idempotent, and re-issuing `sc.exe start` against an
    # already-running service is itself harmless (SCM answers "already
    # running" and Invoke-Sc's -IgnoreFailure below just logs it) -- but
    # checking first means the ordinary case prints one clear line instead
    # of a native command's own error text.
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service) {
        Stop-WithError "the $ServiceName service is not installed -- run 'enable' first"
    }
    if ($service.Status -eq 'Running') {
        Write-Note "the $ServiceName service is already running"
        return
    }
    Invoke-DaemonStart
}

switch ($Command) {
    'enable' {
        # -ForUser selects which half runs (ADR 0003 decision 3); it also
        # names the account, so it feeds $OwnerUser the same way -User does.
        if ($ForUser) {
            $script:OwnerUser = $ForUser
            Invoke-EnableForUser
        } else {
            Invoke-Enable
        }
        exit 0
    }
    'uninstall' { Invoke-Uninstall; exit 0 }
    'status' {
        # Coerced through a variable and [int] rather than `exit
        # (Invoke-Status)`: a stray object reaching that function's output
        # stream would otherwise make `exit` receive an array, and the shape
        # of the failure -- a wrong exit code from a read-only subcommand --
        # is one nobody would think to look for. Every line status prints
        # goes through Write-Host, which does not reach this stream.
        $status = Invoke-Status
        exit ([int] @($status)[-1])
    }
    'daemon' {
        switch ($DaemonSubcommand) {
            'status' { $result = Invoke-DaemonStatus; exit ([int] @($result)[-1]) }
            'start' { Invoke-DaemonStart; exit 0 }
            'stop' { Invoke-DaemonStop; exit 0 }
            'restart' { Invoke-DaemonRestart; exit 0 }
            'ensure-running' { Invoke-DaemonEnsureRunning; exit 0 }
            default { Stop-WithError "daemon: a sub-command is required -- one of status|start|stop|restart|ensure-running" }
        }
    }
}
