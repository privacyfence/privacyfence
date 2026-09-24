#!/usr/bin/env bash
# Runs the PrivacyFence daemon under its own Linux account, and takes that
# back down again on uninstall.
#
# Without this, the daemon and the AI agent it exists to govern are the same
# OS user, which is the root cause of all four weaknesses issue #428 describes:
# the agent can read the session-minting credential, rewrite the always-allow
# rules and PII policy that decide what it is allowed to do, forge a WebAuthn
# credential into the store a local passkey would be checked against, and read
# the audit log's HMAC key. One change closes all four -- a dedicated account
# owning those files -- and this script is that change.
#
#   sudo ./scripts/linux_privilege_separation.sh enable      # from a checkout
#   sudo privacyfence-privilege-separation enable            # from the .deb
#   ... status
#   ... uninstall [--purge]
#
# The .deb installs this same file as /usr/sbin/privacyfence-privilege-
# separation, with the templates it renders under /usr/share/privacyfence/ --
# see TEMPLATE_DIR below. Both invocations do exactly the same thing.
#
# What `enable` does, in order:
#   1. creates the privacyfence system user and group;
#   2. adds you to that group, so the companion app can still reach the daemon;
#   3. creates /var/lib/privacyfence and owns it to that account --
#      authority/ at 0700, handoff/ at 3770, the root at 0711;
#   4. writes the marker file every PrivacyFence process reads to agree on that
#      layout (src/privacyfence/privilege_separation.py);
#   5. installs a *system* systemd unit for the daemon and an XDG autostart
#      entry for the companion's control channel -- ADR 0002's "startup
#      wiring inverts", on this platform.
#
# Nothing moves data out of a home directory (ADR 0041: only the current
# install layout is supported), and nothing ever moves it back into one.
#
# Step 2 is the only one that needs to know *which human* this install is for,
# and ADR 0003 decision 3 splits it out for that reason: an MDM push, an
# unattended `apt` upgrade or a plain root shell resolves no owner account.
# `enable` with no resolvable owner runs everything root can do alone and
# records the group membership as pending; `enable --for-user <name>` closes
# that half later, idempotently, and is what the companion app runs by itself
# at the first real login session.
#
# `uninstall` is the other direction (ADR 0042): it stops and unregisters the
# daemon unit and the companion autostart entry, and leaves the data, the
# marker and the service account and group in place, so a reinstall picks the
# data up where it was. `uninstall --purge` additionally deletes all three.
# The .deb's prerm runs `uninstall` on `remove`; its postrm does the purge
# steps itself on `purge`, because by then this file is already gone (see
# debian/postrm).
#
# Run for you by the .deb's postinst on every install and upgrade -- which,
# since ADR 0003 decision 5, calls the two halves of `enable` separately
# because they have two different failure policies (see debian/postinst, which
# spells both out):
#
#   enable --machine-only     the machine half, and only it. Skips owner
#                             resolution outright rather than taking whatever
#                             $SUDO_USER happens to say, so the group add is
#                             genuinely deferred to the per-user half instead
#                             of riding along and taking the install down with
#                             it when it fails. Loud: no --auto, and postinst
#                             lets its failure fail the package install.
#   enable --auto --for-user  the per-user half, deferrable. `--auto` is the
#                             same `enable`, made safe to run unattended:
#                             anywhere it would otherwise die() on something a
#                             human would resolve interactively (no installed
#                             executables, an unsupported init system, an
#                             unresolvable account), it instead logs why and
#                             exits 0 rather than failing the package configure
#                             step it's called from. A human running this by
#                             hand never wants that silent behavior, which is
#                             why --auto isn't the default. It composes with
#                             --for-user rather than selecting a command, which
#                             is what lets the postinst pair it with exactly
#                             the half that is still allowed to defer.
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

