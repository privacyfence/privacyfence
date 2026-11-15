"""Tests for step_up_config.py: the mode-agnostic WebAuthn step-up config."""
from __future__ import annotations

import pytest

from privacyfence import org_mode, step_up_config
from privacyfence.principal import Principal


class TestStepUpConfigFromOrgConfig:
    """WebAuthn step-up is off by default in org mode (an org install with
    no "step_up" section is not gated -- see web/routes_approvals.py's own
    decide()'s ``if step_up.enabled`` gate)."""

    def test_absent_section_is_disabled_with_defaults(self):
        config = step_up_config.StepUpConfig.from_org_config({})
        assert config.enabled is False
        assert config.scope == "writes_and_pii_reads"
        assert config.scope == step_up_config.DEFAULT_STEP_UP_SCOPE
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

    def test_the_widest_scope_is_accepted(self):
        config = step_up_config.StepUpConfig.from_org_config({"step_up": {"scope": "writes_and_reads"}})
        assert config.scope == "writes_and_reads"

    def test_the_rejection_message_names_every_accepted_scope(self):
        with pytest.raises(org_mode.ConfigurationError) as exc:
            step_up_config.StepUpConfig.from_org_config({"step_up": {"scope": "everything"}})
        for name in step_up_config.STEP_UP_SCOPES:
            assert f'"{name}"' in str(exc.value)

    def test_require_passkey_defaults_false_and_reads_true(self):
        assert step_up_config.StepUpConfig.from_org_config(
            {"step_up": {"enabled": True}},
        ).require_passkey is False
        assert step_up_config.StepUpConfig.from_org_config(
            {"step_up": {"enabled": True, "require_passkey": True}},
        ).require_passkey is True

    def test_batch_defaults_to_single_assertion_and_reads_per_item(self):
        assert step_up_config.StepUpConfig.from_org_config({}).batch == "single_assertion"
        assert step_up_config.StepUpConfig.from_org_config(
            {"step_up": {"batch": "per_item"}},
        ).batch == "per_item"

    def test_invalid_batch_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            step_up_config.StepUpConfig.from_org_config({"step_up": {"batch": "something_else"}})


class TestOrgDefaultsAreNotTheLocalOnes:
    def test_an_organization_config_without_a_section_is_off(self):
        config = step_up_config.StepUpConfig.from_org_config({})
        assert (config.enabled, config.require_passkey) == (False, False)


class TestOffNotice:
    """The fallback for the state neither of
    the other two banners cover -- an install that has simply never turned
    step-up on. Fires exactly when step-up is *not* actually "required"
    (the same ``enabled and require_passkey`` pairing ``observe_step_up_
    requirement`` uses), regardless of ``has_credentials`` -- unlike
    the step-up-disabled notice this doesn't care whether a passkey is
    enrolled, only whether the requirement is in force at all."""

    def test_none_when_step_up_is_genuinely_required(self):
        config = step_up_config.StepUpConfig(enabled=True, require_passkey=True)
        assert config.off_notice() is None

    def test_notice_on_the_ordinary_default(self):
        config = step_up_config.StepUpConfig()
        notice = config.off_notice()
        assert notice is not None
        assert "/settings" in notice

    def test_notice_when_enabled_but_require_passkey_is_off(self):
        config = step_up_config.StepUpConfig(enabled=True, require_passkey=False)
        assert config.off_notice() is not None

    def test_notice_when_require_passkey_but_not_enabled(self):
        config = step_up_config.StepUpConfig(enabled=False, require_passkey=True)
        assert config.off_notice() is not None


class TestLiveStepUpConfig:
    """``LiveStepUpConfig`` mirrors every read a plain ``StepUpConfig``
    offers, off whatever value it currently holds, so every consumer that
    was written against a bare ``StepUpConfig`` (web/routes_approvals.py,
    web/routes_settings.py, web/routes_security.py) keeps working unchanged
    when local mode hands it one of these instead -- see the class's own
    docstring."""

    def test_reads_mirror_the_initial_config(self):
        initial = step_up_config.StepUpConfig(
            enabled=False, scope="writes", rp_id="localhost", rp_name="PrivacyFence", require_passkey=False,
            batch="per_item",
        )
        live = step_up_config.LiveStepUpConfig(initial)
        assert live.enabled is False
        assert live.scope == "writes"
        assert live.rp_id == "localhost"
        assert live.rp_name == "PrivacyFence"
        assert live.require_passkey is False
        assert live.batch == "per_item"

    def test_update_is_visible_to_every_subsequent_read(self):
        live = step_up_config.LiveStepUpConfig(step_up_config.StepUpConfig())
        assert live.enabled is False
        assert live.require_passkey is False
        live.update(step_up_config.StepUpConfig(enabled=True, require_passkey=True))
        assert live.enabled is True
        assert live.require_passkey is True

    def test_off_notice_reflects_the_current_value(self):
        live = step_up_config.LiveStepUpConfig(step_up_config.StepUpConfig(enabled=False, require_passkey=False))
        assert live.off_notice() is not None
        live.update(step_up_config.StepUpConfig(enabled=True, require_passkey=True))
        assert live.off_notice() is None


