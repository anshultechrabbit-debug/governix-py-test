"""Main Policy -> Sub-Policy -> Policy Document -> Versions, end to end through the real pipeline.

Loan Policy
  Gold Loan       Gold Loan Policy       v1 v2 v3
  Home Loan       Home Loan Policy       v1 v2 v3
  Personal Loan   Personal Loan Policy   v1 v2 v3
  (directly)      General Loan Guidelines v1 v2

Runs with the local LLM and embedder (no OpenAI calls), like the rest of app/tests.
"""

import io
import uuid

import pytest
from alembic import command
from sqlalchemy import select

from app.modules.audit.model import AuditEvent
from app.modules.categories.model import Category
from app.modules.policies.model import Policy, PolicyVersion
from app.modules.search.model import Chunk
from app.tests.conftest import alembic_config
from app.tests.factories import login, make_tenant
from app.tests.pdfs import PolicySpec, Section, build_policy_pdf
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration

PRODUCTS = {
    "Gold Loan": ("GOLD LOAN POLICY", "GL-1", "Gold ornaments of 18 to 22 carat purity are accepted as security",
                  ["75%", "72%", "70%"]),
    "Home Loan": ("HOME LOAN POLICY", "HL-2", "Housing loans finance the purchase or construction of a residential house",
                  ["80%", "78%", "76%"]),
    "Personal Loan": ("PERSONAL LOAN POLICY", "PL-3", "Personal loans are unsecured and repaid from monthly salary",
                      ["24 months", "30 months", "36 months"]),
}
DATES = ["01/01/2024", "01/01/2025", "01/01/2026"]


def product_pdf(product: str, version: int) -> bytes:
    title, number, purpose, figures = PRODUCTS[product]
    sections = [
        Section("1", "Purpose", [f"{purpose}."]),
        Section("2", "Limits", [f"The {product.lower()} limit under this policy is {figures[version - 1]} as set by the Bank."]),
    ]
    # Each version adds a section, as real revisions do, so no two versions read alike.
    sections += [Section(str(2 + n), f"Revision {n}", [f"Revision {n} of the {product.lower()} rules adds clause {n}."])
                 for n in range(1, version)]
    return build_policy_pdf(PolicySpec(
        title=title,
        header_lines=[f"Policy No: {number}", f"Version: {version}", "Issued by: Retail Credit",
                      f"Effective Date: {DATES[version - 1]}"],
        sections=sections,
    ))


def guidelines_pdf(version: int) -> bytes:
    return build_policy_pdf(PolicySpec(
        title="GENERAL LOAN GUIDELINES",
        header_lines=["Policy No: LG-9", f"Version: {version}", "Issued by: Credit Policy Cell",
                      f"Effective Date: {DATES[version]}"],
        sections=[Section("1", "Scope", ["These guidelines apply to every loan product of the Bank."]),
                  Section("2", "Documentation", [f"Every sanction file keeps {version + 2} signed copies of the terms."])],
    ))


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


