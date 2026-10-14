import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Eye, MessageSquareReply, MessageSquareWarning, Plus, Send } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { api, ApiError, qs } from "../api/client";
import type { Page, Policy, Ticket, TicketStats, TicketStatus } from "../api/types";
import {
  Badge, Button, Card, EmptyState, ErrorState, Field, Input, Modal, PageHeader, Select, SkeletonRows, Stat, Table, Textarea,
} from "../components/ui";
import { formatDate } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

/**
 * Complaints: a User asks their Branch Manager about a policy (or a general procedure); the Branch
 * Manager replies and resolves. These are tickets of kind "complaint" (tickets/service.py), so every
 * reply, status change and notification is the ticket's, and the full thread is at /support/:id.
 */

const GENERAL = "General operational procedure";

/** A complaint's status in the words of whoever reads it. */
function statusLabel(status: TicketStatus, handler: boolean): [string, "info" | "warn" | "ok" | "neutral"] {
  switch (status) {
    case "open": return ["Pending", "warn"];
    case "in_progress": return ["In progress", "info"];
    case "waiting_for_response": return [handler ? "Waiting for the user" : "Your reply needed", "warn"];
    case "resolved": return ["Resolved", "ok"];
    case "closed": return ["Closed", "neutral"];
  }
}

export function ComplaintsPage() {
  const { me } = useAuth();
  // A Branch Manager handles their branch's complaints; a User sees the ones they raised.
  const handler = me?.role === "branch_manager";
  const box = handler ? "queue" : "mine";
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [responding, setResponding] = useState<Ticket | null>(null);

  const { data: stats } = useQuery({
    queryKey: ["tickets", "stats", "complaint", box],
    queryFn: () => api.get<TicketStats>(`/tickets/stats${qs({ box, kind: "complaint" })}`),
  });
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["tickets", "complaints", box, status, search],
    queryFn: () => api.get<Page<Ticket>>(`/tickets${qs({ box, kind: "complaint", status, search, limit: 100 })}`),
  });

  const pending = stats?.open;
  const inProgress = stats && stats.in_progress + stats.waiting_for_response;
  const resolved = stats && stats.resolved + stats.closed;
  const total = stats && Object.values(stats).reduce((sum, n) => sum + n, 0);

  return (
    <>
      <PageHeader
        title="Complaints"
        subtitle={handler
          ? "Questions and complaints from your branch's users. Reply, and resolve them when they are answered."
          : `Ask your Branch Manager about a policy or a procedure. You are notified of every reply.`}
        actions={!handler && <Button onClick={() => setSubmitting(true)}><Plus className="size-4" />Submit complaint</Button>}
      />

      <div className="mb-5 grid gap-3 sm:mb-6 sm:grid-cols-2 sm:gap-4 xl:grid-cols-4">
        <Stat label="Pending" value={pending ?? "—"} tone={pending ? "warn" : undefined} hint={handler ? "Not answered yet" : "Not picked up yet"} />
        <Stat label="In progress" value={inProgress ?? "—"} hint="Being discussed" />
        <Stat label="Resolved" value={resolved ?? "—"} hint="Answered and closed" />
        <Stat label="Total" value={total ?? "—"} hint={handler ? "From your branch" : "Raised by you"} />
      </div>

      <Card>
        <div className="flex flex-wrap items-center gap-3 border-b border-line p-4">
          <Input className="min-w-0 flex-1 sm:max-w-sm"
            placeholder={handler ? "Search by user, subject, policy or question…" : "Search by subject, policy or question…"}
            value={search} onChange={(e) => setSearch(e.target.value)} />
          <Select className="max-w-[12rem]" aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            <option value="active">Not resolved</option>
            <option value="open">Pending</option>
            <option value="in_progress">In progress</option>
            <option value="waiting_for_response">{handler ? "Waiting for the user" : "Your reply needed"}</option>
            <option value="resolved">Resolved</option>
            <option value="closed">Closed</option>
          </Select>
        </div>
        {error ? (
          <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
        ) : isLoading ? (
          <SkeletonRows />
        ) : !data?.items.length ? (
          <EmptyState icon={<MessageSquareWarning className="size-6" />}
            title={search || status ? "No matching complaints" : "No complaints yet"}
            description={search || status ? "Try other words or another status."
              : handler ? "Complaints your branch's users raise appear here." : "Ask your Branch Manager about a policy or a procedure."}
            action={!handler && !search && !status && <Button onClick={() => setSubmitting(true)}><Plus className="size-4" />Submit complaint</Button>} />
        ) : (
          <Table head={[
            ...(handler ? ["User"] : []), "Policy / category", "Subject & question", "Status", "Manager reply", "Date",
            <span className="block text-right">Actions</span>,
          ]}>
            {data.items.map((ticket) => {
              const [label, tone] = statusLabel(ticket.status, handler);
              const done = ticket.status === "resolved" || ticket.status === "closed";
              return (
                <tr key={ticket.id} className="align-top">
                  {handler && <td className="px-4 py-3 font-medium text-ink">{ticket.created_by_name ?? "—"}</td>}
                  <td className="px-4 py-3 text-ink-soft">
                    {ticket.policy_id && ticket.policy_name
                      ? <Link to={`/policies/${ticket.policy_id}`} className="text-brand-700 hover:underline">{ticket.policy_name}</Link>
                      : ticket.category ?? GENERAL}
                  </td>
                  <td className="max-w-sm px-4 py-3">
                    <Link to={`/support/${ticket.id}`} className="font-medium text-ink hover:underline">{ticket.subject}</Link>
                    <p className="line-clamp-2 text-xs text-muted">{ticket.description}</p>
                  </td>
                  <td className="px-4 py-3"><Badge tone={tone}>{label}</Badge></td>
                  <td className="max-w-xs px-4 py-3 text-xs">
                    {ticket.last_reply_body ? (
                      <>
                        <p className="line-clamp-2 text-ink-soft">{ticket.last_reply_body}</p>
                        <p className="mt-0.5 text-muted">{ticket.last_reply_by ?? "—"}</p>
                      </>
                    ) : <span className="text-muted">Awaiting reply</span>}
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 text-ink-soft">{formatDate(ticket.created_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      {handler && !done && (
                        <Button variant="ghost" size="sm" onClick={() => setResponding(ticket)}>
                          <MessageSquareReply className="size-3.5" />Reply
                        </Button>
                      )}
                      <Link to={`/support/${ticket.id}`}><Button variant="ghost" size="sm"><Eye className="size-3.5" />Open</Button></Link>
                    </div>
                  </td>
                </tr>
              );
            })}
          </Table>
        )}
      </Card>

      <SubmitComplaintModal open={submitting} onClose={() => setSubmitting(false)} />
      <RespondModal ticket={responding} onClose={() => setResponding(null)} />
    </>
  );
}

