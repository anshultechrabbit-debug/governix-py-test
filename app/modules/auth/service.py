import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ConflictError, ValidationError
from app.core.inputs import check_password
from app.core.security import (
    INVALID_RESET_LINK,
    MFA_STEP_EXPIRED,
    create_access_token,
    create_mfa_challenge_token,
    create_password_reset_token,
    decode_mfa_challenge_token,
    decode_password_reset_token,
    generate_opaque_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    reset_token_matches,
    verify_password,
)
from app.modules.audit.service import record_event
from app.modules.auth.dependencies import principal_from_user
from app.modules.auth import alerts, mfa
from app.modules.auth.model import MfaRecoveryCode, RefreshToken
from app.modules.auth.permissions import Principal, Role
from app.modules.auth.schema import Me, MfaChallenge, MfaSetup, MfaStatus, TokenPair
from app.modules.branches.model import Branch
from app.modules.organizations.model import Organization, OrganizationStatus
from app.modules.users.model import User

INVALID_CREDENTIALS = "Invalid email or password."
INVALID_MFA_CODE = "That code is not right. Enter the current code from your authenticator app, or a backup code."
# Tabs of one browser share the refresh cookie, so two of them can refresh at the same moment.
# The one that loses presents a token rotated this long ago; that is a race, not theft.
ROTATION_GRACE = timedelta(seconds=30)


# Shown once the password is right (or the session was valid): wrong passwords still get INVALID_CREDENTIALS, so
# nobody learns which emails belong to suspended accounts by guessing.
ORGANIZATION_SUSPENDED = ("Your organization's access to Governix is suspended. Contact your organization's "
                          "administrator or Governix support.")
_WHO_RESTORES = {Role.DEPARTMENT_USER: "your Branch Manager", Role.BRANCH_MANAGER: "your Organization Admin",
                 Role.ORG_ADMIN: "Governix support (your Master Admin)", Role.SUPPORT_AGENT: "your Master Admin"}


def account_suspended(user: User) -> AuthenticationError:
    who = _WHO_RESTORES.get(Role(user.role), "your administrator") if user.role in set(Role) else "your administrator"
    return AuthenticationError(f"Your account is suspended. Contact {who} to restore access.",
                               code="ACCOUNT_SUSPENDED")


def _is_active(organization: Organization | None) -> bool:
    return (
        organization is not None and organization.deleted_at is None
        and organization.status == OrganizationStatus.ACTIVE
    )


