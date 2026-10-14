import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Building2, Check, ChevronLeft, ChevronRight, Copy, KeyRound, Mail, Pencil, Plus, Shield, Trash2, UserPlus,
  Users as UsersIcon,
} from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError, qs } from "../api/client";
import type { Organization, Page, User, UserCreated, UserStats } from "../api/types";
import { BranchFields, branchBody, branchReady, EMPTY_BRANCH, type BranchDraft } from "../components/BranchFields";
import { useBranches } from "../components/domain";
import {
  Badge, Button, Card, EmptyState, ErrorState, Field, Input, Modal, PageHeader, Select, SkeletonRows, Stat, StatusBadge,
  Table,
} from "../components/ui";
import { copyText } from "../lib/clipboard";
import { cn, formatDate } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

/**
 * The Users screen of the two admins, who each add one role (ASSIGNABLE_ROLES): the Master Admin
 * adds Organization Admins to organizations, an Organization Admin adds Branch Managers to
 * branches (an existing one, or a new one created with them). Branch Managers use UsersPage.
 */

const ROLE_LABEL: Record<User["role"], string> = {
  master_admin: "Master Admin",
  org_admin: "Organization Admin",
  branch_manager: "Branch Manager",
  department_user: "User",
};
const PAGE_SIZES = [5, 10, 25, 50];
const PHONE_PATTERN = "[0-9+()\\-. ]*";

type StatusFilter = "" | "active" | "inactive";
interface Credentials { title: string; email: string; password: string }

/** What differs between the two admins' pages. */
interface Scope {
  adds: "org_admin" | "branch_manager";
  title: string;
  subtitle: string;
  place: "Organization" | "Branch";
  placeOf: (user: User) => string;
  emptyHint: string;
}
const SCOPES: Record<"master_admin" | "org_admin", Scope> = {
  master_admin: {
    adds: "org_admin",
    title: "Users",
    subtitle: "Platform administrators and the admins of every organization.",
    place: "Organization",
    placeOf: (user) => user.organization_name ?? (user.role === "master_admin" ? "Platform" : "—"),
    emptyHint: "Add an organization admin to let a bank manage its own branches and users.",
  },
  org_admin: {
    adds: "branch_manager",
    title: "User management",
    subtitle: "The Branch Managers of your organization and the branches they run. Each manager adds their branch's users.",
    place: "Branch",
    placeOf: (user) => user.branch_name ?? "—",
    emptyHint: "Add a Branch Manager for each branch. They add and manage their branch's users.",
  },
};

