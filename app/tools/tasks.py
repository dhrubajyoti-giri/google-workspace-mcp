"""Google Tasks tools."""
from __future__ import annotations

import logging
from typing import Any

from app.google_client import get_google_client

log = logging.getLogger("google-workspace-mcp.tools.tasks")


def _ensure_auth():
    client = get_google_client()
    if not client.has_token():
        raise RuntimeError(
            "Google authentication required. Complete OAuth via your MCP client."
        )
    return client.get_service("tasks", "v1")


def tasks_list_lists() -> list[dict[str, Any]]:
    """List all Google Tasks task lists.

    Returns a list of task lists with: id, title, kind, etc.
    """
    service = _ensure_auth()
    results = []
    response = service.tasklists().list().execute()
    results.extend(response.get("items", []))
    return results


def tasks_get_list(list_id: str = "@default") -> dict[str, Any]:
    """Get a single task list by ID.

    Parameters:
      list_id — the task list ID (or '@default' for the default list)
    """
    service = _ensure_auth()
    result = service.tasklists().get(tasklist=list_id).execute()
    return {
        "id": result.get("id", ""),
        "title": result.get("title", ""),
        "kind": result.get("kind", ""),
        "selfLink": result.get("selfLink", ""),
    }


def tasks_create_list(title: str = "") -> dict[str, Any]:
    """Create a new Google Tasks task list.

    Parameters:
      title — the title of the new task list
    """
    service = _ensure_auth()
    body = {"title": title}
    result = service.tasklists().insert(body=body).execute()
    list_id = result.get("id", "")
    log.info("Created task list '%s' — ID: %s", title, list_id)
    return {
        "id": list_id,
        "title": result.get("title", ""),
        "kind": result.get("kind", ""),
        "selfLink": result.get("selfLink", ""),
        "message": f"Task list created successfully — '{title}' (ID: {list_id})",
    }


def tasks_delete_list(list_id: str = "") -> dict[str, Any]:
    """Delete a task list by ID.

    Parameters:
      list_id — the task list ID to delete
    """
    service = _ensure_auth()
    service.tasklists().delete(tasklist=list_id).execute()
    log.info("Deleted task list %s", list_id)
    return {
        "id": list_id,
        "status": "deleted",
        "message": f"Task list deleted successfully — ID: {list_id}",
    }


