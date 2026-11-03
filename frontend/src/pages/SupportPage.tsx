import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, LifeBuoy, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { api, qs } from "../api/client";
import type { Page, SupportDashboard, SupportStaff, Ticket, TicketPriority, TicketTeam } from "../api/types";
import {
  ACTIVE, AVAILABILITY, NewTicketModal, PRIORITIES, PRIORITY_TONE, RAISERS, SlaBadge, TEAM_LABEL, TicketStatusBadge,
} from "../components/support";
import { Badge, Button, Card, CardHeader, EmptyState, ErrorState, Input, PageHeader, ProgressBar, Select, SkeletonRows, Stat, Tabs } from "../components/ui";
import { cn, formatDateTime } from "../lib/format";
import { useAuth } from "../store/hooks";

type Box = "mine" | "queue" | "assigned" | "unassigned" | "all";
const PAGE = 25;
const ROLE_LABEL: Record<string, string> = {
  department_user: "User", branch_manager: "Branch Manager", org_admin: "Organization Admin",
};

/** The boxes each person sees: what they raised, the queues they work or manage, and what they oversee. */
function boxesFor(role: string, onDuty: boolean): { id: Box; label: string }[] {
  const tabs: { id: Box; label: string }[] = [];
  if (RAISERS.has(role)) tabs.push({ id: "mine", label: "My tickets" });
  const queue = role === "branch_manager" ? "Branch queue" : role === "org_admin" ? "Head Office queue"
    : role === "master_admin" || role === "support_agent" ? "Technical queue" : "My queue";
  if (onDuty || role === "branch_manager" || role === "org_admin" || role === "master_admin") tabs.push({ id: "queue", label: queue });
  if (onDuty) tabs.push({ id: "assigned", label: "Assigned to me" });
  if (onDuty || ["branch_manager", "org_admin", "master_admin"].includes(role)) tabs.push({ id: "unassigned", label: "Unassigned" });
  if (role === "branch_manager") tabs.push({ id: "all", label: "All from my branch" });
  if (role === "org_admin") tabs.push({ id: "all", label: "All in the bank" });
  return tabs;
}

