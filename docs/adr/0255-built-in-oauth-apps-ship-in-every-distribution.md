# ADR 0255: Built-in OAuth apps ship in the server distributions

## Status

Accepted — 2026-10-30. Implemented: `scripts/builtin_apps.py`, `src/privacyfence/app_credentials.py`,
`src/privacyfence/oauth_apps.py`.
Relates to [ADR 0040](0040-telegram-app-credentials-ship-in-every-distribution.md): the same mechanism for more apps.

## Context

A fresh organization server offered no connector until an administrator had registered an OAuth app
with every provider they wanted. That is a day of console work per provider, and it is what stops
most small teams from trying PrivacyFence. Telegram already ships PrivacyFence's own application
credentials in the build ([ADR 0040](0040-telegram-app-credentials-ship-in-every-distribution.md)).

Three facts shape the decision for OAuth providers:

- A provider's client secret cannot stay secret in a distribution people install. The repository is
  public, and the secret is baked into the image or wheel. It identifies the PrivacyFence app and
  nothing else; every sign-in still needs the person's own consent.
- Slack, Miro and Atlassian refuse to distribute an app that has an `http` redirect, so a built-in
  app cannot use the loopback redirects a desktop install uses. Every built-in app therefore has only
  `https://oauth.privacyfence.eu/...` redirects ([ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)).
- Desktop (local) mode is deprecated in 7 and removed in 8 ([ADR 0261](0261-local-mode-is-deprecated-in-7-and-removed-in-8.md)),
  so it is not worth a second redirect path.

## Decision

- **Six built-in apps**, keyed `google_web`, `slack`, `salesforce`, `atlassian_relay`, `miro` and
  `google_signin` (the opt-in Google sign-in of [ADR 0267](0267-an-organization-server-may-opt-in-to-the-built-in-google-sign-in.md),
  kept in its own Google project). Each has a `client_id` and a `client_secret`.
- **They are baked in at build time.** `scripts/builtin_apps.py write` reads
  `PF_APP_<KEY>_CLIENT_ID` and `PF_APP_<KEY>_CLIENT_SECRET` from CI secrets and writes the
  git-ignored `src/privacyfence/_builtin_apps.py`. A key with only one of the two values is skipped
  with a warning. `check-dist --require` fails a release build that lacks the module, on every tag,
  pre-releases included.
- **They ship only in server distributions:** the organization image and the PyPI wheel. The desktop
  installers (`.dmg`, `.deb`, Windows) ship none, which keeps the secrets in fewer copies.
- **`app_credentials.builtin_app(key)`** reads the module, and falls back to the file named by
  `PRIVACYFENCE_BUILTIN_APPS` (the same JSON shape) for development.
- **They serve organization mode only, and always through the relay.** In
  `oauth_apps.credentials_source` a provider's app is, in organization mode, the stored connector
  config ([ADR 0269](0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md)),
  else the built-in app, else none; in local mode it is the bundle's section or none. A built-in
  section carries the `relay` marker that sends the sign-in through the relay.
- **Salesforce is a packaged External Client App.** Salesforce no longer lets an org create new
  Connected Apps, and an External Client App distributed as *Local* works only in the org that made
  it. So the built-in app is distributed as *Packaged*, in a second-generation managed package that
  each customer org installs once; the app's consumer key and secret stay in the packaging org.
  The package source, minus the global OAuth settings that hold the secret, is in `salesforce/package/`.
  The install link is the repository variable `PF_APP_SALESFORCE_INSTALL_URL`; `builtin_apps.py`
  stores it as the Salesforce entry's `install_url` when it is an `https://login.salesforce.com/`
  URL, and the Admin page shows it to send to the org's Salesforce administrator. The built-in app
  signs in at `login.salesforce.com`; an org that blocks that host sets its My Domain URL in the
  `login_url` setting, which accepts only Salesforce's own hosts so that an administrator cannot
  send people's Salesforce sign-in to another site.
- **Provider docs never tell anyone to trust or pre-approve the built-in app.** They say: do not mark
  the built-in PrivacyFence app as trusted or pre-approved, because that trust would extend to every
  PrivacyFence server and to anyone using the public app; to trust an app, register your own.
- **Rotation.** A built-in secret is rotated by adding the new secret next to the old one where the
  provider allows two (Google and Salesforce do), releasing, and retiring the old one once the
  release has been adopted. `docs/oauth-relay.md` has the steps.
- The provider accounts that own the apps are registered under PrivacyFence role addresses, never
  under a maintainer's personal sign-in.

## Alternatives considered

- **Fetch the credentials from a PrivacyFence endpoint at runtime.** Rejected: it makes PrivacyFence a
  runtime dependency of every connector, and hands the secret to anyone who calls the endpoint, which
  is no better than shipping it.
- **Let the relay broker the token exchange and hold the secrets.** Rejected: every access and refresh
  token would pass through PrivacyFence infrastructure. The relay forwards a one-time code and
  nothing more ([ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)).
- **Ship built-in apps in the desktop installers too.** Rejected: desktop mode ends in 8, and it would
  need loopback redirects the three providers above refuse to distribute.

## Consequences

- A fresh server can connect Google, Slack, Salesforce, Atlassian and Miro with no app registration.
- The secrets are public in effect. Because a refresh token is bound to the app that issued it, a
  stolen refresh token no longer also needs the organization's own client secret to be used.
- **Consent phishing.** Anyone who runs a server registered with the relay can show "PrivacyFence" on
  a provider's consent screen. Domain proof, notification to the maintainers and revocation bound
  this ([ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)); the docs
  tell people not to trust the app, so a trust grant cannot cover other servers.
- Until Google verifies the built-in Google app, people see an "unverified app" screen and the app is
  capped at 100 users in total across every install. Gmail and Drive scopes are restricted, so
  verification includes a yearly third-party security assessment. An organization that needs neither
  limit uses its own Google app, and an Internal app has no cap.
- Every Salesforce org's administrator installs the package once before its people can connect.
- Switching a provider between the built-in and an own app makes people reconnect Salesforce,
  Atlassian and Miro, whose refresh uses the current app's credentials.
- An organization that wants its own app can still have one: [ADR 0269](0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md).

## Verification

`tests/unit/test_builtin_apps_script.py`; `tests/unit/test_oauth_apps.py` (local mode never returns a
built-in app); `tests/unit/test_admin_config.py` (the Salesforce login URL). Code:
`scripts/builtin_apps.py`, `src/privacyfence/app_credentials.py`, `src/privacyfence/oauth_apps.py`,
`.github/workflows/build.yml` and `.github/workflows/publish-pypi.yml` (credential steps and
`check-dist`), `salesforce/package/`.

## Related

ADR 0040, 0259, 0261, 0267, 0269.
