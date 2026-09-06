"""Tests for Google API tools with mocked services."""
import json
import base64
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def mock_gmail_service():
    with patch("app.tools.gmail.get_google_client") as mock_client_cls:
        client = MagicMock()
        client.has_token.return_value = True
        service = MagicMock()
        client.get_service.return_value = service
        mock_client_cls.return_value = client
        yield service


@pytest.fixture
def mock_drive_service():
    with patch("app.tools.drive.get_google_client") as mock_client_cls:
        client = MagicMock()
        client.has_token.return_value = True
        service = MagicMock()
        client.get_service.return_value = service
        mock_client_cls.return_value = client
        yield service


@pytest.fixture
def mock_docs_service():
    with patch("app.tools.docs.get_google_client") as mock_client_cls:
        client = MagicMock()
        client.has_token.return_value = True
        service = MagicMock()
        client.get_service.return_value = service
        mock_client_cls.return_value = client
        yield service


# ── Gmail tool tests ────────────────────────────────────────

def test_gmail_search_returns_message_summaries(mock_gmail_service):
    from app.tools.gmail import gmail_search

    # Mock list result
    mock_gmail_service.users().messages().list(userId="me").execute.return_value = {
        "messages": [
            {"id": "msg1", "threadId": "t1"},
            {"id": "msg2", "threadId": "t2"},
        ]
    }

    # Mock get result for each message
    def mock_message_get(userId="me", id="", format="", metadataHeaders=None):
        result = MagicMock()
        result.execute.return_value = {
            "id": id,
            "threadId": "t1",
            "payload": {"headers": [
                {"name": "Subject", "value": f"Subject for {id}"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "Date", "value": "2024-01-01"},
            ]},
            "snippet": "test snippet",
        }
        return result

    mock_gmail_service.users().messages().get.side_effect = mock_message_get

    results = gmail_search("invoice", max_results=10)
    assert len(results) == 2
    assert results[0]["id"] == "msg1"
    assert results[0]["subject"] == "Subject for msg1"
    assert results[1]["id"] == "msg2"


def test_gmail_send_returns_message_id(mock_gmail_service):
    from app.tools.gmail import gmail_send

    mock_gmail_service.users().messages().send(
        userId="me", body={"raw": "test"}
    ).execute.return_value = {"id": "sent_msg_123", "threadId": "t1"}

    msg_id = gmail_send(
        to="test@example.com",
        subject="Test Email",
        body="Hello world",
        body_format="plain",
    )
    assert msg_id == "sent_msg_123"


def test_gmail_tools_require_auth():
    from app.tools.gmail import gmail_search

    with patch("app.tools.gmail.get_google_client") as mock:
        client = MagicMock()
        client.has_token.return_value = False
        mock.return_value = client

        with pytest.raises(RuntimeError, match="Google authentication required"):
            gmail_search("test")


# ── Drive tool tests ────────────────────────────────────────

def test_drive_search_returns_files(mock_drive_service):
    from app.tools.drive import drive_search

    mock_drive_service.files().list(
        q="name contains test", pageSize=10,
        fields="files(id,name,mimeType,size,modifiedTime,owners/displayName,link)"
    ).execute.return_value = {
        "files": [
            {"id": "f1", "name": "test.txt", "mimeType": "text/plain",
             "size": "1024", "modifiedTime": "2024-01-01",
             "owners": [{"displayName": "tester"}], "link": "https://drive.google.com/file/d/f1/view"},
            {"id": "f2", "name": "data.csv", "mimeType": "text/csv",
             "size": "2048", "modifiedTime": "2024-01-02",
             "owners": [{"displayName": "tester"}], "link": "https://drive.google.com/file/d/f2/view"},
        ]
    }

    results = drive_search("name contains test")
    assert len(results) == 2
    assert results[0]["id"] == "f1"
    assert results[0]["name"] == "test.txt"
    assert results[1]["owner"] == "tester"


# ── Write tool tests ────────────────────────────────────────

def test_gmail_create_draft(mock_gmail_service):
    from app.tools.gmail import gmail_create_draft

    # mock draft response
    mock_gmail_service.users().drafts().create(
        userId="me", body={}
    ).execute.return_value = {"id": "draft_123"}

    draft_id = gmail_create_draft(
        to="test@example.com",
        subject="Draft Subject",
        body="Draft body",
    )
    assert draft_id == "draft_123"