export function AdminUsersPage() {
  const { me } = useAuth();
  const scope = SCOPES[me?.role === "org_admin" ? "org_admin" : "master_admin"];
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<StatusFilter>("");
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(0);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);
  const [deleting, setDeleting] = useState<User | null>(null);
  const [resetting, setResetting] = useState<User | null>(null);
  const [credentials, setCredentials] = useState<Credentials | null>(null);

  const { data: stats } = useQuery({
    queryKey: ["users", "stats"],
    queryFn: () => api.get<UserStats>("/users/stats"),
  });
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["users", { search, status, page, pageSize }],
    queryFn: () => api.get<Page<User>>(`/users${qs({ search, status, limit: pageSize, offset: page * pageSize })}`),
    placeholderData: keepPreviousData,
  });

  // Deleting the last user of the last page leaves it empty: show the page before it.
  const pages = Math.max(1, Math.ceil((data?.total ?? 0) / pageSize));
  useEffect(() => {
    if (data && page >= pages) setPage(pages - 1);
  }, [data, page, pages]);

  const filtered = Boolean(search || status);
  const first = data?.total ? page * pageSize + 1 : 0;
  const last = Math.min((page + 1) * pageSize, data?.total ?? 0);

  return (
    <>
      <PageHeader
        title={scope.title}
        subtitle={scope.subtitle}
        actions={<Button onClick={() => setAdding(true)}><Plus className="size-4" />Add user</Button>}
      />

      <div className="mb-5 grid gap-3 sm:mb-6 sm:grid-cols-3 sm:gap-4">
        <Stat label="Total users" value={stats?.total ?? "—"} hint="All accounts" />
        <Stat label="Active users" value={stats?.active ?? "—"} hint="Can sign in" />
        <Stat label="Inactive users" value={stats?.inactive ?? "—"} tone={stats?.inactive ? "bad" : undefined} hint="Suspended" />
      </div>

      <Card>
        <div className="flex flex-wrap items-center gap-3 border-b border-line p-4">
          <Input
            className="min-w-0 flex-1 sm:max-w-sm"
            placeholder={`Search by name, email or ${scope.place.toLowerCase()}…`}
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0); }}
          />
          <Select
            className="max-w-[12rem]"
            aria-label="Filter by status"
            value={status}
            onChange={(e) => { setStatus(e.target.value as StatusFilter); setPage(0); }}
          >
            <option value="">All statuses</option>
            <option value="active">Active</option>
            <option value="inactive">Suspended</option>
          </Select>
        </div>

        {error ? (
          <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
        ) : isLoading ? (
          <SkeletonRows />
        ) : !data?.items.length ? (
          filtered ? (
            <EmptyState icon={<UsersIcon className="size-6" />} title="No matching users"
              description={`Try another name, email or ${scope.place.toLowerCase()}, or clear the status filter.`} />
          ) : (
            <EmptyState icon={<UsersIcon className="size-6" />} title="No users yet"
              description={scope.emptyHint}
              action={<Button onClick={() => setAdding(true)}><Plus className="size-4" />Add user</Button>} />
          )
        ) : (
          <>
            <Table head={["User", "Role", scope.place, "Status", "Created", <span className="block text-right">Actions</span>]}>
              {data.items.map((user) => {
                const manageable = user.role === scope.adds;
                return (
                  <tr key={user.id}>
                    <td className="px-4 py-3">
                      <p className="font-medium text-ink">{user.full_name}</p>
                      <p className="text-xs text-muted">{user.email}</p>
                      {user.phone && <p className="text-xs text-muted">{user.phone}</p>}
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={user.role === "master_admin" ? "brand" : "info"}>
                        <Shield className="size-3" />{ROLE_LABEL[user.role]}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-ink-soft">{scope.placeOf(user)}</td>
                    <td className="px-4 py-3"><StatusBadge status={user.is_active ? "active" : "suspended"} /></td>
                    <td className="whitespace-nowrap px-4 py-3 text-ink-soft">{formatDate(user.created_at)}</td>
                    <td className="px-4 py-3">
                      {manageable ? (
                        <div className="flex justify-end gap-1">
                          <a href={`mailto:${user.email}`} title={`Email ${user.full_name}`} aria-label={`Email ${user.full_name}`}
                            className="inline-flex h-8 items-center rounded-md px-2.5 text-ink-soft hover:bg-subtle">
                            <Mail className="size-3.5" />
                          </a>
                          <IconButton label={`Edit ${user.full_name}`} onClick={() => setEditing(user)}>
                            <Pencil className="size-3.5" />
                          </IconButton>
                          <IconButton label={`Reset password for ${user.full_name}`} onClick={() => setResetting(user)}>
                            <KeyRound className="size-3.5" />
                          </IconButton>
                          <IconButton label={`Delete ${user.full_name}`} onClick={() => setDeleting(user)} danger>
                            <Trash2 className="size-3.5" />
                          </IconButton>
                        </div>
                      ) : (
                        <p className="text-right text-muted" title="Platform administrators are managed from the command line">—</p>
                      )}
                    </td>
                  </tr>
                );
              })}
            </Table>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3">
              <div className="flex items-center gap-3 text-sm text-ink-soft">
                <span>Showing <strong className="text-ink">{first}–{last}</strong> of <strong className="text-ink">{data.total}</strong> users</span>
                <Select className="h-8 w-28" aria-label="Users per page" value={pageSize}
                  onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}>
                  {PAGE_SIZES.map((size) => <option key={size} value={size}>{size} / page</option>)}
                </Select>
              </div>
              <Pagination page={page} pages={pages} onChange={setPage} />
            </div>
          </>
        )}
      </Card>

      <AddUserModal scope={scope} open={adding} onClose={() => setAdding(false)} onCreated={setCredentials} />
      <EditUserModal scope={scope} user={editing} onClose={() => setEditing(null)} />
      <DeleteUserModal user={deleting} onClose={() => setDeleting(null)} />
      <ResetPasswordModal user={resetting} onClose={() => setResetting(null)} onReset={setCredentials} />
      <CredentialsModal credentials={credentials} onClose={() => setCredentials(null)} />
    </>
  );
}

