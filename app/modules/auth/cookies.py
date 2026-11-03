"""Session tokens travel only in cookies, never in a response body.

    governix_access   HttpOnly  the access JWT; expires with it
    governix_refresh  HttpOnly  the opaque refresh token; ends with the browser session
    governix_csrf     readable  the page echoes it in X-CSRF-Token on every unsafe request

All three are SameSite=Lax. Because the browser sends cookies by itself, an
unsafe request authenticated by cookie must always carry the CSRF header (see
auth.dependencies): another page can make the browser send the cookies but
cannot read the CSRF value to send with them. Lax alone is not enough: on an IP
address every port is the same site. The access JWT carries its own CSRF value,
so only the header issued with that token is accepted.
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


def _samesite(settings: Settings) -> str:
    """AUTH_COOKIE_SAMESITE: "lax" (default) or "strict". "none" would send the session with every cross-site
    request, so it is not accepted."""
    value = settings.AUTH_COOKIE_SAMESITE.lower()
    return value if value in ("lax", "strict") else "lax"


def set_auth_cookies(response: Response, request: Request, settings: Settings, tokens: TokenPair) -> None:
    secure, samesite = _secure(request, settings), _samesite(settings)
    response.set_cookie(
        ACCESS_COOKIE, tokens.access_token, max_age=tokens.expires_in,
        httponly=True, secure=secure, samesite=samesite,
    )
    response.set_cookie(REFRESH_COOKIE, tokens.refresh_token, httponly=True, secure=secure, samesite=samesite)
    response.set_cookie(CSRF_COOKIE, tokens.csrf_token, secure=secure, samesite=samesite)


def clear_auth_cookies(response: Response, request: Request, settings: Settings) -> None:
    secure, samesite = _secure(request, settings), _samesite(settings)
    for name in (ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, httponly=name != CSRF_COOKIE, secure=secure, samesite=samesite)


def csrf_matches(request: Request, expected: str | None) -> bool:
    sent = request.headers.get(CSRF_HEADER)
    return bool(sent and expected) and secrets.compare_digest(sent, expected)


def require_csrf(request: Request, expected: str | None) -> None:
    """Unsafe methods must echo the CSRF value issued with the session."""
    if request.method not in SAFE_METHODS and not csrf_matches(request, expected):
        raise PermissionDeniedError(
            "Missing or invalid CSRF token. Reload the page and try again.", code="CSRF_FAILED"
        )
