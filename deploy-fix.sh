#!/bin/bash
# deploy-fix.sh — Deploy MCPAuthMiddleware fix + scope chooser changes
#
# Resolves: 503 "MCP client 'gws-mcp' is saved but not active yet (status=inactive)"
# Root cause: RequireAuthMiddleware blocked server/discover (pre-auth MCP step) with 401
# Fix: MCPAuthMiddleware allows server/discover without bearer token

MCP_DIR="/root/workspace/projects/google-workspace-mcp"
QWENPAW_TOKEN="12ed652d327c23468d1cd2c49567b2d3"
QWENPAW_API="http://localhost:8088"

echo "=== Step 1: Commit scope chooser changes ==="
cd "$MCP_DIR"
git add -A
git commit -m "scope: group read-only/read+write sections with individual checkboxes" 2>&1 || echo "(already committed)"
git log --oneline -3

echo ""
echo "=== Step 2: Restart MCP server with latest code ==="
echo "Kill old instance (PID 7244 = old code without MCPAuthMiddleware):"
kill 7244 2>/dev/null || echo "  process already stopped"
sleep 2
echo "Start: python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &"

echo ""
echo "=== Step 3: Verify server/discover returns 200 (no auth) ==="
echo 'curl -X POST http://127.0.0.1:8000/mcp/ -H "mcp-method: server/discover" -H "mcp-protocol-version: 2026-07-28" -H "Content-Type: application/json" -d '\''{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}'\'''

echo ""
echo "=== Step 4: Toggle MCP driver off/on (on QwenPaw host) ==="
echo "PATCH $QWENPAW_API/api/mcp/toggle/gws-msp with x-qwenpaw-runtime-token header"
echo "  Then toggle ON, wait 5s, check: GET $QWENPAW_API/api/mcp/tools/gws-msp"

echo ""
echo "=== Step 5: Check DNS ==="
echo "nslookup gws.mcp.dg.linkpic.net  (must resolve for remote driver)"
