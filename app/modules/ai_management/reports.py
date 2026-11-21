"""AI usage reports from the org_ai_usage ledger: one organization's (Org Admin, Master Admin) and the platform's
(the Master Admin dashboard). Settled rows only: a running call's reservation is not usage yet, and a failed call
recorded nothing. Every figure comes from the database; nothing is assumed or filled in.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.modules.ai_management.model import AIFeature, OrgAIUsage, UsageStatus
from app.modules.ai_management.schema import (
    OrganizationAIRow, PlatformAIReport, UsageDay, UsageGroup, UsageReport, UsageTotals,
)

FEATURE_LABELS = {
    AIFeature.QA: "AI questions", AIFeature.SEARCH: "Search", AIFeature.SUMMARY: "Document summaries",
    AIFeature.DOCUMENT_ANALYSIS: "Document analysis", AIFeature.CLASSIFICATION: "Upload classification",
    AIFeature.TICKET_TRIAGE: "Ticket triage", AIFeature.INDEXING: "Document indexing", AIFeature.OTHER: "Other",
}
MAX_DAYS = 366
TOP = 10


def _columns():
    u = OrgAIUsage
    return (
        func.count().filter(u.service == "question"),
        func.count().filter(u.service != "question"),
        func.coalesce(func.sum(u.input_tokens), 0),
        func.coalesce(func.sum(u.output_tokens), 0),
        func.coalesce(func.sum(u.embedding_tokens), 0),
        func.coalesce(func.sum(u.cost_usd), 0),
        func.coalesce(func.sum(u.cost_usd).filter(u.estimated), 0),
    )


def _totals(row, failed: int = 0) -> dict:
    questions, calls, inp, out, emb, cost, est = row
    cost, est = float(cost or 0), float(est or 0)
    return {"questions": int(questions or 0), "calls": int(calls or 0), "failed_calls": failed,
            "input_tokens": int(inp or 0), "output_tokens": int(out or 0), "embedding_tokens": int(emb or 0),
            "total_tokens": int(inp or 0) + int(out or 0) + int(emb or 0), "cost_usd": round(cost, 6),
            "estimated_cost_usd": round(est, 6), "actual_cost_usd": round(cost - est, 6)}


class Filters:
    """The report's scope: organizations (None: all), period and the optional filters."""

    def __init__(self, *, organization_ids: list[uuid.UUID] | None, since: datetime | None, until: datetime | None,
                 branch_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None, feature: str | None = None,
                 model: str | None = None, provider: str | None = None) -> None:
        now = datetime.now(UTC)
        self.until = min(until or now, now + timedelta(days=1))
        start = since or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        self.since = max(start, self.until - timedelta(days=MAX_DAYS))
        self.organization_ids = organization_ids
        self.branch_id, self.user_id, self.feature = branch_id, user_id, feature
        self.model, self.provider = model, provider

    def where(self, *, period: bool = True, settled: bool = True) -> list:
        u = OrgAIUsage
        conditions = []
        if settled:
            conditions.append(u.status == UsageStatus.SETTLED)
        if period:
            conditions += [u.created_at >= self.since, u.created_at < self.until]
        if self.organization_ids is not None:
            conditions.append(u.organization_id.in_(self.organization_ids))
        if self.branch_id:
            conditions.append(u.branch_id == self.branch_id)
        if self.user_id:
            conditions.append(u.user_id == self.user_id)
        if self.feature:
            conditions.append(u.feature == self.feature)
        if self.model:
            conditions.append(u.model == self.model)
        if self.provider:
            conditions.append(u.provider == self.provider)
        return conditions


def _sum(db, conditions) -> UsageTotals:
    row = db.execute(select(*_columns()).where(*conditions)).one()
    return UsageTotals(**_totals(row))


def _grouped(db, column, conditions, labels: dict | None = None, *, limit: int | None = None) -> list[UsageGroup]:
    rows = db.execute(select(column, *_columns()).where(*conditions).group_by(column)).all()
    groups = []
    for key, *values in rows:
        totals = _totals(values)
        name = str(key) if key is not None else None
        groups.append(UsageGroup(key=name, label=(labels or {}).get(name, name) if name else "Not recorded", **totals))
    groups.sort(key=lambda g: (g.cost_usd, g.total_tokens, g.questions), reverse=True)
    return groups[:limit] if limit else groups


