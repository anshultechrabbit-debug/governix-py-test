import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Pencil, Plus } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, qs } from "../api/client";
import type { Branch, Page } from "../api/types";
import { BranchFields, branchAddress, branchBody, branchReady, EMPTY_BRANCH, type BranchDraft } from "../components/BranchFields";
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Modal,
  PageHeader,
  SkeletonRows,
  StatusBadge,
  Table,
} from "../components/ui";
import { formatDateTime } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

export function BranchesPage() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Branch | null>(null);
  const [branch, setBranch] = useState<BranchDraft>(EMPTY_BRANCH);
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["branches"],
    queryFn: () => api.get<Page<Branch>>(`/branches${qs({ limit: 100 })}`),
  });

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      const body = branchBody(branch);
      await api.post<Branch>("/branches", { ...body, code: code.toUpperCase() });
      toast("ok", `Branch "${body.name}" created.`);
      setModalOpen(false);
      setBranch(EMPTY_BRANCH);
      setCode("");
      queryClient.invalidateQueries({ queryKey: ["branches"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to create branch.");
    } finally {
      setSubmitting(false);
    }
  };

  const toggleStatus = async (branch: Branch) => {
    try {
      await api.patch<Branch>(`/branches/${branch.id}`, { is_active: !branch.is_active });
      toast("ok", `Branch "${branch.name}" is now ${!branch.is_active ? "active" : "inactive"}.`);
      queryClient.invalidateQueries({ queryKey: ["branches"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to update branch.");
    }
  };

  return (
    <>
      <PageHeader
        title="Branches"
        subtitle="Manage organizational offices and geographic divisions"
        actions={
          can("branches:manage") && (
            <Button onClick={() => setModalOpen(true)}>
              <Plus className="size-4" /> Add branch
            </Button>
          )
        }
      />

      <Card>
        {error ? (
          <div className="p-4">
            <ErrorState error={error} onRetry={refetch} />
          </div>
        ) : isLoading ? (
          <SkeletonRows />
        ) : !data?.items.length ? (
          <EmptyState
            icon={<Building2 className="size-6" />}
            title="No branches found"
            description="Create branch offices to organize policies and staff by physical location or business unit."
            action={
              can("branches:manage") && (
                <Button onClick={() => setModalOpen(true)}>
                  <Plus className="size-4" /> Add branch
                </Button>
              )
            }
          />
        ) : (
          <Table head={["Branch Name", "Code", "Address", "Status", "Created", "Actions"]}>
            {data.items.map((branch) => (
              <tr key={branch.id}>
                <td className="px-4 py-3 font-medium text-ink">{branch.name}</td>
                <td className="px-4 py-3 font-mono text-xs text-muted">{branch.code}</td>
                <td className="px-4 py-3 text-ink-soft">
                  {branchAddress(branch) ?? <span className="text-muted">Not set</span>}
                </td>
                <td className="px-4 py-3">
                  <StatusBadge status={branch.is_active ? "active" : "suspended"} />
                </td>
                <td className="px-4 py-3 text-ink-soft">{formatDateTime(branch.created_at)}</td>
                <td className="px-4 py-3">
                  <div className="flex gap-1">
                    {can("branches:manage") && (
                      <Button variant="ghost" size="sm" onClick={() => setEditing(branch)}>
                        <Pencil className="size-3.5" />Edit
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => toggleStatus(branch)}
                    >
                      {branch.is_active ? "Deactivate" : "Activate"}
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="Add new branch"
        footer={
          <>
            <Button variant="secondary" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="create-branch-form"
              loading={submitting}
              disabled={!branchReady(branch) || !code.trim()}
            >
              Create branch
            </Button>
          </>
        }
      >
        <form id="create-branch-form" onSubmit={handleCreate} className="space-y-4">
          <BranchFields value={branch} onChange={setBranch} />
          <Field label="Branch code" hint="Short unique identifier, e.g. HQ, MUM-01">
            <Input
              required
              placeholder="HQ"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
            />
          </Field>
        </form>
      </Modal>

      <EditBranchModal branch={editing} onClose={() => setEditing(null)} />
    </>
  );
}

/** Name and address. Branches made before addresses were kept get theirs here. */
function EditBranchModal({ branch, onClose }: { branch: Branch | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState<BranchDraft>(EMPTY_BRANCH);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!branch) return;
    setDraft({
      name: branch.name, street_address: branch.street_address ?? "", state: branch.state ?? "",
      postal_code: branch.postal_code ?? "",
    });
    setError(null);
  }, [branch]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!branch || !branchReady(draft)) return;
    setBusy(true);
    setError(null);
    try {
      await api.patch<Branch>(`/branches/${branch.id}`, branchBody(draft));
      toast("ok", `Branch "${draft.name.trim()}" updated.`);
      void queryClient.invalidateQueries({ queryKey: ["branches"] });
      void queryClient.invalidateQueries({ queryKey: ["users"] }); // the Users page shows branch names
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={Boolean(branch)} onClose={onClose} icon={<Pencil className="size-5" />} title={`Edit ${branch?.name ?? "branch"}`}
      description={branch ? `Code ${branch.code}` : undefined}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="edit-branch-form" loading={busy} disabled={!branchReady(draft)}>Save</Button></>}>
      <form id="edit-branch-form" onSubmit={save} className="space-y-4">
        <BranchFields value={draft} onChange={setDraft} />
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
