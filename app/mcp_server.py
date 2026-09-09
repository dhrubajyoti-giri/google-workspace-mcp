"""MCP server instance with all Google API tools registered.

This module creates the single FastMCP instance that the FastAPI application
mounts at ``/mcp``.  Each tool delegates to the implementation in
``app/tools/*``.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from mcp.server import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import settings
from app.tools.gmail import gmail_search as _gmail_search, gmail_get_message as _gmail_get, gmail_send as _gmail_send, gmail_create_draft as _gmail_create_draft
from app.tools.drive import drive_search as _drive_search, drive_get_file as _drive_get, drive_upload_file as _drive_upload, drive_create_file as _drive_create, drive_delete_file as _drive_delete
from app.tools.docs import docs_get as _docs_get, docs_create as _docs_create, docs_update as _docs_update
from app.tools.sheets import sheets_get as _sheets_get, sheets_update as _sheets_update, sheets_append as _sheets_append
from app.tools.calendar import calendar_list_events as _cal_list, calendar_get_event as _cal_get, calendar_create_event as _cal_create, calendar_update_event as _cal_update, calendar_delete_event as _cal_delete
from app.tools.slides import slides_get as _slides_get, slides_create as _slides_create, slides_update as _slides_update

log = logging.getLogger("google-workspace-mcp")


# ── Scope-based tool filtering ──────────────────────────────────────────────

# Maps each tool name to the Google API scope(s) that satisfy it.
# A tool is shown to a user only if their OAuth token contains at least
# one of the listed scope names (the suffix after auth/ prefix, e.g.
# "gmail.readonly").  Broad scopes (e.g. "gmail.modify", "drive") satisfy
# narrower requirements (e.g. "gmail.readonly", "drive.readonly").
TOOL_SCOPE_REQUIREMENTS: dict[str, list[str]] = {
    # Gmail — read tools need at least a read-level Gmail scope
    "gmail_search":      ["gmail.readonly", "gmail.metadata", "gmail.modify"],
    "gmail_get_message":  ["gmail.readonly", "gmail.metadata", "gmail.modify"],
    # Gmail — write tools need a send/modify-level Gmail scope
    "gmail_send":         ["gmail.send", "gmail.modify"],
    "gmail_create_draft": ["gmail.compose", "gmail.modify"],
    # Drive — read tools
    "drive_search":        ["drive.readonly", "drive", "drive.file", "drive.metadata"],
    "drive_get_file":      ["drive.readonly", "drive", "drive.file"],
    # Drive — write tools
    "drive_upload_file":   ["drive.file", "drive"],
    "drive_create_file":   ["drive.file", "drive"],
    "drive_delete_file":   ["drive.file", "drive"],
    # Docs — read tools need documents.readonly; write tools need documents
    "docs_get":    ["documents.readonly", "documents"],
    "docs_create": ["documents"],
    "docs_update": ["documents"],
    # Sheets — read tools need spreadsheets.readonly; write tools need spreadsheets
    "sheets_get":    ["spreadsheets.readonly", "spreadsheets"],
    "sheets_update": ["spreadsheets"],
    "sheets_append": ["spreadsheets"],
    # Calendar — read tools need calendar.readonly; write tools need calendar
    "calendar_list_events":    ["calendar.readonly", "calendar"],
    "calendar_get_event":      ["calendar.readonly", "calendar"],
    "calendar_create_event":   ["calendar"],
    "calendar_update_event":   ["calendar"],
    "calendar_delete_event":   ["calendar"],
    # Slides — read tools need presentations.readonly; write tools need presentations
    "slides_get":    ["presentations.readonly", "presentations"],
    "slides_create": ["presentations"],
    "slides_update": ["presentations"],
}

# ── Protocol version compatibility patch (fallback) ─────────────────
# QwenPaw's MCP client advertises a protocol version newer than what the
# installed `mcp` SDK (v1.29.1, pinned <2.0) lists in SUPPORTED_PROTOCOL_VERSIONS.
#
# PRIMARY FIX: ServerDiscoverMiddleware in main.py dynamically adds the
# client's protocol version (from the `mcp-protocol-version` request header)
# to SUPPORTED_PROTOCOL_VERSIONS on every request.  This is forward-compatible:
# any future QwenPaw protocol version is accepted automatically.
#
# THIS STATIC PATCH: keeps 2026-07-28 as a known-good fallback for requests
# that might bypass the middleware (edge cases).  Safe to leave in — it's
# idempotent and doesn't affect future versions handled dynamically.
try:
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    if "2026-07-28" not in SUPPORTED_PROTOCOL_VERSIONS:
        SUPPORTED_PROTOCOL_VERSIONS.append("2026-07-28")
        log.info("Added protocol version 2026-07-28 to SUPPORTED_PROTOCOL_VERSIONS (fallback)")
except Exception:
    pass

# streamable_http_path="/" so the Starlette ASGI app routes at "/" internally;
# when FastAPI mounts it at "/mcp", the full path becomes /mcp (not /mcp/mcp).
_domain = urlparse(settings.external_url).hostname or "localhost"

# DNS rebinding protection: extra allowed hosts come from MCP_ALLOWED_HOSTS env var.
# No hardcoded Docker hostnames — user configures via env var.
# Auto-add ":*" wildcard port variant for bare hostnames (e.g. "gws-mcp" -> "gws-mcp:*")
# so that "gws-mcp:8000" matches when user only enters "gws-mcp".
_raw_hosts: list[str] = [h.strip() for h in settings.allowed_hosts_raw.split(",") if h.strip()]
_extra_hosts: list[str] = []
for h in _raw_hosts:
    _extra_hosts.append(h)
    # If host has no port wildcard and no explicit port, add wildcard variant
    if not h.endswith(":*") and ":" not in h:
        _extra_hosts.append(f"{h}:*")

mcp = FastMCP(
    name=settings.mcp_server_name,
    instructions=f"Google Workspace MCP v{settings.mcp_server_version} — Gmail, Drive, Docs, Sheets, Calendar, Slides",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_domain, f"{_domain}:*", "localhost", "127.0.0.1", "[::1]", "localhost:*", "127.0.0.1:*", "[::1]:*", *_extra_hosts],
        allowed_origins=[
            settings.external_url,
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    ),
    stateless_http=True,         # stateless server — no Mcp-Session-Id required
    max_request_body_size=8 * 1024 * 1024,  # allow up to 8 MB payloads
)

# ── Gmail tools ──────────────────────────────────────────────

@mcp.tool()
def gmail_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Search the authenticated user's Gmail mailbox.

    Uses Gmail search syntax.
    Examples:
      - 'invoice' — any message containing "invoice"
      - 'from:example.com' — messages from a specific sender
      - 'subject:"meeting notes"' — messages with a subject match
      - 'is:unread' — unread messages
      - 'after:2024/01/01 before:2024/02/01' — date range
    """
    return _gmail_search(query, max_results)


