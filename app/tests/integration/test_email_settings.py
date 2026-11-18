"""Settings › Email: the Master Admin's mail server, which emails go out, and that they go out."""

import pytest

from app.modules.auth.permissions import Role
from app.modules.email import service as email
from app.tests.factories import login, make_tenant, make_user

pytestmark = pytest.mark.integration

FORM = {"enabled": True, "host": "smtp.example.com", "port": 587, "username": "mailer@example.com",
        "password": "s3cret-pass", "security": "starttls", "from_email": "no-reply@example.com",
        "from_name": "Governix", "portal_url": "http://10.0.0.5:7001"}


@pytest.fixture
def sent(monkeypatch):
    """The emails handed to the sender, instead of a mail server."""
    outbox = []
    monkeypatch.setattr(email, "_deliver_quietly", lambda config, to, subject, text, html: outbox.append(
        {"to": to, "subject": subject, "text": text, "host": config.host}))

    class Now:
        def submit(self, fn, *args):
            fn(*args)

    monkeypatch.setattr(email, "_POOL", Now())
    email.forget_cached()
    yield outbox
    email.forget_cached()


def test_master_admin_saves_the_mail_server_and_the_password_never_comes_back(client, db, sent):
    master_user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    master = login(client, master_user)
    assert master.get("/settings/email").json()["data"]["source"] == "none"
    saved = master.put("/settings/email", json={**FORM, "events": {"password_changed": False}})
    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["source"] == "settings" and data["has_password"] is True and "s3cret" not in saved.text
    assert {e["key"]: e["enabled"] for e in data["events"]}["password_changed"] is False
    # Saving again without a password keeps the stored one.
    again = master.put("/settings/email", json={**FORM, "password": None})
    assert again.json()["data"]["has_password"] is True


def test_only_the_master_admin_sees_the_mail_settings(client, db):
    tenant = make_tenant(db)
    db.commit()
    assert login(client, tenant.admin).get("/settings/email").status_code == 403


def test_a_new_account_gets_a_welcome_email_with_its_portal(client, db, sent):
    tenant = make_tenant(db)
    tenant.org.allowed_port = 7005
    master_user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    master = login(client, master_user)
    master.put("/settings/email", json=FORM)
    created = master.post("/users", json={"email": "new.admin@example.com", "full_name": "New Admin",
                                          "role": "org_admin", "organization_id": str(tenant.org.id)})
    assert created.status_code == 201, created.text
    welcome = [m for m in sent if m["to"] == "new.admin@example.com"]
    assert welcome and welcome[0]["host"] == "smtp.example.com"
    assert created.json()["data"]["temporary_password"] in welcome[0]["text"]
    assert "http://10.0.0.5:7005/login" in welcome[0]["text"]  # the bank's own port


def test_switched_off_means_no_email(client, db, sent):
    master_user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    master = login(client, master_user)
    master.put("/settings/email", json={**FORM, "enabled": False})
    tenant = make_tenant(db)
    db.commit()
    master.post("/users", json={"email": "quiet@example.com", "full_name": "Quiet", "role": "org_admin",
                                "organization_id": str(tenant.org.id)})
    assert sent == []


def test_the_test_email_reports_what_the_server_said(client, db, monkeypatch):
    master_user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    master = login(client, master_user)
    delivered = []
    monkeypatch.setattr(email, "deliver", lambda config, **kw: delivered.append((config.host, kw["to"])))
    result = master.post("/settings/email/test", json={"to": "me@example.com", "settings": FORM}).json()["data"]
    assert result["ok"] is True and delivered == [("smtp.example.com", "me@example.com")]

    def refuse(config, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(email, "deliver", refuse)
    failed = master.post("/settings/email/test", json={"to": "me@example.com", "settings": FORM}).json()["data"]
    assert failed["ok"] is False and "smtp.example.com:587" in failed["message"]
