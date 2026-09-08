"""OAuth flow: two-step Google OAuth + scope selection + MCP auth code.

This module implements the web UI layer on top of the OAuthAuthorizationServerProvider.

Flow (from MCP client via /authorize):
  1. MCP client -> /authorize -> provider.authorize() -> redirect to /oauth/scale?rid=XXX
  2. /oauth/scale -> (no email yet) -> redirect to Google (scope=openid email)  [identify]
  3. Google -> /oauth/callback?code=...&state=XXX|identify
  4. Callback -> exchange code -> extract email -> store in session -> redirect to /oauth/scale
  5. /oauth/scale -> render scope selection form (pre-populated from registry if user exists)
  6. User POST /oauth/scale -> redirect to Google (scope=selected)  [authorize]
  7. Google -> /oauth/callback?code=...&state=XXX|authorize
  8. Callback -> exchange code -> store in registry -> generate MCP auth code
  9. Redirect to MCP client's redirect_uri with ?code=<mcp_code>&state=<original_state>

"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi import HTTPException

from app.config import settings
from app.oauth_provider import provider, registry

log = logging.getLogger("google-workspace-mcp")

router = APIRouter(prefix="/oauth", tags=["oauth"])


# ── Google OAuth state stores ─────────────────────────────────────────────

# Google OAuth state -> {flow, rid, step, created_at, expires_at}
_google_state_store: dict[str, dict[str, Any]] = {}

# rid (request_id from provider's auth request store) -> {email, selected_scopes, expires_at}
# Set after step 1 (identify) completes; used in steps 2, 5, 8
_callback_session: dict[str, dict[str, Any]] = {}


# ── Helpers ────────────────────────────────────────────────────────────────

def _cleanup_stores() -> None:
    """Remove expired entries from in-memory stores."""
    now = time.time()
    for store in [_google_state_store, _callback_session]:
        expired = [k for k, v in store.items() if v.get("expires_at", 0) < now]
        for k in expired:
            del store[k]


def _get_redirect_uri() -> str:
    """Google OAuth redirect URI (where Google sends the user back)."""
    return f"{settings.external_url}/oauth/callback"


def _get_flow(scopes: list[str]) -> Any:
    """Create a Google OAuth Flow with the given scopes."""
    import json
    from pathlib import Path
    from google_auth_oauthlib.flow import Flow

    client_config_path = settings.google_client_secret_file
    path = Path(client_config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Google client_secret.json not found at {client_config_path}. "
            f"Mount your Google OAuth credentials there (see docker-compose.yml)."
        )
    client_config = json.loads(path.read_text())

    flow = Flow.from_client_config(
        client_config,
        scopes=scopes,
        redirect_uri=_get_redirect_uri(),
    )
    return flow


def _google_auth_url(scopes: list[str], state: str, access_type: str = "offline") -> str:
    """Build a Google OAuth authorization URL and store the flow for later exchange.

    Args:
        scopes: Google API scopes to request
        state: Google OAuth state parameter (carries our internal {rid}|{step})
        access_type: "offline" (get refresh token) or "online" (no refresh token)

    Returns:
        Google authorization URL (browser should redirect user here)
    """
    _cleanup_stores()
    flow = _get_flow(scopes)
    auth_url, _ = flow.authorization_url(
        access_type=access_type,
        prompt="consent",                  # always show consent screen (ensures refresh token)
        include_granted_scopes="true",     # include previously granted scopes (incremental addition)
        state=state,
        # Note: include_granted_scopes may cause oauthlib to emit a
        # "Scope has changed" Warning. _exchange_code() catches it.
    )
    # Store the flow keyed by the Google OAuth state string
    _google_state_store[state] = {
        "flow": flow,
        "rid": state.split("|")[0] if "|" in state else state,
        "step": state.split("|")[1] if "|" in state else "unknown",
        "created_at": time.time(),
        "expires_at": time.time() + 600,  # 10 min TTL
    }
    return auth_url


def _build_creds_from_token(token_response: dict[str, Any], flow: Any) -> Any:
    """Build Google Credentials from a token response dict.

    This is used when flow.credentials fails (e.g., after a scope-mismatch
    Warning from include_granted_scopes). Builds Credentials directly from
    the raw token response returned by Google's token endpoint.
    """
    from google.oauth2.credentials import Credentials as GCredentials
    import datetime

    client_config = flow.client_config.get("web", {})
    scope_str = token_response.get("scope", "")
    scopes = scope_str.split() if scope_str else []

    creds = GCredentials(
        token=token_response.get("access_token"),
        refresh_token=token_response.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_config.get("client_id"),
        client_secret=client_config.get("client_secret"),
        scopes=scopes,
    )

    # Set expiry from token response
    expires_in = token_response.get("expires_in")
    if expires_in:
        creds.expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=int(expires_in))

    return creds


def _exchange_code(code: str, state: str) -> tuple[dict[str, Any], Any]:
    """Exchange Google authorization code for token.

    Returns (token_response_dict, flow_object).

    Handles the "scope mismatch" warning (oauthlib raises a Warning when
    include_granted_scopes causes previously-granted scopes to mismatch)
    by recovering the token from the oauth2session. The returned flow may
    have a broken .credentials property after Warning recovery — callers
    should use _build_creds_from_token() as a fallback.
    """
    stored = _google_state_store.pop(state, None)
    if not stored:
        raise HTTPException(400, "Invalid or expired OAuth state")

    flow = stored["flow"]
    flow.redirect_uri = _get_redirect_uri()

    try:
        token_response = flow.fetch_token(code=code)
    except (Exception, Warning) as e:
        # oauthlib may raise a Warning when scopes change due to
        # include_granted_scopes. The token is usually already in the session.
        log.warning("flow.fetch_token raised %s: %s — attempting token recovery", type(e).__name__, e)
        sess = flow.oauth2session
        # Check truthiness (not just None) — sess.token may be {} (falsy)
        if not hasattr(sess, "token") or not sess.token:
            raise  # real error, re-raise
        token_response = sess.token

    return token_response, flow


def _safe_get_credentials(token_response: dict[str, Any], flow: Any) -> Any:
    """Get a Credentials object, trying flow.credentials first then token_response."""
    try:
        creds = flow.credentials
        if creds and creds.token:
            return creds
    except Exception:
        pass
    # Fallback: build from token_response directly
    return _build_creds_from_token(token_response, flow)


def _extract_email(token_response: dict[str, Any], flow: Any) -> str | None:
    """Extract the user's Google email from the OAuth token response.

    Method 1: decode the id_token JWT (fast, no network).
    Method 2: call the Google userinfo API (fallback).
    """
    # Method 1: decode id_token (JWT) — prefer token_response, fall back to creds
    id_token_value = token_response.get("id_token")
    if not id_token_value:
        try:
            creds = _safe_get_credentials(token_response, flow)
            id_token_value = getattr(creds, "id_token", None)
        except Exception:
            pass
    if id_token_value:
        import jwt
        try:
            claims = jwt.decode(id_token_value, options={"verify_signature": False})
            email = claims.get("email")
            if email:
                return email
        except Exception as e:
            log.warning("Failed to decode id_token: %s", e)

    # Method 2: call userinfo API (fallback)
    try:
        from googleapiclient.discovery import build
        creds = _safe_get_credentials(token_response, flow)
        if creds:
            service = build("oauth2", "v2", credentials=creds)
            user_info = service.userinfo().get().execute()
            return user_info.get("email")
    except Exception:
        pass

    return None


def _build_token_data(token_response: dict[str, Any], flow: Any) -> dict[str, Any]:
    """Build a Google Credentials-compatible dict for registry storage."""
    creds = _safe_get_credentials(token_response, flow)
    # Use scopes from the token response (reflects what Google actually granted,
    # including previously granted scopes via include_granted_scopes=true)
    scope_str = token_response.get("scope", "")
    if scope_str:
        scopes = scope_str.split()
    elif creds and creds.scopes:
        scopes = list(creds.scopes)
    else:
        scopes = []
    return {
        "token": creds.token if creds else token_response.get("access_token"),
        "refresh_token": creds.refresh_token if creds else token_response.get("refresh_token"),
        "token_uri": creds.token_uri if creds else "https://oauth2.googleapis.com/token",
        "client_id": creds.client_id if creds else None,
        "client_secret": creds.client_secret if creds else None,
        "expiry": creds.expiry.isoformat() if creds and creds.expiry else None,
        "scopes": scopes,
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/scale", response_class=HTMLResponse)
def scope_selector(request: Request):
    """Entry point from /authorize (via provider).

    Step A: If user not yet identified -> redirect to Google (minimal scopes).
    Step B: If user identified -> render scope selection form.
    """
    rid = request.query_params.get("rid")
    if not rid:
        return _error_page("Missing request ID")

    _cleanup_stores()

    # Check if user was already identified (from step 1)
    session = _callback_session.get(rid)
    if not session or not session.get("email"):
        # Step A: redirect to Google to identify the user (step 1)
        state = f"{rid}|identify"
        auth_url = _google_auth_url(
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
            state=state,
            access_type="online",  # no refresh token needed in identify step
        )
        return RedirectResponse(url=auth_url, status_code=302)

    # Step B: render scope selection form
    email = session["email"]
    existing_scopes = registry.get_scopes(email) or []
    auth_req = provider._auth_requests.get(rid)
    client_scopes = auth_req["scopes"] if auth_req else []

    mode = settings.scope_selector_mode
    return _render_scope_form(rid, email, existing_scopes, client_scopes, mode, request)


@router.post("/scale", response_class=HTMLResponse)
async def scope_submit(request: Request):
    """Handle scope selection submission -> redirect to Google with selected scopes."""
    rid = request.query_params.get("rid")
    if not rid:
        return _error_page("Missing request ID")

    _cleanup_stores()
    session = _callback_session.get(rid)
    if not session:
        return _error_page("Session expired. Please try again.")

    # Parse selected scopes from the form
    form = await request.form()
    selected = []
    for scope in settings.all_scopes:
        # Checkboxes are named "scope_<encoded_scope>"
        field_name = "scope_" + scope.replace("://", "_").replace("/", "_")
        if form.get(field_name):
            selected.append(scope)

    if not selected:
        return _error_page("Please select at least one scope.")

    session["selected_scopes"] = selected

    # Redirect to Google with selected scopes (step: authorize)
    state = f"{rid}|authorize"
    auth_url = _google_auth_url(scopes=selected, state=state)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback", response_class=HTMLResponse)
def callback(request: Request):
    """Google OAuth callback (public — Google redirects here).

    Handles both steps:
    - state={rid}|identify: exchange code, extract email, redirect to /oauth/scale
    - state={rid}|authorize: exchange code, store in registry, complete MCP auth
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error")

    if error:
        return _error_page(f"Google OAuth error: {error}")

    if not code or not state:
        return _error_page("Missing code or state parameter")

    # Parse Google state: {rid}|{step}
    parts = state.split("|", 1)
    if len(parts) != 2:
        return _error_page("Invalid Google OAuth state format")
    rid, step = parts

    _cleanup_stores()

    # Exchange code for Google token
    try:
        token_response, flow = _exchange_code(code, state)
    except HTTPException:
        raise
    except Exception as e:
        log.error("Token exchange failed: %s", e, exc_info=True)
        return _error_page(f"Token exchange failed: {e}")

    # Verify rid still exists (auth request from MCP client or manual flow)
    auth_req = provider._auth_requests.get(rid)
    if auth_req is None:
        return _error_page("Request expired. Please restart authorization via your MCP client.")

    if step == "identify":
        # Step 1: identify the user → store email → redirect to scope form
        email = _extract_email(token_response, flow)
        if not email:
            return _error_page("Could not determine your Google email. Please try again.")

        _callback_session[rid] = {
            "email": email,
            "selected_scopes": None,
            "expires_at": time.time() + 600,
        }
        log.info("User identified: %s", email)

        # Redirect to scope selection page
        return RedirectResponse(
            url=f"{settings.external_url}/oauth/scale?rid={rid}",
            status_code=302,
        )

    elif step == "authorize":
        # Step 2: user authorized with selected scopes -> store in registry
        session = _callback_session.get(rid, {})
        email = session.get("email")
        if not email:
            # Fallback: extract email from this token response too
            email = _extract_email(token_response, flow)

        if not email:
            return _error_page("Could not determine user email.")

        # Use scopes actually granted by Google (includes previously granted
        # via include_granted_scopes=true, plus the newly selected ones)
        scope_str = token_response.get("scope", "")
        granted_scopes = scope_str.split() if scope_str else session.get("selected_scopes", settings.default_scopes)
        if not granted_scopes:
            granted_scopes = settings.default_scopes

        # Store in registry (keyed by Google email)
        creds_data = _build_token_data(token_response, flow)
        registry.save(email, creds_data, granted_scopes)

        # Clean up in-memory stores
        provider._auth_requests._requests.pop(rid, None)
        del _callback_session[rid]

        if auth_req.get("redirect_uri"):
            # MCP OAuth flow -> generate auth code -> redirect to MCP client
            mcp_code = provider._generate_mcp_auth_code(
                client_id=auth_req["client_id"],
                code_challenge=auth_req.get("code_challenge", ""),
                redirect_uri=auth_req.get("redirect_uri", ""),
                scopes=granted_scopes,
                subject=email,
            )

            redirect_uri = auth_req.get("redirect_uri", "")
            mcp_state = auth_req.get("mcp_state", "")
            params = f"code={mcp_code}&state={mcp_state}"
            return RedirectResponse(url=f"{redirect_uri}?{params}", status_code=302)
        else:
            # Manual flow -> show success page
            log.info("Manual OAuth complete for %s, scopes=%d", email, len(granted_scopes))
            # Generate a short-lived MCP access token for testing
            access_token = provider._issue_access_token(email, granted_scopes)
            return _success_page_with_token(email, granted_scopes, access_token)

    else:
        return _error_page(f"Unknown OAuth step: {step}")


