from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import AuthenticationError
from app.core.responses import ApiResponse, error_response, ok
from app.modules.auth.cookies import (
    CSRF_COOKIE,
    REFRESH_COOKIE,
    clear_auth_cookies,
    csrf_matches,
    require_csrf,
    set_auth_cookies,
)
from app.modules.auth.dependencies import CurrentPrincipal
from app.modules.auth.schema import AuthSession, ChangePasswordRequest, LoginRequest, Me
from app.modules.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AuthService:
    return AuthService(db, settings)


Service = Annotated[AuthService, Depends(get_auth_service)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]


@router.post("/login", response_model=ApiResponse[AuthSession])
def login(body: LoginRequest, request: Request, response: Response, service: Service, settings: AppSettings):
    tokens = service.login(body.email, body.password)
    set_auth_cookies(response, request, settings, tokens)
    return ok(AuthSession(expires_in=tokens.expires_in))


@router.post("/refresh", response_model=ApiResponse[AuthSession])
def refresh(request: Request, response: Response, service: Service, settings: AppSettings):
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            refresh_token = auth_header[7:].strip()
    if not refresh_token:
        raise AuthenticationError("Authentication required.")
    if request.cookies.get(CSRF_COOKIE):
        require_csrf(request, request.cookies.get(CSRF_COOKIE))
    try:
        tokens = service.refresh(refresh_token)
    except AuthenticationError as exc:
        if exc.code == "TOKEN_ROTATED":
            raise  # the cookies now hold another tab's fresh session; keep them
        # The session is over: drop its cookies so the page stops trying to restore it.
        failed = error_response(exc.status_code, exc.code, exc.message, exc.details)
        clear_auth_cookies(failed, request, settings)
        return failed
    set_auth_cookies(response, request, settings, tokens)
    return ok(AuthSession(expires_in=tokens.expires_in))


@router.post("/logout", response_model=ApiResponse[dict])
def logout(request: Request, response: Response, service: Service, settings: AppSettings):
    # Signing out always clears the cookies; only a request from the page itself revokes the session.
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if refresh_token and csrf_matches(request, request.cookies.get(CSRF_COOKIE)):
        service.logout(refresh_token)
    clear_auth_cookies(response, request, settings)
    return ok({})


@router.get("/me", response_model=ApiResponse[Me])
def me(principal: CurrentPrincipal, service: Service):
    return ok(service.me(principal))


@router.post("/change-password", response_model=ApiResponse[dict])
def change_password(
    body: ChangePasswordRequest, principal: CurrentPrincipal, request: Request, response: Response,
    service: Service, settings: AppSettings,
):
    service.change_password(principal, body.current_password, body.new_password)
    clear_auth_cookies(response, request, settings)  # every session has just ended, this one too
    return ok({})
