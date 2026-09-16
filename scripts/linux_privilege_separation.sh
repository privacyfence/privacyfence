#!/usr/bin/env bash
# #428 Phase 4 (B5b): opt into -- or back out of -- running the PrivacyFence
# daemon under its own Linux account.
#
# Until this runs, the daemon and the AI agent it exists to govern are the same
# OS user, which is the root cause of all four weaknesses issue #428 describes:
# the agent can read the session-minting credential, rewrite the always-allow
# rules and PII policy that decide what it is allowed to do, forge a WebAuthn
# credential into the store a local passkey would be checked against, and read
# the audit log's HMAC key. One change closes all four -- a dedicated account
# owning those files -- and this script is that change, made reversible.
#
#   sudo ./scripts/linux_privilege_separation.sh enable      # from a checkout
#   sudo privacyfence-privilege-separation enable            # from the .deb
#   ... status
#   ... disable
#
# The .deb installs this same file as /usr/sbin/privacyfence-privilege-
# separation, with the templates it renders under /usr/share/privacyfence/ --
# see TEMPLATE_DIR below. Both invocations do exactly the same thing.
#
# What `enable` does, in order:
#   1. creates the privacyfence system user and group;
#   2. adds you to that group, so the companion app can still reach the daemon;
#   3. moves ~/.privacyfence to /var/lib/privacyfence and re-owns it --
#      authority/ at 0700, handoff/ at 2770, the root at 0711;
#   4. writes the marker file every PrivacyFence process reads to agree on that
#      layout (src/privacyfence/privilege_separation.py);
#   5. replaces the two things that start the daemon in your own session -- the
#      XDG autostart entry the .deb installs and the `--user` systemd unit a
#      pip/pipx install documents -- with a *system* systemd unit for the
#      daemon and an XDG autostart entry for the companion's control channel.
#      That is ADR 0002's "startup wiring inverts", on this platform.
#
# Step 3 moves live connector OAuth tokens. `disable` moves them back, but this
# is still the step to take a backup before: it is the one part of this that
# touches data you cannot re-mint from a config file.
#
# Ships opt-in deliberately. #428 P4 does not default on for a platform until
# that platform's opt-in has soaked through a full release cycle.
set -euo pipefail

# ── Constants. Every one of these is also declared in
#    src/privacyfence/privilege_separation.py, and
#    tests/unit/test_privilege_separation.py asserts the two agree -- this
#    script and that module are the two halves of one contract, and a silent
#    drift between them would leave a daemon looking for its data somewhere
#    the installer never put it. ──────────────────────────────────────────────
SERVICE_ACCOUNT="privacyfence"
SERVICE_GROUP="privacyfence"
SYSTEM_ROOT="/var/lib/privacyfence"
MARKER_NAME="privilege-separation.json"
MARKER_VERSION=1
HANDOFF_DIR_NAME="handoff"
SYSTEM_ROOT_MODE=711
HANDOFF_DIR_MODE=2770
AUTHORITY_DIR_MODE=700
HANDOFF_FILE_MODE=640

DAEMON_UNIT="privacyfence-daemon.service"
DAEMON_UNIT_PATH="/etc/systemd/system/${DAEMON_UNIT}"
COMPANION_AUTOSTART="privacyfence-companion.desktop"
COMPANION_AUTOSTART_PATH="/etc/xdg/autostart/${COMPANION_AUTOSTART}"
# The two pre-Phase-4 ways the daemon starts in the logged-in user's own
# session. Left in place, either would start a *second* daemon as that user --
# which, on a separated install, now refuses to start rather than quietly
# seeding a default policy (privilege_separation.check_runtime_identity).
LEGACY_AUTOSTART_PATH="/etc/xdg/autostart/privacyfence.desktop"
LEGACY_USER_UNIT="privacyfence.service"

