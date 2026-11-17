"""Organizations > Nginx: one click puts a bank live on its port and its domain (no real nginx: the check and reload
commands are stand-ins, and certbot is a script that writes a certificate)."""

import stat

import pytest

from app.modules.auth.permissions import Role
from app.modules.organizations.model import OrganizationStatus
from app.tests.factories import login, make_org, make_user

pytestmark = pytest.mark.integration

BASE = "governix.test"


@pytest.fixture
def dirs(tmp_path):
    paths = {name: tmp_path / name for name in ("available", "enabled", "certs")}
    for path in paths.values():
        path.mkdir()
    return paths


def fake_certbot(tmp_path, certs) -> str:
    """Like `certbot certonly`: writes the certificate under --cert-name."""
    script = tmp_path / "certbot.sh"
    script.write_text(
        '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = "--cert-name" ] && name="$2"; shift; done\n'
        f'mkdir -p "{certs}/$name" && touch "{certs}/$name/fullchain.pem" "{certs}/$name/privkey.pem"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def settings(settings, dirs):
    return settings.model_copy(update={
        "NGINX_CONF_DIR": dirs["available"], "NGINX_ENABLED_DIR": dirs["enabled"], "NGINX_CERT_DIR": dirs["certs"],
        "NGINX_BASE_DOMAIN": BASE, "NGINX_TEST_COMMAND": "true", "NGINX_RELOAD_COMMAND": "true",
    })


@pytest.fixture
def bank(db):
    org = make_org(db, "hdfc")
    org.allowed_port = 7003
    db.commit()
    return org


@pytest.fixture
def master(client, db):
    user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    return login(client, user)


def test_one_click_serves_port_and_domain(master, bank, dirs):
    response = master.post(f"/nginx/{bank.id}/deploy")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["state"] == "deployed" and data["domains"] == [f"hdfc.{BASE}"]
    assert data["urls"] == [f"http://hdfc.{BASE}"] and data["https"] is False
    live = dirs["available"] / "governix-hdfc.conf"
    assert (dirs["enabled"] / live.name).resolve() == live.resolve()
    config = live.read_text()
    assert "listen 7003;" in config and f"server_name hdfc.{BASE};" in config and "listen 80;" in config
    assert master.get("/nginx").json()["data"]["base_domain"] == BASE


def test_a_refused_config_puts_everything_back(master, bank, dirs, settings):
    settings.NGINX_TEST_COMMAND = "false"  # the app reads this same settings object
    response = master.post(f"/nginx/{bank.id}/deploy")
    assert response.status_code == 503 and response.json()["error"]["reason"] == "NGINX_TEST_FAILED"
    assert not list(dirs["available"].glob("*.conf")) and not list(dirs["enabled"].iterdir())


def test_disable_unlinks_and_deploy_restores(master, bank, dirs):
    master.post(f"/nginx/{bank.id}/deploy")
    data = master.post(f"/nginx/{bank.id}/disable").json()["data"]
    assert data["state"] == "disabled" and not list(dirs["enabled"].iterdir())
    assert master.post(f"/nginx/{bank.id}/deploy").json()["data"]["state"] == "deployed"
    assert len(list(dirs["enabled"].iterdir())) == 1


def test_certbot_turns_on_https(tmp_path, master, bank, dirs, settings):
    settings.NGINX_CERTBOT_COMMAND = fake_certbot(tmp_path, dirs["certs"])
    data = master.post(f"/nginx/{bank.id}/deploy").json()["data"]
    assert data["https"] is True and data["warning"] is None and data["state"] == "deployed"
    assert data["urls"] == [f"https://hdfc.{BASE}"]
    config = (dirs["available"] / "governix-hdfc.conf").read_text()
    assert "listen 443 ssl;" in config and "return 301 https://$host$request_uri;" in config
    assert f"{dirs['certs']}/governix-hdfc/fullchain.pem" in config


def test_failed_certificate_keeps_the_site_on_http(master, bank, settings):
    settings.NGINX_CERTBOT_COMMAND = "false"
    data = master.post(f"/nginx/{bank.id}/deploy").json()["data"]
    assert data["state"] == "deployed" and data["https"] is False
    assert "no certificate was issued" in data["warning"]


def test_tenant_info_by_domain_or_port(client, db, bank):
    other = make_org(db, "axis")
    other.allowed_port = 7004
    db.commit()
    assert client.get(f"/tenant-info?host=hdfc.{BASE}").json()["data"]["name"] == bank.name
    assert client.get("/tenant-info?port=7003").json()["data"]["domain"] == f"hdfc.{BASE}"
    # On its domain the port is 80/443: the domain decides.
    assert client.get(f"/tenant-info?host=axis.{BASE}&port=7003").json()["data"]["slug"] == "axis"
    assert client.get("/tenant-info", headers={"Host": f"hdfc.{BASE}"}).json()["data"]["slug"] == "hdfc"
    missing = client.get(f"/tenant-info?host=nobody.{BASE}")
    assert missing.status_code == 404 and missing.json()["error"]["reason"] == "TENANT_NOT_FOUND"
    bank.status = OrganizationStatus.SUSPENDED
    db.commit()
    assert client.get("/tenant-info?port=7003").status_code == 404


def test_ip_based_setup_links_the_port(master, bank, settings):
    """No domain (the live site is on an IP): the bank is served on its port, linked on the server's address."""
    settings.NGINX_BASE_DOMAIN = None
    settings.NGINX_PUBLIC_HOST = "89.116.122.215"
    data = master.post(f"/nginx/{bank.id}/deploy").json()["data"]
    assert data["state"] == "deployed" and data["domains"] == []
    assert data["urls"] == ["http://89.116.122.215:7003"]
    assert "listen 80;" not in data["config"]