@mcp.tool()
def gmail_get_message(message_id: str) -> dict[str, Any]:
    """Retrieve a single Gmail message by ID.

    Returns the full message: headers, body (text + HTML), labels, and internal date.
    """
    return _gmail_get(message_id)


@mcp.tool()
def gmail_send(to: str, subject: str, body: str, body_format: str = "plain", cc: str = "", bcc: str = "") -> dict[str, Any]:
    """Send an email via the authenticated Gmail account.

    Parameters:
      to — comma-separated recipient list
      subject — email subject
      body — email body content
      body_format — 'plain' (default) or 'html'
      cc — comma-separated CC recipients (optional)
      bcc — comma-separated BCC recipients (optional)

    Returns: dict with id, threadId, labelIds, to, subject, status, message.
    """
    return _gmail_send(to, subject, body, body_format, cc, bcc)


# ── Drive tools ──────────────────────────────────────────────

@mcp.tool()
def drive_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Search the authenticated user's Google Drive.

    Uses Google Drive v3 search query syntax.
    Examples:
      - 'name contains \"budget\"'
      - 'mimeType=\"application/vnd.google-apps.document\"'
      - 'modifiedTime > \"2024-01-01T00:00:00\"'
      - 'name contains \"project\" and trashed=false'
    """
    return _drive_search(query, max_results)


@mcp.tool()
def drive_get_file(file_id: str) -> dict[str, Any]:
    """Get a file's metadata and content.

    For Google Docs/Sheets/Slides, content is exported as plain text or CSV.
    For binary files, content is returned as base64.
    """
    return _drive_get(file_id)


# ── Docs tools ───────────────────────────────────────────────

@mcp.tool()
def docs_get(document_id: str) -> dict[str, Any]:
    """Retrieve a Google Doc's content and structural information.

    Parameters:
      document_id — the Google Doc ID (from the doc's URL)

    Returns: title, body text, body_sections (with heading levels),
    tables, lists, and document metadata.
    """
    return _docs_get(document_id)


# ── Sheets tools ─────────────────────────────────────────────

@mcp.tool()
def sheets_get(spreadsheet_id: str, range: str = "A1:Z100") -> dict[str, Any]:
    """Retrieve values from a Google Sheets spreadsheet.

    Parameters:
      spreadsheet_id — the spreadsheet ID (from the sheet's URL)
      range — A1 notation range, e.g. 'Sheet1!A1:F20' (default: 'A1:Z100')

    Returns: spreadsheetId, sheetNames, range, values (2D array).
    """
    return _sheets_get(spreadsheet_id, range)


# ── Calendar tools ───────────────────────────────────────────

@mcp.tool()
def calendar_list_events(calendar_id: str = "primary", time_min: str = None, time_max: str = None, max_results: int = 10) -> list[dict[str, Any]]:
    """List events from a Google Calendar.

    Parameters:
      calendar_id — 'primary' (default) or a calendar ID/email
      time_min — ISO 8601 start time (e.g. '2024-01-01T00:00:00+05:30')
      time_max — ISO 8601 end time
      max_results — max events to return (default 10)

    Returns: list of events with id, summary, start, end, location, description.
    """
    return _cal_list(calendar_id, time_min, time_max, max_results)


@mcp.tool()
def calendar_get_event(calendar_id: str = "primary", event_id: str = "") -> dict[str, Any]:
    """Get a single calendar event by ID."""
    return _cal_get(calendar_id, event_id)


# ── Slides tools ─────────────────────────────────────────────

@mcp.tool()
def slides_get(presentation_id: str) -> dict[str, Any]:
    """Retrieve a Google Slides presentation's structure and text content.

    Parameters:
      presentation_id — the presentation ID (from the URL)

    Returns: title, slideCount, slides (each with pageElements/text).
    """
    return _slides_get(presentation_id)


# ── Write tools ──────────────────────────────────────────────

@mcp.tool()
def gmail_create_draft(to: str, subject: str, body: str, body_format: str = "plain", cc: str = "", bcc: str = "") -> dict[str, Any]:
    """Create a draft email in Gmail.

    Parameters:
      to — comma-separated recipient list
      subject — email subject
      body — email body content
      body_format — 'plain' (default) or 'html'
      cc — comma-separated CC recipients (optional)
      bcc — comma-separated BCC recipients (optional)

    Returns: dict with id, to, subject, status, message.
    """
    return _gmail_create_draft(to, subject, body, body_format, cc, bcc)


@mcp.tool()
def drive_upload_file(name: str, content_base64: str, mime_type: str = "text/plain", parent_folder_id: str = "") -> dict[str, Any]:
    """Upload a file to Google Drive.

    Parameters:
      name — filename
      content_base64 — file content as base64-encoded string
      mime_type — MIME type (e.g. 'text/plain', 'application/pdf')
      parent_folder_id — optional parent folder ID

    Returns: file metadata (id, name, mimeType, size, webViewLink).
    """
    return _drive_upload(name, content_base64, mime_type, parent_folder_id)


@mcp.tool()
def drive_create_file(name: str, content: str, mime_type: str = "text/plain", parent_folder_id: str = "") -> dict[str, Any]:
    """Create a text file in Google Drive with inline content."""
    return _drive_create(name, content, mime_type, parent_folder_id)


@mcp.tool()
def drive_delete_file(file_id: str) -> dict[str, Any]:
    """Delete a file from Google Drive by ID. Returns id, status, message."""
    return _drive_delete(file_id)


@mcp.tool()
def docs_create(title: str, content: str = "") -> dict[str, Any]:
    """Create a new Google Doc.

    Parameters:
      title — document title
      content — optional initial text content

    Returns: dict with id, title, url, status, message.
    """
    return _docs_create(title, content)


@mcp.tool()
def docs_update(document_id: str, text: str, location_index: int = 1) -> dict[str, Any]:
    """Insert text into a Google Doc at the specified location.

    Parameters:
      document_id — the Google Doc ID
      text — text to insert
      location_index — insertion index (1 = document start; default 1)

    Returns the batchUpdate response.
    """
    return _docs_update(document_id, text, location_index)


@mcp.tool()
def sheets_update(spreadsheet_id: str, range: str, values: list[list[str]]) -> dict[str, Any]:
    """Update cell values in a Google Sheet.

    Parameters:
      spreadsheet_id — the spreadsheet ID
      range — A1 notation range (e.g. 'Sheet1!A1:C3')
      values — 2D array of cell values (row-major)

    Returns: the update response.
    """
    return _sheets_update(spreadsheet_id, range, values)


@mcp.tool()
def sheets_append(spreadsheet_id: str, range: str, values: list[list[str]]) -> dict[str, Any]:
    """Append rows to the end of a Google Sheet.

    Parameters:
      spreadsheet_id — the spreadsheet ID
      range — A1 notation range, determines which sheet
      values — 2D array of rows to append (row-major)

    Returns: the append response (appendedRange, appendedRows).
    """
    return _sheets_append(spreadsheet_id, range, values)


@mcp.tool()
def calendar_create_event(calendar_id: str = "primary", summary: str = "", start_time: str = "", end_time: str = "", description: str = "", location: str = "", attendees: list[dict[str, str]] = None) -> dict[str, Any]:
    """Create a calendar event.

    Parameters:
      calendar_id — 'primary' (default) or calendar ID
      summary — event title
      start_time — ISO 8601 start (e.g. '2024-06-01T14:00:00+05:30')
      end_time — ISO 8601 end time
      description — event description (optional)
      location — event location (optional)
      attendees — list of {'email': 'addr'} dicts (optional)

    Returns: dict with id, summary, start, end, location, status, htmlLink, message.
    """
    return _cal_create(calendar_id, summary, start_time, end_time, description, location, attendees)


@mcp.tool()
def calendar_update_event(calendar_id: str = "primary", event_id: str = "", summary: str = "", start_time: str = "", end_time: str = "", description: str = "", location: str = "") -> dict[str, Any]:
    """Update a calendar event by ID. Only non-empty fields are sent.

    Parameters:
      calendar_id — 'primary' (default) or calendar ID
      event_id — the event to update
      summary — new title (or empty to keep)
      start_time — new ISO 8601 start (or empty to keep)
      end_time — new ISO 8601 end (or empty to keep)
      description — new description (or empty to keep)
      location — new location (or empty to keep)

    Returns: dict with id, updated_fields, status, htmlLink, message.
    """
    return _cal_update(calendar_id, event_id, summary, start_time, end_time, description, location)


@mcp.tool()
def calendar_delete_event(calendar_id: str = "primary", event_id: str = "") -> dict[str, Any]:
    """Delete a calendar event by ID. Returns id, status, message."""
    return _cal_delete(calendar_id, event_id)


@mcp.tool()
def slides_create(title: str = "Untitled Presentation") -> dict[str, Any]:
    """Create a new Google Slides presentation.

    Parameters:
      title — presentation title

    Returns: dict with id, title, url, status, message.
    """
    return _slides_create(title)


@mcp.tool()
def slides_update(presentation_id: str, requests_json: str) -> dict[str, Any]:
    """Batch update a Google Slides presentation.

    Parameters:
      presentation_id — the presentation ID
      requests_json — JSON array of Slides API request objects as a string.
        Example: '[{"createSlide": {"slideObjectProperties": {"title": "New Slide"}}}]'

    Returns: the batchUpdate response with replies.
    """
    return _slides_update(presentation_id, requests_json)


# ── Scope-based tool filtering ──────────────────────────────────────────────
# Wire up filtering so that ``tools/list`` only returns the tools the
# authenticated user is actually authorized to use (based on the Google API
# scopes in their OAuth token).  This replaces the previous behaviour where
# every user saw all 23 tools regardless of the scopes they granted.

_orig_list_tools = mcp.list_tools  # bound method — self is already captured


def _strip_scope(scope_url: str) -> str:
    """Extract the short scope name from a full Google API scope URL.

    ``https://www.googleapis.com/auth/gmail.readonly`` → ``gmail.readonly``
    """
    prefix = "https://www.googleapis.com/auth/"
    return scope_url[len(prefix):] if scope_url.startswith(prefix) else scope_url


async def _filtered_list_tools() -> list[Any]:
    """Return only the tools the current user is authorized to use.

    Looks up the user's Google email from the MCP auth context (set by
    ``AuthContextMiddleware``), fetches their granted scopes from the
    registry, and keeps only tools whose required scope is satisfied.
    """
    tools = await _orig_list_tools()

    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        from app.registry import Registry

        access_token = get_access_token()
        if access_token is None or not access_token.subject:
            # No authenticated user in context — no tools available.
            return []

        _reg = Registry(settings.registry_file)
        user_scopes = _reg.get_scopes(access_token.subject) or []
        user_scope_names = {_strip_scope(s) for s in user_scopes}

        filtered: list[Any] = []
        for t in tools:
            required = TOOL_SCOPE_REQUIREMENTS.get(t.name, [])
            if not required:
                # Unknown tool — include to be safe.
                filtered.append(t)
                continue
            if any(sc in user_scope_names for sc in required):
                filtered.append(t)
            else:
                log.debug(
                    "Filtering tool %s — user lacks scopes %s (has: %s)",
                    t.name, required, sorted(user_scope_names),
                )
        return filtered
    except Exception as e:
        log.warning(
            "Scope-based tool filtering failed (%s) — falling back to all tools", e,
        )
        return tools


mcp.list_tools = _filtered_list_tools


# ── ASGI app factory for FastAPI mount ───────────────────────

def create_mcp_asgi():
    """Return the Starlette ASGI app for streamable HTTP transport.

    FastAPI mounts this at ``/mcp`` so the full MCP endpoint becomes
    ``POST https://<domain>/mcp``.
    """
    return mcp.streamable_http_app()
