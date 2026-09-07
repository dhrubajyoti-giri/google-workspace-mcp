"""Google Calendar tools."""
from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.google_client import get_google_client

log = logging.getLogger("google-workspace-mcp.tools.calendar")


def _ensure_auth():
    client = get_google_client()
    if not client.has_token():
        raise RuntimeError(
            "Google authentication required. Complete OAuth via your MCP client."
        )
    return client.get_service("calendar", "v3")


def calendar_list_events(
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """List events from a Google Calendar.

    Parameters:
      calendar_id — 'primary' (default) or a calendar ID/email
      time_min — ISO 8601 start time (e.g. '2024-01-01T00:00:00+05:30')
      time_max — ISO 8601 end time
      max_results — max events to return (default 10, max 2500)

    Returns a list of events with: id, summary, start, end, location, description.
    """
    service = _ensure_auth()

    kwargs: dict[str, Any] = {
        "calendarId": calendar_id,
        "maxResults": min(max_results, 2500),
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if time_min:
        kwargs["timeMin"] = time_min
    if time_max:
        kwargs["timeMax"] = time_max

    events_result = service.events().list(**kwargs).execute()
    events = events_result.get("items", [])

    out: list[dict[str, Any]] = []
    for event in events:
        start = event.get("start", {})
        end = event.get("end", {})
        out.append({
            "id": event.get("id", ""),
            "summary": event.get("summary", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "location": event.get("location", ""),
            "description": event.get("description", ""),
            "status": event.get("status", ""),
            "htmlLink": event.get("htmlLink", ""),
        })
    return out


def calendar_get_event(
    calendar_id: str = "primary",
    event_id: str = "",
) -> dict[str, Any]:
    """Get a single calendar event by ID."""
    service = _ensure_auth()
    event = service.events().get(
        calendarId=calendar_id,
        eventId=event_id,
    ).execute()

    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id", ""),
        "summary": event.get("summary", ""),
        "start": start.get("dateTime") or start.get("date", ""),
        "end": end.get("dateTime") or end.get("date", ""),
        "location": event.get("location", ""),
        "description": event.get("description", ""),
        "status": event.get("status", ""),
        "htmlLink": event.get("htmlLink", ""),
        "attendees": event.get("attendees", []),
        "recurrence": event.get("recurrence", []),
    }


def calendar_create_event(
    calendar_id: str = "primary",
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
    attendees: list[dict[str, str]] = None,
) -> str:
    """Create a calendar event.

    Parameters:
      calendar_id — 'primary' (default) or calendar ID
      summary — event title
      start_time — ISO 8601 start (e.g. '2024-01-15T14:00:00+05:30')
      end_time — ISO 8601 end time
      description — event description
      location — event location
      attendees — list of {'email': 'name@example.com'} dicts (optional)

    Returns the new event ID.
    """
    service = _ensure_auth()
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start_time, "timeZone": settings.tz},
        "end": {"dateTime": end_time, "timeZone": settings.tz},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = attendees

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    log.info("Created event '%s' — ID: %s", summary, event["id"])
    return event["id"]


def calendar_update_event(
    calendar_id: str = "primary",
    event_id: str = "",
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
) -> str:
    """Update a calendar event by ID.

    Only non-empty fields are sent in the patch.
    Returns the updated event ID.
    """
    service = _ensure_auth()
    body: dict[str, Any] = {}
    if summary:
        body["summary"] = summary
    if start_time:
        body["start"] = {"dateTime": start_time, "timeZone": settings.tz}
    if end_time:
        body["end"] = {"dateTime": end_time, "timeZone": settings.tz}
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    event = service.events().update(
        calendarId=calendar_id,
        eventId=event_id,
        body=body,
    ).execute()
    log.info("Updated event %s", event_id)
    return event["id"]


def calendar_delete_event(calendar_id: str = "primary", event_id: str = "") -> str:
    """Delete a calendar event by ID. Returns the deleted event ID."""
    service = _ensure_auth()
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    log.info("Deleted event %s from calendar %s", event_id, calendar_id)
    return event_id


