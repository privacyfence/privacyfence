; PrivacyFence Windows installer (Inno Setup 6).
;
; The Windows analogue of build_dmg.sh's DMG: one distributable
; carrying both the daemon and the Claude Desktop extension (.mcpb), plus
; the privilege-separation step (ADR 0003 decision 4) that installs the
; daemon as a Windows service and the companion's sign-in task, and the
; matching `uninstall` step on the way out (ADR 0042).
;
; Built by scripts/build_installer.ps1, which passes every {#...} value below
; on the command line (/D...) rather than hardcoding a version or absolute
; paths here -- this file has no VERSION of its own to keep in sync, same
; "derive it from the git tag, don't hand-bump a second copy" reasoning this
; repo's CLAUDE.md gives for pyproject.toml/__init__.py.
;
; Do not run this directly with defaults -- it expects every /D on
; build_installer.ps1's iscc.exe invocation to be supplied.

#ifndef AppVersion
  #error "Pass /DAppVersion=x.y.z (see scripts/build_installer.ps1)"
#endif
#ifndef DistDir
  #error "Pass /DDistDir=<path to dist/PrivacyFenceApp> (see scripts/build_installer.ps1)"
#endif
#ifndef McpbPath
  #error "Pass /DMcpbPath=<path to the built .mcpb> (see scripts/build_installer.ps1)"
#endif
#ifndef IconPath
  #error "Pass /DIconPath=<path to privacyfence.ico> (see scripts/build_installer.ps1)"
#endif
#ifndef OutputDir
  #error "Pass /DOutputDir=<output directory> (see scripts/build_installer.ps1)"
#endif
#ifndef SetupBaseName
  #error "Pass /DSetupBaseName=<setup exe base name, no extension> (see scripts/build_installer.ps1)"
#endif

#define AppName "PrivacyFence"
#define AppExeName "PrivacyFenceApp.exe"
#define AliasExeName "privacyfence-app.exe"
; Matches daemon.ts's Windows DEFAULT_APP_PATH -- keep these in sync if this changes.
#define InstallDirName "PrivacyFence"
; #428 Phase 4 (B5c). Both of these are created by
; scripts/windows_privilege_separation.ps1's `enable` -- which, since ADR 0003
; decision 4, [Code]'s CurStepChanged(ssPostInstall) below runs as part of
; every install. They are still named here rather than derived, because
; *uninstall* keeps a floor under that script's own `uninstall` (see
; [UninstallRun]) that does not depend on it running at all. Kept in
; sync with privilege_separation.WINDOWS_COMPANION_TASK_NAME /
; WINDOWS_SERVICE_NAME (tests/unit/test_privilege_separation.py asserts it).
#define CompanionTaskName "PrivacyFenceCompanion"
#define ServiceName "PrivacyFence"
#define CompanionExeName "PrivacyFenceCompanion.exe"

