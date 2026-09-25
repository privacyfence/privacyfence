<!--
The one canonical description of PrivacyFence. tests/unit/test_website_canonical_description.py
requires this text, verbatim (whitespace-normalized, tags stripped), in README.md and on the
homepage (website/index.html). Change it here first, then in both places, in the same PR.
This file is repository-only: scripts/build_site.py does not publish it.
-->

PrivacyFence is an open-source privacy and approval gateway between AI assistants and your business systems. It connects MCP-compatible assistants such as Claude Desktop and Claude Code to Gmail, Google Drive, Calendar, Slack, Salesforce, Jira, Confluence, Telegram, and more.

PrivacyFence enforces, independently of the AI, what an assistant may see and do. Sensitive reads and consequential actions require human approval, while routine requests can be automated by policy. Optional PII detection runs locally before personal data reaches the AI, and every decision is audited.

PrivacyFence runs on an employee’s own computer (macOS, Windows, or Linux) or as a central deployment on infrastructure the organization controls, allowing web clients such as claude.ai to connect as well. Connector credentials stay with PrivacyFence, never with the AI client, and no data passes through PrivacyFence-operated servers — there are none.
