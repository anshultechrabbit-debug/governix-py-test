"""Organization-wise AI management: the six modules, permissions, tenant isolation and concurrent spending."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.infrastructure.ai.llm.base import LLMProvider, LLMResult
from app.modules.ai_management.model import OrgAIUsage, OrgAPIKey, UsageStatus
from app.modules.ai_management.runtime import OrganizationLLM, organization_llm, reserve_question
from app.modules.ai_management.service import AIBlocked, AIManagementService, AIModelNotConfigured, window
from app.modules.auth.permissions import Role
from app.modules.notifications.model import Notification
from app.tests.factories import login, make_tenant, make_user

pytestmark = pytest.mark.integration


class PaidLLM(LLMProvider):
    """Reports gpt-4o-mini usage (0.15 / 0.60 USD per 1M tokens) without calling anyone: a token per 4 characters
    of prompt, and `output_tokens` of reply, never more than the budget it was given."""

    model_id = "gpt-4o-mini"
    model = "gpt-4o-mini"

    def __init__(self, output_tokens=500, max_output_tokens=1000):
        self.output_tokens, self.max_output_tokens = output_tokens, max_output_tokens
        self.budgets = []

    def generate_json(self, system, user, schema, *, context=None):
        self.budgets.append(self.max_output_tokens)
        return LLMResult(content={}, model="gpt-4o-mini-2024-07-18", input_tokens=len(system + user) // 4,
                         output_tokens=min(self.output_tokens, self.max_output_tokens))


@pytest.fixture
def setup(client, db):
    tenant = make_tenant(db)
    other = make_tenant(db)
    master_user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    return tenant, other, master_user, login(client, master_user)


def settings_of(auth_client):
    return auth_client.client.app.state.settings


def metered(app, settings, org_id, inner):
    return OrganizationLLM(inner, app.state.session_factory, settings, org_id, provider="openai", user_id=None)


def ledger(db, org_id):
    db.expire_all()
    return db.scalars(select(OrgAIUsage).where(OrgAIUsage.organization_id == org_id)).all()


# 1. API key vault ---------------------------------------------------------------------------------------------------

def test_keys_are_encrypted_masked_and_never_cross_organizations(setup, db):
    tenant, other, _, master = setup
    raw = "local-secret-key-that-must-never-leave-1234"
    added = master.post(f"/organizations/{tenant.org.id}/ai/keys",
                        json={"provider": "local", "api_key": raw, "label": "Primary"})
    assert added.status_code == 201, added.text
    key = added.json()["data"]
    assert raw not in added.text and key["masked_key"] == "loca••••1234" and key["validation_status"] == "valid"
    stored = db.get(OrgAPIKey, uuid.UUID(key["id"]))
    assert raw not in stored.key_encrypted

    overview = master.get(f"/organizations/{tenant.org.id}/ai")
    assert raw not in overview.text and len(overview.json()["data"]["keys"]) == 1
    # Another organization's URL never reaches this key.
    assert master.post(f"/organizations/{other.org.id}/ai/keys/{key['id']}/disable").status_code == 404
    assert master.get(f"/organizations/{other.org.id}/ai").json()["data"]["keys"] == []

    rotated = master.post(f"/organizations/{tenant.org.id}/ai/keys/{key['id']}/rotate",
                          json={"provider": "openai", "api_key": "local-rotated-secret-key-9876"})
    assert rotated.json()["data"]["masked_key"] == "loca••••9876" and rotated.json()["data"]["rotated_at"]

    # A second key for the provider becomes the active one; enabling the first switches back.
    second = master.post(f"/organizations/{tenant.org.id}/ai/keys", json={"provider": "local", "api_key": "x" * 24})
    keys = {k["id"]: k["is_active"] for k in master.get(f"/organizations/{tenant.org.id}/ai").json()["data"]["keys"]}
    assert keys == {key["id"]: False, second.json()["data"]["id"]: True}
    master.post(f"/organizations/{tenant.org.id}/ai/keys/{key['id']}/enable")
    keys = {k["id"]: k["is_active"] for k in master.get(f"/organizations/{tenant.org.id}/ai").json()["data"]["keys"]}
    assert keys == {key["id"]: True, second.json()["data"]["id"]: False}
    disabled = master.post(f"/organizations/{tenant.org.id}/ai/keys/{key['id']}/disable")
    assert disabled.json()["data"]["is_active"] is False


def test_key_changes_need_an_authenticator_code(client, db, settings):
    settings.STEP_UP_ENABLED = True
    tenant = make_tenant(db)
    master_user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    refused = login(client, master_user).post(f"/organizations/{tenant.org.id}/ai/keys",
                                              json={"provider": "local", "api_key": "y" * 24})
    assert refused.status_code == 403 and refused.json()["error"]["reason"] == "STEP_UP_SETUP_REQUIRED"


class FakeOpenAI:
    """OpenAI's answers to the calls Governix makes with an organization's keys (no network)."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, *, params=None, timeout=8):
        self.calls.append((url, headers["Authorization"][7:], params or {}))
        if url.endswith("/models"):
            return 200, {"data": [{"id": i} for i in ("gpt-4o-mini", "gpt-4o-mini-tts", "text-embedding-3-small",
                                                      "dall-e-3", "whisper-1", "gpt-4.1")]}
        if url.endswith("/organization/costs"):
            return 200, {"data": [{"results": [{"amount": {"value": 1.25}}, {"amount": {"value": 0.5}}]}],
                         "has_more": False}
        if url.endswith("/usage/completions"):
            return 200, {"data": [{"results": [{"input_tokens": 9000, "output_tokens": 1000,
                                                "num_model_requests": 12}]}], "has_more": False}
        if url.endswith("/usage/embeddings"):
            return 200, {"data": [{"results": [{"input_tokens": 50000, "num_model_requests": 3}]}], "has_more": False}
        return 404, None