def folder(session, name, parent=None, category_type="policy"):
    body = {"name": name, **({"parent_id": parent} if parent else {"category_type": category_type})}
    response = session.post("/categories", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def run_batch(session, app, groups, files, publish=True):
    created = session.post("/uploads/batches", json={"groups": groups, "publish": publish})
    assert created.status_code == 201, created.text
    batch = created.json()["data"]
    for group, group_files in zip(batch["groups"], files, strict=True):
        for item, content in zip(group["items"], group_files, strict=True):
            sent = session.post(f"/uploads/batches/{batch['id']}/items/{item['id']}/file",
                                files={"file": (item["original_filename"], io.BytesIO(content), "application/pdf")})
            assert sent.status_code == 200, sent.text
    drain(app)
    assert session.post(f"/uploads/batches/{batch['id']}/start").status_code == 200
    drain(app)
    return session.get(f"/uploads/batches/{batch['id']}").json()["data"]


def loan_library(admin, app):
    """Loan Policy with three sub-policies, three versions in each, and a document directly in it."""
    loan = folder(admin, "Loan Policy")
    subs = {name: folder(admin, name, loan) for name in PRODUCTS}
    batch = run_batch(admin, app, [
        {"new_policy_name": f"{name} Policy", "category_id": subs[name],
         "items": [{"filename": f"{name.lower().replace(' ', '-')}-v{v}.pdf", "version_label": f"V{v}"} for v in (1, 2, 3)]}
        for name in PRODUCTS
    ], [[product_pdf(name, v) for v in (1, 2, 3)] for name in PRODUCTS])
    assert batch["status"] == "completed", batch
    direct = run_batch(admin, app, [{
        "new_policy_name": "General Loan Guidelines", "category_id": loan,
        "items": [{"filename": "guidelines-v1.pdf"}, {"filename": "guidelines-v2.pdf"}],
    }], [[guidelines_pdf(1), guidelines_pdf(2)]])
    assert direct["status"] == "completed", direct
    policies = {g["new_policy_name"]: g["policy_id"] for g in [*batch["groups"], *direct["groups"]]}
    return loan, subs, policies


def search(session, query, **body):
    response = session.post("/search", json={"query": query, **body})
    assert response.status_code == 200, response.text
    return response.json()["data"]["passages"]


def history(db, policy_id):
    return db.scalars(select(PolicyVersion).where(PolicyVersion.policy_id == policy_id)
                      .order_by(PolicyVersion.effective_from)).all()


# --- acceptance: hierarchy, independent histories, retrieval -----------------------------------------


def test_loan_policy_hierarchy_with_independent_version_histories(db, app, admin):
    loan, subs, policies = loan_library(admin, app)

    for name in PRODUCTS:
        policy = db.get(Policy, uuid.UUID(policies[f"{name} Policy"]))
        assert policy.category_id == db.get(Category, uuid.UUID(subs[name])).id
        assert [v.version_label for v in history(db, policy.id)] == ["1", "2", "3"]  # "V2" is stored as "2"
    guidelines = db.get(Policy, uuid.UUID(policies["General Loan Guidelines"]))
    assert str(guidelines.category_id) == loan
    assert [v.version_label for v in history(db, guidelines.id)] == ["1", "2"]
    # Each document's history is its own: no version belongs to two documents.
    all_versions = db.scalars(select(PolicyVersion)).all()
    assert len(all_versions) == 11 and len({v.document_id for v in all_versions}) == 11

    tree = admin.get("/categories/tree").json()["data"]
    node = next(n for n in tree if n["id"] == loan)
    assert (node["sub_policy_count"], node["document_count"], node["total_document_count"], node["total_version_count"]) == (3, 1, 4, 11)
    assert [s["name"] for s in node["sub_policies"]] == list(PRODUCTS)
    assert all((s["document_count"], s["version_count"]) == (1, 3) for s in node["sub_policies"])

    # The folder lists its own documents, each with every version and its lifecycle.
    listed = admin.get(f"/categories/{subs['Gold Loan']}/documents").json()["data"]["items"]
    assert [d["name"] for d in listed] == ["Gold Loan Policy"]
    assert [(v["version_label"], v["lifecycle"]) for v in listed[0]["versions"]] == [
        ("3", "published"), ("2", "superseded"), ("1", "superseded")]
    main_docs = admin.get(f"/categories/{loan}/documents").json()["data"]["items"]
    assert [d["name"] for d in main_docs] == ["General Loan Guidelines"]


def test_retrieval_stays_inside_the_sub_policy_and_version_asked_for(db, app, admin):
    loan, subs, policies = loan_library(admin, app)
    gold, home = policies["Gold Loan Policy"], policies["Home Loan Policy"]

    in_gold = search(admin, "gold ornaments carat purity security", category_ids=[subs["Gold Loan"]])
    assert in_gold and {p["source"]["policy_id"] for p in in_gold} == {gold}
    # The version in force by default: v3 only.
    assert {p["source"]["version_label"] for p in in_gold} == {"3"}
    # Home Loan never answers from Gold Loan, whatever the words.
    in_home = search(admin, "gold ornaments carat purity security", category_ids=[subs["Home Loan"]])
    assert gold not in {p["source"]["policy_id"] for p in in_home}
    assert {p["source"]["policy_id"] for p in in_home} <= {home}
    # The main policy covers its sub-policies and its own documents.
    in_loan = {p["source"]["policy_id"] for p in search(admin, "limit under this policy", category_ids=[loan], limit=50)}
    assert gold in in_loan and home in in_loan

    # An older version, asked for, is answered from that version alone (no fallback to another).
    v1 = history(db, gold)[0]
    older = search(admin, "gold loan limit", mode="versions", version_ids=[str(v1.id)])
    assert older and {p["source"]["version_id"] for p in older} == {str(v1.id)}
    assert any("75%" in p["text"] for p in older) and not any("70%" in p["text"] for p in older)
    # Citations keep page, version and effective date.
    assert older[0]["source"]["page_start"] >= 1 and older[0]["source"]["effective_from"] == "2024-01-01"


def test_older_versions_stay_listed_and_downloadable(db, app, admin):
    _, _, policies = loan_library(admin, app)
    versions = admin.get(f"/policies/{policies['Home Loan Policy']}/versions").json()["data"]
    assert [v["version_label"] for v in versions] == ["3", "2", "1"]
    oldest = versions[-1]
    assert oldest["lifecycle"] == "superseded" and oldest["effective_to"] is not None
    file = admin.get(f"/documents/{oldest['document_id']}/file")
    assert file.status_code == 200 and file.content.startswith(b"%PDF")
    compare = admin.get(f"/policies/{policies['Home Loan Policy']}/compare",
                        params={"base": oldest["id"], "target": versions[0]["id"]})
    assert compare.status_code == 200, compare.text


# --- folders: names, nesting, moves, order -------------------------------------------------------


def test_sub_policy_names_are_unique_within_their_main_policy_only(admin):
    loan = folder(admin, "Loan Policy")
    priority = folder(admin, "Priority Sector")
    folder(admin, "Gold Loan", loan)
    duplicate = admin.post("/categories", json={"name": "gold loan", "parent_id": loan})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["reason"] == "DUPLICATE_CATEGORY"
    # The same name under another main policy is allowed.
    assert admin.post("/categories", json={"name": "Gold Loan", "parent_id": priority}).status_code == 201
    # A sub-policy takes its main policy's type when none is given; a main policy needs one.
    assert admin.post("/categories", json={"name": "Treasury"}).status_code == 422


def test_only_one_level_of_sub_policies(db, admin):
    loan = folder(admin, "Loan Policy")
    gold = folder(admin, "Gold Loan", loan)
    nested = admin.post("/categories", json={"name": "Gold Coins", "parent_id": gold})
    assert nested.status_code == 422 and nested.json()["error"]["reason"] == "NESTING_NOT_ALLOWED"
    # A main policy with sub-policies never becomes a sub-policy.
    other = folder(admin, "Retail Banking")
    moved = admin.post(f"/categories/{loan}/move", json={"parent_id": other, "confirm": True})
    assert moved.status_code == 409 and moved.json()["error"]["reason"] == "HAS_SUB_POLICIES"
    assert db.get(Category, uuid.UUID(loan)).parent_id is None


def test_moving_a_sub_policy_needs_confirmation_and_is_audited(db, app, admin):
    loan, subs, policies = loan_library(admin, app)
    retail = folder(admin, "Retail Banking")
    gold = subs["Gold Loan"]
    unconfirmed = admin.post(f"/categories/{gold}/move", json={"parent_id": retail})
    assert unconfirmed.status_code == 409 and unconfirmed.json()["error"]["reason"] == "MOVE_REQUIRES_CONFIRMATION"
    moved = admin.post(f"/categories/{gold}/move", json={"parent_id": retail, "confirm": True})
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["parent_id"] == retail
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "category.moved"))
    assert event.details["from_parent"] == "Loan Policy" and event.details["to_parent"] == "Retail Banking"
    # Its documents went with it: Retail Banking now covers Gold Loan, Loan Policy no longer does.
    gold_policy = policies["Gold Loan Policy"]
    assert gold_policy in {p["source"]["policy_id"] for p in search(admin, "gold ornaments carat", category_ids=[retail])}
    assert gold_policy not in {p["source"]["policy_id"] for p in search(admin, "gold ornaments carat", category_ids=[loan])}


