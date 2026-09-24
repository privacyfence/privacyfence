#!/usr/bin/env bash
# #428 Phase 4 (B5a): opt into -- or back out of -- running the PrivacyFence
# daemon under its own macOS account.
#
# Until this runs, the daemon and the AI agent it exists to govern are the same
# OS user, which is the root cause of all four weaknesses issue #428 describes:
# the agent can read the session-minting credential, rewrite the always-allow
# rules and PII policy that decide what it is allowed to do, forge a WebAuthn
# credential into the store a local passkey would be checked against, and read
# the audit log's HMAC key. One change closes all four -- a dedicated account
# owning those files -- and this script is that change, made reversible.
#
#   sudo ./scripts/macos_privilege_separation.sh enable
#   sudo ./scripts/macos_privilege_separation.sh status
#   sudo ./scripts/macos_privilege_separation.sh disable
#
# What `enable` does, in order:
#   1. creates the _privacyfence system user and group;
#   2. adds you to that group, so the companion app can still reach the daemon;
#   3. moves ~/.privacyfence to /Library/Application Support/PrivacyFence and
#      re-owns it -- authority/ at 0700, handoff/ at 3770, the root at 0711;
#   4. copies the daemon/companion image (from --app, normally /Applications/
#      PrivacyFenceApp.app) into a fresh root:wheel-owned copy under
#      /Library/PrivacyFence/image -- see stage_trusted_image()'s own comment
#      for why: /Applications itself is admin-group-writable on every real
#      Mac, so nothing installed directly under it can ever be trusted (B1)
#      without this;
#   5. writes the marker file every PrivacyFence process reads to agree on that
#      layout (src/privacyfence/privilege_separation.py);
#   6. replaces the login-session LaunchAgent with a LaunchDaemon for the
#      daemon and a LaunchAgent for the companion app -- ADR 0002's "startup
#      wiring inverts" -- both pointed at step 4's staged copy, not the
#      original image.
#
# Steps 2 and 3 are the only two that need to know *which human* this install
# is for, and ADR 0003 decision 3 splits them out for that reason: an MDM
# push, a .pkg installed with nobody at the console, or a plain root shell
# resolves no owner account, and that used to leave the whole install
# unseparated. It no longer does. `enable` with no resolvable owner runs
# everything root can do alone and records the group membership as pending;
# `enable --for-user <name>` closes that half later, idempotently, and is
# what the companion app runs by itself at the first real login session.
#
# Step 3 moves live connector OAuth tokens. `disable` moves them back, but this
# is still the step to take a backup before: it is the one part of this that
# touches data you cannot re-mint from a config file.
#
# Ships opt-in by hand via the three subcommands above. #428 D1 (4.1, moved up
# from the original 4.2 plan) additionally auto-runs `enable --auto` once from
# the daemon's own startup path when it finds itself unseparated -- see
# privilege_separation.py's maybe_auto_enable_macos(). `--auto` is the same
# `enable`, made safe to run unattended and non-interactively: anywhere it
# would otherwise die() on something a human would resolve by hand (no
# installed executables), it instead logs why and exits 0 rather than leaving
# a half-finished separation behind. A human running this by hand never wants
# that silent behavior, which is why --auto isn't the default -- and it still
# needs the admin password, same as always: nothing about --auto skips
# require_root.
set -euo pipefail

# ── Constants. Every one of these is also declared in
#    src/privacyfence/privilege_separation.py, and
#    tests/unit/test_privilege_separation.py asserts the two agree -- this
#    script and that module are the two halves of one contract, and a silent
#    drift between them would leave a daemon looking for its data somewhere
#    the installer never put it. ──────────────────────────────────────────────
SERVICE_ACCOUNT="_privacyfence"
SERVICE_GROUP="_privacyfence"
SYSTEM_ROOT="/Library/Application Support/PrivacyFence"
MARKER_NAME="privilege-separation.json"
MARKER_VERSION=1
HANDOFF_DIR_NAME="handoff"
SYSTEM_ROOT_MODE=711
# #428 Phase 2's interim multi-user guard (§2.6, "companion socket
# takeover"): the leading 3 is the sticky bit (01000) on top of the setgid
# bit (02000) this already carried. Setgid alone means every member of
# SERVICE_GROUP can create and delete files here, which is exactly right for
# the daemon and the companion producing group-owned files for each other --
# but it also means any *other* account this install has since been
# extended to (ADR 0003 decision 3's per-user half, run more than once) can
# unlink a peer's companion.sock and rebind it as their own, even though
# they never owned it. The sticky bit is the same fix /tmp has carried since
# 4.3BSD: only a file's own owner (or root) may remove or rename an entry
# here, group write notwithstanding. web/control_channel.py's
# _existing_socket_owner_problem() is the code-level half of this same fix;
# this is the filesystem-level half, and the two are meant to be read
# together, not either alone.
HANDOFF_DIR_MODE=3770
AUTHORITY_DIR_MODE=700
HANDOFF_FILE_MODE=640

DAEMON_LABEL="com.privacyfence.daemon"
COMPANION_LABEL="com.privacyfence.companion"
DAEMON_PLIST="/Library/LaunchDaemons/${DAEMON_LABEL}.plist"
COMPANION_PLIST="/Library/LaunchAgents/${COMPANION_LABEL}.plist"
# The per-user LaunchAgent this replaces (com.privacyfence.app.plist in the
# repo root). Left installed, it would start a *second* daemon as the logged-in
# user -- which, on a separated install, now refuses to start rather than
# quietly seeding a default policy (privilege_separation.check_runtime_identity).
LEGACY_AGENT_LABEL="com.privacyfence.app"

DEFAULT_APP="/Applications/PrivacyFenceApp.app"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_DIR="${REPO_ROOT}/installer/macos"

# Filled in by resolve_owner()/resolve_executables() below.
OWNER_USER=""
OWNER_UID=""
OWNER_HOME=""
DAEMON_EXECUTABLE=""
COMPANION_EXECUTABLE=""
APP_PATH=""

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '→ %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

usage() {
  cat >&2 <<USAGE
usage: sudo $0 {enable|disable|status} [options]
       sudo $0 daemon {status|start|stop|restart|ensure-running}

  --user <name>       the human account that owns this install
                      (default: \$SUDO_USER, i.e. whoever ran sudo). With none
                      resolvable, enable still separates the machine and
                      leaves the group membership pending -- see --for-user.
  --for-user <name>   enable only: run *just* the per-user half against an
                      install the machine half has already separated -- add
                      <name> to ${SERVICE_GROUP} and migrate their
                      ~/.privacyfence. Idempotent, and what the companion app
                      runs when it finds that membership still pending. Works
                      for any number of accounts on the same machine, not
                      just this install's first (recorded) owner -- each gets
                      its own isolated PrivacyFence identity, never merged
                      with anyone else's (docs/adr/0008-one-principal-per-os-
                      user.md).
  --app <path>        PrivacyFenceApp.app to run from
                      (default: ${DEFAULT_APP})
  --daemon-exec <p>   run this instead of the .app's daemon executable
  --companion-exec <p> run this instead of the .app's companion executable
                      (both together let a source/venv install be separated:
                       --daemon-exec .venv/bin/privacyfence-app
                       --companion-exec .venv/bin/privacyfence-companion)
  --auto              enable only: never die(), never prompt -- log and exit 0
                      instead of failing when this can't safely tell who owns
                      the install or find the app bundle. For the daemon's own
                      unattended auto-enable trigger; a human should not pass
                      this.

  daemon status           print a small unprivileged key=value report of the
                          LaunchDaemon's own state -- loaded, pid, owner, last
                          exit status, and whether the control socket exists.
                          Needs no sudo.
  daemon start            alias for ensure-running.
  daemon ensure-running   make sure ${DAEMON_LABEL} is loaded, has a pid owned
                          by ${SERVICE_ACCOUNT}, and has produced its control
                          socket -- bootstrapping or kickstarting it as
                          needed, with retry/backoff around launchd's own
                          bootout/bootstrap race. What 'enable' itself calls
                          at the end, and what the companion app's Start
                          button runs elevated.
  daemon restart          kickstart -k if loaded, otherwise ensure-running.
  daemon stop             bootout, and wait for the job to actually unload.
USAGE
  exit 2
}

AUTO=0
FOR_USER_ONLY=0
# Set by cmd_enable_for_user() when it is running for an account other than
# the marker's recorded owner -- i.e. this install already has one principal
# and this run is adding a second (or third, ...) one. migrate_data() reads
# this to route that account's own ~/.privacyfence into its own per-principal
# subdirectory (users/os-<uid>/) instead of the shared root, per ADR 0008
# ("D2: two identities, not one, per install") -- each OS account PrivacyFence
# ever runs `enable --for-user` for gets fully isolated storage, never merged
# with another account's. #428 Phase 2 §2.6's interim guard used to refuse
# this outright rather than isolate it; ADR 0008 is what let the refusal be
# replaced with a real per-user destination instead of just being deleted.
NON_OWNER_FOR_USER=0
# The sub-verb of `daemon {status|start|stop|restart|ensure-running}`,
# pulled off the argument list before the generic option-parsing loop below
# ever sees it -- see the "── Argument parsing ──" section at the bottom for
# why that has to happen first.
DAEMON_SUBCOMMAND=""

