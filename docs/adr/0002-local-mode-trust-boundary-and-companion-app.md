# ADR 0002: the local-mode trust boundary, and a companion app to replace the agent as the sign-in channel

## Status

Accepted; implemented on all three desktop platforms. [#428](https://github.com/privacyfence/privacyfence/issues/428)
Phase 3 built the companion app this decides, and Phase 4 built the privilege separation behind it
— shipped opt-in on macOS (`scripts/macos_privilege_separation.sh`), Linux
(`scripts/linux_privilege_separation.sh`) and Windows
(`scripts/windows_privilege_separation.ps1`). #428 D1 (4.1) turns it on by default on macOS and
Linux, ahead of the original plan's 4.2 target and its "soak through a full release cycle first"
criterion — the `.deb`'s `postinst` on Linux, an admin-password prompt from the daemon's own first
startup on macOS (`privilege_separation.maybe_auto_enable_macos()`). Windows stays opt-in; the
manual command (`enable`/`disable`/`status`) is unchanged on every platform and remains how to opt
back out.
[#426](https://github.com/privacyfence/privacyfence/issues/426) builds the half that makes
it mean something, and stays gated on that platform's Phase 4 having landed *and soaked* — D1
turning the default on does not by itself satisfy that; see
[`platform-support.md`](../platform-support.md)'s "Known open items" for what a soak still needs
to show on a real machine. Supersedes [ADR 0001](0001-remove-macos-native-extra.md) in part — see
"Relationship to ADR 0001" below.

## Context

P10 left local mode headless. The daemon has no window, no menu bar item and no dock icon; every
human-facing surface it has is served by the embedded web app and reached through a browser. The
consequence, which was not the intent, is that **an MCP client is the only UI affordance local mode
has**, so every recovery path routes through it:

- `privacyfence_get_sign_in_link` (`web/mcp_tools.py`) mints a bootstrap link and hands it to the
  MCP client. The not-authorized page (`web/session_auth.py`) leads with exactly that.
- The expired-link copy names the agent as the intended recovery, not as a fallback.
- `privacyfence_status` was originally written to mint one because the model asked, with no human
  in the loop. That was removed before it shipped, leaving `privacyfence_get_sign_in_link` as the
  only tool that mints, and only when a human asks for it.

Those are three appearances of one architectural fact, not three independent choices. And the
credential being couriered is load-bearing: a bootstrap code yields a `pf_session` cookie, and on
`POST /api/approvals/{id}/decide` the CSRF token *is* that cookie's value — `routes_approvals.py`
renders it into the page straight from the `pf_session` cookie and `_csrf_matches` compares the
two — while the origin check next to it (`session_auth.check_origin`) reads a header the client
sets. Both are correct defenses against a hostile page in the user's browser. Neither constrains a
local process, which holds the cookie and writes its own headers.

Stated plainly: **in local mode the agent is the courier for the credential that authenticates to
the UI that checks the agent.**

`docs/security-and-compliance.md`'s "Local-mode trust boundary" section already says this about the
shipped implementation. That was the honesty half of the response. This ADR is the architecture
half: what the boundary *is*, what gets built to move it, and what that build is allowed to cost.

ADR 0001 removed the `macos-native` extra (`rumps` plus PyObjC/AppKit frameworks) on
dependency-surface grounds and closed with: *"A future native UI would require a new architecture
decision and dependency review rather than silently reusing an old extra."* This is that decision
and that review.

## Decision

### 1. The trust boundary in local mode is the OS user account

Not the browser, not the approval UI, not the bootstrap code's TTL. Every control on the
session-minting path — the short TTL, single-use consumption, the CSRF double-submit, the origin
check, idle and absolute session expiry — limits how long a *leaked* credential stays useful and
stops a hostile web page; none of them decides *who may mint one* on a machine where the daemon and
the agent run as the same user.

This is binding on design, not just on prose: **no local-mode feature may claim a guarantee that
rests on telling two processes apart while they run under the same uid.** Until #428 Phase 4 moves
the daemon off the user's account, a control that only a cooperative local process would respect is
documented as a workflow control, not as a security boundary.

### 2. A companion app returns, as the sign-in channel

A small process in the user's desktop session, whose entire job is to get a human into the web UI
without a credential transiting the model.

Scope, deliberately minimal:

- a tray / menu-bar item with **Open Approvals**, **Open Settings**, and **Quit**;
- it holds the client end of #428 Phase 2's control channel (unix domain socket / named pipe), so
  after Phase 4 it is how a session gets minted at all;
- it opens URLs in the user's browser on behalf of the daemon (see constraint 5 below).

That is the whole product surface. It renders no PrivacyFence content of its own.

### 3. It is not a return to the native approval UI

The web app stays the single implementation of approvals and settings, for ADR 0001's original
reason: one implementation, shared across three platforms, is what keeps approval behavior
consistent and auditable. The companion app opens a browser; it does not draw approval cards,
settings forms, or notification popups, and it is never given approval *content* — the control
channel carries "mint a session", "open this URL", not pending-approval data.

### 4. Dependency budget

ADR 0001's objection was to carrying an AppKit dependency for a UI that did not exist. A used,
load-bearing affordance is a different trade — but it is a trade, so it gets an explicit budget
rather than an open account:

- **The companion is a second entry point of the application that is already packaged**, not a new
  runtime, toolchain, artifact or signing path. Same PyInstaller bundle, same signature, same
  notarization; the control-channel client and the discovery-file reading are the daemon's own
  Python, not a reimplementation in a second language.
- **At most one new direct runtime dependency**, `pystray` (with `Pillow` for the icon image it
  requires), declared platform-conditionally for `sys_platform == "darwin"` and
  `sys_platform == "win32"`, and imported only from the companion entry point.
- **On macOS that transitively re-admits `pyobjc-framework-Cocoa`**, for the `NSStatusItem` a
  menu-bar item is. This is the part of ADR 0001 this ADR supersedes, and it is narrow: AppKit
  reaches the companion's status-item code and nothing else. `rumps` does not come back.
- **On Linux the budget is zero new dependencies, and there is no tray.** pystray's Linux backends
  need PyGObject plus GI typelibs (fragile to bundle into the self-contained `.deb`) or `python-xlib`
  (X11 only, so not Wayland), and GNOME hides legacy tray icons without a user-installed extension.
  A dependency that heavy for an affordance that may silently not appear is a bad trade. Linux gets
  the same companion process with the same menu items expressed as an XDG launcher instead: the
  existing `resources/linux/privacyfence.desktop` entry stops being `NoDisplay=true` and becomes a
  real application-menu entry that opens `/approvals`, with `Desktop Action` entries for Settings
  and Quit.
- **No GUI toolkit at any point.** Qt, GTK, wx and friends are out of budget by construction —
  see decision 3 for why nothing needs one.
- **The daemon's own dependency set does not change.** Nothing in this ADR is importable from
  daemon code, on any platform.

### 5. The companion owns browser-opening for connector OAuth

`oauth_loopback.run_browser_oauth()` calls `webbrowser.open()` on the daemon's machine for the
Slack, Salesforce and Atlassian flows. Once #428 Phase 4 makes the Windows daemon a service, it runs
in session 0 and cannot reach the user's desktop session to open anything. The companion therefore
takes over URL-opening, which makes it a hard prerequisite of Phase 4 on Windows rather than a
convenience.

Only the *opening* moves. The loopback redirect listener stays in the daemon: `127.0.0.1` is
machine-wide, not per-session, so a service-hosted daemon still receives the provider's redirect
from a browser running in the user's session, on the fixed port those three providers' allow-lists
require.

**Amended by #428 Phase 4 (B5c), 2026-09-16: on Windows this is what it always said it would be.**
B5c made the daemon a service, the service runs in session 0, and `webbrowser.open()` from it
reaches nothing — so `scripts/windows_privilege_separation.ps1` refuses to enable without the
companion executable present, and registers its Scheduled Task as part of `enable` rather than
leaving it opt-in. The prediction held; what B5b (below) found is that its *scope* was too narrow,
not that its reasoning was wrong.

**Amended by #428 Phase 4 (B5b), 2026-09-16: this is not a Windows-only requirement, and Linux hit
it first.** The reasoning above is right and its scope was wrong — Session 0 isolation is one way
for a daemon to lose the user's desktop session, but a system systemd unit loses it just as
completely, and B5b is what made that concrete before B5c existed. The consequence for decision 4's
Linux budget is smaller than it sounds: what a separated Linux install autostarts is
`privacyfence-companion --serve`, the `CompanionChannelServer` on its own, with no tray, no icon
and still no new dependency. "No tray on Linux" is what that budget rules out; a socket is not a
tray. The clickable Applications-menu entry stays exactly as decision 4 describes it, and remains
one-shot.

### 5a. The install location is part of the boundary on Windows, so separation needs the elevated install tier

**Added by #428 Phase 4 (B5c), 2026-09-16**, resolving the open question #428's own Phase 4 section
raised and left open: *"Installing a service requires admin, so the non-elevated per-user install
path documented in the README (see [#407](https://github.com/privacyfence/privacyfence/issues/407))
cannot have this. Either two install tiers, or that path is dropped."*

**Decided: two install tiers. The per-user path stays, exactly as it is, and cannot be separated.**

The deciding argument is not that a service install needs administrator rights — that is merely
inconvenient, and a one-time elevation prompt is a price this feature is plainly worth. It is that
**a Windows service runs whatever its `binPath` names**. PrivacyFence installed under
`%LOCALAPPDATA%\Programs` is writable by the logged-in user, which is the same account the agent
runs as; separating such an install would hand that agent a way to replace the daemon's own
executable and have the Service Control Manager run it *as the service account*. That is not a
weaker version of the guarantee — it is a privilege-escalation path that did not exist before,
which makes it strictly worse than the unseparated install it replaced.

macOS and Linux do not face this: `/Applications` and `/opt/privacyfence` are root-owned as a side
effect of how those platforms install software at all, so the question never arose there and no
equivalent check exists in their installers.

**Amended by #428 B1, 2026-09-17: the macOS half of that last paragraph was wrong.**
`/opt/privacyfence` is root-owned as claimed (dpkg-owned). `/Applications` is not: it is
`root:admin drwxrwxr-x`, and a drag-installed `.app` is normally owned by the installing user —
the same account the agent runs as. `privilege_separation.daemon_image_paths()` had documented
itself as "the one check with no POSIX counterpart" on the strength of this paragraph, so until
B1, nothing verified a macOS daemon image was not user-writable before `enable` elevated to it —
the identical escalation this section already refuses on Windows, just unguarded on the platform
whose install path (a drag to `/Applications`) makes it the easiest one to hit by accident.
`scripts/macos_privilege_separation.sh enable`'s `resolve_executables` now refuses the same way
`windows_privilege_separation.ps1`'s does, and `privilege_separation.audit_layout()` re-checks the
image on every start there too, via the POSIX counterpart this section wrongly said didn't need to
exist.

Consequences:

- `scripts/windows_privilege_separation.ps1`'s `enable` reads the install directory's ACL and
  refuses when anything outside `SYSTEM`/`Administrators` can write it, naming the per-user install
  as the likely cause. It is a refusal, not a warning: a half-honest separation is the one outcome
  worth preventing outright.
- `privilege_separation.audit_layout()` re-checks the daemon's own image on every start
  (`windows_acl.image_problems()`), because an install can be replaced in place after `enable` ran.
- `installer/privacyfence.iss` keeps `PrivilegesRequired=lowest`. Nothing about the *installer*
  changes; what is tiered is which installs this opt-in is available to.
- Dropping the per-user path instead was rejected for the reason it was added
  ([#407](https://github.com/privacyfence/privacyfence/issues/407)): it is what makes PrivacyFence
  installable by someone who cannot elevate at all, and privilege separation is opt-in — taking the
  product away from that user to make an optional hardening step universally available is the wrong
  trade in the wrong direction.

### 6. Session minting is made insufficient, not uncallable

The companion runs as the same user as the agent. `SO_PEERCRED`'s uid — or
`GetNamedPipeClientProcessId`'s — therefore cannot tell "the human clicked the tray icon" from "the
agent ran curl". The obvious patch is process identity: check the peer's executable path and code
signature. That is reasonable on macOS and Windows and weak on Linux, where the path is not a stable
identity and there is no signature to check; it would give three platforms three different strengths
of the same claimed guarantee.

So the layering is decided the other way round: **do not try to make session minting un-callable by
the agent; make a session insufficient.** #428 Phase 4 takes the human-authority files off the
agent's uid; #426's passkey then makes possession of a session insufficient to release an approval,
because the decision requires an authenticator the agent has no way to drive.

The asymmetry that produces is recorded here rather than left to be discovered:

- **Integrity is the strong guarantee** — the agent cannot approve its own request.
- **Confidentiality of the review screen is the weaker one** — an agent that reaches the web UI can
  still read what is pending. That is worth saying out loud; the alternative is implying a
  confidentiality property the design does not have.

## Rationale

### Why a companion app rather than hardening the bootstrap chain

The chain is `web_token` (file) → `POST /api/bootstrap` → code → `?bootstrap=` → `pf_session` →
decide. Every defense built so far hardens links 2–5. Link 1 is a file the agent can read, and the
not-authorized page prints the exact command that uses it — deliberately, for a locked-out human, in
`session_auth.py`'s own `curl`/`Get-Content` copy. A chain does not get stronger when you strengthen
its middle. The companion app exists to give the human a link-1 path the agent does not have once
Phase 4 has taken that file away from the user's uid.

### Alternatives weighed for the dependency budget

- **A small per-platform native shim** (Swift/AppKit, Win32, and something for Linux). Keeps the
  Python runtime at zero new dependencies — but costs three toolchains, three signing paths and
  three reimplementations of the control-channel client, for a menu with two items and an
  "open this URL" call. Rejected: the cost lands on the build and release system, which is the part
  of this project least able to absorb triplication, and the saving is a dependency that is measured
  in audit surface, not in code we have to maintain.
- **A cross-platform tray library everywhere, Linux included.** One code path, but it buys that
  uniformity with PyGObject/Xlib inside the `.deb` and an icon that may not render at all on the
  most common Linux desktop. Rejected in favor of an affordance Linux actually has.
- **A GUI toolkit** (Qt/GTK), on the argument that it would also allow native windows later.
  Rejected outright: decision 3 says there are no native windows later, and this is precisely
  ADR 0001's objection at a larger scale.
- **No companion at all; keep the agent as the courier and document it.** Rejected: that is the
  finding, not a response to it. Documentation already exists
  (`docs/security-and-compliance.md`); what is missing is a path a human can take that the model is
  not on.

### Why the existing bundle rather than a separate one

The companion needs the daemon's `paths.py` discovery-file logic, its control-channel client and
its URL handling. Shipping it as a second entry point of the same binary means those are the same
code, and means macOS notarization, Windows signing and the `.deb`'s file list all stay as they are.
The marginal packaging cost of the decision is then one new argv path and, on macOS/Windows, the
tray dependency itself.

## Consequences

- **#428 Phase 3 is unblocked and its scope is fixed** by decisions 2–4. #428's sequencing puts
  this ADR ahead of all four phases; what it actually constrains is Phases 3 and 4 — Phases 1 and 2
  are refactors whose shape this does not change. Phase 4 depends on Phase 3 on Windows, for the
  reason in decision 5.
- **#426 must not start before #428 Phase 4 has landed for a platform.** Decision 6 is the reason:
  a passkey checked against a credential store the agent can write is a checkbox a local process
  ticks for itself, which is worse than not having the feature, because it claims a guarantee.
- **The recovery copy changes once the companion ships.** The not-authorized page and
  `privacyfence_get_sign_in_link`'s own framing currently lead with "ask your MCP client", which is
  correct today and wrong the day a tray icon exists. Both need revisiting in Phase 3; the tool
  itself stays (a headless or remote-ish install still needs it), it just stops being the first
  answer.
- **Startup wiring inverts on all three platforms** in Phase 4: what autostarts in the user's
  session becomes the companion, while the daemon moves to a LaunchDaemon / system systemd unit /
  Windows service. macOS's `LSUIElement` bundle keeps its "no dock icon" behavior and gains a
  status item; the Windows installer's Scheduled Task is disabled and a second one starts the
  companion instead (disabled rather than replaced, so `disable` can restore it and the uninstaller
  still finds it); the `.deb`'s XDG autostart entry does the same and loses `NoDisplay=true`.
  Windows needed one thing the other two did not: its Service Control Manager launches a process
  and then waits to be called back, so `src/privacyfence/windows_service.py` is a service host
  around the same `daemon_main.main()` the console entry point calls — the one place where
  "the same unchanged executable, started by a different manager" was not enough.
- **Local mode gains a UI affordance that is not the browser and not the model**, which is the
  first time since P10 that a human can reach approvals without either.
- **ADR 0001's ban is now scoped rather than absolute** — see below.
- **Org mode is untouched.** Its entry point is `/login` behind an IdP, it has no bootstrap concept,
  and its daemon already runs where the agent has no access. Nothing here applies to it.

## Relationship to ADR 0001

ADR 0001 stands, with one carve-out:

- **Still binding:** the daemon declares and imports no AppKit/PyObjC/`rumps` dependency; approvals
  and settings have exactly one implementation, the web app; `pip install privacyfence[macos-native]`
  remains unsupported and that extra does not come back.
- **Superseded:** "runtime code should not import PyObjC/AppKit frameworks" is now scoped to the
  daemon. The companion entry point may, on macOS only, transitively through `pystray`, for a status
  item and nothing else — which is the "new architecture decision and dependency review" ADR 0001
  asked for rather than the silent reuse of an old extra it warned against.

## Out of scope

Native approval or settings windows; notification popups; any change to org mode; moving the
approval decision off the device entirely (a paired-phone push is the stronger long-term answer and
deserves its own issue, not a line in this one).

## Verification

- `pyproject.toml` stays authoritative for declared dependencies: the companion's entry is
  platform-marked, and nothing macOS-specific is installable on Linux or Windows.
- The daemon's import graph is the check that decision 4's last clause holds — daemon modules must
  remain importable on a machine with none of the companion's dependencies present, which is what
  `tests.yml`'s Ubuntu-only core suite already exercises by running there at all.
- Decision 3 is verifiable by grep as much as by review: no HTML, template or approval-rendering
  code reachable from the companion entry point.

## Related

- [#427](https://github.com/privacyfence/privacyfence/issues/427) — this ADR's own issue
- [#428](https://github.com/privacyfence/privacyfence/issues/428) — privilege separation; Phase 3 is the companion app
- [#426](https://github.com/privacyfence/privacyfence/issues/426) — local-mode passkey; depends on this and on Phase 4
- [#423](https://github.com/privacyfence/privacyfence/issues/423) — the recovery path that currently names the agent
- [#396](https://github.com/privacyfence/privacyfence/issues/396) / [#422](https://github.com/privacyfence/privacyfence/pull/422) — the in-flight instance of the courier pattern
- [ADR 0001](0001-remove-macos-native-extra.md) — superseded in part by this
- `docs/security-and-compliance.md` — "Local-mode trust boundary", the shipped-behavior statement of decision 1
