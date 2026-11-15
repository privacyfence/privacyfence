# Dev vs. live setup

How to run a development server next to a live one on the same machine without the two sharing
state. PrivacyFence has one mode, organization mode, and a single-user install is that mode with no
identity provider ([ADR 0301](adr/0301-desktop-mode-and-its-installers-are-removed-in-8.md)), so a
development server and a live one differ only in where they run and what they keep.

## Two servers, two data directories, two ports

A live server and a development server must not share a data directory or a port. A data directory
holds the passkey, the connected accounts, the rules, the audit log and the instance lock
(`privacyfence.lock`); a second process on the same directory stops on that lock before it reaches
the port, and one that got past it would overwrite the other's state.

| How PrivacyFence runs | Data directory (`paths.data_dir()`) |
|---|---|
| Docker image | the data volume (`/var/lib/privacyfence-org/.privacyfence` in the container); the volume is the only thing the container keeps |
| Source checkout or editable install (`pip install -e .`) | the checkout root (`config/`, `credentials/`, `logs/`, discovery files) |
| Non-editable `pip` install into a virtual environment | `~/.privacyfence` |

`src/privacyfence/paths.py` is authoritative. Two ways to keep a development server apart from a
live one:

1. **Run the live server as the image and develop from a checkout.** The image's data lives in its
   volume and the checkout's lives in the checkout, so they share nothing. Give the checkout its own
   port with `PRIVACYFENCE_PORT` (the image publishes 8765 by default; choose another for the
   development server, for example 8766) and open the development server at
   `http://localhost:<port>`: single-user mode accepts only requests addressed to `localhost`.
2. **Run both from the image.** Give each its own named volume and its own published port
   (`docker run … -p 127.0.0.1:8766:8766 -e PRIVACYFENCE_PORT=8766 -v pf-dev:/var/lib/privacyfence-org/.privacyfence …`),
   as in [Run it on this computer](org-mode-docker.md#run-it-on-this-computer).

A second checkout (a `git worktree`) has its own data directory, so two development servers need
only different ports.

## Running from a checkout

From the repository root, in a virtual environment with the editable install:

```bash
pip install -e ".[test]"
PRIVACYFENCE_PORT=8766 privacyfence-app
```

The first start prints a setup code, as the image does; open `http://localhost:8766/login`, enter
it and add a passkey. The data directory is the checkout root, which `.gitignore` keeps out of
commits. Delete `credentials/`, `config/` and `logs/` there to start from nothing.

Use dedicated QA accounts for connector work, never personal or production ones. See
[`connector-qa.md`](connector-qa.md).

## `PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP`

This variable is for organization-mode development against an identity provider that is served
over plain HTTP only (a devcontainer Keycloak, a loopback OIDC test server). It is never for a real
deployment. It counts as set when its value is anything other than empty, `0`, `false` or `False`.

**What it allows:** it skips the HTTPS check on the issuer and on every endpoint the identity
provider's discovery document names (`org_identity.py`). Those URLs are what organization mode's
trust model relies on being unspoofable in transit, so with the variable set, sign-in has no
protection against a network attacker.

## The image you ship

Test the image that will ship — the pre-flight run's, from the commit being tagged — never a source
process standing in for it. See [`release-testing.md`](release-testing.md) for the checks only a
person can do and [`testing-policy.md`](testing-policy.md) for automated coverage, including the
`pytest.mark.packaged` tests `build.yml` runs against the built image.
