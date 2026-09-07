"""Google Sheets tools."""
from __future__ import annotations

import logging
from typing import Any

from app.google_client import get_google_client

log = logging.getLogger("google-workspace-mcp.tools.sheets")


def _ensure_auth():
    client = get_google_client()
    if not client.has_token():
        raise RuntimeError(
            "Google authentication required. Visit /oauth/start to authorize."
        )
    return client.get_service("sheets", "v4")


def sheets_get(spreadsheet_id: str, range: str = "A1:Z100") -> dict[str, Any]:
    """Retrieve values from a Google Sheets spreadsheet.

    Parameters:
      spreadsheet_id — the spreadsheet ID (from the sheet's URL)
      range — A1 notation range, e.g. 'Sheet1!A1:F20' (default: 'A1:Z100')

    Returns:
      - spreadsheetId
      - sheetNames — list of all worksheet names
      - range — the requested range
      - values — 2D array of cell values (row-major)
      - valueRanges — structured range/value pairs (if batch used)
    """
    service = _ensure_auth()

    # Get spreadsheet metadata (sheet names)
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="properties.title,sheets.properties",
    ).execute()

    sheet_names = [s.get("properties", {}).get("title", "")
                   for s in meta.get("sheets", [])]

    # Get values
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range,
    ).execute()

    return {
        "spreadsheetId": spreadsheet_id,
        "sheetNames": sheet_names,
        "properties_title": meta.get("properties", {}).get("title", ""),
        "range": result.get("range", range),
        "values": result.get("values", []),
    }


def sheets_update(spreadsheet_id: str, range: str, values: list[list[str]]) -> dict[str, Any]:
    """Update cell values in a Google Sheet.

    Parameters:
      spreadsheet_id — the spreadsheet ID
      range — A1 notation range, e.g. 'Sheet1!A1:C3'
      values — 2D array of cell values (row-major)

    Returns: the update response (updatedCells count, etc.).
    """
    service = _ensure_auth()
    body = {"values": values}
    result = service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="RAW",
        body=body,
    ).execute()
    log.info("Updated %sx%s cells in Sheet %s range %s",
             len(values), len(values[0]) if values else 0, spreadsheet_id, range)
    return result


def sheets_append(spreadsheet_id: str, range: str, values: list[list[str]]) -> dict[str, Any]:
    """Append rows to the end of a Google Sheet.

    Parameters:
      spreadsheet_id — the spreadsheet ID
      range — A1 notation range (e.g. 'Sheet1!A1:Z100'); determines which sheet
      values — 2D array of rows to append (row-major)

    Returns: the append response (appendedRange, appendedRows).
    """
    service = _ensure_auth()
    body = {"values": values}
    result = service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()
    log.info("Appended %d rows to Sheet %s", len(values), spreadsheet_id)
    return {
        "spreadsheetId": spreadsheet_id,
        "appendedRange": result.get("range", ""),
        "updates": result.get("updates", {}),
    }
