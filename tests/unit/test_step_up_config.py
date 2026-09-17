"""Tests for step_up_config.py: the mode-agnostic WebAuthn step-up config (#426 Phase 1)."""
from __future__ import annotations

import pytest

from privacyfence import org_mode, step_up_config


class TestStepUpConfigFromOrgConfig:
    """P9, §10.6/§15 D7: WebAuthn step-up is off by default (an existing org
    install with no "step_up" section keeps working exactly as before this
    phase -- see web/routes_org_approvals.py's own decide()'s
    ``if step_up.enabled`` gate). Byte-identical to org_mode.py's own
    pre-Phase-1 parsing -- these assertions are unchanged from before the
    move, only the import moved (see test_org_mode.py's own git history)."""

    def test_absent_section_is_disabled_with_defaults(self):
        config = step_up_config.StepUpConfig.from_org_config({})
        assert config.enabled is False
        assert config.scope == "writes"
        assert config.rp_id == ""
        assert config.rp_name == step_up_config.DEFAULT_RP_NAME
        assert config.require_passkey is False

    def test_default_rp_id_falls_back_to_the_caller_supplied_default(self):
        config = step_up_config.StepUpConfig.from_org_config({}, default_rp_id="pf.example.com")
        assert config.rp_id == "pf.example.com"

    def test_explicit_rp_id_wins_over_the_default(self):
        config = step_up_config.StepUpConfig.from_org_config(
            {"step_up": {"rp_id": "custom.example.com"}}, default_rp_id="pf.example.com",
        )
        assert config.rp_id == "custom.example.com"

    def test_enabled_and_scope_and_rp_name(self):
        config = step_up_config.StepUpConfig.from_org_config({
            "step_up": {"enabled": True, "scope": "writes_and_pii_reads", "rp_name": "Acme PrivacyFence"},
        })
        assert config.enabled is True
        assert config.scope == "writes_and_pii_reads"
        assert config.rp_name == "Acme PrivacyFence"

    def test_invalid_scope_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            step_up_config.StepUpConfig.from_org_config({"step_up": {"scope": "everything"}})

    def test_require_passkey_defaults_false_and_reads_true(self):
        assert step_up_config.StepUpConfig.from_org_config(
            {"step_up": {"enabled": True}},
        ).require_passkey is False
        assert step_up_config.StepUpConfig.from_org_config(
            {"step_up": {"enabled": True, "require_passkey": True}},
        ).require_passkey is True


class TestStepUpConfigFromLocalConfig:
    """#426 Phase 1: local mode's own entry point. Unlike org mode, ``rp_id``
    defaults to a real value (``localhost``) rather than an empty string --
    local mode's own embedded server always answers to it (D1), so
    ``/security`` is reachable on a fresh install with no step_up section at
    all, well before ``enabled`` is ever turned on."""

    def test_absent_section_is_disabled_with_local_defaults(self):
        config = step_up_config.StepUpConfig.from_local_config({})
        assert config.enabled is False
        assert config.scope == "writes"
        assert config.rp_id == step_up_config.DEFAULT_LOCAL_RP_ID
        assert config.rp_id == "localhost"
        assert config.rp_name == step_up_config.DEFAULT_RP_NAME
        assert config.require_passkey is False

    def test_explicit_rp_id_overrides_the_local_default(self):
        config = step_up_config.StepUpConfig.from_local_config({"step_up": {"rp_id": "pf.local"}})
        assert config.rp_id == "pf.local"

    def test_enabled_scope_and_require_passkey(self):
        config = step_up_config.StepUpConfig.from_local_config({
            "step_up": {"enabled": True, "scope": "writes_and_pii_reads", "require_passkey": True},
        })
        assert config.enabled is True
        assert config.scope == "writes_and_pii_reads"
        assert config.require_passkey is True

    def test_invalid_scope_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            step_up_config.StepUpConfig.from_local_config({"step_up": {"scope": "everything"}})

    def test_non_dict_section_is_treated_as_absent(self):
        config = step_up_config.StepUpConfig.from_local_config({"step_up": "nonsense"})
        assert config.rp_id == "localhost"
        assert config.enabled is False


class TestLocalEnrollmentBanner:
    """#426 Phase 3's "loud persistent banner" -- fires only in the one
    state that actually means something is blocked: step-up genuinely in
    force (``enabled`` *and* ``require_passkey``) and nothing enrolled yet.
    See web_shell.py's own TestBanner for how the string this returns is
    rendered."""

    def test_none_when_require_passkey_is_off(self):
        config = step_up_config.StepUpConfig(enabled=True, require_passkey=False)
        assert config.local_enrollment_banner(has_credentials=False) is None

    def test_none_when_step_up_itself_is_disabled(self):
        config = step_up_config.StepUpConfig(enabled=False, require_passkey=True)
        assert config.local_enrollment_banner(has_credentials=False) is None

    def test_none_once_a_credential_is_enrolled(self):
        config = step_up_config.StepUpConfig(enabled=True, require_passkey=True)
        assert config.local_enrollment_banner(has_credentials=True) is None

    def test_banner_text_when_genuinely_unmet(self):
        config = step_up_config.StepUpConfig(enabled=True, require_passkey=True)
        banner = config.local_enrollment_banner(has_credentials=False)
        assert banner is not None
        assert "/security" in banner
