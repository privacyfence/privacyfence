<#
.SYNOPSIS
  #428 Phase 4 (B5c): opt into -- or back out of -- running the PrivacyFence
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
     enable against such an install, which is what settles #407's open question:
     the non-elevated per-user install path cannot be separated. See
     Assert-ImageProtected below.

.EXAMPLE
  # From an elevated PowerShell, against a real install:
  powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" enable
  ... status
  ... disable

.NOTES
  Step 4 moves live connector OAuth tokens. `disable` moves them back, but this
  is still the step to take a backup before: it is the one part of this that
  touches data you cannot re-mint from a config file.

  Ships opt-in deliberately. #428 P4 does not default on for a platform until
  that platform's opt-in has soaked through a full release cycle.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('enable', 'disable', 'status')]
    [string] $Command,

    # The human account that owns this install. Defaults to whoever is running
    # this script, which is right for the ordinary case (UAC's elevation keeps
    # the same user); pass it explicitly when elevating into a *different*
    # administrator account, or the data migration would look in the wrong
    # profile.
    [string] $User,

    [string] $DaemonExec,
    [string] $CompanionExec
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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
$DaemonTaskName = 'PrivacyFence'
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
$script:OwnerLocalAppData = $null

function Write-Note { param([string] $Message) Write-Host "-> $Message" }
function Write-Warn { param([string] $Message) Write-Warning $Message }
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
    # The owner's own profile, resolved from the SID rather than from
    # $env:LOCALAPPDATA: an elevated shell running as a *different*
    # administrator would otherwise migrate that administrator's (empty) data
    # directory and quietly leave the real one behind, unseparated and still
    # readable by the agent.
    $profilePath = $null
    try {
        $profilePath = (Get-CimInstance Win32_UserProfile -Filter "SID='$($script:OwnerSid)'" -ErrorAction Stop).LocalPath
    } catch {
        $profilePath = $null
    }
    if (-not $profilePath) {
        if ($script:OwnerUser -eq ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name -split '\\')[-1]) {
            $script:OwnerLocalAppData = $env:LOCALAPPDATA
            return
        }
        if ($Optional) { return }
        Stop-WithError "could not resolve $($script:OwnerUser)'s user profile -- has that account ever signed in on this machine?"
    }
    $script:OwnerLocalAppData = Join-Path $profilePath 'AppData\Local'
}

