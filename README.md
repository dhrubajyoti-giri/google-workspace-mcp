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
                            │                        │  - token-{user}.json (per-user)
                            │                        │
                            │                        │
 ┌──────────┐      :8088   │                        │
 │  MCP     │◄─────────────┤                        │
 │ Client  │   Bearer token │                        │
 │  (QwenPaw│   on every   │                        │
 │   :8088) │   request    │                        │
 └──────────┘             └────────┬───────────────┘
                                   │
                          ┌────────┴─────────┐
                          │ Google APIs      │
                          │ (oauth2, gmail,  │
                          │  drive, docs…)   │
                          └──────────────────┘
```

**Key difference from gws-mcp:** This bridge owns its OAuth flow and stores
credentials in `/config` — the MCP client never sees tokens, client secrets, or
authorization codes. The OAuth callback goes to the bridge's own domain
(`https://<hostname>/oauth/callback`).

### Security Layers

1. **Bearer token** — Protects all endpoints except `/healthz`, `/`,
   `/oauth/callback`. Each MCP client knows a bearer token that maps to a
   specific user.
2. **Caddy reverse proxy** — HTTPS termination + network isolation.
3. **Google OAuth consent** — Scoped to the GCP project's authorized APIs.

---

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
| `MCP_PORT` | `8000` | Host + container port |
| `GOOGLE_CLIENT_SECRET_FILE` | `/config/client_secret.json` | Mount path for OAuth credentials |
| `GOOGLE_TOKEN_FILE` | `/config/token.json` | Token file path (base; per-user suffix appended in multi-user mode) |
| `GOOGLE_SCOPES` | *(read-only default)* | Comma-separated Google API scopes (global default) |
| `AUTH_TOKEN` | *(empty)* | Single-user bearer token (used when `MCP_USER_MAP` is unset) |
| `MCP_USER_MAP` | *(empty)* | JSON: bearer token → `{"user_id","scopes"}`. Enables multi-user mode. |

---

## Authentication Model

### Single-User Mode (default)

Set `AUTH_TOKEN` in `.env`. All MCP requests must include
`Authorization: Bearer <AUTH_TOKEN>`. One Google token file at
`/config/token.json`.

### Multi-User Mode

Set `MCP_USER_MAP` in `.env` (overrides `AUTH_TOKEN`). Each entry maps a
bearer token to a user with their own Google credentials and scopes:

```json
{
  "token-full-access":   {"user_id": "alice", "scopes": ["gmail.modify","drive","documents","spreadsheets","calendar","presentations"]},
  "token-restricted":    {"user_id": "bob",   "scopes": ["documents.readonly"]}
}
```

Per-user behavior:
- Each user has a **separate Google OAuth flow** (`/oauth/start` with their bearer token)
- Tokens stored at `/config/token-{user_id}.json` (e.g. `token-alice.json`, `token-bob.json`)
- Each MCP client uses **their own bearer token** → routes to their own Google credentials
- Users can have **different scopes** (full access vs restricted)

**OAuth flow per user:**

```
1. User visits /oauth/start with their bearer token
2. Bridge identifies the user from the token → uses their configured scopes
3. Google OAuth → callback → token stored to token-{user_id}.json
4. User's MCP client calls /mcp/ with their bearer token → tools execute
   as that Google user with that user's scopes
```

### Bearer Token Middleware

All endpoints require a valid bearer token **except**:

| Endpoint | Public? | Why |
|---|---|---|
| `/healthz` | ✅ | Monitoring / Docker healthcheck |
| `/` | ✅ | Service info / discovery |
| `/oauth/callback` | ✅ | Google redirects here — no token available yet |
| `/oauth/start` | ❌ | Needs token to identify the user + select scopes |
| `/oauth/status` | ❌ | Shows current user's token status |
| `/oauth/revoke` | ❌ | Deletes current user's token |
| `/mcp/*` | ❌ | MCP protocol — tools execute as the authenticated user |

---

## OAuth Flow

### Single-User Mode

```
1. (Bearer token required) → GET /oauth/start
2. Bridge → redirects to Google consent screen
3. Google → redirects to /oauth/callback?code=...&state=...
4. Bridge → exchanges code for token (incl. refresh_token)
5. Bridge → saves /config/token.json (0600 permissions)
6. Bridge → auto-refreshes access tokens as needed
```

**First run:** Send a request with your bearer token:
```bash
curl -H "Authorization: Bearer your-secret-token" http://localhost:8000/oauth/start
# → follow redirect to Google, then /oauth/callback
```

### Multi-User Mode

```
1. User A: GET /oauth/start with Bearer token-alice
2. Bridge → identifies user "alice", requests alice's scopes → Google consent
3. Google → redirects to /oauth/callback (public, no token needed)
4. Bridge → looks up user_id from OAuth state → stores token to token-alice.json
5. User A's MCP client → POST /mcp/ with Bearer token-alice → tools run as alice
```

