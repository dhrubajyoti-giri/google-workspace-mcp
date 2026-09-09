"""Capture the actual MCP JSON-RPC CallToolResult for each tool return type.

Mirrors the real return-type annotations from google-workspace-mcp's mcp_server.py:
  - str                          (gmail_send, gmail_create_draft, drive_delete_file,
                                   docs_create, calendar_create_event, calendar_update_event,
                                   calendar_delete_event, slides_create)
  - dict[str, Any]               (docs_get, sheets_get, calendar_get_event, slides_get,
                                   drive_get_file, gmail_get_message, drive_upload_file)
  - list[dict[str, Any]]         (gmail_search, drive_search, calendar_list_events)

Uses the SAME code path as the MCP SDK's lowlevel server:
  Tool.run() → FuncMetadata.convert_result() → lowlevel Server.call_tool handler
  → CallToolResult (content + structuredContent).

Then also shows what QwenPaw's agentscope_tool.py adapter extracts from that
result (via _blocks_from_value) — this is what the LLM actually sees.

Run:  python tests/protocol_response_test.py
"""
import json
import textwrap
from typing import Any

from mcp.server.fastmcp.utilities import func_metadata as fm
from mcp.types import CallToolResult


def _make_func_metadata(rv: Any, return_type: Any) -> fm.FuncMetadata:
    """Build FuncMetadata for a synthetic function with a real return annotation."""
    # We use a real function with the proper annotation so the SDK's
    # func_metadata() correctly detects the output model.
    func = type(
        "T",
        (),
        {"__annotations__": {"return": return_type}},
    )()
    # Create a callable with the right annotations
    import inspect
    sig = inspect.signature(lambda: None)
    src = f"def _tool_fn() -> {return_type.__name__ if hasattr(return_type, '__name__') else 'Any'}:\n    return None"

    # Simpler: use exec to create a real function with the annotation
    namespace: dict[str, Any] = {"Any": Any}
    exec(
        textwrap.dedent(src),
        namespace,
    )
    func = namespace["_tool_fn"]
    return fm.func_metadata(func, structured_output=None)


def _simulate_full_flow(label: str, return_value: Any, return_type_str: str):
    """Simulate the full flow: tool_fn → convert_result → CallToolResult →
    QwenPaw adapter _blocks_from_value → LLM text blocks."""

    # Build FuncMetadata with proper return type annotation
    namespace: dict[str, Any] = {"Any": Any, "list": list, "dict": dict}
    code = f"def _tool_fn() -> {return_type_str}:\n    return None"
    exec(code, namespace)
    func = namespace["_tool_fn"]
    meta = fm.func_metadata(func, structured_output=None)

    # Step 1: Tool.run() calls convert_result
    converted = meta.convert_result(return_value)

    # Step 2: lowlevel server wraps into CallToolResult
    if isinstance(converted, tuple):
        unstructured_content, structured_content = converted
        ct = CallToolResult(
            content=list(unstructured_content),
            structuredContent=structured_content,
            isError=False,
        )
    else:
        ct = CallToolResult(
            content=list(converted),
            isError=False,
        )

    # Step 3: QwenPaw agentscope_tool.py _blocks_from_value
    # This is what the LLM adapter extracts
    llm_text_blocks = []

    # Simulate _blocks_from_mcp_content
    for item in ct.content:
        text = getattr(item, "text", None)
        if text is not None:
            llm_text_blocks.append(str(text))

    # Then structuredContent is appended as another text block
    if ct.structuredContent is not None:
        llm_text_blocks.append(json.dumps(ct.structuredContent, indent=2))

    return ct, llm_text_blocks