DAEMON_UNIT="privacyfence-daemon.service"
DAEMON_UNIT_PATH="/etc/systemd/system/${DAEMON_UNIT}"
COMPANION_AUTOSTART="privacyfence-companion.desktop"
COMPANION_AUTOSTART_PATH="/etc/xdg/autostart/${COMPANION_AUTOSTART}"

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
OWNER_HOME=""
DAEMON_EXECUTABLE=""
COMPANION_EXECUTABLE=""

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '→ %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

usage() {
  cat >&2 <<USAGE
usage: sudo $0 {enable|uninstall|status} [options]
       sudo $0 daemon {status|start|stop|restart|ensure-running}

  --user <name>       the human account that owns this install
                      (default: \$SUDO_USER, i.e. whoever ran sudo). With none
                      resolvable, enable still separates the machine and
                      leaves the group membership pending -- see --for-user.
  --for-user <name>   enable only: run *just* the per-user half against an
                      install the machine half has already separated -- add
                      <name> to ${SERVICE_GROUP}. Idempotent, and what the companion app
                      runs when it finds that membership still pending. Works
                      for any number of accounts on the same machine, not
                      just this install's first (recorded) owner -- each gets
                      its own isolated PrivacyFence identity, never merged
                      with anyone else's (docs/adr/0008-one-principal-per-os-
                      user.md).
  --machine-only      enable only: run *just* the machine half -- everything
                      root can do with no human in sight -- and leave the
                      group membership pending even if \$SUDO_USER would have
                      resolved. The other side of --for-user, and what the
                      .deb's postinst runs. Refuses --user/--for-user.
  --daemon-exec <p>   run this instead of ${DEFAULT_DAEMON_EXECUTABLE}
  --companion-exec <p> run this instead of ${DEFAULT_COMPANION_EXECUTABLE}
                      (both together let a source/venv install be separated:
                       --daemon-exec .venv/bin/privacyfence-app
                       --companion-exec .venv/bin/privacyfence-companion)
  --auto              enable only: never die(), never prompt -- log and exit 0
                      instead of failing when this can't safely tell who owns
                      the install or find the daemon. For unattended callers
                      (the .deb's postinst); a human should not pass this.
  --purge             uninstall only: also delete ${SYSTEM_ROOT} (every
                      connector token, the policy and the audit log), the
                      marker, and the ${SERVICE_ACCOUNT} account and group.
                      Without it, uninstall leaves all of those in place so a
                      reinstall picks the data up again.

  daemon status           print a small unprivileged key=value report of the
                          unit's own state (ActiveState/SubState/Result/
                          ExecMainStatus/MainPID via systemctl show). Needs
                          no sudo.
  daemon start            alias for ensure-running.
  daemon ensure-running   make sure ${DAEMON_UNIT} is enabled and active,
                          clearing a prior failed state first so systemd's
                          own start-limit throttling doesn't get in the way.
                          What the companion app's Start button runs
                          elevated.
  daemon restart          systemctl restart if active, otherwise
                          ensure-running.
  daemon stop             systemctl stop, and wait for it to actually go
                          inactive.
USAGE
  exit 2
}

AUTO=0
FOR_USER_ONLY=0
MACHINE_ONLY=0
PURGE=0
# The sub-verb of `daemon {status|start|stop|restart|ensure-running}`,
# pulled off the argument list before the generic option-parsing loop below
# ever sees it -- see the "── Argument parsing ──" section at the bottom for
# why that has to happen first.
DAEMON_SUBCOMMAND=""

