"""Policy v2 vocabulary -- see the "Scope and Verbs" redesign proposal.

This package is being built up in independently-reviewable phases (P0-P9); each phase's exit
criterion is "no behaviour change" until the engine swap (P3) lands, and even then the change is
opt-in (`policy.engine: v2`, default `v1`) until a later phase flips the default. `gate.py` (P3,
shadow-mode evaluation), `daemon_main.py` (P4, the one-time on-disk migration),
`settings_controller.py`/`web/routes_settings.py` (P4, the post-migration Settings notice) are the
first consumers outside this package and its tests.

P5 (`policy/propose.py`, `policy/describe.py`) lands the one writer both "Always allow" surfaces
will share -- the scope catalogue that replaces the five v1 suggestion tables, and the rendering
that lets a surface state a rule's width rather than leave it to be inferred. Neither is consumed
outside `tests/` yet: `gate.py`'s two suggestion call sites switch to them when P6 reworks the
surfaces that can render a widening chip.
"""
from __future__ import annotations
