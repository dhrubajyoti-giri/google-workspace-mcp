# Google Workspace MCP

A standalone MCP server that handles its **own Google OAuth 2.0** and exposes
purpose-built Google Workspace tools to standard MCP clients via
`streamable_http` — any MCP client connects here via the MCP protocol.

## Architecture

```
 ┌──────────┐        ┌─────────────────┐      ┌─────────────────────┐
 │  User   │◄─HTTPS─│    Caddy        │ ◄─── │  google-workspace-mcp    │
 │ (browser)│  :443  │ (TLS proxy)     │  :8000│  (FastAPI + MCP)   │
 └──────────┘        └──────┬──────────┘      └──────┬──────────────┘
                            │                        │
                            │  internal Docker       │  /config (secrets)
                            │  network: caddy + mcp  │  - client_secret.json
                            │                        │  - token.json
                            │                        │
 ┌──────────┐                          │
 │  MCP     │◄─────────────────────────┤
 │  Client │                          │
 └──────────┘                          │
                                   │
                          ┌────────┴─────────┐
                          │ Google APIs      │
                          │ (oauth2, gmail,  │
                          │  drive, docs…)   │
                          └──────────────────┘
```

**Key difference from gws-mcp:** This bridge owns its OAuth flow and stores
credentials in `/config` — the MCP client never sees tokens, client secrets, or
authorization codes. The OAuth callback goes through the MCP client's domain
(`https://<hostname>/api/mcp/oauth/callback`) which Caddy routes to the
bridge.

## Prerequisites

1. Google Cloud project with APIs enabled:
   - Google Workspace API (Gmail, Drive, Docs, Sheets, Calendar, Slides)
   - OAuth consent screen configured (external or internal)
2. OAuth 2.0 Web Application credentials with redirect URI:
   `https://<your-domain>/oauth/callback`
3. Download `client_secret.json` from Google Cloud Console
4. Docker + Docker Compose
5. Network access to the `caddy` and `mcp` external Docker networks

## Installation

```bash
# 1. Clone / copy project
cd /root/workspace/projects/google-workspace-mcp

# Optional: sync with GitHub remote (origin already configured)
git remote -v  # shows: origin  https://github.com/dhrubajyoti-giri/google-workspace-mcp

# 2. Place Google OAuth client_secret.json
cp /path/to/client_secret.json secrets/client_secret.json

# 3. Configure .env (see .env.example)
cp .env.example .env
# Edit .env: set EXTERNAL_URL to your public HTTPS domain

# 4. Build and start
docker compose up -d --build

# 5. Caddy: add this domain to your Caddy config
#    caddy fmt --overwrite Caddyfile && caddy reload
#    NOTE: Caddyfile reverse_proxies to google-workspace-mcp:8000 —
#    if you changed MCP_PORT in .env, update the port in Caddyfile too
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `EXTERNAL_URL` | `https://google-api.mcp.dg.linkpc.net` | Public HTTPS URL for OAuth callback |
| `MCP_PORT` | `8000` | Host + container port (change in `.env` to relocate) |
| `GOOGLE_CLIENT_SECRET_FILE` | `/config/client_secret.json` | Mount path for OAuth credentials |
| `GOOGLE_TOKEN_FILE` | `/config/token.json` | Auto-generated refresh token |
| `GOOGLE_SCOPES` | *(read-only default)* | Comma-separated Google API scopes |

## OAuth Flow

```
1. User (or MCP client) → GET /oauth/start
2. Bridge → redirects to Google consent screen
3. Google → redirects to /oauth/callback?code=...&state=...
4. Bridge → exchanges code for token (incl. refresh_token)
5. Bridge → saves token.json to /config (0600 permissions)
6. Bridge → auto-refreshes access tokens as needed
```

**First run:** Visit `https://<your-domain>/oauth/start` to authorize.

## MCP Transport

- **Type:** Streamable HTTP (MCP protocol v1)
- **Endpoint:** `https://<your-domain>/mcp` (MCP clients connect here; redirect from `/mcp` → `/mcp/` handled automatically)
- **MCP client config:** `drivers/mcp/google-workspace-mcp.yaml` → install into QwenPaw runtime's `drivers/mcp/`

## Tools

### Read (Phase 3)

| Tool | Description |
|---|---|
| `gmail_search(query, max_results)` | Search Gmail with query syntax |
| `gmail_get_message(message_id)` | Get full message (headers, body, snippet) |
| `gmail_send(to, subject, body, body_format)` | Send email |
| `drive_search(query, max_results)` | Search Drive files |
| `drive_get_file(file_id)` | Get file metadata + content (exported if GWorkspace) |
| `docs_get(document_id)` | Get Doc text, headings, tables, bullets |
| `sheets_get(spreadsheet_id, range)` | Get Sheets values + sheet names |
| `calendar_list_events(...)` | List calendar events |
| `calendar_get_event(...)` | Get single event details |
| `slides_get(presentation_id)` | Get Slides structure + text |

### Write (Phase 5)