# ── HTML rendering ──────────────────────────────────────────────────────────

def _error_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html><head><title>OAuth Error</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:sans-serif;max-width:600px;margin:40px auto;padding:0 20px;text-align:center}}
.error{{background:#ffebee;color:#c62828;padding:20px;border-radius:8px}}</style>
</head><body><div class="error"><h2>OAuth Error</h2><p>{message}</p>
<p>If using an MCP client, disconnect and reconnect it.</p></div></body></html>""",
        status_code=400,
    )


def _render_scope_form(rid: str, email: str, existing: list[str], client_scopes: list[str], mode: str, request: Request) -> HTMLResponse:
    """Render the scope selection HTML form.

    **mode** controls which scopes are shown:
      - "all"       → show every available Google scope (default)
      - "requested" → show only scopes the MCP client requested

    In both modes the user's existing grants are pre-selected so that
    re-auth *adds* rather than *replaces*.

    Read-only and write/delete scopes are visually separated so that
    higher-risk scopes are clearly flagged.
    """
    labels = settings.scope_labels
    all_scopes = settings.all_scopes

    # ── Determine which scopes to show in the selector ──
    if mode == "requested":
        visible_scopes = [s for s in all_scopes if s in client_scopes]
    else:
        visible_scopes = all_scopes

    labels_map = settings.scope_labels

    def _field_name(scope: str) -> str:
        return "scope_" + scope.replace("://", "_").replace("/", "_")

    def _checkbox(scope: str, checked: bool = False, css_class: str = "scope-read") -> str:
        field = _field_name(scope)
        label = labels_map.get(scope, scope)
        display = label if len(label) < 60 else label[:57] + "..."
        checked_attr = "checked" if checked else ""
        return (
            f'<label class="{css_class}" style="display:flex;align-items:center;gap:6px;'
            f'padding:4px 0;font-size:13px;">'
            f'<input type="checkbox" name="{field}" value="1" {checked_attr}>'
            f' {display}</label>'
        )

    def _group(title: str, scopes: list[str], css_class: str = "scope-read") -> str:
        if not scopes:
            return ""
        items = "".join(_checkbox(s, checked=s in existing, css_class=css_class) for s in scopes)
        return (
            f'<fieldset class="{css_class}" style="margin:12px 0;padding:12px;border:1px solid #e0e0e0;border-radius:6px;">'
            f'<legend style="font-weight:600;font-size:13px;color:#555;padding:0 6px;">{title}</legend>'
            f'{items}</fieldset>'
        )

    email_label = email if email else "New user"
    mode_label = "All available scopes" if mode == "all" else "Scopes requested by client"

    # Split visible scopes into read-only and write/delete
    read_scopes = [s for s in visible_scopes if s in settings._read_scopes or s in ("openid",)]
    write_scopes = [s for s in visible_scopes if s not in read_scopes]

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Select Scopes</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:700px;margin:30px auto;padding:0 20px;background:#f9f9f9}}
  .container{{background:white;padding:25px;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}}
  h1{{font-size:22px;color:#1a1a1a}}
  .user{{font-size:14px;color:#666;margin:10px 0 20px;font-weight:500}}
  .mode-badge{{font-size:11px;background:#e3f2fd;color:#1565c0;padding:2px 8px;border-radius:4px;display:inline-block}}
  fieldset{{margin:12px 0;border:1px solid #e0e0e0;border-radius:6px;padding:12px}}
  legend{{font-weight:600;font-size:13px;color:#555;padding:0 6px}}
  button{{background:#0066cc;color:white;border:none;padding:12px 24px;border-radius:6px;
          font-size:14px;cursor:pointer;width:100%;font-weight:600;margin-top:10px}}
  button:hover{{background:#0052a3}}
  p.note{{font-size:12px;color:#999;margin-top:15px}}
  label.scope-read{{color:#2e7d37}}
  label.scope-read input[type=checkbox]{{accent-color:#2e7d37}}
  label.scope-write{{color:#e65100;font-weight:500}}
  label.scope-write input[type=checkbox]{{accent-color:#e65100}}
  fieldset.scope-write{{border-color:#ffcc80;background:#fff3e0}}
</style></head><body>
<div class="container">
  <h1>Google Workspace MCP — Scope Selection</h1>
  <p class="user">Account: {email_label}</p>
  <p><span class="mode-badge">{mode_label}</span></p>
  <form method="POST" action="/oauth/scale?rid={rid}">
    <p style="font-size:13px;color:#666;">Select which Google Workspace access you grant to this MCP server.</p>
    {_group("Read-only scopes", read_scopes, css_class="scope-read")}
    {_group("Write / Delete scopes (higher risk)", write_scopes, css_class="scope-write")}
    <button type="submit">Authorize with Google</button>
    <p class="note">Existing grants are pre-checked. New grants are added to your existing scopes.</p>
  </form>
</div>
</body></html>""")
def _success_page_with_token(email: str, scopes: list[str], access_token: str) -> HTMLResponse:
    """Success page for manual OAuth flow — shows MCP bearer token."""
    import urllib.parse
    scopes_display = ", ".join(scopes[:5]) + (" ..." if len(scopes) > 5 else "")
    token_display = access_token[:60] + "..." if len(access_token) > 60 else access_token

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>OAuth Success</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:-apple-system,sans-serif;max-width:600px;margin:40px auto;padding:0 20px;text-align:center}}
  .success{{background:#e8f5e9;padding:25px;border-radius:10px;border:1px solid #c8e6c9}}
  h1{{color:#2e7d32;font-size:22px}}
  code{{background:#f5f5f5;padding:8px 12px;border-radius:4px;font-size:11px;word-break:break-all;display:block;margin:10px 0;text-align:left}}
  .scopes{{background:#f5f5f5;padding:10px;border-radius:4px;font-size:12px;text-align:left;max-height:150px;overflow:auto}}
  button{{background:#0066cc;color:white;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:8px}}
  button:hover{{background:#0052a3}}
</style></head><body>
<div class="success">
  <h1>✓ OAuth Successful</h1>
  <p><strong>Account:</strong> {email}</p>
  <p><strong>Scopes granted:</strong> {len(scopes)}</p>
  <div class="scopes">{scopes_display}</div>
  <p style="margin-top:15px;font-size:12px;text-align:left;">
    <strong>MCP Bearer Token</strong> (for standalone MCP client usage):
  </p>
  <code id="token">{token_display}</code>
  <button onclick="navigator.clipboard.writeText('{access_token}');this.textContent='✓ Copied!'">
    Copy to clipboard
  </button>
  <p style="font-size:10px;color:#999;margin-top:12px;">
    Note: MCP clients with OAuth auto-discovery (QwenPaw, Claude Desktop)
    do not need this token — they discover endpoints automatically.
  </p>
</div>
<p style="margin-top:10px;font-size:12px;color:#999;">
  <p>Authorized accounts are managed via your MCP client.</p>
</p>
</body></html>""")
