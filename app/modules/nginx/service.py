"""nginx server blocks for organization ports (the Node backend's nginx deploy flow).

Each organization with an allowed_port gets `governix-<slug>.conf` in NGINX_CONF_DIR: a server
block on that port serving the built frontend, with /api forwarded to this backend. The files
are the state: present is deployed, `.disabled` is switched off, and a file that differs from
what the settings give now is out of date. Deploys and disables are in the audit log.

A deploy never leaves nginx broken: the new file is checked with `nginx -t` and reloaded, and on
any failure the previous file is put back.
"""

import fcntl
import os
import shlex
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import NotFoundError, ServiceUnavailableError, ValidationError
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal
from app.modules.nginx.schema import NginxDetail, NginxOverview, NginxSite
from app.modules.organizations.model import Organization
from app.modules.organizations.repository import OrganizationRepository

COMMAND_TIMEOUT_SECONDS = 30
# nginx's own output is shown to the Master Admin; the end of it holds the error.
OUTPUT_LIMIT = 2000

TEMPLATE = """\
# Managed by Governix: organization {slug} ({organization_id}).
# Redeploy from Organizations > Nginx instead of editing this file.
server {{
    listen {port};
    listen [::]:{port};
    server_name {server_name};

    root {root};
    index index.html;

    client_max_body_size {max_body}m;

    # The browser calls /api/...; FastAPI serves the same paths without the prefix.
    location /api/ {{
        proxy_pass {upstream}/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Answers stream as server-sent events, and uploads go straight through.
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}

    # Single-page app: any other path loads index.html.
    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
"""