function IconButton({ label, onClick, danger, children }: {
  label: string; onClick: () => void; danger?: boolean; children: ReactNode;
}) {
  return (
    <Button variant="ghost" size="sm" className={cn("px-2.5", danger && "text-bad-600")} onClick={onClick}
      title={label} aria-label={label}>
      {children}
    </Button>
  );
}

/** Page numbers around the current page, with the first and last always shown. */
function Pagination({ page, pages, onChange }: { page: number; pages: number; onChange: (page: number) => void }) {
  if (pages <= 1) return null;
  const shown = [...new Set([0, page - 1, page, page + 1, pages - 1])].filter((p) => p >= 0 && p < pages).sort((a, b) => a - b);
  return (
    <nav className="flex items-center gap-1" aria-label="Pages">
      <Button variant="ghost" size="sm" className="px-2" disabled={page === 0} onClick={() => onChange(page - 1)} aria-label="Previous page">
        <ChevronLeft className="size-4" />
      </Button>
      {shown.map((p, i) => (
        <span key={p} className="flex items-center gap-1">
          {i > 0 && p - shown[i - 1] > 1 && <span className="px-1 text-muted">…</span>}
          <Button variant={p === page ? "primary" : "ghost"} size="sm" className="min-w-8 px-2"
            onClick={() => onChange(p)} aria-current={p === page ? "page" : undefined}>
            {p + 1}
          </Button>
        </span>
      ))}
      <Button variant="ghost" size="sm" className="px-2" disabled={page >= pages - 1} onClick={() => onChange(page + 1)} aria-label="Next page">
        <ChevronRight className="size-4" />
      </Button>
    </nav>
  );
}

interface UserForm {
  firstName: string; lastName: string; email: string; phone: string;
  /** The organization (Master Admin) or existing branch (Organization Admin) the person belongs to. */
  placeId: string;
  /** Organization Admin adding a Branch Manager: a new branch (with its address) or an existing one. */
  branchMode: "new" | "existing";
  branch: BranchDraft;
}
const READ_ONLY = "flex h-9 items-center gap-2 rounded-md border border-line bg-subtle px-3 text-sm text-ink-soft";

const emptyForm = (scope: Scope): UserForm => ({
  firstName: "", lastName: "", email: "", phone: "", placeId: "",
  branchMode: scope.place === "Branch" ? "new" : "existing", branch: EMPTY_BRANCH,
});
const tidy = (name: string) => name.replace(/\s+/g, " ").trim();
const fullNameOf = (form: UserForm) => tidy(`${form.firstName} ${form.lastName}`);
const personReady = (form: UserForm) => fullNameOf(form).length >= 2 && Boolean(form.email.trim());
const placeReady = (form: UserForm) => (form.branchMode === "new" ? branchReady(form.branch) : Boolean(form.placeId));
const formReady = (form: UserForm) => personReady(form) && placeReady(form);

