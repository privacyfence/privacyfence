// The version is never inferred (not from the changelog, not from the last tag): fail loudly when
// the caller did not name one.
import { fail, input } from "../../.shared/util.ts";

if (!input("version", false).trim()) fail("the 'version' input is required, for example --input version=7.1.0 (it is never inferred from CHANGELOG.md or the last tag)");
console.log(JSON.stringify({ ok: true }));
