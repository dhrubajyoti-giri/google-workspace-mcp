"""Gmail tools: search, get message, send, draft."""
from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from typing import Any

from app.google_client import get_google_client

log = logging.getLogger("google-workspace-mcp.tools.gmail")

MAX_RESULTS_LIMIT = 100


def _ensure_auth():
    client = get_google_client()
    if not client.has_token():
        raise RuntimeError(
            "Google authentication required. Complete OAuth via your MCP client."
        )
    return client.get_service("gmail", "v1")


def gmail_search(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Search the authenticated user's Gmail mailbox.

    Uses Gmail search syntax.
    Examples:
      - 'invoice' — any message containing "invoice"
      - 'from:example.com' — messages from a specific sender
      - 'subject:"meeting notes"' — messages with a subject match
      - 'is:unread' — unread messages
      - 'after:2024/01/01 before:2024/02/01' — date range

    Returns a list of message summaries (id, threadId, subject, from, date).
    """
    max_results = min(max_results, MAX_RESULTS_LIMIT)
    service = _ensure_auth()

    results = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()

    messages = results.get("messages", [])
    out: list[dict[str, Any]] = []

    for msg in messages:
        msg_detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg_detail.get("payload", {}).get("headers", [])}
        out.append({
            "id": msg_detail["id"],
            "threadId": msg_detail.get("threadId", ""),
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "snippet": msg_detail.get("snippet", ""),
        })

    return out


def gmail_get_message(message_id: str) -> dict[str, Any]:
    """Retrieve a single Gmail message by ID.

    Returns the full message: headers, body (text + HTML where available),
    labels, and internal date.
    """
    service = _ensure_auth()
    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    # Extract body parts
    text_body = ""
    html_body = ""
    parts = msg.get("payload", {}).get("parts", [])
    for part in parts:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if mime == "text/plain" and not text_body:
                text_body = decoded
            elif mime == "text/html":
                html_body = decoded

    return {
        "id": msg["id"],
        "threadId": msg.get("threadId", ""),
        "labels": msg.get("labelIds", []),
        "internalDate": msg.get("internalDate", ""),
        "headers": headers,
        "snippet": msg.get("snippet", ""),
        "textBody": text_body,
        "htmlBody": html_body,
    }


def gmail_send(
    to: str,
    subject: str,
    body: str,
    body_format: str = "plain",
    cc: str = "",
    bcc: str = "",
) -> dict[str, Any]:
    """Send an email via the authenticated Gmail account.

    Parameters:
      to — comma-separated recipient list
      subject — email subject
      body — email body content
      body_format — 'plain' or 'html'
      cc — comma-separated CC recipients (optional)
      bcc — comma-separated BCC recipients (optional)

    Returns: dict with id, threadId, labelIds, to, subject, status, message.
    """
    mime = MIMEText(body, "html" if body_format == "html" else "plain")
    mime["To"] = to
    mime["Subject"] = subject
    if cc:
        mime["Cc"] = cc
    if bcc:
        mime["Bcc"] = bcc

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")

    service = _ensure_auth()
    sent = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    log.info("Sent email to %s — message ID: %s", to, sent.get("id", ""))
    msg_id = sent.get("id", "")
    return {
        "id": msg_id,
        "threadId": sent.get("threadId", ""),
        "labelIds": sent.get("labelIds", []),
        "to": to,
        "subject": subject,
        "cc": cc,
        "bcc": bcc,
        "status": "sent",
        "message": f"Email sent successfully to {to} — message ID: {msg_id}",
    }


def gmail_create_draft(
    to: str,
    subject: str,
    body: str,
    body_format: str = "plain",
    cc: str = "",
    bcc: str = "",
) -> dict[str, Any]:
    """Create a draft email in the authenticated user's Gmail account.

    Parameters:
      to — comma-separated recipient list
      subject — email subject
      body — email body content
      body_format — 'plain' (default) or 'html'
      cc — comma-separated CC recipients (optional)
      bcc — comma-separated BCC recipients (optional)

    Returns: dict with id, to, subject, status, message.
    """
    mime = MIMEText(body, "html" if body_format == "html" else "plain")
    mime["To"] = to
    mime["Subject"] = subject
    if cc:
        mime["Cc"] = cc
    if bcc:
        mime["Bcc"] = bcc

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    service = _ensure_auth()
    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw}},
    ).execute()

    log.info("Draft created — draft ID: %s, to: %s", draft["id"], to)
    return {
        "id": draft["id"],
        "to": to,
        "subject": subject,
        "status": "draft_created",
        "message": f"Draft created successfully — draft ID: {draft['id']}, to: {to}",
    }
