"""Tests for step_up_config.py: the mode-agnostic WebAuthn step-up config."""
from __future__ import annotations

import pytest

from privacyfence import org_mode, privilege_separation, step_up_config


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


class TestStepUpConfigFromLocalConfig:
    """Local mode's own entry point. Unlike org mode, ``rp_id``
    defaults to a real value (``localhost``) rather than an empty string --
    local mode's own embedded server always answers to it (ADR 0010), so
    ``/security`` is reachable on a fresh install with no step_up section at
    all, well before ``enabled`` is ever turned on."""

    def test_absent_section_is_disabled_with_local_defaults(self):
        config = step_up_config.StepUpConfig.from_local_config({})
        assert config.enabled is False
        assert config.scope == "writes_and_pii_reads"
        assert config.scope == step_up_config.DEFAULT_STEP_UP_SCOPE
        assert config.rp_id == step_up_config.DEFAULT_LOCAL_RP_ID
        assert config.rp_id == "localhost"
        assert config.rp_name == step_up_config.DEFAULT_RP_NAME
        assert config.require_passkey is False

    def test_explicit_rp_id_overrides_the_local_default(self):
        config = step_up_config.StepUpConfig.from_local_config({"step_up": {"rp_id": "pf.local"}})
        assert config.rp_id == "pf.local"

    def test_enabled_scope_and_require_passkey(self, monkeypatch):
        # require_passkey needs a separated install (ADR 0003, "Why not gate
        # the passkey instead") or the developer escape hatch -- this test is
        # about scope/require_passkey parsing, not about that gate, so use
        # the latter rather than standing up a fake separated layout.
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        config = step_up_config.StepUpConfig.from_local_config({
            "step_up": {"enabled": True, "scope": "writes_and_pii_reads", "require_passkey": True},
        })
        assert config.enabled is True
        assert config.scope == "writes_and_pii_reads"
        assert config.require_passkey is True

    def test_invalid_scope_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            step_up_config.StepUpConfig.from_local_config({"step_up": {"scope": "everything"}})

    def test_the_widest_scope_is_accepted(self):
        config = step_up_config.StepUpConfig.from_local_config({"step_up": {"scope": "writes_and_reads"}})
        assert config.scope == "writes_and_reads"

    def test_the_rejection_message_names_every_accepted_scope(self):
        with pytest.raises(org_mode.ConfigurationError) as exc:
            step_up_config.StepUpConfig.from_local_config({"step_up": {"scope": "everything"}})
        for name in step_up_config.STEP_UP_SCOPES:
            assert f'"{name}"' in str(exc.value)

    def test_batch_defaults_to_single_assertion_and_reads_per_item(self):
        assert step_up_config.StepUpConfig.from_local_config({}).batch == "single_assertion"
        assert step_up_config.StepUpConfig.from_local_config(
            {"step_up": {"batch": "per_item"}},
        ).batch == "per_item"

    def test_invalid_batch_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            step_up_config.StepUpConfig.from_local_config({"step_up": {"batch": "something_else"}})

    def test_non_dict_section_is_treated_as_absent(self):
        config = step_up_config.StepUpConfig.from_local_config({"step_up": "nonsense"})
        assert config.rp_id == "localhost"
        assert config.enabled is False


class TestRequirePasskeyNeedsSeparation:
    """ADR 0003, "Why not gate the passkey instead, and leave the installs
    alone": kept anyway as a consequence of decision 1 -- unreachable on a
    shipped install (enforce_separation() already refused to start a
    packaged, unseparated one before this is ever parsed), and it catches
    exactly the developer path: a non-packaged local-mode checkout with
    require_passkey: true but no service account behind it."""

    def test_refuses_when_unseparated_and_no_override(self):
        with pytest.raises(org_mode.ConfigurationError, match="not privilege-separated"):
            step_up_config.StepUpConfig.from_local_config(
                {"step_up": {"enabled": True, "require_passkey": True}},
            )

    def test_allowed_when_unseparated_but_dev_override_set(self, monkeypatch):
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        config = step_up_config.StepUpConfig.from_local_config(
            {"step_up": {"enabled": True, "require_passkey": True}},
        )
        assert config.require_passkey is True

    def test_dev_override_of_0_does_not_count(self, monkeypatch):
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "0")
        with pytest.raises(org_mode.ConfigurationError):
            step_up_config.StepUpConfig.from_local_config(
                {"step_up": {"enabled": True, "require_passkey": True}},
            )

    def test_allowed_when_actually_separated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        config = step_up_config.StepUpConfig.from_local_config(
            {"step_up": {"enabled": True, "require_passkey": True}},
        )
        assert config.require_passkey is True

    def test_false_require_passkey_never_needs_separation(self):
        config = step_up_config.StepUpConfig.from_local_config(
            {"step_up": {"enabled": True, "require_passkey": False}},
        )
        assert config.require_passkey is False


