"""nginx server blocks for organizations: each one's port, and its domain (the Node backend's nginx deploy flow).

Each organization with an allowed_port gets `governix-<slug>.conf` in NGINX_CONF_DIR, holding:
- a server block on its port (organizations are told apart by port);
- with NGINX_BASE_DOMAIN set, a server block for its domain `<slug>.<NGINX_BASE_DOMAIN>` on NGINX_DOMAIN_PORT,
  and once a certificate exists (NGINX_CERTBOT_COMMAND), the domain on 443 with http redirecting to it.
Every block serves the built frontend and forwards /api to this backend.

The files are the state: present is deployed, `.disabled` is switched off, and a file that differs from what the
settings give now is out of date. With NGINX_ENABLED_DIR (sites-available / sites-enabled), a deploy also links the
file there and a disable removes the link. Deploys and disables are in the audit log.

A deploy never leaves nginx broken: the new file is checked with `nginx -t` and reloaded, and on any failure the
previous file (and link) is put back. A failed certificate request leaves the site live on http, with certbot's
message on the page.
"""

import fcntl
import os
import re
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
CERTBOT_TIMEOUT_SECONDS = 180
# nginx's own output is shown to the Master Admin; the end of it holds the error.
OUTPUT_LIMIT = 2000
# A slug is [a-z0-9-]; it names a domain only as a valid DNS label.
_DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

HEADER = """\
# Managed by Governix: organization {slug} ({organization_id}).
# Redeploy from Organizations > Nginx instead of editing this file.
"""

# What every server block serves: the built frontend, and /api forwarded to FastAPI.
SITE = """\
    root {root};
    index index.html;

    client_max_body_size {max_body}m;

    # Compress the app and JSON. text/event-stream is left out: compressing it would hold back streamed answers.
    gzip on;
    gzip_comp_level 5;
    gzip_min_length 1024;
    gzip_proxied any;
    gzip_vary on;
    gzip_types text/css application/javascript application/json image/svg+xml;

    # The browser calls /api/...; FastAPI serves the same paths without the prefix.
    location /api/ {{
        proxy_pass {upstream}/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Port $server_port;
        # Answers stream as server-sent events, and uploads go straight through.
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}

    # Built files carry a content hash in their names, so a browser may keep them for good.
    location /assets/ {{
        expires 1y;
        add_header Cache-Control "public, immutable";
        try_files $uri =404;
    }}

    # Single-page app: any other path loads index.html.
    location / {{
        try_files $uri $uri/ /index.html;
    }}

    # Always checked again, so a redeploy's new file names are picked up at once.
    location = /index.html {{
        add_header Cache-Control "no-cache";
    }}
"""


