// Runs the profile's verify.fast (mode=fast) or verify.dod (mode=dod) commands, in order, with the
// project venv first on PATH. Never exits non-zero for a red check: the result is the node's
// output, and the loop around it decides. The full log goes to $ARTIFACTS_DIR/verify-<mode>.md.
//
// A command whose executable is not on this host (npm here) is "unavailable", not passed. In dod
// mode it does not turn the gate red, because CI runs it on the PR; the PR body must say so. In
// fast mode it is a failure, because fast checks are what every implementation step relies on.
import { appendFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, fail, input, profile, projectEnv, run, tail } from "./util.ts";

const mode = input("mode");
if (mode !== "fast" && mode !== "dod") fail(`mode must be fast or dod, got '${mode}'`);

const entries: Array<string | { run: string; cwd?: string }> = profile().verify?.[mode] ?? [];
if (entries.length === 0) fail(`.claude/toolkit.yaml has no verify.${mode} commands`);

const env = projectEnv();
const PATH = env.PATH ?? process.env.PATH ?? "";
const report = join(artifactsDir(), `verify-${mode}.md`);
writeFileSync(report, `# verify.${mode} at ${run(["git", "rev-parse", "HEAD"]).out.trim()}\n\n`);

const failed: string[] = [];
const unavailable: string[] = [];
for (const e of entries) {
  const cmd = typeof e === "string" ? e : e.run;
  const cwd = typeof e === "string" ? "." : (e.cwd ?? ".");
  const label = cwd === "." ? cmd : `(cd ${cwd} && ${cmd})`;
  const exe = cmd.trim().split(/\s+/)[0];
  if (run(["sh", "-c", `command -v "$0" >/dev/null`, exe], { env: { PATH } }).code !== 0) {
    unavailable.push(label);
    appendFileSync(report, `## UNAVAILABLE: ${label}\n\n\`${exe}\` is not on this host.\n\n`);
    continue;
  }
  const r = run(["bash", "-c", cmd], { cwd, env, timeoutMs: 1_800_000 });
  const ok = r.code === 0;
  if (!ok) failed.push(label);
  appendFileSync(report, `## ${ok ? "PASS" : `FAIL (exit ${r.code})`}: ${label}\n\n\`\`\`\n${tail(r.out + r.err, ok ? 15 : 120)}\n\`\`\`\n\n`);
}

const green = failed.length === 0 && (mode === "dod" || unavailable.length === 0);
console.error(`verify.${mode}: ${green ? "green" : "red"}; failed=${failed.length} unavailable=${unavailable.length}`);
console.log(JSON.stringify({ green, failed, unavailable, report }));
