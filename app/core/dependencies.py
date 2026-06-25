"""
app/core/dependencies.py

FastAPI dependency factory for scope-based access control.

Usage in routes:
    @router.get("/protected")
    def protected(user: User = Depends(get_current_user(["required_scope"]))):
        ...
"""

from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import unquote

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from app.core.exceptions import AuthException, ForbiddenException
from app.core.security import decode_access_token
from app.domain.models import User

# The tokenUrl must match your actual POST /token route path.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

# Cookie name set by POST /token so a browser opening an HTML log viewer can
# authenticate its same-origin fetch() calls (which cannot carry a Bearer header).
ACCESS_TOKEN_COOKIE = "access_token"


def set_access_cookie(response, token: str, max_age: int) -> None:
    """Set the access_token cookie used by HTML viewers."""
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False,  # served over http in dev; set True behind TLS in prod
    )


def safe_next_path(next_path: str | None, fallback: str = "/docs") -> str:
    """Open-redirect guard for the login ``next`` param.

    Only same-origin, absolute *paths* are allowed (must start with a single
    '/'). Anything that could redirect off-site ('//host', 'http://host',
    backslashes, missing leading slash) falls back to a safe local default.

    The value is percent-decoded before the guard checks are applied so that
    encoded bypass attempts like ``/%2fevil.com`` (which a browser normalises to
    ``//evil.com``) are caught. Both one-level and double-encoded inputs are
    checked: if either the once-decoded or twice-decoded form fails a guard,
    the fallback is returned. The once-decoded value is returned on success.
    """
    if not next_path:
        return fallback
    decoded = unquote(next_path)
    double = unquote(decoded)
    for candidate in (decoded, double):
        if not candidate.startswith("/"):
            return fallback
        if candidate.startswith("//") or candidate.startswith("/\\"):
            return fallback
    return decoded


def get_current_user(required_scopes: list[str] | None = None) -> Callable:
    """Dependency factory that validates JWT and enforces scope requirements.

    Args:
        required_scopes: List of scope strings the token must contain ALL of.
                         Pass an empty list or None to only validate the token.

    Returns:
        A FastAPI dependency callable that resolves to the authenticated ``User``.

    Raises:
        AuthException:   Token is missing, invalid, or expired.
        ForbiddenException: Token is valid but lacks a required scope.
    """
    required: list[str] = required_scopes or []

    async def _dependency(token: str = Depends(oauth2_scheme)) -> User:
        payload = decode_access_token(token)  # raises AuthException on failure

        account: str | None = payload.get("sub")
        scopes: list[str] = payload.get("scopes", [])

        if not account:
            raise AuthException(
                "Token is missing subject claim.",
            )

        missing = [s for s in required if s not in scopes]
        if missing:
            raise ForbiddenException(
                f"Token is missing required scopes: {missing}",
                detail={"required": required, "missing": missing},
            )

        return User(account=account, scopes=scopes)

    return _dependency


def get_current_user_cookie_or_header(
    required_scopes: list[str] | None = None,
) -> Callable:
    """Like get_current_user but also accepts the JWT from the access_token cookie.

    HTML log viewers open an unauthed page whose fetch() cannot set an
    Authorization header but DOES send the same-origin cookie. The header wins
    if both are present.
    """
    required: list[str] = required_scopes or []

    async def _dependency(
        request: Request,
        header_token: Optional[str] = Depends(
            OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)
        ),
    ) -> User:
        token = header_token or request.cookies.get(ACCESS_TOKEN_COOKIE)
        if not token:
            raise AuthException("Not authenticated.")

        payload = decode_access_token(token)
        account: str | None = payload.get("sub")
        scopes: list[str] = payload.get("scopes", [])
        if not account:
            raise AuthException("Token is missing subject claim.")
        missing = [s for s in required if s not in scopes]
        if missing:
            raise ForbiddenException(
                f"Token is missing required scopes: {missing}",
                detail={"required": required, "missing": missing},
            )
        return User(account=account, scopes=scopes)

    return _dependency
