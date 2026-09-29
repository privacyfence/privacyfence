# ADR 0109: Gemini Enterprise is matched by the name its admin registers it with

## Status

Accepted — 2026-09-29. Implemented.
Amends [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
(decision 2's registry) and
[ADR 0094](0094-claude-clients-are-matched-by-their-observed-names.md) (decision 1's rule that the
registry holds only observed names).
Part of [issue 393](https://github.com/privacyfence/privacyfence/issues/393).

## Context

[ADR 0107](0107-gemini-cli-and-antigravity-are-not-supported-clients.md) made Gemini Enterprise the
Gemini client PrivacyFence supports. The maintainer's verification against a test organization
deployment on 2026-09-29
([status](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5887866056),
[tools load](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5889243926))
found:

- Gemini Enterprise supports neither Dynamic Client Registration nor OAuth discovery. The admin
  registers its OAuth client on PrivacyFence by hand, through PrivacyFence's existing `/register`,
  and pastes the returned client ID and secret into Google's console. The admin writes the
  registration's `client_name`; Google never sends one. Every test registration used
  `"client_name": "Gemini Enterprise"`, and the setup instructions will prescribe exactly that.
- Its MCP runtime sends the HTTP `User-Agent` `python-httpx/0.27.0`; its token exchange sends
  `Google`.
- Its handshake `clientInfo.name` was not yet read out of the daemon log when this was decided.
- Settings → AI systems offered no Gemini Enterprise entry to pin a registration to, only
  "Gemini CLI", which is a different product
  ([ADR 0107](0107-gemini-cli-and-antigravity-are-not-supported-clients.md)).

ADR 0094 decision 1 holds the registry to names a client was seen sending, and ADR 0103 applied
that to ChatGPT's DCR `client_name`. Gemini Enterprise has no name of its own at registration: the
only DCR name there is is the one PrivacyFence's instructions tell the admin to type.

## Decision

### 1. `gemini-enterprise` matches `Gemini Enterprise`, the prescribed DCR `client_name`

`agent_identity.REGISTRY` gains `gemini-enterprise`, display name "Gemini Enterprise", matching the
exact name `Gemini Enterprise` (case-insensitive, as every entry). This is the one entry keyed on a
name PrivacyFence prescribes rather than one a vendor was observed sending. It is still observed,
in the sense ADR 0094 asks for: it is what every real Gemini Enterprise registration carried. And
it is no weaker a claim than any other registry name, because every DCR `client_name` is
caller-chosen text; it is only ever `client_info`, never attested, until an admin pins the
registration.

### 2. The handshake name is added when observed, and no other name is guessed

The handshake `clientInfo.name` is added to the same entry, in a new ADR, once the maintainer's
capture is posted on issue 393. Until then it is not guessed. It matters only when the admin
registers under a different `client_name`: attribution then falls to the handshake name (ADR 0094
decision 3), which the registry does not yet recognise, so the call is recorded as
`unknown:<the admin's name>`, and the admin can still pin that registration to
`gemini-enterprise`.

### 3. The `User-Agent` is not a name

`python-httpx/0.27.0` gets no entry. Attribution reads MCP and DCR claims only (ADR 0035), and a
generic HTTP library's name would name every Python client Gemini Enterprise.

### 4. It gets its own icon

`resources/agent_icons/gemini-enterprise.png` is the Gemini mark from Lobe Icons (MIT), as
`gemini-cli.png` is Gemini CLI's; see that directory's README. It appears only on the in-app
approval card and list, for an attested identity (ADR 0035 decision 4), never on the website
([ADR 0101](0101-the-website-shows-no-third-party-logos.md)).

## Consequences

- An admin can pin a Gemini Enterprise registration on Settings → AI systems; its cards then show
  Gemini Enterprise as verified.
- An unpinned registration named "Gemini Enterprise" is attributed to Gemini Enterprise, and its
  cards say "Says it is Gemini Enterprise" and **Not verified**.
- The setup instructions must keep prescribing `"client_name": "Gemini Enterprise"`. Changing that
  wording without changing this entry turns new registrations into `unknown:<name>`.
- Anyone who can call `/register` can name a registration "Gemini Enterprise". That is the same
  exposure every registry name already has, and a pin, not the name, is what verifies.
- The registry no longer means "names vendors send" only; the comment on the entry says which kind
  its name is.

## Alternatives considered

- **Wait for the handshake name and key the entry on it alone.** Rejected: the DCR name is the one
  an admin sees on Settings → AI systems (ADR 0094 decision 3), and without an entry there is
  nothing to pin to, which is what blocked the verification's pinning step.
- **Pin Gemini Enterprise registrations to `gemini-cli`.** Rejected: Gemini CLI is a separate,
  unsupported product (ADR 0107), and a pin is an admin's statement of which product is asking.
- **Match the `User-Agent`.** Rejected, as for ChatGPT in
  [ADR 0103](0103-chatgpt-is-matched-by-its-registered-name.md): attribution does not read HTTP
  headers, and this header names a library, not a product.
- **Accept any `client_name` containing "Gemini".** Rejected: ADR 0035's matching is exact, never
  substring, so that a name merely containing a product's name is not that product.
