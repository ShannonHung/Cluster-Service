"""
tests/integration/test_login_page.py

Integration tests for the browser-first /login page.
Verifies GET /login renders correctly and POST /login authenticates and redirects.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


# TestClient that does NOT follow redirects — needed for POST /login assertions.
_no_follow_client = TestClient(app, follow_redirects=False)


# ── GET /login ─────────────────────────────────────────────────────────────────

def test_get_login_returns_html_with_password_input(client: TestClient):
    """GET /login returns 200 with a password input and a form posting to /login."""
    resp = client.get("/login")
    assert resp.status_code == 200
    body = resp.text
    assert 'input type="password"' in body or 'input type=password' in body or 'type="password"' in body
    # Form should POST to /login
    assert 'method="post"' in body or "method='post'" in body or 'action="/login"' in body


def test_get_login_next_param_echoed_in_hidden_input(client: TestClient):
    """GET /login?next=/api/v1/command/execution/abc/view echoes path in hidden next input."""
    next_path = "/api/v1/command/execution/abc/view"
    resp = client.get(f"/login?next={next_path}")
    assert resp.status_code == 200
    # The path substring must appear as the hidden input value
    assert next_path in resp.text


def test_get_login_open_redirect_sanitized(client: TestClient):
    """GET /login?next=//evil.com must NOT echo '//evil.com' as the hidden input value."""
    resp = client.get("/login?next=//evil.com")
    assert resp.status_code == 200
    # safe_next_path should replace '//evil.com' with /docs
    # The literal value must NOT appear as the hidden field value
    assert 'value="//evil.com"' not in resp.text
    assert "value='//evil.com'" not in resp.text
    # The safe fallback must be present
    assert 'value="/docs"' in resp.text


def test_get_login_percent_encoded_slash_redirect_sanitized(client: TestClient):
    """GET /login?next=/%2fevil.com must fall back to /docs (percent-decode bypass).

    A raw /%2f in a query string is decoded by Starlette's query-param layer so
    safe_next_path receives '//evil.com' which is already caught. This test
    confirms the /docs fallback is rendered and no evil.com value leaks out.
    """
    resp = client.get("/login?next=/%2fevil.com")
    assert resp.status_code == 200
    # The safe fallback must be present as the hidden input value
    assert 'value="/docs"' in resp.text
    # evil.com must not appear anywhere in the hidden input value
    assert 'value="/%2fevil.com"' not in resp.text
    assert "value='/%2fevil.com'" not in resp.text
    assert "evil.com" not in resp.text


def test_post_login_percent_encoded_slash_redirect_sanitized():
    """POST /login with next=/%2fevil.com in form body must NOT redirect to that path.

    Unlike GET, multipart form parsing does NOT decode %2f, so safe_next_path
    receives the raw string '/%2fevil.com'. Without the unquote() fix this passes
    the '//' guard and returns '/%2fevil.com' as Location — a browser then
    normalises /%2f to // → off-site redirect. After the fix, it must fall back to /docs.
    """
    resp = _no_follow_client.post(
        "/login",
        data={
            "username": "test_admin",
            "password": "secret",
            "next": "/%2fevil.com",
        },
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    # Must NOT redirect to the percent-encoded bypass path
    assert location != "/%2fevil.com", (
        "safe_next_path allowed /%2fevil.com as redirect target — open redirect via %2f bypass"
    )
    # Must fall back to the safe /docs path
    assert location == "/docs", f"Expected /docs fallback but got {location!r}"


# ── POST /login ────────────────────────────────────────────────────────────────

def test_post_login_correct_creds_redirects_to_next():
    """POST /login with correct creds + next path → 303 Location = next, sets cookie."""
    next_path = "/api/v1/command/execution/abc/view"
    resp = _no_follow_client.post(
        "/login",
        data={
            "username": "test_admin",
            "password": "secret",
            "next": next_path,
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == next_path
    # access_token cookie must be set
    assert "access_token" in resp.cookies


def test_post_login_wrong_creds_rerenders_form():
    """POST /login with wrong creds returns 200, re-renders form with error, no cookie."""
    resp = _no_follow_client.post(
        "/login",
        data={
            "username": "test_admin",
            "password": "wrongpassword",
            "next": "/docs",
        },
    )
    assert resp.status_code == 200
    body = resp.text
    # Should re-render the form (has password input)
    assert 'type="password"' in body
    # Should show error message
    assert "error" in body.lower() or "invalid" in body.lower()
    # Must NOT set the access_token cookie
    assert "access_token" not in resp.cookies
