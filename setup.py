"""Build hook: copies the repository's CHANGELOG.md into the built package as
privacyfence/resources/CHANGELOG.md, so an installed PrivacyFence can show its own release notes
(organization mode's What's new page, src/privacyfence/whats_new.py; ADR 0237).

Everything else about the build is in pyproject.toml. CHANGELOG.md stays at the repository root,
where every other tool expects it. The copy happens in build_py, so it exists only in the built
wheel, never in the source tree. A source checkout reads the root file directly instead.
"""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

CHANGELOG = Path(__file__).resolve().parent / "CHANGELOG.md"
BUNDLED_CHANGELOG = Path("privacyfence") / "resources" / "CHANGELOG.md"


class BuildPyWithChangelog(build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / BUNDLED_CHANGELOG
        self.mkpath(str(target.parent))
        self.copy_file(str(CHANGELOG), str(target))


if __name__ == "__main__":
    setup(cmdclass={"build_py": BuildPyWithChangelog})
