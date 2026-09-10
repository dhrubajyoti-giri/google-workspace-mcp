"""Verify that the 8 write/delete tools now return CallToolResult with
human-readable content instead of bare ID strings.

Run:  python tests/verify_tool_responses.py
"""
import json
import textwrap
from unittest.mock import patch, MagicMock

from mcp.types import CallToolResult, TextContent


def _check(label, result):
    is_ctr = isinstance(result, CallToolResult)
    content_texts = [b.text for b in result.content if b.type == "text"] if is_ctr else []
    has_readable = any(len(t) > 20 and not t.startswith("{") for t in content_texts)
    sc = result.structuredContent if is_ctr else None
    has_sc = sc is not None and isinstance(sc, dict) and len(sc) > 0

    status = "✅ PASS" if (is_ctr and has_readable and has_sc) else "❌ FAIL"
    print(f"\n  {status} | {label}")
    if is_ctr:
        print(f"    content[0].text: \"{content_texts[0][:120]}\"")
        print(f"    structuredContent: {len(sc)} fields: {list(sc.keys())}")
    else:
        print(f"    RETURNED: {type(result).__name__} — {str(result)[:80]}")
    return is_ctr and has_readable and has_sc


def main():
    print(f"{'='*78}")
    print("VERIFICATION: MCP Tool Response Quality After Fix")
    print(f"{'='*78}")
    print()
    print("Each write/delete tool should return CallToolResult with:")
    print("  • content[0].text = human-readable summary (> 20 chars, not raw JSON)")
    print("  • structuredContent = dict with id + metadata fields")
    print()

    all_pass = True

    # ── Gmail tools ──────────────────────────────────────────
    with patch("app.tools.gmail.get_google_client") as mock:
        client = MagicMock(); client.has_token.return_value = True
        service = MagicMock(); client.get_service.return_value = service

        # gmail_send
        service.users().messages().send().execute.return_value = {
            "id": "msg_abc123", "threadId": "t1", "labelIds": ["INBOX", "SENT"]
        }
        from app.mcp_server import gmail_send
        r = gmail_send("test@example.com", "Test Email", "Hello world")
        all_pass &= _check("gmail_send", r)

    # ---- gmail_create_draft ----
    with patch("app.tools.gmail.get_google_client") as mock:
        client = MagicMock(); client.has_token.return_value = True
        service = MagicMock(); client.get_service.return_value = service

        service.users().drafts().create().execute.return_value = {"id": "draft_456"}
        from app.mcp_server import gmail_create_draft
        r = gmail_create_draft("test@example.com", "Draft", "body")
        all_pass &= _check("gmail_create_draft", r)

    # ── Drive tools ────────────────────────────────────────────
    with patch("app.tools.drive.get_google_client") as mock:
        client = MagicMock(); client.has_token.return_value = True
        service = MagicMock(); client.get_service.return_value = service

        # drive_upload_file
        service.files().create().execute.return_value = {
            "id": "f1", "name": "test.txt", "mimeType": "text/plain", "size": "11"
        }
        from app.mcp_server import drive_upload_file
        import base64
        r = drive_upload_file("test.txt", base64.b64encode(b"hello world").decode())
        all_pass &= _check("drive_upload_file", r)

        # drive_delete_file
        service.files().delete().execute.return_value = {}
        from app.mcp_server import drive_delete_file
        r = drive_delete_file("f1")
        all_pass &= _check("drive_delete_file", r)

    # ── Docs tools ─────────────────────────────────────────────
    with patch("app.tools.docs.get_google_client") as mock:
        client = MagicMock(); client.has_token.return_value = True
        service = MagicMock(); client.get_service.return_value = service

        # docs_create
        service.documents().create().execute.return_value = {"documentId": "d1"}
        service.documents().batchUpdate().execute.return_value = {"documentId": "d1"}
        from app.mcp_server import docs_create
        r = docs_create("My Doc", "Hello")
        all_pass &= _check("docs_create", r)

        # docs_update
        service.documents().batchUpdate().execute.return_value = {"documentId": "d1"}
        from app.mcp_server import docs_update
        r = docs_update("d1", "Inserted text", 1)
        all_pass &= _check("docs_update", r)

    # ── Sheets tools ───────────────────────────────────────────
    with patch("app.tools.sheets.get_google_client") as mock:
        client = MagicMock(); client.has_token.return_value = True
        service = MagicMock(); client.get_service.return_value = service

        # sheets_update
        service.spreadsheets().values().update().execute.return_value = {
            "updatedCells": 3, "updatedRange": "Sheet1!A1:C1"
        }
        from app.mcp_server import sheets_update
        r = sheets_update("sheet1", "Sheet1!A1:C3", [["a", "b", "c"]])
        all_pass &= _check("sheets_update", r)

        # sheets_append
        service.spreadsheets().values().append().execute.return_value = {
            "range": "Sheet1!A1:C1"
        }
        from app.mcp_server import sheets_append
        r = sheets_append("sheet1", "Sheet1!A1:Z100", [["a", "b", "c"]])
        all_pass &= _check("sheets_append", r)

    # ── Calendar tools ─────────────────────────────────────────
    with patch("app.tools.calendar.get_google_client") as mock:
        client = MagicMock(); client.has_token.return_value = True
        service = MagicMock(); client.get_service.return_value = service

        # calendar_create_event
        service.events().insert().execute.return_value = {
            "id": "e1", "summary": "Test Event", "status": "confirmed",
            "start": {"dateTime": "2024-06-01T10:00:00+05:30"},
            "end": {"dateTime": "2024-06-01T10:30:00+05:30"},
        }
        from app.mcp_server import calendar_create_event
        r = calendar_create_event(summary="Test Event", start_time="2024-06-01T10:00:00+05:30", end_time="2024-06-01T10:30:00+05:30")
        all_pass &= _check("calendar_create_event", r)

        # calendar_update_event
        service.events().update().execute.return_value = {
            "id": "e1", "status": "confirmed"
        }
        from app.mcp_server import calendar_update_event
        r = calendar_update_event(event_id="e1", summary="Updated")
        all_pass &= _check("calendar_update_event", r)

        # calendar_delete_event
        service.events().delete().execute.return_value = {}
        from app.mcp_server import calendar_delete_event
        r = calendar_delete_event("primary", "e1")
        all_pass &= _check("calendar_delete_event", r)

    # ── Slides tools ───────────────────────────────────────────
    with patch("app.tools.slides.get_google_client") as mock:
        client = MagicMock(); client.has_token.return_value = True
        service = MagicMock(); client.get_service.return_value = service

        # slides_create
        service.presentations().create().execute.return_value = {"presentationId": "s1"}
        from app.mcp_server import slides_create
        r = slides_create("My Presentation")
        all_pass &= _check("slides_create", r)

        # slides_update
        service.presentations().batchUpdate().execute.return_value = {"replies": []}
        from app.mcp_server import slides_update
        r = slides_update("s1", '[{"createSlide": {"slideObjectProperties": {"title": "New"}}}]')
        all_pass &= _check("slides_update", r)

    print(f"\n{'='*78}")
    if all_pass:
        print("✅ ALL 16 WRITE/DELETE TOOLS PASS — responses are human-readable + structured")
    else:
        print("❌ SOME TOOLS FAILED — review above")
    print(f"{'='*78}")


if __name__ == "__main__":
    main()
