// Fails the run before any model spend unless the devflow toolkit, a valid profile, gh and uv are
// all usable. Writes the resolved profile to $ARTIFACTS_DIR/profile.json for every later node.
import { existsSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { artifactsDir, fail, must, onPath, run } from "./util.ts";

const home = process.env.HOME ?? "";

function devflowRoot(): string {
  const candidates = [process.env.CLAUDE_PLUGIN_ROOT ?? "", join(home, ".claude/devflow"), join(process.cwd(), ".claude/devflow")];
  // Plugin installs: ~/.claude/plugins/cache/<marketplace>/devflow/<version>/ and the Claude Project
  // sync location ~/.claude/plugins/synced/<id>/devflow/.
  const cache = join(home, ".claude/plugins/cache");
  if (existsSync(cache)) {
    for (const m of readdirSync(cache)) {
      const d = join(cache, m, "devflow");
      if (existsSync(d)) for (const v of readdirSync(d)) candidates.push(join(d, v));
    }
  }
  const synced = join(home, ".claude/plugins/synced");
  if (existsSync(synced)) for (const s of readdirSync(synced)) candidates.push(join(synced, s, "devflow"));
  const found = candidates.find((d) => d && existsSync(join(d, "scripts/validate_profile.py")));
  if (!found) fail("the devflow toolkit is not installed for this user (no scripts/validate_profile.py in any plugin location)");
  return found;
}

function branchRegex(pattern: string, types: string[] | undefined): string {
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  let re = esc(pattern);
  re = re.replace(esc("<type>"), types && types.length ? `(${types.map(esc).join("|")})` : "[a-z]+");
  re = re.replace(esc("<kebab-case>"), "[a-z0-9]+(-[a-z0-9]+)*");
  return `^${re}$`;
}

const root = devflowRoot();
if (!existsSync(".claude/toolkit.yaml")) fail(".claude/toolkit.yaml not found: this workflow runs only in a repo with a devflow profile");
const v = run(["python3", join(root, "scripts/validate_profile.py"), ".claude/toolkit.yaml", "--resolved"]);
if (v.code !== 0) fail(`the profile does not validate:\n${v.err}`);
if (v.err.trim()) console.error(v.err.trim());
const resolved = JSON.parse(v.out);
writeFileSync(join(artifactsDir(), "profile.json"), JSON.stringify(resolved, null, 2));

if (run(["gh", "auth", "status"]).code !== 0) fail("gh is not authenticated: the PR steps need it");
if (!onPath("uv")) fail("uv is not on PATH: the setup node builds the project venv with it");
must(["git", "remote", "get-url", "origin"]);

console.log(
  JSON.stringify({
    devflow_root: root,
    main_branch: resolved.git.main_branch,
    branch_regex: branchRegex(resolved.git.branch_pattern ?? "<type>/<kebab-case>", resolved.git.branch_types),
  }),
);
