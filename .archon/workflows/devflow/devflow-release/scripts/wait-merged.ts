// One poll of a pull request. Used to wait for a person to merge the changelog PR: finished once it
// is merged; a PR closed without merging fails the run, since the release cannot go on without it.
import { fail, input, must } from "../../.shared/util.ts";

const pr = JSON.parse(must(["gh", "pr", "view", input("number"), "--json", "state,url"]));
if (pr.state === "CLOSED") fail(`${pr.url} was closed without merging; the release cannot go on without its changelog`);
console.log(JSON.stringify({ finished: pr.state === "MERGED", state: pr.state }));