def test_an_openai_key_lists_its_models_and_an_admin_key_reads_what_openai_reports(setup, db, monkeypatch):
    tenant, _, _, master = setup
    fake = FakeOpenAI()
    monkeypatch.setattr("app.modules.ai_management.service._http_get", fake)
    base = f"/organizations/{tenant.org.id}/ai"

    api = master.post(f"{base}/keys", json={"provider": "openai", "api_key": "sk-proj-" + "a" * 40})
    key = api.json()["data"]
    assert key["kind"] == "api" and key["validation_status"] == "valid"
    assert key["available_models"] == ["gpt-4.1", "gpt-4o-mini", "text-embedding-3-small"]  # chat + embedding only
    overview = master.get(base).json()["data"]
    assert overview["catalog"]["providers"] == ["openai"]  # AI_PANEL_PROVIDERS
    assert [m["model_id"] for m in overview["catalog"]["models"]][:3] == ["gpt-4.1", "gpt-4o-mini", "text-embedding-3-small"]
    assert overview["credits"]["provider_account"]["available"] is False

    # An Admin key is told apart by its prefix, never makes AI calls, and reads OpenAI's own figures.
    wrong = master.post(f"{base}/keys", json={"provider": "openai", "kind": "api", "api_key": "sk-admin-" + "b" * 40})
    assert wrong.status_code == 422
    admin = master.post(f"{base}/keys", json={"provider": "openai", "api_key": "sk-admin-" + "b" * 40, "openai_project_id": "proj_bank1"})
    assert admin.json()["data"]["kind"] == "admin" and admin.json()["data"]["openai_project_id"] == "proj_bank1"
    keys = master.get(base).json()["data"]["keys"]
    assert sorted((k["kind"], k["is_active"]) for k in keys) == [("admin", True), ("api", True)]
    service = AIManagementService(db, settings_of(master))
    assert service.decrypted_key(tenant.org.id, "openai") == "sk-proj-" + "a" * 40

    account = master.get(f"{base}/provider-account?refresh=true").json()["data"]
    assert account["available"] and account["scope"] == "project proj_bank1"
    assert (account["spent_usd"], account["input_tokens"], account["output_tokens"], account["embedding_tokens"],
            account["requests"]) == (1.75, 9000, 1000, 50000, 15)
    usage_calls = [c for c in fake.calls if "/organization/" in c[0] and c[2].get("bucket_width")]
    assert usage_calls and all(c[2]["project_ids"] == ["proj_bank1"] for c in usage_calls)
    assert all(c[1].startswith("sk-admin-") for c in usage_calls)  # never the API key

    # Rotating keeps the kind.
    mismatch = master.post(f"{base}/keys/{key['id']}/rotate", json={"provider": "openai", "api_key": "sk-admin-" + "c" * 40})
    assert mismatch.status_code == 422 and mismatch.json()["error"]["reason"] == "KEY_KIND_MISMATCH"