/** A saved user in the form's fields: the name is split at its first space. */
function formOf(user: User, scope: Scope): UserForm {
  const [firstName, ...rest] = tidy(user.full_name).split(" ");
  return {
    firstName, lastName: rest.join(" "), email: user.email, phone: user.phone ?? "",
    placeId: (scope.place === "Organization" ? user.organization_id : user.branch_id) ?? "",
    branchMode: "existing", branch: EMPTY_BRANCH,
  };
}

interface Place { id: string; name: string; note?: string }

/** The organizations (Master Admin) or branches (Organization Admin) a person can be put in. */
function usePlaces(scope: Scope, enabled: boolean): Place[] | undefined {
  const organizations = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.get<Page<Organization>>(`/organizations${qs({ limit: 100 })}`),
    enabled: enabled && scope.place === "Organization",
  }).data?.items;
  const branches = useBranches().data?.items;
  return scope.place === "Organization"
    ? organizations?.map((o) => ({ id: o.id, name: o.name, note: o.status === "suspended" ? "suspended" : undefined }))
    : branches?.map((b) => ({ id: b.id, name: b.name, note: b.is_active ? undefined : "inactive" }));
}

/** Who the person is: the same in Add and Edit, for both admins. */
function PersonFields({ value, onChange }: { value: UserForm; onChange: (form: UserForm) => void }) {
  const set = (field: "firstName" | "lastName" | "email" | "phone") => (e: { target: { value: string } }) =>
    onChange({ ...value, [field]: e.target.value });
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="First name">
          <Input required autoFocus placeholder="e.g. John" value={value.firstName} onChange={set("firstName")} />
        </Field>
        <Field label="Last name">
          <Input placeholder="e.g. Doe" value={value.lastName} onChange={set("lastName")} />
        </Field>
      </div>
      <Field label="Email address">
        <Input type="email" required placeholder="name@company.com" value={value.email} onChange={set("email")} />
      </Field>
      <Field label="Phone number" hint="Optional">
        <Input type="tel" pattern={PHONE_PATTERN} maxLength={50} placeholder="+91 98765 43210" value={value.phone} onChange={set("phone")} />
      </Field>
    </>
  );
}

function RoleField({ scope }: { scope: Scope }) {
  return (
    <Field label="Role">
      <p className={READ_ONLY}><Shield className="size-4 text-brand-600" />{ROLE_LABEL[scope.adds]}</p>
    </Field>
  );
}

/** The existing organization or branch, and the role. `locked`: the place can't be changed, and why. */
function PlaceFields({ scope, value, onChange, places, locked }: {
  scope: Scope; value: UserForm; onChange: (form: UserForm) => void; places?: Place[];
  locked?: { place: string; reason: string };
}) {
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={scope.place}>
          {locked ? (
            <p className={READ_ONLY}><Building2 className="size-4 shrink-0" /><span className="truncate">{locked.place}</span></p>
          ) : (
            <Select required value={value.placeId} onChange={(e) => onChange({ ...value, placeId: e.target.value })}>
              <option value="">Select {scope.place.toLowerCase()}…</option>
              {places?.map((p) => <option key={p.id} value={p.id}>{p.name}{p.note ? ` (${p.note})` : ""}</option>)}
            </Select>
          )}
        </Field>
        <RoleField scope={scope} />
      </div>
      {locked && <p className="text-xs text-muted">{locked.reason}</p>}
    </>
  );
}

/** "1 User details — 2 Branch details". */
function Steps({ step }: { step: 1 | 2 }) {
  const pill = (n: 1 | 2, label: string) => (
    <span className={cn(
      "inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium",
      step === n ? "bg-brand-600 text-white" : "border border-line bg-subtle text-ink-soft",
    )}>
      {n < step
        ? <Check className="size-3.5 text-ok-600" />
        : <span className={cn("flex size-4 items-center justify-center rounded-full text-[10px]", step === n ? "bg-white/25" : "bg-line")}>{n}</span>}
      {label}
    </span>
  );
  return (
    <div className="flex items-center justify-center gap-2" aria-label={`Step ${step} of 2`}>
      {pill(1, "User details")}
      <span className={cn("h-px w-8", step === 2 ? "bg-brand-600" : "bg-line")} />
      {pill(2, "Branch details")}
    </div>
  );
}

