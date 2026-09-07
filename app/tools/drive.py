"""Google Drive tools: search, get file, upload, delete."""
from __future__ import annotations

import base64
import io
import logging
from typing import Any

from app.google_client import get_google_client

log = logging.getLogger("google-workspace-mcp.tools.drive")

MAX_RESULTS_LIMIT = 100


def _ensure_auth():
    client = get_google_client()
    if not client.has_token():
        raise RuntimeError(
            "Google authentication required. Complete OAuth via your MCP client."
        )
    return client.get_service("drive", "v3")


def drive_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Search the authenticated user's Google Drive.

    Uses Google Drive v3 search query syntax (same as Drive UI).
    Examples:
      - 'name contains "budget"'
      - 'mimeType="application/vnd.google-apps.document"'
      - 'modifiedTime > "2024-01-01T00:00:00"'
      - 'name contains "project" and trashed=false'

    Returns matching files with id, name, mimeType, size, modifiedTime.
    """
    max_results = min(max_results, MAX_RESULTS_LIMIT)
    service = _ensure_auth()

    results = service.files().list(
        q=query,
        pageSize=max_results,
        fields="files(id,name,mimeType,size,modifiedTime,owners/displayName,link)",
    ).execute()

    files = []
    for f in results.get("files", []):
        files.append({
            "id": f["id"],
            "name": f.get("name", ""),
            "mimeType": f.get("mimeType", ""),
            "size": f.get("size", "0"),
            "modifiedTime": f.get("modifiedTime", ""),
            "owner": f.get("owners", [{}])[0].get("displayName", ""),
            "webViewLink": f.get("link", f"https://drive.google.com/file/d/{f['id']}/view"),
        })
    return files


def drive_get_file(file_id: str) -> dict[str, Any]:
    """Get a file's metadata and content.

    For Google Docs/Sheets/Slides, returns exported plain text or markdown.
    For binary files, returns base64-encoded content.
    """
    service = _ensure_auth()

    # Get metadata
    metadata = service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size,modifiedTime,owners/displayName,webViewLink",
    ).execute()

    mime = metadata.get("mimeType", "")
    result: dict[str, Any] = {
        "id": metadata["id"],
        "name": metadata.get("name", ""),
        "mimeType": mime,
        "size": metadata.get("size", "0"),
        "modifiedTime": metadata.get("modifiedTime", ""),
        "owner": metadata.get("owners", [{}])[0].get("displayName", ""),
        "webViewLink": metadata.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view"),
    }

    # Export Google Workspace files to plain text
    export_map = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }

    if mime in export_map:
        try:
            content = service.files().export(
                fileId=file_id,
                mimeType=export_map[mime],
            ).execute()
            result["content"] = content.decode("utf-8", errors="replace")
            result["contentFormat"] = "text"
        except Exception as e:
            log.warning("Export failed for %s: %s", file_id, e)
            result["content"] = None
            result["contentError"] = str(e)
    elif not mime.startswith("application/vnd.google-apps"):
        # Regular file — download content
        try:
            fh = service.files().get_media(fileId=file_id)
            content = fh.execute()
            result["content"] = base64.b64encode(content).decode("utf-8")
            result["contentFormat"] = "base64"
        except Exception as e:
            log.warning("Download failed for %s: %s", file_id, e)
            result["content"] = None
            result["contentError"] = str(e)
    else:
        result["content"] = None
        result["contentNote"] = "Google Workspace file — use docs_get/sheets_get/slides_get for content"

    return result


def drive_upload_file(
    name: str,
    content_base64: str,
    mime_type: str = "text/plain",
    parent_folder_id: str = "",
) -> dict[str, Any]:
    """Upload a file to Google Drive.

    Parameters:
      name — filename
      content_base64 — file content encoded as base64 string
      mime_type — MIME type (e.g. 'text/plain', 'application/pdf', 'image/png')
      parent_folder_id — optional parent folder ID

    Returns: file metadata (id, name, mimeType, size, webViewLink).
    """
    from googleapiclient.http import MediaIoBaseUpload

    content = base64.b64decode(content_base64)
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type)
    body: dict[str, Any] = {"name": name}
    if parent_folder_id:
        body["parents"] = [parent_folder_id]

    service = _ensure_auth()
    file = service.files().create(
        body=body,
        media_body=media,
        fields="id,name,mimeType,size,webViewLink,modifiedTime",
    ).execute()

    log.info("Uploaded file '%s' to Drive — ID: %s", name, file["id"])
    return {
        "id": file["id"],
        "name": file.get("name", ""),
        "mimeType": file.get("mimeType", ""),
        "size": file.get("size", "0"),
        "modifiedTime": file.get("modifiedTime", ""),
        "webViewLink": file.get("webViewLink", f"https://drive.google.com/file/d/{file['id']}/view"),
    }


def drive_create_file(
    name: str,
    content: str,
    mime_type: str = "text/plain",
    parent_folder_id: str = "",
) -> dict[str, Any]:
    """Create a text file in Google Drive with inline content."""
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    return drive_upload_file(name, content_b64, mime_type, parent_folder_id)


def drive_delete_file(file_id: str) -> str:
    """Delete a file from Google Drive by ID.

    Moves the file to trash (Google Drive API delete permanently removes).
    Returns the deleted file ID.
    """
    service = _ensure_auth()
    service.files().delete(fileId=file_id).execute()
    log.info("Deleted Drive file — ID: %s", file_id)
    return file_id

