"""docs/configuration-reference.md must name every setting a person can find in the shipped files.

The configuration reference is hand-written (each key needs prose: what it does, which Settings
control writes it, how the code default and the seeded value differ), so it cannot be generated the
way docs/tools-reference.md is. What can drift is coverage: a key added to
src/privacyfence/resources/settings.yaml.example, or an option added to
scripts/build_org_bundle.py, that nobody documents. A person who meets that key in their own
settings.yaml, or in `build_org_bundle.py --help`, then finds nothing in the reference. These tests
fail in that case.

settings.yaml.example is flattened to dotted key paths (`web.notifications.detail`). Three
conventions keep that honest without listing every possible value:

- Keys that the example ships commented out (`# enabled: false` under `step_up:`) are real,
  documented settings too, so commented-out `key: value` lines are uncommented and parsed with the
  rest. Prose comment lines that happen to look like `word: ...` are listed in
  _COMMENT_FALSE_POSITIVES.
- A list of mappings (`auto_accept.rules`) contributes `auto_accept.rules` plus one
  `auto_accept.rules[].<field>` path per field of its items. Lists of scalars stop at the list.
- A map whose keys are chosen by the person rather than by PrivacyFence (`connectors`,
  `agent_overrides`) is documented with a placeholder (`connectors.<name>.enabled`); the example's
  concrete key is replaced with that placeholder before the check (_DYNAMIC_MAPS).

build_org_bundle.py's options are read from its own argparse parser (`build_parser()`), so every
long option, including both halves of a --x/--no-x pair, must appear in the doc.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_ROOT / "docs" / "configuration-reference.md"
EXAMPLE_PATH = REPO_ROOT / "src" / "privacyfence" / "resources" / "settings.yaml.example"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_org_bundle  # noqa: E402

# Top-level (or nested) prose comments in the example that the commented-key pattern below also
# matches. Each is the first word of a wrapped sentence, not a setting.
_COMMENT_FALSE_POSITIVES: frozenset[str] = frozenset({"own"})

# Maps whose keys are the person's own choice. Value: the placeholder the doc uses for that key.
_DYNAMIC_MAPS: dict[str, str] = {
    "connectors": "<name>",
    "agent_overrides": "<client name>",
}

_COMMENTED_KEY_RE = re.compile(r"^(?P<indent>\s*)#(?P<pad> +)(?P<body>[a-z_][a-z0-9_-]*:(?:\s.*)?)$")


def _uncommented_example() -> str:
    """The example with every commented-out ``key: value`` line turned back into YAML and every
    other comment line dropped. A commented key's indentation is the comment's own indentation
    plus the spaces after ``#`` minus one, which is how the example writes nested keys
    (``#   telegram: ...`` under ``# connectors:``)."""
    out: list[str] = []
    for line in EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        match = _COMMENTED_KEY_RE.match(line)
        if match:
            indent = len(match["indent"]) + len(match["pad"]) - 1
            out.append(" " * indent + match["body"])
        elif line.lstrip().startswith("#"):
            continue
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _flatten(value: Any, prefix: str) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            segment = str(key)
            if prefix in _DYNAMIC_MAPS:
                segment = _DYNAMIC_MAPS[prefix]
            path = f"{prefix}.{segment}" if prefix else segment
            paths.append(path)
            paths.extend(_flatten(child, path))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                paths.extend(_flatten(item, f"{prefix}[]"))
    return paths


def _example_key_paths() -> set[str]:
    real = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    with_commented = yaml.safe_load(_uncommented_example())
    paths = set(_flatten(real, "")) | set(_flatten(with_commented, ""))
    return {p for p in paths if p.split(".", 1)[0].split("[", 1)[0] not in _COMMENT_FALSE_POSITIVES}


def _is_section(path: str, all_paths: set[str]) -> bool:
    return any(other.startswith(path + ".") for other in all_paths)


def test_flattening_sees_commented_out_and_nested_keys():
    paths = _example_key_paths()
    # Real, nested keys.
    assert "web.notifications.detail" in paths
    assert "privacy.categories.body" in paths
    # Commented out in the example.
    assert "step_up.require_passkey" in paths
    assert "file_bridge.max_download_bytes" in paths
    # List items and person-chosen map keys.
    assert "auto_accept.rules[].predicate" in paths
    assert "connectors.<name>.enabled" in paths
    assert "agent_overrides.<client name>" in paths
    assert not any(p.startswith("own") for p in paths)


def test_every_settings_yaml_example_key_is_documented():
    doc = DOC_PATH.read_text(encoding="utf-8")
    paths = _example_key_paths()
    # A section heading key (`web`, `web.notifications`) is covered by its leaves; every leaf must
    # appear literally, as a code span.
    leaves = sorted(p for p in paths if not _is_section(p, paths))
    missing = [p for p in leaves if f"`{p}`" not in doc]
    assert missing == [], f"docs/configuration-reference.md does not document: {missing}"


def _bundle_options() -> list[str]:
    parser = build_org_bundle.build_parser()
    options: list[str] = []
    for action in parser._actions:
        for option in action.option_strings:
            if option.startswith("--") and option != "--help":
                options.append(option)
    return options


def test_build_org_bundle_options_are_read_from_its_parser():
    options = _bundle_options()
    assert "--server-bind-host" in options
    assert "--no-agent-links" in options
    assert len(options) > 40


def test_every_build_org_bundle_option_is_documented():
    doc = DOC_PATH.read_text(encoding="utf-8")
    # An option counts as documented when it appears as the start of a code span or right after a
    # separator inside one (`--slack-client-id`, `--slack-client-secret`).
    missing = [o for o in _bundle_options() if not re.search(rf"`{re.escape(o)}[` =]", doc)]
    assert missing == [], f"docs/configuration-reference.md does not document: {missing}"
