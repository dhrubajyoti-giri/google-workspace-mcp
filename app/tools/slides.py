"""Google Slides tools."""
from __future__ import annotations

import logging
from typing import Any

from app.google_client import get_google_client

log = logging.getLogger("google-workspace-mcp.tools.slides")


def _ensure_auth():
    client = get_google_client()
    if not client.has_token():
        raise RuntimeError(
            "Google authentication required. Complete OAuth via your MCP client."
        )
    return client.get_service("slides", "v1")


def slides_get(presentation_id: str) -> dict[str, Any]:
    """Retrieve a Google Slides presentation's structure and text content.

    Parameters:
      presentation_id — the presentation ID (from the presentation's URL)

    Returns:
      - id: presentation ID
      - title: presentation title
      - slideCount: number of slides
      - slides: list of slides, each with pageElements (text, shapes, images)
    """
    service = _ensure_auth()
    pres = service.presentations().get(presentationId=presentation_id).execute()

    slides_out: list[dict[str, Any]] = []
    for slide in pres.get("slides", []):
        objects_on_slide: list[dict[str, Any]] = []
        for elem in slide.get("pageElements", []):
            shape = elem.get("shape", {})
            text_range = shape.get("text", {}).get("textElements", [])
            texts = []
            for te in text_range:
                if "textRun" in te:
                    texts.append(te["textRun"].get("content", ""))
            image = elem.get("image", {})
            objects_on_slide.append({
                "objectId": elem.get("objectId", ""),
                "type": "shape" if shape else ("image" if image else "unknown"),
                "text": " ".join(texts).strip(),
                "shapeType": shape.get("shapeType", ""),
            })

        slides_out.append({
            "objectId": slide.get("objectId", ""),
            "pageElements": objects_on_slide,
            "pageElementsCount": len(slide.get("pageElements", [])),
        })

    return {
        "id": pres.get("presentationId", ""),
        "title": pres.get("title", ""),
        "slideCount": len(slides_out),
        "slides": slides_out,
    }


def slides_create(title: str = "Untitled Presentation") -> str:
    """Create a new Google Slides presentation.

    Parameters:
      title — presentation title

    Returns the new presentation ID.
    """
    service = _ensure_auth()
    pres = service.presentations().create(body={"title": title}).execute()
    log.info("Created presentation '%s' — ID: %s", title, pres["presentationId"])
    return pres["presentationId"]


def slides_update(presentation_id: str, requests_json: str) -> dict[str, Any]:
    """Batch update a Google Slides presentation.

    Parameters:
      presentation_id — the presentation ID
      requests_json — JSON array of Slides API request objects as a string.
        Example: '[{"createSlide": {"slideObjectProperties": {"title": "New Slide"}}}]'

    Returns: the batchUpdate response with replies.
    """
    import json as _json
    service = _ensure_auth()
    requests = _json.loads(requests_json)
    result = service.presentations().batchUpdate(
        presentationId=presentation_id,
        body={"requests": requests},
    ).execute()
    log.info("Applied %d batch updates to presentation %s", len(requests), presentation_id)
    return {
        "presentationId": presentation_id,
        "replies": result.get("replies", []),
    }