def tasks_list_tasks(
    list_id: str = "@default",
    show_completed: bool = False,
    show_deleted: bool = False,
    show_hidden: bool = False,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """List tasks in a task list.

    Parameters:
      list_id — the task list ID (or '@default' for the default list)
      show_completed — if True, include completed tasks
      show_deleted — if True, include deleted tasks
      show_hidden — if True, include hidden tasks
      max_results — max tasks to return (default 100)

    Returns a list of tasks with: id, title, status, due, notes, completed, etc.
    """
    service = _ensure_auth()
    kwargs: dict[str, Any] = {
        "tasklist": list_id,
        "maxResults": min(max_results, 1000),
    }
    if show_completed:
        kwargs["showCompleted"] = "true"
    if show_deleted:
        kwargs["showDeleted"] = "true"
    if show_hidden:
        kwargs["showHidden"] = "true"

    results = []
    response = service.tasks().list(**kwargs).execute()
    raw_tasks = response.get("items", [])

    for task in raw_tasks:
        results.append({
            "id": task.get("id", ""),
            "title": task.get("title", ""),
            "status": task.get("status", ""),
            "notes": task.get("notes", ""),
            "due": task.get("due", ""),
            "completed": task.get("completed", ""),
            "deleted": task.get("deleted", False),
            "hidden": task.get("hidden", False),
            "selfLink": task.get("selfLink", ""),
        })

    return results


def tasks_get_task(list_id: str = "@default", task_id: str = "") -> dict[str, Any]:
    """Get a single task by ID.

    Parameters:
      list_id — the task list ID
      task_id — the task ID
    """
    service = _ensure_auth()
    task = service.tasks().get(tasklist=list_id, task=task_id).execute()
    return {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "status": task.get("status", ""),
        "notes": task.get("notes", ""),
        "due": task.get("due", ""),
        "completed": task.get("completed", ""),
        "deleted": task.get("deleted", False),
        "hidden": task.get("hidden", False),
        "parent": task.get("parent", ""),
        "position": task.get("position", ""),
        "selfLink": task.get("selfLink", ""),
    }


def tasks_create_task(
    list_id: str = "@default",
    title: str = "",
    notes: str = "",
    due: str = "",
    completed: str = "",
) -> dict[str, Any]:
    """Create a new task in a task list.

    Parameters:
      list_id — the task list ID (or '@default' for the default list)
      title — task title (required)
      notes — task notes/description
      due — due date as RFC 3339 timestamp (e.g. '2024-01-15T14:00:00.000Z')
      completed — if set, marks task as complete with this timestamp

    Returns: dict with id, title, status, message.
    """
    service = _ensure_auth()
    body: dict[str, Any] = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = due
    if completed:
        body["completed"] = completed
        body["status"] = "completed"

    result = service.tasks().insert(tasklist=list_id, body=body).execute()
    task_id = result.get("id", "")
    status = result.get("status", "needsAction")
    log.info("Created task '%s' in list %s — ID: %s", title, list_id, task_id)
    return {
        "id": task_id,
        "title": result.get("title", ""),
        "status": status,
        "list_id": list_id,
        "message": f"Task created successfully — '{title}' (ID: {task_id}, list: {list_id})",
    }


def tasks_update_task(
    list_id: str = "@default",
    task_id: str = "",
    title: str = "",
    notes: str = "",
    due: str = "",
    completed: str = "",
    deleted: bool = False,
) -> dict[str, Any]:
    """Update a task by ID. Only non-empty fields are sent in the patch.

    Parameters:
      list_id — the task list ID
      task_id — the task ID to update
      title — new title (leave empty to keep current)
      notes — new notes (leave empty to keep current)
      due — new due date as RFC 3339 timestamp
      completed — timestamp to mark as complete (empty string clears it → sets status to 'needsAction')
      deleted — if True, deletes the task

    Returns: dict with id, title, updated_fields, message.
    """
    service = _ensure_auth()
    body: dict[str, Any] = {}
    updated_fields: list[str] = []

    # If completed is provided, we're toggling completion status
    if completed:
        body["completed"] = completed
        body["status"] = "completed"
        updated_fields.append("completed")
    elif completed == "":
        # Empty string means user wants to un-complete the task
        body["completed"] = None
        body["status"] = "needsAction"
        updated_fields.append("completed")

    if title:
        body["title"] = title
        updated_fields.append("title")
    if notes:
        body["notes"] = notes
        updated_fields.append("notes")
    if due:
        body["due"] = due
        updated_fields.append("due")
    if deleted:
        body["deleted"] = True
        updated_fields.append("deleted")

    result = service.tasks().update(tasklist=list_id, task=task_id, body=body).execute()
    task_id_result = result.get("id", task_id)
    log.info("Updated task %s in list %s", task_id, list_id)
    return {
        "id": task_id_result,
        "title": result.get("title", ""),
        "list_id": list_id,
        "updated_fields": updated_fields,
        "message": f"Task updated successfully — ID: {task_id_result} (fields: {', '.join(updated_fields) if updated_fields else 'none'})",
    }


def tasks_delete_task(list_id: str = "@default", task_id: str = "") -> dict[str, Any]:
    """Delete a task (move to trash).

    Parameters:
      list_id — the task list ID
      task_id — the task ID to delete
    """
    service = _ensure_auth()
    service.tasks().delete(tasklist=list_id, task=task_id).execute()
    log.info("Deleted task %s from list %s", task_id, list_id)
    return {
        "id": task_id,
        "list_id": list_id,
        "status": "deleted",
        "message": f"Task deleted successfully — ID: {task_id} from list: {list_id}",
    }


def tasks_complete_task(list_id: str = "@default", task_id: str = "") -> dict[str, Any]:
    """Mark a task as completed (shortcut for tasks_update_task with completed set).

    Parameters:
      list_id — the task list ID
      task_id — the task ID to complete
    """
    result = tasks_update_task(
        list_id=list_id,
        task_id=task_id,
        completed="1970-01-01T00:00:00.000Z",
    )
    return {
        "id": result.get("id", task_id),
        "title": result.get("title", ""),
        "status": "completed",
        "message": f"Task marked as completed — ID: {task_id}",
    }
