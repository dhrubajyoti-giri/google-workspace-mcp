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

log = logging.getLogger("google-api-bridge")

# ── Protocol version compatibility patch ────────────────────────────
# QwenPaw's MCP client advertises protocol version "2026-07-28", but the
# installed `mcp` SDK (v1.29.1, pinned <2.0 by QwenPaw's constraint) only
# lists up to "2025-11-25" in SUPPORTED_PROTOCOL_VERSIONS.
#
# Without this patch, StreamableHTTPSessionManager._validate_protocol_version()
# returns HTTP 400 for every request with the "2026-07-28" header, and QwenPaw's
# client falls back to legacy/SSE mode where tools aren't registered in the
# active session.
#
# With this patch, 2026-07-28 is accepted during _validate_protocol_version,
# AND the ServerDiscoverMiddleware in main.py responds to QwenPaw's
# non-standard "server/discover" method with the supported versions list
# — allowing the modern stateless transport to connect directly.
try:
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    if "2026-07-28" not in SUPPORTED_PROTOCOL_VERSIONS:
        SUPPORTED_PROTOCOL_VERSIONS.append("2026-07-28")
        log.info("Added protocol version 2026-07-28 to SUPPORTED_PROTOCOL_VERSIONS for QwenPaw compatibility")
except Exception:
    pass

# streamable_http_path="/" so the Starlette ASGI app routes at "/" internally;
# when FastAPI mounts it at "/mcp", the full path becomes /mcp (not /mcp/mcp).
_domain = urlparse(settings.external_url).hostname or "localhost"

mcp = FastMCP(
    name=settings.mcp_server_name,
    instructions=f"Google API Bridge v{settings.mcp_server_version} — Gmail, Drive, Docs, Sheets, Calendar, Slides",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_domain, f"{_domain}:*", "localhost", "127.0.0.1", "[::1]", "localhost:*", "127.0.0.1:*", "[::1]:*"],
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
def gmail_send(to: str, subject: str, body: str, body_format: str = "plain", cc: str = "", bcc: str = "") -> str:
    """Send an email via the authenticated Gmail account.

    Parameters:
      to — comma-separated recipient list
      subject — email subject
      body — email body content
      body_format — 'plain' (default) or 'html'
      cc — comma-separated CC recipients (optional)
      bcc — comma-separated BCC recipients (optional)

    Returns the Gmail message ID.
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
def gmail_create_draft(to: str, subject: str, body: str, body_format: str = "plain", cc: str = "", bcc: str = "") -> str:
    """Create a draft email in Gmail.

    Parameters:
      to — comma-separated recipient list
      subject — email subject
      body — email body content
      body_format — 'plain' (default) or 'html'
      cc — comma-separated CC recipients (optional)
      bcc — comma-separated BCC recipients (optional)

    Returns the Gmail draft ID.
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
def drive_delete_file(file_id: str) -> str:
    """Delete a file from Google Drive by ID. Returns the deleted file ID."""
    return _drive_delete(file_id)


@mcp.tool()
def docs_create(title: str, content: str = "") -> str:
    """Create a new Google Doc.

    Parameters:
      title — document title
      content — optional initial text content

    Returns the new document ID.
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
def calendar_create_event(calendar_id: str = "primary", summary: str = "", start_time: str = "", end_time: str = "", description: str = "", location: str = "", attendees: list[dict[str, str]] = None) -> str:
    """Create a calendar event.

    Parameters:
      calendar_id — 'primary' (default) or calendar ID
      summary — event title
      start_time — ISO 8601 start (e.g. '2024-06-01T14:00:00+05:30')
      end_time — ISO 8601 end time
      description — event description (optional)
      location — event location (optional)
      attendees — list of {'email': 'addr'} dicts (optional)

    Returns the new event ID.
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

    Returns the updated event ID.
    """
    return _cal_update(calendar_id, event_id, summary, start_time, end_time, description, location)


@mcp.tool()
def calendar_delete_event(calendar_id: str = "primary", event_id: str = "") -> str:
    """Delete a calendar event by ID. Returns the deleted event ID."""
    return _cal_delete(calendar_id, event_id)


@mcp.tool()
def slides_create(title: str = "Untitled Presentation") -> str:
    """Create a new Google Slides presentation.

    Parameters:
      title — presentation title

    Returns the new presentation ID.
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


# ── ASGI app factory for FastAPI mount ───────────────────────

def create_mcp_asgi():
    """Return the Starlette ASGI app for streamable HTTP transport.

    FastAPI mounts this at ``/mcp`` so the full MCP endpoint becomes
    ``POST https://<domain>/mcp``.
    """
    return mcp.streamable_http_app()
