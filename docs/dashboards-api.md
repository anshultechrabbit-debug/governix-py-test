# Dashboard API for the admin panel

The admin panel (`Governix-Banking-Admin-Panal-Without-Api`) has three dashboards with sample data. Each one now has
an endpoint. Responses use the usual envelope: the figures are in `data`.

| Dashboard page | Endpoint | Who may call it |
| --- | --- | --- |
| `pages/masteradmin/Dashboard.jsx` | `GET /api/dashboard/platform` | Master Admin |
| `pages/organizationadmin/Dashboard.jsx` | `GET /api/dashboard/organization` | Org Admin |
| `pages/branchmanager/Dashboard.jsx` | `GET /api/dashboard/branch` | Branch Manager |

Any other role gets `403` with `error.reason = "WRONG_DASHBOARD"`.

Query parameters:
- `period`: `7d` (7 daily points, the default), `30d` (4 weekly points) or `90d` (this month and the two before).
- `/platform` also takes `organizations` (rows in the organizations table, default 10) and `audit` (audit events, default 10).
- `/organization` and `/branch` also take `rows` (rows per table or list, default 5) and `activity` (feed entries, default 8).

Two things the sample data shows are not recorded by Governix, so they are worked out like this:
- **Acknowledged / read.** An assignment counts as read once the User has opened one of the policy's documents
  after it was assigned to them. `pending` = assigned but not opened yet.
- **Gateway traffic (requests, bandwidth, latency).** nginx traffic is not recorded. The card gets each
  organization's activity in the app instead: sign-ins, AI questions, documents opened and uploads in the period.

## Master Admin: `GET /api/dashboard/platform`

| UI section | Field | Notes |
| --- | --- | --- |
| KPI "Total Organization" | `organizations.total`, `.active`, `.provisioning`, `.suspended`, `.new_this_month` | "34 Active • 4 Provisioning", "+4 this month" |
| KPI "Total User" | `users.total`, `.active`, `.new_last_30_days`, `.growth_percent` | `growth_percent` is `null` when there were no accounts before |
| KPI "Reverse Proxies" | `proxies.running`, `.with_port`, `.outdated`, `.disabled`, `.not_deployed`, `.first_port`, `.last_port`, `.configured` | "Ports 7001–7015 Routed" = `first_port`–`last_port`; uptime is not measured |
| KPI "Organization Admin" | `org_admins.total`, `.active`, `.organizations` | `organizations` = banks with an active Org Admin |
| KPI "Support Ticket" | `support.counts.total`, `.open`, `.in_progress`, `.resolved`, `.overdue`, `.sla_met_percent` | Technical Support (platform) tickets |
| Recent Tenant Organizations | `recent_organizations[]`: `name`, `slug` (code), `contact_email`, `port`, `status`, `admins`, `branches`, `users`, `storage_bytes`, `created_at` (joined), `nginx_state` | `status`: `active`, `provisioning`, `suspended`. There is no tier, storage limit or uptime SLA |
| Nginx Port Routing | `ports[]`: `port`, `organization_name`, `state` | `state`: `deployed`, `outdated`, `disabled`, `not_deployed`, `unavailable` |
| User Roles chart and circle | `roles[]`: `role`, `count`, `percent` | Roles: `master_admin`, `org_admin`, `branch_manager`, `department_user` (Branch Staff Users), `support_agent` |
| Tenant Gateway Traffic | `activity_by_organization[]`: `name`, `port`, `sign_ins`, `questions`, `documents_opened`, `uploads`, `total` | Busiest first. The "requests / bandwidth" toggle becomes a choice of these counts |
| Support Operations, donut | `support.by_status[]`: `status` (`resolved`, `in_progress`, `open`, `cancelled`), `count`, `percent` | |
| Support Operations, categories | `support.categories[]`: `name`, `total`, `open` | The six busiest |
| Audit Logs | `audit[]`: `actor_name`, `actor_role`, `organization_name`, `summary`, `category`, `severity`, `created_at` | The role filter can use `actor_role` (`master_admin`, `org_admin`, ...; `null` for the system) |

## Org Admin: `GET /api/dashboard/organization`

| UI component | Field | Notes |
| --- | --- | --- |
| `OrgAdminStats` | `users.total`, `users.new_last_90_days` ("+2 this quarter"), `branches.total`, `branches.with_manager` (Assigned Managers), `documents.total` (Global Documents) | Also `users.by_role`, `documents.ready`, `.processing`, `.needs_attention` |
| `OrgAdminBranchUserChart` | `branch_users[]`: `name`, `code`, `users`, `managers`, `status` | `status`: `active`, `needs_attention` (no active manager), `inactive` |
| `OrgAdminTopPolicies` | `top_policies[]`: `name`, `category`, `branches`, `assigned`, `read`, `pending`, `read_percent` | `isComplete` = `pending == 0`; `read_percent` is `null` when nobody is assigned |
| `OrgAdminDocumentActivityChart` | `document_activity[]`: `label` ("Mon"), `start`, `uploads`, `assignments` | Peak = the highest `uploads` |
| `OrgAdminBranchOverview` | `latest_branches[]`: `name`, `code`, `location`, `manager_name`, `manager_email`, `is_active` | `manager_name: null` = "Unassigned" |
| `OrgAdminRecentDocuments` | `recent_documents[]`: `title`, `filename`, `extension`, `category`, `version_label`, `size_bytes`, `status`, `updated_at` | |
| `OrgAdminActivityFeed` | `activity[]`: audit events of the organization (`summary`, `action_label`, `category`, `created_at`, ...) | |
| `OrgAdminQuickActions` (system status) | `portal.port`, `portal.state` | The bank's port and whether it is served |

## Branch Manager: `GET /api/dashboard/branch`

| UI component | Field | Notes |
| --- | --- | --- |
| Header | `branch_name` | Instead of `localStorage` |
| `BranchManagerStats` | `counts.documents`, `counts.staff`, `counts.assignments`, `counts.open_tickets` (Branch Complaints) | |
| `BranchAnalyticsHub`, departments bar chart | `departments[]`: `name`, `count`, `percent` | Users without a department are "No department" |
| `BranchAnalyticsHub`, Document Activity | `document_activity[]`: `label`, `start`, `end`, `uploads`, `assignments` | Use `period` for the 7d / 30d / 90d switch |
| `BranchAnalyticsHub`, Assignment Status donut | `assignment_status.read` (acknowledged), `.pending`, `.unassigned_policies` (available) | |
| `BranchRecentDocuments` | `recent_documents[]`, the same fields as the Org Admin's | |
| `BranchTopPolicies` | `top_policies[]`: `name`, `category`, `assigned` ("8 staff"), `read_percent`, `pending` | Counts the branch's Users only |
| `BranchActivityFeed` | `activity[]`: `kind` (`ticket`, `document`, `assignment`, `staff`), `title`, `detail`, `status`, `at`, `resource_type`, `resource_id` | There is no branch chat in Governix, so there are no chat entries |