# 2. Models ----------------------------------------------------------------------------------------------------------

def test_assigned_models_decide_and_unassigned_ones_are_never_used(setup, db, app, settings):
    tenant, _, _, master = setup
    base = f"/organizations/{tenant.org.id}/ai"
    # Nothing assigned: the platform's model.
    assert master.get(base).json()["data"]["models"]["answers"]["source"] == "platform"

    first = master.post(f"{base}/models", json={"provider": "local", "model_id": "local-a"})
    assert first.status_code == 201, first.text
    second = master.post(f"{base}/models", json={"provider": "openai", "model_id": "gpt-4o-mini"})
    items = {m["model_id"]: m for m in second.json()["data"]["items"]}
    assert items["local-a"]["is_default"] and not items["gpt-4o-mini"]["is_default"]
    assert master.post(f"{base}/models", json={"provider": "local", "model_id": "local-a"}).status_code == 409

    llm = organization_llm(db, app.state.session_factory, settings, tenant.org.id)
    assert isinstance(llm, OrganizationLLM) and llm.provider_name == "local"

    # Disabling the default hands it to the next enabled model.
    after = master.patch(f"{base}/models/{items['local-a']['id']}", json={"is_enabled": False}).json()["data"]
    assert after["answers"]["model_id"] == "gpt-4o-mini"
    master.patch(f"{base}/models/{items['gpt-4o-mini']['id']}", json={"is_enabled": False})
    # Every assigned model disabled: AI is blocked, never the platform default.
    assert master.get(base).json()["data"]["models"]["answers"] is None
    with pytest.raises(AIModelNotConfigured):
        organization_llm(db, app.state.session_factory, settings, tenant.org.id)
    with pytest.raises(AIModelNotConfigured):
        reserve_question(db, settings, tenant.org.id)


# 3. Question limits -------------------------------------------------------------------------------------------------

def test_the_question_quota_is_shared_and_resets_with_its_period(setup, db, settings):
    tenant, other, _, master = setup
    saved = master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 2})
    assert saved.status_code == 200 and saved.json()["data"]["questions"]["remaining"] == 2
    reserve_question(db, settings, tenant.org.id, tenant.user_a1.id)
    reserve_question(db, settings, tenant.org.id, tenant.user_b1.id)  # another user, the same quota
    with pytest.raises(AIBlocked) as refused:
        reserve_question(db, settings, tenant.org.id, tenant.manager_a.id)
    assert refused.value.code == "QUESTION_LIMIT_REACHED" and refused.value.status_code == 429
    reserve_question(db, settings, other.org.id)  # another organization is not affected

    read = master.get(f"/organizations/{tenant.org.id}/ai").json()["data"]["questions"]["questions"]
    assert read == {"limit": 2, "used": 2, "remaining": 0, "percent": 100.0, "exhausted": True}
    # Yesterday's questions belong to yesterday's period: a new day starts with the full quota.
    db.execute(update(OrgAIUsage).where(OrgAIUsage.organization_id == tenant.org.id)
               .values(created_at=datetime.now(UTC) - timedelta(days=1)))
    db.commit()
    reserve_question(db, settings, tenant.org.id)