[Setup]
AppId={{B6E3B6C4-6C2E-4A8B-9C4C-3B6C6E7C6C1B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=PrivacyFence
DefaultDirName={autopf}\{#InstallDirName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir={#OutputDir}
OutputBaseFilename={#SetupBaseName}
SetupIconFile={#IconPath}
Compression=lzma2
SolidCompression=yes
; Setup runs privilege-separation.ps1 `enable` (CurStepChanged below), which
; creates a Windows service, a local group and an ACL'd %ProgramData%
; directory -- none of which a non-elevated token can do. It also has to be a
; machine-wide install: Assert-ImageProtected refuses to run a service out of
; a directory the signed-in user can rewrite. This used to be `lowest`; why
; that stopped working (#410: a LogonTrigger task needs
; SeCreateGlobalPrivilege) is in git history, with the daemon sign-in task it
; was about.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
; The OS floor (docs/platform-support.md's support matrix;
; tests/unit/test_minimum_os_versions.py keeps the two in step): Windows 10 /
; Server 2016. Setup refuses to run on anything older rather than installing
; an app nothing has built or tested for.
MinVersion=10.0
; ADR 0045. RestartManager is the backstop behind [Code]'s PrepareToInstall,
; which stops the service and ends every PrivacyFence process itself, and a
; backstop has to be able to close what it finds. The default, `yes`, only
; asks: none of PrivacyFence's processes has a window that answers (the daemon
; is headless, the companion a tray icon), so RmShutdown fails with "Some
; applications could not be shut down", and under /SUPPRESSMSGBOXES Inno's
; Abort/Retry/Ignore answers Abort -- exit 5, three times before
; PrepareToInstall existed. `force` terminates what does not answer, which is
; no more than PrepareToInstall's own taskkill /F already does.
CloseApplications=force

[Files]
; The whole onedir PyInstaller output -- PrivacyFenceApp.exe,
; privacyfence-app.exe (built as a real copy, not a symlink; see
; build_installer.ps1 step 4), and every bundled dependency/data file.
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; The Claude Desktop extension, alongside the daemon -- mirrors the DMG's
; "one distributable carries both halves" (build_dmg.sh's own module
; comment). Kept at its versioned filename so a user who's kept an older
; installer's copy doesn't collide with it.
Source: "{#McpbPath}"; DestDir: "{app}"; Flags: ignoreversion
; #428 Phase 4 (B5c): the privilege-separation tool and the companion
; autostart task it registers. Both are *installed* rather than extracted to
; {tmp}, because both outlive the install: the script is how a human inspects
; (`status`) the separation afterwards and what the uninstaller runs
; (`uninstall`), and the template is what a later re-run of `enable` renders
; the companion task from.
;
; This used to be the whole of it -- the script shipped, nothing ran it, and a
; Windows install stayed unseparated until somebody typed `enable` into an
; elevated PowerShell. ADR 0003 decision 4 withdraws that: [Code]'s
; CurStepChanged(ssPostInstall) below runs `enable` itself, with Setup's own
; elevated token, and a failure of that step fails the install. The .deb's
; postinst is the same shape; the difference was never a design, only which
; platform had an installer hook wired up.
;
; Renamed to privilege-separation.ps1 on the way in, and the template lands
; beside it: the script resolves its template directory as "the checkout's
; installer\windows, or my own directory", so a real install finds it next
; to itself with no path threaded through. That name is also what
; privilege_separation.PLATFORM_LAYOUTS' Windows status_command quotes at
; anyone reading a daemon log.
Source: "..\scripts\windows_privilege_separation.ps1"; DestDir: "{app}"; \
    DestName: "privilege-separation.ps1"; Flags: ignoreversion
Source: "windows\privacyfence-companion-task.xml.tmpl"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; ADR 0031: opens Approvals through the companion (--launch), which starts
; the tray icon first if it isn't running -- the same thing a double-click on
; the macOS app does. Not the daemon executable (a headless service; nothing
; to show), and no longer the bare settings URL it used to be, which only
; worked for a browser that already had a session cookie.
Name: "{group}\{#AppName}"; Filename: "{app}\{#CompanionExeName}"; Parameters: "--launch"; \
    IconFilename: "{app}\{#AppExeName}"
; #428 Phase 4 (B5c): the companion app (ADR 0002), as a thing a human can
; start by hand. On a privilege-separated install it is started at sign-in by
; its own Scheduled Task and this shortcut is the recovery path when that tray
; icon has been quit or has crashed -- the daemon is a service by then, so
; there is nothing else in the user's session that can mint a sign-in link or
; open a browser for a connector's OAuth flow. On an ordinary install it is
; simply the opt-in way to run it, which is what ADR 0002 Phase 3 always
; intended. The entry above covers the same recovery (--launch starts the
; tray when none is running) and opens Approvals as well; this one only
; starts the tray.
Name: "{group}\{#AppName} Companion"; Filename: "{app}\{#CompanionExeName}"; \
    IconFilename: "{app}\{#CompanionExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; The daemon has no sign-in task: it is a Windows service, installed and
; started by privilege-separation.ps1 `enable` (CurStepChanged below), and the
; only thing started at sign-in is the companion, by the PrivacyFenceCompanion
; task that same step registers.
;
; There is deliberately no "Launch PrivacyFence now" entry here any more.
; Until ADR 0003 decision 4 this section started {#AliasExeName} on the
; Finish page, because otherwise nothing would be running until the next
; sign-in. Both halves of that reasoning are gone now that the install
; separates itself: `enable` starts the PrivacyFence service (sc.exe start)
; and asks Task Scheduler to run the companion in the signed-in session, so
; there is already a daemon serving and a tray icon on the way; and starting
; {#AliasExeName} directly would be *refused* rather than redundant --
; privilege_separation.check_runtime_identity() fails closed for a daemon
; started as the logged-in user against a separated install, which is exactly
; what that checkbox would produce. A Finish-page option whose only outcome is
; an error dialog is worse than no option.
;
; Offer to open the bundled .mcpb right after install (privacyfence/
; privacyfence#407) -- without this, a user has to already know the .mcpb
; ships alongside the daemon rather than being downloaded separately, before
; they can even start looking for it in File Explorer. Its
; installed name matches the [Files] entry above, which copies {#McpbPath}
; into {app} keeping the source filename -- scripts/build_installer.ps1
; builds it as "{#AppName}-{#AppVersion}.mcpb" (ProductName-Version.mcpb),
; so that's reconstructed here rather than threaded through as its own /D
; value.
;
; Two mutually exclusive entries, gated by IsMcpbAssociated() below (see
; [Code]), because "shellexec" only actually does something useful once
; Windows has a real, working .mcpb file association to dispatch to. That
; is *not* the same thing as "Claude Desktop is installed": per Anthropic's
; own docs, Claude Desktop's own installer does not always register the
; .mcpb association cleanly on Windows on the first try, so a machine that
; already has Claude Desktop can still have nothing at HKCR\.mcpb -- this
; was reported against the single-entry shellexec-always version of this
; section, on a machine with Claude Desktop already installed. So this
; can't be "does Claude Desktop's installer exist somewhere" logic; it has
; to be a direct read of the registry state ShellExecute will actually use.
; Whatever the specific reason -- Claude Desktop not installed at all, or
; installed but the association didn't take -- ShellExecuteEx has nothing
; to hand the file to, and Windows answers with its own "how do you want
; to open this file?" picker instead of anything PrivacyFence-specific.
; That's a dead end for the user, not a helpful prompt, so in that case
; this shows File Explorer with the .mcpb pre-selected instead -- always
; succeeds, since explorer.exe needs no file association, and leaves the
; user able to either double-click it (if Claude Desktop is installed and
; they fix the association via Open With, see the README) or drag it onto
; Claude Desktop's own Settings > Extensions page, which accepts a drop
; regardless of file association.
; runascurrentuser on both entries below, and deliberately so: `postinstall`
; alone defaults to `runasoriginaluser`, which -- because PrivilegesRequired=
; admin means Setup itself always runs elevated -- makes Setup spawn a
; helper process under the original, pre-UAC-prompt user's token to open the
; file non-elevated instead. On a real install that mechanism failed
; outright with "Internal error: CallSpawnServer: Unexpected response: $0"
; rather than falling back to running elevated, which is what a machine
; runasoriginaluser cannot resolve an original user token on is documented
; to do. By that point in the Finish page the real work (files, privilege
; separation, the service, the companion task) is already done -- only this
; optional "open the .mcpb for me" convenience step was breaking, behind a
; dialog alarming enough to look like the whole install had failed.
; runascurrentuser skips the original-user spawn entirely and opens the file
; with Setup's own (already-elevated) token instead: the one cost is that
; whatever handles the .mcpb -- Claude Desktop, if IsMcpbAssociated -- can
; launch elevated this one time, which is a one-off inherited-token
; annoyance, not a privilege-separation hole (nothing about the daemon's own
; separation depends on how this Finish-page convenience runs).
Filename: "{app}\{#AppName}-{#AppVersion}.mcpb"; \
    Description: "Install {#AppName} into Claude Desktop"; \
    Flags: postinstall shellexec runascurrentuser skipifsilent; Check: IsMcpbAssociated
Filename: "{win}\explorer.exe"; \
    Parameters: "/select,""{app}\{#AppName}-{#AppVersion}.mcpb"""; \
    Description: "Show the {#AppName} Claude Desktop extension in File Explorer"; \
    Flags: postinstall runascurrentuser skipifsilent; Check: not IsMcpbAssociated

[UninstallRun]
; The floor under CurUninstallStepChanged(usUninstall) below, which runs
; privilege-separation.ps1 `uninstall` first and normally leaves nothing for
; these to do. They exist so that an uninstall never leaves a service pointing
; at program files it is about to delete, even if that script could not run
; at all (an execution policy, a script someone deleted). schtasks and sc.exe
; each exit non-zero for something that does not exist, which Inno ignores
; for an [UninstallRun] entry -- the right behavior: this must not fail an
; uninstall over a step that has already happened. RunOnceId so each runs
; once per uninstall even if Inno retries.
;
; Deleting the service is also what retires the NT SERVICE\PrivacyFence
; virtual account -- it exists only for as long as its service does.
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn ""{#CompanionTaskName}"" /f"; \
    Flags: runhidden; RunOnceId: "RemovePrivacyFenceCompanionTask"
Filename: "{sys}\sc.exe"; Parameters: "stop ""{#ServiceName}"""; \
    Flags: runhidden; RunOnceId: "StopPrivacyFenceService"
Filename: "{sys}\sc.exe"; Parameters: "delete ""{#ServiceName}"""; \
    Flags: runhidden; RunOnceId: "RemovePrivacyFenceService"

; No [UninstallDelete] section, deliberately. PrivacyFence's data --
; credentials, settings, policy, passkeys, the audit log -- lives under
; %ProgramData%\PrivacyFence, not under {app}, and uninstall keeps it (ADR
; 0042: removing the program stops PrivacyFence and leaves its data; purging
; deletes it). The "Delete PrivacyFence data" checkbox the uninstaller shows
; (CurUninstallStepChanged below) is the purge, and it is unchecked by
; default; a silent uninstall never purges. Nothing moves that data back into
; %LOCALAPPDATA% (ADR 0041).

[Code]
(* Backs the [Run] section's Check: on the two mutually-exclusive
   "open the .mcpb" entries above. HKCR is the merged classes-root view --
   HKCU\Software\Classes overlaid on HKLM\Software\Classes -- so this sees a
   per-user Claude Desktop install (matching this installer's own
   PrivilegesRequired=lowest, which can land per-user too) exactly as
   readily as a per-machine one; no separate HKCU fallback needed.

   Deliberately checks for a real command line under the ProgId, not just
   that HKCR\.mcpb has *a* ProgId value: a stale or partially-removed
   association (ProgId key present, shell\open\command missing) would
   otherwise still be reported as "associated" and ShellExecute would fail
   at Finish-click time exactly as if this check didn't exist. *)
function IsMcpbAssociated(): Boolean;
var
  ProgId: String;
begin
  Result := False;
  if RegQueryStringValue(HKCR, '.mcpb', '', ProgId) and (ProgId <> '') then
    Result := RegKeyExists(HKCR, ProgId + '\shell\open\command');
end;

(* Copies whatever a captured [Code] command wrote on stdout/stderr into
   Setup's own log file, one line per log entry, tagged with which step ran
   it.

   Everything a [Code] Exec() runs is otherwise invisible: Inno logs its
   own [Run] entries automatically but says nothing at all about an Exec()
   call made from [Code], and Exec() captures neither stream. A run of consecutive
   real windows-graphical-session.yml runs once established only that a task
   was missing afterwards, never why, until this carried schtasks' own stderr
   into the log -- which named the defect outright. *)
procedure LogCommandOutput(Prefix: String; OutFile: String);
var
  Lines: TArrayOfString;
  I: Integer;
begin
  if not FileExists(OutFile) then
  begin
    Log(Prefix + ': no output file at ' + OutFile);
    Exit;
  end;
  if not LoadStringsFromFile(OutFile, Lines) then
  begin
    Log(Prefix + ': could not read output file ' + OutFile);
    Exit;
  end;
  for I := 0 to GetArrayLength(Lines) - 1 do
    Log(Prefix + ': ' + Lines[I]);
end;

(* The same captured output, as one string, for a message a human will
   actually read -- Log() goes to Setup's log file, which nobody opens
   unprompted (the whole lesson of privacyfence/privacyfence#410). When the
   separation step below fails, what `enable` printed about *why* is the only
   useful thing Setup can say, so it is carried into the error itself. *)
function ReadCapturedOutput(OutFile: String): String;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if not FileExists(OutFile) then
    Exit;
  if not LoadStringsFromFile(OutFile, Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    Result := Result + Lines[I] + #13#10;
end;

(* ADR 0003 decision 4: the installer separates the install, rather than
   shipping privilege-separation.ps1 and hoping somebody runs it.

   Runs `enable` -- not `enable -ForUser` -- with Setup's own elevated token.
   The distinction matters and is ADR 0003 decision 3's: `enable` does every
   step an administrator can take alone (creates the NT SERVICE\PrivacyFence
   virtual account, moves and re-owns %ProgramData%\PrivacyFence, writes the
   marker, installs the service and the companion task) and, for the one step
   that needs to know which human this install is for, adds whoever is running
   Setup to PrivacyFenceUsers -- or, in a SYSTEM-context/MDM install where no
   human account resolves at all, records that membership as pending for the
   companion to close at the first real sign-in. Either way the install ends
   up separated, which is the thing decision 1 is about.

   `enable`'s own refusals are deliberately left exactly as they are. The one
   that can actually fire here is Assert-ImageProtected: an install directory
   the logged-in user can rewrite cannot be separated, because a service runs
   whatever its binPath names. Under PrivilegesRequired=admin, {autopf} is
   %ProgramFiles% and that refusal does not fire; a /DIR= pointed somewhere
   user-writable is the case it exists for, and failing the install there is
   the correct outcome, not a rough edge to smooth over.

   Via cmd.exe for one reason only: Exec() captures neither stream, and what `enable` printed is the only
   explanation of a failure there will ever be. -NoProfile so an administrator
   profile script cannot change what runs, -NonInteractive so a prompt can
   never wait forever behind SW_HIDE on an unattended install, and
   -ExecutionPolicy Bypass because a machine-wide policy of AllSigned or
   Restricted would otherwise block a script Setup just installed itself.

   Returns the captured output through Output whether it succeeded or not. *)
function SeparateInstall(var Output: String): Boolean;
var
  ScriptPath, OutFile, CmdLine: String;
  ResultCode: Integer;
begin
  Result := False;
  Output := '';
  try
    ScriptPath := ExpandConstant('{app}\privilege-separation.ps1');
    OutFile := ExpandConstant('{tmp}\privilege-separation.out');
    Log('SeparateInstall: running ' + ScriptPath + ' enable');
    (* The doubled outer quotes are cmd.exe's own rule for a /C command line
       whose first token is itself a quoted path: cmd strips the outermost
       pair and runs what is left. *)
    CmdLine := '/C ""' + ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe') +
      '" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ScriptPath +
      '" enable > "' + OutFile + '" 2>&1"';
    if not Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode) then
    begin
      Log('SeparateInstall: Exec itself failed to launch cmd.exe');
      Output := 'Setup could not start powershell.exe at all.';
      Exit;
    end;
    LogCommandOutput('SeparateInstall', OutFile);
    Log('SeparateInstall: exit code = ' + IntToStr(ResultCode));
    Output := ReadCapturedOutput(OutFile);
    Result := ResultCode = 0;
  except
    Log('SeparateInstall: exception: ' + GetExceptionMessage);
    Output := GetExceptionMessage;
    Result := False;
  end;
end;

(* The companion, the daemon's image and its non-service alias -- all three
   best effort, each a no-op against a process that is not running (taskkill
   exits non-zero, which this ignores). *)
procedure KillPrivacyFenceProcesses();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#CompanionExeName}"', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#AppExeName}"', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#AliasExeName}"', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

(* Which of those three images is still in the process table, as a
   space-separated list in Running ('' when none). Matched as a whole quoted
   CSV field, case-insensitively, over every session's processes -- Setup is
   elevated, so tasklist sees them all. Returns False when tasklist itself
   could not be run, so the caller can tell "none running" from "could not
   look". *)
function ListPrivacyFenceProcesses(var Running: String): Boolean;
var
  OutFile, CmdLine, Listing: String;
  ResultCode: Integer;
begin
  Result := False;
  Running := '';
  OutFile := ExpandConstant('{tmp}\prepare-tasklist.out');
  CmdLine := '/C ""' + ExpandConstant('{sys}\tasklist.exe') +
    '" /NH /FO CSV > "' + OutFile + '" 2>&1"';
  if not Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) then
  begin
    Log('PrepareToInstall: Exec itself failed to launch cmd.exe for tasklist');
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    LogCommandOutput('PrepareToInstall: tasklist', OutFile);
    Log('PrepareToInstall: tasklist exit code = ' + IntToStr(ResultCode));
    Exit;
  end;
  Listing := Lowercase(ReadCapturedOutput(OutFile));
  if Pos('"' + Lowercase('{#CompanionExeName}') + '"', Listing) > 0 then
    Running := Running + ' {#CompanionExeName}';
  if Pos('"' + Lowercase('{#AppExeName}') + '"', Listing) > 0 then
    Running := Running + ' {#AppExeName}';
  if Pos('"' + Lowercase('{#AliasExeName}') + '"', Listing) > 0 then
    Running := Running + ' {#AliasExeName}';
  Result := True;
end;

(* ADR 0045: stop whatever a previous
   install left running BEFORE Setup copies a single file over it.

   PrepareToInstall is Inno Setup's own hook for exactly this timing -- it
   runs ahead of ssInstall, where [Files] actually copies, unlike
   CurStepChanged(ssPostInstall) above which runs *after*. df1a403d fixed
   this gap only in tests/integration/test_windows_packaged_smoke.py's own
   test harness, around its own upgrade-install step; this is the same fix
   ported into the installer itself, which is what a real upgrade -- not
   just the test's own -- needed all along. It closes two failures:
   v4.1.0a9's real release build hit "Some applications could not be shut
   down" (exit 5) because RestartManager did not win the race against a
   still-running privacyfence-app, and 3079c985 met the same with
   PrivacyFenceCompanion; a separated install's daemon is the harder case,
   because it runs as a Windows service with crash-restart failure actions
   configured (Install-DaemonService's own `sc failure ...` call), so
   killing it by image name only buys about five seconds before the SCM
   relaunches it and Setup's four one-second DeleteFile retries run out
   against the replacement (df1a403d) -- a clean `sc.exe stop` is required,
   not a taskkill. Since ADR 0045 the harness does none of this any more:
   its upgrade test runs Setup once, with no sweep and no retry, so it
   proves this hook rather than repeating it.

   Best-effort throughout, and always returns '' (success): a fresh install
   has no prior service or processes to stop at all, which is the ordinary
   case, not a failure one, and this hook's only job is making sure nothing
   already has a file open before Setup starts overwriting it -- Setup's own
   file-in-use retry and RestartManager (CloseApplications=force, [Setup]
   above) are still the last line of defense, this just gives them nothing
   to do. *)
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OutFile, CmdLine, Running: String;
  ResultCode, Attempt: Integer;
  StillRunning, Listed: Boolean;
begin
  Result := '';
  try
    Log('PrepareToInstall: stopping the {#ServiceName} service (if any) before copying files');
    OutFile := ExpandConstant('{tmp}\prepare-stop-service.out');
    (* Same doubled-outer-quotes /C rule as SeparateInstall's own cmd.exe
       invocation above -- see its comment for why. *)
    CmdLine := '/C ""' + ExpandConstant('{sys}\sc.exe') +
      '" stop "{#ServiceName}" > "' + OutFile + '" 2>&1"';
    if not Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode) then
      Log('PrepareToInstall: Exec itself failed to launch cmd.exe for sc.exe stop')
    else
    begin
      LogCommandOutput('PrepareToInstall: sc.exe stop', OutFile);
      Log('PrepareToInstall: sc.exe stop exit code = ' + IntToStr(ResultCode));
    end;

    (* Poll `sc.exe query` for up to 30s (60 attempts, 500ms apart). A nonzero exit from
       `sc.exe query` means the service does not exist at all (a fresh
       install, or one already uninstalled), which ends the wait
       immediately rather than polling out the full 30s for an answer that
       was never going to change. *)
    StillRunning := True;
    Attempt := 0;
    while StillRunning and (Attempt < 60) do
    begin
      Attempt := Attempt + 1;
      OutFile := ExpandConstant('{tmp}\prepare-query-service.out');
      CmdLine := '/C ""' + ExpandConstant('{sys}\sc.exe') +
        '" query "{#ServiceName}" > "' + OutFile + '" 2>&1"';
      if not Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode) then
      begin
        Log('PrepareToInstall: Exec itself failed to launch cmd.exe for sc.exe query');
        StillRunning := False;
      end
      else if ResultCode <> 0 then
        StillRunning := False
      else
        StillRunning := (Pos('STOPPED', ReadCapturedOutput(OutFile)) = 0);
      if StillRunning then
        Sleep(500);
    end;
    if StillRunning then
      Log('PrepareToInstall: the {#ServiceName} service did not reach STOPPED within 30s -- continuing anyway')
    else
      Log('PrepareToInstall: the {#ServiceName} service is stopped (or was never installed)');

    (* Then every remaining PrivacyFence process, and -- ADR 0045 -- wait
       until they are actually gone rather than only told to go. taskkill /F
       returns once TerminateProcess is issued, not once the process has
       exited and released its image; Setup queries RestartManager the
       moment this hook returns, and a process still tearing down then is
       one it will try to close. Re-killing on every poll also covers one
       that is started again in the window. Up to 30s, 500ms apart, like
       the service wait above; the log line below is what
       tests/integration/test_windows_packaged_smoke.py's upgrade test
       checks. *)
    Log('PrepareToInstall: sweeping stray PrivacyFence processes');
    KillPrivacyFenceProcesses();
    Attempt := 0;
    while True do
    begin
      Listed := ListPrivacyFenceProcesses(Running);
      if (not Listed) or (Running = '') or (Attempt >= 60) then
        Break;
      Attempt := Attempt + 1;
      Sleep(500);
      KillPrivacyFenceProcesses();
    end;
    if not Listed then
      Log('PrepareToInstall: could not list processes to confirm the sweep -- continuing anyway')
    else if Running <> '' then
      Log('PrepareToInstall: still running after 30s:' + Running + ' -- continuing anyway')
    else
      Log('PrepareToInstall: no PrivacyFence process is still running');
  except
    (* Never let a hiccup here fail the install before it has even started
       copying files -- the whole point of this hook is a convenience on top
       of Setup's own file-in-use handling, not a new way to refuse an
       install that would otherwise have succeeded. *)
    Log('PrepareToInstall: exception: ' + GetExceptionMessage);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  SeparationOutput: String;
begin
  if CurStep = ssPostInstall then
  begin
    (* A failure here aborts the install. That is ADR 0003 decision 1 in one
       line: a PrivacyFence that cannot separate itself still renders the
       same approval dialogs, accepts the same passkey enrollment and writes
       the same audit log, none of which mean what they say when the daemon
       and the AI client it governs share an account. Shipping that silently
       is the outcome this refuses.

       RaiseException here stops the rest of ssPostInstall and,
       interactively, shows the message below instead of a success screen. It
       does *not* make Setup's own process exit code non-zero -- Pascal
       Scripting's Abort()/RaiseException only affects Setup's exit code when
       raised from InitializeSetup, InitializeWizard or
       CurStepChanged(ssInstall); by ssPostInstall the "actual installation
       process" Setup's own exit-code table (codes 3/4/7/8) means has already
       finished, and nothing run afterward can retroactively fail it.
       (Confirmed against Inno Setup's own Pascal Scripting reference for
       Abort, not assumed -- see privacyfence/privacyfence's build-failure
       notes for 4.1.0b1.) A caller that needs to detect this refusal
       programmatically -- as tests/integration/test_windows_packaged_smoke.py's
       own test_windows_install_fails_when_the_image_is_user_writable does --
       has to read the install log for SeparateInstall's own failure line (or
       check that the marker/service were never created), not the exit code.

       Every continuation line below starts with a quoted string, never a
       bare #13#10 -- Inno's preprocessor (ISPP) treats a line whose first
       non-blank character is '#' as a directive line, and "unknown
       preprocessor directive" is a compile-time error, not a Pascal one, so
       this bit it once already (privacyfence/privacyfence#411's own CI).
       Each #13#10 pair stays glued to the end of the previous line
       instead. *)
    if not SeparateInstall(SeparationOutput) then
    begin
      Log('SeparateInstall: FAILED; aborting the installation.');
      RaiseException(
        'PrivacyFence could not set up privilege separation, so it has not ' +
        'been installed.' + #13#10 + #13#10 +
        'PrivacyFence runs its approval daemon under a dedicated Windows ' +
        'account, so that the AI client it governs cannot rewrite its own ' +
        'policy, forge a passkey or read the audit log''s key. An install ' +
        'that cannot do that would still show you the same approval prompts ' +
        'while meaning something weaker by them, so it is not installed at ' +
        'all.' + #13#10 + #13#10 +
        'This usually means the install location can be written by the ' +
        'signed-in user. Installing into the default location under ' +
        'Program Files is what this expects.' + #13#10 + #13#10 +
        'The separation step reported:' + #13#10 + SeparationOutput);
    end;
  end;
end;

(* ADR 0042's remove/purge split, for the uninstaller: asks whether to delete
   PrivacyFence's data as well, with the answer defaulting to "no". A custom
   form rather than a Yes/No MsgBox so the choice reads as the checkbox it is
   ("Delete PrivacyFence data", unchecked) rather than as a second "are you
   sure?" that a reflexive Yes would answer destructively. Closing the form
   any other way than OK keeps the data too.

   Only ever called interactively -- CurUninstallStepChanged below never asks
   a silent uninstall anything, and a silent uninstall never purges. *)
function AskDeleteData(): Boolean;
var
  Form: TSetupForm;
  Prompt: TNewStaticText;
  DeleteData: TNewCheckBox;
  OkButton: TNewButton;
begin
  Result := False;
  (* Inno Setup 6.5+ takes the client size here; the parameterless form
     this used first does not compile on the 6.7 the build runners install. *)
  Form := CreateCustomForm(ScaleX(400), ScaleY(160), False, False);
  try
    Form.Caption := 'Uninstall {#AppName}';

    Prompt := TNewStaticText.Create(Form);
    Prompt.Parent := Form;
    Prompt.Left := ScaleX(12);
    Prompt.Top := ScaleY(12);
    Prompt.Width := Form.ClientWidth - ScaleX(24);
    Prompt.AutoSize := False;
    Prompt.Height := ScaleY(64);
    Prompt.WordWrap := True;
    Prompt.Caption :=
      '{#AppName} keeps its settings, connector sign-ins, policy, passkeys ' +
      'and audit log in ' + ExpandConstant('{commonappdata}') + '\{#AppName}. ' +
      'They are kept by default, so reinstalling picks them up again.';

    DeleteData := TNewCheckBox.Create(Form);
    DeleteData.Parent := Form;
    DeleteData.Left := ScaleX(12);
    DeleteData.Top := ScaleY(84);
    DeleteData.Width := Form.ClientWidth - ScaleX(24);
    DeleteData.Height := ScaleY(17);
    DeleteData.Caption := 'Delete {#AppName} data';
    DeleteData.Checked := False;

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'OK';
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    OkButton.Width := ScaleX(75);
    OkButton.Height := ScaleY(23);
    OkButton.Left := Form.ClientWidth - OkButton.Width - ScaleX(12);
    OkButton.Top := Form.ClientHeight - OkButton.Height - ScaleY(12);

    Form.ActiveControl := OkButton;
    if Form.ShowModal() = mrOk then
      Result := DeleteData.Checked;
  finally
    Form.Free();
  end;
end;

(* Runs privilege-separation.ps1 `uninstall` (or `uninstall -Purge`) with the
   uninstaller's own elevated token, before Inno deletes the program files
   the service runs. Same cmd.exe/-NoProfile/-NonInteractive/-ExecutionPolicy
   Bypass invocation, for the same reasons, as SeparateInstall above.

   Never fails the uninstall: the program files are going regardless, and
   the [UninstallRun] floor still stops and deletes the service and the
   companion task if this could not. A failed *purge* is reported
   interactively, because then data the user asked to delete is still on
   disk and they need to know where. *)
function RunSeparationUninstall(Purge: Boolean; var Output: String): Boolean;
var
  ScriptPath, OutFile, CmdLine, PurgeArg: String;
  ResultCode: Integer;
begin
  Result := False;
  Output := '';
  PurgeArg := '';
  if Purge then
    PurgeArg := ' -Purge';
  try
    ScriptPath := ExpandConstant('{app}\privilege-separation.ps1');
    if not FileExists(ScriptPath) then
    begin
      Log('RunSeparationUninstall: ' + ScriptPath + ' is missing -- leaving it to [UninstallRun]');
      Output := ScriptPath + ' is missing.';
      Exit;
    end;
    OutFile := ExpandConstant('{tmp}\privilege-separation-uninstall.out');
    Log('RunSeparationUninstall: running ' + ScriptPath + ' uninstall' + PurgeArg);
    CmdLine := '/C ""' + ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe') +
      '" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ScriptPath +
      '" uninstall' + PurgeArg + ' > "' + OutFile + '" 2>&1"';
    if not Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode) then
    begin
      Log('RunSeparationUninstall: Exec itself failed to launch cmd.exe');
      Output := 'The uninstaller could not start powershell.exe at all.';
      Exit;
    end;
    LogCommandOutput('RunSeparationUninstall', OutFile);
    Log('RunSeparationUninstall: exit code = ' + IntToStr(ResultCode));
    Output := ReadCapturedOutput(OutFile);
    Result := ResultCode = 0;
  except
    Log('RunSeparationUninstall: exception: ' + GetExceptionMessage);
    Output := GetExceptionMessage;
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Purge: Boolean;
  Output: String;
begin
  (* usUninstall: after the user has confirmed, before Inno processes the
     uninstall log -- so before [UninstallRun]'s floor and before any file
     under {app} (this script included) is deleted. *)
  if CurUninstallStep = usUninstall then
  begin
    Purge := False;
    if not UninstallSilent() then
      Purge := AskDeleteData();
    if not RunSeparationUninstall(Purge, Output) then
    begin
      Log('RunSeparationUninstall: FAILED; continuing the uninstall.');
      if Purge and (not UninstallSilent()) then
        MsgBox(
          '{#AppName} was uninstalled, but its data could not be deleted.' + #13#10 + #13#10 +
          'It is still in ' + ExpandConstant('{commonappdata}') + '\{#AppName}. ' +
          'Delete that folder from an administrator account to remove it.' + #13#10 + #13#10 +
          'The uninstall step reported:' + #13#10 + Output,
          mbError, MB_OK);
    end;
  end;
end;
