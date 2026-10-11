# Peek setup

PrivacyFence reaches Peek, a self-hosted server for sharing HTML pages and collecting comments, as
**your own Peek account**.

## What you need

A Peek server you can sign in to. Commenting on a page needs a server with the account comment
endpoint; every other tool works with any Peek server.

## Users connect

In **Settings > Connectors > Peek**, choose **Authenticate…**, enter the server address, approve the
code in the browser that opens, then choose **Done**.

## Values

| Value | Where it comes from |
|---|---|
| Peek server address | Your Peek server's own base URL |

## Build and distribute the bundle

An organization bundle can pin the Peek server address, so users do not type it.

## What the assistant can do

The assistant can list your pages straight away. See the [tools reference](tools-reference.md#peek)
for every Peek tool and its gate.

## Privacy

Visit counts carry visitor names and times only, never IP addresses or browsers.

## Troubleshooting

If sign-in fails, check that the address is exactly the base URL your Peek server uses.