class NginxService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.organizations = OrganizationRepository(session)

    # --- reading ------------------------------------------------------------

    @property
    def conf_dir(self) -> Path | None:
        directory = self.settings.NGINX_CONF_DIR
        return directory if directory is not None and directory.is_dir() else None

    def render(self, organization: Organization) -> str | None:
        if organization.allowed_port is None:
            return None
        return TEMPLATE.format(
            slug=organization.slug,
            organization_id=organization.id,
            port=organization.allowed_port,
            server_name=self.settings.NGINX_SERVER_NAME,
            root=self.settings.NGINX_FRONTEND_ROOT,
            # Room for the multipart envelope around a file at the upload limit.
            max_body=self.settings.MAX_UPLOAD_SIZE_MB + 1,
            upstream=self.settings.NGINX_API_UPSTREAM,
        )

    def _files(self, organization: Organization) -> tuple[Path, Path] | None:
        """(live, disabled) paths. The slug cannot change and is [a-z0-9-] only."""
        if self.conf_dir is None:
            return None
        live = self.conf_dir / f"governix-{organization.slug}.conf"
        return live, live.with_name(live.name + ".disabled")

    def site(self, organization: Organization) -> NginxSite:
        files = self._files(organization)
        configured_dir = self.settings.NGINX_CONF_DIR
        if files is None:
            return NginxSite(
                organization_id=organization.id, port=organization.allowed_port, state="unavailable",
                config_file=str(configured_dir / f"governix-{organization.slug}.conf") if configured_dir else None,
                deployed_at=None,
            )
        live, disabled = files
        if live.exists():
            state = "deployed" if live.read_text() == self.render(organization) else "outdated"
            path = live
        elif disabled.exists():
            state, path = "disabled", disabled
        else:
            state, path = "not_deployed", None
        return NginxSite(
            organization_id=organization.id, port=organization.allowed_port, state=state,
            config_file=str(live),
            deployed_at=datetime.fromtimestamp(path.stat().st_mtime, UTC) if path else None,
        )

    def detail(self, organization: Organization) -> NginxDetail:
        return NginxDetail(
            **self.site(organization).model_dump(),
            configured=self.conf_dir is not None,
            server_name=self.settings.NGINX_SERVER_NAME,
            config=self.render(organization),
        )

    def overview(self) -> NginxOverview:
        return NginxOverview(
            configured=self.conf_dir is not None,
            sites=[self.site(o) for o in self.organizations.with_ports()],
        )

    def get(self, organization_id: uuid.UUID) -> Organization:
        organization = self.organizations.get(organization_id)
        if organization is None:
            raise NotFoundError("Organization not found.")
        return organization

    # --- changing -----------------------------------------------------------

    def deploy(self, principal: Principal, organization_id: uuid.UUID) -> NginxDetail:
        """Write the organization's server block (also re-enables a disabled one)."""
        organization = self.get(organization_id)
        files = self._require_files(organization)
        config = self.render(organization)
        if config is None:
            raise ValidationError(
                f"{organization.name} has no port. Give it one in Edit, then deploy.", code="NO_PORT"
            )
        live, disabled = files
        with _locked(live.parent):
            previous = live.read_text() if live.exists() else None
            _write(live, config)
            try:
                self._apply()
            except Exception:
                if previous is None:
                    live.unlink(missing_ok=True)
                else:
                    _write(live, previous)
                raise
            disabled.unlink(missing_ok=True)
        record_event(
            self.session, "organization.nginx_deployed", actor=principal, organization_id=organization.id,
            resource_type="organization", resource_id=organization.id,
            details={"port": organization.allowed_port, "file": str(live)},
        )
        self.session.commit()
        return self.detail(organization)

    def disable(self, principal: Principal, organization_id: uuid.UUID) -> NginxDetail:
        """Stop serving the organization's port; Deploy turns it back on."""
        organization = self.get(organization_id)
        live, disabled = self._require_files(organization)
        with _locked(live.parent):
            if not live.exists():
                raise ValidationError(f"{organization.name} is not deployed.", code="NOT_DEPLOYED")
            os.replace(live, disabled)
            try:
                self._apply()
            except Exception:
                os.replace(disabled, live)
                raise
        record_event(
            self.session, "organization.nginx_disabled", actor=principal, organization_id=organization.id,
            resource_type="organization", resource_id=organization.id,
            details={"port": organization.allowed_port, "file": str(live)},
        )
        self.session.commit()
        return self.detail(organization)

    def _require_files(self, organization: Organization) -> tuple[Path, Path]:
        files = self._files(organization)
        if files is None:
            raise ServiceUnavailableError(
                "Deploying from Governix is off on this server: NGINX_CONF_DIR is not set or does not exist."
                " Copy the config into nginx by hand, or set it up as described in deploy/README.md.",
                code="NGINX_NOT_CONFIGURED",
            )
        return files

    def _apply(self) -> None:
        """Check every nginx config, then reload. Raises with nginx's own message."""
        for command, reason, failed in (
            (self.settings.NGINX_TEST_COMMAND, "NGINX_TEST_FAILED", "nginx rejected the configuration"),
            (self.settings.NGINX_RELOAD_COMMAND, "NGINX_RELOAD_FAILED", "nginx could not be reloaded"),
        ):
            succeeded, output = _run(command)
            if not succeeded:
                raise ServiceUnavailableError(
                    f"{failed}, so the previous configuration was kept.",
                    code=reason, details={"command": command, "output": output},
                )


def _run(command: str) -> tuple[bool, str]:
    """Run without a shell; (succeeded, the end of its output)."""
    args = shlex.split(command)
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        return False, f"Command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, f"No answer after {COMMAND_TIMEOUT_SECONDS} seconds."
    except OSError as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stderr or result.stdout).strip()[-OUTPUT_LIMIT:]


def _write(path: Path, content: str) -> None:
    """Replace the file in one step, so nginx never reads half of it."""
    temporary = path.with_name(f".{path.name}.tmp")  # not *.conf: nginx's include skips it
    temporary.write_text(content)
    os.replace(temporary, path)


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """One deploy at a time across every API worker on this machine."""
    with open(directory / ".governix-nginx.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
