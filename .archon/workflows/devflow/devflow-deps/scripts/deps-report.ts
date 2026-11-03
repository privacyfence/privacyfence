// Writes deps-report.md from the triage record and the queue: which Dependabot PRs were repaired and
// are green, which need a person (with the reason and the CI evidence), which were skipped and why.
// "complete" means no pull request is left needing a person.
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir } from "../../.shared/util.ts";

const dir = artifactsDir();
const q: Array<{ number: number; status: string; attempts: number; reason: string; outcome: string; ci: string }> = existsSync(join(dir, "deps-queue.json"))
  ? JSON.parse(readFileSync(join(dir, "deps-queue.json"), "utf8"))
  : [];
const facts = existsSync(join(dir, "deps-facts.json")) ? JSON.parse(readFileSync(join(dir, "deps-facts.json"), "utf8")) : { prs: [] };
const queued = new Set(q.map((i) => i.number));
const skipped = (facts.prs as Array<{ number: number; title: string }>).filter((p) => !queued.has(p.number));
const done = q.filter((i) => i.status === "done").map((i) => i.number);
const needs = q.filter((i) => i.status !== "done");

const lines = ["# Dependency pass", "", "## Dependabot pull requests", ""];
for (const i of q) lines.push(`- #${i.number}: ${i.status}; ${i.attempts} attempt(s); last outcome ${i.outcome || "n/a"}, CI ${i.ci || "n/a"}. ${i.reason}`);
for (const p of skipped) lines.push(`- #${p.number}: not worked on by triage (${p.title}); see deps-triage.md`);
lines.push("", "Merging is left to a person; no alert was dismissed.");
const report = join(dir, "deps-report.md");
writeFileSync(report, lines.join("\n") + "\n");
console.log(JSON.stringify({ complete: needs.length === 0, done, needs_person: needs.map((i) => i.number), skipped: skipped.map((p) => p.number), report }));
