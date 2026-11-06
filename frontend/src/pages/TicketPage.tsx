import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowUpCircle, Download, Eye, Forward, Lock, Paperclip, Send, UserCheck } from "lucide-react";
import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api, ApiError } from "../api/client";
import type {
  DestinationKey, ForwardResult, QueueAssignee, TicketDetail, TicketPriority, TicketStatus, TimelineEntry,
} from "../api/types";
import {
  ACTIVE, PRIORITIES, PRIORITY_TONE, RoutingPanel, SlaBadge, statusLabel, TEAM_LABEL, TicketStatusBadge,
} from "../components/support";
import { Badge, Button, Card, CardHeader, ErrorState, Field, KeyValue, Modal, Select, SkeletonRows, Textarea } from "../components/ui";
import { cn, formatBytes, formatDateTime } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

const ROLE: Record<string, string> = {
  department_user: "User", branch_manager: "Branch Manager", org_admin: "Organization Admin",
  master_admin: "Master Admin", support_agent: "Support Agent",
};

export function TicketPage() {
  const { id } = useParams();
  const { me } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [reply, setReply] = useState("");
  const [internal, setInternal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [forwarding, setForwarding] = useState<DestinationKey | "escalate" | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const { data: ticket, error, isLoading, refetch } = useQuery({
    queryKey: ["support", "ticket", id],
    queryFn: () => api.get<TicketDetail>(`/support/tickets/${id}`),
    refetchInterval: 20_000,
  });

  async function run(action: () => Promise<TicketDetail | undefined>, done?: string) {
    setBusy(true);
    try {
      const updated = await action();
      if (updated) queryClient.setQueryData(["support", "ticket", id], updated);
      void queryClient.invalidateQueries({ queryKey: ["support", "tickets"] });
      void queryClient.invalidateQueries({ queryKey: ["support", "dashboard"] });
      if (done) toast("ok", done);
      return true;
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Something went wrong.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function download(attachmentId: string, filename: string) {
    try {
      const { url } = await api.blobUrl(`/support/tickets/${id}/attachments/${attachmentId}`);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch {
      toast("bad", "Could not download the attachment.");
    }
  }

  if (error) return <ErrorState error={error} onRetry={refetch} />;
  if (isLoading || !ticket) return <Card><SkeletonRows rows={6} /></Card>;
  const a = ticket.actions;
  const mine = ticket.created_by_id === me?.id;
  const staffSide = ticket.can_handle;
  const active = ACTIVE.includes(ticket.status);
  const platform = me?.role === "master_admin" || me?.role === "support_agent";
  // Who sent it here, shown to the team now handling it (not to the person who forwarded it).
  const forwardedIn = staffSide && ticket.forwarded && ticket.forwarded.by_id !== me?.id ? ticket.forwarded : null;

  return (
    <>
      <nav className="mb-3 text-sm text-muted">
        <Link to="/support" className="flex items-center gap-1 hover:text-ink"><ArrowLeft className="size-4" />Support Center</Link>
      </nav>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-mono text-xs text-muted">{ticket.reference}</p>
          <h1 className="break-words text-lg font-semibold tracking-tight sm:text-xl">{ticket.subject}</h1>
          <p className="mt-1 text-sm text-muted">
            {ticket.category ?? "Support"}
            {ticket.policy_id && ticket.policy_name && !platform
              ? <> · <Link to={`/policies/${ticket.policy_id}`} className="text-brand-700 hover:underline">{ticket.policy_name}</Link></> : null}
            {" "}· raised {formatDateTime(ticket.created_at)} by {ticket.created_by_name ?? "—"}
            {ticket.created_by_role ? ` (${ROLE[ticket.created_by_role] ?? ticket.created_by_role}` : ""}
            {ticket.branch_name ? `, ${ticket.branch_name}` : ""}{ticket.created_by_role ? ")" : ""}
            {platform && ticket.organization_name ? ` · ${ticket.organization_name}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <SlaBadge dueAt={ticket.due_at} overdue={ticket.overdue} active={active} />
          {ticket.read_only && <Badge tone="neutral"><Eye className="size-3" />Read only</Badge>}
          <Badge tone={PRIORITY_TONE[ticket.priority]} className="capitalize">{ticket.priority} priority</Badge>
          <TicketStatusBadge status={ticket.status} staffSide={staffSide} />
        </div>
      </div>

      {a.confirm && ticket.routing && (
        <Card className="mb-5 p-4 sm:p-5">
          <p className="mb-3 text-sm text-muted">This ticket has not been sent yet. Confirm where it should go.</p>
          <RoutingPanel ticket={ticket} onDone={(sent) => queryClient.setQueryData(["support", "ticket", id], sent)} />
        </Card>
      )}

      {ticket.read_only && (
        <Card className="mb-5 flex items-start gap-3 p-4 sm:p-5">
          <Eye className="mt-0.5 size-4 shrink-0 text-muted" />
          <p className="text-sm text-ink-soft">
            You forwarded this ticket. It is now with <span className="font-medium text-ink">{ticket.queue_name}</span>
            {ticket.assigned_to_name ? <> ({ticket.assigned_to_name})</> : null}. You can follow its progress here, but only that team
            replies or acts on it now.
          </p>
        </Card>
      )}

      {forwardedIn && (
        <Card className="mb-5 border-brand-500/30 bg-brand-50 p-4 sm:p-5">
          <p className="flex flex-wrap items-center gap-1.5 text-sm">
            {forwardedIn.escalation ? <ArrowUpCircle className="size-4 text-brand-700" /> : <Forward className="size-4 text-brand-700" />}
            <span>
              {forwardedIn.escalation ? "Escalated" : "Forwarded"} to {ticket.assigned_to_id === me?.id ? "you" : "this queue"} by{" "}
              <span className="font-medium">{forwardedIn.by_name ?? "a former staff member"}</span>
              {forwardedIn.by_role ? ` (${ROLE[forwardedIn.by_role] ?? forwardedIn.by_role})` : ""}
              {forwardedIn.from_queue ? <> from <span className="font-medium">{forwardedIn.from_queue}</span></> : null}
              {" "}· {formatDateTime(forwardedIn.at)}
            </span>
          </p>
          {forwardedIn.reason && <p className="mt-2 whitespace-pre-wrap text-sm italic text-ink-soft">“{forwardedIn.reason}”</p>}
          <p className="mt-2 text-xs text-muted">
            Your reply goes to {ticket.created_by_name ?? "the requester"}.
            {forwardedIn.by_id !== ticket.created_by_id && ` ${forwardedIn.by_name ?? "The person who forwarded it"} follows the ticket and sees your reply, but no longer replies on it.`}
          </p>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-4">
          <Card className="p-4 sm:p-5"><p className="whitespace-pre-wrap text-sm leading-relaxed">{ticket.description}</p></Card>
          {ticket.resolution_note && (
            <Card className="border-ok-600/30 bg-ok-50 p-4 sm:p-5">
              <p className="text-xs font-medium uppercase tracking-wide text-ok-600">Resolution</p>
              <p className="mt-1 whitespace-pre-wrap text-sm">{ticket.resolution_note}</p>
            </Card>
          )}
          <Card>
            <CardHeader title="Activity" subtitle="Replies, internal notes you can see, and every move" />
            <ol className="space-y-3 p-4 sm:p-5">
              {ticket.timeline.map((entry) => <Entry key={entry.id} entry={entry} me={me?.id} staffSide={staffSide} />)}
              {!ticket.timeline.length && <li className="text-center text-sm text-muted">Nothing yet.</li>}
            </ol>
            {a.reply ? (
              <div className={cn("border-t border-line p-4", internal && "bg-warn-50/60")}>
                <Textarea rows={3} value={reply} onChange={(e) => setReply(e.target.value)} aria-label="Message"
                  placeholder={internal ? "Internal note: only the handling team sees it…" : "Write a reply…"} />
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                  {a.note ? (
                    <label className="flex items-center gap-2 text-sm text-ink-soft">
                      <input type="checkbox" checked={internal} onChange={(e) => setInternal(e.target.checked)} />
                      <Lock className="size-3.5" />Internal note
                    </label>
                  ) : <span />}
                  <Button disabled={!reply.trim()} loading={busy} onClick={async () => {
                    if (await run(() => api.post(`/support/tickets/${id}/messages`, { body: reply.trim(), internal }),
                      internal ? "Note added." : undefined)) setReply("");
                  }}><Send className="size-4" />{internal ? "Add note" : "Send"}</Button>
                </div>
              </div>
            ) : !a.confirm && <p className="border-t border-line px-4 py-3 text-sm text-muted sm:px-5">
              {ticket.read_only && ticket.status !== "closed" && ticket.status !== "cancelled" ? `Read only: you forwarded this ticket to ${ticket.queue_name}.`
                : ticket.status === "cancelled" ? "This ticket was cancelled."
                : ticket.status === "resolved" ? "This ticket is resolved." : "This ticket is closed."}
              {mine && a.statuses.includes("open") && " Reopen it to reply."}
              {!mine && ticket.status === "resolved" && a.statuses.includes("in_progress") && " Move it back to In progress to reply."}</p>}
          </Card>
        </div>

        <aside className="space-y-4">
          <Card className="p-4">
            <KeyValue items={[
              ["With", ticket.status === "awaiting_confirmation" ? "Not sent yet" : ticket.queue_name],
              ["Team", TEAM_LABEL[ticket.team]],
              ["Assigned to", ticket.assigned_to_name ?? (ticket.status === "awaiting_confirmation" ? "—" : "Unassigned")],
              ["Resolution due", ticket.due_at ? formatDateTime(ticket.due_at) : "—"],
              ["First reply", ticket.first_response_at ? formatDateTime(ticket.first_response_at)
                : ticket.response_due_at && active ? `due ${formatDateTime(ticket.response_due_at)}` : "—"],
              ["Reopened", ticket.reopen_count ? `${ticket.reopen_count}×` : "—"],
            ]} />
            {a.take && (
              <Button size="sm" variant="secondary" className="mt-4 w-full" loading={busy}
                onClick={() => run(() => api.post(`/support/tickets/${id}/assign`, { user_id: me?.id }), "Assigned to you.")}>
                <UserCheck className="size-4" />Take this ticket</Button>
            )}
          </Card>

          {staffSide && <StaffActions ticket={ticket} busy={busy} run={run} onForward={setForwarding} />}

          {mine && !staffSide && (a.statuses.length > 0 || a.cancel || a.escalate) && (
            <Card className="space-y-2 p-4">
              {a.statuses.includes("open") && <Button variant="secondary" className="w-full" loading={busy}
                onClick={() => run(() => api.post(`/support/tickets/${id}/status`, { status: "open" }), "Ticket reopened.")}>Reopen</Button>}
              {a.escalate && <Button variant="secondary" className="w-full" onClick={() => setForwarding("escalate")}>
                <ArrowUpCircle className="size-4" />Escalate (overdue)</Button>}
              {a.cancel && !a.confirm && <Button variant="ghost" className="w-full" loading={busy}
                onClick={() => run(() => api.post(`/support/tickets/${id}/cancel`, {}), "Ticket withdrawn.")}>Withdraw ticket</Button>}
            </Card>
          )}

          <Card className="p-4">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-sm font-semibold">Attachments</p>
              {a.attach && <Button size="sm" variant="ghost" onClick={() => file.current?.click()}><Paperclip className="size-3.5" />Attach</Button>}
              <input ref={file} type="file" className="hidden" accept=".pdf,.png,.jpg,.jpeg,.txt,.csv,.docx,.xlsx"
                onChange={(e) => {
                  const chosen = e.target.files?.[0];
                  e.target.value = "";
                  if (!chosen) return;
                  const form = new FormData();
                  form.append("file", chosen);
                  void run(() => api.upload(`/support/tickets/${id}/attachments`, form), "Attached.");
                }} />
            </div>
            {ticket.attachments.length ? (
              <ul className="space-y-1.5 text-sm">
                {ticket.attachments.map((f) => (
                  <li key={f.id}>
                    <button type="button" onClick={() => download(f.id, f.filename)} className="flex w-full items-center gap-2 rounded px-1 py-1 text-left hover:bg-subtle">
                      <Download className="size-3.5 text-muted" /><span className="min-w-0 flex-1 truncate">{f.filename}</span><span className="text-xs text-muted">{formatBytes(f.size_bytes)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : <p className="text-xs text-muted">PDF, images, text, Word or Excel, up to 10 MB.</p>}
          </Card>
        </aside>
      </div>

      <ForwardModal ticket={ticket} to={forwarding} onClose={() => setForwarding(null)} onDone={(result) => {
        setForwarding(null);
        void queryClient.invalidateQueries({ queryKey: ["support"] });
        toast("ok", `${result.reference} is now with ${result.queue_name}${result.assigned_to_name ? ` (${result.assigned_to_name})` : ""}.`);
        // You keep following it, read-only, under "Forwarded by me".
        if (result.ticket) queryClient.setQueryData(["support", "ticket", id], result.ticket);
        else navigate("/support");
      }} />
    </>
  );
}

function Entry({ entry: e, me, staffSide }: { entry: TimelineEntry; me?: string; staffSide: boolean }) {
  const who = e.author_name ?? "Governix";
  const when = formatDateTime(e.created_at);
  if (e.kind === "reply" || e.kind === "note") {
    const note = e.kind === "note";
    return (
      <li className={cn("max-w-[88%] rounded-lg px-4 py-2.5 text-sm",
        note ? "border border-warn-600/30 bg-warn-50" : e.author_id === me ? "ml-auto bg-brand-50" : "bg-subtle")}>
        <p className="mb-1 flex flex-wrap items-center gap-1 text-xs text-muted">
          {note && <><Lock className="size-3" /><span className="font-medium text-warn-600">Internal note</span> ·</>}
          {who}{e.author_role ? ` · ${ROLE[e.author_role] ?? e.author_role}` : ""} · {when}
        </p>
        <p className="whitespace-pre-wrap">{e.body}</p>
      </li>
    );
  }
  let text: string;
  const d = e.data ?? {};
  if (e.kind === "forward") {
    const to = d.to?.queue ?? "another team";
    if (d.initial) text = `${who} sent it to ${to}${d.switched && d.recommended ? ` (chose this over the recommended ${TEAM_LABEL[d.recommended as "technical" | "branch"]})` : ""}`;
    else text = `${who} ${d.escalation ? "escalated" : "forwarded"} it from ${d.from?.queue ?? "?"} to ${to}`;
  } else if (e.kind === "assign") {
    if (d.unassigned) text = "No one was available: waiting in the queue, its managers were told";
    else if (!d.to) text = `${who} unassigned it`;
    else if (d.auto) text = `Assigned to ${d.to_name ?? "a staff member"} by workload${staffSide && d.load ? ` (${d.load.open_tickets}/${d.load.capacity} open)` : ""}`;
    else text = d.to === e.author_id ? `${who} took it` : `${who} assigned it to ${d.to_name ?? "a staff member"}`;
  } else if (d.priority) {
    text = `${who} changed the priority from ${d.priority.from} to ${d.priority.to}`;
  } else if (e.status_change) {
    text = d.auto ? `Closed automatically` : `${who} set the status to ${statusLabel(e.status_change, staffSide)[0].toLowerCase()}`;
  } else text = who;
  return (
    <li className="text-center text-xs text-muted">
      <p>{text} · {when}</p>
      {e.body && e.kind !== "status" && <p className="mx-auto mt-0.5 max-w-md italic text-ink-soft">“{e.body}”</p>}
      {e.body && e.kind === "status" && <p className="mx-auto mt-0.5 max-w-md text-ink-soft">{e.body}</p>}
    </li>
  );
}

function StaffActions({ ticket, busy, run, onForward }: {
  ticket: TicketDetail; busy: boolean; run: (action: () => Promise<TicketDetail | undefined>, done?: string) => Promise<boolean>;
  onForward: (to: DestinationKey | "escalate") => void;
}) {
  const a = ticket.actions;
  const [status, setStatus] = useState<TicketStatus | "">("");
  const [note, setNote] = useState("");
  const [cancelNote, setCancelNote] = useState("");
  const { data: people } = useQuery({
    queryKey: ["support", "assignees", ticket.id, ticket.level],
    queryFn: () => api.get<QueueAssignee[]>(`/support/tickets/${ticket.id}/assignees`),
    enabled: a.assign,
  });
  const statuses = a.statuses.filter((s) => s !== "closed" && s !== "open");
  const resolving = status === "resolved";
  return (
    <>
      {statuses.length > 0 && (
        <Card className="space-y-3 p-4">
          <p className="text-sm font-semibold">Update status</p>
          <Select value={status} onChange={(e) => setStatus(e.target.value as TicketStatus)} aria-label="New status">
            <option value="">Choose…</option>
            {statuses.map((s) => <option key={s} value={s}>{statusLabel(s, true)[0]}</option>)}
          </Select>
          <Field label={resolving ? "Resolution note *" : "Note (optional)"} hint={resolving ? "Shown to the requester." : undefined}>
            <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
          <Button className="w-full" disabled={!status || (resolving && note.trim().length < 3)} loading={busy} onClick={async () => {
            if (await run(() => api.post(`/support/tickets/${ticket.id}/status`, { status, note: note.trim() || null }), "Status updated.")) {
              setStatus(""); setNote("");
            }
          }}>Update</Button>
        </Card>
      )}

      {(a.priority || a.assign) && (
        <Card className="space-y-3 p-4">
          {a.priority && (
            <Field label="Priority" hint="The SLA due times follow it.">
              <Select value={ticket.priority} disabled={busy} onChange={(e) => run(() => api.post(`/support/tickets/${ticket.id}/priority`,
                { priority: e.target.value as TicketPriority }), "Priority changed.")}>
                {PRIORITIES.map((p) => <option key={p} value={p}>{p[0].toUpperCase() + p.slice(1)}</option>)}
              </Select>
            </Field>
          )}
          {a.assign && (
            <Field label="Assign to" hint="People on duty in this queue, with their open tickets.">
              <Select value={ticket.assigned_to_id ?? ""} disabled={busy || !people}
                onChange={(e) => run(() => api.post(`/support/tickets/${ticket.id}/assign`, { user_id: e.target.value || null }), "Assignment changed.")}>
                <option value="">Unassigned</option>
                {people?.map((p) => <option key={p.user_id} value={p.user_id}>
                  {p.name} · {p.open_tickets}/{p.capacity}{p.availability !== "available" ? ` · ${p.availability}` : ""}</option>)}
              </Select>
            </Field>
          )}
        </Card>
      )}

      {(a.forward.length > 0 || a.escalate) && (
        <Card className="space-y-2 p-4">
          <p className="text-sm font-semibold">Forward</p>
          {a.forward.map((d) => (
            <Button key={d.key} variant="secondary" size="sm" className="w-full justify-start" onClick={() => onForward(d.key)}>
              <Forward className="size-4" />To {d.label}</Button>
          ))}
          {a.escalate && a.escalate_to && <Button variant="secondary" size="sm" className="w-full justify-start" onClick={() => onForward("escalate")}>
            <ArrowUpCircle className="size-4" />Escalate to {a.escalate_to}</Button>}
        </Card>
      )}

      {a.cancel && a.assign && (
        <Card className="space-y-2 p-4">
          <Field label="Cancel ticket" hint="For duplicates or requests that need nothing. The requester sees the reason.">
            <Textarea rows={2} value={cancelNote} onChange={(e) => setCancelNote(e.target.value)} placeholder="Reason" />
          </Field>
          <Button variant="danger" size="sm" className="w-full" disabled={!cancelNote.trim()} loading={busy}
            onClick={() => run(() => api.post(`/support/tickets/${ticket.id}/cancel`, { note: cancelNote.trim() }), "Ticket cancelled.")}>Cancel ticket</Button>
        </Card>
      )}
    </>
  );
}

/** Forwarding needs a reason and an explicit confirmation; the person may also pick who gets it. */
function ForwardModal({ ticket, to, onClose, onDone }: {
  ticket: TicketDetail; to: DestinationKey | "escalate" | null; onClose: () => void; onDone: (result: ForwardResult) => void;
}) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [userId, setUserId] = useState("");
  const [busy, setBusy] = useState(false);
  const destination = to === "escalate" ? null : ticket.actions.forward.find((d) => d.key === to);
  const label = to === "escalate" ? ticket.actions.escalate_to ?? "the next level" : destination?.label ?? "";
  const { data: people } = useQuery({
    queryKey: ["support", "assignees", ticket.id, to],
    queryFn: () => api.get<QueueAssignee[]>(`/support/tickets/${ticket.id}/assignees?to=${to}`),
    enabled: Boolean(to && to !== "escalate" && ticket.can_handle),
  });
  function close() {
    setReason(""); setConfirmed(false); setUserId("");
    onClose();
  }
  async function submit() {
    setBusy(true);
    try {
      const result = await api.post<ForwardResult>(`/support/tickets/${ticket.id}/forward`, {
        to, reason: reason.trim(), confirmed, user_id: userId || null,
      });
      setReason(""); setConfirmed(false); setUserId("");
      onDone(result);
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not forward the ticket.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open={to !== null} onClose={close} icon={to === "escalate" ? <ArrowUpCircle className="size-5" /> : <Forward className="size-5" />}
      title={to === "escalate" ? `Escalate ${ticket.reference}` : `Forward ${ticket.reference}`}
      description={`From ${ticket.queue_name} to ${label}. The history and SLA stay with it.`}
      footer={<>
        <Button variant="secondary" onClick={close}>Cancel</Button>
        <Button disabled={!confirmed || reason.trim().length < 3} loading={busy} onClick={submit}>
          {to === "escalate" ? <ArrowUpCircle className="size-4" /> : <Forward className="size-4" />}{to === "escalate" ? "Escalate" : "Forward"}</Button>
      </>}>
      <div className="space-y-4">
        <Field label="Reason *" hint="Recorded on the ticket and sent to the requester and the new team.">
          <Textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. A software error, not a branch procedure" />
        </Field>
        {people && people.length > 0 && (
          <Field label="Assign to" hint="Leave it to the workload rule, or choose someone on duty there.">
            <Select value={userId} onChange={(e) => setUserId(e.target.value)}>
              <option value="">By workload</option>
              {people.map((p) => <option key={p.user_id} value={p.user_id}>{p.name} · {p.open_tickets}/{p.capacity}
                {p.availability !== "available" ? ` · ${p.availability}` : ""}</option>)}
            </Select>
          </Field>
        )}
        {people && people.length === 0 && <p className="text-xs text-muted">No one is on duty there yet: it will wait unassigned and its managers are told.</p>}
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-0.5" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
          <span>I confirm this ticket goes to <span className="font-medium">{label}</span>.</span>
        </label>
      </div>
    </Modal>
  );
}
