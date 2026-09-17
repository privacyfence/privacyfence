"""``StepUpConfig`` (P9, §10.6/§15 D7; #426): the WebAuthn step-up decision,
made concrete per install, in *either* deployment mode.

This lived in ``org_mode.py`` and was org-mode-only by design through 4.0 --
that module's own docstring reasoned that local mode's trust model
(physical possession of the machine) was D7's explicit "not this mode"
case, the same reason ``ServerConfig``/``IdpConfig`` never look at local
mode either. #426/#427/#428 supersede that premise: the adversary that
actually matters in local mode is not a human at the keyboard, it is an AI
agent with shell access on the same machine -- the normal local-mode
install -- and nothing about ``ServerConfig``'s "no server to configure" or
``IdpConfig``'s "no IdP to talk to" reasoning applies to a step-up check,
which needs neither. This module is #426 Phase 1: split out so the same
config shape and the same enrollment surface (web/routes_security.py) serve
both modes, with ``from_org_config`` kept byte-identical to before and a new
``from_local_config`` reading ``config/settings.yaml``'s own ``step_up:``
section.

Enrolling a credential here is necessary but not sufficient for the
guarantee #426's issue body describes -- see webauthn_stepup.py's own
module docstring on where the credential store lives (``paths.
authority_dir()``, #428 Phase 1) and why write access to it is a full
bypass, not merely a tamper-evidence gap. #428 Phase 4 (privilege
separation) is what makes that store, and the ``require_passkey`` flag
below, unwritable by the same agent the check defends against; enforcement
of ``require_passkey`` in local mode is #426 Phase 3, not this one. Phase 1
ships only the config shape and the ability to enroll -- "you can add a
passkey, and nothing yet asks you for it."
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Literal

from .org_mode import ConfigurationError

StepUpScope = Literal["writes", "writes_and_pii_reads"]

DEFAULT_STEP_UP_SCOPE: StepUpScope = "writes"
DEFAULT_RP_NAME = "PrivacyFence"
# WebAuthn treats "localhost" as a secure context even over plain HTTP
# (web/server.py already binds local mode's own embedded server to the
# literal "localhost", not "127.0.0.1", for this same reason -- see that
# module's own docstring) -- so local mode needs no TLS work of its own to
# run the ceremony, and can default rp_id to a value that just works rather
# than requiring an install to set one before /security does anything.
DEFAULT_LOCAL_RP_ID = "localhost"


@dataclass(frozen=True)
class StepUpConfig:
    """§10.6/§15 D7's step-up decision, made concrete per install: "Yes in
    org mode, scoped and configurable -- via a WebAuthn platform
    authenticator ... with IdP acr_values step-up as the org-mode
    alternative ... and OIDC re-auth as the fallback." Org mode reads this
    from ``org_config.json``'s ``step_up`` section (``from_org_config``);
    local mode reads it from ``config/settings.yaml``'s own ``step_up:``
    section (``from_local_config``) -- see this module's own docstring for
    why local mode gets one at all now.

    ``enabled=False`` (the default -- absent ``step_up`` section, or an
    existing install that predates this) is a real off switch, not just "no
    credentials enrolled yet": web/routes_org_approvals.py's decide endpoint
    skips the whole step-up check when this is False, and local mode's own
    decide-time check (#426 Phase 2) does the same -- turning step-up on is
    an explicit opt-in per deployment either way.

    ``require_passkey=False`` (the default) keeps D7's original two-path
    design in org mode -- a WebAuthn assertion *or* a fresh IdP
    re-authentication. Local mode has no IdP, so ``require_passkey`` there
    is the only path a decide-time check (#426 Phase 2/3) could ever offer
    -- this field is generalized here (rather than added fresh) so its name
    and semantics can't drift between the two modes.
    """

    enabled: bool = False
    # "writes" (gate_kind == "popup") is D7's baseline scope -- "scope it to
    # writes, or to writes plus PII-flagged reads ... make the scope
    # configurable" (§10.6). "writes_and_pii_reads" additionally covers a
    # read whose PendingApproval.pii_detected is True, the same signal
    # gate.py's own PII "are you sure?" confirmation already gates on.
    # Defaults to "writes" in both modes -- one key name meaning two
    # different things by mode is exactly what this module exists to stop.
    scope: StepUpScope = DEFAULT_STEP_UP_SCOPE
    # WebAuthn's Relying Party ID -- must be this server's own registrable
    # domain (§10.6: "WebAuthn needs a secure context and a registrable-
    # domain RP ID"). Org mode defaults this to ServerConfig.issuer_url's
    # own hostname (from_org_config, below); local mode defaults it to
    # "localhost" (DEFAULT_LOCAL_RP_ID) -- unlike org mode, local mode
    # always has an rp_id, so /security is always reachable there, whether
    # or not step_up.enabled is ever set.
    rp_id: str = ""
    rp_name: str = DEFAULT_RP_NAME
    require_passkey: bool = False

    @staticmethod
    def from_org_config(org_config: dict[str, Any], *, default_rp_id: str = "") -> "StepUpConfig":
        raw = org_config.get("step_up")
        raw = raw if isinstance(raw, dict) else {}
        scope = raw.get("scope", DEFAULT_STEP_UP_SCOPE)
        if scope not in ("writes", "writes_and_pii_reads"):
            raise ConfigurationError(
                f"org_config.json's \"step_up\".\"scope\" must be \"writes\" or "
                f"\"writes_and_pii_reads\", got {scope!r}"
            )
        return StepUpConfig(
            enabled=bool(raw.get("enabled", False)),
            scope=scope,
            rp_id=raw.get("rp_id", "") or default_rp_id,
            rp_name=raw.get("rp_name", DEFAULT_RP_NAME) or DEFAULT_RP_NAME,
            require_passkey=bool(raw.get("require_passkey", False)),
        )

    def local_enrollment_banner(self, *, has_credentials: bool) -> str | None:
        """#426 Phase 3's "loud persistent banner": ``None`` unless
        ``require_passkey`` is actually in force (``enabled`` too -- see
        this module's own docstring on ``require_passkey``'s dependence on
        it) and nothing is enrolled yet, in which case web_shell.wrap()'s
        own ``banner_html`` renders this on every local-mode page. The
        daemon still starts and serves in this state -- refusing to boot
        would remove the only path to ``/security`` that fixes it -- but
        web/routes_approvals.py's decide() and web/routes_settings.py's
        sensitive actions both hard-fail (403) rather than release
        anything, so this string says exactly that rather than merely
        "step-up is on"."""
        if self.enabled and self.require_passkey and not has_credentials:
            return (
                "Passkey required: no passkey is enrolled, so approving decisions and sensitive "
                'settings changes are blocked until you <a href="/security">add one</a>.'
            )
        return None

    @staticmethod
    def from_local_config(config: dict[str, Any]) -> "StepUpConfig":
        """local mode's own entry point (#426 Phase 1) -- ``config`` is the
        already-loaded ``config/settings.yaml`` dict (daemon_main.py's
        ``load_config()`` result), not a path. Unlike ``from_org_config``,
        ``rp_id`` defaults to a real value (``DEFAULT_LOCAL_RP_ID``) rather
        than requiring one to be set: local mode has no issuer_url to derive
        a default from, and "localhost" is always correct for its own
        embedded server (web/server.py's own D1 bind-host decision)."""
        raw = config.get("step_up")
        raw = raw if isinstance(raw, dict) else {}
        scope = raw.get("scope", DEFAULT_STEP_UP_SCOPE)
        if scope not in ("writes", "writes_and_pii_reads"):
            raise ConfigurationError(
                f"config/settings.yaml's \"step_up\".\"scope\" must be \"writes\" or "
                f"\"writes_and_pii_reads\", got {scope!r}"
            )
        return StepUpConfig(
            enabled=bool(raw.get("enabled", False)),
            scope=scope,
            rp_id=raw.get("rp_id", "") or DEFAULT_LOCAL_RP_ID,
            rp_name=raw.get("rp_name", DEFAULT_RP_NAME) or DEFAULT_RP_NAME,
            require_passkey=bool(raw.get("require_passkey", False)),
        )


class LiveStepUpConfig:
    """A thread-safe, mutable holder around a ``StepUpConfig`` (B9 of the
    4.1.0 action plan): web/server.py's own build_app()/WebServer resolve
    local mode's ``StepUpConfig`` exactly once, at daemon startup, and hand
    that same object to every consumer that gates on it (web/
    routes_approvals.py's decide endpoint, web/routes_settings.py's
    sensitive-action gate and banner, web/routes_security.py's enrollment
    page) -- "one StepUpConfig, read once, drives ... alike" (build_app()'s
    own docstring). That made turning step-up *on* a config-file-plus-
    restart operation with no UI path at all, the actual B9 gap: #426
    shipped the whole chain and then defaulted it off with nothing in
    /settings or /security able to flip it back on short of editing
    ``config/settings.yaml`` by hand -- ``sudo`` and a text editor on a
    privilege-separated install (#428 Phase 4).

    This class closes that gap without touching any of those call sites:
    every attribute/method a ``StepUpConfig`` exposes (``enabled``,
    ``scope``, ``rp_id``, ``rp_name``, ``require_passkey``,
    ``local_enrollment_banner``) is mirrored here, read fresh off whatever
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
    file-plus-restart operation on purpose). Org mode has no equivalent of
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

    def local_enrollment_banner(self, *, has_credentials: bool) -> str | None:
        with self._lock:
            current = self._current
        return current.local_enrollment_banner(has_credentials=has_credentials)

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
    "DEFAULT_STEP_UP_SCOPE",
    "LiveStepUpConfig",
    "StepUpConfig",
    "StepUpScope",
]
