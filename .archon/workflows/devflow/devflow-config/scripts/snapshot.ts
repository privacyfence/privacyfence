// Reads a repository's configuration into $ARTIFACTS_DIR/snapshot.json (and, when a reference
// repository is named, reference.json) so the reviewer judges facts. Read-only. Every endpoint
// records its own status: a 403 or 404 is information ("not readable with this token", "not
// enabled"), not an error, and the reviewer is told which is which.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { api, repoSlug } from "../../.shared/gh.ts";
import { artifactsDir, fail, input } from "../../.shared/util.ts";

const AMBIGUOUS_WITHOUT_ADMIN = [
  "branch_protection", "teams", "deploy_keys", "webhooks", "vulnerability_alerts_enabled",
  "automated_security_fixes", "private_vulnerability_reporting", "open_secret_scanning_alerts",
];

function snapshot(repo: string) {
  const base = `repos/${repo}`;
  const out: Record<string, { status: string; code: number; message?: string; data: unknown }> = {};
  const get = (key: string, ep: string) => {
    const r = api("GET", ep);
    out[key] = { status: r.status, code: r.code, message: r.message || undefined, data: r.data };
    return r;
  };
  const meta = get("repo", base);
  const branch = (meta.data as any)?.default_branch ?? "main";
  const rulesets = get("rulesets", `${base}/rulesets?per_page=100&includes_parents=true`);
  if (Array.isArray(rulesets.data)) {
    for (const rs of rulesets.data as Array<{ id: number }>) get(`ruleset:${rs.id}`, `${base}/rulesets/${rs.id}`);
  }
  const eps: Array<[string, string]> = [
    ["branch_protection", `${base}/branches/${encodeURIComponent(branch)}/protection`],
    ["actions_permissions", `${base}/actions/permissions`],
    ["actions_workflow_permissions", `${base}/actions/permissions/workflow`],
    ["actions_selected", `${base}/actions/permissions/selected-actions`],
    ["actions_fork_pr_approval", `${base}/actions/permissions/fork-pr-contributor-approval`],
    ["actions_workflows", `${base}/actions/workflows?per_page=100`],
    ["actions_secret_names", `${base}/actions/secrets?per_page=100`],
    ["actions_variables", `${base}/actions/variables?per_page=100`],
    ["runners", `${base}/actions/runners`],
    ["environments", `${base}/environments?per_page=100`],
    ["collaborators", `${base}/collaborators?per_page=100&affiliation=all`],
    ["teams", `${base}/teams`],
    ["deploy_keys", `${base}/keys`],
    ["webhooks", `${base}/hooks`],
    ["vulnerability_alerts_enabled", `${base}/vulnerability-alerts`],
    ["automated_security_fixes", `${base}/automated-security-fixes`],
    ["private_vulnerability_reporting", `${base}/private-vulnerability-reporting`],
    ["code_scanning_default_setup", `${base}/code-scanning/default-setup`],
    ["open_code_scanning_alerts", `${base}/code-scanning/alerts?state=open&per_page=100`],
    ["open_dependabot_alerts", `${base}/dependabot/alerts?state=open&per_page=100`],
    ["open_secret_scanning_alerts", `${base}/secret-scanning/alerts?state=open&per_page=100`],
    ["community_profile", `${base}/community/profile`],
    ["pages", `${base}/pages`],
    ["topics", `${base}/topics`],
  ];
  for (const [k, ep] of eps) get(k, ep);
  // For a token without admin, GitHub answers 404 on these instead of 403, so a 404 does not mean
  // "not enabled" (a repository with Dependabot alerts on still answers 404 on vulnerability-alerts).
  if ((meta.data as any)?.permissions?.admin !== true) {
    for (const k of AMBIGUOUS_WITHOUT_ADMIN) if (out[k]?.status === "not_found") out[k].status = "unknown_without_admin";
  }
  return out;
}

const repo = repoSlug();
const reference = input("reference", false).trim();
const dir = artifactsDir();
const snap = snapshot(repo);
writeFileSync(join(dir, "snapshot.json"), JSON.stringify({ repo, snapshot: snap }, null, 2));

let referenceOk = false;
if (reference) {
  if (reference === repo) fail("the reference repository is the repository under review: leave 'reference' empty to review it on its own");
  const ref = snapshot(reference);
  referenceOk = ref.repo.status === "ok";
  writeFileSync(join(dir, "reference.json"), JSON.stringify({ repo: reference, snapshot: ref }, null, 2));
}

const meta: any = snap.repo.data;
const perms = meta?.permissions ?? {};
const unreadable = Object.entries(snap)
  .filter(([k, v]) => v.status === "forbidden" || v.status === "unknown_without_admin" || (v.status === "error" && !k.startsWith("ruleset:")))
  .map(([k, v]) => `${k} (${v.code})`);
if (snap.repo.status !== "ok") fail(`cannot read ${repo}: ${snap.repo.message}`);
console.log(
  JSON.stringify({
    repo,
    default_branch: meta.default_branch,
    visibility: meta.visibility,
    can_admin: perms.admin === true,
    role: perms.admin ? "admin" : perms.maintain ? "maintain" : perms.push ? "write" : "read",
    reference: reference || "",
    reference_ok: referenceOk,
    unreadable,
  }),
);
