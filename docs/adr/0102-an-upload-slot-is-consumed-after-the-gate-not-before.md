# ADR 0102: an upload slot is consumed after the gate, not before it

## Status

Accepted — 2026-09-28. Fixes [issue 793](https://github.com/privacyfence/privacyfence/issues/793).
Amends [ADR 0028](0028-clients-without-the-shim-get-capability-urls.md) for when an `upload_id` is
used up, and follows from [ADR 0093](0093-a-pending-approval-releases-nothing.md).

## Context

A client without the `.mcpb` shim uploads a file by calling `privacyfence_create_upload_slot`,
sending the bytes to the returned URL with a `PUT`, and passing the `upload_id` to the tool that
needs the file (ADR 0028). The slot is single-use.

`drive_upload_file` and the `gmail_*_with_attachments` tools read their files at the very top, in
`local_files.require_local_files()`, so the approval card can show the file's size, preview and PII
scan. For an upload, that read was the single-use `UploadStagingStore.claim()`, which deletes the
slot.

Since ADR 0093, a call whose approval goes pending stops at `gated_call`. The agent re-issues the
identical call once the human decides. That re-issue starts from the top again and finds its slot
already gone, so an approved upload could never complete. ChatGPT's Developer Mode check hit this
on the first upload it tried: two approved uploads failed, and ChatGPT fell back to inline base64.
Before ADR 0093 the pending call simply ran on without a decision, which hid the problem.

The slot's 10-minute lifetime was also shorter than an approval's: a pending approval waits up to
15 minutes, and its decision waits up to 5 more for the re-issued call.

## Decision

- **Reading is not consuming.** Before the gate, a call reads its upload with
  `UploadStagingStore.peek()`: the same principal, filled and expiry checks as `claim()`, and the
  same "no oracle" `None`, but the slot stays.
- **The slot is consumed after the gate, before the write.** The connector calls
  `local_files.commit_uploads()` right after `gated_call` returns. It claims every slot the call
  read. If one is already gone, the call fails before writing anything. A slot therefore still backs
  at most one approved write, even when two calls read it before either passed its gate.
- **Reading a slot holds it for as long as an approval can take.** `peek()` extends the slot's expiry
  to at least `DEFAULT_PENDING_TTL_SECONDS + DEFAULT_LEDGER_TTL_SECONDS` (20 minutes) from the read,
  so it outlives the approval it waits on. It never shortens an expiry.
- The same applies to the shim's bridge uploads, which go through the same store.

## Alternatives considered

- **Consume in the dispatcher after the tool call succeeds.** Rejected: the write would already have
  happened when a second call found the slot gone, so one slot could back two writes.
- **Keep the claimed bytes in memory across the pending result.** Rejected: plaintext would outlive
  the tool call in the daemon's memory, keyed by something other than the capability token, and it
  would be lost on a restart while the approval survives.
- **Make the client upload again after approval.** Rejected: the agent re-issues the identical call,
  as ADR 0093 tells it to; a new slot means new arguments, a new approval, and the same failure.

## Consequences

- An upload through a slot works whether or not its approval goes pending, for every client.
- A denied, failed or never re-issued call leaves its slot until the extended expiry (at most 20
  minutes after it was last read) instead of deleting it at once. The ciphertext stays encrypted on
  disk under a key only the token derives, and only the same principal can use it.
- A connector that reads an upload and forgets `commit_uploads()` leaves the slot reusable until it
  expires. Every such connector has a test that the approved call consumes its slot.