def test_moving_a_document_to_another_sub_policy_moves_its_chunks(db, app, admin):
    _, subs, policies = loan_library(admin, app)
    gold_policy = policies["Gold Loan Policy"]
    moved = admin.patch(f"/policies/{gold_policy}", json={"category_id": subs["Personal Loan"]})
    assert moved.status_code == 200, moved.text
    chunk_folders = set(db.scalars(select(Chunk.category_id).where(Chunk.policy_id == gold_policy)))
    assert chunk_folders == {db.get(Category, uuid.UUID(subs["Personal Loan"])).id}
    assert search(admin, "gold ornaments carat purity", category_ids=[subs["Personal Loan"]])
    assert not search(admin, "gold ornaments carat purity", category_ids=[subs["Gold Loan"]])
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "policy.recategorized"))
    assert event.details["from_category"] == "Loan Policy › Gold Loan"
    assert event.details["to_category"] == "Loan Policy › Personal Loan"


def test_folders_are_arranged_by_drag_and_drop(admin):
    loan = folder(admin, "Loan Policy")
    a, b, c = (folder(admin, name, loan) for name in ("Gold Loan", "Home Loan", "Personal Loan"))
    assert admin.post(f"/categories/{c}/position", json={"before_id": a}).status_code == 200
    node = next(n for n in admin.get("/categories/tree").json()["data"] if n["id"] == loan)
    assert [s["id"] for s in node["sub_policies"]] == [c, a, b]
    # A sibling of another main policy is not a valid anchor.
    other = folder(admin, "Deposits")
    assert admin.post(f"/categories/{a}/position", json={"after_id": other}).status_code == 422