require_macos() {
  [ "$(uname -s)" = "Darwin" ] || die "this script is macOS-only (Linux is scripts/linux_privilege_separation.sh; Windows is scripts/windows_privilege_separation.ps1)"
}

require_root() {
  [ "$(id -u)" = "0" ] || die "run this with sudo -- creating a system account and a LaunchDaemon both need root"
}

resolve_owner() {
  if [ -z "$OWNER_USER" ]; then
    OWNER_USER="${SUDO_USER:-}"
  fi
  [ -n "$OWNER_USER" ] || die "could not tell which human account owns this install -- pass --user <name>"
  [ "$OWNER_USER" != "root" ] || die "--user must be a real login account, not root"
  OWNER_UID="$(id -u "$OWNER_USER" 2>/dev/null)" || die "no such user: ${OWNER_USER}"
  OWNER_HOME="$(dscl . -read "/Users/${OWNER_USER}" NFSHomeDirectory 2>/dev/null | sed 's/^NFSHomeDirectory: //')"
  [ -n "$OWNER_HOME" ] || die "could not resolve ${OWNER_USER}'s home directory"
}

# Mirrors ``privilege_separation._TRUSTED_POSIX_IMAGE_GROUP``: root's own
# group, and the only one besides root itself this trusts to replace what
# the daemon runs (B1). Deliberately not "admin" -- the group
# /Applications is actually group-owned by, and whose members are exactly
# the accounts this check exists to stop trusting; see that constant's own
# comment for the full reasoning. Not cross-checked by TestInstallerContract
# the way SERVICE_ACCOUNT/SERVICE_GROUP/SYSTEM_ROOT are, because this has no
# Linux counterpart to compare against -- /opt is already dpkg-owned.
TRUSTED_IMAGE_GROUP="wheel"

# B1 follow-up: where stage_trusted_image() (below) copies the daemon/
# companion image to before ever trusting it. Deliberately NOT under
# SYSTEM_ROOT -- apply_layout() chowns that whole tree, itself included, to
# ${SERVICE_ACCOUNT}, which require_trusted_image()'s own "owned by anyone
# but root" check would then refuse the moment this lived underneath it. A
# sibling of SYSTEM_ROOT's own parent instead: /Library itself is root:wheel
# on stock macOS (unlike /Applications), the same anchor test_macos_
# graphical_session_autostart.py's own _ROOT_STAGING_PARENT already trusts
# for exactly this reason.
TRUSTED_IMAGE_DIR="/Library/PrivacyFence/image"

# B1: nothing previously verified the daemon/companion image was not
# user-writable before this elevates to it. ADR 0002 §5a used to claim
# /Applications was root-owned the same way /opt is; it is actually
# root:admin drwxrwxr-x, and a drag-installed .app is normally owned by the
# installing user -- the same account the agent runs as. Walks $1 and every
# directory on the way to it (a root-owned, unwritable executable still
# isn't safe if the directory holding it can be emptied and refilled by
# someone else) and dies naming every path that fails: owned by anyone but
# root, or writable by world or by a group other than $TRUSTED_IMAGE_GROUP.
# Mirrors ``privilege_separation._posix_image_problems()``, which
# ``audit_layout()`` re-runs on every daemon start in case the install is
# replaced in place after this check has already passed once.
require_trusted_image() {
  local path="$1" owner group mode image_problems=()
  while :; do
    if [ -e "$path" ]; then
      owner="$(stat -f '%Su' "$path")"
      group="$(stat -f '%Sg' "$path")"
      mode="$(stat -f '%OLp' "$path")"
      if [ "$owner" != "root" ]; then
        image_problems+=("${path} is owned by '${owner}', not root")
      elif [ "$(( 8#${mode} & 2 ))" -ne 0 ]; then
        image_problems+=("${path} is world-writable (mode ${mode})")
      elif [ "$(( 8#${mode} & 16 ))" -ne 0 ] && [ "$group" != "$TRUSTED_IMAGE_GROUP" ]; then
        image_problems+=("${path} is group-writable by '${group}' (mode ${mode}) -- only ${TRUSTED_IMAGE_GROUP} is trusted")
      fi
    fi
    [ "$path" = "/" ] && break
    path="$(dirname "$path")"
  done
  if [ "${#image_problems[@]}" -gt 0 ]; then
    local p
    for p in "${image_problems[@]}"; do
      warn "$p"
    done
    die "$1 is not safe to run as ${SERVICE_ACCOUNT} (B1) -- anyone who can rewrite it, or a directory on the path to it, can run code as that account. Fix the ownership/permissions named above, or install PrivacyFenceApp.app somewhere only root can write to."
  fi
}

# B1 follow-up, discovered by #428 D2's own real `installer -pkg ... -target /`
# coverage (test_macos_pkg_install.py): require_trusted_image() walks every
# directory on the way to the image, and /Applications itself is root:admin
# drwxrwxr-x on every real Mac -- group-writable by the same admin account
# the agent runs as. That made the walk refuse *any* app installed at the
# standard /Applications/PrivacyFenceApp.app location, no matter how the
# bundle itself was owned -- not just for a .pkg install, but for the DMG's
# own daemon-triggered runtime prompt (privilege_separation.
# maybe_auto_enable_macos()) and for a human running `enable` by hand
# against a real drag-installed copy, since all three funnel through this
# same require_trusted_image() call.
#
# Rather than trust wherever --app/--daemon-exec/--companion-exec point,
# `enable` now copies that image -- right now, as root, while it already has
# the administrator authentication this whole command required to run at
# all -- into TRUSTED_IMAGE_DIR, a location this script itself provisions as
# root:wheel and nothing else ever writes to. Everything launchd runs from
# here on is what THIS copy contains: the LaunchDaemon/LaunchAgent plists
# point at it (install_services() renders them from DAEMON_EXECUTABLE/
# COMPANION_EXECUTABLE, which this reassigns), and the running daemon's own
# audit_layout() re-check (privilege_separation.daemon_image_paths(), keyed
# off sys.executable) sees the same trusted path on every subsequent start.
#
# One real consequence: once separated, replacing /Applications/
# PrivacyFenceApp.app in place (a fresh DMG drag) no longer takes effect on
# its own -- the separated daemon keeps running the staged copy until `enable`
# is run again. That is not a bug this introduces so much as the one honest
# way to close the hole above: auto-refreshing the staged copy from an
# already-running, already-elevated process would mean trusting
# /Applications again, silently, which is exactly what this exists to stop
# doing. Re-authenticating (by hand, or a future re-prompt) is the correct
# cost of an upgrade on a separated macOS install.
#
# Called from cmd_enable() only, and only after apply_layout() -- apply_layout()
# chowns SYSTEM_ROOT recursively to ${SERVICE_ACCOUNT}, which would undo this
# function's own root:wheel ownership if staging ran first, and TRUSTED_IMAGE_DIR
# is deliberately outside SYSTEM_ROOT for exactly that reason regardless.
stage_trusted_image() {
  note "staging a root-owned copy of the app image under ${TRUSTED_IMAGE_DIR} (B1)"
  rm -rf "$TRUSTED_IMAGE_DIR"
  mkdir -p "$TRUSTED_IMAGE_DIR"
  if [ -n "$APP_PATH" ]; then
    local staged_app="${TRUSTED_IMAGE_DIR}/$(basename "$APP_PATH")"
    ditto "$APP_PATH" "$staged_app"
    DAEMON_EXECUTABLE="${staged_app}/Contents/MacOS/PrivacyFenceApp"
    COMPANION_EXECUTABLE="${staged_app}/Contents/MacOS/PrivacyFenceCompanion"
  else
    # --daemon-exec/--companion-exec given directly (a source/venv install,
    # no single .app bundle to copy as a unit) -- stage each file on its own.
    local staged_daemon="${TRUSTED_IMAGE_DIR}/$(basename "$DAEMON_EXECUTABLE")"
    local staged_companion="${TRUSTED_IMAGE_DIR}/$(basename "$COMPANION_EXECUTABLE")"
    cp -p "$DAEMON_EXECUTABLE" "$staged_daemon"
    cp -p "$COMPANION_EXECUTABLE" "$staged_companion"
    DAEMON_EXECUTABLE="$staged_daemon"
    COMPANION_EXECUTABLE="$staged_companion"
  fi
  # The parent (/Library/PrivacyFence), not just $TRUSTED_IMAGE_DIR itself --
  # require_trusted_image() walks every ancestor, and while `mkdir -p` as
  # root ordinarily leaves a freshly-created parent root-owned too, this
  # makes it explicit rather than relying on root's own default umask/group.
  chown -R root:wheel "$(dirname "$TRUSTED_IMAGE_DIR")"
  chmod -R go-w "$(dirname "$TRUSTED_IMAGE_DIR")"
  require_trusted_image "$DAEMON_EXECUTABLE"
  require_trusted_image "$COMPANION_EXECUTABLE"
}