DEFAULT_DAEMON_EXECUTABLE="/usr/bin/privacyfence-app"
DEFAULT_COMPANION_EXECUTABLE="/usr/bin/privacyfence-companion"
# This script runs from two places: a source checkout (installer/linux/ sits
# next to scripts/) and the .deb, which installs it as
# /usr/sbin/privacyfence-privilege-separation with its templates under
# /usr/share/privacyfence/. Checking for the checkout layout first means a
# developer's edits to a template take effect without reinstalling anything,
# while a packaged install -- the only one most Linux desktop users have --
# still finds them. render_template() dies on a template that isn't there, so
# a wrong answer here fails loudly rather than installing half a unit.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGED_TEMPLATE_DIR="/usr/share/privacyfence/installer/linux"
if [ -d "${REPO_ROOT}/installer/linux" ]; then
  TEMPLATE_DIR="${REPO_ROOT}/installer/linux"
else
  TEMPLATE_DIR="$PACKAGED_TEMPLATE_DIR"
fi

# Filled in by resolve_owner()/resolve_executables() below.
OWNER_USER=""
OWNER_UID=""
OWNER_HOME=""
DAEMON_EXECUTABLE=""
COMPANION_EXECUTABLE=""

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '→ %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

usage() {
  cat >&2 <<USAGE
usage: sudo $0 {enable|disable|status} [options]

  --user <name>       the human account that owns this install
                      (default: \$SUDO_USER, i.e. whoever ran sudo)
  --daemon-exec <p>   run this instead of ${DEFAULT_DAEMON_EXECUTABLE}
  --companion-exec <p> run this instead of ${DEFAULT_COMPANION_EXECUTABLE}
                      (both together let a source/venv install be separated:
                       --daemon-exec .venv/bin/privacyfence-app
                       --companion-exec .venv/bin/privacyfence-companion)
USAGE
  exit 2
}

require_linux() {
  [ "$(uname -s)" = "Linux" ] || die "this script is Linux-only (macOS is scripts/macos_privilege_separation.sh; #428 P4's Windows phase is B5c)"
}

require_systemd() {
  [ -d /run/systemd/system ] || die "this needs systemd as PID 1 -- nothing here knows how to install a service under another init"
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
}

require_root() {
  [ "$(id -u)" = "0" ] || die "run this with sudo -- creating a system account and a system unit both need root"
}

resolve_owner() {
  if [ -z "$OWNER_USER" ]; then
    OWNER_USER="${SUDO_USER:-}"
  fi
  [ -n "$OWNER_USER" ] || die "could not tell which human account owns this install -- pass --user <name>"
  [ "$OWNER_USER" != "root" ] || die "--user must be a real login account, not root"
  OWNER_UID="$(id -u "$OWNER_USER" 2>/dev/null)" || die "no such user: ${OWNER_USER}"
  OWNER_HOME="$(getent passwd "$OWNER_USER" | cut -d: -f6)"
  [ -n "$OWNER_HOME" ] || die "could not resolve ${OWNER_USER}'s home directory"
}

resolve_executables() {
  : "${DAEMON_EXECUTABLE:=$DEFAULT_DAEMON_EXECUTABLE}"
  : "${COMPANION_EXECUTABLE:=$DEFAULT_COMPANION_EXECUTABLE}"
  [ -x "$DAEMON_EXECUTABLE" ] || die "not executable: ${DAEMON_EXECUTABLE} -- pass --daemon-exec for a source or venv install"
  # The companion is what a human uses once the daemon has no desktop session
  # of its own, so a separated install without one is a locked door. Refuse
  # rather than install half of ADR 0002's inversion.
  [ -x "$COMPANION_EXECUTABLE" ] || die "not executable: ${COMPANION_EXECUTABLE} -- a separated install needs the companion app (ADR 0002 decision 2)"
}

# ── Account provisioning ──────────────────────────────────────────────────────

service_account_exists() { getent passwd "$SERVICE_ACCOUNT" >/dev/null 2>&1; }
service_group_exists() { getent group "$SERVICE_GROUP" >/dev/null 2>&1; }

