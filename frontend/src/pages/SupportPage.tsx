import { useQuery } from "@tanstack/react-query";
import { LifeBuoy, Plus, Send } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { api, ApiError, qs } from "../api/client";
import type { Page, Ticket, TicketDetail, TicketStats } from "../api/types";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Input, Modal, PageHeader, Select, SkeletonRows, Tabs, Textarea } from "../components/ui";
import { formatDateTime } from "../lib/format";
import { useAuth } from "../store/hooks";

type Box = "mine" | "queue";

export const TICKET_STATUS: Record<Ticket["status"], [string, "info" | "warn" | "ok" | "neutral"]> = {
  open: ["Open", "info"],
  in_progress: ["In progress", "info"],
  waiting_for_response: ["Waiting for response", "warn"],
  resolved: ["Resolved", "ok"],
  closed: ["Closed", "neutral"],
};
export const PRIORITY_TONE: Record<Ticket["priority"], "neutral" | "info" | "warn" | "bad"> = {
  low: "neutral", medium: "info", high: "warn", urgent: "bad",
};

/** Support categories offered by default; "Other" lets the person name their own. */
const SUPPORT_CATEGORIES = [
  "Core Banking Software", "Login & Account Access", "Document Upload & Processing", "AI Assistant",
  "Policies & Versions", "Users & Permissions", "Network & Connectivity", "Printer & Hardware",
];
const OTHER = "Other";
const ROLE_LABEL: Record<string, string> = {
  department_user: "User", branch_manager: "Branch Manager", org_admin: "Organization Admin",
};

/**
 * Technical support: anyone in an organization raises a ticket, and the platform team (the
 * Master Admin) handles it. Complaints about policies are separate (ComplaintsPage).
 */
export function SupportPage() {
  const { me } = useAuth();
  const platform = me?.role === "master_admin";
  const raises = !platform;
  // Before support went to the platform, Branch Managers and Organization Admins handled their
  // level's requests; the tab stays while they still have some.
  const { data: older } = useQuery({
    queryKey: ["tickets", "stats", "support", "queue"],
    queryFn: () => api.get<TicketStats>(`/tickets/stats${qs({ box: "queue", kind: "support" })}`),
    enabled: raises && me?.role !== "department_user",
  });
  const hasOlder = Boolean(older && Object.values(older).some(Boolean));
  const [box, setBox] = useState<Box>(raises ? "mine" : "queue");
  const [status, setStatus] = useState("active");
  const [creating, setCreating] = useState(false);
  const params = { box, status, kind: "support", limit: 100 };
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["tickets", params],
    queryFn: () => api.get<Page<Ticket>>(`/tickets${qs(params)}`),
  });

  return (
    <>
      <PageHeader title={platform ? "Platform Support" : "Support"}
        subtitle={platform ? "Support requests from every organization." : "Ask the Governix technical support team for help. You are notified of every reply."}
        actions={raises && <Button onClick={() => setCreating(true)}><Plus className="size-4" />New ticket</Button>} />
      {raises && hasOlder && (
        <Tabs<Box> value={box} onChange={setBox} tabs={[{ id: "mine", label: "My tickets" }, { id: "queue", label: "To handle" }]} />
      )}
      <Card className="mt-4">
        <div className="flex flex-wrap gap-3 border-b border-line p-4">
          <Select className="w-52" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="active">Open and in progress</option>
            <option value="">All statuses</option>
            {Object.entries(TICKET_STATUS).map(([value, [label]]) => <option key={value} value={value}>{label}</option>)}
          </Select>
        </div>
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.items.length ? (
            <EmptyState icon={<LifeBuoy className="size-6" />} title="No tickets"
              description={box === "queue" ? "Nothing is waiting for you." : "Tickets you raise appear here with every response."}
              action={raises && box === "mine" && <Button onClick={() => setCreating(true)}><Plus className="size-4" />New ticket</Button>} />
          ) : (
            <ul className="divide-y divide-line">
              {data.items.map((t) => {
                const [label, tone] = TICKET_STATUS[t.status];
                const from = [t.created_by_name, t.created_by_role && ROLE_LABEL[t.created_by_role]].filter(Boolean).join(", ");
                return (
                  <li key={t.id}>
                    <Link to={`/support/${t.id}`} className="flex flex-wrap items-center gap-3 px-4 sm:px-5 py-3 hover:bg-subtle/50">
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium">{t.subject}</p>
                        <p className="text-xs text-muted">
                          {t.category ?? "Support"}
                          {platform && t.organization_name ? ` · ${t.organization_name}` : ""}
                          {box === "queue" && from ? ` · ${from}` : ""} · updated {formatDateTime(t.updated_at)}
                          {t.message_count ? ` · ${t.message_count} update${t.message_count === 1 ? "" : "s"}` : ""}
                        </p>
                      </div>
                      <Badge tone={tone}>{label}</Badge>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
      </Card>
      <NewTicketModal open={creating} onClose={() => setCreating(false)} />
    </>
  );
}

function NewTicketModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { me } = useAuth();
  const navigate = useNavigate();
  const isUser = me?.role === "department_user";
  const [category, setCategory] = useState(SUPPORT_CATEGORIES[0]);
  const [otherCategory, setOtherCategory] = useState("");
  const [subject, setSubject] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setCategory(SUPPORT_CATEGORIES[0]); setOtherCategory(""); setSubject(""); setDescription(""); setError(null);
  }, [open]);
  const chosen = category === OTHER ? otherCategory.trim() : category;
  const invalid = chosen.length < 2 || subject.trim().length < 3 || description.trim().length < 5;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (invalid) return;
    setBusy(true);
    try {
      const ticket = await api.post<TicketDetail>("/tickets", {
        kind: "support", category: chosen, subject: subject.trim(), description: description.trim(),
      });
      onClose();
      navigate(`/support/${ticket.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not raise the ticket.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} icon={<LifeBuoy className="size-5" />} title="New support ticket"
      description="Submit your question and issue description to technical support."
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="new-ticket" disabled={invalid} loading={busy}><Send className="size-4" />Submit ticket</Button></>}>
      <form id="new-ticket" onSubmit={submit} className="space-y-4">
        <Field label="Support category *">
          <Select value={category} onChange={(e) => setCategory(e.target.value)}>
            {[...SUPPORT_CATEGORIES, OTHER].map((c) => <option key={c} value={c}>{c}</option>)}
          </Select>
        </Field>
        {category === OTHER && (
          <Field label="Your category *">
            <Input autoFocus maxLength={100} placeholder="e.g. Branch scanner setup" value={otherCategory}
              onChange={(e) => setOtherCategory(e.target.value)} />
          </Field>
        )}
        <Field label="Question / issue title *">
          <Input value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={300}
            placeholder="e.g. Receipt printer offline error on station 04…" />
        </Field>
        <Field label="Small description *">
          <Textarea rows={5} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={10000}
            placeholder="Provide a brief description of the issue, symptoms, or error details…" />
        </Field>
        {isUser && (
          <p className="text-xs text-muted">
            A question or complaint about a policy? Use <Link to="/complaints" onClick={onClose} className="text-brand-700 hover:underline">Complaints</Link>; it goes to your Branch Manager.
          </p>
        )}
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}
