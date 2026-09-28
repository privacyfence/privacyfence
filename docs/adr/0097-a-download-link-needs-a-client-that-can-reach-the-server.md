# ADR 0097: A download link needs a client that can reach the server; `drive_get_file_content` returns a document's text

## Status

Accepted — 2026-09-28. Amends [ADR 0092](0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)'s
premise that "a link works for every client"; its limit and its default are unchanged. Refines
[ADR 0028](0028-clients-without-the-shim-get-capability-urls.md).

## Context

ADR 0092 lowered organization mode's inline-download limit to a 100,000-byte tool result, so any
file over about 75 KB now reaches claude.ai as a one-time link
(`https://<host>/mcp-files/fetch/<token>`, ADR 0028). It said a link "works for every client" and
that claude.ai "fetches itself". Neither had been tested on claude.ai: the only claude.ai run in
the 5.0.0 client QA ([M1.3](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865753049))
received its file inline, and its access log has no `/mcp-files/fetch/` request. Every link fetch
on record was Claude Code running `curl`.

On 5.0.0, an organization deployment asked from claude.ai for seven PDFs from Drive, all over the
inline limit. All seven were approved and none could be read. claude.ai fetches a link from its
own sandbox, and that sandbox could not reach the deployment's host. claude.ai reaches a host
from there only when it is on **Settings → Capabilities → Domain allowlist**. Uploads take the
same route: `privacyfence_create_upload_slot`'s `upload_url` is `PUT` to from that sandbox too.

The agent then tried `drive_get_file_content` for the text. For a PDF it returned only
`[binary content — N bytes; use drive_download_file to save it]`, pointing back at the link it
could not open. PrivacyFence already extracts a PDF's text (`text_extraction.extract_text`), but
only for the PII scan and the approval card, never for the result. The call also fetched at most
100 KB, and pypdf cannot parse a PDF without its end.

## Decision

1. **A link works for a client that can reach the server's host.** That is a requirement of the
   deployment, not something PrivacyFence can do for the client. For claude.ai, the host goes on
   **Settings → Capabilities → Domain allowlist**; the setup steps in
   [Connect claude.ai](../connect-claude-ai.md) say so, and so does
   [File delivery](../org-mode-setup-guide.md#13-file-delivery). ADR 0092's limit stays: a
   larger inline result is truncated by claude.ai whatever its network settings.
2. **`drive_get_file_content` returns a PDF, DOCX, PPTX or XLSX file as its extracted text** (a
   `.zip` as its entry list), in both modes. When the capped first fetch was truncated, the file
   is fetched again in full, up to 20,000,000 bytes, so pypdf gets the whole document. The text is
   cut so that its JSON encoding, the form the tool result carries, is at most 100,000
   characters (the same figure as ADR 0092, for the same reason), and the result then says
   `"truncated": true`. The approval card shows exactly that text, and the PII scan reads it.
   A file with no text layer, or over the full-fetch cap, gets a placeholder saying why.
3. **The approval card's PDF embed still comes from the capped first fetch.** It puts the whole
   PDF into the card as a data URI with no size limit of its own, so it keeps the 100 KB bound it
   always had instead of growing to 20 MB.

## Alternatives considered

- **Raise the inline limit again for claude.ai.** Rejected: that is what failed in M1.3. The
  client truncates, and it does so silently.
- **Pick the delivery per client** (inline for claude.ai, links elsewhere). Rejected for the
  reasons ADR 0092 already gives: the client's identity is not trustworthy enough to size a
  response on (ADR 0035), and inline cannot carry a file over the limit anyway.
- **Return the PDF as an MCP embedded resource.** Not done: it is the same bytes in the same tool
  result, so the same limit applies, and how claude.ai treats a PDF resource is untested.
- **Extract text with the PII scan's 20,000-character cap.** Rejected: that cap sizes a regex scan
  and a preview pane. When the text is the result itself, the ceiling is what the client accepts.
- **Cap the text by characters.** Rejected: `json.dumps` escapes every non-ASCII character as
  `\uXXXX`, six characters on the wire, so a Hungarian document of 100,000 characters could exceed
  the client's limit. Measuring the encoded text is exact.

## Consequences

- An organization that uses claude.ai adds its PrivacyFence host to claude.ai's domain allowlist,
  or larger downloads and every upload fail from there. The failure is the client's, after the
  approval: PrivacyFence cannot see it.
- Reading a document no longer needs a download at all when its text is what is wanted, on any
  client and in both modes. Figures, layout and a scanned page's content are not in the text; for
  those the file itself is still `drive_download_file`'s job.
- A `drive_get_file_content` call on a document over 100 KB now downloads it twice before the
  approval card appears: the capped first 100 KB, then the whole file. `drive_download_file`
  already reads a file in full for its PII scan.
- `gmail_download_attachment` and `confluence_download_attachment` have no text-returning
  counterpart yet; on claude.ai without the allowlist entry their larger files remain out of reach.
