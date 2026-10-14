import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal
from app.modules.users.model import User
from app.modules.users.schema import (
    PasswordReset, TemporaryPassword, UserCreate, UserCreated, UserRead, UserStats, UserStatus, UserUpdate,
)
from app.modules.users.service import UserService

router = APIRouter(prefix="/users", tags=["users"])


UserAdmin = Annotated[Principal, Depends(require(Permission.USERS_MANAGE))]


def get_service(db: Annotated[Session, Depends(get_db)]) -> UserService:
    return UserService(db)


Service = Annotated[UserService, Depends(get_service)]


def read(user: User, organization_name: str | None, branch_name: str | None) -> UserRead:
    return UserRead.model_validate(user).model_copy(
        update={"organization_name": organization_name, "branch_name": branch_name}
    )


@router.post("", response_model=ApiResponse[UserCreated], status_code=201)
def create_user(body: UserCreate, principal: UserAdmin, service: Service):
    user, temporary_password = service.create(principal, body)
    created = read(user, *service.names(user))
    return ok(UserCreated(**created.model_dump(), temporary_password=temporary_password))


@router.get("", response_model=ApiResponse[Page[UserRead]])
def list_users(
    principal: UserAdmin,
    service: Service,
    page: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=100)] = None,
    status: UserStatus | None = None,
):
    rows, total = service.list(principal, search=search, status=status, limit=page.limit, offset=page.offset)
    return ok(Page(items=[read(*row) for row in rows], total=total, **page.model_dump()))


@router.get("/stats", response_model=ApiResponse[UserStats])
def user_stats(principal: UserAdmin, service: Service):
    active, inactive = service.stats(principal)
    return ok(UserStats(total=active + inactive, active=active, inactive=inactive))


@router.get("/{user_id}", response_model=ApiResponse[UserRead])
def get_user(user_id: uuid.UUID, principal: CurrentPrincipal, service: Service):
    user = service.get(principal, user_id)
    return ok(read(user, *service.names(user)))


@router.patch("/{user_id}", response_model=ApiResponse[UserRead])
def update_user(user_id: uuid.UUID, body: UserUpdate, principal: UserAdmin, service: Service):
    user = service.update(principal, user_id, body)
    return ok(read(user, *service.names(user)))


@router.delete("/{user_id}", response_model=ApiResponse[dict])
def delete_user(user_id: uuid.UUID, principal: UserAdmin, service: Service):
    service.delete(principal, user_id)
    return ok({})


@router.post("/{user_id}/reset-password", response_model=ApiResponse[TemporaryPassword])
def reset_password(user_id: uuid.UUID, body: PasswordReset, principal: UserAdmin, service: Service):
    return ok(TemporaryPassword(temporary_password=service.reset_password(principal, user_id, body.new_password)))
