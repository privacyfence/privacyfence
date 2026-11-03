// Builds the environment the PrivacyFence session-start hook (.claude/hooks/session-start.sh) builds
// for a cloud session: full tags for setuptools_scm, a .venv with the dev/test/lint extras, and the
// mcpb/shim node_modules when npm exists. The host has no python3-venv/pip, so uv makes the venv.
import { existsSync } from "node:fs";
import { must, onPath, run, tail } from "./util.ts";

const notes: string[] = [];

if (run(["git", "rev-parse", "--is-shallow-repository"]).out.trim() === "true") {
  must(["git", "fetch", "--unshallow", "--tags", "origin"]);
} else {
  must(["git", "fetch", "--tags", "origin"]);
}
if (run(["git", "describe", "--tags"]).code !== 0) notes.push("no tag reachable from HEAD: __version__ will be the fallback_version");

must(["uv", "venv", "--allow-existing", "--seed", ".venv"], { timeoutMs: 300_000 });
must(["uv", "pip", "install", "--python", ".venv/bin/python", "-e", ".[dev,test,lint]"], { timeoutMs: 1_500_000 });

if (!existsSync("mcpb/shim/package.json")) {
  // nothing to install
} else if (!onPath("npm")) {
  notes.push("npm is not on PATH: the mcpb/shim npm checks cannot run here; CI runs them on the PR");
} else if (!existsSync("mcpb/shim/node_modules")) {
  const r = run(["npm", "ci", "--no-audit", "--no-fund"], { cwd: "mcpb/shim", timeoutMs: 600_000 });
  if (r.code !== 0) notes.push(`npm ci in mcpb/shim failed:\n${tail(r.out + r.err, 20)}`);
}

for (const n of notes) console.error(`setup: ${n}`);
console.log(JSON.stringify({ venv: ".venv", notes }));
