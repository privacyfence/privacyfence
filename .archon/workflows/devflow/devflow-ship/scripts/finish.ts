// The run's result: whether a person merged the PR. A PR still open here means the watch loop ran
// out of polls; that fails the run rather than reporting a quiet "not merged".
import { fail, input, must } from "../../.shared/util.ts";

const pr = JSON.parse(must(["gh", "pr", "view", input("number"), "--json", "state,url"]));
if (pr.state === "OPEN") fail(`stopped watching ${pr.url} while it is still open; resume the run to keep watching`);
console.log(JSON.stringify({ merged: pr.state === "MERGED", state: pr.state, url: pr.url }));