resolve_executables() {
  if [ -z "$DAEMON_EXECUTABLE" ] || [ -z "$COMPANION_EXECUTABLE" ]; then
    APP_PATH="${APP_PATH:-$DEFAULT_APP}"
    [ -d "$APP_PATH" ] || die "no app bundle at ${APP_PATH} -- pass --app, or --daemon-exec/--companion-exec for a source install"
    : "${DAEMON_EXECUTABLE:=${APP_PATH}/Contents/MacOS/PrivacyFenceApp}"
    : "${COMPANION_EXECUTABLE:=${APP_PATH}/Contents/MacOS/PrivacyFenceCompanion}"
  fi
  [ -x "$DAEMON_EXECUTABLE" ] || die "not executable: ${DAEMON_EXECUTABLE}"
  # The companion is what a human uses once the daemon has no login session of
  # its own, so a separated install without one is a locked door. Refuse rather
  # than install half of ADR 0002's inversion.
  [ -x "$COMPANION_EXECUTABLE" ] || die "not executable: ${COMPANION_EXECUTABLE} -- a separated install needs the companion app (ADR 0002 decision 2)"
  # Trust is established after staging, not here -- see stage_trusted_image()'s
  # own comment for why checking require_trusted_image() against wherever
  # these point (typically /Applications) would refuse every real install.
}

# ── Account provisioning ──────────────────────────────────────────────────────

next_free_system_id() {
  # System accounts live below 500 on macOS; Apple's own start at 200-ish and
  # third-party ones conventionally take the 300-400 range. Pick the first id
  # free in *both* the user and group namespaces, so the service account and
  # its group can share a number the way every other system account does.
  local used_uids used_gids id
  used_uids="$(dscl . -list /Users UniqueID | awk '{print $2}')"
  used_gids="$(dscl . -list /Groups PrimaryGroupID | awk '{print $2}')"
  for id in $(seq 400 499); do
    if ! grep -qx "$id" <<<"$used_uids" && ! grep -qx "$id" <<<"$used_gids"; then
      printf '%s' "$id"
      return 0
    fi
  done
  die "no free system uid/gid in 400-499 -- create ${SERVICE_ACCOUNT} by hand and re-run"
}

service_account_exists() { dscl . -read "/Users/${SERVICE_ACCOUNT}" >/dev/null 2>&1; }
service_group_exists() { dscl . -read "/Groups/${SERVICE_GROUP}" >/dev/null 2>&1; }

create_service_account() {
  if service_account_exists && service_group_exists; then
    note "service account ${SERVICE_ACCOUNT} already exists -- leaving it as it is"
    return
  fi
  local id
  id="$(next_free_system_id)"
  note "creating the ${SERVICE_ACCOUNT} account and group (id ${id})"
  if ! service_group_exists; then
    dscl . -create "/Groups/${SERVICE_GROUP}"
    dscl . -create "/Groups/${SERVICE_GROUP}" PrimaryGroupID "$id"
    dscl . -create "/Groups/${SERVICE_GROUP}" RealName "PrivacyFence daemon"
    dscl . -create "/Groups/${SERVICE_GROUP}" Password "*"
  fi
  if ! service_account_exists; then
    dscl . -create "/Users/${SERVICE_ACCOUNT}"
    dscl . -create "/Users/${SERVICE_ACCOUNT}" RealName "PrivacyFence daemon"
    dscl . -create "/Users/${SERVICE_ACCOUNT}" UniqueID "$id"
    dscl . -create "/Users/${SERVICE_ACCOUNT}" PrimaryGroupID "$id"
    # No home and no shell: this account exists to own files and run one
    # LaunchDaemon. Nothing should ever log into it, and nothing needs a
    # ~/ for it -- paths.data_dir() resolves the system root instead.
    dscl . -create "/Users/${SERVICE_ACCOUNT}" NFSHomeDirectory /var/empty
    dscl . -create "/Users/${SERVICE_ACCOUNT}" UserShell /usr/bin/false
    dscl . -create "/Users/${SERVICE_ACCOUNT}" Password "*"
    dscl . -create "/Users/${SERVICE_ACCOUNT}" IsHidden 1
  fi
}

# How long to wait for a freshly created account to become resolvable, and
# for the bootstrapped daemon to report a pid. Both are "a lookup that should
# be instant, occasionally is not" waits, not real work -- a second of poll is
# already generous, and the timeout is what turns a hang into a message.
ACCOUNT_RESOLVE_TIMEOUT=30
DAEMON_PID_TIMEOUT=15

