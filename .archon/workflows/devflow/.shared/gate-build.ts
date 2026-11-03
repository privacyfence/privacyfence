// The deterministic gate between implementation and review. Fails the run when an implementer
// stopped as blocked (its reason is in $ARTIFACTS_DIR/blocked.md), when the last fast checks were
// red, when a phase of the plan's manifest has no `Plan-Phase: <slug>/<id>` commit on the branch,
// or when the worktree is not on the plan's branch, is dirty, or has nothing beyond origin/<main>.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, fail, input, must } from "./util.ts";

const branch = input("branch");
const mainBranch = input("main_branch");
const slug = input("slug");
const dir = artifactsDir();

const blocked = join(dir, "blocked.md");
if (existsSync(blocked)) fail(`implementation stopped as blocked; it needs a person:\n${readFileSync(blocked, "utf8")}`);
if (input("green", false) !== "true") fail(`the fast checks are still red after the last implementation step (see ${join(dir, "verify-fast.md")})`);

const current = must(["git", "branch", "--show-current"]);
if (current !== branch) fail(`the worktree is on '${current}', not the plan's branch '${branch}'`);
const dirty = must(["git", "status", "--porcelain"]);
if (dirty) fail(`uncommitted changes after implementation:\n${dirty}`);
must(["git", "fetch", "origin", mainBranch]);
const ahead = Number(must(["git", "rev-list", "--count", `origin/${mainBranch}..HEAD`]));
if (ahead === 0) fail(`'${branch}' has no commits beyond origin/${mainBranch}: nothing was implemented`);

// A workflow with no plan passes phases=none and is judged on the work order alone. Anything else
// must carry a manifest whose every phase landed, so a planner that wrote no plan still fails here.
let phases = 0;
if (input("phases", false) !== "none") {
  const planPath = join(dir, "plan.md");
  if (!existsSync(planPath)) fail(`${planPath} does not exist: the planner wrote no plan`);
  const plan = readFileSync(planPath, "utf8");
  const m = plan.match(/^## Implementation manifest\s*$[\s\S]*?^```ya?ml\s*$([\s\S]*?)^```\s*$/m);
  if (!m) fail("plan.md has no fenced yaml block under '## Implementation manifest'");
  const manifest = Bun.YAML.parse(m[1]) as { plan_slug?: string; phases?: Array<{ id: string }> };
  const planSlug = manifest.plan_slug ?? slug;
  const missing = (manifest.phases ?? [])
    .map((p) => p.id)
    .filter((id) => !must(["git", "log", `origin/${mainBranch}..HEAD`, "--format=%H", "--fixed-strings", `--grep=Plan-Phase: ${planSlug}/${id}`]));
  if (!manifest.phases?.length) fail("the plan's manifest has no phases");
  if (missing.length) fail(`phases with no 'Plan-Phase: ${planSlug}/<id>' commit on ${branch}: ${missing.join(", ")}`);
  phases = manifest.phases.length;
}

console.log(JSON.stringify({ commits: ahead, phases, head: must(["git", "rev-parse", "HEAD"]) }));
