# Job #4315: Move the session token out of the URL query string

**Date:** 2026-09-15

This diff ships a migration from URL-based token authentication to httponly session cookies for OAuth logins. OAuth callbacks now set a secure, httponly `hyqs_session` cookie instead of embedding tokens in redirect URLs, and the backend's `TokenAuth` middleware authenticates via this cookie for regular routes while preserving query-param authentication only for GET SSE endpoints that cannot send headers. The frontend probes `/api/me` on mount to bootstrap cookie-authenticated sessions without requiring a stored token, and the logout route now properly deletes the session cookie. Comprehensive regression tests verify the cookie handoff and enforce that query tokens no longer work on non-SSE routes.
This diff ships a migration from URL-based token authentication to httponly session cookies for OAuth logins. OAuth callbacks now set a secure, httponly `hyqs_session` cookie instead of embedding tokens in redirect URLs, and the backend's `TokenAuth` middleware authenticates via this cookie for regular routes while preserving query-param authentication only for GET SSE endpoints that cannot send headers. The frontend probes `/api/me` on mount to bootstrap cookie-authenticated sessions without requiring a stored token, and the logout route now properly deletes the session cookie. Comprehensive regression tests verify the cookie handoff and enforce that query tokens no longer work on non-SSE routes. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
- hyqs/web/frontend/src/App.jsx
- hyqs/web/frontend/src/App.test.jsx
- hyqs/web/frontend/src/components/OAuthGate.jsx
- hyqs/web/frontend/src/components/OAuthGate.test.jsx
- hyqs/web/oauth.py
- tests/test_oauth_cookie.py
- tests/test_token_auth_cookie.py
