# Telegram setup

PrivacyFence reaches Telegram as **your own account** (through Telegram's MTProto user API), not as
a bot: your AI client sees the chats, groups and channels you are in. Telegram has no browser
sign-in for this kind of access, so connecting means entering your phone number, the code Telegram
sends you and, if you have one, your two-step verification password — in PrivacyFence's own page,
never in a terminal.

Unlike the other providers, Telegram needs **no administrator setup**: no app to register, no
redirect URL, and no section in the organization config bundle.

## What you need

- A Telegram account and access to the phone number or Telegram app that receives its sign-in
  codes.
- A PrivacyFence install. Every distribution — the macOS, Windows and Linux installers and the
  PyPI package — carries PrivacyFence's Telegram app credentials (see
  [ADR 0040](adr/0040-telegram-app-credentials-ship-in-every-distribution.md)).

## Register the app

Nothing to register. Telegram's `api_id` and `api_hash` identify the PrivacyFence application, not
a user or an organization; they are the same for everyone and ship inside PrivacyFence.

**Building PrivacyFence from source** is the one case without them. Register your own application
at [my.telegram.org/apps](https://my.telegram.org/apps) (**Create new application**, platform
**Desktop**) and either:

- set `PRIVACYFENCE_TELEGRAM_API_ID` and `PRIVACYFENCE_TELEGRAM_API_HASH` in the environment
  PrivacyFence runs in, or
- set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` and run `python3 scripts/telegram_credentials.py
  write` before packaging, which writes them into the build (the installer build scripts do this
  themselves). Without both variables it builds without Telegram.

Keep the values private: anyone holding them can act as that Telegram application, and abuse of it
can get it rate-limited or blocked for every user of the build.

## Values

| Item | Value |
|---|---|
| Local redirect | None — Telegram does not use a browser redirect |
| Org redirect | None |
| Scopes | None — the session has your account's full access; PrivacyFence's tools and approvals decide what is used |
| Bundle flags | None — Telegram is not part of `org_config.json` |
| Where the session is stored | `credentials/telegram.session` in PrivacyFence's data folder (see [platform-support.md](platform-support.md)); on an organization server, in each user's own folder on the server |

## Build and distribute the bundle

Nothing to add. Telegram is available on every install that carries the app credentials, with or
without an organization config bundle, and on an organization server without any Telegram-specific
setup.

## Users connect

The shared flow is described in [connecting-a-service.md](connecting-a-service.md). For Telegram:

**Desktop install:** **Settings → Connectors → Telegram → Authenticate…** opens a dialog in the
page.

1. Enter your phone number with its country code (for example `+1 555 000 0000`).
2. Enter the code Telegram sends — usually as a message from **Telegram** in your Telegram app, or
   by SMS if you have no app signed in.
3. If your account has two-step verification, enter your password.

**Organization server:** on `https://<your-server>/connect`, enter your phone number in the
**Telegram** box and click **Connect Telegram**, then the code, then your password if asked.
**Cancel** abandons the attempt. The session is saved on the server, for your account only.

You stay connected until the session expires or is ended — for example from **Settings → Devices**
in a Telegram app.

## Troubleshooting

**The pill says "App credentials missing", or Settings shows "Telegram app credentials are missing
from this build."** — this build carries no Telegram app credentials. Official installers and the
PyPI package always do; for a build from source, see [Register the app](#register-the-app). On an
organization server, the Telegram row then reads **Not set up by your organization**.

**Telegram shows "Not connected" after working before** — the session was ended (from another
device, or by Telegram). Click **Authenticate…** (desktop) or **Reconnect Telegram**
(organization server) and sign in again.

**`PHONE_CODE_INVALID` or `PHONE_CODE_EXPIRED`** — the code was mistyped or is too old. Start again
to get a new code.

**`PASSWORD_HASH_INVALID`** — wrong two-step verification password.

**`PHONE_NUMBER_BANNED` or `AUTH_KEY_UNREGISTERED`** — Telegram invalidated the session. Sign in
again; if that fails on a desktop install, delete `credentials/telegram.session` from PrivacyFence's
data folder (see [platform-support.md](platform-support.md); on a privilege-separated install that
needs administrator rights) and click **Authenticate…**. On an organization server, ask an
administrator to delete your session file on the server.

**The code never arrives by SMS** — Telegram sends it to your Telegram app when one is signed in.
Look for a message from **Telegram** there.
