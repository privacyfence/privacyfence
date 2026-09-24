# ADR 0040: Telegram app credentials ship in every distribution, including PyPI

## Status

Accepted — 2026-09-24. Implemented by the `[cleanup P4]` pull request.

## Context

Telegram's `api_id`/`api_hash` identify the PrivacyFence *application* to Telegram's API, not a
user or an organization: every install uses the same pair, and a user still signs in with their
own phone number, code and optional 2FA (`src/privacyfence/app_credentials.py`). The repository
is public, so the pair lives only in the `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` repository secrets
and is written into the git-ignored `src/privacyfence/_telegram_credentials.py` at build time.

Until now only the DMG, the Windows installer and the `.deb` did that (`build.yml`'s `build`,
`build-windows` and `build-deb` jobs, through `build_dmg.sh`/`build_installer.ps1`/`build_deb.sh`).
`publish-pypi.yml` did not, and even if it had, `setuptools_scm`'s file finder packages only
tracked files, so the module would not have reached the sdist or the wheel. A PyPI install
therefore had no Telegram support unless the user obtained and exported their own pair.

The pair is already recoverable from the three packaged builds: it sits in the bundled Python
bytecode, readable by anyone who unpacks the installer. Putting it in the sdist/wheel makes it
readable with less effort (plain source in a public archive), not readable for the first time.

## Decision

1. Every distribution PrivacyFence publishes ships the Telegram app credentials, **the PyPI
   sdist and wheel included** — maintainer decision G4, approved 2026-09-24.
2. One generator writes the module: `scripts/telegram_credentials.py write`, called by
   `build_dmg.sh`, `build_deb.sh`, `build_installer.ps1` and `publish-pypi.yml`'s `build` job.
   Without both secrets it deletes any stale copy and the build proceeds without Telegram, so
   local and fork builds keep working.
3. `MANIFEST.in` explicitly includes the git-ignored module, so it reaches the sdist and, since
   `python -m build` builds the wheel from the sdist, the wheel.
4. `publish-pypi.yml`'s `build` job inspects the built wheel and sdist
   (`scripts/telegram_credentials.py check-dist`) before any publish job can run: a stable tag
   without the module **fails**; any other build warns.

## Alternatives considered

- **Keep Telegram out of the PyPI build and document bring-your-own credentials.** Rejected: it
  leaves PyPI users with a connector that silently doesn't work, to protect a value the three
  installers already hand out. Registering a personal Telegram app is a real hurdle for the
  intended user.
- **Fetch the credentials at runtime from a PrivacyFence-operated endpoint.** Rejected: adds a
  network dependency and a service to run, and the pair is exactly as public once served.
- **Copy the existing heredoc into `publish-pypi.yml`.** Rejected in favour of one generator: four
  copies of the same file format drift, and the Python generator also rejects a non-integer
  `api_id` at build time rather than at a user's first Telegram sign-in.

## Consequences

- Telegram works on a PyPI install with no extra setup.
- The `api_hash` is readable as plain text in the public sdist/wheel. Accepted: it identifies the
  app, not a user, and grants no access to any account. If Telegram ever revokes the pair for
  misuse by a third party, every distribution needs a new release with a new pair — as it already
  would for the installers.
- A stable release cannot silently lose Telegram support on PyPI; the build fails instead.
- `publish-pypi.yml` now reads the two repository secrets. They are passed only to the one step
  that writes the module, not to the job.

## Verification

- `scripts/telegram_credentials.py` and `tests/unit/test_telegram_credentials.py`: the generator,
  the archive check, that the module stays git-ignored while `MANIFEST.in` includes it, and that
  every build script and `publish-pypi.yml` calls the one generator instead of carrying a copy.
- `.github/workflows/publish-pypi.yml`'s `Write Telegram app credentials` and
  `Check Telegram app credentials are packaged` steps. That workflow runs fully only on a tag push,
  so its first real exercise is the next alpha.

## Related

- `src/privacyfence/app_credentials.py` (the reader), `docs/telegram-setup.md`.
- Build jobs that already shipped the credentials: `.github/workflows/build.yml`.
