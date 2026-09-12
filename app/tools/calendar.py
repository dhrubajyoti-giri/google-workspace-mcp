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
    recurrence: list[str] = None,
    reminders: list[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a calendar event.

    Parameters:
      calendar_id — 'primary' (default) or calendar ID
      summary — event title
      start_time — ISO 8601 start (e.g. '2024-06-01T14:00:00+05:30')
      end_time — ISO 8601 end time
      description — event description (optional)
      location — event location (optional)
      attendees — list of {'email': 'addr'} dicts (optional)
      recurrence — list of RRULE strings, e.g. ['RRULE:FREQ=HOURLY;INTERVAL=1']
        Google supports FREQ=SECONDLY/MINUTELY/HOURLY/DAILY/WEEKLY/MONTHLY/YEARLY
        with INTERVAL, BYDAY, UNTIL, COUNT, etc.
      reminders — list of reminder overrides, each {'method': 'popup'|'email', 'minutes': N}.
        E.g. [{'method': 'popup', 'minutes': 5}] sends a popup 5 min before start.
        Max 5 overrides. Minutes range 0–40320 (4 weeks).

    Returns: dict with id, summary, start, end, location, status, recurrence, htmlLink, message.
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
    if recurrence:
        body["recurrence"] = recurrence
    if reminders:
        body["reminders"] = {"useDefault": False, "overrides": reminders}

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    event_id = event.get("id", "")
    log.info("Created event '%s' — ID: %s", summary, event_id)
    return {
        "id": event_id,
        "summary": event.get("summary", ""),
        "start": event.get("start", {}).get("dateTime", ""),
        "end": event.get("end", {}).get("dateTime", ""),
        "location": event.get("location", ""),
        "status": event.get("status", ""),
        "recurrence": event.get("recurrence", []),
        "reminders": event.get("reminders", {}),
        "htmlLink": event.get("htmlLink", ""),
        "message": f"Event created successfully — '{summary}' (ID: {event_id})",
    }


def calendar_update_event(
    calendar_id: str = "primary",
    event_id: str = "",
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
    recurrence: list[str] = None,
    reminders: list[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Update a calendar event by ID.

    Only non-empty fields are sent in the patch.
    Parameters:
      calendar_id — 'primary' (default) or calendar ID
      event_id — the event to update
      summary — new title (leave empty to keep current)
      start_time — new ISO 8601 start (leave empty to keep)
      end_time — new ISO 8601 end (leave empty to keep)
      description — new description (leave empty to keep)
      location — new location (leave empty to keep)
      recurrence — list of RRULE strings to set (leave empty to keep)
      reminders — list of {'method': 'popup'|'email', 'minutes': N} overrides
        (leave empty to keep current)

    Returns: dict with id, updated_fields, status, message.
    """
    service = _ensure_auth()
    body: dict[str, Any] = {}
    updated_fields: list[str] = []
    if summary:
        body["summary"] = summary
        updated_fields.append("summary")
    if start_time:
        body["start"] = {"dateTime": start_time, "timeZone": settings.tz}
        updated_fields.append("start")
    if end_time:
        body["end"] = {"dateTime": end_time, "timeZone": settings.tz}
        updated_fields.append("end")
    if description:
        body["description"] = description
        updated_fields.append("description")
    if location:
        body["location"] = location
        updated_fields.append("location")
    if recurrence:
        body["recurrence"] = recurrence
        updated_fields.append("recurrence")
    if reminders:
        body["reminders"] = {"useDefault": False, "overrides": reminders}
        updated_fields.append("reminders")

    event = service.events().update(
        calendarId=calendar_id,
        eventId=event_id,
        body=body,
    ).execute()
    resp_id = event.get("id", event_id)
    summary = event.get("summary", "")
    log.info("Updated event '%s' (%s)", summary, resp_id)
    return {
        "id": resp_id,
        "summary": summary,
        "updated_fields": updated_fields,
        "status": event.get("status", ""),
        "htmlLink": event.get("htmlLink", ""),
        "message": f"Event updated — '{summary}' (ID: {resp_id}) — fields: {', '.join(updated_fields) if updated_fields else 'none'}",
    }


def calendar_delete_event(calendar_id: str = "primary", event_id: str = "") -> dict[str, Any]:
    """Delete a calendar event by ID.

    Returns: dict with id, status, message.
    """
    service = _ensure_auth()
    # Retrieve event summary before deleting (for user-facing response)
    summary = event_id
    try:
        event_info = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        summary = event_info.get("summary", event_id)
    except Exception:
        pass
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    log.info("Deleted event '%s' (%s) from calendar %s", summary, event_id, calendar_id)
    return {
        "id": event_id,
        "summary": summary,
        "status": "deleted",
        "message": f"Event deleted — '{summary}' (ID: {event_id})",
    }


