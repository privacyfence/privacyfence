// Settles the plan (the replan's answer wins when the user was asked), checks the branch name
// against git.branch_pattern, and puts the worktree on that branch: resumed from origin when it
// already exists there, otherwise cut from origin/<main>.
import { input, must, run } from "../../.shared/util.ts";

const plan = JSON.parse(input("plan"));
const replanRaw = input("replan", false);
const replan = replanRaw ? JSON.parse(replanRaw) : null;
const mainBranch = input("main_branch");
const branchRe = new RegExp(input("branch_regex"));

const final = replan ?? { ...plan, proceed: true, reason: "plan needed no decision from the user" };
const out = {
  proceed: Boolean(final.proceed),
  reason: String(final.reason ?? ""),
  scope: String(final.scope),
  slug: String(final.slug),
  branch: String(final.branch),
  title: String(final.title),
};

if (out.proceed) {
  if (!branchRe.test(out.branch)) {
    out.proceed = false;
    out.reason = `branch '${out.branch}' does not match git.branch_pattern (${branchRe.source})`;
  } else if (out.branch === mainBranch) {
    out.proceed = false;
    out.reason = `the plan names the main branch '${mainBranch}' as its branch`;
  }
}

if (out.proceed) {
  must(["git", "fetch", "origin", mainBranch]);
  const onOrigin = run(["git", "ls-remote", "--exit-code", "--heads", "origin", out.branch]).code === 0;
  const local = run(["git", "rev-parse", "--verify", "--quiet", `refs/heads/${out.branch}`]).code === 0;
  if (onOrigin) {
    must(["git", "fetch", "origin", out.branch]);
    if (local) {
      must(["git", "switch", out.branch]);
      must(["git", "merge", "--ff-only", `origin/${out.branch}`]);
    } else {
      must(["git", "switch", "-c", out.branch, "--track", `origin/${out.branch}`]);
    }
    console.error(`prepare-branch: resuming ${out.branch} from origin`);
  } else if (local) {
    must(["git", "switch", out.branch]);
  } else {
    must(["git", "switch", "-c", out.branch, `origin/${mainBranch}`]);
  }
}

console.log(JSON.stringify(out));
