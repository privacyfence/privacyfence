# Moving to organization mode

PrivacyFence 8 runs only in organization mode: as an organization server, or as the Docker image
on one computer (single-user mode). The desktop (local) mode, the install you get from the macOS,
Windows or Linux installer, is part of PrivacyFence 7 only. Desktop installs
keep working as they do today for as long as you stay on 7.x. This page is for people who want to
move to organization mode, and for anyone whose PrivacyFence 8 refused to start on a desktop data
directory.

## What a desktop install is

A desktop install runs as a service on your own computer and keeps its data in the PrivacyFence data
directory of that computer. It signs in to your services through a loopback address and connects to
your AI client with a local token. PrivacyFence 8 does not read that data.

## What happens when 8 starts on a desktop install

PrivacyFence 8 checks the data directory before it does anything else. On a desktop data directory it
stops and prints:

> This data directory ({root}) belongs to a PrivacyFence desktop install. PrivacyFence 8 runs only as
> an organization server or the Docker image and does not read desktop data. Keep using PrivacyFence 7
> on this computer, or move to organization mode:
> https://privacyfence.eu/docs/moving-to-organization-mode/

On a data directory that holds an organization server's data but was never prepared for 8, it stops
and prints:

> This data directory ({root}) has not been prepared for PrivacyFence 8. Install the latest
> PrivacyFence 7, start it once, check that the Admin page says "Ready for PrivacyFence 8: yes", then
> start this release again: https://privacyfence.eu/docs/moving-to-organization-mode/

Nothing in the data directory is changed in either case. Starting 7.x on it again works.

An organization server is recognised by its own data: `org/org_bundle.json`, or the 6.x `org/org_config.json`, saying `"mode": "org"`. A leftover `mcp_token` or `web_base_url` on such a server does not stop PrivacyFence 8, also after `--retire-bundle` has deleted the bundle ([ADR 0337](adr/0337-an-organization-server-is-recognised-by-its-own-data.md)).

A data directory that holds no data yet is a fresh install: PrivacyFence 8 writes `data_format.json` at its first start. That includes a directory whose only content is `org/admin_config.json` written before the first start, for example by a deployment tool that sets the `upstream_mcp` groups ([ADR 0317](adr/0317-an-org-directory-holding-only-admin-config-is-fresh.md)).

PrivacyFence 8 also refuses `PRIVACYFENCE_MODE=local`, and a bundle that says `"mode": "local"`.
Organization mode is the only mode: leave the setting out, or set it to `org`.

## Why desktop mode is not part of 8

PrivacyFence's built-in apps, the relay that lets them work on any server, the Admin page and
single-user mode all serve organization mode. Desktop mode cannot use the built-in apps: it signs in
through a loopback address on your computer, which an app shared by many organizations cannot use
with Slack, Miro or Atlassian. Keeping two products costs more with every feature, so organization
mode is the one that stays. The decision is recorded in
[ADR 0261](adr/0261-local-mode-is-deprecated-in-7-and-removed-in-8.md); the built-in apps are
[ADR 0255](adr/0255-built-in-oauth-apps-ship-in-every-distribution.md).

## Two ways forward

**The Docker image on this computer (single-user mode).** The organization image starts without a
configuration file, an identity provider or a public address, serves one administrator at
`http://localhost`, and uses the built-in apps for Google, Slack, Salesforce, Atlassian, Miro and
Telegram. You sign in with a setup code from the container's log and then a passkey. It is the
closest replacement for a desktop install, with the same one person on one computer. Read the
limits in [Security and compliance](security-and-compliance.md#single-user-mode) first: anyone who can
run `docker` on that computer controls the install. The steps are in the "Run it on this computer"
section of [Organization deployment with Docker](org-mode-docker.md#run-it-on-this-computer).

**An organization server.** One PrivacyFence server for a team, behind your identity provider and an
HTTPS address, reachable by claude.ai and ChatGPT as well as desktop clients. Follow
[Organization deployment](org-mode-setup-guide.md). Its administrators use the built-in apps, or
register their organization's own.

## Bringing your rules along

Your always-allow rules and step-up level move to the new install as a file: export them on the
desktop's **Settings → Auto-accept** page (in PrivacyFence 7) and import them on the new one.
Connections, passkeys and scoped tokens do not move; connect your accounts again and enroll a
passkey. The steps and the full list of what does and does not move are in
[Moving a person from a desktop install](org-mode-setup-guide.md#moving-a-person-from-a-desktop-install).

## Before upgrading to 8.x

PrivacyFence 8 does not convert data written by releases before 7. So upgrade to 8.x only from 7.x,
and first:

1. Install the latest 7.x and start it once. Every start brings everyone's stored data up to date and
   records `data_format.json` in the data directory.
2. Open **Admin** and check the line at the bottom of the page: **Ready for PrivacyFence 8: yes.** If
   it says **not yet**, it names the problems found at the last start; fix them and restart. The same
   problems are logged at every start as `Not ready for PrivacyFence 8: …`.

Details are in [ADR 0262](adr/0262-7x-is-the-migration-checkpoint-for-8x.md).

## Going back to 6.x

The upgrade from 6.x to 7.x can be undone on an organization server. 7.x reads `org/org_bundle.json`,
copied once from `org_config.json`, and never changes the 6.x files, so the latest 6.x (6.10.0 or
newer) starts again with the configuration it had. What does not go back: changes made on the Admin
page, bundles installed under 7.x, connections made through the relay, and passkeys or connections
made in single-user mode. See
[ADR 0263](adr/0263-the-6-to-7-upgrade-is-reversible.md).