def usage_report(db, filters: Filters) -> UsageReport:
    from app.modules.branches.model import Branch
    from app.modules.users.model import User

    u = OrgAIUsage
    where = filters.where()
    failed = db.scalar(select(func.count()).where(u.status == UsageStatus.FAILED, *filters.where(settled=False)))
    totals = _sum(db, where)
    totals.failed_calls = int(failed or 0)

    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    rest = filters.where(period=False)
    today = _sum(db, [*rest, u.created_at >= today_start])
    month = _sum(db, [*rest, u.created_at >= month_start])

    day = func.date_trunc("day", func.timezone("UTC", u.created_at)).label("day")
    rows = db.execute(select(day, *_columns()).where(*where).group_by(day)).all()
    by_day = {r[0].date(): _totals(r[1:]) for r in rows}
    daily, cursor = [], filters.since.astimezone(UTC).date()
    last = (filters.until - timedelta(microseconds=1)).astimezone(UTC).date()
    while cursor <= last:
        t = by_day.get(cursor)
        daily.append(UsageDay(date=cursor.isoformat(), questions=t["questions"] if t else 0,
                              input_tokens=t["input_tokens"] if t else 0, output_tokens=t["output_tokens"] if t else 0,
                              embedding_tokens=t["embedding_tokens"] if t else 0,
                              cost_usd=round(t["cost_usd"], 6) if t else 0.0))
        cursor += timedelta(days=1)

    branch_names = {str(i): n for i, n in db.execute(select(Branch.id, Branch.name).where(
        Branch.organization_id.in_(filters.organization_ids)) if filters.organization_ids is not None
        else select(Branch.id, Branch.name)).all()}
    by_branch = _grouped(db, u.branch_id, where, branch_names, limit=TOP)
    for group in by_branch:
        if group.key is None:
            group.label = "Organization level / background"
    by_user = _grouped(db, u.user_id, where, limit=TOP)
    users = {str(i): (n, e) for i, n, e in db.execute(select(User.id, User.full_name, User.email).where(
        User.id.in_([uuid.UUID(g.key) for g in by_user if g.key]))).all()} if by_user else {}
    for group in by_user:
        if group.key is None:
            group.label = "Background processing"
        elif group.key in users:
            group.label = users[group.key][0] or users[group.key][1]

    scope = filters.where(period=False, settled=False)
    feature_values = sorted(f for f in db.scalars(select(u.feature).where(*scope).distinct()) if f)
    model_values = sorted(m for m in db.scalars(select(u.model).where(*scope, u.service != "question").distinct()) if m)
    provider_values = sorted(p for p in db.scalars(select(u.provider).where(*scope, u.service != "question").distinct())
                             if p)
    return UsageReport(
        since=filters.since, until=filters.until, totals=totals, today=today, this_month=month, daily=daily,
        by_feature=_grouped(db, u.feature, where, {str(k): v for k, v in FEATURE_LABELS.items()}),
        by_model=[g for g in _grouped(db, u.model, where) if g.key != "question"],
        by_branch=by_branch, by_user=by_user,
        filters={
            "branches": [{"value": k, "label": v} for k, v in sorted(branch_names.items(), key=lambda kv: kv[1])],
            "features": [{"value": f, "label": FEATURE_LABELS.get(f, f)} for f in feature_values],
            "models": [{"value": m, "label": m} for m in model_values],
            "providers": [{"value": p, "label": p} for p in provider_values],
        },
    )


def platform_report(service, filters: Filters) -> PlatformAIReport:
    """Every organization's AI status, and the platform's usage (the Master Admin dashboard)."""
    from app.modules.ai_management.status import AIStatus, readiness
    from app.modules.organizations.model import Organization

    db = service.db
    orgs = list(db.execute(select(Organization.id, Organization.name).where(Organization.deleted_at.is_(None))
                           .order_by(Organization.name)).all())
    usage = usage_report(db, filters)
    per_org = {str(k): v for k, v in ((g.key, g) for g in _grouped(db, OrgAIUsage.organization_id, filters.where()))}
    counts = {s.value: 0 for s in AIStatus}
    rows: list[OrganizationAIRow] = []
    for org_id, name in orgs:
        if filters.organization_ids is not None and org_id not in filters.organization_ids:
            continue
        state = readiness(service, org_id)
        counts[state.status] += 1
        meters = service._meters(org_id)
        quota = service._quota(org_id)
        tokens = service._tokens(org_id)
        credit = service._credit(org_id)
        group = per_org.get(str(org_id))
        rows.append(OrganizationAIRow(
            organization_id=org_id, name=name, status=state.status, message=state.admin_message,
            questions_limit=quota.question_limit if quota and quota.is_enabled else None,
            total_token_limit=tokens.total_token_limit if tokens and tokens.is_enabled else None,
            budget_usd=float(credit.budget_usd) if credit and credit.is_enabled and credit.budget_usd is not None else None,
            peak_percent=max((m.percent or 0 for m, _, _ in meters.values()), default=None) if meters else None,
            **(group.model_dump(exclude={"key", "label"}) if group else {}),
        ))
    severity = {AIStatus.INVALID: 0, AIStatus.SUSPENDED: 1, AIStatus.LIMIT_REACHED: 2, AIStatus.INCOMPLETE: 3,
                AIStatus.NOT_CONFIGURED: 4, AIStatus.ACTIVE: 9}
    attention = sorted((r for r in rows if r.status != AIStatus.ACTIVE), key=lambda r: (severity[r.status], r.name))
    summary = {
        "total": len(rows),
        "active": counts[AIStatus.ACTIVE],
        "requiring_configuration": counts[AIStatus.NOT_CONFIGURED] + counts[AIStatus.INCOMPLETE]
        + counts[AIStatus.INVALID] + counts[AIStatus.SUSPENDED],
        "not_configured": counts[AIStatus.NOT_CONFIGURED],
        "incomplete": counts[AIStatus.INCOMPLETE],
        "invalid": counts[AIStatus.INVALID],
        "suspended": counts[AIStatus.SUSPENDED],
        "limit_reached": counts[AIStatus.LIMIT_REACHED],
    }
    rows.sort(key=lambda r: (r.cost_usd, r.total_tokens, r.questions), reverse=True)
    return PlatformAIReport(organizations=summary, attention=attention, usage=usage, by_organization=rows,
                            providers=[p["value"] for p in usage.filters["providers"]])