function Get-LegacyDataDir {
    if (-not $script:OwnerLocalAppData) { return $null }
    return (Join-Path $script:OwnerLocalAppData 'PrivacyFence')
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
    foreach ($wellKnown in @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-32-547')) {
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
      strictly worse than the unseparated install it replaced. Windows is the
      only platform where this is reachable: macOS and Linux put the daemon
      under /Applications and /opt as a side effect of how they install
      software at all, while privacyfence.iss offers a non-elevated per-user
      install under %LOCALAPPDATA%\Programs (PrivilegesRequired=lowest).

      Checked rather than documented, and checked again at every daemon start
      (privilege_separation.audit_layout -> windows_acl.image_problems) in case
      the install is later replaced in place.
    #>
    $installDir = Split-Path -Parent $script:DaemonExec
    foreach ($target in @($script:DaemonExec, $installDir)) {
        $acl = Get-Acl -LiteralPath $target
        foreach ($rule in $acl.Access) {
            if (-not (Test-RuleGrantsWrite -Rule $rule)) { continue }
            $identity = $rule.IdentityReference.Value
            if (Test-TrustedIdentity -Identity $identity) { continue }
            Stop-WithError @"
refusing to enable: $target is writable by '$identity'.

The daemon would run this image as $ServiceAccount, so anything that can
rewrite it -- including the AI agent this feature exists to contain -- could
run its own code as that account. That is worse than no separation at all.

This is what a non-elevated, per-user install looks like (#407): PrivacyFence
installed under your own profile rather than under %ProgramFiles%. Re-run the
PrivacyFence installer and let it elevate, so it installs per-machine, then run
this again.
"@
        }
    }
}

# ── Account and group provisioning ───────────────────────────────────────────

function New-ServiceGroup {
    if (-not (Get-LocalGroup -Name $ServiceGroup -ErrorAction SilentlyContinue)) {
        Write-Note "creating the $ServiceGroup local group"
        New-LocalGroup -Name $ServiceGroup -Description 'May read PrivacyFence''s handoff directory (mcp_token and the daemon discovery files).' | Out-Null
    } else {
        Write-Note "local group $ServiceGroup already exists -- leaving it as it is"
    }
    if (-not (Get-LocalGroupMember -Group $ServiceGroup -Member $script:OwnerUser -ErrorAction SilentlyContinue)) {
        Write-Note "adding $($script:OwnerUser) to $ServiceGroup"
        Add-LocalGroupMember -Group $ServiceGroup -Member $script:OwnerUser
    }
    # Deliberately *not* adding $ServiceAccount to this group: a virtual
    # service account has no group memberships, which is why every ACL below
    # names the account and the group separately.
}

# ── Data migration ───────────────────────────────────────────────────────────

# The files that have to end up inside handoff\ rather than at the root of the
# data directory once separation is on, because something in the *user's*
# session reads them: the agent's own credential and the URL it reaches the
# daemon at (mcp_token/mcp_url, read by the MCPB shim), and the discovery files
# a human or the companion reads (web_base_url, <page>_url). Mirrors
# paths.handoff_dir()'s callers -- see test_privilege_separation.py, which
# asserts this list matches the file-name constants those call sites use.
$HandoffFileNames = @('mcp_token', 'mcp_url', 'web_base_url')
# <page>_url, one per page mint_bootstrap_url() has ever minted a link for
# (web/server.py's _bootstrap_url_file_name): approvals_url, settings_url.
$HandoffFileGlob = '*_url'

function Move-Data {
    $legacy = Get-LegacyDataDir
    if (-not $legacy -or -not (Test-Path -LiteralPath $legacy)) {
        Write-Note "no existing $legacy to migrate -- starting the separated install empty"
        New-Item -ItemType Directory -Force -Path $SystemRoot | Out-Null
        return
    }
    if (Test-Path -LiteralPath $SystemRoot) {
        # Something is already there (a previous enable, or a hand-made
        # directory). Merge rather than clobber, then remove the source --
        # leaving a second copy of live OAuth tokens readable by the agent
        # would undo the point of the whole exercise.
        Write-Note "merging $legacy into the existing $SystemRoot"
        Copy-Item -Path (Join-Path $legacy '*') -Destination $SystemRoot -Recurse -Force
        Remove-Item -LiteralPath $legacy -Recurse -Force
    } else {
        Write-Note "moving $legacy to $SystemRoot"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SystemRoot) | Out-Null
        # Move-Item falls back to a copy across volumes, which is what a
        # redirected %ProgramData% or a profile on another drive needs.
        Move-Item -LiteralPath $legacy -Destination $SystemRoot -Force
    }
}

function Move-HandoffFilesIn {
    $target = Join-Path $SystemRoot $HandoffDirName
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    foreach ($name in $HandoffFileNames) {
        $source = Join-Path $SystemRoot $name
        if (Test-Path -LiteralPath $source) { Move-Item -LiteralPath $source -Destination (Join-Path $target $name) -Force }
    }
    Get-ChildItem -Path $SystemRoot -Filter $HandoffFileGlob -File -ErrorAction SilentlyContinue | ForEach-Object {
        Move-Item -LiteralPath $_.FullName -Destination (Join-Path $target $_.Name) -Force
    }
    # Stale POSIX socket files, from a data directory copied off a Mac or a
    # Linux box. Nothing on Windows binds either name -- both control channels
    # are named pipes -- so these are dead files rather than dead sockets, but
    # leaving them would make a `dir` of handoff\ misleading.
    foreach ($stale in @('companion.sock', (Join-Path $AuthorityDirName 'control.sock'))) {
        $path = Join-Path $SystemRoot $stale
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}

function Move-HandoffFilesOut {
    # The reverse, for disable: with no marker, handoff_dir() *is* data_dir(),
    # so every one of these has to be back at the root or the agent loses its
    # token and the shim loses the daemon.
    $source = Join-Path $SystemRoot $HandoffDirName
    if (-not (Test-Path -LiteralPath $source)) { return }
    Get-ChildItem -Path $source -Force | ForEach-Object {
        Move-Item -LiteralPath $_.FullName -Destination (Join-Path $SystemRoot $_.Name) -Force
    }
    Remove-Item -LiteralPath $source -Force -ErrorAction SilentlyContinue
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
    Move-HandoffFilesIn

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
    # Files carried in from %LOCALAPPDATA% keep the ACL they had there -- a
    # move preserves the security descriptor, unlike a create, which inherits.
    # mcp_token in particular is reused across restarts and would otherwise
    # stay unreadable to the agent forever. This is the Windows counterpart of
    # the POSIX scripts' find -exec chmod 640.
    Get-ChildItem -Path $handoff -Force -ErrorAction SilentlyContinue | ForEach-Object {
        Invoke-Icacls @($_.FullName, '/reset', '/q')
    }
}

function Write-Marker {
    $marker = Join-Path $SystemRoot $MarkerName
    Write-Note "writing $marker"
    $payload = [ordered]@{
        version         = $MarkerVersion
        platform        = 'win32'
        service_account = $ServiceAccount
        service_group   = $ServiceGroup
        owner_user      = $script:OwnerUser
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
    Invoke-Sc @(
        'create', $ServiceName,
        'binPath=', "`"$($script:DaemonExec)`" --windows-service",
        'obj=', $ServiceAccount,
        'password=', '',
        'start=', 'auto',
        'DisplayName=', 'PrivacyFence'
    ) | Out-Null
    Invoke-Sc @('description', $ServiceName, 'Runs the PrivacyFence approval daemon under its own account (issue #428 Phase 4).') | Out-Null
    # Crash restart, the thing the Scheduled Task's repeating TimeTrigger was
    # standing in for before there was a service manager involved: three
    # restarts with a widening delay, and the counter resets after a day.
    Invoke-Sc @('failure', $ServiceName, 'reset=', '86400', 'actions=', 'restart/5000/restart/10000/restart/30000') | Out-Null
    Invoke-Sc @('start', $ServiceName) | Out-Null
}

function Uninstall-DaemonService {
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
    Write-Note "stopping and removing the $ServiceName service"
    Invoke-Sc @('stop', $ServiceName) -IgnoreFailure | Out-Null
    Invoke-Sc @('delete', $ServiceName) -IgnoreFailure | Out-Null
}

function Disable-DaemonTask {
    # The installer's own autostart task starts the *daemon* in the logged-in
    # user's session, which on a separated install now refuses to start rather
    # than quietly seeding a default policy over the real one
    # (privilege_separation.check_runtime_identity). Disabled rather than
    # deleted so `disable` can put it back, and so the uninstaller's own
    # schtasks /delete still finds it.
    $task = Get-ScheduledTask -TaskName $DaemonTaskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    if ($task.State -eq 'Disabled') { return }
    Write-Note "disabling the '$DaemonTaskName' scheduled task (it would start a second daemon in your own session)"
    Disable-ScheduledTask -TaskName $DaemonTaskName | Out-Null
}

function Enable-DaemonTask {
    $task = Get-ScheduledTask -TaskName $DaemonTaskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    Write-Note "re-enabling the '$DaemonTaskName' scheduled task"
    Enable-ScheduledTask -TaskName $DaemonTaskName | Out-Null
}

function Install-CompanionTask {
    $template = Join-Path $TemplateDir 'privacyfence-companion-task.xml.tmpl'
    if (-not (Test-Path -LiteralPath $template)) {
        Stop-WithError "missing template: $template"
    }
    Write-Note "registering the '$CompanionTaskName' scheduled task"
    $xml = (Get-Content -LiteralPath $template -Raw).Replace('__EXEC_PATH__', $script:CompanionExec)
    $xmlFile = Join-Path ([System.IO.Path]::GetTempPath()) 'privacyfence-companion-task.xml'
    # Same encoding trap privacyfence-task.xml.tmpl's own header comment
    # documents: schtasks hands the file to MSXML as a Unicode stream, and a
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
    Invoke-Native -FilePath 'schtasks.exe' `
        -Arguments @('/delete', '/tn', $CompanionTaskName, '/f') -IgnoreFailure | Out-Null
}

# ── Subcommands ──────────────────────────────────────────────────────────────

function Invoke-Enable {
    Assert-Windows
    Assert-Administrator
    Resolve-Owner
    Resolve-Executables
    Assert-ImageProtected

    if (Test-Path -LiteralPath (Join-Path $SystemRoot $MarkerName)) {
        Write-Note 'already separated -- re-running to refresh the account, ACLs, service and task'
    }

    Disable-DaemonTask
    Uninstall-DaemonService

    New-ServiceGroup
    Move-Data
    Set-Layout
    Write-Marker
    Install-DaemonService
    Install-CompanionTask

    Write-Host @"

OK  PrivacyFence now runs as $ServiceAccount.

  Data directory   $SystemRoot
  Human authority  $SystemRoot\$AuthorityDirName   ($ServiceAccount only)
  Shared handoff   $SystemRoot\$HandoffDirName   (read-only to $ServiceGroup)
  Daemon           sc.exe query $ServiceName
  Companion        the PrivacyFence tray icon, started at sign-in

  One thing left to do by hand: sign $($script:OwnerUser) out and back in.
  Windows puts group memberships in the logon token, so the session you are in
  right now still does not know it is in $ServiceGroup -- which means the
  companion app and your MCP client cannot read the handoff directory until you
  do. '... status' will tell you when it has taken.

  What this does and does not buy you is written down in
  docs/security-and-compliance.md's "Local-mode trust boundary" section. The
  short version: the agent can no longer rewrite your policy, forge a passkey
  or read the audit key -- and an agent that can get Administrator still
  defeats all of it, because Administrator defeats everything.
"@
}

function Invoke-Disable {
    Assert-Windows
    Assert-Administrator
    Resolve-Owner

    $marker = Join-Path $SystemRoot $MarkerName
    if (-not (Test-Path -LiteralPath $marker)) {
        Stop-WithError "this install is not privilege-separated (no $marker)"
    }

    Uninstall-DaemonService
    Uninstall-CompanionTask

    # The marker goes first: if anything below fails, what is left behind is an
    # unseparated install pointing at a directory that still exists, rather
    # than a separated one whose service is gone.
    Remove-Item -LiteralPath $marker -Force
    Move-HandoffFilesOut

    $legacy = Get-LegacyDataDir
    if (Test-Path -LiteralPath $legacy) {
        Write-Warn "$legacy already exists -- merging $SystemRoot into it"
        Copy-Item -Path (Join-Path $SystemRoot '*') -Destination $legacy -Recurse -Force
        Remove-Item -LiteralPath $SystemRoot -Recurse -Force
    } else {
        Write-Note "moving $SystemRoot back to $legacy"
        Move-Item -LiteralPath $SystemRoot -Destination $legacy -Force
    }
    # Back to what %LOCALAPPDATA% gives an ordinary directory: inherited from
    # the user's own profile, which is what protected this data before
    # separation and what will protect it again afterwards.
    #
    # -IgnoreFailure on both, unlike every icacls call in `enable`: the data
    # has already been moved by the time these run, so aborting here would
    # leave the user with their files back under their own profile and a
    # script that reported failure -- the most confusing outcome available.
    # `/c` already tells icacls to continue past an individual entry it
    # cannot rewrite, so a non-zero exit here means "some entries were
    # skipped", which is a warning worth printing and not a reason to stop.
    Invoke-Icacls @($legacy, '/reset', '/t', '/c', '/q') -IgnoreFailure
    Invoke-Icacls @($legacy, '/setowner', $script:OwnerUser, '/t', '/c', '/q') -IgnoreFailure

    Enable-DaemonTask

    Write-Host @"

OK  Privilege separation is off. Your data is back at $legacy, under your own
    account again.

    The $ServiceGroup local group is left in place on purpose -- it owns
    nothing now, and keeping it means re-enabling does not have to re-add
    anyone. Remove it with:
      Remove-LocalGroup -Name $ServiceGroup

    The $ServiceAccount virtual account needs no cleanup at all: it existed
    only for as long as the service did.
"@
}

function Invoke-Status {
    Assert-Windows
    Resolve-Owner -Optional

    $marker = Join-Path $SystemRoot $MarkerName
    if (-not (Test-Path -LiteralPath $marker)) {
        Write-Host 'privilege separation: OFF'
        $legacy = Get-LegacyDataDir
        if ($legacy) { Write-Host "  data directory: $legacy" }
        Write-Host "  the daemon and the AI agent run as the same account ($($script:OwnerUser))."
        Write-Host '  Run this script with "enable", from an elevated PowerShell, to change that.'
        return 0
    }

    Write-Host 'privilege separation: ON'
    Write-Host "  marker:          $marker"
    Write-Host "  data directory:  $SystemRoot"
    $problems = 0
    $authority = Join-Path $SystemRoot $AuthorityDirName
    $handoff = Join-Path $SystemRoot $HandoffDirName

    foreach ($dir in @($SystemRoot, $authority, $handoff)) {
        if (-not (Test-Path -LiteralPath $dir)) {
            Write-Host "  MISSING          $dir"
            $problems = 1
        }
    }

    if (Test-Path -LiteralPath $SystemRoot) {
        $listable = @(Get-Acl -LiteralPath $SystemRoot | Select-Object -ExpandProperty Access |
            Where-Object { (Test-RuleGrantsRead -Rule $_) -and -not (Test-TrustedIdentity -Identity $_.IdentityReference.Value) -and $_.IdentityReference.Value -ine $ServiceAccount })
        if ($listable.Count -gt 0) {
            Write-Host "  LISTABLE         $SystemRoot can be enumerated by: $(($listable | ForEach-Object { $_.IdentityReference.Value }) -join ', ')"
            $problems = 1
        } else {
            Write-Host "  ok               $SystemRoot is traversable but not listable"
        }
    }

    if (Test-Path -LiteralPath $authority) {
        $exposed = @(Get-Acl -LiteralPath $authority | Select-Object -ExpandProperty Access |
            Where-Object { $_.AccessControlType -eq 'Allow' -and -not (Test-TrustedIdentity -Identity $_.IdentityReference.Value) -and $_.IdentityReference.Value -ine $ServiceAccount })
        if ($exposed.Count -gt 0) {
            Write-Host "  NOT SEPARATED    $authority is reachable by: $(($exposed | ForEach-Object { $_.IdentityReference.Value }) -join ', ')"
            $problems = 1
        } else {
            Write-Host "  ok               $authority is reachable only by $ServiceAccount"
        }
    }

    if (Test-Path -LiteralPath $handoff) {
        $groupRules = @(Get-Acl -LiteralPath $handoff | Select-Object -ExpandProperty Access |
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

    if ($script:OwnerUser) {
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

    $daemonTask = Get-ScheduledTask -TaskName $DaemonTaskName -ErrorAction SilentlyContinue
    if ($daemonTask -and $daemonTask.State -ne 'Disabled') {
        Write-Host "  STILL AUTOSTARTS the '$DaemonTaskName' task would start a second daemon in your own session"
        $problems = 1
    }
    if (Get-ScheduledTask -TaskName $CompanionTaskName -ErrorAction SilentlyContinue) {
        Write-Host "  ok               the '$CompanionTaskName' task starts the companion at sign-in"
    } else {
        Write-Host "  NOT INSTALLED    the '$CompanionTaskName' task -- no tray icon, and connector OAuth cannot open a browser"
        $problems = 1
    }

    return $problems
}

switch ($Command) {
    'enable' { Invoke-Enable; exit 0 }
    'disable' { Invoke-Disable; exit 0 }
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
}