def test_custom_periods_count_from_their_start():
    anchor = datetime(2026, 11, 1, tzinfo=UTC)
    assert window("custom", 10, anchor, datetime(2026, 11, 25, tzinfo=UTC)) == (
        datetime(2026, 11, 21, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert window("weekly", None, None, datetime(2026, 11, 19, 15, tzinfo=UTC))[0] == datetime(2026, 11, 16, tzinfo=UTC)
    assert window("monthly", None, None, datetime(2026, 12, 31, 23, tzinfo=UTC))[1] == datetime(2027, 1, 1, tzinfo=UTC)


# 4. Token limits ----------------------------------------------------------------------------------------------------

def test_actual_tokens_are_recorded_and_the_limit_shrinks_then_blocks_calls(setup, db, app, settings):
    tenant, _, _, master = setup
    master.put(f"/organizations/{tenant.org.id}/ai/tokens", json={"period": "monthly", "total_token_limit": 3000})
    inner = PaidLLM(output_tokens=500, max_output_tokens=1000)
    llm = metered(app, settings, tenant.org.id, inner)
    prompt = "x" * 1992  # 500 tokens with the 8-character system prompt

    llm.generate_json("system: ", prompt, {})
    rows = ledger(db, tenant.org.id)
    assert [(r.status, r.input_tokens, r.output_tokens) for r in rows] == [(UsageStatus.SETTLED, 500, 500)]
    assert float(rows[0].cost_usd) == pytest.approx((500 * 0.15 + 500 * 0.60) / 1e6)
    tokens = master.get(f"/organizations/{tenant.org.id}/ai").json()["data"]["tokens"]
    assert tokens["total"]["used"] == 1000 and tokens["total"]["remaining"] == 2000

    llm.generate_json("system: ", prompt, {})
    # 1,000 left: this call's output budget is cut so that even its worst case fits.
    llm.generate_json("system: ", prompt, {})
    assert inner.budgets == [1000, 1000, inner.budgets[-1]] and 256 <= inner.budgets[-1] < 500
    with pytest.raises(AIBlocked) as refused:
        llm.generate_json("system: ", prompt, {})
    assert refused.value.code == "TOKEN_LIMIT_REACHED"
    rows = ledger(db, tenant.org.id)
    assert all(r.status == UsageStatus.SETTLED for r in rows) and len(rows) == 3  # nothing kept for the refusal
    assert sum(r.input_tokens + r.output_tokens for r in rows) <= 3000  # never over the limit


# 5. Credits ---------------------------------------------------------------------------------------------------------

def test_the_credit_budget_is_spent_by_actual_cost_and_then_blocks(setup, db, app, settings):
    tenant, _, _, master = setup
    master.put(f"/organizations/{tenant.org.id}/ai/credits", json={"budget_usd": 0.01})
    llm = metered(app, settings, tenant.org.id, PaidLLM(output_tokens=5_000, max_output_tokens=10_000))
    prompt = "x" * 80_000  # 20,000 tokens
    llm.generate_json("", prompt, {})  # 0.003 + 0.003 = 0.006 USD
    credits = master.get(f"/organizations/{tenant.org.id}/ai").json()["data"]["credits"]
    assert credits["credits"]["used"] == pytest.approx(0.006) and credits["credits"]["remaining"] == pytest.approx(0.004)
    assert credits["provider_account"]["available"] is False  # no Admin key: the internal budget only
    with pytest.raises(AIBlocked):
        llm.generate_json("", prompt, {})  # its input alone no longer fits the 0.004 left
    db.add(OrgAIUsage(organization_id=tenant.org.id, service="answers", provider="openai", model="gpt-4o-mini",
                      status=UsageStatus.SETTLED, input_tokens=0, output_tokens=0, embedding_tokens=0,
                      cost_usd=0.004, meta={}))
    db.commit()
    with pytest.raises(AIBlocked) as refused:
        reserve_question(db, settings, tenant.org.id)
    assert refused.value.code == "CREDIT_LIMIT_REACHED"


def test_unspent_budget_rolls_over_into_the_next_month(setup, db, settings):
    tenant, _, _, master = setup
    master.put(f"/organizations/{tenant.org.id}/ai/credits", json={"budget_usd": 10, "rollover": True})
    month_start, _ = window("monthly", None, None, datetime.now(UTC))
    last_month = month_start - timedelta(days=3)
    from app.modules.ai_management.model import OrgCreditConfig
    db.execute(update(OrgCreditConfig).values(created_at=last_month - timedelta(days=20)))
    db.add(OrgAIUsage(organization_id=tenant.org.id, created_at=last_month, service="answers", provider="openai",
                      model="gpt-4o-mini", status=UsageStatus.SETTLED, input_tokens=0, output_tokens=0,
                      embedding_tokens=0, cost_usd=4, meta={}))
    db.commit()
    credits = AIManagementService(db, settings).credit_read(tenant.org.id, with_balance=False)
    assert credits.carried_over_usd == pytest.approx(6) and credits.credits.limit == pytest.approx(16)


# 6. Notifications ---------------------------------------------------------------------------------------------------

def test_thresholds_alert_master_and_org_admins_once_per_period(setup, db, settings):
    tenant, other, master_user, master = setup
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "monthly", "question_limit": 10})
    for _ in range(7):
        reserve_question(db, settings, tenant.org.id)

    def alerts(user_id):
        db.expire_all()
        return db.scalars(select(Notification).where(Notification.user_id == user_id,
                                                     Notification.type == "AI_THRESHOLD")).all()

    assert [a.data["threshold"] for a in alerts(master_user.id)] == [70]
    assert [a.data["threshold"] for a in alerts(tenant.admin.id)] == [70]
    assert alerts(other.admin.id) == [] and alerts(tenant.user_a1.id) == []
    reserve_question(db, settings, tenant.org.id)  # 80%
    AIManagementService(db, settings).evaluate_alerts(tenant.org.id)  # again: no duplicate
    db.commit()
    assert sorted(a.data["threshold"] for a in alerts(master_user.id)) == [70, 80]

    # Thresholds are configurable per resource; a disabled resource stays quiet.
    master.put(f"/organizations/{tenant.org.id}/ai/notifications/questions", json={"is_enabled": False})
    reserve_question(db, settings, tenant.org.id)  # 90%
    assert sorted(a.data["threshold"] for a in alerts(master_user.id)) == [70, 80]
    listed = master.get(f"/organizations/{tenant.org.id}/ai").json()["data"]["notifications"]
    assert {n["resource"]: n["sent"] for n in listed}["questions"] == [70, 80]


