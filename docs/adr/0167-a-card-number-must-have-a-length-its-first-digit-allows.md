# ADR 0167: A card number must have a length its first digit allows

## Status

Accepted — 2026-10-12. Implemented.

## Context

A `salesforce_run_report` read raised the PII confirmation on a report with no payment data. The
match was a 13-digit device serial, `1912401001297`, in an opportunity's notes ("2x SN:
1912401001421 , 1912401001297"). It passes Luhn by chance, as about one random number in ten does,
and it is ungrouped, so `_card_grouping_valid` lets it through. About ten other serials in the same
report fail Luhn.

No card network issues a 13-digit number starting with 1. The first digit of a card number is its
major industry identifier (ISO/IEC 7812), and each network uses a fixed set of lengths.

## Decision

The "Credit card number" pattern gets a third validator, `_card_prefix_length_valid`, next to Luhn
and grouping. It looks only at the first digit and the digit count:

| First digit | Lengths | Networks |
|---|---|---|
| 1 | 15 | UATP |
| 2 | 16–19 | Mastercard 2-series, Mir |
| 3 | 14–19 | Amex, Diners Club, JCB |
| 4 | 13–19 | Visa |
| 5 | 13–19 | Mastercard, Maestro |
| 6 | 13–19 | Discover, UnionPay, Maestro |
| 8 | 16 | RuPay |

A run starting with 0, 7 or 9, or with a length its first digit doesn't allow, is not a card.

## Alternatives considered

- **A full BIN/IIN prefix list.** ADR 0147 rejected it because someone has to maintain it. The
  first-digit table changes only when a network adopts a new major industry identifier.
- **Ignoring numbers after "SN", "S/N" or "serial number".** The label is free text an external
  party writes, so it would also hide a real card number written next to it. This gate prefers one
  extra confirmation to a miss.
- **Requiring a keyword ("card", "visa", "exp") near a 13-digit match.** A card number pasted into
  a spreadsheet cell or CSV has no keyword next to it.

## Consequences

- 13-digit serials and IDs starting with 1–3 or 7–9 no longer raise the confirmation.
- A Luhn-valid serial of a length and first digit a card can have (a 16-digit run starting with 4,
  say) still does.
- Lenient by design: 13–19 for Visa, Mastercard and Maestro covers lengths some networks no longer
  issue, so no real card is missed.
