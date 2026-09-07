# Google Workspace MCP

A standalone MCP server exposing Google Workspace tools (Gmail, Drive, Docs, Sheets, Calendar, Slides, Tasks) via **MCP OAuth 2.0** — no static bearer tokens, no per-user config.

## How It Works

```
MCP Client (QwenPaw / Claude Desktop / Claude Code)
  │
  │ POST /mcp/ (no token)
  ├── 401 with WWW-Authenticate: Bearer resource_metadata="https://.../oauth-protected-resource/mcp"
  │
  │ GET /.well-known/oauth-protected-resource/mcp   → discovers auth server URL
  │ GET /.well-known/oauth-authorization-server       → discovers endpoints (/authorize, /token, /register, /revoke)
  │ POST /register                                  → gets client_id
  │ GET /authorize                                  → browser: Google login + scope selection
  │ POST /token                                     → receives MCP JWT (access_token + refresh_token)
  │
  │ POST /mcp/  (Authorization: Bearer <JWT>)
  │
  │ JWT "sub" claim = Google email → GoogleClient looks up credentials in registry.json → calls Google API
```

- **Token format**: JWT signed with `MCP_JWT_SECRET`. `sub` = Google email.
- **Token store**: `registry.json` (per-user Google refresh tokens + granted scopes)
- **OAuth flow**: Two-step — identify (openid/email) → scope selection UI → authorize
- **Token refresh**: MCP access tokens expire in 8h; clients auto-refresh using the 30d refresh token
- **Scope changes**: Re-authorize via the client's "re-connect" button (existing scopes pre-selected)

## Prerequisites

