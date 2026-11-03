import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bot, CheckCircle2, Clock, LifeBuoy, Paperclip, Send, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { api, ApiError } from "../api/client";
import type {
  Routing, StaffAvailability, SupportCategory, TicketDetail, TicketPriority, TicketStatus, TicketTeam,
} from "../api/types";
import { cn, formatDateTime } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";
import { Badge, Button, Field, Input, Modal, Select, Textarea } from "./ui";

type Tone = "info" | "warn" | "ok" | "neutral" | "bad" | "brand";

export const TICKET_STATUS: Record<TicketStatus, [string, Tone]> = {
  awaiting_confirmation: ["Awaiting confirmation", "warn"],
  open: ["Open", "info"],
  in_progress: ["In progress", "info"],
  waiting_for_user: ["Waiting for you", "warn"],
  waiting_for_team: ["Waiting for another team", "neutral"],
  resolved: ["Resolved", "ok"],
  closed: ["Closed", "neutral"],
  cancelled: ["Cancelled", "neutral"],
};
/** The same status in the words of the support side, where "you" is not the person waiting. */
export function statusLabel(status: TicketStatus, staffSide: boolean): [string, Tone] {
  if (staffSide && status === "waiting_for_user") return ["Waiting for the requester", "warn"];
  return TICKET_STATUS[status];
}
export const PRIORITY_TONE: Record<TicketPriority, Tone> = { low: "neutral", medium: "info", high: "warn", urgent: "bad" };
export const PRIORITIES: TicketPriority[] = ["low", "medium", "high", "urgent"];
export const TEAM_LABEL: Record<TicketTeam, string> = { technical: "Technical Support", branch: "Branch Support" };
export const TEAM_HINT: Record<TicketTeam, string> = {
  technical: "Errors, sign-in and access problems, uploads or pages that fail.",
  branch: "Branch procedures and operations, and documents that are missing or wrong.",
};
export const AVAILABILITY: Record<StaffAvailability, [string, Tone]> = {
  available: ["Available", "ok"], busy: ["Busy", "warn"], away: ["Away", "neutral"], offline: ["Offline", "neutral"],
};
export const RAISERS = new Set(["department_user", "branch_manager", "org_admin"]);

export function TicketStatusBadge({ status, staffSide = false }: { status: TicketStatus; staffSide?: boolean }) {
  const [label, tone] = statusLabel(status, staffSide);
  return <Badge tone={tone}>{label}</Badge>;
}

/** Due time of an open ticket: overdue in red, due within a day in amber. */
export function SlaBadge({ dueAt, overdue, active }: { dueAt: string | null; overdue: boolean; active: boolean }) {
  if (!dueAt || !active) return null;
  const hours = (new Date(dueAt).getTime() - Date.now()) / 3_600_000;
  if (overdue) return <Badge tone="bad"><AlertTriangle className="size-3" />Overdue</Badge>;
  return (
    <Badge tone={hours < 24 ? "warn" : "neutral"}>
      <Clock className="size-3" />Due {hours < 24 ? `in ${Math.max(1, Math.round(hours))}h` : formatDateTime(dueAt)}
    </Badge>
  );
}

export const ACTIVE: TicketStatus[] = ["open", "in_progress", "waiting_for_user", "waiting_for_team"];

/**
 * The AI's routing recommendation and the creator's explicit choice. Nothing is sent until they press
 * Confirm; they may switch the team first. With low confidence (or no AI) no team is preselected.
 */
