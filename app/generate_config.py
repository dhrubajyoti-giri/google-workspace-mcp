"""Generate MCP driver config (optional utility — not called on startup).

MCP clients (QwenPaw, Claude Desktop, Claude Code, etc.) auto-discover
OAuth endpoints when they first connect to /mcp/ and receive a 401.
No manual config generation is required.

This module is kept for clients that need an explicit config file.
It generates a minimal driver config with NO bearer token — the client
handles OAuth discovery and token management automatically.

Usage (optional, not run on container startup):
    python -c "from app.generate_config import generate; generate()"
"""
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("google-workspace-mcp.config-gen")

DEFAULT_OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/secrets")


def _build_config() -> dict[str, Any]:
    """Build a minimal MCP driver config WITHOUT a bearer token.

    The MCP client discovers OAuth endpoints at runtime:
    1. POST /mcp/ → 401 with WWW-Authenticate (resource_metadata URL)
    2. GET /.well-known/oauth-protected-resource/mcp → auth server URL
    3. GET /.well-known/oauth-authorization-server → endpoints
    4. POST /register → dynamic client registration → client_id
    5. GET /authorize → user authorizes via Google (scope selector)
    6. POST /token → receives MCP access_token + refresh_token
    7. POST /mcp/ with Bearer token → tools work

    No bearer token is embedded in the config — the client handles the
    entire OAuth flow and token refresh automatically.
    """
    mcp_port = os.environ.get("MCP_PORT", "8000")
    return {
        "name": "google-workspace-mcp",
        "protocol": "mcp",
        "endpoint": {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{mcp_port}/mcp/",
        },
        "credentials": {},
        "config": {
            "display_name": "Google Workspace MCP",
            "description": "Gmail, Drive, Docs, Sheets, Calendar, Slides",
            "tools": None,
        },
        "enabled": True,
        "policy": {
            "default_effect": "ask",
            "rules": [],
        },
    }


def generate(output_dir: str | None = None) -> str:
    """Generate MCP driver config, print to stdout, and save to disk.

    MCP OAuth mode only — no bearer token embedded. The MCP client
    discovers OAuth endpoints at runtime and manages tokens automatically.
    """
    output = output_dir or DEFAULT_OUTPUT_DIR
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)

    config = _build_config()
    json_str = json.dumps(config, indent=2)

    # Save JSON
    json_file = output_path / "mcp-config.json"
    json_file.write_text(json_str, encoding="utf-8")
    log.info("Saved MCP driver config to %s", json_file)

    # Save YAML (if PyYAML available)
    saved_files = [str(json_file)]
    try:
        import yaml
        yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
        yaml_file = output_path / "mcp-config.yaml"
        yaml_file.write_text(yaml_str, encoding="utf-8")
        saved_files.append(str(yaml_file))
        log.info("Saved YAML config to %s", yaml_file)
    except ImportError:
        log.warning("PyYAML not available — skipping YAML output")

    # Print to stdout
    print("=" * 60)
    print("MCP Driver Config (MCP OAuth mode — no token embedded)")
    print("=" * 60)
    print(json_str)
    print("\n" + "=" * 60)
    print("Note: The MCP client auto-discovers OAuth endpoints at runtime.")
    print("      No bearer token needed — complete Google OAuth in-browser.")
    print()

    return f"Generated config to {output_path}"


def main() -> None:
    """Entry point for ``python -m app.generate_config``."""
    generate()


if __name__ == "__main__":
    main()
