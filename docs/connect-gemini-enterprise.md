# Connect Gemini Enterprise

Gemini Enterprise, Google's assistant for businesses, connects to PrivacyFence as a **custom MCP
server data store**, in organization mode only. Gemini Enterprise calls MCP servers from Google's
servers, not from your computer, so it needs a PrivacyFence it can reach over public HTTPS. A local
install listens on `localhost` only, on purpose, so Gemini Enterprise cannot use it.

Which deployment you have is explained in
[Local mode and organization mode](how-it-works.md#local-mode-and-organization-mode).

Unlike claude.ai and ChatGPT, Gemini Enterprise does not register itself with PrivacyFence. An
administrator registers it once, then creates the data store in the Google Cloud console. Each
person then signs in once from the Gemini Enterprise web app.

> **Verification pending.** These steps were run end to end on a test deployment, but pinning
> Gemini Enterprise on **Settings → AI systems** has not been verified yet. Progress is tracked in
> [issue 393](https://github.com/privacyfence/privacyfence/issues/393).

## Before you start

- An [organization deployment](org-mode-setup-guide.md) that Google's servers can reach over
  public HTTPS, with a certificate from a publicly trusted certificate authority. A self-signed
  certificate does not work. The examples below use `https://pf.example.com`.
- A Google Cloud project with Gemini Enterprise and an app, and an account that may change the
  project's organization policies and APIs.

The steps below are in the order that works. Enabling the APIs and signing in from the web app are
easy to miss, and without them the tools do not load.

## 1. Allow custom MCP servers

In the Google Cloud console, open **Organization policies** for the project and set
`discoveryengine.managed.disableCustomMcpServerConnector` to **not enforced**. Without this,
Gemini Enterprise blocks custom MCP server data stores.

## 2. Enable the APIs

Enable these three APIs on the project **before** you create the data store:

- Discovery Engine API
- Connectors API (`connectors.googleapis.com`)
- Secret Manager API (`secretmanager.googleapis.com`)

## 3. Register Gemini Enterprise with PrivacyFence

Register the client with PrivacyFence's `/register` endpoint, from any computer:

```bash
curl -s https://pf.example.com/register -H 'Content-Type: application/json' \
  -d '{"client_name":"Gemini Enterprise",
       "redirect_uris":["https://vertexaisearch.cloud.google.com/oauth-redirect"],
       "token_endpoint_auth_method":"client_secret_basic",
       "grant_types":["authorization_code","refresh_token"],
       "response_types":["code"]}'
```

- **`client_name` must be exactly `Gemini Enterprise`.** That is the name PrivacyFence recognises
  as Gemini Enterprise and the registration an administrator pins (see
  [How it is identified](#how-it-is-identified)).
- The redirect URI is Google's and fixed; Gemini Enterprise uses no other.
- The response holds a `client_id` and a `client_secret`. **Keep the secret private**: anyone who
  has it can act as this client. Paste it into the data store form in the next step and do not
  keep other copies.

**PrivacyFence 5.1.0 and earlier** refuse a client that sends its secret with HTTP Basic
([ADR 0108](adr/0108-oauth-sign-in-popups-keep-their-opener-and-basic-clients-need-not-repeat-their-id.md)).
On those releases, register with `"token_endpoint_auth_method":"client_secret_post"` instead, and
untick **Use HTTP Basic Authentication** in the next step.

## 4. Create the data store

In the Gemini Enterprise console, open **Data stores** → **+ Create data store**, search for
**Custom MCP Server**, and choose **Add MCP server**. Fill in the **MCP Server Configuration**
form:

| Field | Value |
|---|---|
| Authentication method | **OAuth 2.0** |
| MCP Server URL | `https://pf.example.com/mcp` |
| Authorization URL | `https://pf.example.com/authorize` |
| Authorization URL Parameters | empty |
| Token URL | `https://pf.example.com/token` |
| Client ID and Client Secret | from the `/register` response |
| Scopes | empty |
| Enable PKCE Support | **ticked.** It is off by default, and PrivacyFence requires it |
| Use HTTP Basic Authentication | ticked (the default); unticked on 5.1.0 and earlier, see step 3 |

Then:

1. Click **Verify Auth** and sign in with an organization account when PrivacyFence's sign-in
   page opens.
2. Click **Continue** to create the data store, and wait until its connector state shows
   **Active**.
3. Connect the data store to your Gemini Enterprise app.

## 5. Each person authorises from the web app

**Every person who uses PrivacyFence from Gemini Enterprise does this once, including the
administrator.** The console's **Verify Auth** does not leave credentials Gemini Enterprise can
use for tool calls; this step does.

1. Open the Gemini Enterprise web app.
2. Click the puzzle-piece button in the chat bar, then **Authorise** on the PrivacyFence row.
3. Sign in with your organization account when PrivacyFence's sign-in page opens.
4. Reload the page.

People then connect their services at `https://pf.example.com/connect`. Access tokens last one hour
and are refreshed silently; after 30 days you sign in again.

## 6. Enable the actions

In the console, open the data store's **Actions**, click **Reload custom actions**, tick the tools
to offer, and click **Enable actions**. The **Status** column may keep showing "Disabled" after
that.

**Gemini Enterprise enables at most 100 actions per data store, and PrivacyFence can list more:**
the test deployment listed 109: the tools of every service its test user had connected, plus the
`privacyfence_*` tools. A tool that is not enabled is invisible to Gemini, so leave out the ones
people rarely ask for, for example:

- `calendar_list_colors`, `calendar_set_event_color`, `calendar_get_event_visibility`,
  `calendar_set_event_visibility`, `calendar_set_working_location`;
- `drive_sheets_insert_dimensions`, `drive_sheets_delete_dimensions`, `drive_sheets_rename_sheet`;
- `gmail_update_filter`.

Keep `privacyfence_status`, `privacyfence_await_approval` and `privacyfence_check_policy`.

**The list reflects whoever authorised last.** PrivacyFence lists the tools of the services the
person who authorised last has connected, while the enabled actions apply to everyone using the
data store. Reload while signed in as someone who has connected every service the organization
uses. A person who has not connected a service still sees its actions, and those calls fail. The
selection has to be made again on every reload; a new tool arrives disabled. A filter that lets an
administrator choose PrivacyFence's tool list for Gemini Enterprise is tracked in
[issue 811](https://github.com/privacyfence/privacyfence/issues/811).

## Confirmations

Two things can ask you before a tool runs:

1. **Gemini's own confirmation.** Before its tool calls Gemini Enterprise shows its own steps,
   ending in **Action Confirmed**.
2. **PrivacyFence's approval card.** A gated call waits for you at `/approvals`, whatever Gemini
   was told or allowed. This is the confirmation that always decides; see
   [Approvals and policy](approvals-and-policy.md).

Gemini Enterprise does not wait for a pending approval. It posts the `/approvals/<id>` link in the
chat and asks you to say when you have approved it. Approve the card, tell Gemini, and it makes the
call again.

PrivacyFence always tells the client what each tool does: reads are read-only, writes are writes,
and the two tools that delete something are destructive
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)).

## Files

Gemini Enterprise cannot move file bytes, in either direction:

- **Download** (for example `drive_download_file`): a file of up to about 75 KB comes back in the
  tool result. A larger one comes back only as a short-lived, one-time link, which Gemini
  Enterprise does not fetch itself; it shows the link, and you open it in your browser
  ([ADR 0097](adr/0097-a-download-link-needs-a-client-that-can-reach-the-server.md)).
- **Upload** (for example `drive_upload_file`, or an email attachment): not possible. Gemini
  Enterprise has no file to send to an upload link.

**To read a document, no download is needed.** `drive_get_file_content` returns a PDF, Word,
PowerPoint or Excel file's text, so Gemini can read it without any link.

Sizes, lifetimes and the organization settings are in [Files](how-it-works.md#files) and
[File delivery](org-mode-setup-guide.md#13-file-delivery).

## How it is identified

In organization mode, every approval card and audit entry names the AI system that asked. Gemini
Enterprise's calls carry the name it was registered with in step 3, `Gemini Enterprise`.

That name is a **claim**: the card marks it **Not verified**, because any program that can reach
the deployment can register under the same name. An administrator can **pin** the registration on
**Settings → AI systems**; the cards for that registration are then verified. See
[Which AI system is asking](how-it-works.md#which-ai-system-is-asking) and
[AI systems](org-mode-setup-guide.md#11-ai-systems).

**Pinning needs a PrivacyFence release that lists Gemini Enterprise as an AI system.** Until then,
the calls are recorded under the claimed name `Gemini Enterprise` and cannot be pinned.

A registration nobody has used for 180 days is removed. A connector in use keeps its registration,
because each token refresh counts as use. Once it is removed, Gemini Enterprise stops working, and
an administrator registers it again (step 3) and updates the data store's client ID and secret.

## Troubleshooting

| What you see | What to do |
|---|---|
| **Verify Auth** or **Authorise** signs you in, but the connector never connects; the deployment's access log shows `/oauth/idp/callback` and then no `POST /token` | The deployment runs 5.1.0 or earlier, whose sign-in page cut off Gemini's popup. Ask your administrator to upgrade to the release that carries [ADR 0108](adr/0108-oauth-sign-in-popups-keep-their-opener-and-basic-clients-need-not-repeat-their-id.md). |
| `/token` refuses the client with "Missing client_id" | The deployment runs 5.1.0 or earlier with **Use HTTP Basic Authentication** ticked. Register a `client_secret_post` client and untick it (step 3), or upgrade. |
| Sign-in or tool calls fail behind a reverse proxy or firewall | It must let Google's servers through: they call `/token` with the User-Agent `Google` and `/mcp` with `python-httpx`. A User-Agent allow-list or bot rule has to accept both. |
| **Reload custom actions** fails with "Could not acquire the stored end-user credentials" | Nobody has authorised from the web app yet. Do [step 5](#5-each-person-authorises-from-the-web-app), then reload. Check that the APIs in [step 2](#2-enable-the-apis) are enabled. |
| The console will not enable all the actions | Gemini Enterprise enables at most 100. See [step 6](#6-enable-the-actions). |
| PrivacyFence's tools do not attach to a new chat | Switch the connector off and on in the chat bar's puzzle-piece menu, then reload the page. |
| The sign-in page refuses you | Your account is not allowed to sign in to this deployment. Ask your administrator. |
| Tools are listed but calls to one service fail | You have not connected that service. Connect it at `/connect`; see [Connecting a service](connecting-a-service.md). |
| A call waits and nothing happens | It is waiting for your approval at `/approvals`. |
| Every card says **Not verified** | Expected until an administrator pins your registration. |

Deployment problems are in the organization guide's
[Troubleshooting](org-mode-setup-guide.md#15-troubleshooting).
