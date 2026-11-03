// Reads the PR's facts from GitHub and decides only whether something new needs the steward:
// a merge conflict, a failed check, an unresolved review thread whose last word is not ours, or a
// comment/review by someone else we have not handed over yet. What to do about it is the
// steward's judgement. Each condition is handed over once per head (or per comment), recorded in
// $ARTIFACTS_DIR/watch-state.json, so a red check the steward could not fix is not retried every
// poll: "a second red run on the same commit is real" and waits for a person or a new push.
// The facts the steward reads are written to $ARTIFACTS_DIR/pr-state.json.
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, input, must } from "../../.shared/util.ts";

const n = input("number");
const mainBranch = input("main_branch");
const statePath = join(artifactsDir(), "watch-state.json");
const handled: { keys: string[] } = existsSync(statePath) ? JSON.parse(readFileSync(statePath, "utf8")) : { keys: [] };
const seen = new Set(handled.keys);

const me = must(["gh", "api", "user", "-q", ".login"]);
const pr = JSON.parse(
  must(["gh", "pr", "view", n, "--json", "state,url,mergeable,mergeStateStatus,headRefOid,headRefName,statusCheckRollup,comments,reviews"]),
);
const finished = pr.state === "MERGED" || pr.state === "CLOSED";
const [, owner, repo] = new URL(pr.url).pathname.split("/");
const head: string = pr.headRefOid;

const reasons: string[] = [];
const newKeys: string[] = [];
const handOver = (key: string, reason: string) => {
  if (seen.has(key)) return;
  newKeys.push(key);
  reasons.push(reason);
};

const FAILED = new Set(["FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"]);
const checks = (pr.statusCheckRollup ?? []).map((c: any) => ({
  name: c.name ?? c.context,
  result: c.conclusion || c.state || c.status,
  pending: c.__typename === "CheckRun" ? c.status !== "COMPLETED" : c.state === "PENDING" || c.state === "EXPECTED",
  url: c.detailsUrl ?? c.targetUrl ?? "",
}));
const failedChecks = checks.filter((c: any) => !c.pending && FAILED.has(String(c.result)));

const threadsQuery = `query($owner:String!,$repo:String!,$n:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$n){
  reviewThreads(first:100){nodes{id isResolved isOutdated path line comments(last:1){nodes{id author{login} body url}}}}}}}`;
const threads = JSON.parse(
  must(["gh", "api", "graphql", "-f", `query=${threadsQuery}`, "-F", `owner=${owner}`, "-F", `repo=${repo}`, "-F", `n=${n}`]),
).data.repository.pullRequest.reviewThreads.nodes;
const openThreads = threads
  .filter((t: any) => !t.isResolved)
  .map((t: any) => ({ id: t.id, path: t.path, line: t.line, outdated: t.isOutdated, last: t.comments.nodes[0] }))
  .filter((t: any) => t.last && t.last.author?.login !== me);

const others = [
  ...(pr.comments ?? []).map((c: any) => ({ kind: "comment", id: c.id, author: c.author?.login, body: c.body, url: c.url })),
  ...(pr.reviews ?? []).map((r: any) => ({ kind: `review ${r.state}`, id: r.id, author: r.author?.login, body: r.body, url: "" })),
].filter((c) => c.author !== me && (c.body ?? "").trim() !== "");

if (!finished) {
  if (pr.mergeable === "CONFLICTING") {
    const mainSha = must(["git", "ls-remote", "origin", `refs/heads/${mainBranch}`]).split(/\s/)[0];
    handOver(`conflict:${head}:${mainSha}`, `merge conflict with ${mainBranch}`);
  }
  for (const c of failedChecks) handOver(`check:${head}:${c.name}`, `check '${c.name}' ${c.result} on ${head.slice(0, 7)}`);
  for (const t of openThreads) handOver(`thread:${t.id}:${t.last.id}`, `unresolved review thread on ${t.path}:${t.line ?? "?"}`);
  for (const c of others) handOver(`comment:${c.id}`, `${c.kind} by ${c.author}`);
}

const act = !finished && newKeys.length > 0;
if (act) writeFileSync(statePath, JSON.stringify({ keys: [...seen, ...newKeys] }, null, 2));
writeFileSync(
  join(artifactsDir(), "pr-state.json"),
  JSON.stringify(
    {
      number: Number(n),
      url: pr.url,
      state: pr.state,
      head,
      branch: pr.headRefName,
      mergeable: pr.mergeable,
      mergeStateStatus: pr.mergeStateStatus,
      new_this_poll: reasons,
      failed_checks: failedChecks,
      pending_checks: checks.filter((c: any) => c.pending).map((c: any) => c.name),
      open_threads: openThreads,
      comments_from_others: others,
    },
    null,
    2,
  ),
);

console.log(JSON.stringify({ finished, state: pr.state, act, reasons: reasons.join("; ") || "nothing new" }));
