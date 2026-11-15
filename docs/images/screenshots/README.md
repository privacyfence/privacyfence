# Screenshots

Documentation assets. Each file here is used by at least one of the places below.

| File | Used by | Shows |
| ---- | ------- | ----- |
| `gmail-read-thread.png` | `README.md`, website homepage, `/how-it-works/`, `/connectors/google-workspace/` | `gmail_get_thread` approval card |
| `sheets-write.png` | website homepage, `/how-it-works/`, `/connectors/google-workspace/` | `drive_sheets_write_range` approval card |
| `slack-read-thread.png` | `/connectors/slack/` | `slack_get_thread_replies` approval card |
| `salesforce-read-record.png` | `/connectors/salesforce/` | `salesforce_get_record` approval card |
| `jira-read-issue.png` | `/connectors/jira-confluence/` | `jira_get_issue` approval card |
| `telegram-read-chat.png` | `/connectors/telegram/` | `telegram_get_messages` approval card |

`README.md` embeds `gmail-read-thread.png` through a
`raw.githubusercontent.com/.../main/docs/images/screenshots/…` URL, so a change to it shows up there
once it is on `main`. `scripts/build_site.py`'s `STATIC` map copies every file above into the
site's `assets/`, and a push to `main` touching `docs/images/**` redeploys the site.

## Making an image

Each image is one approval card from a running organization server, captured in the light color
scheme at 2× device scale and a width of 1000, opened from `/approvals` → Review the way a person
reaches it. The content is synthetic, so the card shows what the real gate builds for that call
(PII highlighting, the "AI will receive" list, the stated reason and the calling AI system
included). Make a new image when the approval card's visual design changes meaningfully, not on
every card change.

## Maintenance

- Keep only screenshots that current documentation or the website references. When the UI
  changes, replace the affected image and delete obsolete ones rather than keeping historical
  copies here.
- Renaming or removing a file here also means updating `scripts/build_site.py`'s `STATIC` map
  and every page in the table above.
- Never capture real account data, tokens, email addresses, tenant identifiers, or private
  document/message content.
