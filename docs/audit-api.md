# Audit log API

Reference for building audit log screens. There are two read-only endpoints. The Master Admin sees every
organization; an Org Admin sees only their own.

## Base URL

| From | Base URL |
|---|---|
| The React app | `/api` (the Vite proxy / nginx forwards it to the backend). Use the existing `api` client |
| curl on the LAN | `http://192.168.1.134:8000` (no `/api` prefix) |

The examples below use `API=http://192.168.1.134:8000`.

## Authentication

The session lives in cookies only. No token appears in any response body, and an `Authorization` header is
ignored. With curl, log in once and keep the cookies in a file:

```bash
API=http://192.168.1.134:8000

curl -s -c cookies.txt -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"<YOUR_EMAIL>","password":"<YOUR_PASSWORD>"}'
```

- The access cookie lasts **15 minutes**. On `401 TOKEN_EXPIRED`, log in again (the React client refreshes on its
  own).
- Both audit endpoints are `GET`, so no `X-CSRF-Token` header is needed.
- In the browser, the `api` client sends the cookies already. Don't add headers.

## Who can call it

| Role | Access |
|---|---|
| Master Admin | Every organization's events plus platform events. May filter with `organization_id` |
| Org Admin | Only their own organization's events. `organization_id` is ignored |
| Branch Manager, User | `403` |

Show the screen only when `me.permissions` includes `"audit:read"`. To tell the two views apart, use
`me.role === "master_admin"` (show an organization picker) or `"org_admin"` (no picker).

## 1. List events: `GET /audit-events`

Newest first, paginated.

| Query param | Type | Notes |
|---|---|---|
| `action` | string ≤ 100 | Exact match, e.g. `ai.query`. One per request |
| `resource_type` | string ≤ 50 | e.g. `user`, `document`, `policy`, `organization`, `branch` |
| `resource_id` | UUID | One object's history |
| `actor_user_id` | UUID | Everything one user did |
| `since` | ISO date-time | Events at or after it, e.g. `2026-10-01T00:00:00Z`. There is no `until` |
| `organization_id` | UUID | **Master Admin only.** Omit it for all organizations |
| `limit` | 1–200 | Default 50 |
| `offset` | ≥ 0 | Default 0 |

```bash
# Latest 50
curl -s -b cookies.txt "$API/audit-events"

# One event type, 100 per page, second page
curl -s -b cookies.txt "$API/audit-events?action=auth.login_failed&limit=100&offset=100"

# Since a date
curl -s -b cookies.txt -G "$API/audit-events" --data-urlencode "since=2026-10-01T00:00:00Z"

# One user's actions / one document's history
curl -s -b cookies.txt "$API/audit-events?actor_user_id=<USER_UUID>"
curl -s -b cookies.txt "$API/audit-events?resource_type=document&resource_id=<DOCUMENT_UUID>"

# Master Admin: one organization only
curl -s -b cookies.txt "$API/audit-events?organization_id=<ORGANIZATION_UUID>"
```

