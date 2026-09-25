"""Embedded HTTP(S) server package: ``server.py`` (lifecycle,
bind policy, security headers), ``routes_approvals.py`` (the approval
surface WebApprovalUI blocks on), ``routes_mcp.py`` (the Streamable
HTTP MCP endpoint), ``mcp_dispatch.py`` (its connector-call dispatch --
dedupe, gating, meta-tools), ``mcp_tools.py`` (ToolSpec -> MCP tool
translation), ``mcp_auth.py`` (its bearer-token auth), and the settings,
security and org-mode routes alongside them.

Nothing under this package imports AppKit/PyObjC -- it has to stay
importable and testable on any platform (see approval_ui.py's own docstring
for why this matters).
"""
from __future__ import annotations
