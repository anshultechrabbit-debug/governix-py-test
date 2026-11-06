"""Important actions need a fresh authenticator code (step-up), every time.

The page sends the code in the `X-Authenticator-Code` header. The check runs before the action does anything:
- no two-step sign-in on the account -> STEP_UP_SETUP_REQUIRED (the action is refused; set it up first);
- no code -> STEP_UP_REQUIRED (the page asks for one and sends the request again);
- a wrong code -> STEP_UP_INVALID; a code already used -> STEP_UP_CODE_USED (wait for the next one).
Only codes from the app count, not backup codes: a backup code gets someone back in, it does not approve actions.
The accepted step is saved with the action itself, so a code approves one action once. Failures are audited and
rate-limited. All refusals are 403, never 401 (which would make the page try to renew the session).
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import PermissionDeniedError
from app.core.rate_limit import RateLimiter
from app.modules.audit.service import record_event
from app.modules.auth import mfa
from app.modules.auth.dependencies import CurrentPrincipal
from app.modules.auth.permissions import Principal
from app.modules.users.model import User

STEP_UP_HEADER = "X-Authenticator-Code"
_attempts = RateLimiter(10, 5 * 60, message="Too many code attempts. Please wait a few minutes and try again.")


def confirm_step_up(request: Request, principal: Principal, session: Session, settings: Settings, *,
                    action: str) -> None:
    """Refuse unless the request carries a fresh code from the person's authenticator app."""
    if not settings.STEP_UP_ENABLED:
        return
    user = session.get(User, principal.user_id, with_for_update=True)
    if user is None or user.mfa_enabled_at is None:
        raise PermissionDeniedError(
            "This is an important action: it needs a code from an authenticator app. Turn on two-step sign-in "
            "(account menu > Two-step sign-in), then try again.", code="STEP_UP_SETUP_REQUIRED",
        )
    code = (request.headers.get(STEP_UP_HEADER) or "").strip()
    if not code:
        raise PermissionDeniedError("Enter the code from your authenticator app to confirm this action.",
                                    code="STEP_UP_REQUIRED")
    _attempts.check(str(user.id))
    secret = mfa.decrypt(settings, user.mfa_secret)
    step, reused = mfa.check_code(secret, code, last_step=user.mfa_last_step) if secret else (None, False)
    if step is not None:
        user.mfa_last_step = step  # saved with the action: this code approves nothing else
        return
    record_event(session, "auth.step_up_failed", actor=principal, resource_type="user", resource_id=user.id,
                 details={"action": action, "reused": reused})
    session.commit()  # only the audit event (and the row lock) are pending here
    message = mfa.refusal(secret, code, last_step=user.mfa_last_step, email=user.email)
    if mfa.looks_like_recovery(code):
        message = "Backup codes only sign you in. Enter the code your authenticator app shows."
    raise PermissionDeniedError(message, code="STEP_UP_CODE_USED" if reused else "STEP_UP_INVALID")


def _checker(request: Request, principal: CurrentPrincipal, session: Annotated[Session, Depends(get_db)],
             settings: Annotated[Settings, Depends(get_app_settings)]) -> Callable[[str], None]:
    return lambda action: confirm_step_up(request, principal, session, settings, action=action)


# In an endpoint: `confirm_code: StepUp`, then `confirm_code("user.delete")` as its first step (or once it knows
# the request is the important kind: the real deletion after a preview, a change of role). It shares the request's
# database session, so the spent code is saved with the action. Permissions are checked first, by the endpoint's
# own dependencies, so nobody is asked for a code for something they may not do.
StepUp = Annotated[Callable[[str], None], Depends(_checker)]
