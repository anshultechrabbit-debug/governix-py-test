import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Building2, FileText, Landmark, Layers, Trash2, Users } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { DeletableType, DeletionPreview, DeletionResult, HierarchyNode } from "../api/types";
import { useToast } from "../store/hooks";
import { Button, ErrorState, Modal, SkeletonRows } from "./ui";

const PATH: Record<DeletableType, string> = {
  organization: "organizations", branch: "branches", department: "departments",
};
const ROLE_LABEL: Record<string, [string, string]> = {
  org_admin: ["Organization Admin", "Organization Admins"],
  branch_manager: ["Branch Manager", "Branch Managers"],
  department_user: ["User", "Users"],
};
const NODE_ICON: Record<DeletableType, typeof Landmark> = { organization: Landmark, branch: Building2, department: Layers };

function plural(count: number, one: string, many: string) {
  return `${count} ${count === 1 ? one : many}`;
}

/** One level of the tree: its people and policies, then the levels below it. */
function Node({ node }: { node: HierarchyNode }) {
  const Icon = NODE_ICON[node.type];
  const people = Object.entries(node.users).filter(([, n]) => n > 0);
  return (
    <li>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1">
        <Icon className="size-4 shrink-0 text-muted" />
        <span className="font-medium text-ink">{node.name}</span>
        {node.code && <span className="font-mono text-xs text-muted">{node.code}</span>}
        {people.map(([role, n]) => (
          <span key={role} className="inline-flex items-center gap-1 rounded-full bg-subtle px-2 text-xs text-ink-soft">
            <Users className="size-3" />{plural(n, ...(ROLE_LABEL[role] ?? [role, role]))}
          </span>
        ))}
        {node.policies > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-warn-50 px-2 text-xs text-warn-600">
            <FileText className="size-3" />{plural(node.policies, "policy", "policies")} archived
          </span>
        )}
      </div>
      {node.children.length > 0 && (
        <ul className="ml-2 border-l border-line pl-3">
          {node.children.map((child) => <Node key={child.id} node={child} />)}
        </ul>
      )}
    </li>
  );
}

/**
 * Delete an organization, branch or department and everything below it. The dialog shows the
 * hierarchy the backend says the delete reaches; "Delete all" confirms it, Cancel changes nothing.
 * The delete is soft: records are kept but leave Governix, and their users are signed out.
 */
export function DeleteHierarchyDialog({ type, target, onClose, onDeleted, invalidate = [] }: {
  type: DeletableType;
  target: { id: string; name: string } | null;
  onClose: () => void;
  onDeleted?: (result: DeletionResult) => void;
  /** Query keys to refresh after the delete, besides the users list. */
  invalidate?: string[];
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const base = target ? `/${PATH[type]}/${target.id}` : "";
  const preview = useQuery({
    queryKey: ["delete-preview", type, target?.id],
    queryFn: () => api.get<DeletionPreview>(`${base}/delete-preview`),
    enabled: Boolean(target),
    gcTime: 0, // always the current hierarchy, never a cached one
  });
  const remove = useMutation({
    mutationFn: () => api.delete<DeletionResult>(`${base}?confirm=true`),
    onSuccess: (result) => {
      toast("ok", result.message);
      for (const key of [...invalidate, "users", "policies", "documents", "branches"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      onClose();
      onDeleted?.(result);
    },
    onError: (err) => toast("bad", err instanceof ApiError ? err.message : `The ${type} could not be deleted.`),
  });
  const data = preview.data;
  const close = () => !remove.isPending && onClose();

  return (
    <Modal
      open={Boolean(target)}
      onClose={close}
      wide
      icon={<Trash2 className="size-5" />}
      title={`Delete ${target?.name ?? type}?`}
      description={`Everything in this ${type} is deleted with it.`}
      footer={<>
        <Button variant="secondary" onClick={close} disabled={remove.isPending}>Cancel</Button>
        <Button variant="danger" disabled={!data} loading={remove.isPending} onClick={() => remove.mutate()}>
          <Trash2 className="size-4" />Delete all
        </Button>
      </>}
    >
      {preview.error ? <ErrorState error={preview.error} onRetry={preview.refetch} />
        : !data ? <SkeletonRows rows={3} />
        : (
          <div className="space-y-4 text-sm">
            <div className="flex gap-3">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-bad-50">
                <AlertTriangle className="size-5 text-bad-600" />
              </div>
              <p className="text-ink-soft">{data.message}</p>
            </div>
            <div className="max-h-80 overflow-auto rounded-lg border border-line p-3">
              <ul><Node node={data.hierarchy} /></ul>
            </div>
          </div>
        )}
    </Modal>
  );
}
