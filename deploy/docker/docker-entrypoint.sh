#!/bin/sh
# Organization mode only: without an installed bundle the daemon would start in local mode,
# listening on the container's own loopback where nothing can reach it.
set -eu
bundle="$HOME/.privacyfence/org/org_config.json"
if [ ! -f "$bundle" ]; then
    echo "No organization bundle at $bundle -- install one first (docs/org-mode-docker.md, \"Install the bundle\")." >&2
    exit 78
fi
exec privacyfence-app "$@"