function AddUserModal({ scope, open, onClose, onCreated }: {
  scope: Scope; open: boolean; onClose: () => void; onCreated: (credentials: Credentials) => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState(() => emptyForm(scope));
  // An Organization Admin adds a Branch Manager in two steps: the person, then their branch.
  const twoSteps = scope.place === "Branch";
  const [step, setStep] = useState<1 | 2>(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) { setForm(emptyForm(scope)); setStep(1); setError(null); }
  }, [open, scope]);
  const places = usePlaces(scope, open);
  const ready = formReady(form);
  const onStepOne = twoSteps && step === 1;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (onStepOne) {
      if (personReady(form)) setStep(2);
      return;
    }
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.post<UserCreated>("/users", {
        full_name: fullNameOf(form),
        email: form.email.trim(),
        phone: form.phone.trim() || null,
        role: scope.adds,
        ...(scope.place === "Organization" ? { organization_id: form.placeId }
          : form.branchMode === "new" ? { new_branch: branchBody(form.branch) } : { branch_id: form.placeId }),
      });
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      if (form.branchMode === "new" && twoSteps) void queryClient.invalidateQueries({ queryKey: ["branches"] });
      toast("ok", `${created.full_name} added as ${ROLE_LABEL[scope.adds]} of ${scope.placeOf(created)}.`);
      onClose();
      if (created.temporary_password) {
        onCreated({ title: "User added", email: created.email, password: created.temporary_password });
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add the user.");
      if (twoSteps && err instanceof ApiError && err.code === "EMAIL_TAKEN") setStep(1);
    } finally {
      setBusy(false);
    }
  }

  const description = !twoSteps
    ? "Create an organization admin account and assign its organization."
    : step === 1 ? "Step 1 of 2: the Branch Manager's details." : "Step 2 of 2: the branch they run.";
  const footer = !twoSteps ? (
    <><Button variant="secondary" onClick={onClose}>Cancel</Button>
      <Button type="submit" form="add-user-form" loading={busy} disabled={!ready}>Add user</Button></>
  ) : step === 1 ? (
    <><Button variant="secondary" onClick={onClose}>Cancel</Button>
      <Button type="submit" form="add-user-form" disabled={!personReady(form)}>Next: branch details<ChevronRight className="size-4" /></Button></>
  ) : (
    <><Button variant="secondary" onClick={() => setStep(1)}><ChevronLeft className="size-4" />Previous</Button>
      <Button type="submit" form="add-user-form" loading={busy} disabled={!ready}>Add user</Button></>
  );

  return (
    <Modal open={open} onClose={onClose} icon={<UserPlus className="size-5" />} title="Add user" description={description} footer={footer}>
      <form id="add-user-form" onSubmit={submit} className="space-y-4">
        {twoSteps && <Steps step={step} />}
        {!twoSteps ? (
          <>
            <PersonFields value={form} onChange={setForm} />
            <PlaceFields scope={scope} value={form} onChange={setForm} places={places} />
          </>
        ) : step === 1 ? (
          <>
            <PersonFields value={form} onChange={setForm} />
            <RoleField scope={scope} />
          </>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-1 rounded-lg bg-subtle p-1" role="radiogroup" aria-label="Branch">
              {(["new", "existing"] as const).map((mode) => (
                <button key={mode} type="button" role="radio" aria-checked={form.branchMode === mode}
                  onClick={() => setForm({ ...form, branchMode: mode })}
                  className={cn("rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    form.branchMode === mode ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink")}>
                  {mode === "new" ? "New branch" : "Existing branch"}
                </button>
              ))}
            </div>
            {form.branchMode === "new" ? (
              <BranchFields value={form.branch} onChange={(branch) => setForm({ ...form, branch })} autoFocus />
            ) : (
              <Field label="Branch" hint={places && !places.length ? "No branches yet: create a new one." : undefined}>
                <Select required autoFocus value={form.placeId} onChange={(e) => setForm({ ...form, placeId: e.target.value })}>
                  <option value="">Select branch…</option>
                  {places?.map((p) => <option key={p.id} value={p.id}>{p.name}{p.note ? ` (${p.note})` : ""}</option>)}
                </Select>
              </Field>
            )}
          </>
        )}
        {!onStepOne && (
          <p className="text-xs text-muted">A temporary password is generated and shown once after the account is created.</p>
        )}
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}

