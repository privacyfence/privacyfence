"""Tests for policy_engine_config.py: the policy v2 engine switch (P3 of the policy v2 redesign)."""
from __future__ import annotations

import pytest

from privacyfence import org_mode, policy_engine_config


class TestPolicyEngineConfigFromLocalConfig:
    """Installing the P3 release changes nothing about what auto-accepts until an admin opts in
    -- an absent ``policy`` section, or one with no ``engine`` key, must resolve to ``"v1"``."""

    def test_absent_section_defaults_to_v1(self):
        config = policy_engine_config.PolicyEngineConfig.from_local_config({})
        assert config.engine == "v1"
        assert config.engine == policy_engine_config.DEFAULT_POLICY_ENGINE

    def test_non_dict_section_is_treated_as_absent(self):
        config = policy_engine_config.PolicyEngineConfig.from_local_config({"policy": "nonsense"})
        assert config.engine == "v1"

    def test_explicit_v1_is_accepted(self):
        config = policy_engine_config.PolicyEngineConfig.from_local_config({"policy": {"engine": "v1"}})
        assert config.engine == "v1"

    def test_explicit_v2_is_accepted(self):
        config = policy_engine_config.PolicyEngineConfig.from_local_config({"policy": {"engine": "v2"}})
        assert config.engine == "v2"

    def test_unknown_value_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            policy_engine_config.PolicyEngineConfig.from_local_config({"policy": {"engine": "v3"}})