wait_for_service_account() {
  # dscl writes to the local node and the lookups that read it back --
  # getpwnam(3), and whatever launchd does to turn a plist's UserName into a
  # uid -- go through opendirectoryd's cache, which does not always have the
  # new record the instant `dscl . -create` returns. Everything after this
  # point depends on that lookup: apply_layout()'s `chown -R`, and, far more
  # quietly, launchd's own resolution of UserName in the LaunchDaemon plist --
  # launchd runs a job as *root* when the account it names does not resolve at
  # bootstrap time, which is privacyfence/privacyfence#598's Failure A: an
  # install that reports itself separated while its daemon holds every
  # privilege separation exists to drop.
  #
  # So flush the cache and then actually wait for the account to answer,
  # rather than assuming the write is immediately visible.
  dscacheutil -flushcache 2>/dev/null || true
  local deadline=$((SECONDS + ACCOUNT_RESOLVE_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    # id(1) and dscacheutil both answer through the same cache launchd and
    # chown(1) read, which is the point -- `dscl . -read` would go to the
    # node directly and report success while the cache still says no such
    # user.
    if id -u "$SERVICE_ACCOUNT" >/dev/null 2>&1 \
       && dscacheutil -q group -a name "$SERVICE_GROUP" | grep -q '^gid:'; then
      return 0
    fi
    sleep 0.2
  done
  die "${SERVICE_ACCOUNT} was created but still does not resolve through id(1) after ${ACCOUNT_RESOLVE_TIMEOUT}s -- refusing to continue, because launchd silently runs a LaunchDaemon as root when its UserName does not resolve. Check 'dscl . -read /Users/${SERVICE_ACCOUNT}' and re-run."
}

add_owner_to_service_group() {
  note "adding ${OWNER_USER} to the ${SERVICE_GROUP} group"
  dseditgroup -o edit -a "$OWNER_USER" -t user "$SERVICE_GROUP"
}

# What `status` reads back to tell the pending state apart from "not
# separated" (ADR 0003 decision 3).
marker_owner_user() {
  # Every writer of this file -- this script, Linux's own, the .pkg's
  # postinstall, and PowerShell's ConvertTo-Json -- emits one key per line,
  # so a line-oriented read is enough and keeps `status` free of a JSON
  # parser it would otherwise have to carry for one string.
  sed -n 's/.*"owner_user"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${SYSTEM_ROOT}/${MARKER_NAME}" 2>/dev/null | head -n 1
}

# ── Data migration ────────────────────────────────────────────────────────────

legacy_data_dir() { printf '%s/.privacyfence' "$OWNER_HOME"; }

drop_stale_sockets() {
  # A socket is a live process's rendezvous point, not data: nothing can
  # listen on one copied to a new path, and ditto refuses to copy one at all
  # ("Operation not supported on socket"), which under `set -e` aborted
  # `enable` half-way -- before apply_layout() and install_services().
  find "$1" -type s -exec rm -f {} +
}

migrate_data() {
  local legacy target
  # ADR 0008 ("D2: two identities, not one, per install"): an account that is
  # not this install's recorded owner still gets its own ~/.privacyfence
  # migrated -- just never into the shared root the recorded owner's data
  # lives in, which would mix a second person's connector tokens, audit log
  # and policy into the first owner's. Instead it goes to
  # ${SYSTEM_ROOT}/users/os-<uid>, the exact per-principal path
  # src/privacyfence/paths.py's user_dir() resolves to for a
  # Principal(id=f"os-{uid}") that isn't the "local" principal -- so the
  # daemon finds it under the same identity this migrates it as. cmd_enable_
  # for_user() is the only caller that ever sets NON_OWNER_FOR_USER.
  if [ "$NON_OWNER_FOR_USER" = "1" ]; then
    legacy="$(legacy_data_dir)"
    if [ ! -d "$legacy" ]; then
      note "no existing ${legacy} to migrate -- '${OWNER_USER}' starts with no data of their own"
      return
    fi
    drop_stale_sockets "$legacy"
    target="${SYSTEM_ROOT}/users/os-${OWNER_UID}"
    # Same provisioning idiom apply_layout() uses for the top-level
    # directories below, at the mode paths.py's secure_mkdir() itself
    # defaults a per-principal root to (0700) -- not SYSTEM_ROOT_MODE/
    # HANDOFF_DIR_MODE, which exist to let the *shared* root and handoff
    # directory be entered/written by every group member; a single
    # account's own subtree has no such requirement; it is read and written
    # by the daemon alone.
    mkdir -p "$target"
    chown -R "${SERVICE_ACCOUNT}:${SERVICE_GROUP}" "$target"
    chmod 700 "$target"
    # Same by-hand no-clobber merge as the owner's own case below (ditto has
    # no no-clobber flag of its own) -- reused verbatim, just retargeted at
    # this account's own subtree instead of the shared root.
    note "merging ${legacy} into ${target} -- kept separate from this install's other principal(s), not merged into ${SYSTEM_ROOT} itself (no-clobber: anything already in ${target} is left as it is)"
    local entry name
    for entry in "$legacy"/*; do
      [ -e "$entry" ] || continue
      name="$(basename "$entry")"
      if [ -e "${target}/${name}" ]; then
        note "  ${name} already exists in ${target} -- not overwriting it from ${legacy}"
      else
        ditto "$entry" "${target}/${name}"
      fi
    done
    rm -rf "$legacy"
    return
  fi
  # The machine half (ADR 0003 decision 3) runs with no owner resolved, and a
  # machine with no human account has no per-user data directory to move --
  # so this reduces to creating the root the rest of `enable` provisions.
  if [ -z "$OWNER_HOME" ]; then
    note "no owner account resolved -- nothing to migrate, creating ${SYSTEM_ROOT} empty"
    mkdir -p "$SYSTEM_ROOT"
    return
  fi
  legacy="$(legacy_data_dir)"
  if [ ! -d "$legacy" ]; then
    note "no existing ${legacy} to migrate -- starting the separated install empty"
    mkdir -p "$SYSTEM_ROOT"
    return
  fi
  drop_stale_sockets "$legacy"
  if [ -e "$SYSTEM_ROOT" ]; then
    # Something is already there (a previous enable, or a hand-made
    # directory). Merge rather than clobber, then remove the source --
    # leaving a second copy of live OAuth tokens readable by the agent would
    # undo the point of the whole exercise.
    #
    # `ditto` has no documented no-clobber flag -- its man page describes it
    # as a recursive copy that preserves metadata, not one that skips
    # existing destination entries, and testing that assumption at merge
    # time (when what is already in $SYSTEM_ROOT may be live daemon state)
    # is not somewhere to find out it guessed wrong. So the no-clobber
    # contract the plan asks for ("ditto with a pre-check") is implemented
    # by hand instead: walk $legacy's own top-level entries, and `ditto`
    # in only the ones $SYSTEM_ROOT does not already have. Anything
    # $SYSTEM_ROOT already has -- from a previous enable, or from this same
    # guard having already run once -- is left exactly as it is.
    note "merging ${legacy} into the existing ${SYSTEM_ROOT} (no-clobber: anything already in ${SYSTEM_ROOT} is left as it is)"
    local entry name
    for entry in "$legacy"/*; do
      [ -e "$entry" ] || continue
      name="$(basename "$entry")"
      if [ -e "${SYSTEM_ROOT}/${name}" ]; then
        note "  ${name} already exists in ${SYSTEM_ROOT} -- not overwriting it from ${legacy}"
      else
        ditto "$entry" "${SYSTEM_ROOT}/${name}"
      fi
    done
    rm -rf "$legacy"
  else
    # Same volume in every default macOS install, so this is a rename: atomic,
    # and it leaves nothing behind to clean up or to leak.
    note "moving ${legacy} to ${SYSTEM_ROOT}"
    mkdir -p "$(dirname "$SYSTEM_ROOT")"
    mv "$legacy" "$SYSTEM_ROOT"
  fi
}

# The files that have to end up inside handoff/ rather than at the root of
# the data directory once separation is on, because something in the *user's*
# session reads them: the agent's own credential and the URL it reaches the
# daemon at (mcp_token/mcp_url, read by the MCPB shim), and the discovery
# files a human or the companion reads (web_base_url, plus any legacy
# <page>_url below). Mirrors
# paths.handoff_dir()'s callers -- see test_privilege_separation.py, which
# asserts this list matches the file-name constants those call sites use.
HANDOFF_FILE_NAMES=(mcp_token mcp_url web_base_url)
# <page>_url: approvals_url/settings_url/security_url, written by versions
# before the self-approval plan's Phase 2 stopped putting a live sign-in link
# in a group-shared directory. Kept in the glob so an upgrade does not strand
# one outside the handoff directory while it still exists. The daemon no
# longer deletes them (ADR 0041: no upgrade path from earlier layouts).
HANDOFF_FILE_GLOB='*_url'

move_handoff_files_in() {
  local name target="${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}"
  mkdir -p "$target"
  for name in "${HANDOFF_FILE_NAMES[@]}"; do
    if [ -e "${SYSTEM_ROOT}/${name}" ]; then
      mv -f "${SYSTEM_ROOT}/${name}" "${target}/${name}"
    fi
  done
  for name in "${SYSTEM_ROOT}"/${HANDOFF_FILE_GLOB}; do
    if [ -e "$name" ]; then
      mv -f "$name" "${target}/$(basename "$name")"
    fi
  done
  # Both control channels' sockets are recreated on the next start, at new
  # paths (paths.control_socket_dir()), and a stale socket file at the old one
  # is just a dead inode. Phase 2's own bind() unlinks whatever it finds, but
  # not at a path it no longer looks at.
  rm -f "${SYSTEM_ROOT}/companion.sock" "${SYSTEM_ROOT}/authority/control.sock"
  return 0
}

move_handoff_files_out() {
  # The reverse, for disable: with no marker, handoff_dir() *is* data_dir(),
  # so every one of these has to be back at the root or the agent loses its
  # token and the shim loses the daemon.
  local source="${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" entry
  [ -d "$source" ] || return 0
  for entry in "$source"/*; do
    [ -e "$entry" ] || continue
    mv -f "$entry" "${SYSTEM_ROOT}/$(basename "$entry")"
  done
  rmdir "$source" 2>/dev/null || true
  rm -f "${SYSTEM_ROOT}/companion.sock" "${SYSTEM_ROOT}/control.sock"
  return 0
}

apply_layout() {
  note "re-owning ${SYSTEM_ROOT} to ${SERVICE_ACCOUNT}:${SERVICE_GROUP}"
  mkdir -p "${SYSTEM_ROOT}/authority" "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" "${SYSTEM_ROOT}/logs"
  move_handoff_files_in
  # Everything except sockets (ADR 0029). A socket belongs to the process that
  # bound it, at the mode it chose: re-owning a live companion's
  # handoff/companion.sock on a re-run -- every .pkg upgrade is one -- leaves
  # that companion unable to unlink it under handoff/'s sticky bit, and every
  # companion after it refusing to take it over (ADR 0027). `chown -h` and
  # the chmod's `! -type l` keep `find -exec` from following a symlink, which
  # `chown -R`/`chmod -R` never did.
  find "$SYSTEM_ROOT" ! -type s -exec chown -h "${SERVICE_ACCOUNT}:${SERVICE_GROUP}" {} +
  # Tighten everything first, then re-open exactly the two places that have to
  # be reachable from the user's own session. Order matters: the blanket
  # go-rwx below would otherwise undo the handoff directory's group bits.
  find "$SYSTEM_ROOT" ! -type s ! -type l -exec chmod go-rwx {} +
  chmod "$SYSTEM_ROOT_MODE" "$SYSTEM_ROOT"
  chmod "$AUTHORITY_DIR_MODE" "${SYSTEM_ROOT}/authority"
  chmod "$HANDOFF_DIR_MODE" "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}"
  # The discovery files the daemon rewrites on every start would fix
  # themselves, but mcp_token is reused across restarts -- a migrated one
  # would stay 0600 and the agent would never read its own credential again.
  # (privilege_separation.ensure_handoff_file_mode() re-asserts this too; doing
  # it here as well means a correct install doesn't depend on that fix-up.)
  find "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" -type f -exec chmod "$HANDOFF_FILE_MODE" {} +
}

write_marker() {
  local marker="${SYSTEM_ROOT}/${MARKER_NAME}"
  note "writing ${marker}"
  cat > "$marker" <<MARKER
{
  "version": ${MARKER_VERSION},
  "platform": "darwin",
  "service_account": "${SERVICE_ACCOUNT}",
  "service_group": "${SERVICE_GROUP}",
  "owner_user": "${OWNER_USER}",
  "enabled_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
MARKER
  # World-readable on purpose: it holds account and directory names, not
  # secrets, and a process in the user's session that cannot read it cannot
  # tell that separation is on at all.
  chown "${SERVICE_ACCOUNT}:${SERVICE_GROUP}" "$marker"
  chmod 644 "$marker"
}

# ── launchd wiring ────────────────────────────────────────────────────────────

render_template() {
  local template="$1" destination="$2"
  [ -f "$template" ] || die "missing template: ${template}"
  sed \
    -e "s|__DAEMON_LABEL__|${DAEMON_LABEL}|g" \
    -e "s|__COMPANION_LABEL__|${COMPANION_LABEL}|g" \
    -e "s|__DAEMON_EXECUTABLE__|${DAEMON_EXECUTABLE}|g" \
    -e "s|__COMPANION_EXECUTABLE__|${COMPANION_EXECUTABLE}|g" \
    -e "s|__SERVICE_ACCOUNT__|${SERVICE_ACCOUNT}|g" \
    -e "s|__SERVICE_GROUP__|${SERVICE_GROUP}|g" \
    -e "s|__SYSTEM_ROOT__|${SYSTEM_ROOT}|g" \
    "$template" > "$destination"
  chown root:wheel "$destination"
  chmod 644 "$destination"
  plutil -lint "$destination" >/dev/null || die "rendered ${destination} is not a valid plist"
}

stop_legacy_agent() {
  # Nothing to do with no owner resolved (ADR 0003 decision 3's machine half):
  # the per-user LaunchAgent this replaces lives in a home directory, and
  # there is no GUI session to bootout of either.
  [ -n "$OWNER_HOME" ] || return 0
  local legacy_plist="${OWNER_HOME}/Library/LaunchAgents/${LEGACY_AGENT_LABEL}.plist"
  launchctl bootout "gui/${OWNER_UID}/${LEGACY_AGENT_LABEL}" 2>/dev/null || true
  if [ -f "$legacy_plist" ]; then
    note "disabling the old per-user LaunchAgent (${legacy_plist} -> .disabled)"
    mv "$legacy_plist" "${legacy_plist}.disabled"
  fi
}

install_services() {
  note "installing ${DAEMON_PLIST}"
  render_template "${TEMPLATE_DIR}/com.privacyfence.daemon.plist.tmpl" "$DAEMON_PLIST"
  note "installing ${COMPANION_PLIST}"
  render_template "${TEMPLATE_DIR}/com.privacyfence.companion.plist.tmpl" "$COMPANION_PLIST"
  start_daemon_as_service_account
  # The plist itself is what makes the companion start at every future GUI
  # login -- /Library/LaunchAgents is per-machine and launchd bootstraps it
  # into each session as that session is created. The two calls below only
  # start it for an *already* logged-in owner, so with none resolved (the
  # machine half) there is simply nothing to start early.
  #
  # Same bootout/bootstrap race as the daemon's (see wait_for_job_unloaded()
  # and bootstrap_with_retry() below): a companion still loaded from an
  # earlier enable -- an upgrade, or a re-run -- is torn down asynchronously,
  # and bootstrapping straight after the bootout can fail with exit 5/37.
  # Before this waited and retried, that failure was one warning in
  # /var/log/install.log and a companion that silently did not start until
  # the next login (macos-graphical-session.yml run 35871051261).
  if [ -n "$OWNER_UID" ]; then
    local companion_target="gui/${OWNER_UID}/${COMPANION_LABEL}" rc=0
    launchctl bootout "$companion_target" 2>/dev/null || true
    wait_for_job_unloaded "$companion_target" \
      || warn "${companion_target} still looked loaded ${BOOTOUT_SETTLE_TIMEOUT}s after bootout -- bootstrapping anyway"
    bootstrap_with_retry "gui/${OWNER_UID}" "$COMPANION_PLIST" || rc=$?
    if [ "$rc" -ne 0 ]; then
      warn "could not start the companion for ${OWNER_USER} now (launchctl bootstrap exited ${rc}) -- it will start at their next login"
    fi
  fi
}

daemon_pid() {
  # `|| true` on both halves deliberately: `launchctl print` exits non-zero
  # for a job that is not loaded, and with `set -o pipefail` that would make
  # this function's own failure indistinguishable from "no pid right now",
  # which is a state every caller here treats as ordinary.
  local printed
  printed="$(launchctl print "system/${DAEMON_LABEL}" 2>/dev/null || true)"
  printf '%s\n' "$printed" \
    | sed -n -e 's/^[[:space:]]*pid[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' -e '/^[0-9]/q' \
    || true
}

# The account a pid is running as, "" for an empty/absent pid. Split out of
# daemon_owner() below so `daemon status` (§2.1, unprivileged and meant to
# answer immediately) can ask the same question about whatever pid
# daemon_pid() reports *right now*, without also inheriting daemon_owner()'s
# own DAEMON_PID_TIMEOUT wait -- a status query has nothing to wait for: "no
# pid yet" is itself the answer it prints, not a transient state to poll
# through.
owner_of_pid() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  ps -o user= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true
}

daemon_owner() {
  # The account the daemon is *actually* running as, "" if it has no live pid
  # to read one off. Waits for a pid, because `launchctl bootstrap` returns
  # before RunAtLoad has finished spawning the job -- and then for that pid to
  # stop being launchd's xpcproxy trampoline, because launchd reports the pid
  # while xpcproxy is still running in it as root, before it switches to the
  # plist's UserName and execs the app. Reading the owner in that window
  # reports 'root' for a job launchd resolved perfectly well, and the caller
  # then tears a correct daemon down three times and gives up: v4.3.0's
  # post-release build.yml runs hit exactly that, with `launchctl print`
  # showing `state = xpcproxy` next to `username = _privacyfence`.
  local deadline=$((SECONDS + DAEMON_PID_TIMEOUT)) pid="" comm=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    pid="$(daemon_pid)"
    if [ -n "$pid" ]; then
      comm="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
      case "$comm" in
        xpcproxy|*/xpcproxy) ;;
        *) break ;;
      esac
    fi
    sleep 0.2
  done
  owner_of_pid "$pid"
}

