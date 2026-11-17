# ADR 0321: Drive tags are plain properties, and collaborators can forge them

## Status

Accepted — 2026-11-17. Implemented: `src/privacyfence/drive_tags.py`, `src/privacyfence/drive_client.py`.
Relates to [ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md).

## Context

[ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md) stores a tag on the file. Drive offers
two places. `properties` are visible to every app and writable by anyone with edit access to the file.
`appProperties` are visible and writable only by the OAuth client that wrote them. A tag could also carry a
signature that only PrivacyFence can produce.

## Decision

1. **Tags are plain `properties`** named `privacyfence-tag.<name>` with the value `1`, not `appProperties` and
   not signed values. The owner of the project chose this.
2. **The risk, stated plainly.** Anyone with edit access to a file can add or remove its tags, with their own
   tokens or an Apps Script, without PrivacyFence seeing it. A tag rule is therefore exactly as trustworthy as
   the set of people who can edit the tagged files. A read rule can release the content of a file an outsider
   pre-tagged; an update rule can let the agent write into a file an outsider shared and pre-tagged. The risk is
   accepted. The rule's label, the tag tools' descriptions and the reference docs say so.
3. **Only PrivacyFence's own tags are read.** The agent sees tags and never other apps' properties, and raw
   property values never leave the client.

## Alternatives considered

- **`appProperties`.** A collaborator could not forge a tag through their own client, but the owner's other
  scripts and tools could not see or set tags either, which the owner wants to keep usable.
- **An HMAC-signed value with a per-install secret**, settable only by PrivacyFence. A review recommended it; the
  owner declined it, accepting the forgery risk for plain, inspectable, scriptable tags. It is not to be added
  as a "fix" without a new ADR.

## Consequences

- Tags can be listed, set and removed by the owner's own scripts and by Drive's API directly.
- A shared file that a collaborator pre-tagged is covered by a matching rule. Use tag rules only for files whose
  editors you trust, and prefer read-only verbs when in doubt.
- Nothing PrivacyFence does can detect a forged tag.

## Verification

- `tests/unit/test_drive_tags.py` (only the prefixed, valid, value `1` properties become tags), the `DriveFile`
  parsing tests in `tests/unit`, and the wording of the `drive.tag` label.

## Related

- Issue [#1147](https://github.com/privacyfence/privacyfence/issues/1147), plan [`docs/1147/plan.md`](../1147/plan.md).
- [ADR 0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md), [ADR 0322](0322-tag-writing-tools-are-popup-only-and-exempt-from-every-rule.md).