class AuthService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def login(self, email: str, password: str) -> TokenPair | MfaChallenge:
        """Tokens, or, for an account with two-step sign-in, the challenge for its code step (no session yet)."""
        user = self.session.scalar(
            select(User).where(func.lower(User.email) == email.lower(), User.deleted_at.is_(None)).with_for_update()
        )
        now = datetime.now(UTC)

        # LOCKOUT DISABLED (2026-11-05, for now): the temporary lock after repeated failed logins is switched off.
        # To turn it back on, uncomment the three "LOCKOUT DISABLED" blocks in this file and the two skipped tests.
        # if user is not None and user.locked_until and user.locked_until > now:
        #     verify_password(password, None)  # keep timing uniform
        #     raise AuthenticationError(
        #         "Account temporarily locked after repeated failed logins.", code="ACCOUNT_LOCKED"
        #     )

        if not verify_password(password, user.password_hash if user else None):
            if user is not None:
                self._register_failure(user, now)
            raise AuthenticationError(INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")

        if not user.is_active:
            raise account_suspended(user)
        if user.organization_id is not None:
            organization = self.session.get(Organization, user.organization_id)
            if not _is_active(organization):
                raise AuthenticationError(ORGANIZATION_SUSPENDED, code="ORGANIZATION_SUSPENDED")

        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        if user.mfa_enabled_at is not None:
            # The failed-attempt count stays until the code is accepted too: wrong codes add to it.
            token, expires_in = create_mfa_challenge_token(self.settings, user_id=user.id, token_version=user.token_version)
            self.session.commit()
            return MfaChallenge(token=token, expires_in=expires_in)
        return self._complete_login(user, now)

    def verify_mfa(self, challenge: str, code: str) -> TokenPair:
        """The code step of two-step sign-in: an authenticator code, or a backup code, then the session."""
        claims = decode_mfa_challenge_token(self.settings, challenge)
        try:
            user_id = uuid.UUID(claims["sub"])
        except ValueError:
            raise AuthenticationError(MFA_STEP_EXPIRED, code="MFA_CHALLENGE_EXPIRED") from None
        user = self.session.get(User, user_id, with_for_update=True)
        now = datetime.now(UTC)
        # Signed out everywhere, deactivated or reset since the password step: start again.
        if (
            user is None or user.deleted_at is not None or user.mfa_enabled_at is None
            or user.token_version != claims.get("tv")
        ):
            raise AuthenticationError(MFA_STEP_EXPIRED, code="MFA_CHALLENGE_EXPIRED")
        if not user.is_active:
            raise account_suspended(user)
        # LOCKOUT DISABLED (for now), see login().
        # if user.locked_until and user.locked_until > now:
        #     raise AuthenticationError("Account temporarily locked after repeated failed logins.", code="ACCOUNT_LOCKED")
        if user.organization_id is not None and not _is_active(self.session.get(Organization, user.organization_id)):
            raise AuthenticationError(ORGANIZATION_SUSPENDED, code="ORGANIZATION_SUSPENDED")
        used = self._accept_code(user, code, now)
        if used is None:
            message = self._refusal(user, code)
            self._register_failure(user, now, mfa=True)
            raise AuthenticationError(message, code="INVALID_MFA_CODE")
        return self._complete_login(user, now, mfa=used)

    def _complete_login(self, user: User, now: datetime, *, mfa: str | None = None) -> TokenPair:
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        tokens, _ = self._issue_tokens(user, family_id=uuid.uuid4())
        record_event(
            self.session,
            "auth.login",
            actor=principal_from_user(user, self.session),
            resource_type="user",
            resource_id=user.id,
            details={"two_step": mfa} if mfa else None,
        )
        self.session.commit()
        return tokens

    # --- two-step sign-in: the person's own settings -----------------------------------------------------

    def mfa_status(self, principal: Principal) -> MfaStatus:
        user = self.session.get(User, principal.user_id)
        return MfaStatus(enabled=user.mfa_enabled_at is not None, enabled_at=user.mfa_enabled_at,
                         recovery_codes_left=self._codes_left(user.id) if user.mfa_enabled_at else 0)

    def mfa_setup(self, principal: Principal) -> MfaSetup:
        """A new secret for the authenticator app to scan. Nothing changes at sign-in until a code from it is
        confirmed (`mfa_enable`); starting again replaces the unconfirmed secret."""
        user = self.session.get(User, principal.user_id, with_for_update=True)
        if user.mfa_enabled_at is not None:
            raise ConflictError("Two-step sign-in is already on. Turn it off first to move it to another phone.",
                                code="MFA_ALREADY_ENABLED")
        secret = mfa.new_secret()
        user.mfa_secret, user.mfa_last_step = mfa.encrypt(self.settings, secret), None
        self.session.commit()
        label = datetime.now(UTC).strftime("%d %b %Y")
        return MfaSetup(secret=secret, otpauth_uri=mfa.provisioning_uri(secret, email=user.email,
                                                                        issuer=self.settings.APP_NAME, label=label))

    def mfa_enable(self, principal: Principal, code: str) -> list[str]:
        """Turn two-step sign-in on with a first code from the app. Returns the backup codes, shown once."""
        user = self.session.get(User, principal.user_id, with_for_update=True)
        if user.mfa_enabled_at is not None:
            raise ConflictError("Two-step sign-in is already on.", code="MFA_ALREADY_ENABLED")
        secret = mfa.decrypt(self.settings, user.mfa_secret)
        if secret is None:
            raise ValidationError("Start the setup again: scan a new QR code.", code="MFA_NOT_SET_UP")
        step = mfa.accepted_step(secret, code, last_step=None)
        if step is None:
            raise ValidationError(mfa.refusal(secret, code, last_step=None, email=user.email).replace(
                "use the newest Governix entry", "use the entry you just scanned"), code="INVALID_MFA_CODE")
        user.mfa_enabled_at, user.mfa_last_step = datetime.now(UTC), step
        codes = self._replace_recovery_codes(user)
        record_event(self.session, "auth.mfa_enabled", actor=principal, resource_type="user", resource_id=user.id)
        alerts.two_step_changed(self.session, user, "enabled")
        self.session.commit()
        return codes

    def mfa_disable(self, principal: Principal, password: str, code: str) -> MfaStatus:
        """Turn it off: needs the password and a current code from the app."""
        user = self.session.get(User, principal.user_id, with_for_update=True)
        if user.mfa_enabled_at is None:
            raise ValidationError("Two-step sign-in is not on.", code="MFA_NOT_ENABLED")
        if not verify_password(password, user.password_hash):
            raise ValidationError("Password is incorrect.", code="INVALID_CREDENTIALS")
        # Only a code from the app: a backup code (perhaps stolen with the password) signs in, it never removes
        # the protection. Lost the phone? An admin resets it.
        if self._accept_code(user, code, datetime.now(UTC), recovery=False) is None:
            message = ("Backup codes can't turn two-step sign-in off: enter the code your authenticator app shows. "
                       "Lost your phone? Ask your admin to reset it.") if mfa.looks_like_recovery(code) else \
                self._refusal(user, code)
            raise ValidationError(message, code="INVALID_MFA_CODE")
        clear_mfa(self.session, user)
        record_event(self.session, "auth.mfa_disabled", actor=principal, resource_type="user", resource_id=user.id)
        alerts.two_step_changed(self.session, user, "disabled")
        self.session.commit()
        return MfaStatus(enabled=False, enabled_at=None, recovery_codes_left=0)

    def mfa_new_recovery_codes(self, principal: Principal, code: str) -> list[str]:
        """Replace every backup code (the old ones stop working). Needs a code from the app itself."""
        user = self.session.get(User, principal.user_id, with_for_update=True)
        if user.mfa_enabled_at is None:
            raise ValidationError("Two-step sign-in is not on.", code="MFA_NOT_ENABLED")
        if self._accept_code(user, code, datetime.now(UTC), recovery=False) is None:
            raise ValidationError(self._refusal(user, code), code="INVALID_MFA_CODE")
        codes = self._replace_recovery_codes(user)
        record_event(self.session, "auth.mfa_recovery_codes_renewed", actor=principal, resource_type="user",
                     resource_id=user.id)
        alerts.two_step_changed(self.session, user, "recovery_renewed")
        self.session.commit()
        return codes

    def _accept_code(self, user: User, code: str, now: datetime, *, recovery: bool = True) -> str | None:
        """"app" or "recovery" when the code is accepted (and spent), None otherwise."""
        secret = mfa.decrypt(self.settings, user.mfa_secret)
        if secret is not None:
            step = mfa.accepted_step(secret, code, last_step=user.mfa_last_step)
            if step is not None:
                user.mfa_last_step = step
                return "app"
        if recovery and mfa.looks_like_recovery(code):
            stored = self.session.scalar(
                select(MfaRecoveryCode).where(
                    MfaRecoveryCode.user_id == user.id, MfaRecoveryCode.code_hash == mfa.hash_recovery(code),
                    MfaRecoveryCode.used_at.is_(None),
                ).with_for_update()
            )
            if stored is not None:
                stored.used_at = now
                self.session.flush()
                left = self._codes_left(user.id)
                record_event(self.session, "auth.mfa_recovery_used", organization_id=user.organization_id,
                             resource_type="user", resource_id=user.id, details={"left": left})
                alerts.two_step_changed(self.session, user, "recovery_used", backup_codes_left=left)
                return "recovery"
        return None

    def _refusal(self, user: User, code: str) -> str:
        return mfa.refusal(mfa.decrypt(self.settings, user.mfa_secret), code, last_step=user.mfa_last_step,
                           email=user.email)

    def _codes_left(self, user_id: uuid.UUID) -> int:
        return self.session.scalar(
            select(func.count()).select_from(MfaRecoveryCode)
            .where(MfaRecoveryCode.user_id == user_id, MfaRecoveryCode.used_at.is_(None))
        ) or 0

    def _replace_recovery_codes(self, user: User) -> list[str]:
        self.session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
        codes = mfa.new_recovery_codes()
        self.session.add_all(MfaRecoveryCode(user_id=user.id, code_hash=mfa.hash_recovery(c)) for c in codes)
        return codes

    def refresh(self, refresh_token: str) -> TokenPair:
        now = datetime.now(UTC)
        stored = self.session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_token(refresh_token))
            .with_for_update()
        )
        if stored is None:
            raise AuthenticationError("Invalid refresh token.", code="INVALID_TOKEN")
        if stored.revoked_at is not None:
            if stored.replaced_by_id is not None and now - stored.revoked_at < ROTATION_GRACE:
                # Another tab just rotated it; the browser already holds its new cookies.
                raise AuthenticationError("Session was just refreshed.", code="TOKEN_ROTATED")
            # A rotated token was presented again: assume theft and end the whole session family.
            self._revoke_family(stored.family_id, now)
            record_event(
                self.session,
                "auth.refresh_token_reuse",
                resource_type="user",
                resource_id=stored.user_id,
                organization_id=self._organization_of(stored.user_id),
            )
            self.session.commit()
            raise AuthenticationError("Invalid refresh token.", code="INVALID_TOKEN")
        if stored.expires_at <= now:
            raise AuthenticationError("Refresh token expired.", code="TOKEN_EXPIRED")

        user = self.session.get(User, stored.user_id)
        if user is None or user.deleted_at is not None or not user.is_active:
            raise AuthenticationError("Invalid refresh token.", code="INVALID_TOKEN")
        if user.organization_id is not None:
            organization = self.session.get(Organization, user.organization_id)
            if not _is_active(organization):
                raise AuthenticationError(ORGANIZATION_SUSPENDED, code="ORGANIZATION_SUSPENDED")

        tokens, new_token_id = self._issue_tokens(user, family_id=stored.family_id)
        stored.revoked_at = now
        stored.replaced_by_id = new_token_id
        self.session.commit()
        return tokens

    def logout(self, refresh_token: str) -> None:
        stored = self.session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
        )
        if stored is not None:
            self._revoke_family(stored.family_id, datetime.now(UTC))
            self.session.commit()

    def change_password(self, principal: Principal, current: str, new: str) -> None:
        user = self.session.get(User, principal.user_id, with_for_update=True)
        if not verify_password(current, user.password_hash):
            raise ValidationError("Current password is incorrect.", code="INVALID_CREDENTIALS")
        if current == new:
            raise ValidationError("New password must differ from the current password.")
        check_password(new, email=user.email, name=user.full_name)
        user.password_hash = hash_password(new)
        # Log out every session, including this one's other devices.
        self._end_sessions(user)
        record_event(
            self.session, "auth.password_changed", actor=principal, resource_type="user",
            resource_id=user.id,
        )
        self.session.commit()

    def forgot_password(self, email: str) -> tuple[User, str] | None:
        """The account and a reset token for it, or None when no one may reset by this email.

        The caller answers the same either way, so the response never reveals whether an email is
        registered. Deactivated or deleted accounts, and suspended organizations, get no link.
        """
        user = self.session.scalar(
            select(User).where(func.lower(User.email) == email.lower(), User.deleted_at.is_(None))
        )
        if user is None or not user.is_active:
            return None
        if user.organization_id is not None and not _is_active(self.session.get(Organization, user.organization_id)):
            return None
        token = create_password_reset_token(
            self.settings, user_id=user.id, token_version=user.token_version, password_hash=user.password_hash
        )
        record_event(
            self.session, "auth.password_reset_requested", organization_id=user.organization_id,
            resource_type="user", resource_id=user.id,
        )
        self.session.commit()
        return user, token

    def reset_password(self, token: str, new_password: str) -> None:
        """Set the password a reset link was sent for. The link works once, and every session ends."""
        claims = decode_password_reset_token(self.settings, token)
        try:
            user_id = uuid.UUID(claims["sub"])
        except ValueError:
            raise ValidationError(INVALID_RESET_LINK, code="INVALID_RESET_TOKEN") from None
        user = self.session.get(User, user_id, with_for_update=True)
        if (
            user is None or user.deleted_at is not None or not user.is_active
            or not reset_token_matches(claims, token_version=user.token_version, password_hash=user.password_hash)
        ):
            raise ValidationError(INVALID_RESET_LINK, code="INVALID_RESET_TOKEN")
        if verify_password(new_password, user.password_hash):
            raise ValidationError("New password must differ from the current password.")
        check_password(new_password, email=user.email, name=user.full_name)
        user.password_hash = hash_password(new_password)
        # A forgotten password ends the lockout it may have caused.
        user.failed_login_attempts = 0
        user.locked_until = None
        self._end_sessions(user)
        record_event(
            self.session, "auth.password_reset_completed", actor=principal_from_user(user, self.session),
            resource_type="user", resource_id=user.id,
        )
        self.session.commit()

    def _end_sessions(self, user: User) -> None:
        """Invalidate every access token and revoke every refresh token of the user."""
        user.token_version += 1
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    def me(self, principal: Principal) -> Me:
        user = self.session.get(User, principal.user_id)
        organization = (
            self.session.get(Organization, user.organization_id) if user.organization_id else None
        )
        branch = self.session.get(Branch, user.branch_id) if user.branch_id else None
        return Me(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            organization_id=user.organization_id,
            organization_name=organization.name if organization else None,
            branch_id=user.branch_id,
            branch_name=branch.name if branch else None,
            department_id=user.department_id,
            permissions=sorted(principal.permissions),
            mfa_enabled=user.mfa_enabled_at is not None,
        )

    def _issue_tokens(self, user: User, *, family_id: uuid.UUID) -> tuple[TokenPair, uuid.UUID]:
        csrf_token = generate_opaque_token()
        access_token, expires_in = create_access_token(
            self.settings,
            user_id=user.id,
            token_version=user.token_version,
            # The token names its own CSRF value, so a header issued with another session never matches.
            claims={"role": user.role, "org": str(user.organization_id or ""), "csrf": csrf_token},
        )
        refresh_token = generate_opaque_token()
        stored = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_token(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(days=self.settings.REFRESH_TOKEN_TTL_DAYS),
        )
        self.session.add(stored)
        pair = TokenPair(
            access_token=access_token, refresh_token=refresh_token, csrf_token=csrf_token,
            expires_in=expires_in,
        )
        return pair, stored.id

    def _register_failure(self, user: User, now: datetime, *, mfa: bool = False) -> None:
        # Failures are still counted and audited; only the lock is off.
        user.failed_login_attempts += 1
        # LOCKOUT DISABLED (for now), see login().
        # if user.failed_login_attempts >= self.settings.LOGIN_MAX_FAILED_ATTEMPTS:
        #     user.locked_until = now + timedelta(minutes=self.settings.LOGIN_LOCKOUT_MINUTES)
        #     user.failed_login_attempts = 0
        record_event(
            self.session,
            "auth.login_failed",
            organization_id=user.organization_id,
            resource_type="user",
            resource_id=user.id,
            details={"locked": user.locked_until is not None and user.locked_until > now,
                     **({"step": "authenticator code"} if mfa else {})},
        )
        self.session.commit()

    def _revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    def _organization_of(self, user_id: uuid.UUID) -> uuid.UUID | None:
        user = self.session.get(User, user_id)
        return user.organization_id if user else None


def clear_mfa(session: Session, user: User) -> None:
    """Two-step sign-in off: the secret and every backup code are gone (turning it off, or an admin reset)."""
    user.mfa_secret = user.mfa_enabled_at = user.mfa_last_step = None
    session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
