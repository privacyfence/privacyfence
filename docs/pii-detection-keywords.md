# PII detection keywords

Exactly what the PII check matches, category by category and language by language. This page is an
appendix to [Approvals and policy](approvals-and-policy.md#the-pii-check), which explains what
happens when something matches. The patterns live in
[`src/privacyfence/pii_detector.py`](../src/privacyfence/pii_detector.py); if this page and that
file disagree, the file is right.

All patterns are case-insensitive except where noted. A `*` suffix means the match accepts trailing
letters (plurals, grammatical case and possessive endings); a bare word matches only that form.

---

## Language-agnostic

These run whatever the language, because they match a format rather than a label.

| Category | What matches |
|---|---|
| IBAN (bank account number) | 2 letters + 2 digits + 11–30 letters or digits **written without spaces**, that also pass the ISO 7064 mod-97-10 checksum. An IBAN written in groups (`DE89 3704 0044 …`) is not detected |
| Credit card number | 13–19 digits (spaces or dashes allowed) that pass the Luhn checksum. If the digits are grouped, the grouping must look like a real card: groups of 4 with a possibly shorter last group, or Amex 4-6-5 / Diners Club 4-6-4. Digits grouped in pairs never match |
| IP address *(can be turned off)* | an IPv4 address (`a.b.c.d`) |
| Financial figures (currency amounts) *(can be turned off)* | a number next to `$`, `€` or `£`, or next to `USD`, `EUR`, `GBP`, `HUF`, `CHF` (before or after) or `Ft` (after). A bare number never matches |

---

## Hungarian

| Category | Trigger keywords / pattern |
|---|---|
| Hungarian TAJ number (social security) | `TAJ`, optionally `szám` / `száma`, then 9 digits as 3-3-3 (spaces or dashes allowed) |
| Hungarian tax ID (adóazonosító jel) | a standalone 10-digit number starting with `8` |
| Hungarian ID card number | 6 digits + 2 uppercase letters (case-sensitive) |
| Hungarian personal data reference | `személyi szám*`, `lakcím*`, `születési dátum*` / `hely*` / `idő*`, `anyja nev*`, `útlevél szám*` |
| Salary/compensation information | `fizetés*`, `jövedel*` (jövedelem), `bruttó bér*`, `nettó bér*` |

Accented letters also match their unaccented form (`lakcim`, `szuletesi datum`).

## German

| Category | Trigger keywords / pattern |
|---|---|
| German tax ID (Steuer-IdNr.) | a label that **starts with `Steuer`** — `Steuer-ID`, `SteuerID`, `Steuer-IdNr.`, `Steuerliche ID`, `Steuerliche Identifikationsnummer` — then 11 digits as 2-3-3-3 (spaces allowed, dashes not). `IdNr.` or `Identifikationsnummer` on its own, without `Steuer`, does not match |
| German social insurance number | 8 digits + 1 uppercase letter + 3 digits (case-sensitive) |
| German personal data reference | `Personalausweisnummer*`, `Sozialversicherungsnummer*`, `Geburtsdatum*`, `Geburtsort*`, `Wohnanschrift*`, `Anschrift*`, `Reisepassnummer*`, `Steueridentifikationsnummer*` |
| Salary/compensation information | `Gehalt*`, `Vergütung*`, and `Lohn` with an optional `Brutto` / `Netto` / `Monats` / `Jahres` prefix and an optional `abrechnung*` / `steuer*` / `zettel*` / `erhöhung*` suffix. `Lohn` never matches inside a longer word (so `lohnend` does not match) |

## English

| Category | Trigger keywords / pattern |
|---|---|
| US Social Security Number | `123-45-6789` (3-2-4 digits with dashes) |
| UK National Insurance number | 2 uppercase letters + 6 digits + a letter A–D (case-sensitive, no spaces) |
| English personal data reference | `social security number`, `date of birth`, `passport number`, `national insurance number`, `home address`, `driver's license number` / `drivers licence number` |
| Salary/compensation information | `salar*` (salary, salaries), `payslip`, `pay slip`, `take-home pay` |

---

## What text is scanned

The check reads the content being released, not the envelope around it:

- **Gmail** — the message body (every body in a thread). Headers — From, To, Subject, Date — are
  not scanned.
- **Slack, Telegram** — message text, not sender names or channel names.
- **Calendar** — the event description.
- **Jira** — the issue description and comments.
- **Confluence** — the page body.
- **Drive** — a document's text, all of what the AI system receives (at most 100 KB); a sheet
  read, limited to its first 50 rows.
- **Attachments and downloaded or uploaded files** — the text PrivacyFence can extract from the
  file (see [File previews](approvals-and-policy.md#file-previews)), capped at 20,000 characters.
  A file larger than 5 MB is not scanned, and images are not read (no OCR).

Anything past those limits is released without being scanned.

## Categories you can turn off

**IP address** and **Financial figures (currency amounts)** can each be turned off on their own,
without turning off the whole check: in **Settings → General → PII Detection Gate**, or with
`pii_detection.detect_ip_addresses` / `pii_detection.detect_financial_figures` in
`settings.yaml` (both `true` by default). They are separate because they appear constantly in
ordinary business mail — server logs, invoices, budgets — without being about a person. Every other
category is on whenever the check itself is on.

## Deliberately not detected

**Email addresses and phone numbers**, in any language. Almost every email carries the sender's own
address and phone number in its signature, so matching them would flag nearly every card and teach
people to click through the warning.

## Limits of this check

It is a local, regex-based heuristic, not a compliance-grade classifier. It runs on your machine
with no network calls, and it can both miss real personal data and flag things that aren't. A match
means "look more carefully before approving", not a guarantee either way.

Only the category name (for example "IBAN (bank account number)") is shown on the card or written
to the audit log. The matched text is never stored — unless you turn on
`pii_detection.audit_match_details` (off by default), which records the matched text of approved
requests in the audit log for a trial period to tune the patterns. Even then, the categories whose
match *is* the sensitive value (IBAN, credit card, IP address, currency figure, and every ID and
insurance number above) are recorded with all but their first two and last two letters or digits
masked, and a denied request records only a placeholder. See
[Configuration reference](configuration-reference.md) for the key.
