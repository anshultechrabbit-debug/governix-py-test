import { useQueryClient } from "@tanstack/react-query";
import { FolderPlus } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { api, ApiError } from "../api/client";
import type { Policy } from "../api/types";
import { useAuth, useToast } from "../store/hooks";
import { CategoryPicker } from "./CategoryPicker";
import { useBranches, useCategories } from "./domain";
import { Button, Field, Input, Modal, Select, Textarea } from "./ui";

const WHOLE_ORGANIZATION = "";

/**
 * An empty policy (a folder for its versions), created by hand: POST /policies. Its documents
 * are uploaded into it afterwards from the policy's page (Add Version), where this leads.
 */
export function CreatePolicyModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { me } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const categories = useCategories().data;
  const branches = useBranches().data?.items;
  // An Organization Admin's policy can cover the whole organization or one branch;
  // a Branch Manager's always covers their own branch (the backend enforces both).
  const orgAdmin = me?.role === "org_admin";
  const [name, setName] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [branchId, setBranchId] = useState(WHOLE_ORGANIZATION);
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName(""); setBranchId(WHOLE_ORGANIZATION); setDescription(""); setError(null);
    // The default "Policies" category when there is one, else the first.
    setCategoryId((categories?.find((c) => c.slug === "policies") ?? categories?.[0])?.id ?? "");
  }, [open, categories]);

  const ready = name.trim().length >= 2 && Boolean(categoryId);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const policy = await api.post<Policy>("/policies", {
        name: name.replace(/\s+/g, " ").trim(),
        category_id: categoryId,
        description: description.trim() || null,
        ...(orgAdmin && branchId === WHOLE_ORGANIZATION ? { scope: "global" } : { scope: "branch", branch_id: branchId || null }),
      });
      void queryClient.invalidateQueries({ queryKey: ["policies"] });
      toast("ok", `Policy "${policy.name}" created. Add its documents with Add Version.`);
      onClose();
      navigate(`/policies/${policy.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the policy.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} icon={<FolderPlus className="size-5" />} title="Create policy"
      description={`Add a new policy folder for ${me?.organization_name ?? "your organization"}.`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="create-policy-form" loading={busy} disabled={!ready}>
          <FolderPlus className="size-4" />Create policy
        </Button></>}>
      <form id="create-policy-form" onSubmit={submit} className="space-y-4">
        <Field label="Policy name">
          <Input required autoFocus maxLength={500} placeholder="e.g. Retail & Commercial Loan Policy"
            value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Category">
            <CategoryPicker value={categoryId} onChange={setCategoryId} />
          </Field>
          {orgAdmin && (
            <Field label="Applies to">
              <Select value={branchId} onChange={(e) => setBranchId(e.target.value)}>
                <option value={WHOLE_ORGANIZATION}>Whole organization</option>
                {branches?.filter((b) => b.is_active).map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
              </Select>
            </Field>
          )}
        </div>
        <Field label="Description / policy scope" hint="Optional">
          <Textarea rows={3} maxLength={5000}
            placeholder="Brief summary of policy coverage, department ownership and compliance regulations…"
            value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
