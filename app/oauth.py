"""OAuth flow: single-step Google OAuth + MCP auth code.

This module implements the web UI layer on top of the OAuthAuthorizationServerProvider.

Flow (from MCP client via /authorize):
  1. MCP client -> /authorize -> provider.authorize() -> redirect to /oauth/scale?rid=XXX
  2. /oauth/scale -> determine scopes (all or client-requested) -> redirect to Google
     with openid + email + api scopes in ONE redirect (no scope selection form)
  3. Google -> /oauth/callback?code=...&state=XXX|auth
  4. Callback -> exchange code -> extract email -> store in registry -> generate MCP auth code
  5. Redirect to MCP client's redirect_uri with ?code=<mcp_code>&state=<original_state>

Google's consent page shows the requested scopes. The user can accept or deny.
SCOPE_SELECTOR_MODE determines which scopes are sent:
  - "all"      -> all AVAILABLE_SCOPES (+ identity scopes)
  - "requested" -> only the MCP client's requested scopes (+ identity scopes)
No per-user scope selection UI — Google's consent page is the selector.

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


def _google_auth_url(
    scopes: list[str],
    state: str,
    access_type: str = "offline",
    include_granted: bool = True,
) -> str:
    """Build a Google OAuth authorization URL and store the flow for later exchange.

    Args:
        scopes: Google API scopes to request
        state: Google OAuth state parameter (carries our internal {rid}|{step})
        access_type: "offline" (get refresh token) or "online" (no refresh token)
        include_granted: if True, include previously granted scopes (incremental
                         authorization). Set False for the identify step so Google
                         shows only the minimal consent screen.

    Returns:
        Google authorization URL (browser should redirect user here)
    """
    _cleanup_stores()
    flow = _get_flow(scopes)

    auth_kwargs = {
        "access_type": access_type,
        "prompt": "consent",  # always show consent screen (ensures refresh token)
        "state": state,
    }
    # Only include include_granted_scopes when incremental authorization is needed.
    # For the identify step (include_granted=False), omit it entirely so Google
    # shows a minimal consent screen (openid + email only), not all previously
    # granted scopes.
    if include_granted:
        auth_kwargs["include_granted_scopes"] = "true"

    auth_url, _ = flow.authorization_url(**auth_kwargs)
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
        # oauthlib raises a Warning ("Scope has changed") when the token response
        # contains scopes different from what the Flow was created with. This
        # happens with include_granted_scopes=true (stale scopes from prior
        # authorizations). Critically, oauthlib raises the Warning BEFORE
        # assigning the token to sess.token, so sess.token is always falsy after.
        log.warning("flow.fetch_token raised %s: %s — attempting manual token recovery",
                     type(e).__name__, e)
        sess = flow.oauth2session
        if hasattr(sess, "token") and sess.token:
            token_response = sess.token
        else:
            # Fallback: exchange the code manually via Google's token endpoint,
            # bypassing oauthlib's scope validation entirely.
            token_response = _manual_token_fetch(code, flow)
            if not token_response:
                raise  # recovery failed — re-raise original error

    return token_response, flow


def _manual_token_fetch(code: str, flow: Any) -> dict[str, Any] | None:
    """Exchange an authorization code via Google's token endpoint directly.

    Used as a fallback when ``flow.fetch_token`` raises a Warning about scope
    changes (oauthlib validates scopes before assigning the token). This
    bypasses oauthlib's scope validation and returns the raw response.
    """
    import requests
    client_config = flow.client_config.get("web", {})
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": client_config.get("client_id"),
        "client_secret": client_config.get("client_secret"),
        "redirect_uri": _get_redirect_uri(),
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post(token_url, data=data, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        log.error("Manual token fetch failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("Manual token fetch request error: %s", e)
    return None


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
    """Build a Google Credentials-compatible dict for registry storage.

    Only stores token-specific fields per user.  ``client_id`` and
    ``client_secret`` are fixed (from ``client_secret.json``) and loaded at
    runtime by ``GoogleClient`` — no need to store them per user.
    ``scopes`` are NOT stored here either; the registry's top-level ``scopes``
    field (user-selected) is the authoritative scope list.
    """
    creds = _safe_get_credentials(token_response, flow)
    return {
        "token": creds.token if creds else token_response.get("access_token"),
        "refresh_token": creds.refresh_token if creds else token_response.get("refresh_token"),
        "token_uri": creds.token_uri if creds else "https://oauth2.googleapis.com/token",
        "expiry": creds.expiry.isoformat() if creds and creds.expiry else None,
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/scale", response_class=HTMLResponse)
def scope_selector(request: Request):
    """Entry point from /authorize (via provider).

    - skip: redirect directly to Google with default scopes (no chooser)
    - all/requested: render scope selection form. Google consent cannot
      deselect previously-granted scopes, so bridge shows chooser first.
      No pre-checking -- user selects from scratch each time.
    """
    rid = request.query_params.get("rid")
    if not rid:
        return _error_page("Missing request ID")

    _cleanup_stores()

    mode = settings.scope_selector_mode
    if mode == "skip":
        scopes = list(settings.default_scopes)
        for s in ("openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"):
            if s not in scopes:
                scopes.append(s)
        state = f"{rid}|auth"
        auth_url = _google_auth_url(scopes=scopes, state=state, access_type="offline", include_granted=False)
        return RedirectResponse(url=auth_url, status_code=302)

    auth_req = provider._auth_requests.get(rid) if provider and hasattr(provider, "_auth_requests") else None
    client_scopes = auth_req["scopes"] if auth_req else []

    if mode == "requested":
        visible = [s for s in settings.all_scopes if s in client_scopes] or list(settings.all_scopes)
    else:
        visible = list(settings.all_scopes)

    identity_scopes = ("openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile")
    visible = [s for s in visible if s not in identity_scopes]

    return _render_scope_form(rid, visible, request)


@router.post("/scale", response_class=HTMLResponse)
async def scope_submit(request: Request):
    """Process scope selection -> redirect to Google with selected scopes."""
    rid = request.query_params.get("rid")
    if not rid:
        return _error_page("Missing request ID")

    _cleanup_stores()
    form = await request.form()
    selected = []
    for scope in settings.all_scopes:
        field_name = "scope_" + scope.replace("://", "_").replace("/", "_").replace(".", "_")
        if form.get(field_name):
            selected.append(scope)

    if not selected:
        return _error_page("Please select at least one scope.")

    for s in ("openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"):
        if s not in selected:
            selected.append(s)

    state = f"{rid}|auth"
    auth_url = _google_auth_url(scopes=selected, state=state, access_type="offline", include_granted=False)
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

    if step == "auth":
        # Single-step auth: Google returned everything (email + token) in one redirect.
        # Scopes sent were determined by SCOPE_SELECTOR_MODE (all or requested).
        email = _extract_email(token_response, flow)
        if not email:
            return _error_page("Could not determine user email.")

        # Remove identity scopes — they are for auth, not Google API access.
        scope_str = token_response.get("scope", "")
        if isinstance(scope_str, list):
            all_granted = scope_str
        elif isinstance(scope_str, str) and scope_str:
            all_granted = scope_str.split()
        else:
            all_granted = []
        identity_scopes = ("openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile")
        granted_scopes = [s for s in all_granted if s not in identity_scopes]

        # Store in registry (keyed by Google email)
        creds_data = _build_token_data(token_response, flow)
        registry.save(email, creds_data, granted_scopes)
        log.info("Auth complete for %s, scopes=%d", email, len(granted_scopes))

        # Clean up in-memory stores
        provider._auth_requests._requests.pop(rid, None)

        if auth_req.get("redirect_uri"):
            # MCP OAuth flow: generate auth code → redirect to MCP client
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
            access_token = provider._issue_access_token(email, granted_scopes)
            return _success_page_with_token(email, granted_scopes, access_token)

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


def _render_scope_form(rid, visible_scopes, request):
    """Render scope selection form (no pre-checking). Google cannot deselect
    previously-granted scopes, so the bridge lets users choose here first.

    Scopes are grouped into two explicit sections:
      - Read-only scopes  — safe, view-only access
      - Read + Write scopes — modify/send/delete access
    A checkbox per scope controls what is sent to Google. Only checked scopes
    are included in the ``scope`` parameter of the Google authorization request.
    """
    labels = settings.scope_labels
    read_set = set(settings.read_scopes)

    # Partition scopes into read-only and read+write sections
    read_rows = []
    write_rows = []
    for scope in visible_scopes:
        label = labels.get(scope, scope.replace("https://www.googleapis.com/auth/", ""))
        field_name = "scope_" + scope.replace("://", "_").replace("/", "_").replace(".", "_")
        is_readonly = scope in read_set or "readonly" in scope
        checkbox = '<input type="checkbox" name="' + field_name + '" value="1" style="margin-right:10px;width:16px;height:16px">'
        row = '<label style="display:flex;align-items:center;padding:6px 0;border-bottom:1px solid #eee">' + checkbox + label + '</label>'
        if is_readonly:
            read_rows.append(row)
        else:
            write_rows.append(row)

    read_count = len(read_rows)
    write_count = len(write_rows)
    rows_read = "\n".join(read_rows) if read_rows else '<p style="font-size:12px;color:#999">No read-only scopes available.</p>'
    rows_write = "\n".join(write_rows) if write_rows else '<p style="font-size:12px;color:#999">No read+write scopes available.</p>'

    html = (
        '<!DOCTYPE html>\n'
        '<html><head><title>Google Workspace MCP - Scope Selection</title>\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<style>\n'
        '  body{font-family:-apple-system,sans-serif;max-width:720px;margin:30px auto;padding:0 20px;background:#fafafa}\n'
        '  h1{color:#1a237e;font-size:22px}\n'
        '  .subtitle{color:#666;font-size:13px;margin-bottom:20px}\n'
        '  .info{background:#fff3cd;padding:12px 16px;border:1px solid #ffeaa7;border-radius:8px;margin-bottom:20px;font-size:13px}\n'
        '  .info strong{color:#856404}\n'
        '  .section{background:white;border:1px solid #e0e0e0;border-radius:8px;padding:12px 16px;margin-bottom:16px}\n'
        '  .section h3{font-size:14px;font-weight:600;margin:0 0 8px 0}\n'
        '  .section.read h3{color:#2e7d32}\n'
        '  .section.write h3{color:#c62828}\n'
        '  .scopes{max-height:320px;overflow-y:auto}\n'
        '  .actions{text-align:center;margin:24px 0}\n'
        '  .preset-btn{display:inline-block;width:100%;max-width:280px;box-sizing:border-box;\n'
        '    background:#37474f;color:white;border:none;padding:10px 16px;border-radius:6px;\n'
        '    cursor:pointer;font-size:14px;margin:4px 0;text-align:center;white-space:nowrap}\n'
        '  .preset-btn.readonly{background:#2e7d32}.preset-btn.readonly:hover{background:#1b5e20}\n'
        '  .preset-btn.full{background:#c62828}.preset-btn.full:hover{background:#b71c1c}\n'
        '  .preset-btn.submit{background:#2e7d32}.preset-btn.submit:hover{background:#1b5e20}\n'
        '  .summary{font-size:12px;color:#888;margin-top:12px}\n'
        '  input[type="checkbox"]{width:16px;height:16px;cursor:pointer;margin-right:10px;vertical-align:middle}\n'
        '  .scope-row{display:flex;align-items:center;padding:6px 0;border-bottom:1px solid #eee}\n'
        '  @media(max-width:480px){.preset-btn{font-size:13px;padding:10px 14px}}\n'
        '</style></head><body>\n'
        '<h1>Google Workspace MCP</h1>\n'
        '<p class="subtitle">Select which Google API scopes to authorize.</p>\n'
        '<div class="info">\n'
        '  <strong>Important:</strong> Google does not let you deselect previously granted scopes on its consent page.<br>\n'
        '  That is why the bridge shows this selector first. Choose one of the two options below,<br>\n'
        '  then Google will show its consent screen for only those scopes.<br><br>\n'
        '  <strong>Warning:</strong> Scopes already authorized in a previous session <strong>cannot be removed here</strong>.<br>\n'
        '  To truly remove them, go to Google Account → Security → "Third-party apps with account access"<br>\n'
        '  → remove this app, then re-authorize with only the scopes you want.\n'
        '</div>\n'
        '<form method="POST" action="/oauth/scale?rid=' + rid + '">\n'
        '<div class="summary"><strong>' + str(read_count) + ' read-only</strong> + <strong>' + str(write_count) + ' read+write</strong> = ' + str(read_count + write_count) + ' total scopes</div>\n'
        '  <div class="section read">\n'
        '    <h3>\U00002713\U0000fe0f Read-only scopes (' + str(read_count) + ')</h3>\n'
        '    <div class="actions">\n'
        '      <button type="button" class="preset-btn readonly" onclick="selectRead()">\U00001f440 Only Read — read-only access only</button>\n'
        '    </div>\n'
        '    <div class="scopes">' + rows_read + '</div>\n'
        '  </div>\n'
        '  <div class="section write">\n'
        '    <h3>\U0000270f\ufe0f Read + Write scopes (' + str(write_count) + ')</h3>\n'
        '    <div class="actions">\n'
        '      <button type="button" class="preset-btn full" onclick="selectAll(true)">\U000026a0\ufe0f Full Access — read + write</button>\n'
        '    </div>\n'
        '    <div class="scopes">' + rows_write + '</div>\n'
        '  </div>\n'
        '  <div class="actions">\n'
        '    <button type="submit" class="preset-btn submit">Authorize with Google</button>\n'
        '  </div>\n'
        '  <p class="summary">Only checked scopes will be sent to Google for authorization.</p>\n'
        '</form>\n'
        '<script>\n'
        "  function selectAll(checked) {\n"
        "    document.querySelectorAll('input[type=\"checkbox\"]').forEach(function(cb) { cb.checked = checked; });\n"
        '  }\n'
        "  function selectRead() {\n"
        "    document.querySelectorAll('.section.read input[type=\"checkbox\"]').forEach(function(cb) { cb.checked = true; });\n"
        "    document.querySelectorAll('.section.write input[type=\"checkbox\"]').forEach(function(cb) { cb.checked = false; });\n"
        '  }\n'
        '</script>\n'
        '</body></html>'
    )
    return HTMLResponse(html)


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
