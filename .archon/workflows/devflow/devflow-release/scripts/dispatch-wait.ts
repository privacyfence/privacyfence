// Dispatches a workflow_dispatch workflow on a ref, finds the run it started, waits for it to finish
// and fails unless it succeeded (and, when REQUIRED_JOBS is set, unless every named job succeeded).
// Inputs: workflow, ref, expected_sha (the commit the run must be on), fields (JSON object of
// workflow inputs), required_jobs (JSON array of job-name prefixes), timeout_min.
// gh workflow run prints no run id, so the run is found by dispatch time + head commit; a run
// somebody else dispatched at the same moment on the same commit would be ambiguous, and a
// concurrency-group queue is just a longer wait.
import { fail, input, must, run } from "../../.shared/util.ts";

const workflow = input("workflow");
const ref = input("ref");
const expectedSha = input("expected_sha");
const fields: Record<string, string> = JSON.parse(input("fields", false) || "{}");
const requiredJobs: string[] = JSON.parse(input("required_jobs", false) || "[]");
const deadline = Date.now() + Number(input("timeout_min", false) || "120") * 60_000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const started = Date.now() - 10_000;

const args = ["gh", "workflow", "run", workflow, "--ref", ref];
for (const [k, v] of Object.entries(fields)) args.push("-f", `${k}=${v}`);
must(args);
console.error(`dispatched ${workflow} on ${ref} at ${expectedSha.slice(0, 7)}`);

let runId = "";
while (!runId) {
  if (Date.now() > deadline) fail(`${workflow} never appeared as a run on ${expectedSha.slice(0, 7)}`);
  await sleep(5000);
  const runs = JSON.parse(
    must(["gh", "run", "list", "--workflow", workflow, "--event", "workflow_dispatch", "--branch", ref, "--limit", "10", "--json", "databaseId,createdAt,headSha"]),
  ) as Array<{ databaseId: number; createdAt: string; headSha: string }>;
  const mine = runs
    .filter((r) => Date.parse(r.createdAt) >= started && r.headSha === expectedSha)
    .sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt));
  if (mine.length) runId = String(mine[0].databaseId);
}

type View = { status: string; conclusion: string; url: string; jobs: Array<{ name: string; conclusion: string; status: string }> };
let view: View;
for (;;) {
  view = JSON.parse(must(["gh", "run", "view", runId, "--json", "status,conclusion,url,jobs"]));
  if (view.status === "completed") break;
  if (Date.now() > deadline) fail(`${view.url} is still ${view.status} after the timeout; it was not cancelled`);
  await sleep(30_000);
}

const jobs = view.jobs.map((j) => ({ name: j.name, conclusion: j.conclusion }));
const missing = requiredJobs.filter((p) => !jobs.some((j) => j.name.startsWith(p)));
const bad = jobs.filter((j) => j.conclusion !== "success" && j.conclusion !== "skipped");
const requiredBad = requiredJobs.flatMap((p) => jobs.filter((j) => j.name.startsWith(p) && j.conclusion !== "success"));
if (view.conclusion !== "success" || missing.length || requiredBad.length) {
  const why = [
    view.conclusion !== "success" ? `run concluded ${view.conclusion}` : "",
    missing.length ? `jobs that never ran: ${missing.join(", ")}` : "",
    ...bad.map((j) => `job '${j.name}' ${j.conclusion}`),
  ].filter(Boolean);
  fail(`${workflow} at ${expectedSha.slice(0, 7)} is not green: ${why.join("; ")}\n${view.url}`);
}

// The log excerpt a person wants to read (a release dry run prints the tag it would push and the notes).
let excerpt = "";
if (input("log_excerpt", false) === "true") {
  const l = run(["gh", "run", "view", runId, "--log"]);
  excerpt = l.out.trimEnd().split("\n").slice(-60).join("\n");
}
console.log(JSON.stringify({ run_id: Number(runId), url: view.url, conclusion: view.conclusion, jobs, excerpt }));
