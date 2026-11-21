"""Mandatory AI configuration: no organization uses AI until the Master Admin configured, checked and activated
it; status, restrictions, alerts and usage come from the real configuration and ledger.

The numbers in the test names follow the acceptance list of the specification (docs: AGENTS.md, "Mandatory AI
configuration"). Uses the local stand-in models: no provider is called.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.infrastructure.ai.llm.base import LLMProvider, LLMResult
from app.modules.ai_management.model import OrgAISettings, OrgAIUsage, OrgAPIKey, UsageStatus
from app.modules.ai_management.runtime import OrganizationLLM, organization_embedder, organization_llm
from app.modules.ai_management.service import AIBlocked, AIEmbeddingBlocked
from app.modules.ai_management.status import AIStatus, USER_MESSAGE
from app.modules.auth.permissions import Role
from app.modules.notifications.model import Notification
from app.tests.factories import login, make_tenant, make_user

pytestmark = pytest.mark.integration

LOCAL_MODEL = {"provider": "local", "model_id": "local-extractive-v1", "service": "answers"}


@pytest.fixture
def settings(settings):
    """This file tests the real rule: AI only after configuration and activation."""
    return settings.model_copy(update={"AI_REQUIRE_ORG_CONFIG": True})


@pytest.fixture
def setup(client, db):
    tenant = make_tenant(db)
    other = make_tenant(db)
    master_user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    return tenant, other, login(client, master_user)


def configure(master, org_id, *, activate=True):
    assert master.post(f"/organizations/{org_id}/ai/models", json=LOCAL_MODEL).status_code == 201
    if activate:
        activated = master.post(f"/organizations/{org_id}/ai/activate")
        assert activated.status_code == 200, activated.text
        return activated.json()["data"]
    return None


def ask(client_as, question="What is the processing fee?"):
    return client_as.post("/ai/ask", json={"question": question})


class StreamingLLM(LLMProvider):
    """Starts answering (the provider is being paid), then fails."""

    model_id = model = "gpt-4o-mini"
    max_output_tokens = 1000

    def generate_json(self, system, user, schema, *, context=None):
        raise RuntimeError("not used")

    def stream_json(self, system, user, schema, *, context=None):
        yield {"partial": "The processing fee is"}
        raise ConnectionError("connection dropped mid-answer")


# 1-5. A new organization: everything AI is refused, everything else works ----------------------------------------

def test_1_to_5_a_new_organization_has_no_ai_and_every_role_is_told_so(setup, client):
    tenant, _, master = setup
    status = master.get(f"/organizations/{tenant.org.id}/ai/status").json()["data"]
    assert status["status"] == AIStatus.NOT_CONFIGURED and status["ready"] is False
    assert "an answering model" in status["missing"]

    # 3. The Org Admin learns what is wrong (for the login popup), never keys.
    admin = login(client, tenant.admin)
    mine = admin.get("/ai/status").json()["data"]
    assert mine["status"] == "NOT_CONFIGURED" and mine["ready"] is False and mine["details"]["missing"]
    assert mine["meters"] is not None and "key_encrypted" not in str(mine) and "sk-" not in str(mine)

    # 4-5. Branch Manager and staff: the generic message only, and the backend refuses their questions.
    for person in (tenant.manager_a, tenant.user_a1):
        as_person = login(client, person)
        theirs = as_person.get("/ai/status").json()["data"]
        assert theirs["ready"] is False and theirs["message"] == USER_MESSAGE and theirs["details"] is None
        refused = ask(as_person)
        assert refused.status_code == 409 and refused.json()["error"]["reason"] == "AI_NOT_CONFIGURED"
        assert refused.json()["error"]["message"] == USER_MESSAGE

    # 20. Ordinary work goes on: documents, folders, users.
    assert admin.get("/documents").status_code == 200
    assert admin.get("/categories/tree").status_code == 200
    assert admin.get("/users").status_code == 200


def test_2_the_master_admin_dashboard_counts_the_organizations_needing_configuration(setup):
    tenant, other, master = setup
    configure(master, other.org.id)
    report = master.get("/ai/platform").json()["data"]
    assert report["organizations"]["total"] == 2 and report["organizations"]["active"] == 1
    assert report["organizations"]["requiring_configuration"] == 1
    assert [row["organization_id"] for row in report["attention"]] == [str(tenant.org.id)]
    assert report["attention"][0]["status"] == "NOT_CONFIGURED"


def test_the_platform_overview_is_for_the_master_admin_only(setup, client):
    tenant, _, _ = setup
    assert login(client, tenant.admin).get("/ai/platform").status_code == 403


# 6. Configured and activated: AI works --------------------------------------------------------------------------

def test_6_activation_needs_everything_required_and_then_ai_works(setup, client):
    tenant, _, master = setup
    refused = master.post(f"/organizations/{tenant.org.id}/ai/activate")
    assert refused.status_code == 409 and refused.json()["error"]["reason"] == "AI_CONFIGURATION_INCOMPLETE"

    configure(master, tenant.org.id, activate=False)
    pending = master.get(f"/organizations/{tenant.org.id}/ai/status").json()["data"]
    assert pending["status"] == "INCOMPLETE" and pending["can_activate"] is True

    state = master.post(f"/organizations/{tenant.org.id}/ai/activate").json()["data"]
    assert state["status"] == "ACTIVE" and state["ready"] is True

    user = login(client, tenant.user_a1)
    assert user.get("/ai/status").json()["data"]["ready"] is True
    assert ask(user).status_code == 200
    # The Org Admins were told AI is available.
    assert login(client, tenant.admin).get("/ai/status").json()["data"]["status"] == "ACTIVE"


def test_activation_notifies_the_org_admins_once(setup, db):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    db.expire_all()
    sent = db.scalars(select(Notification).where(Notification.user_id == tenant.admin.id,
                                                 Notification.type == "AI_STATUS")).all()
    assert len(sent) == 1 and sent[0].title == "AI is now available"
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"question_limit": 100})
    db.expire_all()
    assert len(db.scalars(select(Notification).where(Notification.user_id == tenant.admin.id,
                                                     Notification.type == "AI_STATUS")).all()) == 1


# 7. An invalid key ----------------------------------------------------------------------------------------------

def test_7_a_rejected_key_makes_the_configuration_invalid(setup, client, db):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    # The provider rejected the organization's OpenAI key (set directly: the test calls no provider).
    master.post(f"/organizations/{tenant.org.id}/ai/models",
                json={"provider": "openai", "model_id": "gpt-4o-mini", "service": "answers", "is_default": True})
    from app.modules.auth import mfa

    db.add(OrgAPIKey(organization_id=tenant.org.id, provider="openai", kind="api", key_hint="sk-p••••0000",
                     key_encrypted=mfa.encrypt(client.app.state.settings, "sk-proj-rejected-0000"),
                     is_active=True, validation_status="invalid", details={}))
    db.commit()
    status = master.get(f"/organizations/{tenant.org.id}/ai/status").json()["data"]
    assert status["status"] == "INVALID" and status["code"] == "AI_CONFIGURATION_INVALID"
    refused = ask(login(client, tenant.user_a1))
    assert refused.status_code == 409 and refused.json()["error"]["reason"] == "AI_CONFIGURATION_INVALID"


def test_a_key_that_cannot_use_the_assigned_model_is_invalid(setup, client, db):
    tenant, _, master = setup
    master.post(f"/organizations/{tenant.org.id}/ai/models",
                json={"provider": "openai", "model_id": "gpt-4o", "service": "answers"})
    from app.modules.auth import mfa

    db.add(OrgAPIKey(organization_id=tenant.org.id, provider="openai", kind="api", key_hint="sk-p••••1111",
                     key_encrypted=mfa.encrypt(client.app.state.settings, "sk-proj-limited-1111"),
                     is_active=True, validation_status="valid", details={"models": ["gpt-4o-mini"]}))
    db.commit()
    status = master.get(f"/organizations/{tenant.org.id}/ai/status").json()["data"]
    assert status["status"] == "INVALID" and status["code"] == "AI_MODEL_NOT_ALLOWED"


# 8-9. Usage is recorded with its feature, branch and request ----------------------------------------------------

def test_8_usage_is_recorded_per_feature_branch_and_request(setup, client, db):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    response = ask(login(client, tenant.manager_a))
    assert response.status_code == 200
    db.expire_all()
    question = db.scalars(select(OrgAIUsage).where(OrgAIUsage.organization_id == tenant.org.id,
                                                   OrgAIUsage.service == "question")).one()
    assert question.feature == "qa" and question.branch_id == tenant.branch_a.id
    assert question.request_id and question.user_id == tenant.manager_a.id

    report = login(client, tenant.admin).get(f"/organizations/{tenant.org.id}/ai/usage").json()["data"]
    assert report["totals"]["questions"] == 1
    assert any(g["key"] == str(tenant.branch_a.id) and g["questions"] == 1 for g in report["by_branch"])
    filtered = login(client, tenant.admin).get(f"/organizations/{tenant.org.id}/ai/usage",
                                               params={"branch_id": str(tenant.branch_b.id)}).json()["data"]
    assert filtered["totals"]["questions"] == 0


def test_9_embeddings_are_refused_until_ai_is_ready_and_metered_after(setup, app, db):
    tenant, _, master = setup
    settings = app.state.settings
    with pytest.raises(AIEmbeddingBlocked) as refused:
        organization_embedder(db, app.state.session_factory, settings, tenant.org.id, None)
    assert refused.value.code == "AI_NOT_CONFIGURED"
    configure(master, tenant.org.id)
    from app.infrastructure.ai.embeddings.factory import create_embedder

    embedder = organization_embedder(db, app.state.session_factory, settings, tenant.org.id, create_embedder(settings))
    embedder.embed(["Processing fee is 1% of the loan amount."])
    db.expire_all()
    row = db.scalars(select(OrgAIUsage).where(OrgAIUsage.organization_id == tenant.org.id,
                                              OrgAIUsage.service == "embeddings")).one()
    assert row.feature == "indexing" and row.status == UsageStatus.SETTLED


# 10-13. Limits: blocked at 100%, alerts once per threshold, a raise resumes at once ---------------------------

def test_10_and_13_a_used_up_question_limit_blocks_and_raising_it_resumes_at_once(setup, client):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 1})
    user = login(client, tenant.user_a1)
    assert ask(user).status_code == 200
    refused = ask(user)
    assert refused.status_code == 429 and refused.json()["error"]["reason"] == "QUESTION_LIMIT_REACHED"
    status = user.get("/ai/status").json()["data"]
    assert status["status"] == "LIMIT_REACHED" and status["ready"] is False

    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 5})
    assert ask(user).status_code == 200


def test_11_a_used_up_budget_blocks_paid_calls(setup, app, db):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    master.put(f"/organizations/{tenant.org.id}/ai/credits", json={"budget_usd": 0.000001})
    db.add(OrgAIUsage(organization_id=tenant.org.id, service="answers", provider="openai", model="gpt-4o-mini",
                      status=UsageStatus.SETTLED, input_tokens=100, output_tokens=100, embedding_tokens=0,
                      cost_usd=0.01, meta={}))
    db.commit()
    llm = OrganizationLLM(StreamingLLM(), app.state.session_factory, app.state.settings, tenant.org.id,
                          provider="openai", user_id=None)
    with pytest.raises(AIBlocked) as refused:
        llm.generate_json("system", "user", {})
    assert refused.value.code == "CREDIT_LIMIT_REACHED"


def test_12_alerts_go_once_per_threshold_and_again_after_the_limit_changes(setup, client, db):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 10})
    user = login(client, tenant.user_a1)

    def alerts():
        db.expire_all()
        return [n.data["threshold"] for n in db.scalars(select(Notification).where(
            Notification.user_id == tenant.admin.id, Notification.type == "AI_THRESHOLD")
            .order_by(Notification.created_at))]

    for _ in range(7):
        ask(user)
    assert alerts() == [70]
    ask(user)
    assert alerts() == [70, 80]
    ask(user)
    ask(user)  # 90%, then 100%
    assert alerts() == [70, 80, 90, 100]
    ask(user)  # refused: nothing new
    assert alerts() == [70, 80, 90, 100]
    body = db.scalars(select(Notification).where(Notification.user_id == tenant.admin.id,
                                                 Notification.type == "AI_THRESHOLD")).first().body
    assert "7 of 10 questions" in body

    # 13. A higher limit re-arms the thresholds above what is used now (10 of 20 = 50%).
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 20})
    for _ in range(4):
        ask(user)  # 14 of 20 = 70%
    assert alerts()[-1] == 70 and len(alerts()) == 5


# 14. Suspension stops AI at once ---------------------------------------------------------------------------------

def test_14_suspending_blocks_at_once_even_calls_already_prepared(setup, client, app):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    llm = organization_llm(app.state.session_factory(), app.state.session_factory, app.state.settings,
                           tenant.org.id)
    suspended = master.post(f"/organizations/{tenant.org.id}/ai/suspend", json={"reason": "Billing review"})
    assert suspended.json()["data"]["status"] == "SUSPENDED"
    with pytest.raises(AIBlocked) as refused:
        llm.generate_json("system", "user", {})
    assert refused.value.code == "AI_SUSPENDED"
    response = ask(login(client, tenant.user_a1))
    assert response.status_code == 403 and response.json()["error"]["reason"] == "AI_SUSPENDED"
    admin_view = login(client, tenant.admin).get("/ai/status").json()["data"]
    assert admin_view["status"] == "SUSPENDED" and "Billing review" in admin_view["message"]

    assert master.post(f"/organizations/{tenant.org.id}/ai/activate").json()["data"]["status"] == "ACTIVE"
    assert ask(login(client, tenant.user_a1)).status_code == 200


# 16. A provider failure after tokens were spent is still billed ---------------------------------------------------

def test_16_a_failure_after_the_answer_started_counts_the_estimate(setup, app, db):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    llm = OrganizationLLM(StreamingLLM(), app.state.session_factory, app.state.settings, tenant.org.id,
                          provider="openai", user_id=tenant.user_a1.id, feature="qa")
    with pytest.raises(ConnectionError):
        list(llm.stream_json("system prompt", "user prompt", {}))
    db.expire_all()
    row = db.scalars(select(OrgAIUsage).where(OrgAIUsage.organization_id == tenant.org.id,
                                              OrgAIUsage.service == "answers")).one()
    assert row.status == UsageStatus.SETTLED and row.estimated is True and float(row.cost_usd) > 0
    assert row.error == "ConnectionError" and row.feature == "qa"


# 17. Tenant isolation ------------------------------------------------------------------------------------------

def test_17_an_organization_never_sees_another_organizations_ai(setup, client):
    tenant, other, master = setup
    configure(master, other.org.id)
    admin = login(client, tenant.admin)
    assert admin.get(f"/organizations/{other.org.id}/ai/status").status_code == 403
    assert admin.get(f"/organizations/{other.org.id}/ai/usage").status_code == 403
    assert admin.post(f"/organizations/{tenant.org.id}/ai/activate").status_code == 403
    # Another organization's activation does nothing for this one.
    assert ask(login(client, tenant.user_a1)).json()["error"]["reason"] == "AI_NOT_CONFIGURED"


# 19. A new period: the counters start again ----------------------------------------------------------------------

def test_19_a_new_period_starts_with_the_full_quota(setup, client, db):
    tenant, _, master = setup
    configure(master, tenant.org.id)
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 1})
    user = login(client, tenant.user_a1)
    ask(user)
    assert user.get("/ai/status").json()["data"]["status"] == "LIMIT_REACHED"
    db.execute(update(OrgAIUsage).where(OrgAIUsage.organization_id == tenant.org.id)
               .values(created_at=datetime.now(UTC) - timedelta(days=1)))
    db.commit()
    assert user.get("/ai/status").json()["data"]["status"] == "ACTIVE"
    assert ask(user).status_code == 200


# The migration keeps AI on for organizations the Master Admin had already configured --------------------------

def test_settings_rows_belong_to_one_organization(setup, db):
    tenant, other, master = setup
    configure(master, tenant.org.id)
    db.expire_all()
    rows = db.scalars(select(OrgAISettings)).all()
    assert [r.organization_id for r in rows] == [tenant.org.id] and rows[0].is_active
    assert uuid.UUID(str(rows[0].organization_id)) != other.org.id