export function SupportPage() {
  const { me } = useAuth();
  const role = me?.role ?? "department_user";
  const { data: duty } = useQuery({
    queryKey: ["support", "duty"], queryFn: () => api.get<SupportStaff | null>("/support/staff/me"), staleTime: 60_000,
  });
  const onDuty = Boolean(duty?.is_active);
  const tabs = useMemo(() => boxesFor(role, onDuty), [role, onDuty]);
  const [box, setBox] = useState<Box>(tabs[0]?.id ?? "mine");
  useEffect(() => { if (!tabs.some((t) => t.id === box)) setBox(tabs[0]?.id ?? "mine"); }, [tabs, box]);
  const [status, setStatus] = useState("active");
  const [team, setTeam] = useState<TicketTeam | "">("");
  const [priority, setPriority] = useState<TicketPriority | "">("");
  const [overdue, setOverdue] = useState(false);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  useEffect(() => setOffset(0), [box, status, team, priority, overdue, search]);

  const params = { box, status, team, priority, overdue: overdue || undefined, search: search.trim() || undefined, limit: PAGE, offset };
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["support", "tickets", params],
    queryFn: () => api.get<Page<Ticket>>(`/support/tickets${qs(params)}`),
    refetchInterval: 30_000,
  });
  const { data: dash } = useQuery({
    queryKey: ["support", "dashboard", box],
    queryFn: () => api.get<SupportDashboard>(`/support/tickets/dashboard${qs({ box })}`),
    refetchInterval: 30_000,
  });
  const raises = RAISERS.has(role);
  const staffSide = box !== "mine";
  const done = dash ? dash.by_status.resolved + dash.by_status.closed : undefined;

  return (
    <>
      <PageHeader
        title="Support Center"
        subtitle={raises
          ? "Raise a ticket and follow it. We recommend the right team, you confirm, and you are notified of every update."
          : "Tickets routed to your queue, with their SLA and workload."}
        actions={<div className="flex flex-wrap gap-2">
          {duty && <AvailabilitySwitch duty={duty} />}
          {raises && <Button onClick={() => setCreating(true)}><Plus className="size-4" />New ticket</Button>}
        </div>}
      />

      <div className="mb-5 grid gap-3 sm:grid-cols-2 sm:gap-4 xl:grid-cols-4">
        <Stat label="Active" value={dash?.active ?? "—"} hint={staffSide ? "Open, in progress or waiting" : "Not resolved yet"} />
        <Stat label="Overdue" value={dash?.overdue ?? "—"} tone={dash?.overdue ? "bad" : undefined}
          hint={dash?.response_overdue ? `${dash.response_overdue} awaiting a first reply past target` : "Past their resolution time"} />
        {staffSide
          ? <Stat label="Unassigned" value={dash?.unassigned ?? "—"} tone={dash?.unassigned ? "warn" : undefined} hint="Waiting for someone to take them" />
          : <Stat label="Awaiting confirmation" value={dash?.by_status.awaiting_confirmation ?? "—"} tone={dash?.by_status.awaiting_confirmation ? "warn" : undefined} hint="Drafts you have not sent" />}
        <Stat label="Resolved" value={done ?? "—"} hint="Resolved or closed" />
      </div>

      {tabs.length > 1 && <Tabs<Box> value={box} onChange={setBox} tabs={tabs} />}

      <div className="mt-4 grid gap-5 xl:grid-cols-[minmax(0,1fr)_18rem]">
        <Card>
          <div className="flex flex-wrap gap-2 border-b border-line p-3 sm:p-4">
            <Input className="min-w-0 flex-1 sm:max-w-xs" placeholder="Search subject, person, SUP-number…" value={search}
              onChange={(e) => setSearch(e.target.value)} aria-label="Search tickets" />
            <Select className="w-44" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
              <option value="active">Active</option>
              <option value="">All statuses</option>
              {box === "mine" && <option value="awaiting_confirmation">Awaiting confirmation</option>}
              <option value="open">Open</option>
              <option value="in_progress">In progress</option>
              <option value="waiting_for_user">Waiting for requester</option>
              <option value="waiting_for_team">Waiting for another team</option>
              <option value="done">Resolved, closed, cancelled</option>
            </Select>
            <Select className="w-44" value={team} onChange={(e) => setTeam(e.target.value as TicketTeam | "")} aria-label="Team">
              <option value="">All teams</option>
              <option value="technical">{TEAM_LABEL.technical}</option>
              <option value="branch">{TEAM_LABEL.branch}</option>
            </Select>
            <Select className="w-36" value={priority} onChange={(e) => setPriority(e.target.value as TicketPriority | "")} aria-label="Priority">
              <option value="">Any priority</option>
              {PRIORITIES.map((p) => <option key={p} value={p}>{p[0].toUpperCase() + p.slice(1)}</option>)}
            </Select>
            <label className="flex items-center gap-2 px-1 text-sm text-ink-soft">
              <input type="checkbox" checked={overdue} onChange={(e) => setOverdue(e.target.checked)} />Overdue only
            </label>
          </div>
          {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
            : isLoading ? <SkeletonRows />
            : !data?.items.length ? (
              <EmptyState icon={<LifeBuoy className="size-6" />} title="No tickets here"
                description={box === "mine" ? "Tickets you raise appear here with every update." : "Nothing matches in this queue."}
                action={raises && box === "mine" && <Button onClick={() => setCreating(true)}><Plus className="size-4" />New ticket</Button>} />
            ) : (
              <>
                <ul className="divide-y divide-line">
                  {data.items.map((t) => <TicketRow key={t.id} ticket={t} staffSide={staffSide} platform={role === "master_admin" || role === "support_agent"} />)}
                </ul>
                {data.total > PAGE && (
                  <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-sm text-muted">
                    <span>{offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}</span>
                    <div className="flex gap-1">
                      <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))} aria-label="Previous page"><ChevronLeft className="size-4" /></Button>
                      <Button size="sm" variant="ghost" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)} aria-label="Next page"><ChevronRight className="size-4" /></Button>
                    </div>
                  </div>
                )}
              </>
            )}
        </Card>

        {staffSide && dash && <aside className="space-y-4"><Breakdown dash={dash} /></aside>}
      </div>
      <NewTicketModal open={creating} onClose={() => setCreating(false)} />
    </>
  );
}

