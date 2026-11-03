import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Headset, Pencil, Plus, Shuffle, Tags, Trash2, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, qs } from "../api/client";
import type {
  CategoryRoute, Organization, Page, StaffAvailability, StaffCandidate, SupportCategoryAdmin, SupportStaff,
  SupportStaffCreated, TicketLevel,
} from "../api/types";
import { useBranches } from "../components/domain";
import { AVAILABILITY } from "../components/support";
import {
  Badge, Button, Card, EmptyState, ErrorState, Field, Input, Menu, Modal, PageHeader, ProgressBar, Select, SkeletonRows, Tabs,
} from "../components/ui";
import { copyText } from "../lib/clipboard";
import { formatDateTime } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

const LEVEL_LABEL: Record<TicketLevel, string> = {
  branch: "Branch Support", organization: "Head Office Support", platform: "Technical Support",
};
const ROUTE_LABEL: Record<CategoryRoute, string> = {
  technical: "Technical Support", branch: "Branch Support", policy: "AI Assistant / policy question", auto: "Let the AI decide",
};
type View = "staff" | "categories";

/**
 * Support staff, each level by its own manager (the backend enforces the same boundaries):
 * Master Admin, Governix's Technical Support; Org Admin, Head Office and the bank's branches;
 * Branch Manager, their own branch.
 */
export function SupportStaffPage() {
  const { me } = useAuth();
  const role = me?.role;
  const [view, setView] = useState<View>("staff");
  const levels: TicketLevel[] = role === "master_admin" ? ["platform"] : role === "org_admin" ? ["organization", "branch"] : ["branch"];
  const [level, setLevel] = useState<TicketLevel | "">(levels.length === 1 ? levels[0] : "");
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<SupportStaff | null>(null);
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["support", "staff", level, search],
    queryFn: () => api.get<SupportStaff[]>(`/support/staff${qs({ level, search: search.trim() || undefined })}`),
  });

  async function act(action: () => Promise<unknown>, done: string) {
    try {
      await action();
      void queryClient.invalidateQueries({ queryKey: ["support"] });
      toast("ok", done);
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Something went wrong.");
    }
  }

  return (
    <>
      <PageHeader
        title="Support Staff"
        subtitle={role === "master_admin" ? "Governix's Technical Support team: who is on duty, their skills, capacity and the banks they serve."
          : role === "org_admin" ? "Head Office and branch support staff of your bank. They keep their usual role and also handle tickets."
          : "Your branch's support staff. They keep their usual role and also handle tickets."}
        actions={view === "staff" && <Button onClick={() => setAdding(true)}><UserPlus className="size-4" />Add support staff</Button>}
      />
      {role === "org_admin" && (
        <Tabs<View> value={view} onChange={setView} tabs={[{ id: "staff", label: "Staff" }, { id: "categories", label: "Ticket categories" }]} />
      )}

      {view === "categories" ? <CategoriesPanel /> : (
        <Card className="mt-4">
          <div className="flex flex-wrap gap-2 border-b border-line p-3 sm:p-4">
            <Input className="min-w-0 flex-1 sm:max-w-xs" placeholder="Search name or email…" value={search}
              onChange={(e) => setSearch(e.target.value)} aria-label="Search staff" />
            {levels.length > 1 && (
              <Select className="w-52" value={level} onChange={(e) => setLevel(e.target.value as TicketLevel | "")} aria-label="Queue">
                <option value="">All queues</option>
                {levels.map((l) => <option key={l} value={l}>{LEVEL_LABEL[l]}</option>)}
              </Select>
            )}
          </div>
          {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
            : isLoading ? <SkeletonRows />
            : !data?.length ? (
              <EmptyState icon={<Headset className="size-6" />} title="No support staff yet"
                description="Until someone is on duty, tickets wait unassigned and the queue's manager is told."
                action={<Button onClick={() => setAdding(true)}><UserPlus className="size-4" />Add support staff</Button>} />
            ) : (
              <ul className="divide-y divide-line">
                {data.map((s) => {
                  const [label, tone] = AVAILABILITY[s.availability];
                  return (
                    <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3 sm:px-5">
                      <div className="min-w-0 flex-1">
                        <p className="flex flex-wrap items-center gap-2 font-medium">{s.full_name}
                          {s.staff_role === "lead" && <Badge tone="brand">Lead</Badge>}
                          {!s.is_active && <Badge tone="neutral">Deactivated</Badge>}
                          {!s.account_active && <Badge tone="bad">Account inactive</Badge>}
                        </p>
                        <p className="text-xs text-muted">{s.email} · {LEVEL_LABEL[s.level]}{s.branch_name ? ` · ${s.branch_name}` : ""}
                          {s.level === "platform" ? ` · serves ${s.serves.length ? s.serves.map((o) => o.name).join(", ") : "every bank"}` : ""}
                          {s.last_assigned_at ? ` · last ticket ${formatDateTime(s.last_assigned_at)}` : ""}</p>
                        {s.skills.length > 0 && <div className="mt-1 flex flex-wrap gap-1">{s.skills.map((k) => <Badge key={k}>{k}</Badge>)}</div>}
                      </div>
                      <div className="w-40">
                        <div className="flex justify-between text-xs text-muted"><span>Open tickets</span><span className="tabular-nums">{s.open_tickets}/{s.capacity}</span></div>
                        <div className="mt-1"><ProgressBar value={Math.min(1, s.load) * 100} tone={s.load >= 1 ? "bad" : "brand"} /></div>
                      </div>
                      <Badge tone={tone}>{label}</Badge>
                      <Menu items={[
                        { label: "Edit", icon: <Pencil className="size-4" />, onSelect: () => setEditing(s) },
                        s.open_tickets > 0 && { label: `Reassign ${s.open_tickets} open ticket${s.open_tickets === 1 ? "" : "s"}`, icon: <Shuffle className="size-4" />,
                          onSelect: () => act(async () => {
                            const { moved } = await api.post<{ moved: number }>(`/support/staff/${s.id}/reassign`, {});
                            toast("ok", `${moved} ticket${moved === 1 ? "" : "s"} reassigned by workload.`);
                          }, "Done.") },
                        { label: s.is_active ? "Deactivate" : "Activate", onSelect: () => act(() => api.patch(`/support/staff/${s.id}`, { is_active: !s.is_active }),
                          s.is_active ? `${s.full_name} gets no new tickets.` : `${s.full_name} is back on duty.`) },
                        { label: "Remove from support", danger: true, icon: <Trash2 className="size-4" />, disabled: s.open_tickets > 0,
                          onSelect: () => act(() => api.delete(`/support/staff/${s.id}`), `${s.full_name} is no longer on support duty.`) },
                      ]} />
                    </li>
                  );
                })}
              </ul>
            )}
        </Card>
      )}
      <AddStaffModal open={adding} onClose={() => setAdding(false)} levels={levels} />
      <EditStaffModal staff={editing} onClose={() => setEditing(null)} />
    </>
  );
}

