"""Which organization a portal address belongs to, for its sign-in page (public: no session yet).

A bank's portal is reached on its port (http://server:7003), on its own domain (custom_domain, e.g.
https://portal.hdfc.com) or, with NGINX_BASE_DOMAIN, on https://hdfc.governix.example.com (port 80/443). The page asks `GET /tenant-info?port=&host=`; the domain wins over
the port, because on a domain the port is just 80 or 443. Only what the sign-in page shows is returned (name, colors,
font, logo); a suspended or deleted organization is not found.
"""

import uuid
from typing import Annotated

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings, get_storage
from app.core.exceptions import NotFoundError
from app.core.responses import ApiResponse, ok
from app.modules.organizations.model import Organization, OrganizationStatus
from app.infrastructure.storage.base import Storage

router = APIRouter(tags=["tenant"])


class TenantInfo(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    allowed_port: int | None
    port: int | None
    domain: str | None
    primary_color: str | None
    secondary_color: str | None
    brand_font: str | None
    logo_url: str | None


def _host(value: str | None) -> str:
    return (value or "").split(":")[0].strip().lower().rstrip(".")


def find_tenant(db: Session, settings: Settings, *, host: str | None, port: int | None) -> Organization | None:
    live = (Organization.deleted_at.is_(None), Organization.status == OrganizationStatus.ACTIVE)
    base = settings.NGINX_BASE_DOMAIN
    host = _host(host)
    if host:
        organization = db.scalar(select(Organization).where(Organization.custom_domain == host, *live))
        if organization is not None:
            return organization
    if base and host.endswith(f".{base}"):
        slug = host[: -len(base) - 1]
        organization = db.scalar(select(Organization).where(Organization.slug == slug, *live))
        if organization is not None:
            return organization
    if port is not None:
        return db.scalar(select(Organization).where(Organization.allowed_port == port, *live))
    return None


def portal_domain(organization: Organization, settings: Settings) -> str | None:
    """The address its portal is announced on: its own domain first, else <slug>.<NGINX_BASE_DOMAIN>."""
    if organization.custom_domain:
        return organization.custom_domain
    base = settings.NGINX_BASE_DOMAIN
    return f"{organization.slug}.{base}" if base else None


def _found(db: Session, settings: Settings, request: Request, host: str | None, port: int | None) -> Organization:
    # nginx passes the address the browser used as Host (and the port as X-Forwarded-Port).
    host = host or request.headers.get("x-forwarded-host") or request.headers.get("host")
    if port is None and (forwarded := request.headers.get("x-forwarded-port", "")).isdigit():
        port = int(forwarded)
    organization = find_tenant(db, settings, host=host, port=port)
    if organization is None:
        raise NotFoundError("No organization is served at this address.", code="TENANT_NOT_FOUND")
    return organization


Port = Annotated[int | None, Query(ge=1, le=65535)]
Host = Annotated[str | None, Query(max_length=253)]


@router.get("/tenant-info", response_model=ApiResponse[TenantInfo])
def tenant_info(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    port: Port = None,
    host: Host = None,
):
    organization = _found(db, settings, request, host, port)
    domain = portal_domain(organization, settings)
    logo = None
    if organization.logo_key is not None:
        query = {"v": int(organization.updated_at.timestamp())}
        if organization.allowed_port is not None:
            query["port"] = organization.allowed_port
        if domain:
            query["host"] = domain
        logo = f"{settings.API_PUBLIC_PREFIX.rstrip('/')}/tenant-info/logo?{urlencode(query)}"
    return ok(TenantInfo(
        id=organization.id, name=organization.name, slug=organization.slug, status=organization.status,
        allowed_port=organization.allowed_port, port=organization.allowed_port,
        domain=domain,
        primary_color=organization.primary_color, secondary_color=organization.secondary_color,
        brand_font=organization.brand_font, logo_url=logo,
    ))


@router.get("/tenant-info/logo")
def tenant_logo(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    storage: Annotated[Storage, Depends(get_storage)],
    port: Port = None,
    host: Host = None,
):
    organization = _found(db, settings, request, host, port)
    if organization.logo_key is None:
        raise NotFoundError("No logo.")
    with storage.open(organization.logo_key) as handle:
        content = handle.read()
    return Response(content, media_type=organization.logo_content_type or "image/png",
                    headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})
