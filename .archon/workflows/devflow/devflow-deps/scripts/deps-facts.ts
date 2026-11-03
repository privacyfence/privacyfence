// Reads, read-only, what there is to do: the open pull requests Dependabot opened (with their
// checks, mergeability and files), the open Dependabot alerts (package, manifest, severity, first
// patched version) and the open code scanning alerts. Written to $ARTIFACTS_DIR/deps-facts.json.
// Which PR covers which alert, and whether a bump is safe, is for the triage agent to judge.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { api, repoSlug } from "../../.shared/gh.ts";
import { artifactsDir, fail, must } from "../../.shared/util.ts";

const repo = repoSlug();
const prs = JSON.parse(
  must(["gh", "pr", "list", "--state", "open", "--author", "app/dependabot", "--limit", "100", "--json",
    "number,title,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,createdAt,labels,files,statusCheckRollup,url"]),
) as any[];

const pr = prs.map((p) => ({
  number: p.number,
  title: p.title,
  url: p.url,
  branch: p.headRefName,
  head: p.headRefOid,
  base: p.baseRefName,
  mergeable: p.mergeable,
  merge_state: p.mergeStateStatus,
  opened: p.createdAt,
  files: (p.files ?? []).map((f: any) => f.path),
  failed_checks: (p.statusCheckRollup ?? [])
    .filter((c: any) => ["FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"].includes(String(c.conclusion || c.state)))
    .map((c: any) => ({ name: c.name ?? c.context, link: c.detailsUrl ?? c.targetUrl ?? "" })),
  pending_checks: (p.statusCheckRollup ?? []).filter((c: any) => (c.__typename === "CheckRun" ? c.status !== "COMPLETED" : c.state === "PENDING")).length,
}));

const al = api("GET", `repos/${repo}/dependabot/alerts?state=open&per_page=100`);
if (al.status === "forbidden") fail(`this token cannot read Dependabot alerts (${al.message}); they need admin or the security_events scope`);
const alerts = (Array.isArray(al.data) ? (al.data as any[]) : []).map((a) => ({
  number: a.number,
  package: a.dependency?.package?.name,
  ecosystem: a.dependency?.package?.ecosystem,
  manifest: a.dependency?.manifest_path,
  scope: a.dependency?.scope,
  severity: a.security_advisory?.severity,
  ghsa: a.security_advisory?.ghsa_id,
  summary: a.security_advisory?.summary,
  vulnerable_range: a.security_vulnerability?.vulnerable_version_range,
  first_patched: a.security_vulnerability?.first_patched_version?.identifier ?? "",
  url: a.html_url,
}));

const cs = api("GET", `repos/${repo}/code-scanning/alerts?state=open&per_page=100`);
const code = (Array.isArray(cs.data) ? (cs.data as any[]) : []).map((a) => ({
  number: a.number, rule: a.rule?.id, severity: a.rule?.security_severity_level ?? a.rule?.severity, file: a.most_recent_instance?.location?.path, url: a.html_url,
}));

writeFileSync(join(artifactsDir(), "deps-facts.json"), JSON.stringify({ repo, prs: pr, alerts, code_scanning: code, code_scanning_readable: cs.status === "ok" }, null, 2));
console.log(JSON.stringify({ repo, prs: pr.length, alerts: alerts.length, code_scanning: code.length, total: pr.length + alerts.length + code.length }));
