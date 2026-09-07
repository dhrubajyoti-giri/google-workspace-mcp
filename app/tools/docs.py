"""Google Docs tools: get document content."""
from __future__ import annotations

import logging
from typing import Any

from app.google_client import get_google_client

log = logging.getLogger("google-workspace-mcp.tools.docs")


def _ensure_auth():
    client = get_google_client()
    if not client.has_token():
        raise RuntimeError(
            "Google authentication required. Complete OAuth via your MCP client."
        )
    return client.get_service("docs", "v1")


def docs_get(document_id: str) -> dict[str, Any]:
    """Retrieve a Google Doc's content and structural information.

    Parameters:
      document_id — the Google Doc ID (from the doc's URL)

    Returns:
      - title: document title
      - id: document ID
      - body: text content extracted from the document (paragraphs joined by newlines)
      - body_sections: structured sections with heading levels
      - tables: list of tables found in the document
      - lists: list of bullet/numbered lists
      - lastUpdated: document update time (if available)
    """
    service = _ensure_auth()
    doc = service.documents().get(documentId=document_id).execute()

    doc_id = doc.get("documentId", "")
    title = doc.get("title", "")

    body_text_parts: list[str] = []
    sections: list[dict[str, Any]] = []
    tables: list[dict] = []
    lists: list[str] = []
    current_heading: str | None = None

    def _extract_text(element: dict) -> None:
        """Recursively extract text from a document element."""
        if "paragraph" in element:
            para = element["paragraph"]
            para_text_parts: list[str] = []

            # Track headings
            para_style = para.get("paragraphStyle", {})
            named_style = para_style.get("namedStyleType", "")

            if named_style in ("HEADING_1", "HEADING_2", "HEADING_3", "HEADING_4", "HEADING_5", "HEADING_6"):
                heading_text = ""
                for elem in para.get("elements", []):
                    if "textRun" in elem:
                        heading_text += elem["textRun"].get("content", "")
                if heading_text.strip():
                    sections.append({
                        "text": heading_text.strip(),
                        "level": int(named_style[-1]),
                        "startIndex": element.get("startIndex", 0),
                    })
                    body_text_parts.append(f"\n{heading_text.strip()}\n")

            # Extract paragraph content
            for elem in para.get("elements", []):
                if "textRun" in elem:
                    text = elem["textRun"].get("content", "")
                    if text:
                        body_text_parts.append(text)
                        para_text_parts.append(text)

            # Check for tables within this paragraph (rare, but possible)
            for child in para.get("table", {}).get("tableCells", []):
                _extract_table(child)

        if "table" in element:
            _extract_table(element["table"])

        if "table" in element and "tableRows" not in element:
            pass  # handled above

        if "list" not in element:
            return

        for child_elem in element.get("listItem", {}).get("elements", []):
            if "textRun" in child_elem:
                text = child_elem["textRun"].get("content", "")
                if text.strip():
                    lists.append(text.strip())

    def _extract_table(table: dict) -> None:
        table_data = {"rows": [], "startIndex": table.get("startIndex", 0)}
        for row in table.get("tableRows", []):
            row_data = []
            for cell in row.get("tableCells", []):
                cell_text_parts: list[str] = []
                for elem in cell.get("elements", []):
                    if "paragraph" in elem:
                        for pe in elem["paragraph"].get("elements", []):
                            if "textRun" in pe:
                                cell_text_parts.append(pe["textRun"].get("content", ""))
                row_data.append(" ".join(cell_text_parts).strip())
            table_data["rows"].append(row_data)
        tables.append(table_data)

    for elem in doc.get("body", {}).get("content", []):
        _extract_text(elem)

    body_text = "".join(body_text_parts).strip()

    return {
        "id": doc_id,
        "title": title,
        "body": body_text,
        "body_sections": sections,
        "tables": tables,
        "lists": lists,
        "bodyTextSize": doc.get("bodyTextSize", ""),
        "lineMode": doc.get("documentStyle", {}).get("lineMode", ""),
    }


def docs_create(title: str, content: str = "") -> str:
    """Create a new Google Doc.

    Parameters:
      title — document title
      content — optional initial text content (inserted after creation)

    Returns the new document ID.
    """
    service = _ensure_auth()
    doc = service.documents().create(body={"title": title}, fields="documentId").execute()
    doc_id = doc["documentId"]

    if content:
        docs_update(doc_id, content)

    log.info("Created Google Doc '%s' — ID: %s", title, doc_id)
    return doc_id


def docs_update(document_id: str, text: str, location_index: int = 1) -> dict[str, Any]:
    """Insert text into a Google Doc at the specified location.

    Parameters:
      document_id — the Google Doc ID
      text — text to insert
      location_index — insertion index (1 = after the document start; default 1)

    Returns the batchUpdate response.
    """
    service = _ensure_auth()
    requests = [{
        "insertText": {
            "location": {"index": location_index},
            "text": text,
        }
    }]
    result = service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()
    log.info("Inserted %d chars into Doc %s", len(text), document_id)
    return result
