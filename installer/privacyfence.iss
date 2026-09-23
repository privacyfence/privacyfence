; PrivacyFence Windows installer (Inno Setup 6).
;
; The now-removed docs/windows-support-plan.md Phase 4 (B4 in the now-removed docs/windows-linux-support-
; plan.md) -- the Windows analogue of build_dmg.sh's DMG: one distributable
; carrying both the daemon and the Claude Desktop extension (.mcpb), plus
; (unlike the drag-to-Applications DMG) the autostart wiring a real installer
; can do that a disk image can't -- registering/removing the Task Scheduler
; task from Phase 3.
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
; Where the embedded web settings/approval UI listens by default -- see
; web/server.py's DEFAULT_PORT / default host. Not user-configurable at
; install time (no settings UI toggle exists for this on any platform
; today, same as Phase 3.3's autostart decision).
#define SettingsUrl "http://localhost:8765/settings"
; Matches daemon.ts's Windows DEFAULT_APP_PATH (docs/windows-support-
; plan.md Phase 7 / B6) -- keep these in sync if this changes.
#define InstallDirName "PrivacyFence"
#define TaskName "PrivacyFence"
; #428 Phase 4 (B5c). Both of these are created by
; scripts/windows_privilege_separation.ps1's `enable` -- which, since ADR 0003
; decision 4, [Code]'s CurStepChanged(ssPostInstall) below runs as part of
; every install. They are still named here rather than derived, because
; *uninstall* has to clean them up without running that script at all. Kept in
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
; This used to be `lowest`, on the theory that a single-user desktop daemon
; has no reason to demand an admin elevation prompt just to install under
; Program Files. That theory was wrong, and not in a way any amount of
; reasoning from documentation would have caught -- a real non-admin user
; hit it, the same failure first reported as privacyfence/privacyfence#410:
; a `lowest` install with no explicit "Run as administrator" resolves
; {autopf} to {userpf} and launches Setup with an ordinary, non-elevated
; token, and RegisterAutostartTask() below then fails outright on that
; token, every time, not occasionally. #410 was fixed by making that
; failure visible (a warning dialog instead of only a log line) and by
; widening the mcpb shim's own daemon lookup to also check the non-admin
; install location -- not by fixing the registration failure itself, which
; kept happening on every non-elevated install afterward. The reason is
; `schtasks /create /xml` registering a LogonTrigger task at all --
; regardless of whether its Principal is a GroupId or the calling user's
; own UserId -- needs the SeCreateGlobalPrivilege user right, which Windows
; grants by default only to Administrators, SERVICE, LOCAL SERVICE and
; NETWORK SERVICE. A UAC-filtered admin token (the ordinary, non-elevated
; token an admin account's own processes run with, same as a plain
; standard-user token for this purpose) does not carry it, so "Access is
; denied" is the deterministic outcome, not a flake -- the installer's own
; dialog asking the user to "report it if it keeps happening" was asking
; for reports of something that happens every time. `admin` makes Setup's
; own manifest require an elevated token before RegisterAutostartTask()
; ever runs, closing the gap at its actual cause rather than adding another
; fallback path around it.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

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
; The autostart task-definition template (see [Code]'s RegisterAutostartTask
; below) -- dontcopy means Setup extracts it to {tmp} for [Code] to read at
; install time, but it's never actually installed into {app}.
Source: "privacyfence-task.xml.tmpl"; Flags: dontcopy
; #428 Phase 4 (B5c): the privilege-separation tool and the companion
; autostart task it registers. Both are *installed* rather than extracted to
; {tmp}, because both outlive the install: the script is how a human inspects
; (`status`) or unwinds (`disable`) the separation afterwards, and the template
; is what a later re-run of `enable` renders the companion task from.
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
; Points at the web settings UI in the default browser, not at the daemon
; executable directly -- there's nothing useful to show for double-clicking
; a headless background daemon (same reasoning as the Linux .deb plan's
; P3.2 for its own .desktop entry).
Name: "{group}\{#AppName}"; Filename: "{#SettingsUrl}"; IconFilename: "{app}\{#AppExeName}"
; #428 Phase 4 (B5c): the companion app (ADR 0002), as a thing a human can
; start by hand. On a privilege-separated install it is started at sign-in by
; its own Scheduled Task and this shortcut is the recovery path when that tray
; icon has been quit or has crashed -- the daemon is a service by then, so
; there is nothing else in the user's session that can mint a sign-in link or
; open a browser for a connector's OAuth flow. On an ordinary install it is
; simply the opt-in way to run it, which is what ADR 0002 Phase 3 always
; intended; the Start Menu entry above still opens the settings page directly.
Name: "{group}\{#AppName} Companion"; Filename: "{app}\{#CompanionExeName}"; \
    IconFilename: "{app}\{#CompanionExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; The autostart Task Scheduler task itself (Phase 3.1) is registered from