def test_archiving_a_main_policy_archives_its_sub_policies(db, admin):
    loan = folder(admin, "Loan Policy")
    gold = folder(admin, "Gold Loan", loan)
    assert admin.patch(f"/categories/{loan}", json={"is_active": False}).status_code == 200
    assert db.get(Category, uuid.UUID(gold)).is_active is False
    refused = admin.patch(f"/categories/{gold}", json={"is_active": True})
    assert refused.status_code == 422 and refused.json()["error"]["reason"] == "PARENT_ARCHIVED"


# --- uploads: versions, drafts, AI classification ----------------------------------------------------


def test_duplicate_version_numbers_are_refused_before_upload(db, app, admin):
    _, subs, policies = loan_library(admin, app)
    twice = admin.post("/uploads/batches", json={"groups": [{
        "new_policy_name": "Car Loan Policy", "category_id": subs["Personal Loan"],
        "items": [{"filename": "a.pdf", "version_label": "2"}, {"filename": "b.pdf", "version_label": "v2"}],
    }]})
    assert twice.status_code == 422 and twice.json()["error"]["reason"] == "DUPLICATE_VERSION_LABEL"
    existing = admin.post("/uploads/batches", json={"groups": [{
        "policy_id": policies["Gold Loan Policy"], "items": [{"filename": "gold.pdf", "version_label": "V3"}],
    }]})
    assert existing.status_code == 409 and existing.json()["error"]["reason"] == "VERSION_CONFLICT"
    assert len(history(db, policies["Gold Loan Policy"])) == 3  # nothing overwritten


def test_a_draft_registers_nothing_until_published(db, app, admin):
    loan = folder(admin, "Loan Policy")
    gold = folder(admin, "Gold Loan", loan)
    draft = run_batch(admin, app, [{
        "new_policy_name": "Gold Loan Policy", "category_id": gold,
        "items": [{"filename": "gold-v1.pdf"}, {"filename": "gold-v2.pdf"}],
    }], [[product_pdf("Gold Loan", 1), product_pdf("Gold Loan", 2)]], publish=False)
    assert draft["status"] == "draft" and draft["publish"] is False
    assert db.scalar(select(Policy.id)) is None and db.scalar(select(PolicyVersion.id)) is None
    assert not search(admin, "gold ornaments carat purity")
    pending = admin.get(f"/categories/{gold}/pending").json()["data"]
    assert len(pending) == 2 and all(p["draft"] for p in pending)
    detail = admin.get(f"/documents/{pending[0]['document_id']}").json()["data"]
    assert detail["draft"] is True and detail["filed_by_batch"] is False

    published = admin.post(f"/uploads/batches/{draft['id']}/publish")
    assert published.status_code == 200, published.text
    drain(app)
    batch = admin.get(f"/uploads/batches/{draft['id']}").json()["data"]
    assert batch["status"] == "completed", batch
    assert [v.version_label for v in history(db, batch["groups"][0]["policy_id"])] == ["1", "2"]
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "upload_batch.published")) is not None


