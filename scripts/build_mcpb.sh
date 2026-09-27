#!/usr/bin/env bash
# Build PrivacyFence's Claude Desktop extensions — one-click .mcpb installs
# that register an MCP server for PrivacyFence with no manual
# claude_desktop_config.json edits.
#
# Builds two files from the same shim and the same manifest template
# (mcpb/manifest.json.tmpl), differing only in their manifest (ADR 0087):
#
#   PrivacyFence-<version>.mcpb            the default: the daemon's truthful
#                                          tool annotations, so Claude Desktop
#                                          may ask before a write.
#   PrivacyFence-no-prompts-<version>.mcpb "PrivacyFence (no Claude prompts)":
#                                          starts the shim with
#                                          --tool-annotations=all-read-only,
#                                          which sends
#                                          X-PrivacyFence-Tool-Annotations on
#                                          every /mcp request (ADR 0086), so
#                                          every tool is advertised read-only
#                                          and only PrivacyFence's own
#                                          approval asks.
#
# A user installs one of the two, never both (both would list every tool
# twice). Both talk to the daemon's /mcp Streamable HTTP endpoint, the only
# transport there is. Requires web.mcp.enabled in config/settings.yaml (on by
# default).
#
# A small Node/TypeScript MCP server with no connector clients, no PII
# detection, no PyObjC/AppKit — bundled by esbuild into a single
# dependency-free server/shim.js, so the .mcpb ships no Python framework
# and no node_modules/ directory. Claude Desktop supplies the Node runtime
# itself (server.type = "node" in the manifest). This script does NOT
# depend on build_dmg.sh.
#
# The extension still talks to the PrivacyFence daemon, so the daemon
# (PrivacyFenceApp.app, built separately by build_dmg.sh, still Python) must
# be installed and configured on its own — this bundle only wires up the MCP
# server entry.
#
# Prerequisites:
#   node + npm on PATH (npm installs mcpb/shim/'s build-time deps; npx runs
#   the @anthropic-ai/mcpb CLI).
#   pip install -e .   # PrivacyFence itself -- not built by this script, only
#                       # needed so VERSION below can read its installed
#                       # metadata (git-tag-derived, see this repo's
#                       # CLAUDE.md "Releasing" section).
#
# Usage:
#   ./scripts/build_mcpb.sh
#
# Output: dist/PrivacyFence-<version>.mcpb and
#         dist/PrivacyFence-no-prompts-<version>.mcpb
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="$(command -v python3)"
VERSION=$("$PYTHON" -c "from importlib.metadata import version; print(version('privacyfence'))")

echo "=== Building PrivacyFence's Claude Desktop extensions ${VERSION} ==="

echo ""
echo "→ Building the Node shim (mcpb/shim/dist/shim.js)…"
(
  cd mcpb/shim
  npm ci --silent
  npm run build --silent
)

# build_stage <stage dir> <manifest.json source>: one extension's staging tree.
build_stage() {
  local stage="$1" manifest="$2"
  rm -rf "$stage"
  mkdir -p "${stage}/server"
  cp mcpb/shim/dist/shim.js "${stage}/server/shim.js"
  cp "$manifest" "${stage}/manifest.json"
  cp src/privacyfence/resources/icon_512.png "${stage}/icon.png"
}

# pack <stage dir> <output .mcpb>
pack() {
  local stage="$1" out="$2"
  echo "→ Validating $(basename "$out")'s manifest…"
  npx --yes @anthropic-ai/mcpb validate "${stage}/manifest.json"
  echo "→ Packing $(basename "$out")…"
  rm -f "$out"
  npx --yes @anthropic-ai/mcpb pack "$stage" "$out"
}

# The two manifests come from the same template (scripts/mcpb_manifest.py):
# the no-prompts one changes four fields and nothing else -- its own name (so
# Claude Desktop keeps the two apart), a display name and description that say
# what it does and that only one of the two should be installed, and the flag
# the shim turns into the X-PrivacyFence-Tool-Annotations header. The shim
# itself is byte-identical in both.
mkdir -p build
MANIFEST="build/mcpb-manifest.json"
NO_PROMPTS_MANIFEST="build/mcpb-no-prompts-manifest.json"
"$PYTHON" scripts/mcpb_manifest.py default "$VERSION" "$MANIFEST"
"$PYTHON" scripts/mcpb_manifest.py no-prompts "$VERSION" "$NO_PROMPTS_MANIFEST"

OUT="dist/PrivacyFence-${VERSION}.mcpb"
NO_PROMPTS_OUT="dist/PrivacyFence-no-prompts-${VERSION}.mcpb"

echo "→ Staging PrivacyFence.mcpb and PrivacyFence-no-prompts.mcpb…"
build_stage build/mcpb-stage "$MANIFEST"
build_stage build/mcpb-no-prompts-stage "$NO_PROMPTS_MANIFEST"

# No code signing needed: plain JS with no Mach-O binaries. Only
# PrivacyFenceApp.app, built and signed by build_dmg.sh, needs a Developer
# ID signature and notarization.

mkdir -p dist
pack build/mcpb-stage "$OUT"
pack build/mcpb-no-prompts-stage "$NO_PROMPTS_OUT"

echo ""
echo "✓ Done: ${OUT}   ($(du -sh "$OUT" | cut -f1))"
echo "        ${NO_PROMPTS_OUT}   ($(du -sh "$NO_PROMPTS_OUT" | cut -f1))"
echo ""
echo "Install ONE of the two by double-clicking it in Claude Desktop, or drag it"
echo "onto Settings → Extensions → Install Extension…"
