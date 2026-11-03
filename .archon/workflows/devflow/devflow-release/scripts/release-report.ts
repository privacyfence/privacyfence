// After a real cut: finds the build.yml and publish-pypi.yml runs the tag push started on the
// tagged commit. A tag with no build.yml run means the release did not start (the tag exists but
// nothing builds): that fails the run with the recovery the docs give, which is not a retry.
import { fail, input, must } from "../../.shared/util.ts";

const version = input("version");
const sha = input("main_sha");
const deadline = Date.now() + 10 * 60_000;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const find = (workflow: string): string => {
  const runs = JSON.parse(
    must(["gh", "run", "list", "--workflow", workflow, "--event", "push", "--limit", "10", "--json", "url,headSha,headBranch"]),
  ) as Array<{ url: string; headSha: string; headBranch: string }>;
  return runs.find((r) => r.headSha === sha && r.headBranch === `v${version}`)?.url ?? "";
};

let build = "";
let pypi = "";
while (Date.now() < deadline && !(build && pypi)) {
  build ||= find("build.yml");
  pypi ||= find("publish-pypi.yml");
  if (!(build && pypi)) await sleep(15_000);
}
if (!build) {
  fail(`tag v${version} was pushed but build.yml never started on ${sha.slice(0, 7)}: the tag exists and the release did not start. Delete the tag and push it again; do not retry the dispatch (docs/releasing.md).`);
}
console.log(JSON.stringify({ tag: `v${version}`, build_url: build, publish_pypi_url: pypi || "not started within 10 minutes" }));