require_linux() {
  [ "$(uname -s)" = "Linux" ] || die "this script is Linux-only (macOS is scripts/macos_privilege_separation.sh; Windows is scripts/windows_privilege_separation.ps1)"
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
  id -u "$OWNER_USER" >/dev/null 2>&1 || die "no such user: ${OWNER_USER}"
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

# What `status` reads back to tell the pending state apart from "not
# separated" (ADR 0003 decision 3).
marker_owner_user() {
  # Every writer of this file -- this script, macOS' own, the .pkg's
  # postinstall, and PowerShell's ConvertTo-Json -- emits one key per line,
  # so a line-oriented read is enough and keeps `status` free of a JSON
  # parser it would otherwise have to carry for one string.
  sed -n 's/.*"owner_user"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${SYSTEM_ROOT}/${MARKER_NAME}" 2>/dev/null | head -n 1
}

# What `status` reports as the data directory of an install that is not
# separated: paths.data_dir()'s default for a pip/pipx or source install.
unseparated_data_dir() { printf '%s/.privacyfence' "$OWNER_HOME"; }

apply_layout() {
  note "re-owning ${SYSTEM_ROOT} to ${SERVICE_ACCOUNT}:${SERVICE_GROUP}"
  mkdir -p "${SYSTEM_ROOT}/authority" "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" "${SYSTEM_ROOT}/logs"
  # Everything except sockets (ADR 0029). A socket belongs to the process that
  # bound it, at the mode it chose: re-owning a live companion's
  # handoff/companion.sock on a re-run -- every .deb upgrade is one -- leaves
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
  # The daemon writes these at the right mode itself
  # (privilege_separation.ensure_handoff_file_mode()); re-asserting it here
  # means a correct install doesn't depend on that fix-up, and repairs one a
  # human has chmod-ed by hand.
  find "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" -type f -exec chmod "$HANDOFF_FILE_MODE" {} +
}

write_marker() {
  local marker="${SYSTEM_ROOT}/${MARKER_NAME}" recorded_owner=""
  # An owner already recorded in this file is kept, whoever this run resolved.
  # `owner_user` is what privilege_separation.owner_uid() maps to the install's
  # original principal (ADR 0008), so it names the first human only and is
  # never rewritten: `enable --for-user <second>` adds that account alongside
  # the owner and must not hand it the owner's data. The same rule is what
  # stops the machine half clearing it (ADR 0003 decisions 3 and 5): that runs
  # with no owner resolved on every upgrade of an install whose per-user half
  # is already closed -- an unattended `apt` upgrade, and every `dpkg -i` now
  # that the .deb's postinst runs it as --machine-only. `uninstall --purge` is
  # the one thing that removes the owner, by removing this file entirely. See
  # docs/adr/0043-the-recorded-owner-is-never-rewritten.md.
  if [ -f "$marker" ]; then
    recorded_owner="$(marker_owner_user)"
    [ -z "$recorded_owner" ] || note "keeping the owner already recorded in ${marker}: ${recorded_owner}"
  fi
  [ -n "$recorded_owner" ] || recorded_owner="$OWNER_USER"
  note "writing ${marker}"
  cat > "$marker" <<MARKER
{
  "version": ${MARKER_VERSION},
  "platform": "linux",
  "service_account": "${SERVICE_ACCOUNT}",
  "service_group": "${SERVICE_GROUP}",
  "owner_user": "${recorded_owner}",
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

install_services() {
  note "installing ${DAEMON_UNIT_PATH}"
  render_template "${TEMPLATE_DIR}/${DAEMON_UNIT}.tmpl" "$DAEMON_UNIT_PATH"
  note "installing ${COMPANION_AUTOSTART_PATH}"
  mkdir -p "$(dirname "$COMPANION_AUTOSTART_PATH")"
  render_template "${TEMPLATE_DIR}/${COMPANION_AUTOSTART}.tmpl" "$COMPANION_AUTOSTART_PATH"
  systemctl daemon-reload
  systemctl enable --now "$DAEMON_UNIT"
}

daemon_unit_is_active() {
  command -v systemctl >/dev/null 2>&1 || return 1
  [ "$(systemctl is-active "$DAEMON_UNIT" 2>/dev/null || true)" = "active" ]
}

# Best-effort by design: `uninstall` runs from the .deb's prerm, which must
# never fail a removal, and has to work on a machine that is half-way through
# being taken apart (systemd not PID 1 in a container, a unit already gone).
uninstall_services() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$DAEMON_UNIT" >/dev/null 2>&1 || true
  fi
  rm -f "$DAEMON_UNIT_PATH" "$COMPANION_AUTOSTART_PATH"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
}

# `uninstall --purge`'s half (ADR 0042). debian/postrm's `purge` case runs
# the same three steps on its own, because this file is already deleted by
# the time dpkg calls it -- tests/unit/test_privilege_separation.py holds the
# two to the same paths and names. userdel before groupdel: a group cannot be
# deleted while it is still some account's primary group.
purge_data_and_account() {
  note "deleting ${SYSTEM_ROOT} (connector tokens, policy, audit log, marker)"
  rm -rf "$SYSTEM_ROOT"
  if service_account_exists; then
    note "deleting the ${SERVICE_ACCOUNT} account"
    userdel "$SERVICE_ACCOUNT" >/dev/null 2>&1 || warn "could not delete the ${SERVICE_ACCOUNT} account"
  fi
  if service_group_exists; then
    note "deleting the ${SERVICE_GROUP} group"
    groupdel "$SERVICE_GROUP" >/dev/null 2>&1 || warn "could not delete the ${SERVICE_GROUP} group"
  fi
}

# ── Daemon manager (#428 Phase 2) ─────────────────────────────────────────────
#
# `daemon {status|start|stop|restart|ensure-running}` is what the companion
# app's tray menu runs -- `status` unprivileged, on every poll, and
# `start`/`stop`/`restart` elevated (src/privacyfence/service_control.py),
# only when a human clicks the corresponding menu item. It is also what
# `cmd_enable`'s own install_services() already achieves via `systemctl
# enable --now`, so unlike macOS's launchd there is no separate bootout/
# bootstrap race to guard against here -- systemd itself starts the unit as
# the account the unit file names, with no window for a script-observable
# wrong-owner race the way launchd's UserName resolution has. What still
# needs doing by hand is correct sequencing and a clear exit code.
DAEMON_STOP_TIMEOUT=15

cmd_daemon_status() {
  # Unprivileged and meant to answer immediately: `systemctl show` always
  # exits 0 and prints every requested property, even for a unit that has
  # never existed (every value comes back empty), so there is no "not
  # loaded" failure mode to special-case the way launchctl has on macOS --
  # only "systemctl itself could not be run at all" is worth dying on.
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found -- cannot read ${DAEMON_UNIT}'s state"
  local output active_state="" sub_state="" result="" exec_main_status="" main_pid=""
  output="$(systemctl show "$DAEMON_UNIT" -p ActiveState,SubState,Result,ExecMainStatus,MainPID 2>/dev/null)" \
    || die "could not run systemctl show ${DAEMON_UNIT}"
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      ActiveState) active_state="$value" ;;
      SubState) sub_state="$value" ;;
      Result) result="$value" ;;
      ExecMainStatus) exec_main_status="$value" ;;
      MainPID) main_pid="$value" ;;
    esac
  done <<<"$output"
  local control_socket_present=false
  [ -e "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}/control.sock" ] && control_socket_present=true
  printf 'active_state=%s\n' "$active_state"
  printf 'sub_state=%s\n' "$sub_state"
  printf 'result=%s\n' "$result"
  printf 'exec_main_status=%s\n' "$exec_main_status"
  printf 'pid=%s\n' "$([ "$main_pid" != "0" ] && printf '%s' "$main_pid")"
  printf 'control_socket_present=%s\n' "$control_socket_present"
  return 0
}

