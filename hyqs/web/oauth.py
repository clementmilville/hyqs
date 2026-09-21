"""OAuth 2.0 / OIDC login handlers: Google and Apple.

Exports ``build_oauth_routes(config, store) -> list[Route]`` for registration
in build_app(). All routes are unauthenticated (no bearer token required).
"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from hyqs.config import Config
    from hyqs.pipeline.store import JobStore

_STATE_COOKIE = "hyqs_oauth_state"
SESSION_COOKIE_NAME = "hyqs_session"
SESSION_COOKIE_MAX_AGE = 30 * 24 * 3600
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_APPLE_AUTH_URL = "https://appleid.apple.com/auth/authorize"
_APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
_APPLE_AUDIENCE = "https://appleid.apple.com"

_NO_INVITATION_HTML = """\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Access Denied – Hyqs</title>
<style>body{font-family:sans-serif;max-width:480px;margin:4rem auto;padding:0 1rem}
h1{font-size:1.5rem}a{color:#5b8dee}</style>
</head>
<body>
<h1>No invitation found</h1>
<p>Your email address has not been invited to this system.</p>
<p>Please ask an administrator to send you an invitation, then try again.</p>
<p><a href="/">← Back to login</a></p>
</body>
</html>
"""

_NETWORK_ERROR_HTML = """\
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Login Unavailable – Hyqs</title>
<style>body{font-family:sans-serif;max-width:480px;margin:4rem auto;padding:0 1rem}
h1{font-size:1.5rem}a{color:#5b8dee}</style>
</head>
<body>
<h1>Login temporarily unavailable</h1>
<p>There was a network hiccup while completing your sign-in. Please try again.</p>
<p>(Error 502)</p>
<p><a href="/">← Try again</a></p>
</body>
</html>
"""


def _redirect_uri(request: Request, provider: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oauth/{provider}/callback"


def _make_apple_client_secret(config: "Config") -> str:
    import time

    import jwt  # PyJWT

    now = int(time.time())
    private_key = config.apple_private_key.replace("\\n", "\n")
    return jwt.encode(
        {
            "iss": config.apple_team_id,
            "iat": now,
            "exp": now + 86400 * 180,
            "aud": _APPLE_AUDIENCE,
            "sub": config.apple_client_id,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": config.apple_key_id},
    )


def _issue_or_provision(store: "JobStore", email: str, name: str, provider: str) -> str | None:
    """Return a new session token for email, provisioning the account if invited."""
    user = store.get_user_by_email(email)
    if user is not None:
        if user.is_active and user.oauth_provider == provider:
            return store.create_session(user.id)
        return None
    invitation = store.consume_invitation(email)
    if invitation is None:
        return None
    user = store.provision_oauth_user(email, name, provider)
    return store.create_session(user.id)


def build_oauth_routes(config: "Config", store: "JobStore") -> list[Route]:
    async def google_redirect(request: Request) -> Response:
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": config.google_client_id,
            "redirect_uri": _redirect_uri(request, "google"),
            "scope": "openid email profile",
            "response_type": "code",
            "state": state,
            "prompt": "select_account",
        }
        resp = RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")
        resp.set_cookie(_STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
        return resp

    async def google_callback(request: Request) -> Response:
        state_cookie = request.cookies.get(_STATE_COOKIE, "")
        state_param = request.query_params.get("state", "")
        if not state_cookie or state_cookie != state_param:
            return HTMLResponse("Invalid OAuth state", status_code=400)

        code = request.query_params.get("code", "")
        if not code:
            return HTMLResponse("Missing authorization code", status_code=400)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                token_resp = await client.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": config.google_client_id,
                        "client_secret": config.google_client_secret,
                        "redirect_uri": _redirect_uri(request, "google"),
                        "grant_type": "authorization_code",
                    },
                )
                if token_resp.status_code != 200:
                    return HTMLResponse("Token exchange failed", status_code=502)
                access_token = token_resp.json().get("access_token", "")

                userinfo_resp = await client.get(
                    _GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_resp.status_code != 200:
                    return HTMLResponse("Failed to fetch user info", status_code=502)
                userinfo = userinfo_resp.json()
        except httpx.HTTPError:
            resp = HTMLResponse(_NETWORK_ERROR_HTML, status_code=502)
            resp.delete_cookie(_STATE_COOKIE)
            return resp

        email = (userinfo.get("email") or "").strip().lower()
        name = (userinfo.get("name") or "").strip() or email
        if not email:
            return HTMLResponse("No email returned by Google", status_code=400)

        session_token = _issue_or_provision(store, email, name, "google")
        if session_token is None:
            resp = HTMLResponse(_NO_INVITATION_HTML, status_code=403)
            resp.delete_cookie(_STATE_COOKIE)
            return resp

        resp = RedirectResponse("/")
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            session_token,
            max_age=SESSION_COOKIE_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        resp.delete_cookie(_STATE_COOKIE)
        return resp

    async def apple_redirect(request: Request) -> Response:
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": config.apple_client_id,
            "redirect_uri": _redirect_uri(request, "apple"),
            "scope": "name email",
            "response_type": "code",
            "response_mode": "form_post",
            "state": state,
            "prompt": "select_account",
        }
        # Apple's form_post callback is cross-site, so cookie must be samesite=none+secure.
        resp = RedirectResponse(f"{_APPLE_AUTH_URL}?{urlencode(params)}")
        resp.set_cookie(
            _STATE_COOKIE, state, httponly=True, samesite="none", secure=True, max_age=600
        )
        return resp

    async def apple_callback(request: Request) -> Response:
        form = await request.form()
        state_cookie = request.cookies.get(_STATE_COOKIE, "")
        state_param = form.get("state", "")
        if not state_cookie or state_cookie != state_param:
            return HTMLResponse("Invalid OAuth state", status_code=400)

        code = form.get("code", "")
        if not code:
            return HTMLResponse("Missing authorization code", status_code=400)

        client_secret = _make_apple_client_secret(config)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                token_resp = await client.post(
                    _APPLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": config.apple_client_id,
                        "client_secret": client_secret,
                        "redirect_uri": _redirect_uri(request, "apple"),
                        "grant_type": "authorization_code",
                    },
                )
                if token_resp.status_code != 200:
                    return HTMLResponse("Token exchange failed", status_code=502)
        except httpx.HTTPError:
            resp = HTMLResponse(_NETWORK_ERROR_HTML, status_code=502)
            resp.delete_cookie(_STATE_COOKIE)
            return resp

        import jwt  # PyJWT

        id_token = token_resp.json().get("id_token", "")
        claims = jwt.decode(id_token, options={"verify_signature": False})
        email = (claims.get("email") or "").strip().lower()
        if not email:
            return HTMLResponse("No email in Apple identity token", status_code=400)

        # Apple sends user name only on the first authorization, in the form POST body.
        try:
            user_data = json.loads(form.get("user") or "{}")
            name_data = user_data.get("name") or {}
            first = (name_data.get("firstName") or "").strip()
            last = (name_data.get("lastName") or "").strip()
            name = f"{first} {last}".strip() or email
        except (ValueError, AttributeError):
            name = email

        session_token = _issue_or_provision(store, email, name, "apple")
        if session_token is None:
            resp = HTMLResponse(_NO_INVITATION_HTML, status_code=403)
            resp.delete_cookie(_STATE_COOKIE)
            return resp

        resp = RedirectResponse("/")
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            session_token,
            max_age=SESSION_COOKIE_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        resp.delete_cookie(_STATE_COOKIE)
        return resp

    async def list_auth_providers(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "google": bool(config.google_client_id),
                "apple": bool(config.apple_client_id),
            }
        )

    return [
        Route("/api/auth/oauth/google/redirect", google_redirect),
        Route("/api/auth/oauth/google/callback", google_callback),
        Route("/api/auth/oauth/apple/redirect", apple_redirect),
        Route("/api/auth/oauth/apple/callback", apple_callback, methods=["POST"]),
        Route("/api/auth/providers", list_auth_providers),
    ]