**Response**

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "8f0c…",
        "organization_id": "d81d8ac2-7d9c-490b-bf60-c816300b2ae7",
        "actor_user_id": "1b2e…",
        "action": "user.created",
        "resource_type": "user",
        "resource_id": "5a77…",
        "request_id": "c3f1…",
        "details": { "email": "manager@bank.example", "role": "branch_manager" },
        "created_at": "2026-10-29T10:42:13.512Z",
        "actor_name": "Anshul Jha",
        "actor_email": "admin@bank.example",
        "actor_role": "Organization Admin",
        "organization_name": "Acme Bank",
        "resource_name": "Priya Shah (manager@bank.example)",
        "action_label": "User added",
        "category": "Users",
        "severity": "info",
        "summary": "Anshul Jha added Priya Shah (manager@bank.example) as Branch Manager.",
        "facts": [
          { "label": "Role", "value": "Branch Manager" },
          { "label": "Email", "value": "manager@bank.example" }
        ]
      }
    ],
    "total": 1342,
    "limit": 50,
    "offset": 0
  },
  "request_id": "…"
}
```

**Ready-to-show fields.** Use these in the UI instead of the raw ids and `details`:

| Field | What to show |
|---|---|
| `summary` | The main line: one plain-English sentence saying what happened |
| `action_label` | Short title or badge, e.g. "User added", "Failed sign-in", "Organization updated" |
| `category` | Group or filter chip: `Sign-in & security`, `Users`, `Organizations`, `Branches & departments`, `Policies`, `Documents`, `Assistant`, `Support` |
| `severity` | `info` (neutral), `warning` (amber: deletions, failed sign-ins, rejections, withdrawals), `critical` (red: account locked, suspected stolen session) |
| `actor_name`, `actor_email`, `actor_role` | Who did it. All `null` for system events and failed sign-ins |
| `organization_name` | Which organization. `null` for platform events |
| `resource_name` | What it was done to: a user as "Name (email)", or a branch, policy, document or ticket name |
| `facts` | The details as `label: value` lines for the details drawer, with ids and hashes left out, roles spelled out and booleans as Yes/No |

Names are looked up at read time, including deleted users and records, so old events still show who and what. The raw `details` stay in the response if you need them.

Sample `summary` lines:
- "Anshul Jha signed in."
- "Someone tried to sign in as Priya Shah (priya@bank.example) with a wrong password. The account is now locked for a while."
- "Master Admin suspended “Acme Bank”. Its users can no longer sign in."
- "Anshul Jha created the branch “Downtown” (code DT01)."
- "Riya asked the assistant “What is the FOIR limit?”. No answer was given: the documents don't state it."

- `organization_id` is `null` for platform events (e.g. Master Admin actions).
- `actor_user_id` is `null` for system events (workers, failed logins for unknown emails).
- Paging: there are more events while `offset + limit < total`.

## 2. Event types: `GET /audit-events/actions`

Every `action` value recorded in the caller's scope, sorted. Use it to fill the event-type filter dropdown.

```bash
curl -s -b cookies.txt "$API/audit-events/actions"
curl -s -b cookies.txt "$API/audit-events/actions?organization_id=<ORGANIZATION_UUID>"   # Master Admin
```

```json
{ "success": true, "data": ["ai.query", "auth.login", "auth.login_failed", "branch.created", "user.created"], "request_id": "…" }
```

## Event types you'll see

Group them in the UI by the part before the dot.

| Group | Actions |
|---|---|
| Sign-in and passwords | `auth.login`, `auth.login_failed`, `auth.refresh_token_reuse`, `auth.password_changed`, `auth.password_reset_requested`, `auth.password_reset_completed` |
| Users | `user.created`, `user.updated`, `user.deleted`, `user.password_reset`, `user.master_admin_bootstrapped` |
| Organizations | `organization.created`, `organization.updated`, `organization.logo_updated`, `organization.nginx_deployed`, `organization.nginx_disabled` |
| Branches, departments, categories | `branch.created`, `branch.updated`, `department.created`, `department.updated`, `category.created`, `category.updated` |
| Policies | `policy.created`, `policy.updated`, `policy.deleted`, `policy.moved`, `policy.assigned`, `policy.unassigned`, `policy.versions_reordered`, `policy.latest_version_changed` |
| Documents | `document.uploaded`, `document.confirmed`, `document.rejected`, `document.updated`, `document.moved`, `document.archived`, `document.deleted`, `document.retry`, `document.duplicate_override`, `document.viewed`, `document.accessed` |
| Assistant | `ai.query` |
| Support | `ticket.created`, `ticket.assigned`, `ticket.status_changed` |

Use `/audit-events/actions` as the source of truth; this table is a guide for labels and icons.

`details` differs by action. Show it as a key/value list or collapsible JSON. Useful fields in `ai.query`:
`question`, `status` (`answered` / `no_answer`), `no_answer_reason`, `answer`, `citations`, `model`,
`latency_ms`, `plan.checks` (why an answer was withheld).

## Errors

The body is `{ "success": false, "error": { "code": <HTTP status>, "reason": "<ID>", "message": "…", "details": … } }`.
In the React client, `ApiError.code` holds `reason`.

| HTTP | `reason` | When |
|---|---|---|
| 401 | `UNAUTHENTICATED` | No session cookie |
| 401 | `TOKEN_EXPIRED` | Session older than 15 min (the client refreshes) |
| 403 | `PERMISSION_DENIED` | Role without `audit:read` |
| 422 | `VALIDATION_ERROR` | Bad UUID, `limit` > 200, bad date. `details` lists `{field, message}` |

## In the React app

`Page<T>` and `AuditEvent` in `frontend/src/api/types.ts` already include these fields:

```ts
export interface AuditEvent {
  id: UUID; organization_id: UUID | null; actor_user_id: UUID | null; action: string; resource_type: string | null;
  resource_id: UUID | null; request_id: string | null; details: Record<string, any>; created_at: string;
  actor_name: string | null; actor_email: string | null; actor_role: string | null;
  organization_name: string | null; resource_name: string | null;
  action_label: string; category: string; severity: "info" | "warning" | "critical";
  summary: string; facts: { label: string; value: string }[];
}
```

```ts
import { api, qs } from "../api/client";
import type { AuditEvent, Page } from "../api/types";

const events = await api.get<Page<AuditEvent>>(`/audit-events${qs({
  action,                         // "" or undefined is skipped by qs()
  organization_id: orgId,         // Master Admin only
  actor_user_id: userId,
  since,                          // ISO string
  limit: 50,
  offset: page * 50,
})}`);

const actions = await api.get<string[]>(`/audit-events/actions${qs({ organization_id: orgId })}`);
```

Suggested screen:
- **Filters:** event type (from `/actions`), organization (Master Admin only, from `/organizations`), user,
  "since" date.
- **List:** a severity dot, `summary` as the main line, then `action_label` · `organization_name` · relative time
  underneath. Show `actor_role` beside the name.
- **Details drawer:** the `facts` as a two-column list, the full time, and `request_id` (small, for support).
- **Pagination** from `total`.