; [Code]'s RegisterAutostartTask below, via CurStepChanged(ssPostInstall)
; -- not a [Run] entry -- see that function's own comment and
; privacyfence-task.xml.tmpl's header comment for what actually gets
; registered and why this needs a real Task Scheduler XML task definition
; rather than a plain `schtasks /create` CLI call (short version: two
; independent CLI-flag attempts at this were each found broken by a real
; windows-graphical-session.yml run -- invalid /ri/du flags for an ONLOGON
; schedule, then a trigger that only fired for the installing account
; instead of any interactive logon -- and the capability this task
; actually needs, "run for whichever user just logged on, in their own
; session, restarting on crash," has no equivalent exposed through
; schtasks.exe's plain flags at all).
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
; Must remove the scheduled task -- wired into the uninstaller here, not
; left as a manual step (Phase 3.2). RunOnceId so this only ever runs once
; per uninstall even if Inno retries the uninstall step.
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn ""{#TaskName}"" /f"; \
    Flags: runhidden; RunOnceId: "RemovePrivacyFenceTask"
; #428 Phase 4 (B5c): the two things a privilege-separated install adds, torn
; down here rather than left behind. Since ADR 0003 decision 4 every install
; has both; they are still tolerant of finding neither -- an install whose
; separation was unwound with `disable` first (the documented order, see
; [UninstallDelete] below) has already removed them, and schtasks and sc.exe
; each exit non-zero for something that does not exist, which Inno ignores for
; an [UninstallRun] entry. That is the right behavior either way: this must
; not fail an uninstall over a step that has already happened.
;
; Deleting the service is also what retires the NT SERVICE\PrivacyFence
; virtual account -- it exists only for as long as its service does, which is
; the whole appeal of a virtual account over a real one nobody would ever
; remember to delete.
;
; The data directory is deliberately not touched; see [UninstallDelete] below.
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn ""{#CompanionTaskName}"" /f"; \
    Flags: runhidden; RunOnceId: "RemovePrivacyFenceCompanionTask"
Filename: "{sys}\sc.exe"; Parameters: "stop ""{#ServiceName}"""; \
    Flags: runhidden; RunOnceId: "StopPrivacyFenceService"
Filename: "{sys}\sc.exe"; Parameters: "delete ""{#ServiceName}"""; \
    Flags: runhidden; RunOnceId: "RemovePrivacyFenceService"

[UninstallDelete]
; Explicitly scope what uninstall does NOT touch (Phase 4.3): per-user data
; -- credentials, settings, the audit log -- lives under
; %LOCALAPPDATA%\PrivacyFence\ (paths.py's data_dir(), via its
; windows_data_dir() branch -- see that function's own docstring for why
; that's a different convention from the POSIX ~/.privacyfence dotfile
; rather than the same name reused under %USERPROFILE%), created by the app
; on first run. Uninstalling removes the program files (handled
; automatically by Inno Setup for everything under {app}) and the scheduled
; task (UninstallRun, above) only -- there is deliberately no
; [UninstallDelete] entry naming %LOCALAPPDATA%\PrivacyFence, unlike the
; entries a "clean uninstall" for a typical app might add.
;
; #428 Phase 4 (B5c) adds a second such directory and the same rule applies
; to it: a privilege-separated install keeps its state under
; %ProgramData%\PrivacyFence instead, holding the same credentials, settings
; and audit log plus the policy and passkeys the service account owns. It is
; not removed here either. Since ADR 0003 decision 4 that is every install,
; not only one that opted in, so the order matters to everyone now:
; uninstalling while separated leaves a directory no account can read except
; the service account that no longer exists and the Administrators group --
; which is why scripts/windows_privilege_separation.ps1 disable, run *before*
; uninstalling, is the documented order (docs/platform-support.md). An
; administrator can still recover the directory afterwards by taking
; ownership of it.

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

   Everything about the autostart registration below was otherwise
   invisible: Inno logs its own [Run] entries automatically but says
   nothing at all about an Exec() call made from [Code], and Exec()
   captures neither stream. So a run of consecutive real
   windows-graphical-session.yml runs could establish only that the task
   was missing afterwards, never why -- the install log they dumped
   (tests/integration/test_windows_graphical_session_autostart.py's own
   _install_log_tail) went straight from "Installation process succeeded"
   to "Deinitializing Setup" with no trace of the registration attempt in
   between. An exit code alone would not have closed that gap either: when
   this finally ran, schtasks' own stderr was the thing that named the
   defect outright ("(1,40)::ERROR: unable to switch the encoding", the
   task XML's encoding declaration -- see privacyfence-task.xml.tmpl). *)
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

(* Registers the autostart Task Scheduler task (Phase 3.1) via a real Task
   Scheduler XML task definition (privacyfence-task.xml.tmpl, extracted to
   the temp directory by the [Files] "dontcopy" entry above), not
   schtasks.exe's plain /create flags -- see [Run]'s own comment and that
   template's own header comment for why. Substitutes the real installed
   AliasExeName path for the template's __EXEC_PATH__ placeholder, writes
   the result to a scratch file, then runs `schtasks /create /xml <file>
   /f` against it. Called from CurStepChanged(ssPostInstall) below, i.e.
   after the app's files are already in place (Files copy happens during
   ssInstall, before ssPostInstall) but before this file's own [Run]
   entries execute, so the path it substitutes in always exists by the
   time schtasks reads it.

   schtasks runs through cmd.exe rather than directly for one reason only:
   so its stdout and stderr can be redirected to a file and read back into
   the log (LogCommandOutput above).

   The whole body is wrapped in try/except with a Log() at each step. That
   was added while the failure was still unexplained, on the theory that an
   unhandled Pascal Script runtime exception was silently aborting Setup's
   post-install processing; the actual defect turned out to be in the task
   XML itself, and no exception was ever involved. It stays because the
   reasoning behind it holds regardless: an exception here would otherwise
   abort the rest of post-install with the overall install still reporting
   success, and nothing would say so.

   Deliberately using this parenthesis-asterisk comment style rather than
   curly braces: Pascal's curly-brace comments don't nest, and the
   {app}/{tmp}-style Inno constant references this comment needs to talk
   about would otherwise close the comment early at their own closing
   brace -- exactly the "'BEGIN' expected" compile error an earlier
   version of this comment actually hit. *)
function RegisterAutostartTask(): Boolean;
var
  TemplateFile, XmlFile, OutFile, XmlContent, ExecPath, CmdLine: String;
  RawContent: AnsiString;
  ResultCode: Integer;
begin
  Result := False;
  try
    Log('RegisterAutostartTask: starting');
    ExtractTemporaryFile('privacyfence-task.xml.tmpl');
    TemplateFile := ExpandConstant('{tmp}\privacyfence-task.xml.tmpl');
    Log('RegisterAutostartTask: template path = ' + TemplateFile);
    { LoadStringFromFile's own "var S" output parameter is typed
      AnsiString, not String -- passed as RawContent here and converted
      (a plain assignment allows the AnsiString/String conversion that a
      var parameter, like StringChangeEx's own first argument below,
      does not) rather than declared as the var parameter's own type
      throughout, so every other call in this function can use the
      ordinary String type. }
    if not LoadStringFromFile(TemplateFile, RawContent) then
    begin
      Log('RegisterAutostartTask: LoadStringFromFile returned False');
      Exit;
    end;
    Log('RegisterAutostartTask: loaded template, ' + IntToStr(Length(RawContent)) + ' bytes');
    XmlContent := RawContent;
    ExecPath := ExpandConstant('{app}\{#AliasExeName}');
    Log('RegisterAutostartTask: exec path = ' + ExecPath);
    StringChangeEx(XmlContent, '__EXEC_PATH__', ExecPath, False);
    XmlFile := ExpandConstant('{tmp}\privacyfence-task.xml');
    if not SaveStringToFile(XmlFile, XmlContent, False) then
    begin
      Log('RegisterAutostartTask: SaveStringToFile returned False');
      Exit;
    end;
    Log('RegisterAutostartTask: wrote ' + XmlFile + ', ' + IntToStr(Length(XmlContent)) + ' bytes, running schtasks');

    OutFile := ExpandConstant('{tmp}\schtasks-create.out');
    { The doubled outer quotes are cmd.exe's own rule for a /C command line
      whose first token is itself a quoted path: cmd strips the outermost
      pair and runs what is left. }
    CmdLine := '/C ""' + ExpandConstant('{sys}\schtasks.exe') +
      '" /create /tn "{#TaskName}" /xml "' + XmlFile + '" /f > "' + OutFile + '" 2>&1"';
    if not Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode) then
    begin
      Log('RegisterAutostartTask: Exec itself failed to launch cmd.exe');
      Exit;
    end;
    LogCommandOutput('RegisterAutostartTask: schtasks', OutFile);
    Log('RegisterAutostartTask: schtasks exit code = ' + IntToStr(ResultCode));
    Result := ResultCode = 0;
  except
    Log('RegisterAutostartTask: exception: ' + GetExceptionMessage);
    Result := False;
  end;
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

   Via cmd.exe for the same single reason RegisterAutostartTask goes through
   it: Exec() captures neither stream, and what `enable` printed is the only
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
    (* Same doubled-outer-quotes rule cmd.exe imposes on RegisterAutostartTask's
       own /C line: cmd strips the outermost pair and runs what is left. *)
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

(* #428 Phase 2 (local-mode-fixes-plan.md §2.1): stop whatever a previous
   install left running BEFORE Setup copies a single file over it.

   PrepareToInstall is Inno Setup's own hook for exactly this timing -- it
   runs ahead of ssInstall, where [Files] actually copies, unlike
   CurStepChanged(ssPostInstall) above which runs *after*. df1a403d fixed
   this gap only in tests/integration/test_windows_packaged_smoke.py's own
   test harness (_stop_daemon_service/_kill_stray_app_processes, called by
   the test around its own upgrade-install step); this is the same fix
   ported into the installer itself, which is what a real upgrade -- not
   just the test's own -- needed all along. See that module's own
   docstrings for the two failures this closes: v4.1.0a9's real release
   build hit "Some applications could not be shut down" (exit 5) because
   RestartManager did not win the race against a still-running
   privacyfence-app/PrivacyFenceCompanion; a separated install's daemon is
   the harder case, because it runs as a Windows service with crash-restart
   failure actions configured (Install-DaemonService's own `sc failure ...`
   call), so killing it by image name only buys about five seconds before
   the SCM relaunches it -- a clean `sc.exe stop` is required, not a
   taskkill, for the same reason _stop_daemon_service() spells out.

   Best-effort throughout, and always returns '' (success): a fresh install
   has no prior service or processes to stop at all, which is the ordinary
   case, not a failure one, and this hook's only job is making sure nothing
   already has a file open before Setup starts overwriting it -- Setup's own
   file-in-use retry/RestartManager machinery is still the last line of
   defense, this just gives it far less to do. *)
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OutFile, CmdLine: String;
  ResultCode, Attempt: Integer;
  StillRunning: Boolean;
begin
  Result := '';
  try
    Log('PrepareToInstall: stopping the {#ServiceName} service (if any) before copying files');
    OutFile := ExpandConstant('{tmp}\prepare-stop-service.out');
    (* Same doubled-outer-quotes /C rule as RegisterAutostartTask/
       SeparateInstall's own cmd.exe invocations above -- see either one's
       comment for why. *)
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

    (* Poll `sc.exe query` for up to 30s (60 attempts, 500ms apart) -- the
       same 30s local-mode-fixes-plan.md §2.1 names. A nonzero exit from
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

    (* The companion, and the daemon's own non-service alias -- both best
       effort, both a no-op against a process that is not running (taskkill
       exits non-zero, which this ignores, same as
       _kill_stray_app_processes() does in the test module cited above).
       All three image names, ported from that same helper's own sweep, so
       a real upgrade gets the protection the test was previously only
       asserting for itself. *)
    Log('PrepareToInstall: sweeping stray PrivacyFence processes');
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#CompanionExeName}"', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#AppExeName}"', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#AliasExeName}"', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
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
  AutostartRegistered: Boolean;
  SeparationOutput: String;
begin
  if CurStep = ssPostInstall then
  begin
    (* A failed registration deliberately does not abort the install: every
       other part of the install is still usable without this task, and what
       it would cost is narrower than it used to be -- see the dialog below.
       It is logged as a failure rather than passed over silently so that the
       install log actually says so, which is also what Phase 7's
       graphical-session test reads back when it finds the task missing. *)
    AutostartRegistered := RegisterAutostartTask();
    if not AutostartRegistered then
      Log('RegisterAutostartTask: FAILED; the sign-in task was not registered.');

    (* Deliberately after RegisterAutostartTask, not before: `enable`'s last
       act is to *disable* that task (it would start a second daemon, in the
       user's own session, against a data directory only the service account
       can read -- see Disable-DaemonTask in the .ps1). Registering it
       afterwards would hand the separated install exactly the thing the
       separation just took away, and `... status` would report
       STILL AUTOSTARTS on a fresh install. `disable` puts it back.

       Unlike the autostart step above, a failure here aborts the install.
       That is ADR 0003 decision 1 in one line: a PrivacyFence that cannot
       separate itself still renders the same approval dialogs, accepts the
       same passkey enrollment and writes the same audit log, none of which
       mean what they say when the daemon and the AI client it governs share
       an account. Shipping that silently is the outcome this refuses.

       RaiseException here stops the rest of ssPostInstall (nothing past this
       point runs: no finish-page autostart note, nothing) and, interactively,
       shows the message below instead of a success screen. It does *not*
       make Setup's own process exit code non-zero -- Pascal Scripting's
       Abort()/RaiseException only affects Setup's exit code when raised from
       InitializeSetup, InitializeWizard or CurStepChanged(ssInstall); by
       ssPostInstall the "actual installation process" Setup's own exit-code
       table (codes 3/4/7/8) means has already finished, and nothing run
       afterward can retroactively fail it. (Confirmed against Inno Setup's
       own Pascal Scripting reference for Abort, not assumed -- see
       privacyfence/privacyfence's build-failure notes for 4.1.0b1.) A caller
       that needs to detect this refusal programmatically -- as
       tests/integration/test_windows_packaged_smoke.py's own
       test_windows_install_fails_when_the_image_is_user_writable does --
       has to read the install log for SeparateInstall's own failure line (or
       check that the marker/service were never created), not the exit code. *)
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

    (* Reported only now, once separation has succeeded, because what a
       missing sign-in task actually costs depends on that -- and because an
       install that is about to abort should not first stop to discuss its
       autostart arrangements.

       The dialog exists at all because Log() alone was not enough:
       privacyfence/privacyfence#410 was a real install reporting overall
       success while autostart silently never got wired up, discovered only
       after the next reboot left the daemon not running with no clue why.
       What it says has changed with decision 4, because the daemon is a
       service now and the service is what starts it -- this task is the
       *unseparated* install's autostart, which is to say the one `disable`
       hands back. Saying "PrivacyFence will not start at sign-in" here
       would be alarming and wrong.

       WizardSilent guards it so an unattended/scripted install (this repo's
       own /VERYSILENT integration tests included) never blocks on a message
       box nobody is there to dismiss.

       Every continuation line below starts with a quoted string or
       ExpandConstant, never a bare #13#10 -- Inno's preprocessor (ISPP)
       treats a line whose first non-blank character is '#' as a directive
       line, and "unknown preprocessor directive" is a compile-time error,
       not a Pascal one, so this bit it once already
       (privacyfence/privacyfence#411's own CI). Each #13#10 pair stays glued
       to the end of the previous line instead. *)
    if (not AutostartRegistered) and (not WizardSilent()) then
      MsgBox(
        'PrivacyFence could not register its Windows sign-in task.' + #13#10 + #13#10 +
        'This does not stop PrivacyFence from running: the daemon runs as a ' +
        'Windows service and starts with the machine. The task only matters ' +
        'if you later turn privilege separation off, which moves the daemon ' +
        'back into your own session and relies on it.' + #13#10 + #13#10 +
        'Re-running this installer may resolve it -- if it keeps ' +
        'happening, please report it to the PrivacyFence project.',
        mbInformation, MB_OK);
  end;
end;
