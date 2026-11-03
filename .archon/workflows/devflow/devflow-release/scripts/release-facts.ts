// Read-only facts for a release, taken at origin/main's tip (the commit release.yml tags): the
// channel, the recent tags, the rendered release notes, and the OAuth relay's health. Detaches the
// worktree at origin/main so scripts/ and CHANGELOG.md are the ones that will be tagged.
// A malformed version fails the run. A missing changelog section does not: it is reported, because
// the next node writes it.
import { fail, input, must, run } from "../../.shared/util.ts";

const version = input("version").replace(/^v/, "");
must(["git", "fetch", "--tags", "origin", "main"]);
must(["git", "checkout", "--detach", "origin/main"]);
const sha = must(["git", "rev-parse", "HEAD"]);
const subject = must(["git", "log", "-1", "--format=%s", sha]);

const ch = run(["python3", "scripts/r2_release.py", "channel", version]);
if (ch.code !== 0) fail(`'${version}' is not a release version:\n${ch.err.trim() || ch.out.trim()}`);
const channel = ch.out.trim();

const tags = must(["git", "tag", "--sort=-creatordate"]).split("\n").slice(0, 8);
if (tags.includes(`v${version}`)) fail(`tag v${version} already exists`);
const onCommit = must(["git", "tag", "--points-at", sha]);

const stable = channel === "stable";
let changelogOk = !stable; // only a stable release renders notes from CHANGELOG.md
let notes = "";
let changelogError = "";
if (stable) {
  const cl = run(["python3", "scripts/changelog_section.py", version]);
  changelogOk = cl.code === 0;
  notes = cl.out;
  changelogError = changelogOk ? "" : cl.err.trim().split("\n")[0];
}

const repo = must(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]);
const tokenAdmin = must(["gh", "api", `repos/${repo}`, "-q", ".permissions.admin"]) === "true";

let relay = "unreachable";
try {
  const r = await fetch("https://oauth.privacyfence.eu/health", { method: "HEAD", signal: AbortSignal.timeout(15000) });
  relay = String(r.status);
} catch (e) {
  relay = `unreachable (${(e as Error).message})`;
}

console.log(
  JSON.stringify({
    version,
    channel,
    main_sha: sha,
    main_subject: subject,
    tags_on_commit: onCommit,
    recent_tags: tags,
    changelog_ok: changelogOk,
    changelog_error: changelogError,
    notes,
    relay_status: relay,
    token_admin: tokenAdmin,
  }),
);
