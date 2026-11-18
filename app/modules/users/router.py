import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.users.model import User
from app.modules.users.schema import (
    PasswordReset, TemporaryPassword, UserAccess, UserCreate, UserCreated, UserRead, UserSort, UserStats, UserStatus,
    UserUpdate,
)
from app.modules.users.service import UserService
from app.core.config import Settings
from app.core.dependencies import get_app_settings
from app.modules.email import service as email
from app.modules.auth.step_up import StepUp

router = APIRouter(prefix="/users", tags=["users"])


UserAdmin = Annotated[Principal, Depends(require(Permission.USERS_MANAGE))]
# Changing these needs an authenticator code (auth/step_up.py): who the account is and what it may do. A name, a
# phone and turning an account on or off (is_active, 2026-11-17, the user's choice) are routine.
SENSITIVE_USER_FIELDS = frozenset({"email", "role", "organization_id", "branch_id", "department_id"})


def sensitive_changes(body: UserUpdate, user: User) -> set[str]:
    """The sensitive fields the request really changes. Forms send the whole user: an email or role sent unchanged
    with a status change asks for no code."""
    changed = set()
    for field in body.model_fields_set & SENSITIVE_USER_FIELDS:
        new, old = getattr(body, field), getattr(user, field)
        if field == "email":
            new, old = (new or "").lower(), (old or "").lower()
        if new != old:
            changed.add(field)
    return changed


def get_service(db: Annotated[Session, Depends(get_db)]) -> UserService:
    return UserService(db)


Service = Annotated[UserService, Depends(get_service)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]


def read(user: User, organization_name: str | None, branch_name: str | None, *, can_manage: bool = True) -> UserRead:
    return UserRead.model_validate(user).model_copy(
        update={"organization_name": organization_name, "branch_name": branch_name, "can_manage": can_manage}
    )


@router.post("", response_model=ApiResponse[UserCreated], status_code=201)
def create_user(body: UserCreate, principal: UserAdmin, service: Service, confirm_code: StepUp,
                settings: AppSettings):
    confirm_code("user.create")
    user, temporary_password = service.create(principal, body)
    email.account_created(service.session, settings, user, temporary_password)
    created = read(user, *service.names(user))
    return ok(UserCreated(**created.model_dump(), temporary_password=temporary_password))


@router.get("", response_model=ApiResponse[Page[UserRead]])
def list_users(
    principal: UserAdmin,
    service: Service,
    page: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=100)] = None,
    status: UserStatus | None = None,
    role: Role | None = None,
    organization_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    access: UserAccess | None = None,
    sort: UserSort = "name",
):
    """The users the caller can list, searched (name, email, phone, organization, branch), filtered and sorted
    by the server; `total` counts every match, for paging."""
    rows, total = service.list(principal, search=(search or "").strip() or None, status=status, role=role,
                               organization_id=organization_id, branch_id=branch_id, access=access, sort=sort,
                               limit=page.limit, offset=page.offset)
    return ok(Page(items=[read(*row, can_manage=service.can_manage(principal, row[0])) for row in rows],
                   total=total, **page.model_dump()))


@router.get("/stats", response_model=ApiResponse[UserStats])
def user_stats(principal: UserAdmin, service: Service):
    return ok(UserStats(**service.stats(principal)))


@router.get("/{user_id}", response_model=ApiResponse[UserRead])
def get_user(user_id: uuid.UUID, principal: CurrentPrincipal, service: Service):
    user = service.view(principal, user_id)
    return ok(read(user, *service.names(user), can_manage=service.can_manage(principal, user)))


@router.patch("/{user_id}", response_model=ApiResponse[UserRead])
def update_user(user_id: uuid.UUID, body: UserUpdate, principal: UserAdmin, service: Service, confirm_code: StepUp):
    if sensitive_changes(body, service.get(principal, user_id)):
        confirm_code("user.update")
    user = service.update(principal, user_id, body)
    return ok(read(user, *service.names(user)))


@router.delete("/{user_id}", response_model=ApiResponse[dict])
def delete_user(user_id: uuid.UUID, principal: UserAdmin, service: Service, confirm_code: StepUp):
    confirm_code("user.delete")
    service.delete(principal, user_id)
    return ok({})


@router.post("/{user_id}/reset-password", response_model=ApiResponse[TemporaryPassword])
def reset_password(user_id: uuid.UUID, body: PasswordReset, principal: UserAdmin, service: Service,
                   confirm_code: StepUp, settings: AppSettings):
    confirm_code("user.password_reset")
    password = service.reset_password(principal, user_id, body.new_password)
    email.password_reset_by_admin(service.session, settings, service.get(principal, user_id), password)
    return ok(TemporaryPassword(temporary_password=password))


@router.post("/{user_id}/reset-mfa", response_model=ApiResponse[UserRead])
def reset_mfa(user_id: uuid.UUID, principal: UserAdmin, service: Service, confirm_code: StepUp,
              settings: AppSettings):
    """Turn off a user's two-step sign-in (lost phone). They are signed out everywhere and may set it up again."""
    confirm_code("user.mfa_reset")
    user = service.reset_mfa(principal, user_id)
    email.two_step_reset(service.session, settings, user)
    return ok(read(user, *service.names(user)))
