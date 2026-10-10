# ADR 0147: PII number patterns ignore digits glued to a decimal point

## Status

Accepted — 2026-10-10. Implemented.

## Context

A Grist `grist_get_records` read of an Opportunities table raised the PII confirmation on every
read, and all 25 matches were false alarms. Each came from one side of a decimal number in a float
cell. `\b` sits between `.` and the first fraction digit, so the fraction of `0.6838738069560423`
was tested as a card number on its own (about one random fraction in ten passes Luhn), and the
fraction `8288798133` in `14908.8288798133` is a standalone 10-digit run starting with 8, which is
a Hungarian tax ID. The Luhn check was already in place. Every connector that shows long floats has
the same problem.

## Decision

The "Credit card number" and "Hungarian tax ID" patterns get two lookarounds, defined once in
`pii_detector.py`:

- `(?<!\d\.)` in front: a digit run with `<digit>.` right before it is never a match.
- `(?!\.0*[1-9])` behind: a digit run followed by a non-zero fraction is never a match.

A zero fraction is the exception: `8123456789.0` (how Grist and `json.dumps` render a whole float)
and `8123456789.` at the end of a sentence still match, so an ID kept in a numeric column is caught.
Nothing else about either pattern changes.

## Alternatives considered

- **Also treating `,` as a decimal separator** (Hungarian `14908,8288798133`). Drive CSV files join
  cells with commas and no spaces, so a real card number or tax ID next to a numeric cell would stop
  matching. For this gate, missing real PII is worse than one extra confirmation.
- **A stricter Luhn check or a card BIN/IIN prefix check.** Luhn exists already, a prefix check does
  nothing about decimal fractions, and it needs a BIN list someone maintains.
- **Skipping float cells in the Grist connector.** Salesforce and Sheets reads have the same false
  positive; fixing the pattern fixes all of them.

## Consequences

- Long floats no longer raise the PII confirmation.
- Whole integers in Int columns (for example `8500000000`) are still flagged as a tax ID; that is the
  pattern working as intended.
- A Hungarian-locale decimal (`14908,8288798133`) can still raise one extra confirmation.