daemon_account_diagnostics() {
  # Printed only when the daemon has already come up as the wrong account, so
  # the cost is nothing on a working install and the one report a reader
  # actually needs is in the output they already have -- rather than in a CI
  # artifact, or nowhere at all on a real Mac. Everything here answers the
  # same question from a different side: what the plist asked for, what the
  # directory says, and what launchd made of it.
  echo "---- UserName/GroupName in ${DAEMON_PLIST} ----"
  /usr/libexec/PlistBuddy -c 'Print :UserName' -c 'Print :GroupName' "$DAEMON_PLIST" 2>&1 || true
  echo "---- id ${SERVICE_ACCOUNT} ----"
  id "$SERVICE_ACCOUNT" 2>&1 || true
  echo "---- dscl . -read /Users/${SERVICE_ACCOUNT} ----"
  dscl . -read "/Users/${SERVICE_ACCOUNT}" UniqueID PrimaryGroupID NFSHomeDirectory UserShell 2>&1 || true
  echo "---- dscacheutil -q user -a name ${SERVICE_ACCOUNT} ----"
  dscacheutil -q user -a name "$SERVICE_ACCOUNT" 2>&1 || true
  echo "---- launchctl print system/${DAEMON_LABEL} (head) ----"
  launchctl print "system/${DAEMON_LABEL}" 2>&1 | head -40 || true
}

# How long to wait, after a `bootout`, for `launchctl print` to actually
# start failing before the next `bootstrap` -- launchd accepts a bootout
# request and tears the job down asynchronously, so issuing a bootstrap
# immediately afterwards can race the teardown and hand back exit 5 ("Input/
# output error") or 37 ("Operation already in progress") purely because the
# old instance had not finished leaving yet. Both start_daemon_as_service_
# account() and `daemon stop`/`daemon restart` below hit a bootout before a
# bootstrap or the next `launchctl print`, so this is shared rather than
# reimplemented at each call site.
BOOTOUT_SETTLE_TIMEOUT=10

# Takes the full service target (`system/<label>`, `gui/<uid>/<label>`), so
# install_services()' companion bootout/bootstrap in the owner's GUI domain
# waits out the same teardown the daemon's does.
wait_for_job_unloaded() {
  local target="$1" timeout="${2:-$BOOTOUT_SETTLE_TIMEOUT}" deadline
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    launchctl print "$target" >/dev/null 2>&1 || return 0
    sleep 0.2
  done
  return 1
}

wait_for_daemon_unloaded() {
  wait_for_job_unloaded "system/${DAEMON_LABEL}" "$@"
}

# The backoff schedule "daemon ensure-running"/"daemon restart" and
# start_daemon_as_service_account() below all retry a failing `launchctl
# bootstrap` against, once wait_for_daemon_unloaded() above has already
# removed the most common cause of exit 5/37 (a bootout that had not
# actually finished). A residual race can still happen -- launchd's own
# bookkeeping, not just the job's runtime state, needs a moment to settle --
# so this is the second layer, not a replacement for the wait.
BOOTSTRAP_RETRY_DELAYS=(1 2 4 8 16)

# Runs `launchctl bootstrap <domain> <plist>`, retrying with the backoff
# above when launchctl fails with exit 5 ("Input/output error") or 37
# ("Operation already in progress") -- both are symptoms of the bootout/
# bootstrap race this function exists to ride out, not of anything wrong
# with the plist or the account. Any other exit code is returned
# immediately, unretried: retrying a config error five times with sleeps in
# between only delays reporting it. Returns 0 on the bootstrap that
# succeeds, or the last nonzero exit code once every retry in
# BOOTSTRAP_RETRY_DELAYS has also failed with 5 or 37.
bootstrap_with_retry() {
  local domain="$1" plist="$2" delay rc
  if launchctl bootstrap "$domain" "$plist"; then
    return 0
  fi
  rc=$?
  for delay in "${BOOTSTRAP_RETRY_DELAYS[@]}"; do
    if [ "$rc" != 5 ] && [ "$rc" != 37 ]; then
      return "$rc"
    fi
    warn "launchctl bootstrap ${domain} exited ${rc} (bootout/bootstrap race) -- retrying in ${delay}s"
    sleep "$delay"
    if launchctl bootstrap "$domain" "$plist"; then
      return 0
    fi
    rc=$?
  done
  return "$rc"
}

bootstrap_daemon_with_retry() {
  bootstrap_with_retry system "$DAEMON_PLIST"
}

