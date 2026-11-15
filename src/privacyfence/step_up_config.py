"""``StepUpConfig``: the WebAuthn step-up decision, made concrete per install,
for the organization deployment.

``from_org_config`` reads the org config's ``step_up:`` section; one config shape and one
enrollment surface (web/routes_security.py) serve it.

Enrolling a credential is necessary but not sufficient for the guarantee --
see webauthn_stepup.py's own module docstring on where the credential store
lives (``paths.authority_dir()``) and why write access to it is a full
bypass, not merely a tamper-evidence gap. This module only parses the
config; web/routes_approvals.py and web/routes_settings.py enforce it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import paths
from .org_mode import ConfigurationError
from .secure_files import atomic_write_json

if TYPE_CHECKING:
    from .principal import Principal

logger = logging.getLogger(__name__)

StepUpScope = Literal["writes", "writes_and_pii_reads", "writes_and_reads"]
StepUpBatchMode = Literal["single_assertion", "per_item"]

# Widening ladder, narrowest first -- also the order both validators below
# name them in, and the order the Admin page lists them in.
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
# An install with its own ``scope:`` set keeps exactly what it set; only an
# install that never expressed an opinion gets this default.
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


def widest_scope(*scopes: str) -> StepUpScope:
    """The widest of ``scopes`` on the ``STEP_UP_SCOPES`` ladder. A value
    that is not on the ladder (a hand-edited preference file, say) is
    ignored rather than trusted, so it can never narrow anything; with no
    valid value at all, the narrowest rung comes back."""
    ranks = [STEP_UP_SCOPES.index(s) for s in scopes if s in STEP_UP_SCOPES]
    return STEP_UP_SCOPES[max(ranks, default=0)]


# --------------------------------------------------------------------- #
# A principal's own hardening on top of the configured scope (ADR 0091).
#
# ``step_up.scope`` -- the admin config's -- is the *minimum*: nothing a person does from the
# Settings page can make an install ask for a passkey on less than that.
# What a person can do is ask for more, for their own approvals only: one
# small file per principal under ``paths.authority_dir()``, the same
# service-owned root the passkey credential store itself lives in (ADR
# 0003), so the agent step-up defends against cannot quietly narrow it
# back either. ``effective_scope`` is the one place the two combine.
# --------------------------------------------------------------------- #

STEP_UP_PREFERENCE_FILE_NAME = "step_up_preference.json"


def _preference_path(principal: "Principal") -> Path:
    return paths.authority_dir(principal) / STEP_UP_PREFERENCE_FILE_NAME


def personal_scope(principal: "Principal") -> StepUpScope | None:
    """The scope ``principal`` chose for themselves in Settings, or ``None``
    when they never chose one (or the file cannot be read -- which only
    ever falls back to the configured minimum, never below it)."""
    path = _preference_path(principal)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read the step-up preference for %s -- using the configured scope", principal.id)
        return None
    scope = raw.get("scope") if isinstance(raw, dict) else None
    return scope if scope in STEP_UP_SCOPES else None


def set_personal_scope(principal: "Principal", scope: str, *, floor: str) -> StepUpScope:
    """Record ``scope`` as ``principal``'s own choice and return the scope
    now in effect for them. A choice no wider than ``floor`` (the configured
    minimum) is stored as no choice at all, so a later widening of the
    configured scope is never held back by a stale personal value. Raises
    ``ValueError`` for a value that is not on the ladder."""
    if scope not in STEP_UP_SCOPES:
        raise ValueError(f"step-up scope must be one of {_SCOPE_CHOICES_TEXT}, got {scope!r}")
    effective = widest_scope(floor, scope)
    stored = scope if effective != widest_scope(floor) else None
    atomic_write_json(_preference_path(principal), {"scope": stored})
    return effective


def effective_scope(step_up: "StepUpConfig", principal: "Principal") -> StepUpScope:
    """The scope a decision by ``principal`` is actually checked against:
    the configured ``step_up.scope``, widened by the principal's own
    Settings choice, never narrowed by it."""
    chosen = personal_scope(principal)
    return widest_scope(step_up.scope, chosen) if chosen is not None else widest_scope(step_up.scope)


def step_up_scope_fields(step_up: "StepUpConfig | None", principal: "Principal") -> dict[str, Any]:
    """The Settings page's view of the above, one shape for the
    ``general`` state: whether a scope is in force at all (``step_up.
    enabled``), the configured minimum, and the scope in effect for
    ``principal``. settings_window_html.py's Security card draws the
    ``set_step_up_scope`` control from these three."""
    if step_up is None:
        return {"step_up_scope_active": False, "step_up_scope_floor": "", "step_up_scope": ""}
    return {
        "step_up_scope_active": bool(step_up.enabled),
        "step_up_scope_floor": widest_scope(step_up.scope),
        "step_up_scope": effective_scope(step_up, principal),
    }


@dataclass(frozen=True)
class StepUpConfig:
    """The step-up decision, made concrete per install: step-up is scoped
    and configurable, via a WebAuthn assertion, with IdP acr_values step-up
    as the org-mode alternative and OIDC re-auth as the fallback there
    (ADR 0034 for what it gates, ADR 0055 for which authenticators
    enroll). It is read from the admin config's ``step_up`` section
    (``from_org_config``).

    ``enabled=False`` is a real off switch, not just "no credentials
    enrolled yet": web/routes_approvals.py's decide endpoint skips the
    whole step-up check when this is False.

    ``require_passkey=False`` keeps the two-path design -- a WebAuthn
    assertion *or* a fresh IdP re-authentication.

    Both default to False: that is the value a caller that constructs one
    directly gets and the value an organization config with no ``step_up``
    section resolves to, since an IdP and a human administrator write that
    config and an unstated opinion there stays an unstated opinion.
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
    # Defaults to DEFAULT_STEP_UP_SCOPE. See that constant for why the floor is
    # "writes_and_pii_reads" rather than "writes".
    scope: StepUpScope = DEFAULT_STEP_UP_SCOPE
    # WebAuthn's Relying Party ID -- must be this server's own registrable
    # domain, because WebAuthn needs a secure context and a registrable-
    # domain RP ID. It defaults to ServerConfig.issuer_url's own hostname
    # (from_org_config, below).
    rp_id: str = ""
    rp_name: str = DEFAULT_RP_NAME
    require_passkey: bool = False
    # The approval binder's own batch-decide knob -- see
    # DEFAULT_STEP_UP_BATCH_MODE's own comment. One key, one meaning.
    batch: StepUpBatchMode = DEFAULT_STEP_UP_BATCH_MODE

    @staticmethod
    def from_org_config(org_config: dict[str, Any], *, default_rp_id: str = "") -> "StepUpConfig":
        raw = org_config.get("step_up")
        raw = raw if isinstance(raw, dict) else {}
        scope = raw.get("scope", DEFAULT_STEP_UP_SCOPE)
        if scope not in STEP_UP_SCOPES:
            raise ConfigurationError(
                f"org_bundle.json's \"step_up\".\"scope\" must be one of "
                f"{_SCOPE_CHOICES_TEXT}, got {scope!r}"
            )
        batch = raw.get("batch", DEFAULT_STEP_UP_BATCH_MODE)
        if batch not in ("single_assertion", "per_item"):
            raise ConfigurationError(
                f"org_bundle.json's \"step_up\".\"batch\" must be \"single_assertion\" or "
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

    def off_notice(self) -> str | None:
        """The notice that step-up is off.
        webauthn_stepup.py's ``step_up_disabled_notice`` only speaks once a
        disable *transition* has been latched, which says nothing about the ordinary default
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


__all__ = [
    "DEFAULT_RP_NAME",
    "DEFAULT_STEP_UP_BATCH_MODE",
    "DEFAULT_STEP_UP_SCOPE",
    "STEP_UP_SCOPES",
    "StepUpBatchMode",
    "StepUpConfig",
    "StepUpScope",
    "effective_scope",
    "personal_scope",
    "set_personal_scope",
    "step_up_scope_fields",
    "widest_scope",
]