- Docker + Docker Compose
- A publicly reachable HTTPS URL (`EXTERNAL_URL`) behind a TLS proxy (Caddy, nginx, Cloudflare Tunnel)
- Google Cloud OAuth 2.0 credentials (`client_secret.json`) — [instructions](#getting-google-oauth-credentials)

## Quick Start

### 1. Configure

```bash
cp .env.example .env

# Edit .env:
#   EXTERNAL_URL=https://mcp.yourdomain.com      # your public HTTPS domain
#   MCP_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
#   SECRETS_VOLUME_PATH=/absolute/path/to/secrets  # host dir for OAuth files
```

### 2. Add Google credentials

```bash
cp your-downloaded-client_secret.json /absolute/path/to/secrets/client_secret.json
```

### 3. Start

```bash
docker compose up -d --build
```

### 4. Verify

```bash
curl http://localhost:8000/healthz
# Expected: {"status":"ok",...}  (may show "degraded" until first user authorizes)
```

### 5. Add to your MCP client (no bearer token needed)

The MCP client auto-discovers OAuth on first connect — **no token in the config**.

**QwenPaw** — place this JSON as a new driver config:

```json
{
  "key": "google-workspace-mcp",
  "name": "Google Workspace MCP",
  "description": "Gmail, Drive, Docs, Sheets, Calendar, Slides",
  "enabled": true,
  "transport": "streamable_http",
  "url": "http://127.0.0.1:8000/mcp/"
}
```

> QwenPaw fields `oauth_status`, `access_summary`, `tools`, and `headers` are auto-populated after the first connection. Set only `key`, `name`, `transport`, and `url`.

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "google-workspace-mcp": {
      "url": "http://127.0.0.1:8000/mcp/"
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add google-workspace-mcp --transport http --url http://127.0.0.1:8000/mcp/
```

### 6. Authorize

On first MCP tool call, your client will:
1. Auto-discover OAuth endpoints (via the 401 → `WWW-Authenticate` → `/.well-known/...`)
2. Register a dynamic client
3. Open a browser to Google login + scope selection
4. Store the MCP token internally

After authorization, `oauth_status` in the client shows `"connected"` and tools are available.

## Getting Google OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use existing)
3. Enable APIs: Gmail API, Drive API, Docs API, Sheets API, Calendar API, Slides API
4. Go to APIs & Services → Credentials → Create Credentials → OAuth client ID
5. Application type: **Web application**
6. Name: `google-workspace-mcp`
7. **Authorized redirect URIs**: Add `https://mcp.yourdomain.com/oauth/callback`
8. Download the JSON → save as `secrets/client_secret.json`
9. Set `EXTERNAL_URL` in `.env` to match your domain (for OAuth callback + discovery)

## Scope Selection

The `/authorize` flow shows a scope selector with all Google API scopes. Existing grants are shown pre-selected on re-authorization.

| Service | Read scopes | Write scopes |
|---|---|---|
| Gmail | `gmail.readonly` | `gmail.modify`, `gmail.send`, `gmail.compose`, `gmail.metadata`, `gmail.settings.basic`, `gmail.labels`, `gmail.insert` |
| Drive | `drive.readonly` | `drive`, `drive.file` |
| Docs | `documents.readonly` | `documents` |
| Sheets | `spreadsheets.readonly` | `spreadsheets` |
| Calendar | `calendar.readonly` | `calendar` |
| Slides | `presentations.readonly` | `presentations` |
| Other | `contacts.readonly`, `tasks.readonly` | `contacts`, `tasks`, `chat.bot`, `chat.messages`, `chat.spaces` |

Plus always included: `openid`, `userinfo.email`, `userinfo.profile`

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `EXTERNAL_URL` | `https://mcp.example.com` | Public HTTPS URL (OAuth callbacks + metadata discovery) |
| `MCP_JWT_SECRET` | `change-me-in-production` | Secret for signing MCP JWT access/refresh tokens |
| `MCP_PORT` | `8000` | Container listen port |
| `GOOGLE_CLIENT_SECRET_FILE` | `/secrets/client_secret.json` | Path to Google OAuth credentials (mounted) |
| `GOOGLE_REGISTRY_FILE` | `/secrets/registry.json` | Per-user token registry (auto-populated) |
| `GOOGLE_SCOPES` | *(see .env)* | Default Google API scopes if MCP client doesn't request any |
| `MCP_ACCESS_TOKEN_TTL` | `28800` | MCP access token lifetime in seconds (8h) — client auto-refreshes |
| `MCP_REFRESH_TOKEN_TTL` | `2592000` | MCP refresh token lifetime in seconds (30d) |
| `MCP_AUTH_CODE_TTL` | `600` | MCP authorization code lifetime in seconds (10 min) |
| `SCOPE_SELECTOR_MODE` | `all` | `all` (default — show every available scope in selector) · `requested` (show only MCP-client-requested scopes) |
| `SECRETS_VOLUME_PATH` | `/path/to/secrets` | Host directory for `client_secret.json` + `registry.json` |
| `TZ` | `Asia/Kolkata` | Timezone |
| `LOG_LEVEL` | `INFO` | Log verbosity |

## Token Lifetimes

| Token | Lifetime | Auto-managed |
|---|---|---|
| MCP access token (JWT) | 8 hours (`MCP_ACCESS_TOKEN_TTL`) | Client refreshes via refresh token |
| MCP refresh token (JWT) | 30 days (`MCP_REFRESH_TOKEN_TTL`) | Rotated on each refresh |
| MCP auth code | 10 minutes (`MCP_AUTH_CODE_TTL`) | Exchanged once for tokens |
| Google refresh token | Unlimited (until revoked) | Auto-refreshed by server when access expires |

All MCP token lifetimes are configurable. Google's token refresh is transparent — `GoogleClient.get_credentials()` refreshes silently when the access token expires.

## Docker Compose

```yaml
# docker-compose.yml uses env_file: .env only (no environment: block).
# Secrets volume mounted at /secrets in container.
services:
  google-workspace-mcp:
    build: .
    env_file: .env
    volumes:
      - ${SECRETS_VOLUME_PATH}:/secrets:rw
```

## Caddy Proxy (recommended)

```caddyfile
mcp.yourdomain.com {
    # Block /mcp from public internet (clients connect via localhost)
    route /mcp/* {
        respond 403
    }
    # Proxy OAuth + health endpoints
    reverse_proxy google-workspace-mcp:8000
}
```

## Troubleshooting

| Problem | Solution |
|---|---|
| `401` on `/mcp/` after auth | MCP client should auto-refresh. If stuck, restart client or re-authorize |
| "No Google credentials found for user@..." | User hasn't completed OAuth. Trigger OAuth via your MCP client (it discovers endpoints automatically) |
| `403 insufficient_scope` | Re-authorize with additional scopes via your MCP client (disconnect + reconnect) |
| `404` on `/.well-known/oauth-authorization-server` | Check `EXTERNAL_URL` matches your Caddy domain |
| Client can't discover OAuth | Verify `EXTERNAL_URL` is publicly reachable over HTTPS |
| `client_secret.json not found` | Mount it at `/secrets/client_secret.json` (or your configured path) |

## Project Structure

```
google-workspace-mcp/
├── app/
│   ├── config.py            # Settings (env-driven, no hardcoded values)
│   ├── main.py              # FastAPI + MCP OAuth middleware + routes
│   ├── mcp_server.py        # FastMCP server with all tools
│   ├── oauth.py             # Two-step Google OAuth web UI (/oauth/*)
│   ├── oauth_provider.py    # OAuthAuthorizationServerProvider implementation
│   ├── registry.py          # JSON-backed per-user token registry
│   ├── google_client.py     # Per-user Google credentials (via JWT subject)
│   ├── generate_config.py   # Optional config generator (manual use only)
│   └── tools/               # Tool implementations (gmail, drive, docs, ...)
├── Caddyfile                # Proxy: blocks /mcp, proxies OAuth + healthz
├── Caddyfile.example-full-proxy
├── docker-compose.yml       # env_file only (no environment: block)
├── Dockerfile
├── .env / .env.example
└── README.md
```

## Works With

Any MCP client supporting OAuth 2.0 Dynamic Client Registration + PKCE:
- QwenPaw
- Claude Desktop
- Claude Code
- Any compliant implementation

## Token Management

Once a Google account is authorized (entry created in `registry.json`), the
user does **not** need to re-authorize until that entry is removed.
Google's refresh token persists in the registry; when the Google access
token expires, the bridge silently refreshes it — no user interaction
needed.

If you need to remove an account, delete its entry from `registry.json`
(the secrets volume mount) and restart the container.