start_daemon_as_service_account() {
  # The one thing `launchctl bootstrap` will not tell you: it exits 0 having
  # started the job as root when the plist's UserName did not resolve
  # (privacyfence/privacyfence#598 Failure A). Everything downstream then
  # *says* the install is separated -- the marker, `status`, the approvals UI
  # -- while the daemon holds exactly the privileges separation exists to
  # drop. And it is not only a reporting problem: a root-owned daemon on a
  # separated install is refused by privilege_separation.check_runtime_
  # identity() on every start, so launchd's KeepAlive relaunches it forever
  # and no control socket ever appears -- #598's Failure B, same cause.
  #
  # wait_for_service_account() above removes the obvious reason for that
  # lookup to fail, and is not sufficient: this has been observed with
  # `id -u ${SERVICE_ACCOUNT}` already answering and `chown -R` already
  # having used the same name. launchd resolves UserName in its own process,
  # through its own cache, and nothing a script does from outside makes that
  # resolution observable *before* the job is started.
  #
  # So the guarantee is established by observation rather than by assumption:
  # start the job, read back which account it actually came up as, and if it
  # is the wrong one, tear it down, flush the lookup caches and start it
  # again. Only after every attempt has failed the same way is this fatal --
  # and it is fatal, rather than leaving a daemon running with every
  # privilege the install claims it dropped.
  #
  # That is Failure A, the UserName-resolution race -- a different failure
  # from the bootout/bootstrap I/O race (exit 5/37) bootstrap_daemon_with_
  # retry() above exists for, and both can happen to the very same
  # `bootstrap` call. So the two are layered rather than merged: each of
  # this loop's (up to three) attempts waits out the bootout first, then
  # lets bootstrap_daemon_with_retry() absorb the I/O race, and only then
  # reads back the owner to catch Failure A.
  local attempt owner
  for attempt in 1 2 3; do
    launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
    wait_for_daemon_unloaded || warn "${DAEMON_LABEL} still looked loaded ${BOOTOUT_SETTLE_TIMEOUT}s after bootout -- bootstrapping anyway"
    if ! bootstrap_daemon_with_retry; then
      warn "launchctl bootstrap kept failing with the bootout/bootstrap race (attempt ${attempt}) even after retrying with backoff"
      sleep 1
      continue
    fi
    owner="$(daemon_owner)"
    # No pid to read: under this job's KeepAlive a daemon that starts and
    # exits has none at any given instant. A real problem, but a different
    # one, and not this function's to diagnose (`status`, and the daemon's
    # own log under ${SYSTEM_ROOT}/logs, are) -- nothing here can be improved
    # by another bootstrap.
    if [ -z "$owner" ]; then
      warn "${DAEMON_LABEL} did not report a running pid within ${DAEMON_PID_TIMEOUT}s -- check ${SYSTEM_ROOT}/logs and 'sudo $0 status'"
      return 0
    fi
    if [ "$owner" = "$SERVICE_ACCOUNT" ]; then
      [ "$attempt" = "1" ] || note "${DAEMON_LABEL} is running as ${SERVICE_ACCOUNT} (attempt ${attempt})"
      return 0
    fi
    warn "${DAEMON_LABEL} started as '${owner}', not ${SERVICE_ACCOUNT} -- launchd did not resolve the plist's UserName (attempt ${attempt}); flushing the lookup caches and starting it again"
    daemon_account_diagnostics >&2
    dscacheutil -flushcache 2>/dev/null || true
    dsmemberutil flushcache 2>/dev/null || true
    id -u "$SERVICE_ACCOUNT" >/dev/null 2>&1 || true
    sleep 1
  done
  launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
  die "${DAEMON_LABEL} kept starting as '${owner}' instead of ${SERVICE_ACCOUNT} -- launchd is not resolving the UserName in ${DAEMON_PLIST}. The daemon has been stopped rather than left running with every privilege this install reports it dropped. Check 'dscl . -read /Users/${SERVICE_ACCOUNT}' and 'id ${SERVICE_ACCOUNT}', then re-run this command."
}

uninstall_services() {
  launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
  launchctl bootout "gui/${OWNER_UID}/${COMPANION_LABEL}" 2>/dev/null || true
  rm -f "$DAEMON_PLIST" "$COMPANION_PLIST"
}

# ── Daemon manager (#428 Phase 2) ─────────────────────────────────────────────
#
# `daemon {status|start|stop|restart|ensure-running}` is what the companion
# app's tray menu runs -- `status` unprivileged, on every poll, and
# `start`/`stop`/`restart` elevated (src/privacyfence/service_control.py),
# only when a human clicks the corresponding menu item. It is also what
# `cmd_enable` itself calls at the very end (see below), and what
# installer/macos/pkg/postinstall calls a second time after `enable --auto`
# returns, so that a daemon which comes up stopped after a .pkg upgrade gets
# one more chance to start before the tray's own Start button becomes the
# only way to notice.
#
# Up to 30s to see both a pid and the control socket -- generous on purpose:
# ensure_running/start/restart may have just issued a bootstrap or a
# kickstart, and launchd's own RunAtLoad, plus the daemon's own startup work
# (audit_layout(), opening its listeners), both take real wall-clock time.
DAEMON_READY_TIMEOUT=30

cmd_daemon_status() {
  # Unprivileged and meant to answer immediately -- see owner_of_pid()'s own
  # comment for why this does not call daemon_owner(). Exit 0 in every case
  # except "launchctl itself could not be run at all": launchd reporting
  # "not loaded" is itself the state a caller asked about, not a failure of
  # this subcommand.
  command -v launchctl >/dev/null 2>&1 \
    || die "launchctl not found -- cannot read ${DAEMON_LABEL}'s state"
  local printed="" loaded=false pid="" owner="" last_exit_status="" control_socket_present=false
  if printed="$(launchctl print "system/${DAEMON_LABEL}" 2>/dev/null)"; then
    loaded=true
  fi
  if [ "$loaded" = "true" ]; then
    pid="$(printf '%s\n' "$printed" \
      | sed -n -e 's/^[[:space:]]*pid[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' -e '/^[0-9]/q')"
    # "last exit code = " is launchctl print's own label -- kept exactly as
    # it prints it, since src/privacyfence/daemon_status.py's
    # _macos_status() parses the same field from its own launchctl print
    # call and the two must not drift apart on what to look for.
    last_exit_status="$(printf '%s\n' "$printed" \
      | sed -n -e 's/^[[:space:]]*last exit code[[:space:]]*=[[:space:]]*\(-\{0,1\}[0-9][0-9]*\).*/\1/p' -e '/^-\{0,1\}[0-9]/q')"
    owner="$(owner_of_pid "$pid")"
  fi
  [ -e "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}/control.sock" ] && control_socket_present=true
  printf 'loaded=%s\n' "$loaded"
  printf 'pid=%s\n' "$pid"
  printf 'owner=%s\n' "$owner"
  printf 'last_exit_status=%s\n' "$last_exit_status"
  printf 'control_socket_present=%s\n' "$control_socket_present"
  return 0
}

cmd_daemon_ensure_running() {
  # 1-2: not loaded -> bootstrap (with the same bootout/bootstrap-race retry
  # start_daemon_as_service_account() uses, since the same launchd race can
  # happen to a plain `daemon start` as to `enable`'s own first bootstrap).
  if ! launchctl print "system/${DAEMON_LABEL}" >/dev/null 2>&1; then
    note "${DAEMON_LABEL} is not loaded -- bootstrapping it"
    bootstrap_daemon_with_retry \
      || die "launchctl bootstrap kept failing (bootout/bootstrap race) even after retrying with backoff -- check ${SYSTEM_ROOT}/logs"
  # 3: loaded but no pid -> kickstart. No -k here: that flag kills and
  # relaunches an already-running job, which is `daemon restart`'s job, not
  # this one's -- a loaded-but-pidless job has nothing running to kill.
  elif [ -z "$(daemon_pid)" ]; then
    note "${DAEMON_LABEL} is loaded but has no pid -- kickstarting it"
    launchctl kickstart "system/${DAEMON_LABEL}" \
      || die "launchctl kickstart failed for ${DAEMON_LABEL}"
  fi

  # 4: wait for both a pid and the control socket.
  local deadline=$((SECONDS + DAEMON_READY_TIMEOUT)) pid="" sock="${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}/control.sock"
  while [ "$SECONDS" -lt "$deadline" ]; do
    pid="$(daemon_pid)"
    [ -n "$pid" ] && [ -e "$sock" ] && break
    sleep 0.5
  done
  [ -n "$pid" ] \
    || die "${DAEMON_LABEL} still has no pid ${DAEMON_READY_TIMEOUT}s after starting it -- check ${SYSTEM_ROOT}/logs and 'sudo $0 status'"
  [ -e "$sock" ] \
    || die "${DAEMON_LABEL} has pid ${pid} but ${sock} still does not exist ${DAEMON_READY_TIMEOUT}s later -- check ${SYSTEM_ROOT}/logs"

  # 5: verify the owner.
  local owner
  owner="$(owner_of_pid "$pid")"
  [ "$owner" = "$SERVICE_ACCOUNT" ] \
    || die "${DAEMON_LABEL} (pid ${pid}) is running as '${owner}', not ${SERVICE_ACCOUNT} -- refusing to report success"

  note "${DAEMON_LABEL} is running as ${SERVICE_ACCOUNT} (pid ${pid})"
}

cmd_daemon_restart() {
  if launchctl print "system/${DAEMON_LABEL}" >/dev/null 2>&1; then
    note "restarting ${DAEMON_LABEL}"
    launchctl kickstart -k "system/${DAEMON_LABEL}" \
      || die "launchctl kickstart -k failed for ${DAEMON_LABEL}"
  fi
  # Whether that kickstart just ran, or the job was not loaded at all: the
  # same wait-for-pid-and-socket-and-owner verification applies either way,
  # so ensure-running's own logic finishes the job rather than duplicating
  # its last three steps here.
  cmd_daemon_ensure_running
}

cmd_daemon_stop() {
  launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
  if wait_for_daemon_unloaded 15; then
    note "${DAEMON_LABEL} stopped"
    return 0
  fi
  die "${DAEMON_LABEL} is still loaded 15s after bootout -- 'sudo launchctl print system/${DAEMON_LABEL}' to see why"
}

