# Build PrivacyFence-<version>-setup.exe -- the Windows installer.
#
# PowerShell, not bash, since this step only ever runs on a
# Windows build host -- mirrors build_dmg.sh being bash because it only
# ever runs on macOS.
#
# Like the DMG, the installer is the single distributable: it carries both
# halves of PrivacyFence (the daemon .exe and PrivacyFence.mcpb) so the user
# flow is "run the installer, then double-click PrivacyFence.mcpb in Claude
# Desktop" -- no separate downloads.
#
# Prerequisites (needed only on your build machine, not end-user machines):
#   pip install -e ".[dev]"     # PrivacyFence itself + pyinstaller + Pillow
#                                # (icon conversion, step 1), so VERSION
#                                # below can read its installed metadata
#                                # (git-tag-derived, see this repo's
#                                # CLAUDE.md "Releasing" section)
#   Inno Setup 6                # https://jrsoftware.org/isinfo.php -- iscc.exe
#                                # must be on PATH (winget install
#                                # JRSoftware.InnoSetup, or add its install
#                                # dir to PATH by hand)
#   Git for Windows              # supplies the bash scripts/build_mcpb.sh
#                                # needs; its bash.exe must be on PATH (the
#                                # default Git for Windows install already
#                                # does this)
#   node + npm on PATH           # for scripts/build_mcpb.sh
#   eSigner CodeSignTool         # only if $env:CODESIGNTOOL_DIR is set -- SSL.com's IV
#                                # code-signing cert's private key lives only in eSigner's cloud
#                                # HSM (CA/B Forum's 2023 key-storage rules dropped exportable
#                                # code-signing .pfx files entirely), so signing goes through
#                                # SSL.com's CodeSignTool CLI (https://github.com/SSLcom/
#                                # CodeSignTool/releases -- the Windows *-windows.zip asset --
#                                # unzip and point $env:CODESIGNTOOL_DIR at the extracted
#                                # directory) rather than signtool.exe against a local cert
#                                # store. CodeSignTool.bat bundles its own JDK, so no separate
#                                # Java install is needed.
#
# Usage:
#   pwsh ./scripts/build_installer.ps1
#
# Optional signing env vars (unset means an unsigned build):
#   CODESIGNTOOL_DIR   Path to an extracted CodeSignTool release (contains CodeSignTool.bat)
#   ES_USERNAME        SSL.com account username
#   ES_PASSWORD        SSL.com account password
#   ES_CREDENTIAL_ID   eSigner credential ID for the enrolled code-signing cert
#   ES_TOTP_SECRET     eSigner TOTP secret (base32), for headless OTP generation
#
# Output: dist/PrivacyFence-<version>-setup.exe
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Version comes from the git tag via setuptools_scm now (this repo's
# CLAUDE.md "Releasing" section) -- read back through the installed
# package's own metadata, same as PrivacyFenceApp.win.spec and
# src/privacyfence/__init__.py. Fails clearly if PrivacyFence itself hasn't
# been `pip install -e .`d yet, same as build_dmg.sh's identical check.
$Version = python -c "from importlib.metadata import version; print(version('privacyfence'))"
if ($LASTEXITCODE -ne 0) { throw "Could not resolve installed privacyfence version" }
$ProductName = "PrivacyFence"
$AppName = "PrivacyFenceApp"
$SetupName = "${ProductName}-${Version}-setup.exe"

Write-Host "=== Building ${ProductName} ${Version} (Windows) ==="

# -- 1. Convert PNG icon to a multi-resolution .ico --------------------------
# Neither sips/iconutil (macOS-only, used by build_dmg.sh) nor a Windows-
# native equivalent is available cross-platform in CI, so this uses Pillow
# instead (added to the `dev` extra in pyproject.toml, not a runtime
# dependency -- mirrors how pyinstaller itself is dev-extra-only).
$IconSrc = "src/privacyfence/resources/icon_512.png"
$IcoPath = "build/privacyfence.ico"
if (-not (Test-Path $IcoPath)) {
    Write-Host "-> Converting icon to .ico..."
    New-Item -ItemType Directory -Force -Path "build" | Out-Null
    python -c @"
from PIL import Image
img = Image.open('$IconSrc')
img.save('$IcoPath', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
"@
    if ($LASTEXITCODE -ne 0) { throw "Icon conversion failed" }
}

# -- 2. Bake in the Telegram app credentials ---------------------------------
# Same reasoning and same git-ignored destination module as build_dmg.sh's
# identically-numbered step: api_id/api_hash identify the PrivacyFence app to
# Telegram (not an organization -- see docs/telegram-setup.md), and this repo
# is public, so they're never committed. CI supplies them as
# TELEGRAM_API_ID/TELEGRAM_API_HASH env vars; a local build without them set
# just ships without Telegram support. scripts/telegram_credentials.py is the
# one generator every build shares.
python scripts/telegram_credentials.py write
if ($LASTEXITCODE -ne 0) { throw "Writing Telegram app credentials failed" }

# -- 3. Build the onedir daemon with PyInstaller -----------------------------
# --clean forces a fresh module analysis every time, same reasoning as
# build_dmg.sh's identical flag: PyInstaller otherwise caches "module not
# found" results in build/PrivacyFenceApp.win/, so a rebuild right after
# _telegram_credentials.py first appears (step 2, above) could otherwise
# silently keep using a stale analysis from before the file existed.
Write-Host "-> Running PyInstaller..."
pyinstaller --noconfirm --clean PrivacyFenceApp.win.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$DistDir = "dist/${AppName}"

# -- 4. Create privacyfence-app.exe alongside PrivacyFenceApp.exe -----------
# The Task Scheduler task (installed by the .iss script below) and
# the mcpb shim's own daemon auto-start (daemon.ts's DEFAULT_APP_PATH) both
# look for this name; the main exe is "PrivacyFenceApp.exe". Windows has no
# cheap equivalent NTFS behaves well with for a symlink from an unprivileged
# installer context (unlike the macOS/Linux builds' plain `ln -s`), so this
# ships as a real copy instead -- same effect (both names resolve on disk).
$MainExe = Join-Path $DistDir "PrivacyFenceApp.exe"
$AliasExe = Join-Path $DistDir "privacyfence-app.exe"
Write-Host "-> Creating privacyfence-app.exe copy..."
Copy-Item -Force $MainExe $AliasExe

# -- 5. Build the Claude Desktop extension (.mcpb) ---------------------------
# scripts/build_mcpb.sh is plain bash + Node/TypeScript -- no macOS-specific
# step in it -- so it runs unchanged here via Git for Windows' bash.exe
# rather than being ported to PowerShell: Git for Windows is already a
# near-universal Windows dev-machine prerequisite, and this avoids a second,
# drifting copy of that script's logic.
Write-Host "-> Building PrivacyFence's Claude Desktop extension..."
bash scripts/build_mcpb.sh
if ($LASTEXITCODE -ne 0) { throw "build_mcpb.sh failed" }
$McpbPath = "dist/${ProductName}-${Version}.mcpb"

# -- 6. Optional code-signing of the daemon + companion executables ---------
# Sign PrivacyFenceApp.exe, privacyfence-app.exe, and (ADR 0002)
# PrivacyFenceCompanion.exe -- signing only the installer and not these would
# still show an unrecognized-publisher warning if a user runs any of them
# directly rather than through the installer.
#
# Goes through eSigner CodeSignTool rather than signtool.exe against a local
# cert store: the cert's private key lives only in eSigner's cloud HSM, so
# there's no local .pfx/thumbprint for signtool to sign against. CodeSignTool
# authenticates per call with the eSigner credential below; -override with no
# -output_dir_path (per `CodeSignTool sign -h`) signs $Path in place.
function Invoke-Signing([string]$Path) {
    if (-not $env:CODESIGNTOOL_DIR) { return }
    Write-Host "-> Signing $Path..."
    # CodeSignTool.bat resolves its own bundled JDK/jar via %CODE_SIGN_TOOL_PATH% (falling back to
    # its own working directory otherwise) -- set it explicitly so this works regardless of $PWD.
    $env:CODE_SIGN_TOOL_PATH = $env:CODESIGNTOOL_DIR
    $CodeSignTool = Join-Path $env:CODESIGNTOOL_DIR "CodeSignTool.bat"
    & $CodeSignTool sign `
        "-credential_id=$env:ES_CREDENTIAL_ID" `
        "-username=$env:ES_USERNAME" `
        "-password=$env:ES_PASSWORD" `
        "-totp_secret=$env:ES_TOTP_SECRET" `
        "-input_file_path=$Path" `
        -override
    if ($LASTEXITCODE -ne 0) { throw "CodeSignTool failed on $Path" }
}
$CompanionExe = Join-Path $DistDir "PrivacyFenceCompanion.exe"
Invoke-Signing $MainExe
Invoke-Signing $AliasExe
Invoke-Signing $CompanionExe

# -- 7. Build the installer with Inno Setup ----------------------------------
Write-Host "-> Running Inno Setup..."
New-Item -ItemType Directory -Force -Path "dist" | Out-Null
iscc.exe `
    "/DAppVersion=$Version" `
    "/DDistDir=$((Resolve-Path $DistDir).Path)" `
    "/DMcpbPath=$((Resolve-Path $McpbPath).Path)" `
    "/DIconPath=$((Resolve-Path $IcoPath).Path)" `
    "/DOutputDir=$((Resolve-Path 'dist').Path)" `
    "/DSetupBaseName=${ProductName}-${Version}-setup" `
    "installer/privacyfence.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup (iscc.exe) failed" }

$SetupPath = "dist/${SetupName}"

# -- 8. Optional code-signing of the installer itself ------------------------
Invoke-Signing $SetupPath

Write-Host ""
Write-Host "Done: $SetupPath"
Write-Host "  Size: $((Get-Item $SetupPath).Length / 1MB) MB"