| Tool | Description |
|---|---|
| `gmail_create_draft(to, subject, body, body_format)` | Create a Gmail draft |
| `drive_upload_file(name, content_base64, mime_type, parent_folder_id)` | Upload file to Drive |
| `drive_create_file(name, content, mime_type, parent_folder_id)` | Create text file in Drive |
| `drive_delete_file(file_id)` | Delete a Drive file |
| `docs_create(title, content)` | Create a new Google Doc |
| `docs_update(document_id, text, location_index)` | Insert text into a Doc |
| `sheets_update(spreadsheet_id, range, values)` | Update cell values in a Sheet |
| `sheets_append(spreadsheet_id, range, values)` | Append rows to a Sheet |
| `calendar_create_event(...)` | Create a calendar event |
| `calendar_update_event(...)` | Update a calendar event |
| `calendar_delete_event(calendar_id, event_id)` | Delete a calendar event |
| `slides_create(title)` | Create a new presentation |
| `slides_update(presentation_id, requests_json)` | Batch update slides

## Connecting MCP Clients

The bridge is a standalone MCP server — a standard MCP client connects via a
driver YAML. Since MCP clients may **not** interpolate `${ENV_VAR}` in the
`endpoint.url` field (only in headers), the URL must be written
literally into the driver config.

> **Auto-generation on container start:** When the container boots, it
> automatically generates the MCP driver config (JSON + YAML) to the
> `OUTPUT_DIR` (`/output` by default, or `${OUTPUT_PATH}` volume mount),
> and prints it to **stdout**. You can grab it from container logs or the
> mounted volume — no separate script run needed.

### Option A — Automated (recommended)

Use the config generation script to produce the driver YAML from `.env`:

```bash
# In the bridge project directory
./scripts/gen-mcp-driver-config.sh
# → reads MCP_PORT + AUTH_TOKEN from .env
# → prints the config to console
# → saves to drivers/mcp/google-workspace-mcp-generated.yaml
#   (NOT auto-installed — you do it manually)
```

Then manually copy the generated config to your MCP client and restart:

```bash
cp drivers/mcp/google-workspace-mcp-generated.yaml \
   /app/working/workspaces/default/drivers/mcp/google-workspace-mcp.yaml
qwenpaw daemon restart
```

### Option B — Manual

1. Set `AUTH_TOKEN` in the bridge `.env` (e.g. `AUTH_TOKEN=your-secret-here`)
2. Set the **same** `AUTH_TOKEN` as an environment variable in QwenPaw's
   runtime (same value as in the bridge `.env`)
3. Copy `drivers/mcp/google-workspace-mcp.yaml` to your MCP client's
   `data/drivers/mcp/`
4. Replace `__MCP_PORT__` in the YAML with the port from your `.env`
   (default `8000`) — e.g. `http://127.0.0.1:8000/mcp/`
5. Restart your MCP client

### MCP Client Auth Model

The bridge protects `/mcp/*` with a **bearer-token** middleware.
The MCP client must send `Authorization: Bearer <token>` on every MCP request.
The token must be **the same value** set as `AUTH_TOKEN` in both the
bridge's `.env` (docker-compose) and QwenPaw's environment.

| Layer | What the MCP client sees |
|---|---|
| Endpoint | `http://127.0.0.1:<MCP_PORT>/mcp/` (localhost only) |
| Auth header | `Authorization: Bearer ${AUTH_TOKEN}` (QwenPaw env interpolation) |
| Google OAuth | Fully opaque — the bridge exchanges codes/tokens internally; the MCP client never sees Google credentials |

## Testing

```bash
# Unit tests
pip install -r requirements.txt pytest
pytest tests/ -v

# Health check
curl http://localhost:${MCP_PORT:-8000}/healthz

# OAuth status (check if token is stored)
curl http://localhost:${MCP_PORT:-8000}/oauth/status

# MCP protocol test (requires EXTERNAL_URL host header for transport security)
curl -H "Host: $(grep EXTERNAL_URL .env | cut -d= -f2-)" \
     -H "Accept: application/json, text/event-stream" \
     -X POST http://localhost:${MCP_PORT:-8000}/mcp/ \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"}}}'
```

## Troubleshooting

| Issue | Fix |
|---|---|
| `OAuth credentials lack required scopes` | Re-authorize: delete `/config/token.json`, visit `/oauth/start` |
| `client_secret.json not found` | Place it in `secrets/client_secret.json` before starting |
| Check the driver config endpoint URL and bearer token | Check `drivers/mcp/google-workspace-mcp.yaml` endpoint URL |
| Caddy 502 Bad Gateway | Verify container is running: `docker compose ps` |

## Phases

- [x] **Phase 1** — Infrastructure (Docker, FastAPI, healthz, config)
- [x] **Phase 2** — Auth (OAuth 2.0 flow, token store, auto-refresh)
- [x] **Phase 3** — Read tools (Gmail, Drive, Docs, Sheets, Calendar, Slides)
- [x] **Phase 4** — MCP driver config + Caddy routing
- [x] **Phase 5** — Write tools (create, update, delete)
- [~] **Phase 6** — Additional APIs (Tasks, Contacts, Chat) — scopes configured in `.env`; tool implementations pending
- [~] **Phase 7** — E2E testing — 30 unit tests pass; full OAuth + Docker + client integration pending host deployment