/** A User raises a complaint: about one of their policies, or a general procedure. */
function SubmitComplaintModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { me } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [policyId, setPolicyId] = useState("");
  const [subject, setSubject] = useState("");
  const [details, setDetails] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) { setPolicyId(""); setSubject(""); setDetails(""); setError(null); }
  }, [open]);
  // A User's policies are the ones assigned to them; nothing else can be chosen (the API checks it).
  const policies = useQuery({
    queryKey: ["policies", "assignable"],
    queryFn: () => api.get<Page<Policy>>(`/policies${qs({ status: "active", limit: 200 })}`),
    enabled: open,
  }).data?.items;
  const ready = subject.trim().length >= 3 && details.trim().length >= 5;
  const manager = me?.branch_name ? `the ${me.branch_name} Branch Manager` : "your Branch Manager";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await api.post("/tickets", {
        kind: "complaint", subject: subject.trim(), description: details.trim(), priority: "medium",
        policy_id: policyId || null, category: policyId ? null : GENERAL,
      });
      void queryClient.invalidateQueries({ queryKey: ["tickets"] });
      toast("ok", `Sent to ${manager}. You'll be notified when they reply.`);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not submit the complaint.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} icon={<MessageSquareWarning className="size-5" />} title="Submit new complaint"
      description={`Forward your query directly to ${manager}.`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="complaint-form" loading={busy} disabled={!ready}><Send className="size-4" />Submit complaint</Button></>}>
      <form id="complaint-form" onSubmit={submit} className="space-y-4">
        <Field label="Related policy / category">
          <Select value={policyId} onChange={(e) => setPolicyId(e.target.value)}>
            <option value="">{GENERAL}</option>
            {policies?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        </Field>
        <Field label="Subject / question title *">
          <Input required autoFocus maxLength={300} placeholder="e.g. Dual authorization for commercial wires"
            value={subject} onChange={(e) => setSubject(e.target.value)} />
        </Field>
        <Field label="Question / complaint details *">
          <Textarea required rows={5} maxLength={10000}
            placeholder="Explain the specific issue, procedure ambiguity, or question in detail…"
            value={details} onChange={(e) => setDetails(e.target.value)} />
        </Field>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}

/** A Branch Manager answers a complaint: a reply keeps it open, Resolve answers and resolves it. */
function RespondModal({ ticket, onClose }: { ticket: Ticket | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<"reply" | "resolve" | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (ticket) { setText(""); setError(null); } }, [ticket]);

  async function send(resolve: boolean) {
    if (!ticket) return;
    setBusy(resolve ? "resolve" : "reply");
    setError(null);
    try {
      if (resolve) await api.post(`/tickets/${ticket.id}/status`, { status: "resolved", note: text.trim() || null });
      else await api.post(`/tickets/${ticket.id}/messages`, { body: text.trim() });
      void queryClient.invalidateQueries({ queryKey: ["tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["ticket", ticket.id] });
      toast("ok", resolve ? `Resolved. ${ticket.created_by_name ?? "The user"} has been notified.`
        : `Reply sent to ${ticket.created_by_name ?? "the user"}.`);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal open={Boolean(ticket)} onClose={onClose} wide icon={<MessageSquareReply className="size-5" />}
      title={ticket?.subject ?? ""}
      description={ticket ? `${ticket.created_by_name ?? "A user"} · ${ticket.policy_name ?? ticket.category ?? GENERAL}` : undefined}
      footer={<>
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="secondary" disabled={!text.trim() || busy !== null} loading={busy === "reply"} onClick={() => send(false)}>
          <Send className="size-4" />Send reply
        </Button>
        <Button disabled={busy !== null} loading={busy === "resolve"} onClick={() => send(true)}>
          <CheckCircle2 className="size-4" />{text.trim() ? "Reply & resolve" : "Resolve"}
        </Button>
      </>}>
      <div className="space-y-4">
        <div className="rounded-md bg-subtle px-3 py-2.5 text-sm">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">Question</p>
          <p className="whitespace-pre-wrap text-ink">{ticket?.description}</p>
        </div>
        <Field label="Your reply" hint="Shown to the user. With Resolve, it is the resolution; they can reopen it if it doesn't answer them.">
          <Textarea rows={5} autoFocus maxLength={10000}
            placeholder="Give procedural guidance, reference the relevant section of the policy, or the resolution…"
            value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
        {ticket && <p className="text-xs text-muted">The full conversation is on the <Link to={`/support/${ticket.id}`} className="text-brand-700 hover:underline">complaint's page</Link>.</p>}
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </div>
    </Modal>
  );
}
