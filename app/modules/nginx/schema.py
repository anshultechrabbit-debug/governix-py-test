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


class NginxDetail(NginxSite):
    configured: bool
    # NGINX_SERVER_NAME; "_" answers any host name (organizations are told apart by port).
    server_name: str
    # What a deploy writes now; None when the organization has no port.
    config: str | None


class NginxOverview(BaseModel):
    configured: bool
    sites: list[NginxSite]
