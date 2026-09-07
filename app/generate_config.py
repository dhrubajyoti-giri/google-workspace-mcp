"""Generate MCP driver config on container startup.

Runs before uvicorn starts. Reads MCP_PORT and AUTH_TOKEN from the
environment, prints the config to stdout, and saves it as JSON
(+ YAML) to the output directory (default: /output).

This is the automated replacement for the manual ``gen-mcp-driver-config.sh``
script — when the container restarts, the config is regenerated automatically
with the current port and settings.

Usage inside container:
    python -c "from app.generate_config import generate; generate()"

Or via shell:
    python -m app.generate_config
"""
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("google-workspace-mcp.config-gen")

# Default output directory (can be overridden via OUTPUT_DIR env var)
DEFAULT_OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")


def _build_config() -> dict[str, Any]:
    """Build the MCP driver config dict from environment variables."""
    mcp_port = os.environ.get("MCP_PORT", "8000")
    mcp_host = os.environ.get("MCP_HOST", "0.0.0.0")

    config: dict[str, Any] = {
        "name": "google-workspace-mcp",
        "protocol": "mcp",
        "endpoint": {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{mcp_port}/mcp/",
            # QwenPaw resolves ${AUTH_TOKEN} from its own environment at request time.
            # Must match AUTH_TOKEN in the bridge's .env.
            "headers": {
                "Authorization": "Bearer ${AUTH_TOKEN}",
            },
        },
        "credentials": {},
        "config": {
            "display_name": "Google Workspace MCP",
            "description": (
                "Standalone MCP server — Gmail, Drive, Docs, Sheets, "
                "Calendar, Slides, Forms, Tasks, Chat"
            ),
            "tools": None,
        },
        "enabled": True,
        "policy": {
            "default_effect": "ask",
            "rules": [],
        },
    }
    return config


def generate(output_dir: str | None = None) -> str:
    """Generate MCP driver config as JSON, print it, and save to disk.

    Writes two files:
      - ``mcp-config.json``  — JSON format (for direct use)
      - ``mcp-config.yaml``  — YAML format (for QwenPaw driver directory)

    Returns the JSON string (also printed to stdout).
    """
    config = _build_config()
    output = output_dir or DEFAULT_OUTPUT_DIR
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)

    # ── Save JSON ──
    json_str = json.dumps(config, indent=2)
    json_file = output_path / "mcp-config.json"
    json_file.write_text(json_str, encoding="utf-8")
    log.info("Saved MCP driver config (JSON) to %s", json_file)

    # ── Save YAML (if PyYAML is available) ──
    try:
        import yaml
        yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
        yaml_file = output_path / "mcp-config.yaml"
        yaml_file.write_text(yaml_str, encoding="utf-8")
        log.info("Saved MCP driver config (YAML) to %s", yaml_file)
    except ImportError:
        log.warning("PyYAML not available — skipping YAML output")
        yaml_file = None

    # ── Print to stdout ──
    print("=" * 60)
    print("MCP Driver Config (auto-generated on container start)")
    print("=" * 60)
    print(json_str)
    print("=" * 60)

    return json_str


def main() -> None:
    """Entry point for ``python -m app.generate_config``."""
    generate()


if __name__ == "__main__":
    main()
