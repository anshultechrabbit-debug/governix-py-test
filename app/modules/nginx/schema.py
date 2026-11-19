import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# deployed: the file on disk is what the organization's settings give now.
# outdated: a file is deployed, but the port or the NGINX_* settings changed since.
# disabled: deployed once, then switched off (kept as <file>.disabled, which nginx ignores).
# unavailable: NGINX_CONF_DIR is not set or missing, so nothing on disk can be read.
NginxState = Literal["deployed", "outdated", "disabled", "not_deployed", "unavailable"]


class NginxSite(BaseModel):
    organization_id: uuid.UUID
    port: int | None
    state: NginxState
    # Where the file is, or would be written.
    config_file: str | None
    deployed_at: datetime | None
    # Host names it is served on besides its port: its custom domain, then <slug>.<NGINX_BASE_DOMAIN>.
    domains: list[str] = []
    # The bank's own domain (also in `domains`), set on the organization; None when it has none.
    custom_domain: str | None = None
    # Every domain has a certificate, so all are served over https.
    https: bool = False
    # Where to open it: its domains first ("https://hdfc.governix.example.com"), then its port on
    # NGINX_PUBLIC_HOST ("http://89.116.122.215:7003").
    urls: list[str] = []


class NginxDetail(NginxSite):
    configured: bool
    # NGINX_SERVER_NAME; "_" answers any host name (organizations are told apart by port).
    server_name: str
    # sites-enabled link a deploy makes (NGINX_ENABLED_DIR), or None.
    enabled_link: str | None = None
    # A deploy asks certbot for a certificate for the domains (NGINX_CERTBOT_COMMAND).
    certbot: bool = False
    # What a deploy writes now; None when the organization has no port.
    config: str | None
    # After a deploy: why https is not on yet (certbot's message); the site is live on http.
    warning: str | None = None


class NginxOverview(BaseModel):
    configured: bool
    # NGINX_BASE_DOMAIN: organizations are served on <slug>.<this>; None when off.
    base_domain: str | None = None
    sites: list[NginxSite]