### Clearing Authorization

To revoke a user's Google token (no manual file deletion needed):

```bash
# Multi-user
curl -X POST -H "Authorization: Bearer token-alice" http://localhost:8000/oauth/revoke

# Single-user
curl -X POST -H "Authorization: Bearer your-secret-token" http://localhost:8000/oauth/revoke
```

Returns: `{"status":"success","user_id":"alice","message":"Token for user alice revoked. Re-authorize at /oauth/start."}`

---

## MCP Transport

- **Type:** Streamable HTTP (MCP protocol v1)
- **Endpoint:** `https://<your-domain>/mcp` (MCP clients connect here; redirect from `/mcp` → `/mcp/` handled automatically)
- **Bearer token:** Required on all `/mcp/*` requests

### Config Auto-Generation

When the container starts, it **automatically** generates the MCP driver
config (JSON + YAML) to `/output` and prints it to **stdout**:

```bash
# View from container logs
docker compose logs google-workspace-mcp | head -20

# Grab from mounted volume
cat /tmp/gws-output/mcp-config.json        # single-user
cat /tmp/gws-output/mcp-config-alice.json   # multi-user (per-user)
```

---

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
| `slides_update(presentation_id, requests_json)` | Batch update slides |

---

## Connecting MCP Clients

The bridge is a standalone MCP server — a standard MCP client connects via a
driver config. Since MCP clients may **not** interpolate `${ENV_VAR}` in the
`endpoint.url` field (only in headers), the URL is written literally into the
config.

> **Auto-generation on container start:** When the container boots, it
> automatically generates the MCP driver config (JSON + YAML) to the
> `OUTPUT_DIR` (`/output`, or `${OUTPUT_PATH}` volume mount) and prints it to
> **stdout**. No separate script run needed.

### Single-User: Install the Config

```bash
# From the output volume or stdout
cp /tmp/gws-output/mcp-config.json \
   /app/working/workspaces/default/drivers/mcp/google-workspace-mcp.yaml
qwenpaw daemon restart
```

### Multi-User: Install One Config Per User

```bash
# Copy each user's config to QwenPaw
cp /tmp/gws-output/mcp-config-alice.yaml \
   /app/working/workspaces/default/drivers/mcp/google-workspace-mcp-alice.yaml
cp /tmp/gws-output/mcp-config-bob.yaml \
   /app/working/workspaces/default/drivers/mcp/google-workspace-mcp-bob.yaml
qwenpaw daemon restart
```

Each config uses a different bearer token — the bridge routes tool calls to the
correct Google account based on which token is presented.

---

## Testing

```bash
# Unit tests
pip install -r requirements.txt pytest
pytest tests/ -v

# Health check (public endpoint)
curl http://localhost:${MCP_PORT:-8000}/healthz

# OAuth status (requires bearer token)
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:${MCP_PORT:-8000}/oauth/status

# Revoke token (requires bearer token)
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" http://localhost:${MCP_PORT:-8000}/oauth/revoke

# MCP protocol test
curl -H "Host: $(grep EXTERNAL_URL .env | cut -d= -f2-)" \
     -H "Accept: application/json, text/event-stream" \
     -H "Authorization: Bearer YOUR_TOKEN" \
     -X POST http://localhost:${MCP_PORT:-8000}/mcp/ \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0.0"}}}'
```

## Troubleshooting

| Issue | Fix |
|---|---|
| `OAuth credentials lack required scopes` | Re-authorize: `curl -X POST -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8000/oauth/revoke`, then `/oauth/start` |
| `client_secret.json not found` | Place it in `secrets/client_secret.json` before starting |
| `401 Unauthorized` on `/mcp` | Check bearer token matches `AUTH_TOKEN` (single-user) or a key in `MCP_USER_MAP` (multi-user) |
| Token file location | Single-user: `/config/token.json` · Multi-user: `/config/token-{user_id}.json` |
| Caddy 502 Bad Gateway | Verify container is running: `docker compose ps` |

## Phases

- [x] **Phase 1** — Infrastructure (Docker, FastAPI, healthz, config)
- [x] **Phase 2** — Auth (OAuth 2.0 flow, token store, auto-refresh, `/oauth/revoke`)
- [x] **Phase 3** — Read tools (Gmail, Drive, Docs, Sheets, Calendar, Slides)
- [x] **Phase 4** — MCP driver config + Caddy routing + bearer-token middleware
- [x] **Phase 5** — Write tools (create, update, delete)
- [x] **Phase 6** — Multi-user support (per-user tokens, scopes, bearer token routing)
- [~] **Phase 7** — E2E testing — 30 unit tests pass; full OAuth + Docker + client integration pending host deployment
