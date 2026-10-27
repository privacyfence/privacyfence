# ADR 0238: Org mode shows each person the changes since the version they last saw

## Status

Accepted — 2026-10-27. Implemented.

## Context

An organization upgrades its PrivacyFence server for everyone at once. Nothing told a person what
changed. One person uses several browsers and devices, so a per-browser "seen" flag (the existing
dismissible notice) would show the same news again on each of them. The wheel now carries the
changelog
([ADR 0237](0237-the-wheel-bundles-the-changelog-through-a-setup-py-build-hook.md)).

## Decision

- **After an upgrade, each person sees a strip** on `/approvals`, `/settings` and `/connect`:
  "PrivacyFence was updated to X. See what's new". It links to `/whats-new`, which lists every
  changelog section released since the version that person last saw, newest first (at most ten).
- **The seen version is stored per principal on the server**, in `users/<id>/whats_new.json`, so it
  holds across browsers and devices. It never moves backwards, so a downgrade does not make old
  news appear again.
- **A person seen for the first time gets no news.** Their first page view records the running
  version. People who already exist when this ships have no file and count as first-seen, so they
  see nothing for the release that introduces the feature.
- **Closing the strip posts to `/api/whats-new/seen`; opening `/whats-new` also marks the notes
  seen.** Both record the server's running version, never one the client sends. The POST needs a
  session, a matching Origin and the CSRF token. Both routes are classified as ADR 0014 requires.
- **`GET /whats-new` changes state.** A cross-site top-level navigation to it can mark a person's
  notes as seen. The only effect is a strip that does not show. This is accepted.
- **Development builds re-announce.** Every newer `.devN` build counts as newer and shows
  `[Unreleased]` again. Accepted: production runs tagged releases. A pre-release shows its
  `[Unreleased]` entries.
- **Nothing is announced without a bundled changelog**, and the stored version is then left as it
  is, so a later version that has one still announces.
- **Reading `CHANGELOG.md` for a known version is allowed.** ADR 0023 and rule 2 of the changelog's
  own comment forbid parsing it to *determine* a version; this reads a section *for* the version
  `setuptools_scm` already gave. Local mode mounts none of this.

## Alternatives considered

- **`localStorage`, as the existing dismissible notice does** — per browser, not per person, so
  the news returns on every other browser and device.
- **A modal over the approval queue** — it blocks the page people open PrivacyFence to use.
- **Only the running version's section** — a person who skipped releases would miss changes.
- **Local mode too** — a single user already sees the update flow of the desktop app, and the
  bundled changelog is not part of PyInstaller builds.

## Consequences

- A person sees each upgrade's news once, wherever they sign in next.
- `users/<id>/whats_new.json` is new per-person data; the setup guide's backup table lists it.
- A failure to read or write the seen state never fails a page; it is logged and the strip is
  skipped or repeated.

## Verification

- `src/privacyfence/whats_new.py` and `tests/unit/test_whats_new.py`: parsing, version selection
  and the seen state.
- `src/privacyfence/web/routes_whats_new.py` and `tests/unit/web/test_routes_whats_new.py`: the
  page, the seen endpoint and the strip on the org pages.
- `tests/unit/test_web_shell.py`: the strip markup and its script.

## Related

- [ADR 0014](0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md)
- [ADR 0023](0023-changelog-is-the-only-source-of-release-notes.md)
- [ADR 0237](0237-the-wheel-bundles-the-changelog-through-a-setup-py-build-hook.md)