nologin_shell() {
  # Debian puts it in /usr/sbin, some distributions in /sbin, and a container
  # image may have neither -- /bin/false is the universally present fallback
  # and means the same thing here (nothing ever logs into this account).
  local candidate
  for candidate in /usr/sbin/nologin /sbin/nologin /bin/false; do
    if [ -x "$candidate" ]; then printf '%s' "$candidate"; return 0; fi
  done
  printf '/bin/false'
}

create_service_account() {
  if service_account_exists && service_group_exists; then
    note "service account ${SERVICE_ACCOUNT} already exists -- leaving it as it is"
    return
  fi
  note "creating the ${SERVICE_ACCOUNT} system account and group"
  if ! service_group_exists; then
    groupadd --system "$SERVICE_GROUP"
  fi
  if ! service_account_exists; then
    # --system picks an id below UID_MIN (login.defs), which is what keeps
    # this account out of the display manager's user list. No home directory
    # is created: the account exists to own files under SYSTEM_ROOT and run
    # one unit, and paths.data_dir() resolves that root rather than a ~/.
    useradd --system \
      --gid "$SERVICE_GROUP" \
      --home-dir "$SYSTEM_ROOT" \
      --no-create-home \
      --shell "$(nologin_shell)" \
      --comment "PrivacyFence daemon" \
      "$SERVICE_ACCOUNT"
  fi
}

