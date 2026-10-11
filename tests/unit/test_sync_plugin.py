"""The privacyfence-sync Claude plugin: its files, its pinned README copy and its release build."""

from __future__ import annotations

import importlib.util
import json
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins/privacyfence-sync"
SKILL_DIR = PLUGIN / "skills/privacyfence-sync-recipes"
README_COPY = SKILL_DIR / "references/README.md"


def _load_build_script():
    spec = importlib.util.spec_from_file_location("build_sync_plugin", ROOT / "scripts/build_sync_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reference_readme_matches_sync_readme():
    assert README_COPY.read_bytes() == (ROOT / "sync/README.md").read_bytes()


def test_marketplace_lists_the_plugin_at_its_path():
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text("utf-8"))
    entries = {p["name"]: p for p in marketplace["plugins"]}
    assert marketplace["name"] == "privacyfence"
    assert entries["privacyfence-sync"]["source"] == "./plugins/privacyfence-sync"
    assert (ROOT / entries["privacyfence-sync"]["source"] / ".claude-plugin/plugin.json").is_file()


def test_plugin_json_has_no_version_in_the_repo():
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text("utf-8"))
    assert manifest["name"] == "privacyfence-sync"
    assert "version" not in manifest


def test_skill_front_matter_has_name_and_description():
    lines = (SKILL_DIR / "SKILL.md").read_text("utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    fields = dict(line.split(": ", 1) for line in lines[1:end])
    assert fields["name"] == SKILL_DIR.name
    assert fields["description"].strip()


def test_build_script_stamps_the_version_and_zips_the_plugin_root(tmp_path):
    out = tmp_path / "out" / "plugin.zip"
    assert _load_build_script().main(["--version", "9.8.7", "--out", str(out)]) == 0
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(".claude-plugin/plugin.json"))
    assert manifest["version"] == "9.8.7"
    assert "skills/privacyfence-sync-recipes/SKILL.md" in names
    assert "skills/privacyfence-sync-recipes/references/README.md" in names
    assert not any(n.startswith("plugins/") for n in names)


def test_build_script_refuses_a_stale_readme_copy(tmp_path, capsys):
    root = tmp_path / "repo"
    (root / "sync").mkdir(parents=True)
    shutil.copy(ROOT / "sync/README.md", root / "sync/README.md")
    shutil.copytree(PLUGIN, root / "plugins/privacyfence-sync")
    stale = root / "plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md"
    stale.write_text(stale.read_text("utf-8") + "\nedited\n", encoding="utf-8")
    out = tmp_path / "plugin.zip"
    rc = _load_build_script().main(["--version", "1.0.0", "--out", str(out), "--root", str(root)])
    assert rc == 1
    assert not out.exists()
    assert "differs" in capsys.readouterr().err
