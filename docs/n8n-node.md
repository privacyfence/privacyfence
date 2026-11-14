# The PrivacyFence node for n8n

The **PrivacyFence** node lets an n8n workflow call a tool your PrivacyFence server allows it to use. The
address comes from the credential, the tool picker lists only what the token may call, the `reason` is a
field of its own, and the result is the tool's JSON. The same node can be handed to an AI Agent as a tool.

It is the community node `n8n-nodes-privacyfence`, published on npm. It talks to PrivacyFence's `/mcp`
address with a [scoped token](approvals-and-policy.md), so every call goes through the same gate as any other
client's.

## Requirements

- A **self-hosted** n8n that allows community nodes.
- To have PrivacyFence put the credential into n8n for you
  ([Delivering tokens to n8n](org-mode-setup-guide.md#delivering-tokens-to-n8n)): n8n **2.40.0 or later**.
  Using the node with a credential you create by hand has no such requirement.
- A PrivacyFence server that n8n can reach, and a scoped token (**Settings → Scoped tokens**).

## Install

In n8n open **Settings → Community nodes → Install** and enter `n8n-nodes-privacyfence`. On a self-hosted n8n
you can instead run `npm install n8n-nodes-privacyfence` in `~/.n8n/nodes` and restart n8n. The editor's
node picker then lists **PrivacyFence**.

## The credential

Create a credential of type **PrivacyFence API** with two fields:

| Field | What to enter |
|---|---|
| **PrivacyFence MCP address** | Your server's `/mcp` address, for example `https://privacyfence.example.org/mcp` |
| **Token** | A scoped PrivacyFence token |

Press **Test** on the credential: it reads the tool list and answers "Connected to PrivacyFence" with the
number of tools the token can call. The node sends the token only to this address and never follows a
redirect.

## A worked example

Suppose the token covers one auto-approved tool, `drive_list_shared_drives`.

1. Add a **PrivacyFence** node and pick the credential.
2. **Operation**: *Call a Tool*. **Tool Name or ID**: pick `drive_list_shared_drives`. The list shows only the tools
   this token may call.
3. **Reason**: the default is `n8n workflow: <the workflow's name>`. PrivacyFence records it in its audit
   log; change it to say why this call is made.
4. **Arguments**: the tool's arguments as a JSON object, without `reason`:

   ```json
   {}
   ```

   (This tool takes no arguments; for one that does, put them here, written as `{"name": "value"}`.)

5. Run the node. The item it returns is the tool's own JSON. A result that
   is an array or a plain value comes back as `{"result": ...}`, and text that is not JSON as `{"text": ...}`.

The **List Tools** operation returns one item per tool the token may call, with its `name`, `description` and
`readOnly`.

The node has no address field of its own and no way to override the credential's address.

## Using it as an AI Agent tool

Connect the PrivacyFence node to an **AI Agent** node's tool input. The agent sees the node's own parameters,
not the PrivacyFence tool's input schema: it chooses the tool and writes the `arguments` JSON object itself.
Give the agent a clear description of the tools it may use, and keep the token's rules narrow.

## When a call is refused

PrivacyFence refuses a call the token does not cover, and a call that would need a person's approval is
refused at once, so a workflow never waits for one. The node stops with PrivacyFence's own text, which begins
`Request denied`, for example:

```
Request denied: ...
```

Turn on **Continue On Fail** in the node's settings to get an error item `{"error": "Request denied ..."}`
for that input instead, and let the workflow carry on.

A tool that needs a file from the person's computer is refused with `This tool needs a file from the
person's computer, which n8n cannot provide.`

## Having PrivacyFence deliver the credential

An administrator can make PrivacyFence create this credential in n8n for an AI client's token proposal.
Under **Admin → Agent runtimes** set **Credential type in n8n** to **PrivacyFence node**. The
**PrivacyFence MCP address for n8n** is then required, because the credential keeps it. The setting is
**server-wide**: every delivery creates a PrivacyFence API credential, and an AI Agent in n8n uses one
PrivacyFence node per call instead of the MCP Client Tool. The default, **MCP Client node**, delivers n8n's
built-in bearer credential and needs no community node. The steps are in
[Delivering tokens to n8n](org-mode-setup-guide.md#delivering-tokens-to-n8n).

If the community node is not installed in that n8n, delivery says so and mints no token.

## What the credential exposes

The PrivacyFence API credential holds the token **and the address it is sent to**. Anyone who can edit the
credential in n8n can change the address and receive the token at a host of their own, in the same way anyone
who can edit workflows could attach a bearer credential to an HTTP Request node. Give the token narrow rules
and a short lifetime, and let only people you trust edit workflows and credentials in that n8n
([Security and compliance](security-and-compliance.md#tokens-delivered-to-n8n)).

## Troubleshooting

The node's errors are fixed texts; `<host>` is the host of the credential's address. None contains the
token.

| Message | Meaning and what to do |
|---|---|
| `PrivacyFence at <host> could not be reached.` | n8n could not connect, or the call timed out. Check the address in the credential, that the server is running and reachable from n8n, and the node's **Timeout** option (default 60000 ms). |
| `PrivacyFence at <host> refused the token (HTTP 401).` (or 403), `it may be revoked or expired.` | The token is revoked, expired or wrong. Create a new token in **Settings → Scoped tokens** and update the credential. |
| `PrivacyFence at <host> redirected the request; check the PrivacyFence MCP address in the credential.` | The address redirects, for example from `http` to `https` or to another path. The node never follows a redirect: enter the final `/mcp` address. |
| `PrivacyFence at <host> did not speak MCP as expected.` | The address answers, but is not a PrivacyFence `/mcp` endpoint (a proxy, a login page, a wrong path), or it speaks an MCP revision the node does not. Check the path ends in `/mcp`. |
| `PrivacyFence at <host> answered HTTP <status>.` | Any other HTTP answer. Check the server's log and any proxy in front of it. |
| `PrivacyFence at <host> answered an MCP error: <message>` | PrivacyFence answered with an error; the message is its own, cut to 300 characters. |
| `The PrivacyFence credential needs an address and a token.` | A field of the credential is empty. |
| `The arguments must be a JSON object.` | **Arguments** is not a JSON object such as `{}`. |
| `The PrivacyFence node failed unexpectedly.` | An error the node does not describe, on purpose, so that nothing sensitive reaches the item. Run the node again and check the PrivacyFence audit log. |
| `n8n does not know the PrivacyFence credential type. Ask the n8n administrator to install the community node n8n-nodes-privacyfence, then try again.` (`NODE_NOT_INSTALLED`) | Shown by PrivacyFence to the person or AI client when delivery is set to the PrivacyFence node and n8n has no such credential type. Install the community node in that n8n, restart it if it asks, and try again. No token was created. |