def test_ai_classification_only_suggests(db, app, admin):
    loan, subs, policies = loan_library(admin, app)
    folders_before = db.scalars(select(Category.id)).all()
    suggestion = admin.post("/uploads/suggestions", data={"classify": "true"},
                            files={"file": ("gold-v4.pdf", io.BytesIO(product_pdf("Gold Loan", 3) + b"\n%v4"), "application/pdf")})
    assert suggestion.status_code == 200, suggestion.text
    classification = suggestion.json()["data"]["classification"]
    # The file is another version of an existing document: it goes where that document is.
    assert classification["source"] == "existing_document"
    assert classification["existing_policy_id"] == policies["Gold Loan Policy"]
    assert (classification["main_policy_id"], classification["sub_policy_id"]) == (loan, subs["Gold Loan"])
    assert classification["requires_confirmation"] is True and classification["reason"]
    assert 0 < classification["confidence"] <= 1
    # Nothing was created, moved or uploaded.
    assert db.scalars(select(Category.id)).all() == folders_before
    assert len(db.scalars(select(PolicyVersion.id)).all()) == 11


def test_ai_classification_without_a_model_uses_the_folders_words(admin):
    loan = folder(admin, "Loan Policy")
    gold = admin.post("/categories", json={"name": "Gold Loan", "parent_id": loan, "keywords": ["gold ornaments", "carat"]})
    gold_id = gold.json()["data"]["id"]
    # Worded so that no seeded folder ("Policies", "Circulars" ...) shares its words.
    pdf = build_policy_pdf(PolicySpec(title="GOLD ORNAMENT LENDING", header_lines=["Reference: GL-1"], sections=[
        Section("1", "Security", ["Gold ornaments of 22 carat purity are accepted as security.",
                                  "Gold ornaments are weighed and valued by the branch appraiser before disbursal.",
                                  "The carat purity is recorded on the receipt given to the borrower."]),
    ]))
    suggestion = admin.post("/uploads/suggestions", data={"classify": "true"},
                            files={"file": ("x.pdf", io.BytesIO(pdf), "application/pdf")})
    classification = suggestion.json()["data"]["classification"]
    assert classification["source"] == "keywords"
    assert (classification["main_policy_id"], classification["sub_policy_id"]) == (loan, gold_id)


def test_filing_elsewhere_than_the_ai_suggested_is_audited(db, app, admin):
    loan = folder(admin, "Loan Policy")
    gold, home = folder(admin, "Gold Loan", loan), folder(admin, "Home Loan", loan)
    batch = run_batch(admin, app, [{
        "new_policy_name": "Gold Loan Policy", "category_id": home,
        "classification": {"category_id": gold, "main_policy_id": loan, "confidence": 0.82,
                           "reason": "It is about gold ornaments.", "source": "ai"},
        "items": [{"filename": "gold-v1.pdf"}],
    }], [[product_pdf("Gold Loan", 1)]])
    assert batch["status"] == "completed", batch
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "classification.overridden"))
    assert event.details["suggested"] == "Loan Policy › Gold Loan" and event.details["chosen"] == "Loan Policy › Home Loan"
    assert event.details["confidence"] == 0.82


# --- permissions and tenant isolation ------------------------------------------------------------------


def test_only_org_admins_manage_folders_but_branch_managers_upload_into_them(client, app, tenant, admin):
    loan = folder(admin, "Loan Policy")
    gold = folder(admin, "Gold Loan", loan)
    manager = login(client, tenant.manager_a)
    assert manager.post("/categories", json={"name": "Home Loan", "parent_id": loan}).status_code == 403
    assert manager.post(f"/categories/{gold}/move", json={"parent_id": None, "confirm": True}).status_code == 403
    assert manager.post(f"/categories/{gold}/position", json={}).status_code == 403
    assert manager.get("/categories/tree").status_code == 200
    batch = run_batch(manager, app, [{"new_policy_name": "Gold Loan Policy", "category_id": gold,
                                      "items": [{"filename": "gold-v1.pdf"}]}], [[product_pdf("Gold Loan", 1)]])
    assert batch["status"] == "completed", batch
    user = login(client, tenant.user_a1)
    assert user.post("/categories", json={"name": "X Loan", "parent_id": loan}).status_code == 403
    assert user.post("/uploads/batches", json={"groups": [{"category_id": gold, "items": [{"filename": "a.pdf"}]}]}).status_code == 403


