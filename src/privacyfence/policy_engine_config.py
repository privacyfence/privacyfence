"""``PolicyEngineConfig`` (P3 of the policy v2 redesign): which auto-accept engine decides.

P3 lands ``policy/engine.py``/``policy/compat.py`` -- a v2 rule evaluator, and a compiler
that turns today's ``auto_accept_rules``/``auto_accept_grants`` config into v2 rules in
memory, with no on-disk format change. Nothing about *what* is trusted changes in this
phase; what changes is which of two evaluators gets to decide, and P3's own safety net
is explicit about the default: "Both engines run on every real call; the old one decides,
the new one logs" (a `WARNING` per disagreement, in ``gate.py``). This module is the one
switch that overrides that default -- ``policy.engine: v2`` makes the new engine
authoritative instead, with the old one now the one shadowed. Flipping back to ``v1`` is
the redesign's documented rollback for a v2 regression: no release needed, just an edit to
``settings.yaml`` and a hot reload.

Modeled directly on ``step_up_config.StepUpConfig``: a local-mode entry point reading
``config/settings.yaml``'s own ``policy:`` section, validated the same way (an unknown
value raises ``ConfigurationError`` rather than silently falling back). Org mode reads the
same section from its own per-principal ``settings.yaml`` (``cfg`` in
``daemon_main._load_principal_settings``, not the install-wide config -- ``policy.engine``
is a per-principal choice about that principal's own auto-accept rules, the same way the
rules themselves are per-principal), so there is no separate ``from_org_config``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .org_mode import ConfigurationError

PolicyEngineVersion = Literal["v1", "v2"]

DEFAULT_POLICY_ENGINE: PolicyEngineVersion = "v1"


@dataclass(frozen=True)
class PolicyEngineConfig:
    """Which auto-accept evaluator is authoritative. Defaults to ``"v1"`` -- the existing
    ``AutoAcceptEvaluator``, unchanged by P3 -- so installing this release changes nothing
    about what auto-accepts until an admin opts in to ``"v2"``."""

    engine: PolicyEngineVersion = DEFAULT_POLICY_ENGINE

    @staticmethod
    def from_local_config(config: dict[str, Any]) -> "PolicyEngineConfig":
        """``config`` is the already-loaded ``config/settings.yaml`` dict
        (``daemon_main.load_config()``'s result, or a per-principal equivalent in org
        mode), not a path."""
        raw = config.get("policy")
        raw = raw if isinstance(raw, dict) else {}
        engine = raw.get("engine", DEFAULT_POLICY_ENGINE)
        if engine not in ("v1", "v2"):
            raise ConfigurationError(
                f'config/settings.yaml\'s "policy"."engine" must be "v1" or "v2", got {engine!r}'
            )
        return PolicyEngineConfig(engine=engine)


__all__ = [
    "DEFAULT_POLICY_ENGINE",
    "PolicyEngineConfig",
    "PolicyEngineVersion",
]
