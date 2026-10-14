import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Landmark, Pencil, Plus, Server } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { api, ApiError, qs } from "../api/client";
import type { NginxOverview, Organization, OrganizationOptions, Page } from "../api/types";
import {
  firstFreePort, newOrgForm, OrganizationFields, orgBody, orgFormOf, orgFormReady, OrgLogo, uploadLogo, type OrgForm,
} from "../components/OrganizationForm";
import { NginxBadge } from "../components/NginxBadge";
import {
  Button, Card, EmptyState, ErrorState, Field, Input, Modal, PageHeader, PasswordInput, SkeletonRows, StatusBadge, Table,
} from "../components/ui";
// import { AIUsageCard } from "../components/AIUsageCard";
import { formatDateTime } from "../lib/format";
import { useToast } from "../store/hooks";

/** The port range, the ports in use and the fonts, while a form is open. */
function useOrganizationOptions(enabled: boolean) {
  return useQuery({
    queryKey: ["organization-options"],
    queryFn: () => api.get<OrganizationOptions>("/organizations/options"),
    enabled,
  }).data;
}

export function OrganizationsPage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Organization | null>(null);
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.get<Page<Organization>>(`/organizations${qs({ limit: 100 })}`),
  });
  // Each organization's port is served by an nginx server block, deployed from its Nginx page.
  const { data: nginx } = useQuery({
    queryKey: ["nginx"],
    queryFn: () => api.get<NginxOverview>("/nginx"),
  });
  const nginxSites = new Map(nginx?.sites.map((site) => [site.organization_id, site]));

  const toggleStatus = async (org: Organization) => {
    const nextStatus = org.status === "active" ? "suspended" : "active";
    try {
      await api.patch<Organization>(`/organizations/${org.id}`, { status: nextStatus });
      toast("ok", `Organization ${org.name} marked as ${nextStatus}.`);
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to update organization status.");
    }
  };

  return (
    <>
      <PageHeader
        title="Organizations"
        subtitle="Create organizations, their port, branding and first administrator"
        actions={<Button onClick={() => setCreating(true)}><Plus className="size-4" />New organization</Button>}
      />
      {/* OpenAI usage card: hidden for now. */}
      {/* <div className="mb-6"><AIUsageCard /></div> */}
      <Card>
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.items.length ? (
            <EmptyState icon={<Landmark className="size-6" />} title="No organizations registered"
              description="Create your first organization with its admin to onboard a bank."
              action={<Button onClick={() => setCreating(true)}><Plus className="size-4" />Add organization</Button>} />
          ) : (
            <Table head={["Organization", "Port", "Nginx", "Branding", "Contact", "Status", "Created", ""]}>
              {data.items.map((org) => (
                <tr key={org.id}>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <OrgLogo org={org} />
                      <div><p className="font-medium text-ink">{org.name}</p><p className="font-mono text-xs text-muted">{org.slug}</p></div>
                    </div>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-ink-soft">{org.allowed_port ?? "—"}</td>
                  <td className="px-4 py-3">
                    {nginxSites.get(org.id) ? <NginxBadge state={nginxSites.get(org.id)!.state} /> : <span className="text-muted">—</span>}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex gap-1.5">
                      {[org.primary_color, org.secondary_color].map((c, i) => c
                        ? <span key={i} className="size-5 rounded border border-line" style={{ backgroundColor: c }} title={c} />
                        : <span key={i} className="size-5 rounded border border-dashed border-line-strong" title="Default" />)}
                    </div>
                    {org.brand_font && <p className="mt-1 text-[11px] text-muted">{org.brand_font}</p>}
                  </td>
                  <td className="px-4 py-3 text-xs text-ink-soft">{org.contact_email ?? "—"}{org.contact_phone && <><br />{org.contact_phone}</>}</td>
                  <td className="px-4 py-3"><StatusBadge status={org.status} /></td>
                  <td className="px-4 py-3 text-ink-soft">{formatDateTime(org.created_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setEditing(org)}><Pencil className="size-3.5" />Edit</Button>
                      <Link to={`/organizations/${org.id}/nginx`}
                        className="inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-sm font-medium text-ink-soft hover:bg-subtle">
                        <Server className="size-3.5" />Nginx
                      </Link>
                      <Button variant="ghost" size="sm" onClick={() => toggleStatus(org)}>{org.status === "active" ? "Suspend" : "Activate"}</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </Table>
          )}
      </Card>
      <CreateOrganizationModal open={creating} onClose={() => setCreating(false)} />
      <EditOrganizationModal org={editing} onClose={() => setEditing(null)} />
    </>
  );
}

function CreateOrganizationModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const options = useOrganizationOptions(open);
  const [form, setForm] = useState<OrgForm>(newOrgForm);
  const [admin, setAdmin] = useState({ full_name: "", email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setForm(newOrgForm()); setAdmin({ full_name: "", email: "", password: "" }); setError(null);
  }, [open]);
  // Start on the first free port once the ports in use are known.
  useEffect(() => {
    const port = open && options ? firstFreePort(options) : undefined;
    if (port) setForm((current) => (current.port ? current : { ...current, port: String(port) }));
  }, [open, options]);

  const adminGiven = Boolean(admin.email || admin.full_name || admin.password);
  const invalid = !orgFormReady(form, options)
    || (adminGiven && (!admin.email || admin.full_name.trim().length < 2 || admin.password.length < 12));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (invalid) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.post<Organization>("/organizations", {
        ...orgBody(form), slug: form.slug,
        admin: adminGiven ? { ...admin, full_name: admin.full_name.trim() } : undefined,
      });
      let logoError: string | null = null;
      if (form.logo) {
        try {
          await uploadLogo(created.id, form.logo);
        } catch (err) {
          logoError = err instanceof ApiError ? err.message : "The upload failed.";
        }
      }
      if (logoError) toast("bad", `"${created.name}" was created, but its logo was not saved: ${logoError} Upload it again from Edit.`);
      else toast("ok", `Organization "${created.name}" created on port ${created.allowed_port}${adminGiven ? ` with admin ${admin.email}` : ""}.`);
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
      void queryClient.invalidateQueries({ queryKey: ["organization-options"] });
      void queryClient.invalidateQueries({ queryKey: ["nginx"] });
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create the organization.");
      void queryClient.invalidateQueries({ queryKey: ["organization-options"] }); // a port may have been taken meanwhile
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} wide icon={<Building2 className="size-5" />} title="Add new organization"
      description="Configure the tenant's name, network port, logo and brand theme."
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="create-org-form" loading={busy} disabled={invalid}>Create organization</Button></>}>
      <form id="create-org-form" onSubmit={submit} className="space-y-6">
        <OrganizationFields value={form} onChange={setForm} options={options} />
        <section className="space-y-4 rounded-lg border border-line p-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-ink-soft">First organization admin</h3>
            <span className="text-xs text-muted">Optional</span>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Full name"><Input value={admin.full_name} onChange={(e) => setAdmin({ ...admin, full_name: e.target.value })} /></Field>
            <Field label="Email"><Input type="email" value={admin.email} onChange={(e) => setAdmin({ ...admin, email: e.target.value })} placeholder="admin@abcbank.com" /></Field>
          </div>
          <Field
            label="Temporary password"
            hint={
              admin.password && admin.password.length < 12
                ? `At least 12 characters required (${admin.password.length}/12 entered)`
                : "At least 12 characters. Share it securely; the admin can change it after signing in."
            }
          >
            <PasswordInput value={admin.password} onChange={(e) => setAdmin({ ...admin, password: e.target.value })} autoComplete="new-password" />
          </Field>
        </section>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}

function EditOrganizationModal({ org, onClose }: { org: Organization | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const options = useOrganizationOptions(Boolean(org));
  const [form, setForm] = useState<OrgForm>(newOrgForm);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!org) return;
    setForm(orgFormOf(org)); setError(null);
  }, [org]);

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/organizations/${org!.id}`, orgBody(form));
      if (form.logo) await uploadLogo(org!.id, form.logo);
      toast("ok", "Organization updated.");
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
      void queryClient.invalidateQueries({ queryKey: ["organization-options"] });
      void queryClient.invalidateQueries({ queryKey: ["my-organization"] });
      void queryClient.invalidateQueries({ queryKey: ["nginx"] }); // a new port leaves the deployed file out of date
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={Boolean(org)} onClose={onClose} wide icon={<Pencil className="size-5" />} title={`Edit ${org?.name ?? "organization"}`}
      description="Change the tenant's name, network port, logo and brand theme."
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="edit-org-form" loading={busy} disabled={!orgFormReady(form, options, org?.id)}>Save</Button></>}>
      <form id="edit-org-form" onSubmit={save} className="space-y-6">
        <OrganizationFields value={form} onChange={setForm} options={options} org={org} />
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