cmd_daemon_ensure_running() {
  if daemon_unit_is_active; then
    note "${DAEMON_UNIT} is already active"
    return 0
  fi
  # A unit systemd has already given up on (Result=exit-code from a prior
  # crash) is subject to systemd's own start-limit throttling -- repeated
  # `systemctl start` calls inside its burst window silently do nothing.
  # `reset-failed` clears that bookkeeping before every start attempt here,
  # not just the ones that already look failed, since it is a harmless no-op
  # against a unit that isn't in that state.
  systemctl reset-failed "$DAEMON_UNIT" >/dev/null 2>&1 || true
  note "starting ${DAEMON_UNIT}"
  systemctl enable --now "$DAEMON_UNIT" || die "systemctl enable --now ${DAEMON_UNIT} failed"
  daemon_unit_is_active || die "${DAEMON_UNIT} did not become active -- check 'systemctl status ${DAEMON_UNIT}' and 'journalctl -u ${DAEMON_UNIT}'"
  note "${DAEMON_UNIT} is active"
}

cmd_daemon_restart() {
  if daemon_unit_is_active; then
    note "restarting ${DAEMON_UNIT}"
    systemctl restart "$DAEMON_UNIT" || die "systemctl restart ${DAEMON_UNIT} failed"
    daemon_unit_is_active || die "${DAEMON_UNIT} did not come back up after restart -- check 'systemctl status ${DAEMON_UNIT}'"
    return 0
  fi
  cmd_daemon_ensure_running
}

