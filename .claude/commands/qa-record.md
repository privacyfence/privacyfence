---
description: Record one connector's live QA fixture on the self-hosted runner and pull it back
argument-hint: "<connector>  (one of CONNECTOR_CHECKS in scripts/qa_fixture_recorder.py)"
---

Record the live QA fixture for: **$ARGUMENTS**

No connector's live OAuth credentials exist on this machine and none ever will — `docs/testing-policy.md`
is explicit that the real QA grants live only on the self-hosted runner. So this does not run
`scripts/qa_fixture_recorder.py` locally. It dispatches the workflow that can.

1. **Check the connector name is real** before dispatching — it must appear in `CONNECTOR_CHECKS` in
   `scripts/qa_fixture_recorder.py`. A name that isn't there makes the recorder report "has no
   recorder implementation yet" and then *exit 0*: a green run that recorded nothing. If no
   connector was named above, ask me which one rather than guessing.

   For a connector that is new, its token must first be connected on the QA server
   (`https://runner.privacyfence.eu`, `/connect`), which needs the connect pull request merged and
   deployed (`docs/connector-qa.md`, "The QA server"). Without it the run has no token to copy.

2. **Dispatch `qa-record-fixture.yml`** with `ref` set to `main` and inputs `connector` set to
   that name and `branch` set to the current branch (it must already be pushed). It runs on the
   `privacyfence-test` self-hosted runner, which admits only the workflow as it stands on `main`;
   a GitHub-hosted job then commits the recorded fixture to the named branch. It refuses `main`,
   `stable`, `releases/*`, `refs/…`, `pull/…` and any branch that does not exist.

3. **Expect to queue.** It shares a concurrency group (`connector-live-check`,
   `cancel-in-progress: false`) with `connector-live-check.yml`, because both copy the runner's one
   persistent credential store into their checkout and copy refreshed tokens back out. A queued run
   is correct behaviour. Wait; do not re-dispatch, and do not cancel the run that is holding the group.

4. **When it finishes**, `git pull` so the committed fixture reaches this checkout, then **read the
   fixture diff** and confirm no real account identifier, tenant URL, access token or private
   content entered the repository. The recorder redacts, but `docs/connector-qa.md` ("Reviewing recorded fixtures") says to
   review the diff anyway — a fixture that arrived by dispatch gets exactly the same read as one
   recorded by hand.

5. **Report** the run URL, whether the fixture changed, and what you saw in the diff. If the run
   failed, give me the actual error from its log — an expired QA grant needs a reconnect on the
   runner and is not something to retry.
