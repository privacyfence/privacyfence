// The only gate between an agent's proposed settings changes and the GitHub API. An operation
// passes only if it targets the repository under review, matches the allow-list of settings
// resources below, and does not touch what no run may change: repository visibility, name,
// archive state, transfer or deletion; secrets; webhooks; deploy keys; the removal or demotion of
// an admin. Accepted operations are written to config-ops.json, which the apply step reads by
// proposal id, so what the person approves is exactly what runs. The agent's text decides nothing.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, input } from "../../.shared/util.ts";

type Op = { method: string; endpoint: string; body: string };
type Proposal = { id: string; area: string; title: string; why: string; evidence: string; risk: string; reversible: boolean; kind: string; ops: Op[] };

const repo = input("repo");
const proposals: Proposal[] = JSON.parse(input("proposals"));
const snap = JSON.parse(readFileSync(join(artifactsDir(), "snapshot.json"), "utf8")).snapshot;
const admins = new Set<string>(((snap.collaborators?.data ?? []) as any[]).filter((c) => c.permissions?.admin || c.role_name === "admin").map((c) => c.login));
const prefix = `repos/${repo}`;

const REPO_PATCH_KEYS = new Set([
  "allow_merge_commit", "allow_squash_merge", "allow_rebase_merge", "allow_auto_merge", "allow_update_branch",
  "delete_branch_on_merge", "has_wiki", "has_projects", "has_issues", "has_discussions", "web_commit_signoff_required",
  "merge_commit_title", "merge_commit_message", "squash_merge_commit_title", "squash_merge_commit_message", "security_and_analysis",
]);
const RULES: Array<[RegExp, string[]]> = [
  [/^\/rulesets$/, ["POST"]],
  [/^\/rulesets\/\d+$/, ["PUT", "DELETE"]],
  [/^\/actions\/permissions$/, ["PUT"]],
  [/^\/actions\/permissions\/(workflow|selected-actions|fork-pr-contributor-approval)$/, ["PUT"]],
  [/^\/branches\/[^/]+\/protection$/, ["PUT", "DELETE"]],
  [/^\/vulnerability-alerts$/, ["PUT", "DELETE"]],
  [/^\/automated-security-fixes$/, ["PUT", "DELETE"]],
  [/^\/private-vulnerability-reporting$/, ["PUT", "DELETE"]],
  [/^\/code-scanning\/default-setup$/, ["PATCH"]],
  [/^\/topics$/, ["PUT"]],
  [/^\/environments\/[^/]+$/, ["PUT"]],
  [/^\/collaborators\/[^/]+$/, ["PUT", "DELETE"]],
  [/^$/, ["PATCH"]],
];

function refuse(op: Op): string {
  const m = op.method.toUpperCase();
  if (!op.endpoint.startsWith(`${prefix}`)) return `targets '${op.endpoint}', not ${prefix}`;
  if (op.endpoint.includes("..") || op.endpoint.includes("?")) return "endpoint has a path escape or a query string";
  const rest = op.endpoint.slice(prefix.length);
  const rule = RULES.find(([re]) => re.test(rest));
  if (!rule) return `'${rest || "/"}' is not a settings resource this workflow may change`;
  if (!rule[1].includes(m)) return `${m} is not allowed on '${rest || "/"}' (allowed: ${rule[1].join(", ")})`;
  let body: any = {};
  if (op.body) {
    try {
      body = JSON.parse(op.body);
    } catch {
      return "body is not valid JSON";
    }
  }
  if (rest === "") {
    const bad = Object.keys(body).filter((k) => !REPO_PATCH_KEYS.has(k));
    if (bad.length) return `repository fields not allowed to change: ${bad.join(", ")} (visibility, name, archive and default branch are never changed here)`;
  }
  const who = rest.match(/^\/collaborators\/([^/]+)$/)?.[1];
  if (who && admins.has(who) && (m === "DELETE" || (m === "PUT" && body.permission && body.permission !== "admin"))) {
    return `'${who}' is an administrator: removing or demoting an admin is for a person to do in the UI`;
  }
  return "";
}

const accepted: Proposal[] = [];
const rejected: Array<{ id: string; reason: string }> = [];
const seen = new Set<string>();
for (const p of proposals) {
  if (seen.has(p.id)) {
    rejected.push({ id: p.id, reason: "duplicate proposal id" });
    continue;
  }
  seen.add(p.id);
  if (!/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.test(p.id)) {
    rejected.push({ id: p.id, reason: "proposal ids are lowercase kebab-case (c1, c2, ...)" });
    continue;
  }
  if (p.kind !== "api") continue; // file proposals go through a pull request, not through here
  if (!p.ops.length) {
    rejected.push({ id: p.id, reason: "an api proposal with no operations" });
    continue;
  }
  const why = p.ops.map(refuse).find(Boolean);
  if (why) rejected.push({ id: p.id, reason: why });
  else accepted.push({ ...p, risk: p.area === "access" && p.risk === "low" ? "medium" : p.risk });
}
writeFileSync(join(artifactsDir(), "config-ops.json"), JSON.stringify(accepted, null, 2));

const options = accepted.map((p) => ({ id: p.id, label: `${p.id} [${p.risk}${p.reversible ? "" : ", not reversible"}] ${p.title}` }));
const ask =
  `Settings changes proposed for ${repo}. Pick the ones to apply now; each is applied through the GitHub API with the before and after recorded in config-changes.md.\n\n` +
  accepted
    .map((p) => `**${p.id}** (${p.area}, ${p.risk}) ${p.title}\nWhy: ${p.why}\nEvidence: ${p.evidence}\nCalls: ${p.ops.map((o) => `${o.method} ${o.endpoint.slice(prefix.length) || "/"}`).join("; ")}`)
    .join("\n\n") +
  (rejected.length ? `\n\nNot offered (refused by the allow-list): ${rejected.map((r) => `${r.id}: ${r.reason}`).join("; ")}` : "");
console.log(JSON.stringify({ count: accepted.length, options, ask, rejected }));
