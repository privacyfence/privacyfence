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
#      re-owns it -- authority/ at 0700, handoff/ at 2770, the root at 0711;
#   4. writes the marker file every PrivacyFence process reads to agree on that
#      layout (src/privacyfence/privilege_separation.py);
#   5. replaces the login-session LaunchAgent with a LaunchDaemon for the
#      daemon and a LaunchAgent for the companion app -- ADR 0002's "startup
#      wiring inverts".
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
# resolvable owner, no installed executables), it instead logs why and exits
# 0 rather than leaving a half-finished separation behind. A human running
# this by hand never wants that silent behavior, which is why --auto isn't
# the default -- and it still needs the admin password, same as always:
# nothing about --auto skips require_root.
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
HANDOFF_DIR_MODE=2770
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

  --user <name>       the human account that owns this install
                      (default: \$SUDO_USER, i.e. whoever ran sudo)
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
USAGE
  exit 2
}

AUTO=0

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

add_owner_to_service_group() {
  note "adding ${OWNER_USER} to the ${SERVICE_GROUP} group"
  dseditgroup -o edit -a "$OWNER_USER" -t user "$SERVICE_GROUP"
}

# ── Data migration ────────────────────────────────────────────────────────────

legacy_data_dir() { printf '%s/.privacyfence' "$OWNER_HOME"; }

migrate_data() {
  local legacy
  legacy="$(legacy_data_dir)"
  if [ ! -d "$legacy" ]; then
    note "no existing ${legacy} to migrate -- starting the separated install empty"
    mkdir -p "$SYSTEM_ROOT"
    return
  fi
  if [ -e "$SYSTEM_ROOT" ]; then
    # Something is already there (a previous enable, or a hand-made directory).
    # Merge rather than clobber, then remove the source -- leaving a second
    # copy of live OAuth tokens readable by the agent would undo the point of
    # the whole exercise.
    note "merging ${legacy} into the existing ${SYSTEM_ROOT}"
    ditto "$legacy" "$SYSTEM_ROOT"
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
# files a human or the companion reads (web_base_url, <page>_url). Mirrors
# paths.handoff_dir()'s callers -- see test_privilege_separation.py, which
# asserts this list matches the file-name constants those call sites use.
HANDOFF_FILE_NAMES=(mcp_token mcp_url web_base_url)
# <page>_url, one per page mint_bootstrap_url() has ever minted a link for
# (web/server.py's _bootstrap_url_file_name): approvals_url, settings_url.
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
  chown -R "${SERVICE_ACCOUNT}:${SERVICE_GROUP}" "$SYSTEM_ROOT"
  # Tighten everything first, then re-open exactly the two places that have to
  # be reachable from the user's own session. Order matters: the blanket
  # go-rwx below would otherwise undo the handoff directory's group bits.
  chmod -R go-rwx "$SYSTEM_ROOT"
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
  launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
  launchctl bootstrap system "$DAEMON_PLIST"
  launchctl bootout "gui/${OWNER_UID}/${COMPANION_LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/${OWNER_UID}" "$COMPANION_PLIST" || warn "could not start the companion for ${OWNER_USER} now -- it will start at their next login"
}

uninstall_services() {
  launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
  launchctl bootout "gui/${OWNER_UID}/${COMPANION_LABEL}" 2>/dev/null || true
  rm -f "$DAEMON_PLIST" "$COMPANION_PLIST"
}

# ── Subcommands ───────────────────────────────────────────────────────────────

cmd_enable() {
  require_macos
  require_root
  resolve_owner
  resolve_executables

  if [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ]; then
    note "already separated -- re-running to refresh the account, layout and launchd jobs"
  fi

  stop_legacy_agent
  launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true

  create_service_account
  add_owner_to_service_group
  migrate_data
  apply_layout
  write_marker
  install_services

  cat <<DONE

✓ PrivacyFence now runs as ${SERVICE_ACCOUNT}.

  Data directory   ${SYSTEM_ROOT}
  Human authority  ${SYSTEM_ROOT}/authority   (0700, ${SERVICE_ACCOUNT} only)
  Shared handoff   ${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}   (2770, ${SERVICE_GROUP} group)

  One thing left to do by hand: log ${OWNER_USER} out and back in. macOS
  evaluates group membership when a login session is created, so the session
  you are in right now still does not know it is in ${SERVICE_GROUP} -- which
  means the companion app and your MCP client cannot reach the handoff
  directory until you do. 'sudo $0 status' will tell you when it has taken.

  What this does and does not buy you is written down in
  docs/security-and-compliance.md's "Local-mode trust boundary" section. The
  short version: the agent can no longer rewrite your policy, forge a passkey
  or read the audit key -- and an agent that can get root still defeats all
  of it, because root defeats everything.
DONE
}

cmd_disable() {
  require_macos
  require_root
  resolve_owner

  [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ] || die "this install is not privilege-separated (no ${SYSTEM_ROOT}/${MARKER_NAME})"

  note "stopping and removing the LaunchDaemon and companion LaunchAgent"
  uninstall_services

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
  # can't use resolve_owner()'s own die().
  OWNER_USER="${OWNER_USER:-${SUDO_USER:-}}"
  [ -n "$OWNER_USER" ] || return 0
  OWNER_UID="$(id -u "$OWNER_USER" 2>/dev/null || true)"
  OWNER_HOME="$(dscl . -read "/Users/${OWNER_USER}" NFSHomeDirectory 2>/dev/null | sed 's/^NFSHomeDirectory: //')"
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
  _check_mode() {
    local path="$1" expected="$2" actual
    actual="$(stat -f '%OLp' "$path" 2>/dev/null || true)"
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

  [ "$problems" = "0" ] || return 1
}

# ── Argument parsing ──────────────────────────────────────────────────────────

[ $# -ge 1 ] || usage
COMMAND="$1"; shift
while [ $# -gt 0 ]; do
  case "$1" in
    --user) OWNER_USER="${2:-}"; shift 2 ;;
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
    if [ "$AUTO" = "1" ]; then
      note "auto-enabling privilege separation (#428 D1, 4.1)"
      # Subshell, not a direct call: die() calls exit, which under
      # set -euo pipefail would take this whole process down with it if
      # called directly -- including whatever elevated `do shell script`
      # invoked us. A subshell's exit only ends the subshell, and testing it
      # in `if` is exempt from errexit, so a resolve_owner()/
      # resolve_executables()/cmd_enable failure lands here instead.
      if ! ( cmd_enable ); then
        warn "auto-enable did not run to completion -- this install stays opt-in."
        warn "rerun without --auto to see why, or once it's clear: sudo $0 enable"
      fi
    else
      cmd_enable
    fi
    ;;
  disable) cmd_disable ;;
  status) cmd_status ;;
  *) usage ;;
esac
