// Creates the release tag with the repo's own scripts/tag_release.py, in a fresh clone of main so
// the script's "checked-out branch is main, clean, equal to origin/main" checks hold whatever the
// run's worktree is on. mode=dry creates only a local tag in the throwaway clone (every check the
// script makes, nothing leaves the machine); mode=cut also pushes it, which starts build.yml and
// publish-pypi.yml. The commit tagged must be the one the person confirmed and build.yml proved.
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, fail, input, must, run } from "../../.shared/util.ts";

const mode = input("mode");
const version = input("version").replace(/^v/, "");
const expected = input("main_sha");
if (mode !== "dry" && mode !== "cut") fail(`mode must be dry or cut, got '${mode}'`);

const url = must(["git", "remote", "get-url", "origin"]);
const script = join(process.cwd(), "scripts/tag_release.py");
const scratch = join(artifactsDir(), ".archon/scratch");
mkdirSync(scratch, { recursive: true });
const clone = join(scratch, `tag-${mode}-${Date.now()}`);
must(["git", "clone", "--quiet", "--branch", "main", url, clone], { timeoutMs: 300_000 });
must(["git", "fetch", "--quiet", "--tags", "origin"], { cwd: clone });

const head = must(["git", "rev-parse", "HEAD"], { cwd: clone });
if (head !== expected) {
  fail(`main moved: it is at ${head.slice(0, 12)} now, but ${expected.slice(0, 12)} was confirmed and built. Nothing was tagged. Start a new run.`);
}

const args = ["python3", script, version, "--repo", clone, "--no-fetch"];
if (mode === "cut") args.push("--push");
const r = run(args, { timeoutMs: 120_000 });
if (r.code !== 0) {
  fail(`tag_release.py refused (${mode}):\n${(r.err || r.out).trim()}${mode === "cut" ? "\nNo tag was pushed unless the output above says so; check `git ls-remote --tags origin`." : ""}`);
}
console.error(r.out.trim());

if (mode === "cut") {
  const remote = must(["git", "ls-remote", "--tags", "origin", `refs/tags/v${version}`]).split(/\s/)[0];
  const tagged = must(["git", "rev-parse", `v${version}^{commit}`], { cwd: clone });
  if (!remote || tagged !== expected) fail(`the push reported success but origin does not show v${version} at ${expected.slice(0, 12)}`);
}
console.log(JSON.stringify({ ok: true, tag: `v${version}`, sha: expected, pushed: mode === "cut" }));
