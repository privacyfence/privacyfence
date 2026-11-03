// The work queue for the Dependabot pull requests the triage agent chose to work on.
//   init  builds deps-queue.json from the triage items (action "work"), taking each PR's branch from
//         deps-facts.json rather than from the agent
//   next  hands out the next pending item and counts the attempt (an item gets at most 2)
//   mark  records how an attempt ended: done, needs-person, or back to pending for one retry when CI
//         is red after a push and an attempt remains
// State lives in a file, so a resumed run continues where it stopped.
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, fail, input } from "../../.shared/util.ts";

type Item = { number: number; branch: string; status: "pending" | "done" | "needs-person"; attempts: number; reason: string; outcome: string; ci: string };
const path = join(artifactsDir(), "deps-queue.json");
const MAX_ATTEMPTS = 2;
const mode = input("mode");
const load = (): Item[] => (existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : []);
const save = (q: Item[]) => writeFileSync(path, JSON.stringify(q, null, 2));
const remaining = (q: Item[]) => q.filter((i) => i.status === "pending").length;

if (mode === "init") {
  const triage = JSON.parse(input("items")) as Array<{ number: number; action: string; reason: string }>;
  const facts = JSON.parse(readFileSync(join(artifactsDir(), "deps-facts.json"), "utf8")).prs as Array<{ number: number; branch: string }>;
  const q: Item[] = [];
  for (const t of triage.filter((t) => t.action === "work")) {
    const f = facts.find((p) => p.number === t.number);
    if (!f) fail(`triage chose PR #${t.number}, which is not an open Dependabot pull request in deps-facts.json`);
    q.push({ number: t.number, branch: f.branch, status: "pending", attempts: 0, reason: t.reason, outcome: "", ci: "" });
  }
  save(q);
  console.log(JSON.stringify({ queued: q.length }));
} else if (mode === "next") {
  const q = load();
  const item = q.find((i) => i.status === "pending");
  if (!item) {
    console.log(JSON.stringify({ has_item: false, number: 0, branch: "", attempts: 0, remaining: 0 }));
  } else {
    item.attempts += 1;
    save(q);
    console.log(JSON.stringify({ has_item: true, number: item.number, branch: item.branch, attempts: item.attempts, remaining: remaining(q) }));
  }
} else if (mode === "mark") {
  const q = load();
  const n = Number(input("number"));
  const item = q.find((i) => i.number === n);
  if (!item) fail(`PR #${n} is not in the queue`);
  const outcome = input("outcome"); // pushed | nothing-to-push | needs-person
  const ci = input("ci", false) || "n/a"; // green | red | timeout | n/a
  item.outcome = outcome;
  item.ci = ci;
  if (outcome === "needs-person") item.status = "needs-person";
  else if (ci === "green") item.status = "done";
  else if (item.attempts < MAX_ATTEMPTS && ci === "red") item.status = "pending";
  else item.status = "needs-person";
  if (item.status === "needs-person" && !item.reason.includes("[")) item.reason += ` [after ${item.attempts} attempt(s): ${outcome}, CI ${ci}]`;
  save(q);
  console.log(JSON.stringify({ number: n, status: item.status, remaining: remaining(q) }));
} else fail(`mode must be init, next or mark, got '${mode}'`);
