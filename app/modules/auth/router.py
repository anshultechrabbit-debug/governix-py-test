from typing import Annotated

from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import AuthenticationError
from app.core.security import MFA_STEP_EXPIRED
from app.core.rate_limit import RateLimiter
from app.core.responses import ApiResponse, error_response, ok
from app.infrastructure.mail.smtp import send_email
from app.modules.auth.cookies import (
    CSRF_COOKIE,
    MFA_COOKIE,
    REFRESH_COOKIE,
    clear_auth_cookies,
    clear_mfa_cookie,
    csrf_matches,
    require_csrf,
    set_auth_cookies,
    set_mfa_cookie,
)
from app.modules.auth.dependencies import CurrentPrincipal
from app.modules.auth.schema import (
    AuthSession,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    Me,
    Message,
    MfaChallenge,
    MfaCode,
    MfaDisable,
    MfaSetup,
    MfaStatus,
    RecoveryCodes,
    ResetPasswordRequest,
)
from app.modules.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AuthService:
    return AuthService(db, settings)


Service = Annotated[AuthService, Depends(get_auth_service)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]

# Forgot password sends email to whoever is named, so it is limited per address and per client.
_RESET_LIMIT = "Too many password reset requests. Please wait a few minutes and try again."
_reset_by_email = RateLimiter(3, 15 * 60, message=_RESET_LIMIT)
_reset_by_client = RateLimiter(10, 15 * 60, message=_RESET_LIMIT)
RESET_SENT = "If an account uses that email, we have sent it a link to reset the password."


# Codes are 6 digits: besides the account lockout, limit how fast anyone can guess them.
_MFA_LIMIT = "Too many code attempts. Please wait a few minutes and try again."
_mfa_by_client = RateLimiter(30, 5 * 60, message=_MFA_LIMIT)
_mfa_by_user = RateLimiter(10, 5 * 60, message=_MFA_LIMIT)


@router.post("/login", response_model=ApiResponse[AuthSession])
def login(body: LoginRequest, request: Request, response: Response, service: Service, settings: AppSettings):
    """Signs in, or, for an account with two-step sign-in, answers `mfa_required` and opens the code step:
    send the code to /auth/mfa/verify."""
    result = service.login(body.email, body.password)
    if isinstance(result, MfaChallenge):
        set_mfa_cookie(response, request, settings, result.token, result.expires_in)
        return ok(AuthSession(expires_in=result.expires_in, mfa_required=True))
    clear_mfa_cookie(response, request, settings)
    set_auth_cookies(response, request, settings, result)
    return ok(AuthSession(expires_in=result.expires_in))


@router.post("/mfa/verify", response_model=ApiResponse[AuthSession])
def verify_mfa(body: MfaCode, request: Request, response: Response, service: Service, settings: AppSettings):
    """The code step of a two-step sign-in: a code from the authenticator app, or a backup code. Wrong codes
    count toward the account lockout, as wrong passwords do."""
    _mfa_by_client.check(request.client.host if request.client else "unknown")
    challenge = request.cookies.get(MFA_COOKIE)
    if not challenge:
        raise AuthenticationError(MFA_STEP_EXPIRED, code="MFA_CHALLENGE_EXPIRED")
    tokens = service.verify_mfa(challenge, body.code)
    clear_mfa_cookie(response, request, settings)
    set_auth_cookies(response, request, settings, tokens)
    return ok(AuthSession(expires_in=tokens.expires_in))


@router.get("/mfa", response_model=ApiResponse[MfaStatus])
def mfa_status(principal: CurrentPrincipal, service: Service):
    return ok(service.mfa_status(principal))


@router.post("/mfa/setup", response_model=ApiResponse[MfaSetup])
def mfa_setup(principal: CurrentPrincipal, service: Service):
    """Start setting up an authenticator app: scan the QR code (`otpauth_uri`) or type `secret`, then confirm with
    /auth/mfa/enable. Until then sign-in is unchanged."""
    return ok(service.mfa_setup(principal))


@router.post("/mfa/enable", response_model=ApiResponse[RecoveryCodes])
def mfa_enable(body: MfaCode, principal: CurrentPrincipal, service: Service):
    """Confirm the setup with a code from the app. Returns 10 one-time backup codes, shown only now."""
    _mfa_by_user.check(str(principal.user_id))
    return ok(RecoveryCodes(codes=service.mfa_enable(principal, body.code)))


@router.post("/mfa/disable", response_model=ApiResponse[MfaStatus])
def mfa_disable(body: MfaDisable, principal: CurrentPrincipal, service: Service):
    """Turn two-step sign-in off. Needs the password and a current code (or a backup code)."""
    _mfa_by_user.check(str(principal.user_id))
    return ok(service.mfa_disable(principal, body.password, body.code))


@router.post("/mfa/recovery-codes", response_model=ApiResponse[RecoveryCodes])
def mfa_recovery_codes(body: MfaCode, principal: CurrentPrincipal, service: Service):
    """New backup codes (the old ones stop working). Needs a code from the app."""
    _mfa_by_user.check(str(principal.user_id))
    return ok(RecoveryCodes(codes=service.mfa_new_recovery_codes(principal, body.code)))


@router.post("/refresh", response_model=ApiResponse[AuthSession])
def refresh(request: Request, response: Response, service: Service, settings: AppSettings):
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    from_cookie = bool(refresh_token)
    if not refresh_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            refresh_token = auth_header[7:].strip()
    if not refresh_token:
        raise AuthenticationError("Authentication required.")
    if from_cookie:
        # A refresh by cookie must come from the page: the CSRF cookie is echoed in the header. Without the CSRF
        # cookie there is nothing to echo, and the request is refused rather than let through.
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


@router.post("/forgot-password", response_model=ApiResponse[Message])
def forgot_password(
    body: ForgotPasswordRequest, request: Request, background: BackgroundTasks, service: Service,
    settings: AppSettings,
):
    """Email a single-use reset link. The answer is the same whether or not the email is registered."""
    _reset_by_client.check(request.client.host if request.client else "unknown")
    _reset_by_email.check(body.email.lower())
    issued = service.forgot_password(body.email)
    if issued is not None:
        user, token = issued
        link = f"{settings.PASSWORD_RESET_URL}?{urlencode({'token': token})}"
        background.add_task(
            send_email, settings, to=user.email, subject=f"Reset your {settings.APP_NAME} password",
            body=(
                f"Hello {user.full_name},\n\n"
                f"We received a request to reset your {settings.APP_NAME} password. Open this link within "
                f"{settings.PASSWORD_RESET_TTL_MINUTES} minutes to choose a new one:\n\n{link}\n\n"
                "The link works once. If you did not ask for this, ignore this email: your password stays"
                " the same.\n"
            ),
        )
    return ok(Message(message=RESET_SENT))


@router.post("/reset-password", response_model=ApiResponse[Message])
def reset_password(
    body: ResetPasswordRequest, request: Request, response: Response, service: Service, settings: AppSettings,
):
    """Set a new password from a reset link. Every session ends; the user signs in with the new password."""
    service.reset_password(body.token, body.new_password)
    clear_auth_cookies(response, request, settings)
    return ok(Message(message="Your password has been reset. Sign in with your new password."))