class TestPersonalScopeWidensNeverNarrows:
    """ADR 0091: the configured ``step_up.scope`` is the minimum, a
    principal's own Settings choice can only add to it."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        from privacyfence import paths
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    PRINCIPAL = Principal(id="p1")

    def test_widest_scope_ranks_the_ladder_and_ignores_unknown_values(self):
        assert step_up_config.widest_scope("writes", "writes_and_reads") == "writes_and_reads"
        assert step_up_config.widest_scope("writes_and_pii_reads", "writes") == "writes_and_pii_reads"
        assert step_up_config.widest_scope("writes", "everything") == "writes"
        assert step_up_config.widest_scope() == "writes"

    def test_no_choice_means_the_configured_scope(self):
        cfg = step_up_config.StepUpConfig(enabled=True, scope="writes_and_pii_reads")
        assert step_up_config.personal_scope(self.PRINCIPAL) is None
        assert step_up_config.effective_scope(cfg, self.PRINCIPAL) == "writes_and_pii_reads"

    def test_a_wider_choice_widens(self):
        cfg = step_up_config.StepUpConfig(enabled=True, scope="writes_and_pii_reads")
        assert step_up_config.set_personal_scope(self.PRINCIPAL, "writes_and_reads", floor=cfg.scope) == "writes_and_reads"
        assert step_up_config.effective_scope(cfg, self.PRINCIPAL) == "writes_and_reads"

    def test_a_narrower_choice_cannot_go_below_the_configured_scope(self):
        cfg = step_up_config.StepUpConfig(enabled=True, scope="writes_and_pii_reads")
        assert step_up_config.set_personal_scope(self.PRINCIPAL, "writes", floor=cfg.scope) == "writes_and_pii_reads"
        # Stored as no choice at all, so it holds nothing back later.
        assert step_up_config.personal_scope(self.PRINCIPAL) is None
        assert step_up_config.effective_scope(cfg, self.PRINCIPAL) == "writes_and_pii_reads"

    def test_a_hand_written_narrower_preference_still_cannot_narrow(self):
        # Even a file that bypassed set_personal_scope only ever widens.
        from privacyfence import paths
        path = paths.authority_dir(self.PRINCIPAL) / step_up_config.STEP_UP_PREFERENCE_FILE_NAME
        path.write_text('{"scope": "writes"}', encoding="utf-8")
        cfg = step_up_config.StepUpConfig(enabled=True, scope="writes_and_reads")
        assert step_up_config.effective_scope(cfg, self.PRINCIPAL) == "writes_and_reads"

    def test_a_raised_configured_scope_overrides_an_older_narrower_choice(self):
        step_up_config.set_personal_scope(self.PRINCIPAL, "writes_and_pii_reads", floor="writes")
        cfg = step_up_config.StepUpConfig(enabled=True, scope="writes_and_reads")
        assert step_up_config.effective_scope(cfg, self.PRINCIPAL) == "writes_and_reads"

    def test_an_unknown_scope_is_refused(self):
        with pytest.raises(ValueError):
            step_up_config.set_personal_scope(self.PRINCIPAL, "nothing", floor="writes")

    def test_an_unreadable_preference_falls_back_to_the_configured_scope(self):
        from privacyfence import paths
        path = paths.authority_dir(self.PRINCIPAL) / step_up_config.STEP_UP_PREFERENCE_FILE_NAME
        path.write_text("not json", encoding="utf-8")
        assert step_up_config.personal_scope(self.PRINCIPAL) is None

    def test_choices_are_per_principal(self):
        other = Principal(id="p2")
        step_up_config.set_personal_scope(self.PRINCIPAL, "writes_and_reads", floor="writes")
        cfg = step_up_config.StepUpConfig(enabled=True, scope="writes")
        assert step_up_config.effective_scope(cfg, other) == "writes"

    def test_settings_fields(self):
        cfg = step_up_config.StepUpConfig(enabled=True, scope="writes")
        step_up_config.set_personal_scope(self.PRINCIPAL, "writes_and_reads", floor=cfg.scope)
        assert step_up_config.step_up_scope_fields(cfg, self.PRINCIPAL) == {
            "step_up_scope_active": True, "step_up_scope_floor": "writes", "step_up_scope": "writes_and_reads",
        }
        assert step_up_config.step_up_scope_fields(None, self.PRINCIPAL)["step_up_scope_active"] is False
