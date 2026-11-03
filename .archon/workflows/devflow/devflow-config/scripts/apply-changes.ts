// Applies the approved settings changes, one proposal at a time, in the order of config-ops.json
// (written by validate-ops, never taken from the approval text). For each operation: read the
// resource first (the "before" for rollback), make the call, read it again (the "after"). A proposal
// stops at its first failed call; others carry on. Everything is written to config-changes.md.
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { api } from "../../.shared/gh.ts";
import { artifactsDir, input } from "../../.shared/util.ts";

const approved = new Set<string>(JSON.parse(input("approved", false) || "[]"));
const ops: Array<{ id: string; title: string; ops: Array<{ method: string; endpoint: string; body: string }> }> = JSON.parse(readFileSync(join(artifactsDir(), "config-ops.json"), "utf8"));
const log = join(artifactsDir(), "config-changes.md");
writeFileSync(log, `# Settings changes applied\n\n`);

const applied: string[] = [];
const failed: Array<{ id: string; error: string }> = [];
for (const p of ops.filter((o) => approved.has(o.id))) {
  appendFileSync(log, `## ${p.id}: ${p.title}\n\n`);
  let error = "";
  for (const op of p.ops) {
    const getable = op.method !== "POST";
    const before = getable ? api("GET", op.endpoint) : null;
    const r = api(op.method, op.endpoint, op.body || undefined);
    const after = r.status === "ok" && op.method !== "DELETE" ? api("GET", op.endpoint) : null;
    appendFileSync(
      log,
      `### ${op.method} ${op.endpoint}\n\nResult: ${r.status} (${r.code}) ${r.message}\n\nBody sent:\n\n\`\`\`json\n${op.body || "(none)"}\n\`\`\`\n\nBefore (restore from this):\n\n\`\`\`json\n${JSON.stringify(before?.data ?? null, null, 2)}\n\`\`\`\n\nAfter:\n\n\`\`\`json\n${JSON.stringify(after?.data ?? null, null, 2)}\n\`\`\`\n\n`,
    );
    if (r.status !== "ok") {
      error = `${op.method} ${op.endpoint}: ${r.code} ${r.message}`;
      break;
    }
  }
  if (error) failed.push({ id: p.id, error });
  else applied.push(p.id);
}
const skipped = ops.filter((o) => !approved.has(o.id)).map((o) => o.id);
console.error(`applied ${applied.length}, failed ${failed.length}, not approved ${skipped.length}`);
console.log(JSON.stringify({ applied, failed, failed_count: failed.length, skipped, log }));
