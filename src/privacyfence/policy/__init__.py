"""Policy v2 vocabulary -- see the "Scope and Verbs" redesign proposal.

This package is being built up in independently-reviewable phases (P0-P9); each phase's exit
criterion is "no behaviour change" until the engine swap (P3) lands, and even then the change is
opt-in (`policy.engine: v2`, default `v1`) until a later phase flips the default. `gate.py` (P3,
shadow-mode evaluation), `daemon_main.py` (P4, the one-time on-disk migration),
`settings_controller.py`/`web/routes_settings.py` (P4, the post-migration Settings notice) are the
first consumers outside this package and its tests.
"""
from __future__ import annotations