cmd_daemon() {
  local subcommand="$1"
  case "$subcommand" in
    status)
      require_macos
      cmd_daemon_status
      ;;
    start | ensure-running)
      require_macos
      require_root
      cmd_daemon_ensure_running
      ;;
    restart)
      require_macos
      require_root
      cmd_daemon_restart
      ;;
    stop)
      require_macos
      require_root
      cmd_daemon_stop
      ;;
    *)
      usage
      ;;
  esac
}

# ── Subcommands ───────────────────────────────────────────────────────────────

cmd_enable() {
  require_macos
  require_root
  # Deliberately the optional resolution, not resolve_owner()'s die() (ADR
  # 0003 decision 3): with no owner account to resolve -- a .pkg installed
  # with nobody at the console, an MDM push -- this still separates the
  # machine completely, and records the one step that genuinely needs a human
  # (the group membership) as pending rather than abandoning the whole
  # install to the unseparated layout the way it used to.
  resolve_owner_optional
  resolve_executables

  if [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ]; then
    note "already separated -- re-running to refresh the account, layout and launchd jobs"
  fi

  stop_legacy_agent
  launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true

  create_service_account
  # Before anything that resolves the account by name: apply_layout()'s
  # `chown -R`, and install_services()' `launchctl bootstrap`, which is the
  # one that fails *silently* (#598 Failure A). See wait_for_service_account().
  wait_for_service_account
  if [ -n "$OWNER_USER" ]; then
    add_owner_to_service_group
  else
    note "no owner account resolved -- leaving the ${SERVICE_GROUP} membership pending"
  fi
  migrate_data
  apply_layout
  # After apply_layout(), never before -- see stage_trusted_image()'s own
  # comment on why (apply_layout() recursively re-owns SYSTEM_ROOT itself,
  # which would undo this if it ran first; TRUSTED_IMAGE_DIR lives outside
  # SYSTEM_ROOT regardless, so the two never actually touch each other).
  stage_trusted_image
  write_marker
  install_services

  # #428 Phase 2: install_services()'s own start_daemon_as_service_account()
  # already tries to get the daemon running, but "tried and the LaunchDaemon
  # reported a pid" is not the same claim as "reachable, with a control
  # socket, and owned by the right account" -- and the #598 upgrade-gap bug
  # this phase exists to close was exactly a case where the first succeeded
  # and the second silently did not. Run in a subshell, the same way --auto
  # itself is below: cmd_daemon_ensure_running's own die() would otherwise
  # take this whole `enable` down with it under set -euo pipefail, which is
  # not what a hiccup *here* -- after everything else above has already
  # succeeded -- should do to the rest of the install. A failure is reported,
  # once, clearly, and left to the companion's own Start button (or
  # installer/macos/pkg/postinstall's own follow-up ensure-running call) to
  # recover from.
  if ! ( cmd_daemon_ensure_running ); then
    warn "could not confirm ${DAEMON_LABEL} is running after enable -- check ${SYSTEM_ROOT}/logs, or run 'sudo $0 daemon ensure-running' by hand. The companion app's tray menu will offer to start it."
  fi

  cat <<DONE

✓ PrivacyFence now runs as ${SERVICE_ACCOUNT}.

  Data directory   ${SYSTEM_ROOT}
  Human authority  ${SYSTEM_ROOT}/authority   (0700, ${SERVICE_ACCOUNT} only)
  Shared handoff   ${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}   (3770, ${SERVICE_GROUP} group)
DONE

  if [ -n "$OWNER_USER" ]; then
    cat <<DONE

  One thing left to do by hand: log ${OWNER_USER} out and back in. macOS
  evaluates group membership when a login session is created, so the session
  you are in right now still does not know it is in ${SERVICE_GROUP} -- which
  means the companion app and your MCP client cannot reach the handoff
  directory until you do. 'sudo $0 status' will tell you when it has taken.
DONE
  else
    cat <<DONE

  Nobody is in ${SERVICE_GROUP} yet: this ran with no human account to add,
  which is the ordinary case for an MDM push or a .pkg installed with nobody
  at the console. The install is separated regardless -- what is pending is
  one re-runnable step, which the companion app takes by itself at the first
  real login session, or which you can take now:

    sudo $0 enable --for-user <name>
DONE
  fi

  cat <<DONE

  What this does and does not buy you is written down in
  docs/security-and-compliance.md's "Local-mode trust boundary" section. The
  short version: the agent can no longer rewrite your policy, forge a passkey
  or read the audit key -- and an agent that can get root still defeats all
  of it, because root defeats everything.
DONE
}

cmd_enable_for_user() {
  # ADR 0003 decision 3's per-user half, on its own: the two steps of `enable`
  # that need to know which human this install is for. Runs against an install
  # the machine half has already separated, and re-runs harmlessly against one
  # that is already complete -- dseditgroup is idempotent, there is nothing
  # left to migrate once ~/.privacyfence is gone, and the layout and marker
  # are rewritten to the same values.
  #
  # Deliberately no resolve_executables()/stage_trusted_image(): this installs
  # no launchd jobs and starts nothing, so an install whose .app has moved can
  # still have a second user added to the group.
  require_macos
  require_root
  resolve_owner

  [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ] \
    || die "this install is not privilege-separated yet -- run 'sudo $0 enable' first"

  # #428 Phase 2 §2.6's interim multi-user guard used to refuse this outright
  # for anyone but the recorded owner, because the only alternative on offer
  # at the time was merging a second account's ~/.privacyfence into data the
  # first owner's connectors already lived in. ADR 0008 ("D2: two identities,
  # not one, per install") replaces that refusal with a real second identity
  # instead: adding another OS account to ${SERVICE_GROUP} is now the normal,
  # supported way to let more than one human use this install, and
  # migrate_data() below sends that account's own data into its own isolated
  # users/os-<uid>/ rather than the shared root, so nothing merges. There is
  # no override flag to pass here any more -- this always was the interim
  # guard's job, and now that the isolation exists, the guard has nothing
  # left to guard against.
  local recorded_owner
  recorded_owner="$(marker_owner_user)"
  if [ -n "$recorded_owner" ] && [ "$recorded_owner" != "$OWNER_USER" ]; then
    note "adding '${OWNER_USER}' alongside this install's existing owner '${recorded_owner}' -- each gets its own isolated PrivacyFence identity (docs/adr/0008-one-principal-per-os-user.md); '${OWNER_USER}'s own $(legacy_data_dir) will be migrated into its own storage, not merged with '${recorded_owner}'s"
    NON_OWNER_FOR_USER=1
  fi

  add_owner_to_service_group
  # Anything this human accumulated under ~/.privacyfence before the machine
  # half ran -- live connector OAuth tokens included -- still has to follow
  # the service account, and it merges in owned by them at their own modes.
  # So the layout is re-asserted rather than assumed, and the marker is
  # rewritten with the owner it was missing.
  migrate_data
  apply_layout
  write_marker

  cat <<DONE

✓ ${OWNER_USER} is now a member of ${SERVICE_GROUP}.

  One thing left to do by hand: log ${OWNER_USER} out and back in. macOS
  evaluates group membership when a login session is created, so the session
  you are in right now still does not know it is in ${SERVICE_GROUP} -- which
  means the companion app and your MCP client cannot reach the handoff
  directory until you do. 'sudo $0 status' will tell you when it has taken.
DONE
}

cmd_disable() {
  require_macos
  require_root
  resolve_owner

  [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ] || die "this install is not privilege-separated (no ${SYSTEM_ROOT}/${MARKER_NAME})"

  note "stopping and removing the LaunchDaemon and companion LaunchAgent"
  uninstall_services
  rm -rf "$(dirname "$TRUSTED_IMAGE_DIR")"

  local legacy
  legacy="$(legacy_data_dir)"
  # The marker goes first: if anything below fails, what is left behind is an
  # unseparated install pointing at a directory that still exists, rather than
  # a separated one whose services are gone.
  rm -f "${SYSTEM_ROOT}/${MARKER_NAME}"
  move_handoff_files_out
  if [ -e "$legacy" ]; then
    warn "${legacy} already exists -- merging ${SYSTEM_ROOT} into it"
    ditto "$SYSTEM_ROOT" "$legacy"
    rm -rf "$SYSTEM_ROOT"
  else
    note "moving ${SYSTEM_ROOT} back to ${legacy}"
    mv "$SYSTEM_ROOT" "$legacy"
  fi
  chown -R "${OWNER_USER}" "$legacy"
  chmod -R go-rwx "$legacy"
  chmod 700 "$legacy"

  local legacy_plist="${OWNER_HOME}/Library/LaunchAgents/${LEGACY_AGENT_LABEL}.plist"
  if [ -f "${legacy_plist}.disabled" ]; then
    note "restoring the old per-user LaunchAgent"
    mv "${legacy_plist}.disabled" "$legacy_plist"
    launchctl bootstrap "gui/${OWNER_UID}" "$legacy_plist" 2>/dev/null || true
  fi

  cat <<DONE

✓ Privilege separation is off. Your data is back at ${legacy}, owned by
  ${OWNER_USER} again.

  The ${SERVICE_ACCOUNT} account and group are left in place on purpose -- they
  own nothing now, and keeping them means re-enabling doesn't have to pick a
  new uid. Remove them with:
    sudo dscl . -delete /Users/${SERVICE_ACCOUNT}
    sudo dscl . -delete /Groups/${SERVICE_GROUP}
DONE
}

