#!/bin/sh
# The image always runs in organization mode (PRIVACYFENCE_MODE=org). Without an installed bundle
# there is no identity provider, which makes it a single-user install reachable only on localhost,
# unless the identity provider (your own, or the built-in Google sign-in) and the server address
# come from the environment.
set -eu
bundle="$HOME/.privacyfence/org/org_bundle.json"
# A 6.x org_config.json counts too: the daemon copies it to org_bundle.json on start.
legacy_bundle="$HOME/.privacyfence/org/org_config.json"
if [ ! -f "$bundle" ] && [ ! -f "$legacy_bundle" ]; then
    if [ -n "${PRIVACYFENCE_IDP_ISSUER:-}" ] && [ -n "${PRIVACYFENCE_IDP_CLIENT_ID:-}" ]; then
        echo "No organization bundle: using the IdP and server from the environment." >&2
    elif [ "${PRIVACYFENCE_IDP:-}" = "builtin-google" ]; then
        echo "No organization bundle: using the built-in Google sign-in and the server from the environment." >&2
    else
        echo "No organization bundle: starting in single-user mode at http://localhost:8765 (docs/org-mode-docker.md, \"Run it on this computer\")." >&2
    fi
fi
exec privacyfence-app "$@"