class TestDefaultLocalStepUp:
    """The default ADR 0003's amendment under *Out of scope* records. The
    whole decision is "is this a build
    ADR 0003 already guarantees is privilege-separated?", because a passkey
    checked against a credential store the agent can write is a checkbox a
    local process ticks for itself (ADR 0002 decision 6)."""

    @pytest.fixture
    def packaged(self, monkeypatch):
        monkeypatch.setattr(step_up_config.paths, "is_bundled", lambda: True)

    def test_off_on_a_source_checkout(self):
        # Nothing separates a checkout, an editable install or a `pipx
        # install privacyfence`, so nothing here may turn this on.
        assert step_up_config.default_local_step_up() is False

    def test_on_for_a_packaged_separated_install(self, packaged, monkeypatch):
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        assert step_up_config.default_local_step_up() is True

    def test_on_for_a_packaged_install_with_the_dev_override(self, packaged, monkeypatch):
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        assert step_up_config.default_local_step_up() is True

    def test_off_for_a_packaged_install_that_is_somehow_unseparated(self, packaged, monkeypatch, caplog):
        # Unreachable on a real shipped install -- enforce_separation() has
        # already refused to serve one by the time any config is read -- and
        # deliberately not an error anyway: a *default* that could fail a
        # daemon's boot would turn an unexpected packaging state into an
        # install nobody can start.
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: False)
        with caplog.at_level("WARNING"):
            assert step_up_config.default_local_step_up() is False
        assert "not privilege-separated" in caplog.text


class TestPackagedLocalDefaults:
    """Plan item 1.1, through ``from_local_config``: the same decision as it
    is actually reached, plus the two things that must not change with it --
    an explicit value still wins in both directions, and a hand-written
    ``require_passkey: true`` an install cannot back is still refused
    outright rather than quietly downgraded."""

    @pytest.fixture
    def packaged_and_separated(self, monkeypatch):
        monkeypatch.setattr(step_up_config.paths, "is_bundled", lambda: True)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)

    def test_a_fresh_packaged_install_requires_a_passkey(self, packaged_and_separated):
        config = step_up_config.StepUpConfig.from_local_config({})
        assert (config.enabled, config.require_passkey) == (True, True)

    def test_a_packaged_install_with_the_key_absent_from_an_existing_section(self, packaged_and_separated):
        # A settings.yaml that has a step_up: section but said nothing about
        # these two -- the shape settings.yaml.example now seeds.
        config = step_up_config.StepUpConfig.from_local_config({"step_up": {"rp_name": "PrivacyFence"}})
        assert (config.enabled, config.require_passkey) == (True, True)

    def test_an_explicit_false_still_wins_on_a_packaged_install(self, packaged_and_separated):
        # Every install seeded from a pre-1.1 example has both keys written
        # out as false -- an upgrade must not silently flip those on, least
        # of all on an install with nothing enrolled, where it would block
        # every approval at the next restart.
        config = step_up_config.StepUpConfig.from_local_config(
            {"step_up": {"enabled": False, "require_passkey": False}},
        )
        assert (config.enabled, config.require_passkey) == (False, False)

    def test_a_defaulted_on_value_never_fails_the_boot(self, monkeypatch):
        # Packaged, but unseparated: default_local_step_up() answers False,
        # so from_local_config() returns an off config rather than raising
        # the ConfigurationError an explicit `require_passkey: true` gets.
        monkeypatch.setattr(step_up_config.paths, "is_bundled", lambda: True)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: False)
        config = step_up_config.StepUpConfig.from_local_config({})
        assert (config.enabled, config.require_passkey) == (False, False)

    def test_an_explicit_true_on_an_unseparated_install_still_raises(self, monkeypatch):
        monkeypatch.setattr(step_up_config.paths, "is_bundled", lambda: True)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: False)
        with pytest.raises(org_mode.ConfigurationError):
            step_up_config.StepUpConfig.from_local_config({"step_up": {"require_passkey": True}})

    def test_org_mode_defaults_are_untouched(self, packaged_and_separated):
        # is_bundled() is about how *this* process was built, and org mode
        # has an administrator writing the bundle -- an unstated opinion
        # there stays unstated. Guards against wiring the local default into
        # the shared dataclass by accident.
        config = step_up_config.StepUpConfig.from_org_config({})
        assert (config.enabled, config.require_passkey) == (False, False)


class TestLocalEnrollmentBanner:
    """The loud, persistent "passkey required" banner -- fires only in the one
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


class TestOffNotice:
    """B23 of the 4.1.0 action plan: the fallback for the state neither of
    the other two banners cover -- an install that has simply never turned
    step-up on. Fires exactly when step-up is *not* actually "required"
    (the same ``enabled and require_passkey`` pairing ``observe_step_up_
    requirement`` uses), regardless of ``has_credentials`` -- unlike
    ``local_enrollment_banner`` this doesn't care whether a passkey is
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
    """B9: ``LiveStepUpConfig`` mirrors every read a plain ``StepUpConfig``
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

    def test_local_enrollment_banner_reflects_the_current_value(self):
        live = step_up_config.LiveStepUpConfig(step_up_config.StepUpConfig(enabled=False, require_passkey=False))
        assert live.local_enrollment_banner(has_credentials=False) is None
        live.update(step_up_config.StepUpConfig(enabled=True, require_passkey=True))
        banner = live.local_enrollment_banner(has_credentials=False)
        assert banner is not None
        assert "/security" in banner

    def test_off_notice_reflects_the_current_value(self):
        live = step_up_config.LiveStepUpConfig(step_up_config.StepUpConfig())
        assert live.off_notice() is not None
        live.update(step_up_config.StepUpConfig(enabled=True, require_passkey=True))
        assert live.off_notice() is None
