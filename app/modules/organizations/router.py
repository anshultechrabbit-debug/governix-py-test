import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings, get_storage
from app.core.exceptions import NotFoundError
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.infrastructure.storage.base import Storage
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.organizations.schema import (
    DeletionPreview,
    DeletionResult,
    OrganizationCreate,
    OrganizationOptions,
    OrganizationRead,
    OrganizationUpdate,
    organization_read,
)
from app.modules.organizations.service import OrganizationService

router = APIRouter(prefix="/organizations", tags=["organizations"])

MasterAdmin = Annotated[Principal, Depends(require(Permission.ORGANIZATIONS_MANAGE))]
StorageDep = Annotated[Storage, Depends(get_storage)]


def get_service(
    db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_app_settings)]
) -> OrganizationService:
    return OrganizationService(db, settings)


Service = Annotated[OrganizationService, Depends(get_service)]


@router.post("", response_model=ApiResponse[OrganizationRead], status_code=201)
def create_organization(body: OrganizationCreate, principal: MasterAdmin, service: Service):
    """Create an organisation (with branding) and, optionally, its first admin."""
    return ok(organization_read(service.create(principal, body)))


@router.get("", response_model=ApiResponse[Page[OrganizationRead]])
def list_organizations(
    principal: MasterAdmin, service: Service, page: Annotated[PageParams, Depends(page_params)]
):
    items, total = service.list(limit=page.limit, offset=page.offset)
    return ok(Page(items=[organization_read(o) for o in items], total=total, **page.model_dump()))


@router.get("/options", response_model=ApiResponse[OrganizationOptions])
def organization_options(principal: MasterAdmin, service: Service):
    """For the create and edit forms: the port range, the ports already in use, and the brand fonts."""
    return ok(service.options())


@router.get("/me", response_model=ApiResponse[OrganizationRead])
def my_organization(principal: CurrentPrincipal, service: Service):
    """The caller's organisation: name, branding and contact details."""
    if principal.organization_id is None:
        raise NotFoundError("You do not belong to an organization.")
    return ok(organization_read(service.get(principal.organization_id)))


@router.get("/{organization_id}", response_model=ApiResponse[OrganizationRead])
def get_organization(organization_id: uuid.UUID, principal: MasterAdmin, service: Service):
    return ok(organization_read(service.get(organization_id)))


@router.patch("/{organization_id}", response_model=ApiResponse[OrganizationRead])
def update_organization(
    organization_id: uuid.UUID, body: OrganizationUpdate, principal: MasterAdmin, service: Service
):
    return ok(organization_read(service.update(principal, organization_id, body)))


@router.get("/{organization_id}/delete-preview", response_model=ApiResponse[DeletionPreview])
def organization_deletion_preview(organization_id: uuid.UUID, principal: MasterAdmin, service: Service):
    """What deleting the organisation would also delete: its branches, departments, users and policies."""
    return ok(service.deletion_preview(organization_id))


@router.delete("/{organization_id}", response_model=ApiResponse[DeletionResult])
def delete_organization(
    organization_id: uuid.UUID, principal: MasterAdmin, service: Service, confirm: Annotated[bool, Query()] = False,
):
    """Soft-delete the organisation and everything in it. Without confirm=true: 409
    DELETE_CONFIRMATION_REQUIRED, with the preview in `error.details`, and nothing changes."""
    return ok(service.delete(principal, organization_id, confirm=confirm))


@router.post("/{organization_id}/logo", response_model=ApiResponse[OrganizationRead])
def upload_logo(
    organization_id: uuid.UUID, principal: MasterAdmin, service: Service, storage: StorageDep,
    file: Annotated[UploadFile, File()],
):
    organization = service.set_logo(principal, organization_id, storage, file=file.file, filename=file.filename or "")
    return ok(organization_read(organization))


@router.get("/{organization_id}/logo")
def get_logo(organization_id: uuid.UUID, principal: CurrentPrincipal, service: Service, storage: StorageDep):
    if principal.role is not Role.MASTER_ADMIN and principal.organization_id != organization_id:
        raise NotFoundError("Organization not found.")
    organization = service.get(organization_id)
    if organization.logo_key is None:
        raise NotFoundError("No logo.")
    with storage.open(organization.logo_key) as handle:
        content = handle.read()
    return Response(content, media_type=organization.logo_content_type or "image/png",
                    headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})