function TicketRow({ ticket: t, staffSide, platform }: { ticket: Ticket; staffSide: boolean; platform: boolean }) {
  const from = [t.created_by_name, t.created_by_role && ROLE_LABEL[t.created_by_role]].filter(Boolean).join(", ");
  const active = ACTIVE.includes(t.status);
  return (
    <li>
      <Link to={`/support/${t.id}`} className="flex flex-wrap items-center gap-3 px-4 py-3 hover:bg-subtle/50 sm:px-5">
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium"><span className="mr-2 font-mono text-xs text-muted">{t.reference}</span>{t.subject}</p>
          <p className="mt-0.5 text-xs text-muted">
            {t.queue_name}{t.category ? ` · ${t.category}` : ""}
            {platform && t.organization_name ? ` · ${t.organization_name}` : ""}
            {staffSide && from ? ` · from ${from}` : ""}
            {` · ${t.assigned_to_name ? `with ${t.assigned_to_name}` : t.status === "awaiting_confirmation" ? "not sent yet" : "unassigned"}`}
            {` · updated ${formatDateTime(t.updated_at)}`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <SlaBadge dueAt={t.due_at} overdue={t.overdue} active={active} />
          {(t.priority === "high" || t.priority === "urgent") && <Badge tone={PRIORITY_TONE[t.priority]} className="capitalize">{t.priority}</Badge>}
          <TicketStatusBadge status={t.status} staffSide={staffSide} />
        </div>
      </Link>
    </li>
  );
}

function Breakdown({ dash }: { dash: SupportDashboard }) {
  return (
    <>
      <Card>
        <CardHeader title="Active by priority" />
        <ul className="space-y-1.5 p-4 text-sm">
          {PRIORITIES.slice().reverse().map((p) => (
            <li key={p} className="flex items-center justify-between">
              <Badge tone={PRIORITY_TONE[p]} className="capitalize">{p}</Badge><span className="tabular-nums">{dash.by_priority[p] ?? 0}</span>
            </li>
          ))}
          {(["technical", "branch"] as TicketTeam[]).map((team) => (
            <li key={team} className="flex items-center justify-between border-t border-line pt-1.5 text-ink-soft first-of-type:border-0">
              <span>{TEAM_LABEL[team]} (all)</span><span className="tabular-nums">{dash.by_team[team] ?? 0}</span>
            </li>
          ))}
        </ul>
      </Card>
      {dash.by_assignee.length > 0 && (
        <Card>
          <CardHeader title="Workload" subtitle="Active tickets against capacity" />
          <ul className="space-y-3 p-4 text-sm">
            {dash.by_assignee.map((a) => (
              <li key={a.user_id}>
                <div className="flex items-center justify-between gap-2"><span className="truncate">{a.name}</span>
                  <span className="tabular-nums text-xs text-muted">{a.open_tickets}{a.capacity ? ` / ${a.capacity}` : ""}</span></div>
                {a.load !== null && <div className="mt-1"><ProgressBar value={Math.min(1, a.load) * 100} tone={a.load >= 1 ? "bad" : "brand"} /></div>}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}

/** A staff member sets their own availability: away, busy or offline get no new tickets. */
function AvailabilitySwitch({ duty }: { duty: SupportStaff }) {
  const [value, setValue] = useState(duty.availability);
  const [busy, setBusy] = useState(false);
  useEffect(() => setValue(duty.availability), [duty.availability]);
  if (!duty.is_active) return null;
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className={cn("size-2 rounded-full", value === "available" ? "bg-ok-600" : value === "busy" ? "bg-warn-600" : "bg-line-strong")} aria-hidden />
      <Select className="w-36" value={value} disabled={busy} aria-label="My availability" onChange={async (e) => {
        const next = e.target.value as SupportStaff["availability"];
        setBusy(true);
        try {
          await api.patch("/support/staff/me", { availability: next });
          setValue(next);
        } finally {
          setBusy(false);
        }
      }}>
        {Object.entries(AVAILABILITY).map(([key, [label]]) => <option key={key} value={key}>{label}</option>)}
      </Select>
    </label>
  );
}
