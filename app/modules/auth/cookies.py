"""Session tokens travel only in cookies, never in a response body.

    governix_access   HttpOnly  the access JWT; expires with it
    governix_refresh  HttpOnly  the opaque refresh token; ends with the browser session
    governix_csrf     readable  the page echoes it in X-CSRF-Token on every unsafe request

All three are SameSite=Strict. Because the browser sends cookies by itself, an
unsafe request must also carry the CSRF header: another site can make the
browser send the cookies but cannot read the CSRF value to send with them. The
access JWT carries its own CSRF value, so only the header issued with that
token is accepted.
"""

import secrets

from fastapi import Request, Response

from app.core.config import Settings
from app.core.exceptions import PermissionDeniedError
from app.modules.auth.schema import TokenPair

ACCESS_COOKIE = "governix_access"
REFRESH_COOKIE = "governix_refresh"
CSRF_COOKIE = "governix_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _secure(request: Request, settings: Settings) -> bool:
    if settings.AUTH_COOKIE_SECURE is not None:
        return settings.AUTH_COOKIE_SECURE
    return request.url.scheme == "https"


def set_auth_cookies(response: Response, request: Request, settings: Settings, tokens: TokenPair) -> None:
    secure = _secure(request, settings)
    response.set_cookie(
        ACCESS_COOKIE, tokens.access_token, max_age=tokens.expires_in,
        httponly=True, secure=secure, samesite="lax",
    )
    response.set_cookie(REFRESH_COOKIE, tokens.refresh_token, httponly=True, secure=secure, samesite="lax")
    response.set_cookie(CSRF_COOKIE, tokens.csrf_token, secure=secure, samesite="lax")


def clear_auth_cookies(response: Response, request: Request, settings: Settings) -> None:
    secure = _secure(request, settings)
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, httponly=name != CSRF_COOKIE, secure=secure, samesite="lax")


def csrf_matches(request: Request, expected: str | None) -> bool:
    sent = request.headers.get(CSRF_HEADER)
    return bool(sent and expected) and secrets.compare_digest(sent, expected)


def require_csrf(request: Request, expected: str | None) -> None:
    """Unsafe methods must echo the CSRF value issued with the session."""
    if request.method not in SAFE_METHODS and not csrf_matches(request, expected):
        raise PermissionDeniedError(
            "Missing or invalid CSRF token. Reload the page and try again.", code="CSRF_FAILED"
        )