cmd_daemon_stop() {
  systemctl stop "$DAEMON_UNIT" 2>/dev/null || true
  local deadline=$((SECONDS + DAEMON_STOP_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    daemon_unit_is_active || { note "${DAEMON_UNIT} stopped"; return 0; }
    sleep 0.3
  done
  die "${DAEMON_UNIT} is still active ${DAEMON_STOP_TIMEOUT}s after stop -- 'systemctl status ${DAEMON_UNIT}' to see why"
}

cmd_daemon() {
  local subcommand="$1"
  case "$subcommand" in
    status)
      require_linux
      cmd_daemon_status
      ;;
    start | ensure-running)
      require_linux
      require_systemd
      require_root
      cmd_daemon_ensure_running
      ;;
    restart)
      require_linux
      require_systemd
      require_root
      cmd_daemon_restart
      ;;
    stop)
      require_linux
      require_systemd
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
  require_linux
  require_systemd
  require_root
  # Deliberately the optional resolution, not resolve_owner()'s die() (ADR
  # 0003 decision 3): with no owner account to resolve this still separates
  # the machine completely, and records the one step that genuinely needs a
  # human -- the group membership -- as pending rather than abandoning the
  # whole install to the unseparated layout the way it used to.
  #
  # --machine-only goes one step further and declines to resolve an owner it
  # *could* have (ADR 0003 decision 5). That is not the same thing as there
  # being none: the .deb's postinst runs this half with $SUDO_USER sitting
  # right there in its environment, and lets its failure fail the package
  # install. Letting the group add ride along on that call would put the one
  # step that is allowed to defer inside the one call that is not.
  if [ "$MACHINE_ONLY" = "1" ]; then
    note "machine half only -- leaving the ${SERVICE_GROUP} membership to 'enable --for-user'"
  else
    resolve_owner_optional
  fi
  resolve_executables

  if [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ]; then
    note "already separated -- re-running to refresh the account, layout and units"
  fi

  systemctl disable --now "$DAEMON_UNIT" >/dev/null 2>&1 || true

  create_service_account
  if [ -n "$OWNER_USER" ]; then
    add_owner_to_service_group
  else
    note "no owner account resolved -- leaving the ${SERVICE_GROUP} membership pending"
  fi
  apply_layout
  write_marker
  install_services

  cat <<DONE

✓ PrivacyFence now runs as ${SERVICE_ACCOUNT}.

  Data directory   ${SYSTEM_ROOT}
  Human authority  ${SYSTEM_ROOT}/authority   (0700, ${SERVICE_ACCOUNT} only)
  Shared handoff   ${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}   (3770, ${SERVICE_GROUP} group)
  Daemon           systemctl status ${DAEMON_UNIT}
  Logs             journalctl -u ${DAEMON_UNIT} -f
DONE

  if [ -n "$OWNER_USER" ]; then
    cat <<DONE

  One thing left to do by hand: log ${OWNER_USER} out and back in. Group
  membership is evaluated when a session is created, so the session you are in
  right now still does not know it is in ${SERVICE_GROUP} -- which means the
  companion app and your MCP client cannot reach the handoff directory until
  you do. 'sudo $0 status' will tell you when it has taken.
