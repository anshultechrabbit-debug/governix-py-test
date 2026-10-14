import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.nginx.schema import NginxDetail, NginxOverview
from app.modules.nginx.service import NginxService

router = APIRouter(prefix="/nginx", tags=["nginx"])

MasterAdmin = Annotated[Principal, Depends(require(Permission.ORGANIZATIONS_MANAGE))]


def get_service(
    db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_app_settings)]
) -> NginxService:
    return NginxService(db, settings)


Service = Annotated[NginxService, Depends(get_service)]


@router.get("", response_model=ApiResponse[NginxOverview])
def overview(principal: MasterAdmin, service: Service):
    return ok(service.overview())


@router.get("/{organization_id}", response_model=ApiResponse[NginxDetail])
def detail(organization_id: uuid.UUID, principal: MasterAdmin, service: Service):
    return ok(service.detail(service.get(organization_id)))


@router.post("/{organization_id}/deploy", response_model=ApiResponse[NginxDetail])
def deploy(organization_id: uuid.UUID, principal: MasterAdmin, service: Service):
    return ok(service.deploy(principal, organization_id))


@router.post("/{organization_id}/disable", response_model=ApiResponse[NginxDetail])
def disable(organization_id: uuid.UUID, principal: MasterAdmin, service: Service):
    return ok(service.disable(principal, organization_id))