def test_folders_and_documents_never_cross_organisations(db, client, app, admin):
    loan, subs, policies = loan_library(admin, app)
    other = make_tenant(db)
    outsider = login(client, other.admin)
    assert outsider.get(f"/categories/{subs['Gold Loan']}").status_code == 404
    assert outsider.get(f"/categories/{subs['Gold Loan']}/documents").status_code == 404
    refused = outsider.post("/categories", json={"name": "Gold Loan", "parent_id": loan})
    assert refused.status_code == 422 and refused.json()["error"]["reason"] == "PARENT_NOT_FOUND"
    assert outsider.post(f"/categories/{subs['Gold Loan']}/move", json={"parent_id": None, "confirm": True}).status_code == 404
    assert loan not in {n["id"] for n in outsider.get("/categories/tree").json()["data"]}
    assert not search(outsider, "gold ornaments carat purity", category_ids=[loan])
    assert outsider.patch(f"/policies/{policies['Gold Loan Policy']}", json={"category_id": subs["Home Loan"]}).status_code == 404


# --- migration -------------------------------------------------------------------------------------------


def test_migration_keeps_every_category_and_document(db, test_database_url, client, app, admin, tenant):
    """Upgrade leaves every existing category a main policy with its documents; downgrade flattens safely."""
    _, subs, policies = loan_library(admin, app)
    # A sub-policy with the name of a main policy: the downgrade must rename it, not fail or drop it.
    circulars = db.scalar(select(Category).where(Category.organization_id == tenant.org.id, Category.slug == "circulars"))
    clash = folder(admin, circulars.name, str(db.scalar(select(Category.id).where(Category.name == "Loan Policy"))))
    counts = (len(db.scalars(select(Category.id)).all()), len(db.scalars(select(Policy.id)).all()),
              len(db.scalars(select(PolicyVersion.id)).all()), len(db.scalars(select(Chunk.id)).all()))
    db.close()
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "c8e1f4a7b2d5")
        command.upgrade(config, "head")
    finally:
        command.upgrade(config, "head")
    db.expire_all()
    after = (len(db.scalars(select(Category.id)).all()), len(db.scalars(select(Policy.id)).all()),
             len(db.scalars(select(PolicyVersion.id)).all()), len(db.scalars(select(Chunk.id)).all()))
    assert after == counts
    # After the round trip every category is a main policy again; documents stay in their folder.
    assert db.scalar(select(Category.id).where(Category.parent_id.is_not(None))) is None
    assert db.get(Category, uuid.UUID(clash)).name == f"{circulars.name} (Loan Policy)"
    assert str(db.get(Policy, uuid.UUID(policies["Gold Loan Policy"])).category_id) == subs["Gold Loan"]
    assert len(history(db, policies["Gold Loan Policy"])) == 3


def test_a_branch_manager_offers_a_new_version_of_a_bank_wide_policy_for_approval(db, client, app, tenant, admin):
    """A Branch Manager may not change a bank-wide policy, but may send its new version to an Org Admin."""
    from app.modules.notifications.model import Notification

    loan = folder(admin, "Loan Policy")
    gold = folder(admin, "Gold Loan", loan)
    first = run_batch(admin, app, [{"new_policy_name": "Gold Loan Policy", "category_id": gold,
                                    "items": [{"filename": "gold-v1.pdf"}]}], [[product_pdf("Gold Loan", 1)]])
    policy_id = first["groups"][0]["policy_id"]
    assert db.get(Policy, uuid.UUID(policy_id)).branch_id is None  # bank-wide

    manager = login(client, tenant.manager_a)
    batch = run_batch(manager, app, [{"policy_id": policy_id, "items": [{"filename": "gold-v2.pdf"}]}],
                      [[product_pdf("Gold Loan", 2)]])
    item = batch["groups"][0]["items"][0]
    assert item["status"] == "needs_review" and "approval" in item["message"]
    assert len(history(db, policy_id)) == 1  # nothing registered by the Branch Manager
    notice = db.scalar(select(Notification).where(Notification.user_id == tenant.admin.id,
                                                  Notification.type == "VERSION_APPROVAL"))
    assert notice is not None and notice.link == f"/documents/{item['document_id']}"
    # Approving registers it.
    analysis = admin.get(f"/documents/{item['document_id']}/analysis").json()["data"]
    approved = admin.post(f"/documents/{item['document_id']}/confirm", json={
        "action": "add_version", "policy_id": policy_id,
        "version": {"version_label": "2", "effective_from": analysis["detected"]["effective_date"]},
    })
    assert approved.status_code == 200, approved.text
    assert [v.version_label for v in history(db, policy_id)] == ["1", "2"]
    # A direct change to the policy itself is still refused.
    assert manager.patch(f"/policies/{policy_id}", json={"name": "Renamed"}).status_code in (403, 404)