# Permissions --------------------------------------------------------------------------------------------------------

def test_org_admin_reads_only_its_own_limits_and_changes_nothing(setup, client):
    tenant, other, _, master = setup
    master.post(f"/organizations/{tenant.org.id}/ai/keys", json={"provider": "local", "api_key": "z" * 24})
    admin = login(client, tenant.admin)
    own = admin.get(f"/organizations/{tenant.org.id}/ai")
    assert own.status_code == 200
    data = own.json()["data"]
    assert data["can_manage"] is False and data["keys"] == [] and data["notifications"] == [] and data["catalog"] is None
    assert admin.get(f"/organizations/{other.org.id}/ai").status_code == 403
    assert admin.put(f"/organizations/{tenant.org.id}/ai/questions", json={"question_limit": 5}).status_code == 403
    assert login(client, tenant.user_a1).get(f"/organizations/{tenant.org.id}/ai").status_code == 403
    assert login(client, tenant.manager_a).get(f"/organizations/{tenant.org.id}/ai").status_code == 403


def test_a_used_up_quota_refuses_the_question_before_any_ai_call(setup, client):
    tenant, _, _, master = setup
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 1})
    user = login(client, tenant.user_a1)
    user.post("/ai/ask", json={"question": "What is the processing fee?"})
    refused = user.post("/ai/ask", json={"question": "What is the processing fee?"})
    assert refused.status_code == 429 and refused.json()["error"]["reason"] == "QUESTION_LIMIT_REACHED"


# Concurrency --------------------------------------------------------------------------------------------------------

def test_concurrent_questions_never_overspend_the_quota(setup, app, settings):
    tenant, _, _, master = setup
    master.put(f"/organizations/{tenant.org.id}/ai/questions", json={"period": "daily", "question_limit": 5})
    results: list[str] = []

    def ask():
        with app.state.session_factory() as session:
            try:
                reserve_question(session, settings, tenant.org.id)
                results.append("ok")
            except AIBlocked:
                results.append("refused")

    threads = [threading.Thread(target=ask) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 5 and results.count("refused") == 7
