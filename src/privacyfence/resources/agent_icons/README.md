# Agent icons

The brand mark of each AI system in `agent_identity.REGISTRY`, shown beside its name on the
approval card and in the approval list (see `approval_icons.agent_icon_path()`).

One PNG per registry entry, named for its `agent_id`:

- `claude.png`
- `claude-code.png`
- `chatgpt.png`
- `gemini-cli.png`
- `cursor.png`

**Where a mark may appear is decided in
[ADR 0006](../../../../docs/adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md)
decision 4 and
[ADR 0035](../../../../docs/adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
decision 4, not here:** only an **attested** identity gets its mark. A claimed one (a handshake
`clientInfo.name` that happens to match a registry entry) is shown as "Says it is …" with no mark,
and an unrecognised one as "Unrecognised AI system". Adding a file here never widens that.

These files are bundled and read from disk. They are never fetched at runtime, and nothing ever
reads `clientInfo.icons` or `website_url`: a caller-supplied URL on the approval card would be both
a tracking beacon and a borrowed brand mark (ADR 0035 decision 2).

## Current sources

Each file is the vendor's mark as redistributed by an established icon set, rendered once from that
set's SVG to a 256×256 transparent PNG with headless Chromium. No artwork was edited; the only
change is the fill colour, set to the set's own recorded brand colour for monochrome SVGs.

| File | Source | Set licence | Colour |
| --- | --- | --- | --- |
| `claude.png` | [Simple Icons](https://simpleicons.org) 16.32.0, `icons/claude.svg` (npm `simple-icons`); Simple Icons records its source as `https://claude.ai` | CC0-1.0 | `#D97757` (Simple Icons' `hex`) |
| `claude-code.png` | Simple Icons 16.32.0, `icons/claudecode.svg`; source recorded as `https://code.claude.com` | CC0-1.0 | `#D97757` |
| `cursor.png` | Simple Icons 16.32.0, `icons/cursor.svg`; source recorded as `https://cursor.com/brand` (Cursor's brand page) | CC0-1.0 | `#000000` |
| `chatgpt.png` | [Lobe Icons](https://github.com/lobehub/lobe-icons) 1.95.1, `icons/openai.svg` (npm `@lobehub/icons-static-svg`) — the OpenAI mark, which ChatGPT uses. Simple Icons no longer carries an OpenAI mark | MIT | `#000000` |
| `gemini-cli.png` | Lobe Icons 1.95.1, `icons/geminicli-color.svg` — Gemini CLI's own terminal mark, in its own colours | MIT | as published |

Both sets' licences cover their SVG data only. **The marks themselves are trademarks of their
owners** (Anthropic, Anysphere, OpenAI, Google), used here nominatively, to tell the person
approving a request which product is asking. Follow each owner's usage rules (no recolouring
beyond the brand colour, no distortion, no implied endorsement) rather than editing a file to fit.

`cursor.png` and `chatgpt.png` are black marks. The card and the list draw every agent mark on a
light rounded tile, so they stay visible in dark mode without recolouring the mark.

To replace a file with one taken directly from the vendor's own brand or press kit, keep the
filename, keep it square, and update this table.