def test_drive_upload_file(mock_drive_service):
    from app.tools.drive import drive_upload_file

    # mock files().create().execute()
    mock_create = mock_drive_service.files().create()
    mock_create.execute.return_value = {
        "id": "file123",
        "name": "test.txt",
        "mimeType": "text/plain",
        "size": "11",
        "webViewLink": "https://drive.google.com/file/d/file123/view",
    }

    content_b64 = base64.b64encode(b"hello world").decode("utf-8")
    result = drive_upload_file(name="test.txt", content_base64=content_b64)
    assert result["id"] == "file123"
    assert result["name"] == "test.txt"


def test_drive_delete_file(mock_drive_service):
    from app.tools.drive import drive_delete_file

    # mock files().delete().execute()
    mock_drive_service.files().delete(fileId="file123").execute.return_value = {}

    file_id = drive_delete_file("file123")
    assert file_id == "file123"


def test_docs_create(mock_docs_service):
    from app.tools.docs import docs_create

    mock_docs_service.documents().create().execute.return_value = {"documentId": "doc456"}
    # Mock docs_update call (for content insertion)
    mock_docs_service.documents().batchUpdate().execute.return_value = {"documentId": "doc456"}

    doc_id = docs_create(title="Test Doc", content="Hello")
    assert doc_id == "doc456"


def test_sheets_update():
    from app.tools.sheets import sheets_update

    with patch("app.tools.sheets.get_google_client") as mock_client_cls:
        client = MagicMock()
        client.has_token.return_value = True
        service = MagicMock()
        client.get_service.return_value = service
        mock_client_cls.return_value = client

        service.spreadsheets().values().update().execute.return_value = {
            "updatedCells": 3,
            "updatedRange": "Sheet1!A1:C1",
        }

        result = sheets_update("sheet_id", "Sheet1!A1:C1", [["a", "b", "c"]])
        assert result["updatedCells"] == 3


def test_sheets_append():
    from app.tools.sheets import sheets_append

    with patch("app.tools.sheets.get_google_client") as mock_client_cls:
        client = MagicMock()
        client.has_token.return_value = True
        service = MagicMock()
        client.get_service.return_value = service
        mock_client_cls.return_value = client

        service.spreadsheets().values().append().execute.return_value = {
            "range": "Sheet1!A1:C1",
        }

        result = sheets_append("sheet_id", "Sheet1!A1:Z100", [["a", "b", "c"]])
        assert "appendedRange" in result


def test_calendar_create_event():
    from app.tools.calendar import calendar_create_event

    with patch("app.tools.calendar.get_google_client") as mock_client_cls:
        client = MagicMock()
        client.has_token.return_value = True
        service = MagicMock()
        client.get_service.return_value = service
        mock_client_cls.return_value = client

        service.events().insert().execute.return_value = {
            "id": "event_42",
            "summary": "Test Event",
        }

        event_id = calendar_create_event(
            summary="Test Event",
            start_time="2024-06-01T10:00:00+05:30",
            end_time="2024-06-01T10:30:00+05:30",
        )
        assert event_id == "event_42"


def test_calendar_delete_event():
    from app.tools.calendar import calendar_delete_event

    with patch("app.tools.calendar.get_google_client") as mock_client_cls:
        client = MagicMock()
        client.has_token.return_value = True
        service = MagicMock()
        client.get_service.return_value = service
        mock_client_cls.return_value = client

        service.events().delete().execute.return_value = {}
        result = calendar_delete_event("primary", "event_42")
        assert result == "event_42"


# ── Docs tool tests ─────────────────────────────────────────

def test_docs_get_extracts_body_text(mock_docs_service):
    from app.tools.docs import docs_get

    mock_docs_service.documents().get(documentId="doc123").execute.return_value = {
        "documentId": "doc123",
        "title": "Test Document",
        "body": {
            "content": [
                {"startIndex": 1, "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "elements": [{"textRun": {"content": "Introduction\n"}}],
                }},
                {"startIndex": 2, "paragraph": {
                    "elements": [{"textRun": {"content": "Hello world"}}],
                }},
            ]
        },
        "bodyTextSize": "100",
        "documentStyle": {"lineMode": "LINE_HEIGHT"},
    }

    result = docs_get("doc123")
    assert result["id"] == "doc123"
    assert result["title"] == "Test Document"
    assert "Introduction" in result["body"]
    assert "Hello world" in result["body"]
    # Heading should be in sections
    assert any(s["text"] == "Introduction" and s["level"] == 1 for s in result["body_sections"])
