// Waits (up to 45 minutes) for the checks on a pull request's current head to finish, then reports
// green, red or timeout. A red result writes the failing jobs' logs to deps/pr-<n>-ci.md for the
// next attempt. When the item was not worked on (nothing pushed), it just reads the state now.
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, input, must, run } from "../../.shared/util.ts";

const n = input("number");
const deadline = Date.now() + 45 * 60_000;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const FAILED = ["FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"];

type Check = { name: string; link: string; pending: boolean; failed: boolean };
const read = () => {
  const v = JSON.parse(must(["gh", "pr", "view", n, "--json", "statusCheckRollup,headRefOid,state"]));
  const checks: Check[] = (v.statusCheckRollup ?? []).map((c: any) => ({
    name: c.name ?? c.context,
    link: c.detailsUrl ?? c.targetUrl ?? "",
    pending: c.__typename === "CheckRun" ? c.status !== "COMPLETED" : c.state === "PENDING" || c.state === "EXPECTED",
    failed: FAILED.includes(String(c.conclusion || c.state)),
  }));
  return { head: v.headRefOid as string, state: v.state as string, checks };
};

let v = read();
// A push takes a moment to start its checks: with none reported yet, wait a few minutes for them.
const started = Date.now();
while ((v.checks.length === 0 || v.checks.some((c) => c.pending)) && Date.now() < deadline && !(v.checks.length === 0 && Date.now() - started > 5 * 60_000)) {
  await sleep(30_000);
  v = read();
}
const failed = v.checks.filter((c) => c.failed);
const state = v.state !== "OPEN" ? "closed" : v.checks.some((c) => c.pending) ? "timeout" : failed.length ? "red" : "green";

let log = "";
if (state === "red") {
  const dir = join(artifactsDir(), "deps");
  mkdirSync(dir, { recursive: true });
  log = join(dir, `pr-${n}-ci.md`);
  let text = `# Failing checks on PR ${n} at ${v.head}\n\n`;
  for (const c of failed) {
    const id = c.link.match(/\/runs\/(\d+)/)?.[1];
    const out = id ? run(["gh", "run", "view", id, "--log-failed"]).out.trimEnd().split("\n").slice(-150).join("\n") : "(no run id in the check link)";
    text += `## ${c.name}\n${c.link}\n\n\`\`\`\n${out}\n\`\`\`\n\n`;
  }
  writeFileSync(log, text);
}
console.log(JSON.stringify({ state, head: v.head, failed: failed.map((c) => c.name), log }));
