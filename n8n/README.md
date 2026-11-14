# n8n-nodes-privacyfence

An [n8n](https://n8n.io) community node for [PrivacyFence](https://github.com/privacyfence/privacyfence).
The **PrivacyFence** node calls a tool your PrivacyFence server lets a workflow use. The address comes
from the credential, the tool picker lists only what the token may call, `reason` is a field of its
own, and the result is the tool's JSON. A refused call stops the node with PrivacyFence's own text, or
becomes an error item when **Continue On Fail** is on. The node can also be used as a tool by an AI Agent.

## Install

In n8n, open **Settings → Community nodes → Install** and enter `n8n-nodes-privacyfence`. On a
self-hosted n8n you can instead run `npm install n8n-nodes-privacyfence` in `~/.n8n/nodes` and restart.

## Credential

Create a **PrivacyFence API** credential with:

- **PrivacyFence MCP address**: your server's `/mcp` address, for example `https://privacyfence.example.org/mcp`.
- **Token**: a scoped PrivacyFence token.

The node sends the token only to that address and never follows a redirect. Press **Test** on the
credential to check the connection.

## Usage

1. Add the **PrivacyFence** node and pick your credential.
2. Choose **Call a Tool**, pick the tool and type a **Reason** (PrivacyFence records it in its audit log).
3. Put the tool's arguments in **Arguments** as a JSON object, without `reason`.

**List Tools** returns one item per tool the token may call.

## More

Documentation: https://github.com/privacyfence/privacyfence/blob/main/docs/n8n-node.md