add_owner_to_service_group() {
  note "adding ${OWNER_USER} to the ${SERVICE_GROUP} group"
  usermod -aG "$SERVICE_GROUP" "$OWNER_USER"
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
    cp -a "${legacy}/." "$SYSTEM_ROOT/"
    rm -rf "$legacy"
  else
    # /home and /var are separate filesystems often enough that this cannot
    # assume a rename: mv falls back to a copy-then-delete across devices,
    # which is what we want, and is atomic where they do share one.
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
  "platform": "linux",
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

# ── systemd / XDG wiring ──────────────────────────────────────────────────────

render_template() {
  local template="$1" destination="$2"
  [ -f "$template" ] || die "missing template: ${template}"
  sed \
    -e "s|__DAEMON_EXECUTABLE__|${DAEMON_EXECUTABLE}|g" \
    -e "s|__COMPANION_EXECUTABLE__|${COMPANION_EXECUTABLE}|g" \
    -e "s|__SERVICE_ACCOUNT__|${SERVICE_ACCOUNT}|g" \
    -e "s|__SERVICE_GROUP__|${SERVICE_GROUP}|g" \
    -e "s|__SYSTEM_ROOT__|${SYSTEM_ROOT}|g" \
    "$template" > "$destination"
  chown root:root "$destination"
  chmod 644 "$destination"
}

stop_legacy_autostart() {
  # The .deb's own autostart entry. Renamed rather than deleted so `disable`
  # can put it back. XDG autostart only reads *.desktop, so the .disabled
  # suffix is enough to stop it while leaving the file where dpkg expects it.
  #
  # That file is a dpkg conffile, so a later `apt upgrade` sees it as
  # "removed by the local admin" and can put it back -- at which point the
  # next graphical login starts a second daemon as the logged-in user. That
  # one fails closed rather than doing damage (check_runtime_identity
  # refuses to run as the wrong account on a separated install), and
  # `status` reports it as STILL AUTOSTARTS with the reason. Re-running
  # `enable` moves it aside again. Making dpkg itself aware of the opt-in
  # would take a package trigger, which is more machinery than an opt-in
  # this size earns; the loud-and-recoverable failure is the trade.
  if [ -f "$LEGACY_AUTOSTART_PATH" ]; then
    note "disabling the daemon's XDG autostart entry (${LEGACY_AUTOSTART_PATH} -> .disabled)"
    mv "$LEGACY_AUTOSTART_PATH" "${LEGACY_AUTOSTART_PATH}.disabled"
  fi
  # The pip/pipx path's `--user` unit. Stopping it needs the owner's own
  # session bus, which exists only while they are logged in -- best-effort,
  # then move the unit file aside so it cannot come back at their next login
  # regardless.
  local user_unit="${OWNER_HOME}/.config/systemd/user/${LEGACY_USER_UNIT}"
  if [ -f "$user_unit" ]; then
    note "disabling the old --user unit (${user_unit} -> .disabled)"
    runuser -u "$OWNER_USER" -- env "XDG_RUNTIME_DIR=/run/user/${OWNER_UID}" \
      systemctl --user disable --now "$LEGACY_USER_UNIT" >/dev/null 2>&1 || true
    mv "$user_unit" "${user_unit}.disabled"
  fi
}

install_services() {
  note "installing ${DAEMON_UNIT_PATH}"
  render_template "${TEMPLATE_DIR}/${DAEMON_UNIT}.tmpl" "$DAEMON_UNIT_PATH"
  note "installing ${COMPANION_AUTOSTART_PATH}"
  mkdir -p "$(dirname "$COMPANION_AUTOSTART_PATH")"
  render_template "${TEMPLATE_DIR}/${COMPANION_AUTOSTART}.tmpl" "$COMPANION_AUTOSTART_PATH"
  systemctl daemon-reload
  systemctl enable --now "$DAEMON_UNIT"
}

uninstall_services() {
  systemctl disable --now "$DAEMON_UNIT" >/dev/null 2>&1 || true
  rm -f "$DAEMON_UNIT_PATH" "$COMPANION_AUTOSTART_PATH"
  systemctl daemon-reload || true
}

# ── Subcommands ───────────────────────────────────────────────────────────────

cmd_enable() {
  require_linux
  require_systemd
  require_root
  resolve_owner
  resolve_executables

  if [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ]; then
    note "already separated -- re-running to refresh the account, layout and units"
  fi

  stop_legacy_autostart
  systemctl disable --now "$DAEMON_UNIT" >/dev/null 2>&1 || true

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
  Daemon           systemctl status ${DAEMON_UNIT}
  Logs             journalctl -u ${DAEMON_UNIT} -f

  One thing left to do by hand: log ${OWNER_USER} out and back in. Group
  membership is evaluated when a session is created, so the session you are in
  right now still does not know it is in ${SERVICE_GROUP} -- which means the
  companion app and your MCP client cannot reach the handoff directory until
  you do. 'sudo $0 status' will tell you when it has taken.

  What this does and does not buy you is written down in
  docs/security-and-compliance.md's "Local-mode trust boundary" section. The
  short version: the agent can no longer rewrite your policy, forge a passkey
  or read the audit key -- and an agent that can get root still defeats all
  of it, because root defeats everything.
DONE
}

cmd_disable() {
  require_linux
  require_root
  resolve_owner

  [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ] || die "this install is not privilege-separated (no ${SYSTEM_ROOT}/${MARKER_NAME})"

  note "stopping and removing the system unit and the companion autostart entry"
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
    cp -a "${SYSTEM_ROOT}/." "$legacy/"
    rm -rf "$SYSTEM_ROOT"
  else
    note "moving ${SYSTEM_ROOT} back to ${legacy}"
    mv "$SYSTEM_ROOT" "$legacy"
  fi
  chown -R "${OWNER_USER}" "$legacy"
  chmod -R go-rwx "$legacy"
  chmod 700 "$legacy"

  if [ -f "${LEGACY_AUTOSTART_PATH}.disabled" ]; then
    note "restoring the daemon's XDG autostart entry"
    mv "${LEGACY_AUTOSTART_PATH}.disabled" "$LEGACY_AUTOSTART_PATH"
  fi
  local user_unit="${OWNER_HOME}/.config/systemd/user/${LEGACY_USER_UNIT}"
  if [ -f "${user_unit}.disabled" ]; then
    note "restoring the old --user unit"
    mv "${user_unit}.disabled" "$user_unit"
    runuser -u "$OWNER_USER" -- env "XDG_RUNTIME_DIR=/run/user/${OWNER_UID}" \
      systemctl --user enable --now "$LEGACY_USER_UNIT" >/dev/null 2>&1 || true
  fi

  cat <<DONE

✓ Privilege separation is off. Your data is back at ${legacy}, owned by
  ${OWNER_USER} again.

  The ${SERVICE_ACCOUNT} account and group are left in place on purpose -- they
  own nothing now, and keeping them means re-enabling doesn't have to pick a
  new uid. Remove them with:
    sudo userdel ${SERVICE_ACCOUNT}
    sudo groupdel ${SERVICE_GROUP}
DONE
}

resolve_owner_optional() {
  # status has to work on a machine where --user wasn't passed and SUDO_USER
  # isn't set (run without sudo, which status deliberately allows), so it
  # can't use resolve_owner()'s own die().
  OWNER_USER="${OWNER_USER:-${SUDO_USER:-}}"
  [ -n "$OWNER_USER" ] || return 0
  OWNER_UID="$(id -u "$OWNER_USER" 2>/dev/null || true)"
  OWNER_HOME="$(getent passwd "$OWNER_USER" 2>/dev/null | cut -d: -f6)"
}

cmd_status() {
  require_linux
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
    actual="$(stat -c '%a' "$path" 2>/dev/null || true)"
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
  authority_owner="$(stat -c '%U' "${SYSTEM_ROOT}/authority" 2>/dev/null || true)"
  if [ "$authority_owner" != "$SERVICE_ACCOUNT" ]; then
    echo "  NOT SEPARATED    ${SYSTEM_ROOT}/authority is owned by '${authority_owner}', not ${SERVICE_ACCOUNT}"
    problems=1
  fi

  if [ -n "$OWNER_USER" ]; then
    if getent group "$SERVICE_GROUP" | cut -d: -f4 | tr ',' '\n' | grep -qx "$OWNER_USER"; then
      echo "  ok               ${OWNER_USER} is a member of ${SERVICE_GROUP}"
    else
      echo "  NOT A MEMBER     ${OWNER_USER} is not in ${SERVICE_GROUP} -- the companion cannot reach the daemon"
      problems=1
    fi
    # Distinct from the check above: membership can be recorded in /etc/group
    # and still absent from an already-running login session, which is exactly
    # the "log out and back in" case enable prints about.
    if id -Gn "$OWNER_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$SERVICE_GROUP"; then
      echo "  ok               ${OWNER_USER}'s login session has picked that membership up"
    else
      echo "  PENDING LOGOUT   ${OWNER_USER}'s current session predates the group change -- log out and back in"
      problems=1
    fi
  fi

  if [ -f "$LEGACY_AUTOSTART_PATH" ]; then
    echo "  STILL AUTOSTARTS ${LEGACY_AUTOSTART_PATH} would start a second daemon in your own session"
    problems=1
  fi

  [ -f "$COMPANION_AUTOSTART_PATH" ] \
    && echo "  ok               ${COMPANION_AUTOSTART_PATH} autostarts the companion channel" \
    || { echo "  NOT INSTALLED    ${COMPANION_AUTOSTART_PATH} -- connector OAuth cannot open a browser"; problems=1; }

  if [ "$(systemctl is-active "$DAEMON_UNIT" 2>/dev/null || true)" = "active" ]; then
    echo "  ok               ${DAEMON_UNIT} is active"
  else
    echo "  NOT RUNNING      ${DAEMON_UNIT} (systemctl status ${DAEMON_UNIT})"
    problems=1
  fi

  [ "$problems" = "0" ] || return 1
}

# ── Argument parsing ──────────────────────────────────────────────────────────

[ $# -ge 1 ] || usage
COMMAND="$1"; shift
while [ $# -gt 0 ]; do
  case "$1" in
    --user) OWNER_USER="${2:-}"; shift 2 ;;
    --daemon-exec) DAEMON_EXECUTABLE="${2:-}"; shift 2 ;;
    --companion-exec) COMPANION_EXECUTABLE="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$COMMAND" in
  enable) cmd_enable ;;
  disable) cmd_disable ;;
  status) cmd_status ;;
  *) usage ;;
esac
