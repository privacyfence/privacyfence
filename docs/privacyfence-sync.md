# privacyfence-sync

`privacyfence-sync` runs a data-sync recipe against PrivacyFence with no AI model involved, so a
regular refresh costs no model tokens. A recipe reads from your connected services (Salesforce
reports, Jira searches, Grist tables, Drive files), runs your own scripts on the files, writes the
result back to Grist, and can publish an HTML dashboard on Peek.

## Who it is for

You design the sync with Claude once: the Grist data model, the sources, the transforms, the
dashboard. `privacyfence-sync` then repeats it on a schedule. It is an ordinary client of
PrivacyFence, like Claude Code: every call goes through the gate, your rules and the audit log, and
it holds none of your service credentials, cannot approve anything and cannot change rules. It
declares an unattended session (see [Unattended sessions](how-it-works.md#unattended-sessions)),
so a step that no rule covers fails at once instead of waiting for a card nobody will answer.

## Install

Python 3.11 or newer, in its own virtual environment, together with anything your scripts import:

```sh
python3 -m venv ~/.venvs/privacyfence-sync
~/.venvs/privacyfence-sync/bin/pip install privacyfence-sync
```

It needs no PrivacyFence install of its own, so it works against an organization server from a
computer that has none.

## Connect

**Local mode.** Get your AI client token with `privacyfence-app --print-mcp-token` (or **Copy AI
Client Token** in the companion app) and put it in the environment variable `PRIVACYFENCE_TOKEN` of
the process that runs `privacyfence-sync`. A token is never accepted as a command-line argument;
`--token-env NAME` names another variable. The server defaults to `http://127.0.0.1:8765/mcp`
(`--server`, the recipe's `server`, or `PRIVACYFENCE_MCP_URL`).

**Organization mode.** Sign in once with `privacyfence-sync login --server https://<host>/mcp`.
Runs use the stored sign-in as long as `PRIVACYFENCE_TOKEN` is not set. A sign-in lasts 30 days;
after that a run exits with code 4 and you sign in again.

**Unattended sessions must be allowed.** Both modes need `unattended_sessions.enabled` in the
organization bundle (`build_org_bundle.py --enable-unattended-sessions`, see the
[configuration reference](configuration-reference.md)). Without it a run stops before its first
step.

## The rules a recipe needs

Every step needs an auto-accept rule, because a scheduled run gets no cards. Create them in
**Settings → Auto-accept → Add a rule** (each asks for your passkey): the report, Jira project,
Drive folder, Grist document and Peek page the recipe touches, with the verbs its steps use. The
export tools and a Peek page upload also need the **personal data** checkbox on their rule if the
data holds personal data ([Letting personal data through](approvals-and-policy.md#letting-personal-data-through)).

To find a missing rule, run the recipe once with `privacyfence-sync run recipe.yaml --attended`:
each uncovered step shows a card, and the card's **Always allow** button or Settings adds the rule.
Then run it without `--attended` and no card should appear.

## The recipe-writing plugin

The `privacyfence-sync` plugin gives Claude a skill that writes recipes, their scripts, dashboard
templates and the list of rules for you, and ports an existing data-sync skill.

- **Claude Code:** `/plugin marketplace add privacyfence/privacyfence#stable`, then
  `/plugin install privacyfence-sync@privacyfence`.
- **Claude Cowork:** Plugins → Add → Add marketplace, and enter `privacyfence/privacyfence#stable`.
- **Claude Cowork and claude.ai, by file:** download `privacyfence-sync-plugin.zip` from the
  [latest release](https://github.com/privacyfence/privacyfence/releases/latest) and upload it as a
  plugin.

`#stable` is a branch that each stable release moves to its own commit, so the marketplace follows the
latest stable release. Without `#stable` it follows `main`, which can be newer than the
`privacyfence-sync` you installed. For an exact version, use the release zip or `#v<version>`.

## The recipe format

The recipe format, step types, script steps, dashboard templates, scheduling, exit codes and limits
are in the
[`privacyfence-sync` README](https://github.com/privacyfence/privacyfence/blob/main/sync/README.md)
on GitHub. Why it is a separate client: [ADR 0161](adr/0161-privacyfence-sync-is-a-separate-plain-mcp-client.md).
