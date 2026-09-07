"""Generate MCP driver config on container startup.

Runs before uvicorn starts. Reads MCP_PORT and AUTH_TOKEN / MCP_USER_MAP
from the environment, prints the config(s) to stdout, and saves them as
JSON (+ YAML) to the output directory (default: /output).

Single-user mode (AUTH_TOKEN set, no MCP_USER_MAP):
  → mcp-config.json, mcp-config.yaml (one config)

Multi-user mode (MCP_USER_MAP set):
  → mcp-config-{user_id}.json, mcp-config-{user_id}.yaml (one per user)
  → Each uses the user's specific bearer token from MCP_USER_MAP

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
from pathlib import Path
from typing import Any

log = logging.getLogger("google-workspace-mcp.config-gen")

# Default output directory (can be overridden via OUTPUT_DIR env var)
DEFAULT_OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")


def _parse_user_map() -> dict[str, dict[str, Any]]:
    """Parse MCP_USER_MAP env var into {token: {"user_id": str}}."""
    raw = os.environ.get("MCP_USER_MAP", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        result: dict[str, dict[str, Any]] = {}
        for token, info in parsed.items():
            if isinstance(info, str):
                result[token] = {"user_id": info}
            elif isinstance(info, dict):
                result[token] = {"user_id": info.get("user_id", "")}
            else:
                raise ValueError(f"Invalid MCP_USER_MAP entry: {token}")
        return result
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse MCP_USER_MAP: %s", e)
        return {}


def _build_config(bearer_token: str, user_id: str | None = None) -> dict[str, Any]:
    """Build an MCP driver config dict with the given bearer token."""
    mcp_port = os.environ.get("MCP_PORT", "8000")
    display_name = user_id or "google-workspace-mcp"

    config: dict[str, Any] = {
        "name": display_name,
        "protocol": "mcp",
        "endpoint": {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{mcp_port}/mcp/",
            "headers": {
                "Authorization": f"Bearer {bearer_token}",
            },
        },
        "credentials": {},
        "config": {
            "display_name": f"Google Workspace MCP" + (f" ({user_id})" if user_id else ""),
            "description": "Standalone MCP server — Gmail, Drive, Docs, Sheets, Calendar, Slides",
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
    """Generate MCP driver config(s), print to stdout, and save to disk.

    In single-user mode: generates one config (mcp-config.{json|yaml}).
    In multi-user mode: generates one config per user (mcp-config-{user_id}.{json|yaml}).

    Returns a summary string.
    """
    output = output_dir or DEFAULT_OUTPUT_DIR
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)

    user_map = _parse_user_map()
    generated: list[tuple[str, str]] = []  # (user_id_or_default, json_str)

    # ── Build configs ──
    if user_map:
        # Multi-user: one config per user
        log.info("Multi-user mode: %d users configured", len(user_map))
        for token, info in user_map.items():
            uid = info["user_id"]
            config = _build_config(bearer_token=token, user_id=uid)
            generated.append((uid, json.dumps(config, indent=2)))
    else:
        # Single-user: one config with AUTH_TOKEN
        auth_token = os.environ.get("AUTH_TOKEN", "")
        if not auth_token:
            log.warning("Neither MCP_USER_MAP nor AUTH_TOKEN set — generating config with empty bearer token")
        config = _build_config(bearer_token=auth_token)
        generated.append(("default", json.dumps(config, indent=2)))

    # ── Save to disk ──
    all_saved: list[str] = []
    for name, json_str in generated:
        suffix = "" if name == "default" else f"-{name}"

        # JSON
        json_file = output_path / f"mcp-config{suffix}.json"
        json_file.write_text(json_str, encoding="utf-8")
        all_saved.append(str(json_file))
        log.info("Saved MCP driver config (JSON) for %s to %s", name or "default", json_file)

        # YAML (if PyYAML available)
        config_obj = json.loads(json_str)
        try:
            import yaml
            yaml_str = yaml.dump(config_obj, default_flow_style=False, sort_keys=False)
            yaml_file = output_path / f"mcp-config{suffix}.yaml"
            yaml_file.write_text(yaml_str, encoding="utf-8")
            all_saved.append(str(yaml_file))
            log.info("Saved MCP driver config (YAML) for %s to %s", name or "default", yaml_file)
        except ImportError:
            log.warning("PyYAML not available — skipping YAML output")

    # ── Print to stdout ──
    print("=" * 60)
    if user_map:
        print(f"MCP Driver Configs (auto-generated — {len(user_map)} users)")
    else:
        print("MCP Driver Config (auto-generated on container start)")
    print("=" * 60)
    for name, json_str in generated:
        print(f"\n--- {name} ---")
        print(json_str)
    print("\n" + "=" * 60)

    return f"Generated {len(generated)} config(s) to {output_path}"


def main() -> None:
    """Entry point for ``python -m app.generate_config``."""
    generate()


if __name__ == "__main__":
    main()
