# Telegram setup

PrivacyFence reaches Telegram as **your own account** (through Telegram's MTProto user API), not as
a bot: your AI client sees the chats, groups and channels you are in. Telegram has no browser
sign-in for this kind of access, so connecting means entering your phone number, the code Telegram
sends you and, if you have one, your two-step verification password — in PrivacyFence's own page,
never in a terminal.

Unlike the other providers, Telegram needs **no administrator setup**: no app to register, no
redirect URL, and no section in the organization config bundle. The only switch is the Telegram row
on the Admin page.

## What you need

- A Telegram account and access to the phone number or Telegram app that receives its sign-in
  codes.
- A PrivacyFence server. The Docker image carries PrivacyFence's Telegram app credentials (see
  [ADR 0040](adr/0040-telegram-app-credentials-ship-in-every-distribution.md)).

## Register the app

Nothing to register. Telegram's `api_id` and `api_hash` identify the PrivacyFence application, not
a user or an organization; they are the same for everyone and ship inside PrivacyFence.

**Building the image from source** is the one case without them. Register your own application
at [my.telegram.org/apps](https://my.telegram.org/apps) (**Create new application**) and either:

- set `PRIVACYFENCE_TELEGRAM_API_ID` and `PRIVACYFENCE_TELEGRAM_API_HASH` in the environment
  PrivacyFence runs in, or
- set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` and run `python3 scripts/telegram_credentials.py
  write` before building the wheel, which writes them into the build. Without both variables it
  builds without Telegram.

Keep the values private: anyone holding them can act as that Telegram application, and abuse of it
can get it rate-limited or blocked for every user of the build.

## Values

| Item | Value |
|---|---|
| Redirect | None — Telegram does not use a browser redirect |
| Scopes | None — the session has your account's full access; PrivacyFence's tools and approvals decide what is used |
| Switch | **Admin → Services → Telegram** (offer or stop offering it). The old bundle flag `--enable-telegram` is refused. |
| Where the session is stored | In each user's own folder in the server's data volume |

## Offer Telegram

Telegram is offered or not on **Admin → Services**, with the switch on its row (administrators only;
offering a service asks for a passkey, stopping it does not). A fresh Docker single-user install
offers it already, and a new multi-user server offers nothing until an administrator turns
services on. Until it is offered, Telegram is not listed on
`/connect`, even in a build that carries the app credentials
([ADR 0196](adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md),
[ADR 0257](adr/0257-operational-settings-live-in-a-daemon-written-admin-config.md)).

## Users connect

The shared flow is described in [connecting-a-service.md](connecting-a-service.md). For Telegram:

On `https://<your-server>/connect`, enter your phone number with its country code (for example
`+1 555 000 0000`) in the **Telegram** box and click **Connect Telegram**. Then enter the code
Telegram sends — usually as a message from **Telegram** in your Telegram app, or by SMS if you have
no app signed in — and your two-step verification password if your account has one. **Cancel**
abandons the attempt. The session is saved on the server, for your account only.

You stay connected until the session expires or is ended — for example from **Settings → Devices**
in a Telegram app.

## Troubleshooting

**Telegram app credentials are missing from this build** — the image carries no Telegram app
credentials. The official image always has them; for a build from source, see
[Register the app](#register-the-app). The Telegram row then reads **Not set up by your
organization**.

**Telegram shows "Not connected" after working before** — the session was ended (from another
device, or by Telegram). Click **Reconnect Telegram** and sign in again.

**`PHONE_CODE_INVALID` or `PHONE_CODE_EXPIRED`** — the code was mistyped or is too old. Start again
to get a new code.

**`PASSWORD_HASH_INVALID`** — wrong two-step verification password.

**`PHONE_NUMBER_BANNED` or `AUTH_KEY_UNREGISTERED`** — Telegram invalidated the session. Sign in
again; if that fails, ask an administrator to delete your session file on the server.

**The code never arrives by SMS** — Telegram sends it to your Telegram app when one is signed in.
Look for a message from **Telegram** there.