DONE
  else
    cat <<DONE

  Nobody is in ${SERVICE_GROUP} yet: this ran with no human account to add,
  which is the ordinary case for an MDM push or an unattended upgrade. The
  install is separated regardless -- what is pending is one re-runnable step,
  which the companion app takes by itself at the first real login session, or
  which you can take now:

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
  # ADR 0003 decision 3's per-user half, on its own: the one step of `enable`
  # that needs to know which human this install is for. Runs against an
  # install the machine half has already separated, and re-runs harmlessly
  # against one that is already complete -- `usermod -aG` is idempotent, and
  # the layout and marker are rewritten to the same values.
  #
  # Any number of accounts can be added this way. Each gets its own isolated
  # PrivacyFence identity (ADR 0008): the daemon creates
  # ${SYSTEM_ROOT}/users/os-<uid>/ on that account's first connection
  # (paths.user_dir()), so there is nothing to provision for it here.
  #
  # Deliberately no resolve_executables()/require_systemd(): this installs no
  # services and starts nothing, so a source install whose executables have
  # moved can still have a second user added to the group.
  require_linux
  require_root
  resolve_owner

  [ -f "${SYSTEM_ROOT}/${MARKER_NAME}" ] \
    || die "this install is not privilege-separated yet -- run 'sudo $0 enable' first"

  local recorded_owner
  recorded_owner="$(marker_owner_user)"
  if [ -n "$recorded_owner" ] && [ "$recorded_owner" != "$OWNER_USER" ]; then
    note "adding '${OWNER_USER}' alongside this install's existing owner '${recorded_owner}' -- each gets its own isolated PrivacyFence identity (docs/adr/0008-one-principal-per-os-user.md)"
  fi

  add_owner_to_service_group
  # The layout is re-asserted rather than assumed. The marker records this
  # account as the owner only if it has none yet; write_marker never replaces
  # one already recorded.
  apply_layout
  write_marker

  cat <<DONE

✓ ${OWNER_USER} is now a member of ${SERVICE_GROUP}.

  One thing left to do by hand: log ${OWNER_USER} out and back in. Group
  membership is evaluated when a session is created, so the session you are in
  right now still does not know it is in ${SERVICE_GROUP} -- which means the
  companion app and your MCP client cannot reach the handoff directory until
  you do. 'sudo $0 status' will tell you when it has taken.
DONE
}

cmd_uninstall() {
  # ADR 0042. Stops and unregisters what `enable` installed, and nothing
  # else unless --purge is given: the data, the marker and the service
  # account and group stay, so a reinstall's `enable` finds the same data
  # directory, owned by the same account, and the daemon carries on where it
  # left off. Nothing is ever moved into a home directory.
  #
  # No marker check: this has to work on an install that never got as far as
  # writing one (a failed configure), and on one whose units are already
  # gone. Every step is a no-op when there is nothing to undo.
  require_linux
  require_root

  note "stopping ${DAEMON_UNIT} and removing it and the companion autostart entry"
  uninstall_services

  if [ "$PURGE" = "1" ]; then
    purge_data_and_account
    cat <<DONE

✓ PrivacyFence's service, its data and the ${SERVICE_ACCOUNT} account are gone.
DONE
    return 0
  fi

  cat <<DONE

✓ PrivacyFence's service is stopped and unregistered.

  Your data is still in ${SYSTEM_ROOT}, owned by ${SERVICE_ACCOUNT}, and a
  reinstall picks it up again. To delete it -- every connector token, the
  policy and the audit log -- together with the ${SERVICE_ACCOUNT} account and
  group:
    sudo $0 uninstall --purge
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
  OWNER_HOME="$(getent passwd "$OWNER_USER" 2>/dev/null | cut -d: -f6)"
}

