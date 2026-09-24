# shellcheck shell=bash
# Sourced by scripts/build_dmg.sh and scripts/build_pkg.sh -- not run directly.
#
# sign_with_timestamp_retry <file> <command> [args...]
#
# Runs a signing command (codesign / productsign) that talks to Apple's secure
# timestamp server, and re-runs it with backoff when -- and only when -- it
# failed because that server was unreachable. build.yml run 36060781689 went red
# on exactly this: `codesign` reported "The timestamp service is not available."
# for the app bundle, and on a tag push that one outage blocks the whole release
# (publish-pypi.yml's wait_for_build gates on build.yml). The timestamp is not
# optional -- notarization rejects a Developer ID signature without a secure
# one -- so the fix is to wait the outage out, never to sign without it.
#
# A failure whose output matches none of the transient patterns below (a bad
# identity, an entitlement problem, an invalid binary, a locked keychain) is
# returned on the first attempt with the command's own exit status: retrying a
# real signing failure only delays the red job by a minute and a half.
#
# Re-running is safe for both callers: build_dmg.sh's codesign passes --force,
# so a retry replaces whatever subcomponent signatures the failed attempt had
# already written, and build_pkg.sh removes productsign's output before every
# attempt, re-signing the same unchanged unsigned input.
#
# SIGN_RETRY_DELAYS (seconds, space-separated) sets the backoff; one delay per
# retry, so the default "10 20 40" means at most four attempts. Tests set it to
# zeros.

# Transient, and nothing else. The first is the codesign/productsign text for
# the timestamp server being unreachable; the rest are the NSURLErrorDomain
# descriptions the same fetch reports when the network under it fails, which
# name no property of the thing being signed.
_SIGN_TRANSIENT_PATTERN='timestamp service is not available|The request timed out|The network connection was lost|A server with the specified hostname could not be found|The Internet connection appears to be offline|Could not connect to the server'

sign_with_timestamp_retry() {
  local file="$1"
  shift
  local -a delays
  read -r -a delays <<< "${SIGN_RETRY_DELAYS:-10 20 40}"
  local max_attempts=$(( ${#delays[@]} + 1 ))
  local attempt=1 rc=0 output transient

  while :; do
    # Output is captured so the retry decision can read it, then replayed in
    # full: the job log should look exactly as it would without this wrapper.
    output="$("$@" 2>&1)" && rc=0 || rc=$?
    [ -n "$output" ] && printf '%s\n' "$output"
    [ "$rc" -eq 0 ] && return 0

    transient="$(printf '%s\n' "$output" | grep -m1 -i -E "$_SIGN_TRANSIENT_PATTERN" || true)"
    if [ -z "$transient" ] || [ "$attempt" -ge "$max_attempts" ]; then
      return "$rc"
    fi

    local delay="${delays[$(( attempt - 1 ))]}"
    echo "⚠ signing ${file}: attempt ${attempt}/${max_attempts} failed (exit ${rc}): ${transient} -- retrying in ${delay}s" >&2
    sleep "$delay"
    attempt=$(( attempt + 1 ))
  done
}