export function RoutingPanel({ ticket, onDone }: { ticket: TicketDetail; onDone: (t: TicketDetail) => void }) {
  const routing = ticket.routing as Routing;
  const { can } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // The recommendation is preselected (even an uncertain one); the creator may switch, and nothing is sent
  // until they press Confirm. Only with no recommendation at all must they pick.
  const [team, setTeam] = useState<TicketTeam | null>(routing.team);
  const [busy, setBusy] = useState<"confirm" | "cancel" | null>(null);

  async function confirm() {
    if (!team) return;
    setBusy("confirm");
    try {
      const sent = await api.post<TicketDetail>(`/support/tickets/${ticket.id}/confirm`, { team });
      void queryClient.invalidateQueries({ queryKey: ["support"] });
      toast("ok", `${sent.reference} sent to ${sent.queue_name}.`);
      onDone(sent);
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not send the ticket.");
    } finally {
      setBusy(null);
    }
  }

  async function cancel() {
    setBusy("cancel");
    try {
      const cancelled = await api.post<TicketDetail>(`/support/tickets/${ticket.id}/cancel`, {});
      void queryClient.invalidateQueries({ queryKey: ["support"] });
      onDone(cancelled);
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not cancel the ticket.");
    } finally {
      setBusy(null);
    }
  }

  const sourceLabel = routing.source === "ai" ? "AI recommendation"
    : routing.source === "text" ? "From your title and description"
    : routing.source === "category" ? "From the category" : null;
  return (
    <div className="space-y-4">
      {routing.redirect && (
        <div className="rounded-lg border border-brand-500/30 bg-brand-50 p-3 text-sm">
          <p className="flex items-center gap-2 font-medium text-brand-700"><Bot className="size-4" />This looks like a question about what a policy says</p>
          <p className="mt-1 text-ink-soft">The AI Assistant answers these straight from your documents, with citations. A ticket is for a person to act on, for example a document that is missing or wrong.</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {can("ai:query") ? (
              <Button size="sm" onClick={() => navigate("/assistant", { state: { question: `${ticket.subject}. ${ticket.description}` } })}>
                <Sparkles className="size-4" />Ask the AI Assistant
              </Button>
            ) : can("policies:read") && <Button size="sm" onClick={() => navigate("/policies")}>Browse policies</Button>}
            <Button size="sm" variant="secondary" loading={busy === "cancel"} onClick={cancel}><X className="size-4" />Cancel this ticket</Button>
          </div>
        </div>
      )}

      <div>
        <p className="text-sm font-semibold">{routing.redirect ? "Still need a person? Choose who should take it" : "Where should this go?"}</p>
        {!routing.team ? (
          <p className="mt-0.5 flex items-center gap-1.5 text-xs text-warn-600"><AlertTriangle className="size-3.5" />
            No recommendation for this one. Choose the team that should take it.</p>
        ) : routing.needs_choice ? (
          <p className="mt-0.5 flex items-center gap-1.5 text-xs text-warn-600"><AlertTriangle className="size-3.5" />
            Suggested below, but not certain. Check it, or switch, before sending.</p>
        ) : <p className="mt-0.5 text-xs text-muted">Suggested below. You can switch before sending.</p>}
      </div>

      <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Destination team">
        {(["technical", "branch"] as TicketTeam[]).map((option) => {
          const recommended = routing.team === option;
          const chosen = team === option;
          return (
            <button key={option} type="button" role="radio" aria-checked={chosen} onClick={() => setTeam(option)}
              className={cn("rounded-lg border p-3 text-left transition-colors",
                chosen ? "border-brand-500 bg-brand-50 ring-2 ring-brand-100" : "border-line hover:border-brand-500/50")}>
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold">{routing.options[option]?.queue ?? TEAM_LABEL[option]}</span>
                {recommended && <Badge tone="brand"><Sparkles className="size-3" />Recommended</Badge>}
              </span>
              <span className="mt-1 block text-xs text-muted">{TEAM_HINT[option]}</span>
            </button>
          );
        })}
      </div>

      {routing.team && (
        <div className="rounded-md bg-subtle px-3 py-2 text-xs text-ink-soft">
          <p><span className="font-medium">{sourceLabel}</span>
            {routing.source === "ai" && <> · {Math.round(routing.confidence * 100)}% sure</>}: {routing.reason}</p>
          <p className="mt-1">
            Priority: <span className="font-medium capitalize">{ticket.priority}</span>
            {!routing.priority_chosen && routing.source === "ai" ? " (suggested)" : ""}
            {routing.issue_type ? <> · Issue: {routing.issue_type.replace(/_/g, " ")}</> : null}
            {routing.suggested_assignee && team === routing.team ? <> · Likely handled by {routing.suggested_assignee.name}</> : null}
          </p>
        </div>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        {!routing.redirect && <Button variant="ghost" loading={busy === "cancel"} onClick={cancel}>Cancel ticket</Button>}
        <Button disabled={!team} loading={busy === "confirm"} onClick={confirm}>
          <Send className="size-4" />{team ? `Confirm and send to ${routing.options[team]?.queue ?? TEAM_LABEL[team]}` : "Choose a team"}
        </Button>
      </div>
    </div>
  );
}

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.txt,.csv,.docx,.xlsx";
const MAX_BYTES = 10 * 1024 * 1024;

/** Write the ticket, read the AI's recommendation, then confirm or switch where it goes. */
export function NewTicketModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const toast = useToast();
  const [categoryId, setCategoryId] = useState("");
  const [subject, setSubject] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState<TicketPriority | "">("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<TicketDetail | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const { data: categories } = useQuery({
    queryKey: ["support", "categories"], queryFn: () => api.get<SupportCategory[]>("/support/categories"), enabled: open,
  });
  useEffect(() => {
    if (!open) return;
    setSubject(""); setDescription(""); setPriority(""); setFiles([]); setError(null); setDraft(null); setCategoryId("");
  }, [open]);
  useEffect(() => {
    if (open && !categoryId && categories?.length) setCategoryId(categories[0].id);
  }, [open, categories, categoryId]);

  const category = categories?.find((c) => c.id === categoryId);
  const invalid = !categoryId || subject.trim().length < 3 || description.trim().length < 10;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (invalid) return;
    setBusy(true);
    setError(null);
    try {
      let ticket = await api.post<TicketDetail>("/support/tickets", {
        category_id: categoryId, subject: subject.trim(), description: description.trim(), priority: priority || null,
      });
      for (const file of files) {
        const form = new FormData();
        form.append("file", file);
        try {
          ticket = await api.upload<TicketDetail>(`/support/tickets/${ticket.id}/attachments`, form);
        } catch (err) {
          toast("bad", `${file.name}: ${err instanceof ApiError ? err.message : "not attached"}`);
        }
      }
      setDraft(ticket);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the ticket.");
    } finally {
      setBusy(false);
    }
  }

  function addFiles(chosen: FileList | null) {
    const list = Array.from(chosen ?? []);
    const tooBig = list.filter((f) => f.size > MAX_BYTES);
    if (tooBig.length) toast("bad", `Over 10 MB: ${tooBig.map((f) => f.name).join(", ")}`);
    setFiles((current) => [...current, ...list.filter((f) => f.size <= MAX_BYTES)].slice(0, 5));
  }

  return (
    <Modal open={open} onClose={onClose} wide icon={<LifeBuoy className="size-5" />}
      title={draft ? `${draft.reference}: confirm where it goes` : "New support ticket"}
      description={draft ? draft.subject : "Describe the problem. We recommend the right team, and you confirm before it is sent."}
      footer={!draft && <>
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="new-ticket" disabled={invalid} loading={busy}><Sparkles className="size-4" />Continue</Button>
      </>}>
      {draft ? (
        draft.status === "awaiting_confirmation" && draft.routing ? (
          <RoutingPanel ticket={draft} onDone={(sent) => {
            onClose();
            if (sent.status === "cancelled") toast("ok", `${sent.reference} cancelled.`);
            else navigate(`/support/${sent.id}`);
          }} />
        ) : <p className="flex items-center gap-2 text-sm"><CheckCircle2 className="size-4 text-ok-600" />Sent.</p>
      ) : (
        <form id="new-ticket" onSubmit={submit} className="space-y-4">
          <Field label="Category *" hint={category?.description ?? undefined}>
            <Select value={categoryId} onChange={(e) => setCategoryId(e.target.value)} disabled={!categories}>
              {!categories && <option>Loading…</option>}
              {categories?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
          <Field label="Subject *">
            <Input value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={300}
              placeholder="e.g. Cannot sign in after the password reset" />
          </Field>
          <Field label="Details *" hint="What happened, what you expected, and any error message. At least 10 characters.">
            <Textarea rows={6} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={10000}
              placeholder="Steps, error text, which customer or screen is affected…" />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Priority" hint="Leave it to the recommendation, or set it yourself.">
              <Select value={priority} onChange={(e) => setPriority(e.target.value as TicketPriority | "")}>
                <option value="">Suggest one for me</option>
                {PRIORITIES.map((p) => <option key={p} value={p} className="capitalize">{p[0].toUpperCase() + p.slice(1)}</option>)}
              </Select>
            </Field>
            <Field label="Attachments" hint="PDF, images, text, Word or Excel, up to 10 MB each.">
              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" size="sm" variant="secondary" onClick={() => picker.current?.click()}><Paperclip className="size-3.5" />Add files</Button>
                <input ref={picker} type="file" multiple accept={ACCEPT} className="hidden"
                  onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
                {files.map((f, i) => (
                  <span key={`${f.name}-${i}`} className="inline-flex items-center gap-1 rounded bg-subtle px-2 py-0.5 text-xs">
                    {f.name}
                    <button type="button" aria-label={`Remove ${f.name}`} onClick={() => setFiles((all) => all.filter((_, j) => j !== i))}><X className="size-3" /></button>
                  </span>
                ))}
              </div>
            </Field>
          </div>
          {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
        </form>
      )}
    </Modal>
  );
}