def main():
    test_cases = [
        # ── str-returning tools (8 tools) ──────────────────────────
        ("gmail_send", "msg_abc123xyz", "str"),
        ("gmail_create_draft", "draft_789", "str"),
        ("drive_delete_file", "file_xyz", "str"),
        ("docs_create", "doc456", "str"),
        ("calendar_create_event", "event_42", "str"),
        ("calendar_update_event", "event_42", "str"),
        ("calendar_delete_event", "event_42", "str"),
        ("slides_create", "pres_abc", "str"),

        # ── dict-returning tools ──────────────────────────────────
        ("docs_get", {
            "id": "doc456", "title": "Project Report",
            "body": "Introduction\nThis is the report body.\nConclusion",
            "body_sections": [{"text": "Introduction", "level": 1}],
            "tables": [], "lists": [],
        }, "dict[str, Any]"),

        ("gmail_get_message", {
            "id": "msg1", "threadId": "t1", "labels": ["INBOX"],
            "headers": {"Subject": "Invoice", "From": "billing@example.com"},
            "snippet": "Please find the invoice attached.",
            "textBody": "Please find the invoice attached.",
            "htmlBody": "<p>Please find the invoice attached.</p>",
        }, "dict[str, Any]"),

        ("drive_get_file", {
            "id": "file_xyz", "name": "report.pdf",
            "mimeType": "application/pdf", "size": "10240",
            "content": "base64-encoded...", "contentFormat": "base64",
        }, "dict[str, Any]"),

        # ── list[dict]-returning tools ────────────────────────────
        ("gmail_search", [
            {"id": "msg1", "subject": "Invoice", "from": "billing@example.com", "snippet": "Please find attached..."},
            {"id": "msg2", "subject": "Meeting Notes", "from": "team@example.com", "snippet": "Action items..."},
        ], "list[dict[str, Any]]"),

        ("drive_search", [
            {"id": "f1", "name": "test.txt", "mimeType": "text/plain", "size": "1024"},
            {"id": "f2", "name": "data.csv", "mimeType": "text/csv", "size": "2048"},
        ], "list[dict[str, Any]]"),

        ("calendar_list_events", [
            {"id": "e1", "summary": "Standup", "start": "2024-06-01T10:00:00+05:30"},
            {"id": "e2", "summary": "Review", "start": "2024-06-01T14:00:00+05:30"},
        ], "list[dict[str, Any]]"),
    ]

    print(f"{'='*78}")
    print("MCP JSON-RPC Protocol Response Analysis")
    print("google-workspace-mcp tool return types → CallToolResult → LLM input")
    print(f"{'='*78}")
    print()
    print("The MCP SDK converts each Python return value into a CallToolResult.")
    print("QwenPaw's driver adapter (_blocks_from_value in agentscope_tool.py)")
    print("then extracts text blocks from `content[]` and appends `structuredContent`")
    print("as another text block. The LLM sees all of these concatenated.")
    print()

    str_tools = []
    dict_tools = []
    list_tools = []

    for label, rv, rt in test_cases:
        ct, llm_blocks = _simulate_full_flow(label, rv, rt)

        # Classify
        if rt == "str":
            str_tools.append((label, ct, llm_blocks))
        elif rt == "dict[str, Any]":
            dict_tools.append((label, ct, llm_blocks))
        else:
            list_tools.append((label, ct, llm_blocks))

    # ── Category 1: str-returning tools ──────────────────────
    print(f"\n{'─'*78}")
    print("CATEGORY 1: Tools returning `str` (8 tools — the problematic ones)")
    print(f"{'─'*78}")
    print(textwrap.dedent("""
    These tools return a bare Python string (just an ID):
      gmail_send, gmail_create_draft, drive_delete_file, docs_create,
      calendar_create_event, calendar_update_event, calendar_delete_event,
      slides_create

    Because `str` is a primitive type, the MCP SDK wraps it as
    {"result": "<the string>"} for structuredContent. The unstructured
    content is just [TextContent(text="<the string>")].
    """))

    for label, ct, blocks in str_tools:
        print(f"\n  Tool: {label}()")
        print(f"  ┌─────────────────────────────────────────────────────────────")
        print(f"  │ ACTUAL MCP JSON-RPC response (what QwenPaw receives):")
        rpc_result = json.loads(ct.model_dump_json(by_alias=True, exclude_none=True))
        print(f"  │   {json.dumps(rpc_result, indent=2).replace(chr(10), chr(10) + '  │   ')}")
        print(f"  ├─────────────────────────────────────────────────────────────")
        print(f"  │ What the LLM sees (QwenPaw _blocks_from_value output):")
        for i, b in enumerate(blocks):
            print(f"  │   block[{i}]: \"{b}\"")
        print(f"  └─────────────────────────────────────────────────────────────")

    # ── Category 2: dict-returning tools ─────────────────────
    print(f"\n{'─'*78}")
    print("CATEGORY 2: Tools returning `dict[str, Any]` (6 tools)")
    print(f"{'─'*78}")
    print(textwrap.dedent("""
    These tools return a Python dict. The SDK JSON-encodes the dict into a
    single TextContent (content[0].text) and sets structuredContent to the
    dict itself. The LLM sees the full JSON, but as a dense blob.
    """))

    for label, ct, blocks in dict_tools:
        text = blocks[0] if blocks else ""
        preview = text[:80] + "..." if len(text) > 80 else text
        print(f"\n  Tool: {label}()")
        print(f"    content[0].text preview: \"{preview}\"")
        print(f"    structuredContent: dict with {len(ct.structuredContent or {})} keys")
        print(f"    LLM sees: full JSON dump (usable but not human-readable)")

    # ── Category 3: list[dict]-returning tools ───────────────
    print(f"\n{'─'*78}")
    print("CATEGORY 3: Tools returning `list[dict]` (3 tools)")
    print(f"{'─'*78}")
    print(textwrap.dedent("""
    These tools return a list of dicts. The SDK creates one TextContent
    per list item (content[0], content[1], etc.) and wraps the list as
    {"result": [...]} in structuredContent.

    PROBLEM: If the LLM/client only reads content[0], it sees only the
    first result. The remaining results are silently lost.
    """))

    for label, ct, blocks in list_tools:
        print(f"\n  Tool: {label}()")
        print(f"    content[] has {len(ct.content)} text blocks (one per list item)")
        print(f"    structuredContent: {{\"result\": [list of {len(ct.structuredContent.get('result', []))} items]}}")
        print(f"    ⚠ If client reads content[0] only → sees 1 of {len(ct.content)} results")

    # ── Summary ──────────────────────────────────────────────
    print(f"\n{'='*78}")
    print("SUMMARY OF PROBLEMS")
    print(f"{'='*78}")
    print("""
    1. str-returning tools (8 tools):
       → content[0].text = just the bare ID string (e.g. "msg_abc123xyz")
       → structuredContent = {"result": "msg_abc123xyz"}
       → The LLM has ZERO context: what was sent? who was it sent to?
         what operation was performed? Just an ID.

    2. dict-returning tools (6 tools):
       → content[0].text = JSON-encoded dict (usable but dense)
       → structuredContent = the dict itself (correct, but redundant with content)

    3. list[dict]-returning tools (3 tools):
       → content[] has N blocks (one per item)
       → structuredContent wraps in {"result": [...]} (redundant nesting)
       → RISK: client reading content[0] only sees 1 of N results

    SPEC REFERENCE:
    MCP 2026-07-28 spec says CallToolResult should have:
      content: [{ type: "text", text: "human-readable summary" }]
      structuredContent: { ...machine-parseable fields... }
    Best practice: content should be a human-readable SUMMARY,
    structuredContent carries the structured data.
    Source: https://modelcontextprotocol.io/specification/2026-07-28/server/tools
    """)

    # ── Good example ──────────────────────────────────────────
    print(f"{'─'*78}")
    print("RECOMMENDED: What gmail_send SHOULD return")
    print(f"{'─'*78}")
    good = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "resultType": "complete",
            "content": [
                {"type": "text", "text": "Sent email to test@example.com with subject 'Test Email'. Message ID: msg_abc123xyz"}
            ],
            "structuredContent": {
                "messageId": "msg_abc123xyz",
                "status": "sent",
                "to": "test@example.com",
                "subject": "Test Email",
            },
        },
    }
    print(json.dumps(good, indent=2))


if __name__ == "__main__":
    main()