function SkillsInput({ value, onChange }: { value: string[]; onChange: (skills: string[]) => void }) {
  const [text, setText] = useState("");
  const add = () => {
    const tag = text.trim();
    if (tag && !value.some((v) => v.toLowerCase() === tag.toLowerCase())) onChange([...value, tag]);
    setText("");
  };
  return (
    <div>
      <div className="flex gap-2">
        <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. login, KYC, core banking"
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); } }} />
        <Button type="button" variant="secondary" onClick={add} disabled={!text.trim()}><Plus className="size-4" /></Button>
      </div>
      {value.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {value.map((k) => (
            <button key={k} type="button" onClick={() => onChange(value.filter((v) => v !== k))} className="rounded-full bg-subtle px-2 py-0.5 text-xs hover:bg-bad-50" title="Remove">
              {k} ×
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function DutyFields({ form, set, platform }: { form: Duty; set: (patch: Partial<Duty>) => void; platform: boolean }) {
  const { data: organizations } = useQuery({
    queryKey: ["organizations", "all"], queryFn: () => api.get<Page<Organization>>(`/organizations${qs({ limit: 100 })}`),
    enabled: platform,
  });
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Role"><Select value={form.staff_role} onChange={(e) => set({ staff_role: e.target.value as "agent" | "lead" })}>
          <option value="agent">Agent</option><option value="lead">Lead (also assigns)</option></Select></Field>
        <Field label="Capacity" hint="Open tickets at which they are full.">
          <Input type="number" min={1} max={500} value={form.capacity} onChange={(e) => set({ capacity: Math.max(1, Number(e.target.value) || 1) })} /></Field>
        <Field label="Availability"><Select value={form.availability} onChange={(e) => set({ availability: e.target.value as StaffAvailability })}>
          {Object.entries(AVAILABILITY).map(([k, [label]]) => <option key={k} value={k}>{label}</option>)}</Select></Field>
      </div>
      <Field label="Skills" hint="Tickets that need a skill go to those who have it first. The AI picks only from these tags.">
        <SkillsInput value={form.skills} onChange={(skills) => set({ skills })} />
      </Field>
      {platform && (
        <Field label="Banks served" hint="None ticked: every bank.">
          <div className="max-h-40 space-y-1 overflow-auto rounded-md border border-line p-2">
            {organizations?.items.map((o) => (
              <label key={o.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={form.serves.includes(o.id)}
                  onChange={(e) => set({ serves: e.target.checked ? [...form.serves, o.id] : form.serves.filter((x) => x !== o.id) })} />{o.name}
              </label>
            ))}
            {!organizations && <p className="text-xs text-muted">Loading…</p>}
          </div>
        </Field>
      )}
    </>
  );
}

interface Duty { staff_role: "agent" | "lead"; skills: string[]; capacity: number; availability: StaffAvailability; serves: string[] }
const EMPTY_DUTY: Duty = { staff_role: "agent", skills: [], capacity: 10, availability: "available", serves: [] };

function AddStaffModal({ open, onClose, levels }: { open: boolean; onClose: () => void; levels: TicketLevel[] }) {
  const { me } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data: branches } = useBranches();
  const platform = levels[0] === "platform";
  const [level, setLevel] = useState<TicketLevel>(levels[0]);
  const [branchId, setBranchId] = useState("");
  const [mode, setMode] = useState<"existing" | "new">(platform ? "new" : "existing");
  const [search, setSearch] = useState("");
  const [userId, setUserId] = useState("");
  const [account, setAccount] = useState({ full_name: "", email: "", phone: "" });
  const [duty, setDuty] = useState<Duty>(EMPTY_DUTY);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<SupportStaffCreated | null>(null);
  useEffect(() => {
    if (!open) return;
    setLevel(levels[0]); setBranchId(me?.role === "branch_manager" ? me.branch_id ?? "" : ""); setMode(platform ? "new" : "existing");
    setSearch(""); setUserId(""); setAccount({ full_name: "", email: "", phone: "" }); setDuty(EMPTY_DUTY); setCreated(null);
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  const needsBranch = level === "branch" && me?.role === "org_admin";
  const { data: candidates } = useQuery({
    queryKey: ["support", "candidates", level, branchId, search],
    queryFn: () => api.get<StaffCandidate[]>(`/support/staff/candidates${qs({ level, branch_id: branchId || undefined, search: search.trim() || undefined })}`),
    enabled: open && mode === "existing" && (!needsBranch || Boolean(branchId)),
  });
  const invalid = mode === "new" ? account.full_name.trim().length < 2 || !account.email.includes("@") : !userId;

  async function submit() {
    setBusy(true);
    try {
      const result = await api.post<SupportStaffCreated>("/support/staff", {
        level, branch_id: level === "branch" ? branchId || null : null,
        user_id: mode === "existing" ? userId : null,
        new_account: mode === "new" ? { full_name: account.full_name.trim(), email: account.email.trim(), phone: account.phone.trim() || null } : null,
        ...duty, serves: platform ? duty.serves : null,
      });
      void queryClient.invalidateQueries({ queryKey: ["support"] });
      if (result.temporary_password) setCreated(result);
      else {
        toast("ok", `${result.full_name} is on support duty.`);
        onClose();
      }
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not add them.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} wide icon={<UserPlus className="size-5" />} title="Add support staff"
      description="They keep their account and role; this puts them on duty in a queue."
      footer={created ? <Button onClick={onClose}>Done</Button> : <>
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button disabled={invalid || (level === "branch" && !branchId)} loading={busy} onClick={submit}>Add</Button>
      </>}>
      {created ? (
        <div className="space-y-3 text-sm">
          <p><span className="font-medium">{created.full_name}</span> can now sign in as a Support Agent with this temporary password. It is shown once.</p>
          <div className="flex items-center gap-2 rounded-md bg-subtle px-3 py-2 font-mono">
            <span className="flex-1 break-all">{created.temporary_password}</span>
            <Button size="sm" variant="ghost" onClick={() => { void copyText(created.temporary_password ?? ""); toast("ok", "Copied."); }}><Copy className="size-4" /></Button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          {levels.length > 1 && (
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Queue"><Select value={level} onChange={(e) => { setLevel(e.target.value as TicketLevel); setUserId(""); }}>
                {levels.map((l) => <option key={l} value={l}>{LEVEL_LABEL[l]}</option>)}</Select></Field>
              {level === "branch" && (
                <Field label="Branch *"><Select value={branchId} onChange={(e) => { setBranchId(e.target.value); setUserId(""); }}>
                  <option value="">Choose…</option>
                  {branches?.items.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}</Select></Field>
              )}
            </div>
          )}
          {platform && (
            <Tabs<"existing" | "new"> value={mode} onChange={setMode}
              tabs={[{ id: "new", label: "New support agent" }, { id: "existing", label: "Existing account" }]} />
          )}
          {mode === "new" ? (
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Full name *"><Input value={account.full_name} onChange={(e) => setAccount({ ...account, full_name: e.target.value })} /></Field>
              <Field label="Email *"><Input type="email" value={account.email} onChange={(e) => setAccount({ ...account, email: e.target.value })} /></Field>
              <Field label="Phone"><Input value={account.phone} onChange={(e) => setAccount({ ...account, phone: e.target.value })} /></Field>
            </div>
          ) : (
            <Field label="Person *" hint={level === "branch" ? "People of that branch who are not on duty yet." : level === "organization" ? "Anyone in your bank who is not on duty yet." : "Support agents and Master Admins."}>
              <Input placeholder="Search name or email…" value={search} onChange={(e) => setSearch(e.target.value)} />
              <Select className="mt-2" value={userId} onChange={(e) => setUserId(e.target.value)} disabled={!candidates}>
                <option value="">{needsBranch && !branchId ? "Choose a branch first" : candidates?.length === 0 ? "No one found" : "Choose…"}</option>
                {candidates?.map((c) => <option key={c.user_id} value={c.user_id}>{c.full_name} · {c.email}{c.branch_name ? ` · ${c.branch_name}` : ""}</option>)}
              </Select>
            </Field>
          )}
          <DutyFields form={duty} set={(patch) => setDuty({ ...duty, ...patch })} platform={platform} />
        </div>
      )}
    </Modal>
  );
}

function EditStaffModal({ staff, onClose }: { staff: SupportStaff | null; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [duty, setDuty] = useState<Duty>(EMPTY_DUTY);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (staff) setDuty({ staff_role: staff.staff_role, skills: staff.skills, capacity: staff.capacity, availability: staff.availability,
      serves: staff.serves.map((o) => o.id) });
  }, [staff]);
  if (!staff) return null;
  const platform = staff.level === "platform";
  async function save() {
    if (!staff) return;
    setBusy(true);
    try {
      await api.patch(`/support/staff/${staff.id}`, { ...duty, serves: platform ? duty.serves : undefined });
      void queryClient.invalidateQueries({ queryKey: ["support"] });
      toast("ok", "Saved. Their open tickets stay with them.");
      onClose();
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open onClose={onClose} wide icon={<Pencil className="size-5" />} title={staff.full_name}
      description={`${LEVEL_LABEL[staff.level]}${staff.branch_name ? ` · ${staff.branch_name}` : ""}`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button loading={busy} onClick={save}>Save</Button></>}>
      <div className="space-y-4"><DutyFields form={duty} set={(patch) => setDuty({ ...duty, ...patch })} platform={platform} /></div>
    </Modal>
  );
}

/** Organization Admin: the ticket categories and where each points before the AI reads a ticket. */
function CategoriesPanel() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [route, setRoute] = useState<CategoryRoute>("auto");
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["support", "categories", "manage"], queryFn: () => api.get<SupportCategoryAdmin[]>("/support/categories/manage"),
  });
  async function act(action: () => Promise<unknown>, done: string) {
    try {
      await action();
      void queryClient.invalidateQueries({ queryKey: ["support", "categories"] });
      toast("ok", done);
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Something went wrong.");
    }
  }
  return (
    <Card className="mt-4">
      <div className="flex flex-wrap items-end gap-2 border-b border-line p-3 sm:p-4">
        <Field label="New category"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. ATM & Cash Machines" /></Field>
        <Field label="Points to"><Select value={route} onChange={(e) => setRoute(e.target.value as CategoryRoute)}>
          {Object.entries(ROUTE_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}</Select></Field>
        <Button disabled={name.trim().length < 2} onClick={() => act(async () => {
          await api.post("/support/categories", { name: name.trim(), route });
          setName("");
        }, "Category added.")}><Tags className="size-4" />Add</Button>
      </div>
      {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div> : isLoading ? <SkeletonRows /> : (
        <ul className="divide-y divide-line">
          {data?.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center gap-3 px-4 py-3 sm:px-5">
              <div className="min-w-0 flex-1">
                <p className="font-medium">{c.name} {!c.is_active && <Badge tone="neutral">Off</Badge>}</p>
                {c.description && <p className="text-xs text-muted">{c.description}</p>}
              </div>
              <Select className="w-60" value={c.route} aria-label={`Where ${c.name} points`}
                onChange={(e) => act(() => api.patch(`/support/categories/${c.id}`, { route: e.target.value }), "Saved.")}>
                {Object.entries(ROUTE_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </Select>
              <Button size="sm" variant="ghost" onClick={() => act(() => api.patch(`/support/categories/${c.id}`, { is_active: !c.is_active }),
                c.is_active ? `${c.name} is no longer offered.` : `${c.name} is offered again.`)}>{c.is_active ? "Turn off" : "Turn on"}</Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
