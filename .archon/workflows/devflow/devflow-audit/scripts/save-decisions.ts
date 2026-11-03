// Saves the maintainer's answer to the audit's open questions where the planner reads it.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, input } from "../../.shared/util.ts";

const path = join(artifactsDir(), "audit-decisions.md");
writeFileSync(path, `# Maintainer decisions on the audit's open questions\n\nVerbatim answer:\n\n${input("answer", false)}\n`);
console.log(JSON.stringify({ path }));
