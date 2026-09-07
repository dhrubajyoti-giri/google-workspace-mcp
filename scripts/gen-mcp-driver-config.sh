#!/usr/bin/env bash
# gen-mcp-driver-config.sh — Generate QwenPaw MCP driver config from .env
#
# Reads MCP_PORT + AUTH_TOKEN from the bridge .env file, substitutes
# MCP_PORT into the driver YAML template, prints the result to stdout,
# and saves it to a local YAML file.
#
# The AUTH_TOKEN header uses ${AUTH_TOKEN} interpolation — QwenPaw
# resolves it from its own environment at runtime, so the token
# never appears in the YAML file on disk.
#
# This script does NOT write to QwenPaw's runtime directory.
# The user manually installs the generated config into QwenPaw.
#
# Usage:
#   ./scripts/gen-mcp-driver-config.sh                    # use ./../.env
#   ./scripts/gen-mcp-driver-config.sh /path/to/.env      # use custom .env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

ENV_FILE="${1:-$PROJECT_DIR/.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env file not found at $ENV_FILE"
    echo "  cp $PROJECT_DIR/.env.example $PROJECT_DIR/.env"
    echo "  # edit .env: set EXTERNAL_URL, AUTH_TOKEN, MCP_PORT"
    exit 1
fi

# ── Read MCP_PORT from .env (default 8000) ──
MCP_PORT=$(grep -E '^MCP_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
MCP_PORT="${MCP_PORT:-8000}"

# ── Read AUTH_TOKEN from .env (optional but recommended for security) ──
AUTH_TOKEN=$(grep -E '^AUTH_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")

# ── Read EXTERNAL_URL from .env (for OAuth instructions in output) ──
EXTERNAL_URL=$(grep -E '^EXTERNAL_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")

TEMPLATE="$PROJECT_DIR/drivers/mcp/google-api-bridge.yaml"
OUTPUT="$PROJECT_DIR/drivers/mcp/google-api-bridge-generated.yaml"

if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: Template not found at $TEMPLATE"
    exit 1
fi

# Substitute __MCP_PORT__ placeholder with the actual port.
# ${AUTH_TOKEN} is left as-is for QwenPaw to interpolate at runtime.
sed "s|__MCP_PORT__|$MCP_PORT|g" "$TEMPLATE" > "$OUTPUT"

# ── Print the generated config to console ──
echo "=== Generated QwenPaw MCP driver config (port=$MCP_PORT) ==="
cat "$OUTPUT"
echo "=== End of config ==="
echo ""

if [ -n "$AUTH_TOKEN" ]; then
    echo "✓ AUTH_TOKEN found — QwenPaw will interpolate it at runtime"
else
    echo "⚠  AUTH_TOKEN not found in .env — /mcp will reject all requests until set"
fi
echo "✓ Config saved to: $OUTPUT"
echo ""
echo "Next steps (MANUAL):"
echo "  1. Set AUTH_TOKEN in QwenPaw's environment (same value as in .env)"
echo "  2. Copy $OUTPUT to QwenPaw's drivers/mcp/ directory"
echo "  3. Restart QwenPaw:  qwenpaw daemon reload-config  (or full restart for tool discovery)"
echo "  4. If new to OAuth:  https://${EXTERNAL_URL}/oauth/start"
