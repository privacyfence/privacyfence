# ADR 0237: The wheel bundles the changelog through a setup.py build hook

## Status

Accepted — 2026-10-27. Implemented.

## Context

Organization mode shows each person the release notes since the version they last used
([ADR 0238](0238-org-mode-shows-each-person-the-changes-since-the-version-they-last-saw.md)). The
server needs `CHANGELOG.md` at run time, but the built wheel held only files under
`src/privacyfence/`. The organization image installs that wheel, and so does the pip install in the
setup guide, so neither had the changelog. `CHANGELOG.md` has to stay at the repository root:
`scripts/changelog_section.py`, `.claude/toolkit.yaml`, `CONTRIBUTING.md` and GitHub's own
changelog link all expect it there. There was no `setup.py`; the build backend is
`setuptools.build_meta`, configured in `pyproject.toml`.

## Decision

- **A root `setup.py` holds one `build_py` subclass.** After the normal `build_py` step it copies
  the root `CHANGELOG.md` to `privacyfence/resources/CHANGELOG.md` inside the build directory.
  Everything else about the build stays in `pyproject.toml`.
- **The copy exists only in the built wheel**, never in the source tree. A source checkout reads the
  root file directly.
- **A missing `CHANGELOG.md` fails the build.** The sdist always carries it, so a build from an
  sdist works.
- **`setup()` runs only under `if __name__ == "__main__"`.** `setuptools.build_meta` executes
  `setup.py` as `__main__`, so the build is unaffected, and a unit test can import the file
  without running a build.
- PyInstaller builds (local mode) are unchanged: local mode does not show the page.

## Alternatives considered

- **A committed copy under `src/privacyfence/resources/`, kept in step by a test** — every
  changelog line would be written twice, and two parallel branches would conflict in two files.
- **A symlink in `resources/`** — the Windows checkout gets a text file unless `core.symlinks` is
  set, and setuptools' symlink handling in sdists is easy to get wrong.
- **`[tool.setuptools.data-files]`** — it installs outside the package (`<prefix>/share/…`), and
  finding it at run time depends on the install scheme (venv, `--user`, Docker).
- **Fetching the GitHub Release body at run time** — organization servers often have no egress to
  GitHub, and it would be a new outbound call from a privacy gateway. A pre-release has no body of
  its own.
- **Moving `CHANGELOG.md` into the package** — every tool, doc and convention expects it at the
  root.

## Consequences

- An installed PrivacyFence can read its own release notes with no network access.
- The build gains a `setup.py`. Packaging settings still belong in `pyproject.toml`; a change that
  adds build logic there must keep this hook working.
- The wheel grows by the size of the changelog (about 290 KB).

## Verification

- `setup.py`.
- `tests/unit/test_setup_py.py`: the hook's paths, and that it copies the file into the build
  directory.
- `tests/integration/test_wheel_bundles_changelog.py`: builds the real wheel and checks it carries
  the file.
- `tests/integration/test_org_docker_image.py`: `test_the_image_bundles_the_changelog`.

## Related

- [ADR 0023](0023-changelog-is-the-only-source-of-release-notes.md)
- [ADR 0238](0238-org-mode-shows-each-person-the-changes-since-the-version-they-last-saw.md)