def _server(listen: list[str], server_name: str, body: str) -> str:
    lines = [f"    listen {spec};" for spec in listen]
    return "server {\n" + "\n".join(lines) + f"\n    server_name {server_name};\n\n" + body + "}\n"


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

    @property
    def enabled_dir(self) -> Path | None:
        directory = self.settings.NGINX_ENABLED_DIR
        return directory if directory is not None and directory.is_dir() else None

    def domains(self, organization: Organization) -> list[str]:
        """The host names the organization is served on besides its port."""
        base = self.settings.NGINX_BASE_DOMAIN
        if base and _DNS_LABEL.fullmatch(organization.slug):
            return [f"{organization.slug}.{base}"]
        return []

    def cert_dir(self, organization: Organization) -> Path:
        return self.settings.NGINX_CERT_DIR / f"governix-{organization.slug}"

    def has_certificate(self, organization: Organization) -> bool:
        directory = self.cert_dir(organization)
        try:
            return (directory / "fullchain.pem").is_file() and (directory / "privkey.pem").is_file()
        except OSError:  # /etc/letsencrypt is root's: no access reads as no certificate
            return False

    def render(self, organization: Organization) -> str | None:
        if organization.allowed_port is None:
            return None
        body = SITE.format(
            root=self.settings.NGINX_FRONTEND_ROOT,
            # Room for the multipart envelope around a file at the upload limit.
            max_body=self.settings.MAX_UPLOAD_SIZE_MB + 1,
            upstream=self.settings.NGINX_API_UPSTREAM,
        )
        port = organization.allowed_port
        parts = [
            HEADER.format(slug=organization.slug, organization_id=organization.id),
            "# Its own port.",
            _server([str(port), f"[::]:{port}"], self.settings.NGINX_SERVER_NAME, body),
        ]
        domains = self.domains(organization)
        if domains:
            names = " ".join(domains)
            http = self.settings.NGINX_DOMAIN_PORT
            if self.has_certificate(organization):
                cert = self.cert_dir(organization)
                parts += [
                    "# Its domain: http goes to https.",
                    _server([str(http), f"[::]:{http}"], names, "    return 301 https://$host$request_uri;\n"),
                    "# Its domain over https.",
                    _server(["443 ssl", "[::]:443 ssl"], names,
                            f"    ssl_certificate {cert}/fullchain.pem;\n"
                            f"    ssl_certificate_key {cert}/privkey.pem;\n\n" + body),
                ]
            else:
                parts += ["# Its domain.", _server([str(http), f"[::]:{http}"], names, body)]
        return "\n".join(parts)

    def _files(self, organization: Organization) -> tuple[Path, Path] | None:
        """(live, disabled) paths. The slug cannot change and is [a-z0-9-] only."""
        if self.conf_dir is None:
            return None
        live = self.conf_dir / f"governix-{organization.slug}.conf"
        return live, live.with_name(live.name + ".disabled")

    def _link(self, live: Path) -> Path | None:
        return self.enabled_dir / live.name if self.enabled_dir is not None else None

    def _urls(self, organization: Organization) -> list[str]:
        scheme = "https" if self.has_certificate(organization) else "http"
        port = "" if scheme == "https" or self.settings.NGINX_DOMAIN_PORT == 80 else f":{self.settings.NGINX_DOMAIN_PORT}"
        urls = [f"{scheme}://{domain}{port}" for domain in self.domains(organization)]
        # Its port on the server's public address (IP-based setups): always plain http.
        if self.settings.NGINX_PUBLIC_HOST and organization.allowed_port is not None:
            urls.append(f"http://{self.settings.NGINX_PUBLIC_HOST}:{organization.allowed_port}")
        return urls

    def site(self, organization: Organization) -> NginxSite:
        files = self._files(organization)
        configured_dir = self.settings.NGINX_CONF_DIR
        common = {
            "organization_id": organization.id, "port": organization.allowed_port,
            "domains": self.domains(organization), "https": self.has_certificate(organization),
            "urls": self._urls(organization),
        }
        if files is None:
            return NginxSite(
                **common, state="unavailable", deployed_at=None,
                config_file=str(configured_dir / f"governix-{organization.slug}.conf") if configured_dir else None,
            )
        live, disabled = files
        link = self._link(live)
        if live.exists():
            linked = link is None or (link.is_symlink() and link.resolve() == live.resolve())
            state = "deployed" if linked and live.read_text() == self.render(organization) else "outdated"
            path = live
        elif disabled.exists():
            state, path = "disabled", disabled
        else:
            state, path = "not_deployed", None
        return NginxSite(
            **common, state=state, config_file=str(live),
            deployed_at=datetime.fromtimestamp(path.stat().st_mtime, UTC) if path else None,
        )

    def detail(self, organization: Organization, *, warning: str | None = None) -> NginxDetail:
        files = self._files(organization)
        link = self._link(files[0]) if files else None
        return NginxDetail(
            **self.site(organization).model_dump(),
            configured=self.conf_dir is not None,
            server_name=self.settings.NGINX_SERVER_NAME,
            enabled_link=str(link) if link else None,
            certbot=bool(self.settings.NGINX_CERTBOT_COMMAND),
            config=self.render(organization),
            warning=warning,
        )

    def overview(self) -> NginxOverview:
        return NginxOverview(
            configured=self.conf_dir is not None,
            base_domain=self.settings.NGINX_BASE_DOMAIN,
            sites=[self.site(o) for o in self.organizations.with_ports() if o.deleted_at is None],
        )

    def get(self, organization_id: uuid.UUID) -> Organization:
        organization = self.organizations.get(organization_id)
        if organization is None:
            raise NotFoundError("Organization not found.")
        return organization

    # --- changing -----------------------------------------------------------

    def deploy(self, principal: Principal, organization_id: uuid.UUID) -> NginxDetail:
        """Write the organization's server blocks (port and domain) and make them live; also re-enables a
        disabled one. With NGINX_CERTBOT_COMMAND, a domain without a certificate gets one, then https."""
        organization = self.get(organization_id)
        live, disabled = self._require_files(organization)
        if organization.allowed_port is None:
            raise ValidationError(
                f"{organization.name} has no port. Give it one in Edit, then deploy.", code="NO_PORT"
            )
        warning = None
        with _locked(live.parent):
            self._go_live(live, self.render(organization))
            disabled.unlink(missing_ok=True)
            domains = self.domains(organization)
            if domains and self.settings.NGINX_CERTBOT_COMMAND and not self.has_certificate(organization):
                warning = self._certify(organization, live, domains)
        record_event(
            self.session, "organization.nginx_deployed", actor=principal, organization_id=organization.id,
            resource_type="organization", resource_id=organization.id,
            details={"port": organization.allowed_port, "file": str(live), "domains": self.domains(organization),
                     "https": self.has_certificate(organization)},
        )
        self.session.commit()
        return self.detail(organization, warning=warning)

    def disable(self, principal: Principal, organization_id: uuid.UUID) -> NginxDetail:
        """Stop serving the organization's port and domain; Deploy turns them back on."""
        organization = self.get(organization_id)
        live, disabled = self._require_files(organization)
        link = self._link(live)
        with _locked(live.parent):
            if not live.exists():
                raise ValidationError(f"{organization.name} is not deployed.", code="NOT_DEPLOYED")
            os.replace(live, disabled)
            was_linked = link is not None and link.is_symlink()
            if was_linked:
                link.unlink()
            try:
                self._apply()
            except Exception:
                os.replace(disabled, live)
                if was_linked:
                    link.symlink_to(live)
                raise
        record_event(
            self.session, "organization.nginx_disabled", actor=principal, organization_id=organization.id,
            resource_type="organization", resource_id=organization.id,
            details={"port": organization.allowed_port, "file": str(live)},
        )
        self.session.commit()
        return self.detail(organization)

    def _go_live(self, live: Path, config: str) -> None:
        """Write the file, link it, check and reload; on any failure put the previous file and link back."""
        link = self._link(live)
        if link is not None and link.exists() and not link.is_symlink():
            raise ValidationError(f"{link} exists and is not a link Governix made; move it away, then deploy.",
                                  code="NGINX_LINK_TAKEN")
        previous = live.read_text() if live.exists() else None
        had_link = link is not None and link.is_symlink()
        _write(live, config)
        if link is not None and not had_link:
            link.symlink_to(live)
        try:
            self._apply()
        except Exception:
            if previous is None:
                live.unlink(missing_ok=True)
            else:
                _write(live, previous)
            if link is not None and not had_link:
                link.unlink(missing_ok=True)
            raise

    def _certify(self, organization: Organization, live: Path, domains: list[str]) -> str | None:
        """Ask certbot for the domain's certificate, then serve it over https. The site stays live on http
        whatever happens; the returned text says why https is not on."""
        command = [*shlex.split(self.settings.NGINX_CERTBOT_COMMAND), "--cert-name",
                   f"governix-{organization.slug}"]
        for domain in domains:
            command += ["-d", domain]
        succeeded, output = _run_args(command, timeout=CERTBOT_TIMEOUT_SECONDS)
        if not succeeded:
            return f"The site is live on http, but no certificate was issued: {output}"
        if not self.has_certificate(organization):
            return (f"certbot finished, but no certificate is readable in {self.cert_dir(organization)}; "
                    "the site is live on http.")
        try:
            self._go_live(live, self.render(organization))
        except ServiceUnavailableError as exc:
            return f"The certificate was issued, but nginx refused the https configuration: {exc.message}"
        return None

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
    return _run_args(shlex.split(command))


def _run_args(args: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return False, f"Command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, f"No answer after {timeout} seconds."
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
