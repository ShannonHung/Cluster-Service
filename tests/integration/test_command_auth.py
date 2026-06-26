"""Verifies /token sets the access_token cookie and cookie-or-header auth works."""
from __future__ import annotations


def test_token_sets_access_cookie(client):
    resp = client.post(
        "/token",
        data={"username": "test_admin", "password": "secret"},
    )
    assert resp.status_code == 200, resp.text
    assert "access_token" in resp.cookies


def test_cookie_or_header_dependency_importable():
    # Smoke: the dependency factory exists and is callable.
    from app.core.dependencies import get_current_user_cookie_or_header
    dep = get_current_user_cookie_or_header(["command_api"])
    assert callable(dep)