function EditUserModal({ scope, user, onClose }: { scope: Scope; user: User | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState(() => emptyForm(scope));
  const [active, setActive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!user) return;
    setForm(formOf(user, scope)); setActive(user.is_active); setError(null);
  }, [user, scope]);
  // Someone who signed in has notifications and history in their organization, so only an
  // account that never signed in (added to the wrong organization) can move to another
  // (the backend enforces it). A Branch Manager can move between branches of the organization.
  const movable = scope.place === "Branch" || !user?.last_login_at;
  const places = usePlaces(scope, Boolean(user) && movable);
  const placeField = scope.place === "Organization" ? "organization_id" : "branch_id";

  // Only what changed is sent, so the audit entry lists the real changes.
  const changes: Record<string, unknown> = {};
  if (user) {
    if (fullNameOf(form) !== tidy(user.full_name)) changes.full_name = fullNameOf(form);
    if (form.email.trim().toLowerCase() !== user.email) changes.email = form.email.trim();
    if ((form.phone.trim() || null) !== user.phone) changes.phone = form.phone.trim() || null;
    if (form.placeId !== (user[placeField] ?? "")) changes[placeField] = form.placeId;
    if (active !== user.is_active) changes.is_active = active;
  }
  const changed = Object.keys(changes).length > 0;

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!user || !changed) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await api.patch<User>(`/users/${user.id}`, changes);
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      toast("ok", placeField in changes
        ? `${saved.full_name} moved to ${scope.placeOf(saved)}.`
        : `${saved.full_name} updated.`);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={Boolean(user)} onClose={onClose} icon={<Pencil className="size-5" />} title={`Edit ${user?.full_name ?? "user"}`}
      description={`Change the account's details, ${scope.place.toLowerCase()} and status.`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="edit-user-form" loading={busy} disabled={!formReady(form) || !changed}>Save</Button></>}>
      <form id="edit-user-form" onSubmit={save} className="space-y-4">
        <PersonFields value={form} onChange={setForm} />
        <PlaceFields
          scope={scope}
          value={form}
          onChange={setForm}
          places={places}
          locked={movable ? undefined : {
            place: user ? scope.placeOf(user) : "—",
            reason: `${user?.full_name} has signed in before, so the account stays with its organization. To give them access to another organization, add a new account there.`,
          }}
        />
        <Field label="Status" hint={active ? undefined : "A suspended user is signed out everywhere and cannot sign in."}>
          <Select value={active ? "active" : "inactive"} onChange={(e) => setActive(e.target.value === "active")}>
            <option value="active">Active</option>
            <option value="inactive">Suspended</option>
          </Select>
        </Field>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}

/**
 * Only an account that never signed in can be deleted (the backend enforces it): anyone who
 * signed in may have acted, and deleting them would blank their name in the audit trail.
 */