resolve_owner_optional() {
  # status has to work on a machine where --user wasn't passed and SUDO_USER
  # isn't set (run without sudo, which status deliberately allows), so it
  # can't use resolve_owner()'s own die(). enable's machine half (ADR 0003
  # decision 3) takes the same shape for the same reason.
  OWNER_USER="${OWNER_USER:-${SUDO_USER:-}}"
  # `sudo` from a root login shell leaves SUDO_USER=root, and root is never
  # the human an install belongs to -- resolve_owner() refuses it outright,
  # and reaching a different answer here would have the machine half add root
  # to the service group.
  [ "$OWNER_USER" != "root" ] || OWNER_USER=""
  [ -n "$OWNER_USER" ] || return 0
  OWNER_UID="$(id -u "$OWNER_USER" 2>/dev/null || true)"
  OWNER_HOME="$(dscl . -read "/Users/${OWNER_USER}" NFSHomeDirectory 2>/dev/null | sed 's/^NFSHomeDirectory: //')"
}

# Permission bits *including* setuid/setgid/sticky, as `chmod` takes them
# (711, 3770). `%OLp` alone drops the leading digit, so handoff/'s 3770 always
# read back as 770 and `status` reported a correct install as WRONG MODE.
octal_mode() {
  local mode
  mode="$(stat -f '%Op' "$1")" || return 1
  mode="${mode: -4}"
  printf '%s\n' "${mode#0}"
}

cmd_status() {
  require_macos
  resolve_owner_optional

  if [ ! -f "${SYSTEM_ROOT}/${MARKER_NAME}" ]; then
    echo "privilege separation: OFF"
    if [ -n "$OWNER_HOME" ]; then echo "  data directory: $(legacy_data_dir)"; fi
    echo "  the daemon and the AI agent run as the same account (${OWNER_USER:-this one})."
    echo "  Run 'sudo $0 enable' to change that."
    return 0
  fi

  echo "privilege separation: ON"
  echo "  marker:          ${SYSTEM_ROOT}/${MARKER_NAME}"
  echo "  data directory:  ${SYSTEM_ROOT}"
  local problems=0
  # Distinct from "OFF" above on purpose (ADR 0003 decision 3): this install
  # *is* separated -- the machine half ran -- and what is outstanding is one
  # re-runnable step. Reporting it as not separated would say the daemon and
  # the agent share an account, which is exactly what is no longer true.
  local marker_owner
  marker_owner="$(marker_owner_user)"
  if [ -n "$marker_owner" ]; then
    echo "  owner:           ${marker_owner}"
  else
    echo "  PENDING USER     no owner recorded -- nobody has been added to ${SERVICE_GROUP} yet."
    echo "                   The companion app closes this at the first login session, or:"
    echo "                   sudo $0 enable --for-user <name>"
    problems=1
  fi
  _check_mode() {
    local path="$1" expected="$2" actual
    actual="$(octal_mode "$path" 2>/dev/null || true)"
    if [ -z "$actual" ]; then
      echo "  MISSING          ${path}"
      problems=1
    elif [ "$actual" != "$expected" ]; then
      echo "  WRONG MODE       ${path} is ${actual}, expected ${expected}"
      problems=1
    else
      echo "  ok  ${expected}       ${path}"
    fi
  }
  _check_mode "$SYSTEM_ROOT" "$SYSTEM_ROOT_MODE"
  _check_mode "${SYSTEM_ROOT}/authority" "$AUTHORITY_DIR_MODE"
  _check_mode "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" "$HANDOFF_DIR_MODE"

  local authority_owner
  authority_owner="$(stat -f '%Su' "${SYSTEM_ROOT}/authority" 2>/dev/null || true)"
  if [ "$authority_owner" != "$SERVICE_ACCOUNT" ]; then
    echo "  NOT SEPARATED    ${SYSTEM_ROOT}/authority is owned by '${authority_owner}', not ${SERVICE_ACCOUNT}"
    problems=1
  fi

  if [ -n "$OWNER_USER" ]; then
    if dseditgroup -o checkmember -m "$OWNER_USER" "$SERVICE_GROUP" >/dev/null 2>&1; then
      echo "  ok               ${OWNER_USER} is a member of ${SERVICE_GROUP}"
    else
      echo "  NOT A MEMBER     ${OWNER_USER} is not in ${SERVICE_GROUP} -- the companion cannot reach the daemon"
      problems=1
    fi
    # Distinct from the check above: membership can be recorded in the
    # directory and still absent from an already-running login session, which
    # is exactly the "log out and back in" case enable prints about.
    if id -Gn "$OWNER_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$SERVICE_GROUP"; then
      echo "  ok               ${OWNER_USER}'s login session has picked that membership up"
    else
      echo "  PENDING LOGOUT   ${OWNER_USER}'s current session predates the group change -- log out and back in"
      problems=1
    fi
  fi

  launchctl print "system/${DAEMON_LABEL}" >/dev/null 2>&1 \
    && echo "  ok               ${DAEMON_LABEL} is loaded" \
    || { echo "  NOT LOADED       ${DAEMON_LABEL}"; problems=1; }

  # ADR 0008: the recorded `owner_user` above is only this install's *first*
  # principal, not its only one -- `enable --for-user` for a second account
  # gives it its own users/os-<uid>/ instead of touching the owner's data at
  # all (see migrate_data()'s ADR 0008 comment), so it never shows up as an
  # "owner" and would otherwise be invisible here. This lists whichever of
  # those subdirectories actually exist, which is a report of who has
  # finished the per-user half, not of who is merely in ${SERVICE_GROUP} --
  # a group member who hasn't yet run `enable --for-user` (or the companion
  # app hasn't done it for them) has no directory here yet and is still
  # "pending" in the same sense the very first owner_membership_pending()
  # case always was.
  if [ -d "${SYSTEM_ROOT}/users" ]; then
    local other_dir other_uid other_name printed_header=0
    for other_dir in "${SYSTEM_ROOT}/users"/os-*; do
      [ -d "$other_dir" ] || continue
      if [ "$printed_header" = "0" ]; then
        echo "  other accounts using this install:"
        printed_header=1
      fi
      other_uid="$(basename "$other_dir")"
      other_uid="${other_uid#os-}"
      # Best-effort uid->name for a friendlier report; the directory name
      # itself (os-<uid>) is what actually matters and is printed either way.
      other_name="$(dscl . -search /Users UniqueID "$other_uid" 2>/dev/null | awk '{print $1; exit}')"
      if [ -n "$other_name" ]; then
        echo "    ${other_name} (os-${other_uid})"
      else
        echo "    os-${other_uid} (no matching /Users record -- account since removed?)"
      fi
    done
  fi

  [ "$problems" = "0" ] || return 1
}

# ── Argument parsing ──────────────────────────────────────────────────────────

[ $# -ge 1 ] || usage
COMMAND="$1"; shift
# `daemon`'s sub-verb is a positional, consumed here, before the generic
# option-parsing loop below ever runs -- that loop's `*) die "unknown
# option: $1"` would otherwise treat `start`/`stop`/etc. as an unrecognized
# flag, since none of its `--xxx` cases match a bare word. Every other
# command here takes only `--flag [value]` options, so this is the one place
# a second positional argument is legal at all.
if [ "$COMMAND" = "daemon" ]; then
  [ $# -ge 1 ] || usage
  DAEMON_SUBCOMMAND="$1"; shift
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --user) OWNER_USER="${2:-}"; shift 2 ;;
    --for-user) OWNER_USER="${2:-}"; FOR_USER_ONLY=1; shift 2 ;;
    --app) APP_PATH="${2:-}"; shift 2 ;;
    --daemon-exec) DAEMON_EXECUTABLE="${2:-}"; shift 2 ;;
    --companion-exec) COMPANION_EXECUTABLE="${2:-}"; shift 2 ;;
    --auto) AUTO=1; shift ;;
    -h|--help) usage ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$COMMAND" in
  enable)
    # --for-user selects which half runs; --auto is orthogonal and wraps
    # whichever one it is -- which is what lets the companion app's own
    # elevated invocation (ADR 0003 decision 3) reuse the same non-fatal
    # shape the daemon's auto-enable prompt already has.
    if [ "$FOR_USER_ONLY" = "1" ]; then
      ENABLE_COMMAND=cmd_enable_for_user
    else
      ENABLE_COMMAND=cmd_enable
    fi
    if [ "$AUTO" = "1" ]; then
      note "auto-enabling privilege separation (#428 D1, 4.1)"
      # Subshell, not a direct call: die() calls exit, which under
      # set -euo pipefail would take this whole process down with it if
      # called directly -- including whatever elevated `do shell script`
      # invoked us. A subshell's exit only ends the subshell, and testing it
      # in `if` is exempt from errexit, so a resolve_owner()/
      # resolve_executables()/cmd_enable failure lands here instead.
      if ! ( "$ENABLE_COMMAND" ); then
        warn "auto-enable did not run to completion."
        warn "rerun without --auto to see why, or once it's clear: sudo $0 enable"
      fi
    else
      "$ENABLE_COMMAND"
    fi
    ;;
  disable) cmd_disable ;;
  status) cmd_status ;;
  daemon) cmd_daemon "$DAEMON_SUBCOMMAND" ;;
  *) usage ;;
esac
