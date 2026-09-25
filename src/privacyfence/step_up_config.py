"""``StepUpConfig``: the WebAuthn step-up decision, made concrete per install,
in *either* deployment mode.

Step-up applies to local mode as well as org mode because the adversary that
matters in local mode is not a human at the keyboard, it is an AI agent with
shell access on the same machine -- the normal local-mode install (ADR 0002,
ADR 0003). The reasoning that keeps ``ServerConfig``/``IdpConfig`` out of
local mode ("no server to configure", "no IdP to talk to") does not apply to
a step-up check, which needs neither. So one config shape and one enrollment
surface (web/routes_security.py) serve both modes: ``from_org_config``
reads the org config's ``step_up:`` section, and ``from_local_config`` reads
``config/settings.yaml``'s own.

Enrolling a credential is necessary but not sufficient for the guarantee --
see webauthn_stepup.py's own module docstring on where the credential store
lives (``paths.authority_dir()``) and why write access to it is a full
bypass, not merely a tamper-evidence gap. Privilege separation (ADR 0003) is
what makes that store, and the ``require_passkey`` flag below, unwritable by
the same agent the check defends against. This module only parses the
config; web/routes_approvals.py and web/routes_settings.py enforce it.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

from . import paths, privilege_separation
from .org_mode import ConfigurationError

logger = logging.getLogger(__name__)

StepUpScope = Literal["writes", "writes_and_pii_reads", "writes_and_reads"]
StepUpBatchMode = Literal["single_assertion", "per_item"]

# Widening ladder, narrowest first -- also the order both validators below
# name them in, and the order scripts/build_org_bundle.py's own
# --step-up-scope choices list repeats (that script is deliberately
# stdlib-only, so it cannot import this tuple; keep the two in sync).
STEP_UP_SCOPES: tuple[StepUpScope, ...] = ("writes", "writes_and_pii_reads", "writes_and_reads")
_SCOPE_CHOICES_TEXT = ", ".join(f'"{name}"' for name in STEP_UP_SCOPES)

# "writes_and_pii_reads", not the narrower "writes". Both are reasonable
# against a human at the keyboard, but ADR 0003's adversary is an agent
# with code execution on this machine, and against that adversary
# "writes" leaves every read a session alone can release, including one
# pii_detector.py *did* flag as carrying personal data. Exfiltration is the
# obvious thing such an agent wants and the one thing "writes" never asks
# a human about, so the default is one rung wider. The widest
# rung ("writes_and_reads", every gated read, flagged or not) stays opt-in:
# it asks for a passkey on reads nothing has any reason to think are
# sensitive, which is the kind of prompt people learn to click through
# (ADR 0067).
#
# One value for both modes, deliberately -- see this module's own docstring
# on why a key that means two different things by mode is the thing this
# module exists to stop. An install with its own ``scope:`` set (either
# mode) keeps exactly what it set; only an install that never expressed an
# opinion gets this default.
DEFAULT_STEP_UP_SCOPE: StepUpScope = "writes_and_pii_reads"
# The approval binder's own knob: "single_
# assertion" is one bound WebAuthn ceremony over the whole selected set
# (webauthn_stepup.batch_decision_fingerprint) -- the whole point of the
# binder, and the default. "per_item" is the escape hatch for an install
# that wants no single prompt to ever cover more than one decision: the
# batch decide endpoint refuses outright to release anything that needs
# step-up (nothing applied), rather than silently downgrading to one
# assertion per item within the same request -- see web/routes_approvals.py's
# own batch_decide for exactly what that refusal looks like. Deny-only
# batches are unaffected either way, since denying never needs step-up.
# See ADR 0065.
DEFAULT_STEP_UP_BATCH_MODE: StepUpBatchMode = "single_assertion"
DEFAULT_RP_NAME = "PrivacyFence"
# WebAuthn treats "localhost" as a secure context even over plain HTTP
# (web/server.py already binds local mode's own embedded server to the
# literal "localhost", not "127.0.0.1", for this same reason -- see that
# module's own docstring) -- so local mode needs no TLS work of its own to
# run the ceremony, and can default rp_id to a value that just works rather
# than requiring an install to set one before /security does anything.
DEFAULT_LOCAL_RP_ID = "localhost"


def default_local_step_up() -> bool:
    """What ``step_up.enabled``/``step_up.require_passkey`` default to in
    local mode when ``config/settings.yaml`` expresses no opinion about
    them -- the default ADR 0003's amendment under *Out of scope* records.

    True on a **packaged** install, False everywhere else. That split is
    the whole of the decision, and it is ADR 0003's own gate reused rather
    than a second one: ``privilege_separation.enforce_separation()`` is
    scoped to ``paths.is_bundled()`` too, and refuses to serve a packaged
    install it could not separate -- so on exactly the builds this returns
    True for, the credential store a passkey is checked against is already
    out of the agent's reach by the time this is read. Everywhere else
    (a source checkout, an editable install, ``pipx install privacyfence``)
    nothing separates anything, and ADR 0002 decision 6's verdict on
    turning this on there still stands: a passkey checked against a
    credential store the agent can write is a checkbox a local process
    ticks for itself.

    The separation check is repeated here rather than assumed from
    ``is_bundled()`` alone because a *default* must never be able to fail a
    daemon's boot. ``from_local_config``'s ``ConfigurationError`` below is
    the right answer for somebody who wrote ``require_passkey: true`` into
    a file by hand; it is the wrong answer for a value nobody chose, which
    would turn an unexpected packaging state into an install that will not
    start at all. So a packaged install that somehow reaches this
    unseparated defaults *off*, loudly, instead.

    Deliberately not retroactive. This is consulted only for a key that is
    absent, and every install seeded from a settings.yaml.example older
    than this one has ``enabled: false``/``require_passkey: false`` written
    out in full -- so an existing install keeps the posture it has, and it
    is fresh installs that come up protected. An upgrade that silently
    flipped this on would also be an upgrade that, for installs with
    nothing enrolled, blocked every approval the moment it restarted; Phase
    1.2's first-run enrollment is what makes that state short-lived on a
    fresh install, and it has no equivalent for one that is already set up.
    """
    if not paths.is_bundled():
        return False
    if privilege_separation.is_enabled() or privilege_separation.dev_allows_unseparated():
        return True
    logger.warning(
        "This is a packaged build that is not privilege-separated, so step-up is defaulting to "
        "off rather than on -- a passkey checked against a credential store the agent can write "
        "guarantees nothing (ADR 0002 decision 6).",
    )
    return False


@dataclass(frozen=True)
class StepUpConfig:
    """The step-up decision, made concrete per install: step-up is scoped
    and configurable, via a WebAuthn assertion, with IdP acr_values step-up
    as the org-mode alternative and OIDC re-auth as the fallback there
    (ADR 0034 for what it gates, ADR 0055 for which authenticators
    enroll). Org mode reads this
    from ``org_config.json``'s ``step_up`` section (``from_org_config``);
    local mode reads it from ``config/settings.yaml``'s own ``step_up:``
    section (``from_local_config``) -- see this module's own docstring for
    why local mode gets one at all.

    ``enabled=False`` is a real off switch, not just "no credentials
    enrolled yet": web/routes_approvals.py's decide endpoint skips the
    whole step-up check when this is False, in both modes.

    ``require_passkey=False`` keeps org mode's two-path design -- a WebAuthn
    assertion *or* a fresh IdP re-authentication. Local mode has no IdP, so
    a passkey is the only path its decide-time check can offer. The field
    lives on this shared class so its name and semantics can't drift
    between the two modes.

    Both default to False *on this class*, which is the value a caller that
    constructs one directly gets and the value org mode resolves for an
    ``org_config.json`` with no ``step_up`` section -- org mode has an IdP
    and a human administrator writing that bundle, so an unstated opinion
    there stays an unstated opinion. Local mode does not read these
    field defaults for an absent key: ``from_local_config`` resolves its
    own from ``default_local_step_up()``, which is True on a packaged
    install. The two are not the same question, so they are not
    the same default -- see that function's own docstring.
    """

    enabled: bool = False
    # "writes" (gate_kind == "popup") is the baseline scope: every gated
    # write. "writes_and_pii_reads" additionally covers a
    # read whose PendingApproval.pii_detected is True, the same signal
    # gate.py's own PII "are you sure?" confirmation already gates on.
    # "writes_and_reads" goes one step further and covers every gated read,
    # flagged or not -- the two narrower scopes both leave an unflagged read
    # releasable by a session alone, which is only the guarantee an install
    # wants if it trusts pii_detector.py to have seen everything worth
    # confirming; this third value is for the installs that don't (a read
    # nobody flagged still discloses whatever the connector returned).
    # Defaults to DEFAULT_STEP_UP_SCOPE in both modes -- one key name
    # meaning two different things by mode is exactly what this module
    # exists to stop. See that constant for why the floor is
    # "writes_and_pii_reads" rather than "writes".
    scope: StepUpScope = DEFAULT_STEP_UP_SCOPE
    # WebAuthn's Relying Party ID -- must be this server's own registrable
    # domain, because WebAuthn needs a secure context and a registrable-
    # domain RP ID. Org mode defaults this to ServerConfig.issuer_url's
    # own hostname (from_org_config, below); local mode defaults it to
    # "localhost" (DEFAULT_LOCAL_RP_ID) -- unlike org mode, local mode
    # always has an rp_id, so /security is always reachable there, whether
    # or not step_up.enabled is ever set.
    rp_id: str = ""
    rp_name: str = DEFAULT_RP_NAME
    require_passkey: bool = False
    # The approval binder's own batch-decide knob -- see
    # DEFAULT_STEP_UP_BATCH_MODE's own comment. One key, one meaning, in
    # both modes -- the same reasoning every other field on this class
    # already gives for living here instead of duplicated per mode.
    batch: StepUpBatchMode = DEFAULT_STEP_UP_BATCH_MODE

    @staticmethod
    def from_org_config(org_config: dict[str, Any], *, default_rp_id: str = "") -> "StepUpConfig":
        raw = org_config.get("step_up")
        raw = raw if isinstance(raw, dict) else {}
        scope = raw.get("scope", DEFAULT_STEP_UP_SCOPE)
        if scope not in STEP_UP_SCOPES:
            raise ConfigurationError(
                f"org_config.json's \"step_up\".\"scope\" must be one of "
                f"{_SCOPE_CHOICES_TEXT}, got {scope!r}"
            )
        batch = raw.get("batch", DEFAULT_STEP_UP_BATCH_MODE)
        if batch not in ("single_assertion", "per_item"):
            raise ConfigurationError(
                f"org_config.json's \"step_up\".\"batch\" must be \"single_assertion\" or "
                f"\"per_item\", got {batch!r}"
            )
        return StepUpConfig(
            enabled=bool(raw.get("enabled", False)),
            scope=scope,
            rp_id=raw.get("rp_id", "") or default_rp_id,
            rp_name=raw.get("rp_name", DEFAULT_RP_NAME) or DEFAULT_RP_NAME,
            require_passkey=bool(raw.get("require_passkey", False)),
            batch=batch,
        )

    def local_enrollment_banner(self, *, has_credentials: bool) -> str | None:
        """The loud, persistent "passkey required" banner: ``None`` unless
        ``require_passkey`` is actually in force (``enabled`` too -- see
        this module's own docstring on ``require_passkey``'s dependence on
        it) and nothing is enrolled yet, in which case web_shell.wrap()'s
        own ``banner_html`` renders this on every local-mode page. The
        daemon still starts and serves in this state -- refusing to boot
        would remove the only path to ``/security`` that fixes it -- but
        web/routes_approvals.py's decide() and web/routes_settings.py's
        sensitive actions both hard-fail (403) rather than release
        anything, so this string says exactly that rather than merely
        "step-up is on". See ADR 0069."""
        if self.enabled and self.require_passkey and not has_credentials:
            return (
                "Passkey required: no passkey is enrolled, so approving decisions and sensitive "
                'settings changes are blocked until you <a href="/security">add one</a>.'
            )
        return None

    def off_notice(self) -> str | None:
        """The notice that step-up is off.
        ``local_enrollment_banner`` above only speaks once ``require_
        passkey`` is actually in force, and webauthn_stepup.py's
        ``step_up_disabled_notice`` only once a disable *transition* has
        been latched -- neither says anything about the ordinary default
        an install ships with (``enabled=False``, nothing in ``step_up:``
        at all), so without this a fresh install would give no sign the
        control existed, let alone that it was off. This covers that case:
        ``None`` whenever step-up is actually in force (``enabled and
        require_passkey`` -- the same pairing ``observe_step_up_
        requirement`` treats as "required"), a short sentence otherwise.

        Unlike the other two, this is advisory rather than a live
        state indicator -- an install that has simply never turned this on
        is not misconfigured or compromised, just less protected than it
        could be -- so web_shell.wrap()'s caller renders it as a
        dismissible notice (``dismissible_notice_html``), not the
        non-dismissable ``banner_html`` strip: once a person has seen it,
        it should not keep reappearing for as long as the install stays in
        this same, safe-if-less-protected default state."""
        if self.enabled and self.require_passkey:
            return None
        return (
            "Approvals are not passkey-protected: anyone (or anything) with a session on this "
            'install can approve its own writes. <a href="/settings">Turn on step-up</a> to '
            "require a passkey first."
        )

    @staticmethod
    def from_local_config(config: dict[str, Any]) -> "StepUpConfig":
        """local mode's own entry point -- ``config`` is the
        already-loaded ``config/settings.yaml`` dict (daemon_main.py's
        ``load_config()`` result), not a path. Unlike ``from_org_config``,
        ``rp_id`` defaults to a real value (``DEFAULT_LOCAL_RP_ID``) rather
        than requiring one to be set: local mode has no issuer_url to derive
        a default from, and "localhost" is always correct for its own
        embedded server (web/server.py's bind-host decision, ADR 0010)."""
        raw = config.get("step_up")
        raw = raw if isinstance(raw, dict) else {}
        scope = raw.get("scope", DEFAULT_STEP_UP_SCOPE)
        if scope not in STEP_UP_SCOPES:
            raise ConfigurationError(
                f"config/settings.yaml's \"step_up\".\"scope\" must be one of "
                f"{_SCOPE_CHOICES_TEXT}, got {scope!r}"
            )
        batch = raw.get("batch", DEFAULT_STEP_UP_BATCH_MODE)
        if batch not in ("single_assertion", "per_item"):
            raise ConfigurationError(
                f"config/settings.yaml's \"step_up\".\"batch\" must be \"single_assertion\" or "
                f"\"per_item\", got {batch!r}"
            )
        # The *absent*-key default is packaging-dependent --
        # see ``default_local_step_up()`` for why, and why it is resolved
        # once here so ``enabled`` and ``require_passkey`` can never default
        # apart. An explicitly written value always wins, in both
        # directions: an install that says ``require_passkey: false`` keeps
        # it off, packaged or not.
        default_on = default_local_step_up()
        require_passkey_is_explicit = "require_passkey" in raw
        require_passkey = bool(raw.get("require_passkey", default_on))
        # ADR 0003, "Why not gate the passkey instead": kept as a consequence
        # of decision 1 rather than dropped, because it is unreachable on a
        # shipped install (daemon_main.py's enforce_separation() already
        # refused to start a packaged, unseparated one before load_config()
        # -- and therefore this -- is ever reached) and catches exactly the
        # developer path that gate does not cover: a non-packaged local-mode
        # checkout with `require_passkey: true` in its own settings.yaml but
        # no service account backing it. ADR 0002 decision 6 already named
        # why that combination is worse than not having the feature at all --
        # "a passkey checked against a credential store the agent can write
        # is a checkbox a local process ticks for itself". The escape hatch
        # is the same one decision 7 gives the daemon-startup gate's sibling
        # case, in the same house spelling.
        # Only ever raised for a value somebody wrote down: the defaulted
        # one already answered this question for itself (``default_local_
        # step_up()``'s own docstring on why a default must not be able to
        # fail a boot), so ``require_passkey_is_explicit`` is what separates
        # "you asked for something this install cannot back" from "nobody
        # asked for anything".
        separated_or_dev = privilege_separation.is_enabled() or privilege_separation.dev_allows_unseparated()
        if require_passkey and require_passkey_is_explicit and not separated_or_dev:
            raise ConfigurationError(
                "config/settings.yaml's \"step_up\".\"require_passkey\" is true, but this install "
                "is not privilege-separated (ADR 0003) -- the credential store a "
                "passkey is checked against is writable by the same account the agent runs as, so "
                "turning this on makes the guarantee worse, not better. Separate this install "
                f"first, or set {privilege_separation.DEV_ALLOW_UNSEPARATED_ENV}=1 for local "
                "development (never in a real deployment)."
            )
        return StepUpConfig(
            enabled=bool(raw.get("enabled", default_on)),
            scope=scope,
            rp_id=raw.get("rp_id", "") or DEFAULT_LOCAL_RP_ID,
            rp_name=raw.get("rp_name", DEFAULT_RP_NAME) or DEFAULT_RP_NAME,
            require_passkey=require_passkey,
            batch=batch,
        )


class LiveStepUpConfig:
    """A thread-safe, mutable holder around a ``StepUpConfig``: web/server.py's
    own build_app()/WebServer resolve local mode's ``StepUpConfig`` exactly once, at daemon startup, and hand
    that same object to every consumer that gates on it (web/
    routes_approvals.py's decide endpoint, web/routes_settings.py's
    sensitive-action gate and banner, web/routes_security.py's enrollment
    page) -- "one StepUpConfig, read once, drives ... alike" (build_app()'s
    own docstring). A plain ``StepUpConfig`` would make turning step-up
    *on* a config-file-plus-restart operation with no UI path at all:
    editing ``config/settings.yaml`` by hand, which is ``sudo`` and a text
    editor on a privilege-separated install.

    This class gives it a UI path without touching any of those call sites:
    every attribute/method a ``StepUpConfig`` exposes (``enabled``,
    ``scope``, ``rp_id``, ``rp_name``, ``require_passkey``,
    ``local_enrollment_banner``, ``off_notice``) is mirrored here, read
    fresh off whatever
    ``StepUpConfig`` is currently held rather than fixed at construction
    time -- so daemon_main.py's local-mode boot path can hand *this*
    object, instead of a bare ``StepUpConfig``, to every one of the above
    consumers with no change to any of them, and settings_controller.py's
    ``enable_step_up`` (wired in via ``SettingsController.wire_step_up``,
    the same after-the-fact pattern ``wire_unattended_listener`` already
    uses) can call ``update()`` to make a config change take effect for the
    very next request -- no restart, matching every other settings.yaml
    write this codebase already hot-reloads (auto-accept rules, the privacy
    filter).

    Deliberately one-directional in what it's used for: only ``enable_step_
    up`` ever calls ``update()``, always turning step-up *on* (see that
    method's own docstring for why turning it back *off* stays a config-
    file-plus-restart operation on purpose, and ADR 0068). Org mode has no equivalent of
    this class -- its own ``StepUpConfig`` is re-derived from
    ``org.org_config`` on every ``_build_org_app`` call instead (web/
    server.py), which already has no restart problem to solve.
    """

    def __init__(self, initial: StepUpConfig) -> None:
        self._lock = threading.Lock()
        self._current = initial

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._current.enabled

    @property
    def scope(self) -> StepUpScope:
        with self._lock:
            return self._current.scope

    @property
    def rp_id(self) -> str:
        with self._lock:
            return self._current.rp_id

    @property
    def rp_name(self) -> str:
        with self._lock:
            return self._current.rp_name

    @property
    def require_passkey(self) -> bool:
        with self._lock:
            return self._current.require_passkey

    @property
    def batch(self) -> StepUpBatchMode:
        with self._lock:
            return self._current.batch

    def local_enrollment_banner(self, *, has_credentials: bool) -> str | None:
        with self._lock:
            current = self._current
        return current.local_enrollment_banner(has_credentials=has_credentials)

    def off_notice(self) -> str | None:
        with self._lock:
            current = self._current
        return current.off_notice()

    def update(self, cfg: StepUpConfig) -> None:
        """Swap in a freshly loaded ``StepUpConfig`` -- called by
        settings_controller.py's ``enable_step_up`` right after it persists
        the same change to ``config/settings.yaml``, so every consumer
        holding this object sees the new value on its very next read."""
        with self._lock:
            self._current = cfg


__all__ = [
    "DEFAULT_LOCAL_RP_ID",
    "DEFAULT_RP_NAME",
    "DEFAULT_STEP_UP_BATCH_MODE",
    "DEFAULT_STEP_UP_SCOPE",
    "LiveStepUpConfig",
    "STEP_UP_SCOPES",
    "StepUpBatchMode",
    "StepUpConfig",
    "StepUpScope",
    "default_local_step_up",
]