cmd_status() {
  require_linux
  resolve_owner_optional

  if [ ! -f "${SYSTEM_ROOT}/${MARKER_NAME}" ]; then
    echo "privilege separation: OFF"
    if [ -n "$OWNER_HOME" ]; then echo "  data directory: $(unseparated_data_dir)"; fi
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

  [ -f "$COMPANION_AUTOSTART_PATH" ] \
    && echo "  ok               ${COMPANION_AUTOSTART_PATH} autostarts the companion channel" \
    || { echo "  NOT INSTALLED    ${COMPANION_AUTOSTART_PATH} -- connector OAuth cannot open a browser"; problems=1; }

  if [ "$(systemctl is-active "$DAEMON_UNIT" 2>/dev/null || true)" = "active" ]; then
    echo "  ok               ${DAEMON_UNIT} is active"
  else
    echo "  NOT RUNNING      ${DAEMON_UNIT} (systemctl status ${DAEMON_UNIT})"
    problems=1
  fi

  # ADR 0008: the recorded `owner_user` above is only this install's *first*
  # principal, not its only one -- `enable --for-user` for a second account
  # gives it its own users/os-<uid>/ instead of touching the owner's data at
  # all (see cmd_enable_for_user()), so it never shows up as an
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
      other_name="$(getent passwd "$other_uid" 2>/dev/null | cut -d: -f1)"
      if [ -n "$other_name" ]; then
        echo "    ${other_name} (os-${other_uid})"
      else
        echo "    os-${other_uid} (no matching passwd entry -- account since removed?)"
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
    --machine-only) MACHINE_ONLY=1; shift ;;
    --daemon-exec) DAEMON_EXECUTABLE="${2:-}"; shift 2 ;;
    --companion-exec) COMPANION_EXECUTABLE="${2:-}"; shift 2 ;;
    --auto) AUTO=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) usage ;;
    *) die "unknown option: $1" ;;
  esac
done

# The two halves of ADR 0003 decision 3 are complements, not modifiers of one
# another: naming a user and then asking for the half that deliberately has no
# user is a caller that means one of the two and typed both. Refuse rather
# than silently pick, since picking wrong here is the difference between an
# install whose owner is in ${SERVICE_GROUP} and one whose owner is not.
if [ "$MACHINE_ONLY" = "1" ] && { [ "$FOR_USER_ONLY" = "1" ] || [ -n "$OWNER_USER" ]; }; then
  die "--machine-only is the half that has no owner -- it cannot be combined with --user/--for-user"
fi

if [ "$PURGE" = "1" ] && [ "$COMMAND" != "uninstall" ]; then
  die "--purge only applies to uninstall"
fi

case "$COMMAND" in
  enable)
    # --for-user/--machine-only select which half runs; --auto is orthogonal
    # and wraps whichever one it is. That is what lets the .deb's postinst
    # spell ADR 0003 decision 5 as two calls with two different failure
    # policies -- `enable --machine-only` unconditional and loud, `enable
    # --auto --for-user` still allowed to defer -- rather than one call whose
    # halves it cannot tell apart.
    if [ "$FOR_USER_ONLY" = "1" ]; then
      ENABLE_COMMAND=cmd_enable_for_user
    else
      ENABLE_COMMAND=cmd_enable
    fi
    if [ "$AUTO" = "1" ]; then
      note "auto-enabling privilege separation (#428 D1, 4.1) -- see debian/postinst"
      # Run in a subshell: die() calls exit, and under set -euo pipefail an
      # exit from a *direct* call would take this whole process down with it
      # -- including the postinst that's calling us. A subshell's exit only
      # ends the subshell, and testing it in `if` is exempt from errexit, so
      # a resolve_owner()/resolve_executables()/cmd_enable failure lands here
      # instead of failing the package configure step.
      if ! ( "$ENABLE_COMMAND" ); then
        warn "auto-enable did not run to completion."
        warn "rerun without --auto to see why, or once it's clear: sudo $0 enable"
      fi
    else
      "$ENABLE_COMMAND"
    fi
    ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  daemon) cmd_daemon "$DAEMON_SUBCOMMAND" ;;
  *) usage ;;
esac
