#!/usr/bin/env bash
# install-mcp-driver.sh — Generate QwenPaw MCP driver config from .env
#
# Reads MCP_PORT + AUTH_TOKEN from the bridge .env file,
# substitutes MCP_PORT into the driver YAML template,
# and writes the result to the QwenPaw runtime's drivers/mcp/ directory.
#
# The AUTH_TOKEN header uses ${AUTH_TOKEN} interpolation — QwenPaw
# resolves it from its own environment at runtime, so the token
# never appears in the YAML file on disk.
#
# Usage:
#   ./scripts/install-mcp-driver.sh                    # use ./../.env
#   ./scripts/install-mcp-driver.sh /path/to/.env       # use custom .env
#   ./scripts/install-mcp-driver.sh /path/to/.env /path/to/qwenpaw/data/drivers/mcp

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

ENV_FILE="${1:-$PROJECT_DIR/.env}"
DRIVER_OUTPUT_DIR="${2:-/app/working/workspaces/default/drivers/mcp}"

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

# ── Generate driver config from template ──
mkdir -p "$DRIVER_OUTPUT_DIR"

TEMPLATE="$PROJECT_DIR/drivers/mcp/google-api-bridge.yaml"
OUTPUT="$DRIVER_OUTPUT_DIR/google-api-bridge.yaml"

if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: Template not found at $TEMPLATE"
    exit 1
fi

# Substitute __MCP_PORT__ placeholder with the actual port.
# ${AUTH_TOKEN} is left as-is for QwenPaw to interpolate at runtime.
sed "s|__MCP_PORT__|$MCP_PORT|g" "$TEMPLATE" > "$OUTPUT"
chmod 600 "$OUTPUT"

if [ -n "$AUTH_TOKEN" ]; then
    echo "✓ AUTH_TOKEN found — QwenPaw will interpolate it at runtime"
else
    echo "⚠  AUTH_TOKEN not found in .env — /mcp will reject all requests until set"
fi
echo "✓ Driver config written to $OUTPUT (port=$MCP_PORT, url=http://127.0.0.1:$MCP_PORT/mcp/)"
echo ""
echo "Next steps:"
echo "  1. Ensure AUTH_TOKEN is set in QwenPaw's environment"
echo "  2. Restart QwenPaw:  qwenpaw daemon reload-config  (or full restart for tool discovery)"
echo "  3. If new to OAuth:  https://${EXTERNAL_URL}/oauth/start"
