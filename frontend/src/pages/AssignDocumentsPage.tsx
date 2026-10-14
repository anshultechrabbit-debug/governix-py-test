import { useQuery } from "@tanstack/react-query";
import { ClipboardCheck, Plus, Settings2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { UserAssignments } from "../api/types";
import { AssignPolicyModal, UserPoliciesModal } from "../components/Assignments";
import { Badge, Button, Card, EmptyState, ErrorState, Input, PageHeader, SkeletonRows, Stat, Table } from "../components/ui";
import { formatDate } from "../lib/format";

/** How many assigned policies a row names before "+N more". */
const SHOWN = 3;

/**
 * A Branch Manager's Users and the documents (policies) each may read. Users see, search and ask
 * the AI about a policy only while it is assigned to them, so this is their whole access.
 */
export function AssignDocumentsPage() {
  const [search, setSearch] = useState("");
  const [assigning, setAssigning] = useState(false);
  const [managing, setManaging] = useState<{ id: string; full_name: string } | null>(null);
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["assignments", "by-user"],
    queryFn: () => api.get<UserAssignments[]>("/assignments/users"),
  });

  const users = data ?? [];
  const term = search.trim().toLowerCase();
  const shown = term
    ? users.filter((u) => u.full_name.toLowerCase().includes(term) || u.email.toLowerCase().includes(term)
      || u.policies.some((p) => p.policy_name.toLowerCase().includes(term)))
    : users;
  const withDocuments = users.filter((u) => u.policies.length).length;
  const assignments = users.reduce((total, u) => total + u.policies.length, 0);

  return (
    <>
      <PageHeader
        title="Assign documents"
        subtitle="Give your branch's users the policies they need. A user reads and asks the AI only about what is assigned to them."
        actions={<Button onClick={() => setAssigning(true)} disabled={!users.length}><Plus className="size-4" />Assign document</Button>}
      />

      <div className="mb-5 grid gap-3 sm:mb-6 sm:grid-cols-2 sm:gap-4 xl:grid-cols-4">
        <Stat label="Branch users" value={data ? users.length : "—"} hint="Users you manage" />
        <Stat label="Assigned documents" value={data ? assignments : "—"} hint="Across all users" />
        <Stat label="Users with documents" value={data ? withDocuments : "—"} hint="Can read at least one policy" />
        <Stat label="Without any document" value={data ? users.length - withDocuments : "—"}
          tone={data && users.length - withDocuments ? "warn" : undefined} hint="Can't read or ask about anything yet" />
      </div>

      <Card>
        <div className="border-b border-line p-4">
          <Input className="max-w-sm" placeholder="Search by user, email or policy…" value={search}
            onChange={(e) => setSearch(e.target.value)} />
        </div>
        {error ? (
          <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
        ) : isLoading ? (
          <SkeletonRows />
        ) : !users.length ? (
          <EmptyState icon={<ClipboardCheck className="size-6" />} title="No users in your branch yet"
            description="Add your branch's users first; then assign them the policies they need."
            action={<Link to="/users"><Button>Go to Branch Users</Button></Link>} />
        ) : !shown.length ? (
          <EmptyState icon={<ClipboardCheck className="size-6" />} title="No matching users"
            description="Try another name, email or policy." />
        ) : (
          <Table head={["User", "Assigned documents", "Count", "Added", <span className="block text-right">Actions</span>]}>
            {shown.map((user) => (
              <tr key={user.user_id}>
                <td className="px-4 py-3">
                  <p className="font-medium text-ink">
                    {user.full_name}{!user.is_active && <Badge tone="bad" className="ml-2">Suspended</Badge>}
                  </p>
                  <p className="text-xs text-muted">{user.email}</p>
                </td>
                <td className="px-4 py-3">
                  {user.policies.length ? (
                    <div className="flex max-w-md flex-wrap gap-1.5">
                      {user.policies.slice(0, SHOWN).map((p) => (
                        <Link key={p.policy_id} to={`/policies/${p.policy_id}`} title={p.branch_id ? "Branch policy" : "Global policy"}>
                          <Badge tone={p.branch_id ? "info" : "brand"} className="max-w-[16rem] truncate hover:underline">{p.policy_name}</Badge>
                        </Link>
                      ))}
                      {user.policies.length > SHOWN && <Badge>+{user.policies.length - SHOWN} more</Badge>}
                    </div>
                  ) : <span className="text-muted">None yet</span>}
                </td>
                <td className="px-4 py-3 tabular-nums text-ink-soft">{user.policies.length}</td>
                <td className="whitespace-nowrap px-4 py-3 text-ink-soft">{formatDate(user.created_at)}</td>
                <td className="px-4 py-3 text-right">
                  <Button variant="ghost" size="sm" onClick={() => setManaging({ id: user.user_id, full_name: user.full_name })}>
                    <Settings2 className="size-3.5" />Manage
                  </Button>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <AssignPolicyModal open={assigning} onClose={() => setAssigning(false)} />
      <UserPoliciesModal user={managing} onClose={() => setManaging(null)} />
    </>
  );
}
