"""Tests for scripts/mypy_strict_modules.py, and for the real config/workflow it depends on.

Imported by file path (importlib) rather than as a package, since scripts/ isn't part of the
installed ``privacyfence`` distribution -- same pattern as tests/unit/test_changelog_section.py.

Half of this file tests the parser against synthetic pyproject/src trees. The other half asserts
things about this repo's own `pyproject.toml` and `.github/workflows/tests.yml`, because the bug
this script exists to fix was entirely a property of those two files agreeing: five modules carried
`[[tool.mypy.overrides]]` blocks described as "promoted to blocking" while the only mypy step in CI
was `continue-on-error: true`, so nothing gated on them. A unit test of the parser alone would have
passed happily in that world.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "mypy_strict_modules.py"
_spec = importlib.util.spec_from_file_location("mypy_strict_modules", _SCRIPT_PATH)
mypy_strict_modules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mypy_strict_modules)

REAL_PYPROJECT = REPO_ROOT / "pyproject.toml"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _write_pyproject(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestPromotedModules:
    def test_reads_overrides_in_file_order(self, tmp_path):
        pyproject = _write_pyproject(
            tmp_path,
            """
[[tool.mypy.overrides]]
module = "pkg.b"
disallow_untyped_defs = true

[[tool.mypy.overrides]]
module = "pkg.a"
warn_return_any = true
""",
        )

        assert mypy_strict_modules.promoted_modules(pyproject) == ["pkg.b", "pkg.a"]

    def test_relaxing_override_is_not_a_promotion(self, tmp_path):
        # `ignore_missing_imports`/`ignore_errors` blocks are how mypy is told to care *less*
        # about a module. Treating one as a promotion would put a deliberately unchecked module
        # into the blocking run.
        pyproject = _write_pyproject(
            tmp_path,
            """
[[tool.mypy.overrides]]
module = "pkg.vendored"
ignore_errors = true
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = "pkg.opted_out"
disallow_untyped_defs = false
""",
        )

        assert mypy_strict_modules.promoted_modules(pyproject) == []

    def test_list_valued_module_key_is_expanded(self, tmp_path):
        # mypy accepts a list under `module`; this repo doesn't use that shape today, but a
        # future override written that way must not silently drop out of the gate.
        pyproject = _write_pyproject(
            tmp_path,
            """
[[tool.mypy.overrides]]
module = ["pkg.a", "pkg.b"]
strict_equality = true
""",
        )

        assert mypy_strict_modules.promoted_modules(pyproject) == ["pkg.a", "pkg.b"]

    def test_no_mypy_config_at_all(self, tmp_path):
        pyproject = _write_pyproject(tmp_path, '[project]\nname = "x"\n')

        assert mypy_strict_modules.promoted_modules(pyproject) == []


class TestModulePath:
    def test_resolves_module_and_package(self, tmp_path):
        src = tmp_path / "src"
        (src / "pkg" / "sub").mkdir(parents=True)
        (src / "pkg" / "mod.py").write_text("", encoding="utf-8")
        (src / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")

        assert mypy_strict_modules.module_path("pkg.mod", src) == src / "pkg" / "mod.py"
        assert (
            mypy_strict_modules.module_path("pkg.sub", src)
            == src / "pkg" / "sub" / "__init__.py"
        )

    def test_missing_module_is_an_error_not_a_silent_skip(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()

        with pytest.raises(mypy_strict_modules.ResolutionError) as excinfo:
            mypy_strict_modules.module_path("pkg.renamed_away", src)

        assert "renamed" in str(excinfo.value)

    def test_wildcard_is_rejected(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()

        with pytest.raises(mypy_strict_modules.ResolutionError) as excinfo:
            mypy_strict_modules.module_path("pkg.*", src)

        assert "wildcard" in str(excinfo.value)


class TestMain:
    def test_list_prints_repo_relative_paths(self, capsys):
        assert mypy_strict_modules.main(["--list"]) == 0

        lines = capsys.readouterr().out.split()
        assert lines, "the ratchet has promoted modules, so --list must print something"
        for line in lines:
            assert not Path(line).is_absolute()
            assert (REPO_ROOT / line).is_file()

    def test_unresolvable_module_exits_two_without_running_mypy(self, tmp_path, capsys):
        pyproject = _write_pyproject(
            tmp_path,
            """
[[tool.mypy.overrides]]
module = "privacyfence.module_that_does_not_exist"
disallow_untyped_defs = true
""",
        )

        assert mypy_strict_modules.main(["--pyproject", str(pyproject)]) == 2
        assert "module_that_does_not_exist" in capsys.readouterr().err


class TestRealRepo:
    """The half that would have caught the original "gates nothing" state."""

    def test_repo_has_promoted_modules_and_they_all_resolve(self):
        paths = mypy_strict_modules.promoted_paths()

        assert paths, (
            "no module is promoted to blocking any more -- if that is deliberate, the "
            "blocking mypy step in tests.yml and the ratchet's documentation need to go too"
        )
        for path in paths:
            assert path.is_file()

    def test_every_override_in_pyproject_is_accounted_for(self):
        # Guards the STRICTNESS_FLAGS list against an override that promotes a module using a
        # flag the script doesn't know about -- that module would look relaxed and fall out of
        # the blocking run with nothing to notice.
        data = tomllib.loads(REAL_PYPROJECT.read_text(encoding="utf-8"))
        overrides = data["tool"]["mypy"]["overrides"]
        promoted = set(mypy_strict_modules.promoted_modules())

        for override in overrides:
            module = override["module"]
            relaxes_only = not any(
                value is True
                for key, value in override.items()
                if key != "module" and key in mypy_strict_modules.STRICTNESS_FLAGS
            )
            assert (module in promoted) or relaxes_only, (
                f"{module} has an override this script reads as neither a promotion nor a "
                "relaxation -- add its flag to STRICTNESS_FLAGS"
            )

    def test_ci_runs_the_blocking_step(self):
        # The whole point: a blocking step, not a second `continue-on-error` one. Read as text
        # rather than parsed YAML so this test needs no PyYAML and still fails if someone adds
        # `continue-on-error: true` under the step.
        workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")

        assert "scripts/mypy_strict_modules.py" in workflow

        step_start = workflow.index("scripts/mypy_strict_modules.py")
        # Everything from the step's own `- name:` line to the next step's.
        block_start = workflow.rindex("      - name:", 0, step_start)
        next_step = workflow.find("      - name:", step_start)
        block = workflow[block_start:next_step if next_step != -1 else len(workflow)]

        assert "continue-on-error" not in block, (
            "the promoted-modules mypy step must be blocking -- a continue-on-error step is "
            "exactly the state this script was added to fix"
        )