function DeleteUserModal({ user, onClose }: { user: User | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [signedIn, setSignedIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (user) { setSignedIn(Boolean(user.last_login_at)); setError(null); }
  }, [user]);

  async function remove() {
    if (!user) return;
    setBusy(true);
    setError(null);
    try {
      await api.delete(`/users/${user.id}`);
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      toast("ok", `${user.full_name} deleted.`);
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.code === "USER_HAS_SIGNED_IN") setSignedIn(true);
      else setError(err instanceof ApiError ? err.message : "Could not delete the user.");
    } finally {
      setBusy(false);
    }
  }

  async function suspend() {
    if (!user) return;
    setBusy(true);
    setError(null);
    try {
      await api.patch<User>(`/users/${user.id}`, { is_active: false });
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      toast("ok", `${user.full_name} suspended.`);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not suspend the user.");
    } finally {
      setBusy(false);
    }
  }

  const footer = !signedIn ? (
    <><Button variant="secondary" onClick={onClose}>Cancel</Button>
      <Button variant="danger" loading={busy} onClick={remove}><Trash2 className="size-4" />Delete permanently</Button></>
  ) : user?.is_active ? (
    <><Button variant="secondary" onClick={onClose}>Cancel</Button>
      <Button variant="danger" loading={busy} onClick={suspend}>Suspend account</Button></>
  ) : (
    <Button variant="secondary" onClick={onClose}>Close</Button>
  );

  return (
    <Modal open={Boolean(user)} onClose={onClose} icon={<Trash2 className="size-5" />}
      title={signedIn ? `${user?.full_name} can't be deleted` : `Delete ${user?.full_name}?`} footer={footer}>
      <div className="space-y-3 text-sm text-ink-soft">
        {!signedIn ? (
          <p>{user?.full_name} ({user?.email}) has never signed in. The account will be removed permanently; this can't be undone.</p>
        ) : (
          <>
            <p>
              {user?.full_name} has signed in before, so deleting the account would remove their name from the audit trail
              of everything they did.
            </p>
            <p>
              {user?.is_active
                ? "Suspend it instead: they are signed out everywhere and can't sign in, and you can reactivate the account later from Edit."
                : "The account is already suspended, so they can't sign in."}
            </p>
          </>
        )}
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-bad-600" role="alert">{error}</p>}
      </div>
    </Modal>
  );
}

function ResetPasswordModal({ user, onClose, onReset }: {
  user: User | null; onClose: () => void; onReset: (credentials: Credentials) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (user) setError(null); }, [user]);

  async function reset() {
    if (!user) return;
    setBusy(true);
    setError(null);
    try {
      const { temporary_password } = await api.post<{ temporary_password: string | null }>(`/users/${user.id}/reset-password`, {});
      onClose();
      if (temporary_password) onReset({ title: "Password reset", email: user.email, password: temporary_password });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reset the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={Boolean(user)} onClose={onClose} icon={<KeyRound className="size-5" />} title={`Reset password for ${user?.full_name}?`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button loading={busy} onClick={reset}>Generate new password</Button></>}>
      <div className="space-y-3 text-sm text-ink-soft">
        <p>A new temporary password is generated and shown to you once. {user?.full_name} is signed out everywhere and must use it to sign in.</p>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-bad-600" role="alert">{error}</p>}
      </div>
    </Modal>
  );
}

/** Shows a generated password once. */
function CredentialsModal({ credentials, onClose }: { credentials: Credentials | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => setCopied(false), [credentials]);

  async function copy() {
    if (credentials) setCopied(await copyText(credentials.password));
  }

  return (
    <Modal open={Boolean(credentials)} onClose={onClose} icon={<KeyRound className="size-5" />} title={credentials?.title ?? ""}
      description="Copy the password now. It isn't stored and won't be shown again."
      footer={<Button onClick={onClose}>Done</Button>}>
      <div className="space-y-4">
        <Field label="Email">
          <Input readOnly value={credentials?.email ?? ""} />
        </Field>
        <Field label="Temporary password" hint="Share it securely. They can change it after signing in.">
          <div className="flex gap-2">
            <Input readOnly value={credentials?.password ?? ""} className="font-mono" onFocus={(e) => e.target.select()} />
            <Button variant="secondary" onClick={copy} aria-label="Copy password">
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}{copied ? "Copied" : "Copy"}
            </Button>
          </div>
        </Field>
      </div>
    </Modal>
  );
}
