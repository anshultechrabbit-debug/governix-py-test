import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft, Check, CheckCircle2, Clock, Copy, FileCode2, Globe, PowerOff, RefreshCw, Rocket, Server,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router";
import { api, ApiError } from "../api/client";
import type { NginxDetail, Organization } from "../api/types";
import { NginxBadge } from "../components/NginxBadge";
import { Button, Card, CardHeader, ErrorState, PageHeader, Skeleton } from "../components/ui";
import { copyText } from "../lib/clipboard";
import { cn, formatDateTime } from "../lib/format";
import { useToast } from "../store/hooks";

interface Failure { message: string; output?: string }

/**
 * One organization's nginx server block (Organizations > Nginx): where it is served, the file,
 * the config a deploy writes, and Redeploy / Disable. A deploy is checked with `nginx -t` and
 * rolled back on any failure (app/modules/nginx/service.py).
 */
export function NginxPage() {
  const { id } = useParams();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState<"deploy" | "disable" | null>(null);
  const [confirmingDisable, setConfirmingDisable] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [copied, setCopied] = useState(false);

  const { data: org } = useQuery({
    queryKey: ["organization", id],
    queryFn: () => api.get<Organization>(`/organizations/${id}`),
  });
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["nginx", id],
    queryFn: () => api.get<NginxDetail>(`/nginx/${id}`),
  });

  async function run(action: "deploy" | "disable") {
    setBusy(action);
    setFailure(null);
    try {
      const result = await api.post<NginxDetail>(`/nginx/${id}/${action}`);
      queryClient.setQueryData(["nginx", id], result);
      void queryClient.invalidateQueries({ queryKey: ["nginx"], exact: true });
      toast("ok", action === "deploy"
        ? `${org?.name ?? "The organization"} is live on port ${result.port}.`
        : `Port ${result.port} no longer serves ${org?.name ?? "the organization"}.`);
    } catch (err) {
      const details = err instanceof ApiError ? (err.details as { output?: string } | undefined) : undefined;
      setFailure({ message: err instanceof ApiError ? err.message : "The request failed.", output: details?.output });
    } finally {
      setBusy(null);
      setConfirmingDisable(false);
    }
  }

  async function copy() {
    if (data?.config) setCopied(await copyText(data.config));
  }

  const live = data?.state === "deployed" || data?.state === "outdated";
  const address = data?.port ? `http://${window.location.hostname}:${data.port}` : null;
  const canDeploy = Boolean(data?.configured && data.config);
  const deployLabel = data?.state === "disabled" ? "Enable proxy" : data?.state === "not_deployed" ? "Deploy config" : "Redeploy config";

  return (
    <>
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            <Link to="/organizations" aria-label="Back to Organizations" title="Back to Organizations"
              className="flex size-9 items-center justify-center rounded-full border border-line text-ink-soft hover:bg-subtle hover:text-ink">
              <ArrowLeft className="size-4" />
            </Link>
            Nginx configuration
            {data && <NginxBadge state={data.state} />}
          </span>
        }
        subtitle={<>Reverse-proxy routing that serves Governix for <strong className="text-ink">{org?.name ?? "…"}</strong> on its own port.</>}
        actions={data && (
          <>
            {data.configured && live && (
              <Button variant="secondary" className="text-bad-600" disabled={busy !== null} onClick={() => setConfirmingDisable(true)}>
                <PowerOff className="size-4" />Disable proxy
              </Button>
            )}
            {canDeploy && (
              <Button loading={busy === "deploy"} disabled={busy !== null} onClick={() => run("deploy")}>
                {data.state === "not_deployed" ? <Rocket className="size-4" /> : <RefreshCw className="size-4" />}{deployLabel}
              </Button>
            )}
          </>
        )}
      />

      {error ? (
        <ErrorState error={error} onRetry={refetch} />
      ) : isLoading || !data ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-36" />)}
        </div>
      ) : (
        <div className="space-y-5">
          {confirmingDisable && (
            <Card className="flex flex-wrap items-center justify-between gap-3 border-bad-600/30 bg-bad-50 px-4 py-3">
              <p className="text-sm text-bad-600">
                Users of {org?.name} won't reach Governix on port {data.port} until you enable it again. Other organizations
                are not affected.
              </p>
              <div className="flex gap-2">
                <Button variant="secondary" onClick={() => setConfirmingDisable(false)}>Cancel</Button>
                <Button variant="danger" loading={busy === "disable"} onClick={() => run("disable")}>Disable port {data.port}</Button>
              </div>
            </Card>
          )}
          {failure && (
            <div className="rounded-md bg-bad-50 px-4 py-3 text-sm text-bad-600" role="alert">
              <p className="font-medium">{failure.message}</p>
              {failure.output && <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-xs">{failure.output}</pre>}
            </div>
          )}
          {!data.configured && (
            <Note>
              Deploying from here is off on this server, because NGINX_CONF_DIR is not set. Copy the configuration below into
              nginx yourself, or set the server up as described in deploy/README.md.
            </Note>
          )}
          {data.port === null && (
            <Note tone="warn">
              {org?.name ?? "This organization"} has no port. Set one in Organizations &gt; Edit, then deploy.
            </Note>
          )}
          {data.state === "outdated" && (
            <Note tone="warn">
              The deployed file no longer matches the organization's port or the server's nginx settings. Redeploy to apply them.
            </Note>
          )}

          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <InfoCard label="Nginx port" icon={<Server className="size-4" />}
              value={<span className="text-2xl font-semibold tracking-tight sm:text-3xl">{data.port ? `Port ${data.port}` : "No port"}</span>}
              hint={address && live
                ? <a href={address} target="_blank" rel="noreferrer" className="text-brand-700 hover:underline">{address}</a>
                : "Where this organization is served"} />
            <InfoCard label="Server name" icon={<Globe className="size-4" />}
              value={data.server_name === "_" ? "Any host name" : data.server_name}
              hint={data.server_name === "_" ? "Organizations are told apart by port" : "Virtual host"} />
            <InfoCard label="Configuration file" icon={<FileCode2 className="size-4" />}
              value={
                <span className="block truncate rounded-md border border-line bg-subtle px-2.5 py-1.5 font-mono text-xs" title={data.config_file ?? undefined}>
                  {data.config_file ?? "Not set"}
                </span>
              }
              hint={{
                deployed: "Included by nginx",
                outdated: "Included by nginx, out of date",
                disabled: "Switched off (.disabled, not included)",
                not_deployed: "Not written yet",
                unavailable: "NGINX_CONF_DIR is not set",
              }[data.state]} />
            <InfoCard label="Last deployed" icon={<Clock className="size-4" />}
              value={data.deployed_at ? formatDateTime(data.deployed_at) : "Never"}
              hint={data.state === "deployed"
                ? <span className="flex items-center gap-1 text-ok-600"><CheckCircle2 className="size-3.5" />Synced &amp; active</span>
                : data.state === "outdated" ? <span className="text-warn-600">Redeploy to apply changes</span>
                : data.state === "disabled" ? "Proxy disabled"
                : "Not live"} />
          </div>

          {data.config && (
            <Card>
              <CardHeader
                title="Configuration"
                subtitle={data.state === "outdated" ? "What a redeploy writes. It differs from the file on the server." : "The server block a deploy writes for this organization."}
                actions={
                  <Button variant="secondary" size="sm" onClick={copy}>
                    {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}{copied ? "Copied" : "Copy"}
                  </Button>
                }
              />
              <pre className="max-h-[28rem] overflow-auto p-4 font-mono text-xs leading-relaxed text-ink sm:px-5">{data.config}</pre>
            </Card>
          )}
        </div>
      )}
    </>
  );
}

function InfoCard({ label, icon, value, hint }: { label: string; icon: ReactNode; value: ReactNode; hint: ReactNode }) {
  return (
    <Card className="flex min-w-0 flex-col gap-3 p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand-600">{icon}</span>
      </div>
      <div className="min-w-0 break-words text-sm font-semibold text-ink">{value}</div>
      <div className="text-xs text-muted">{hint}</div>
    </Card>
  );
}

function Note({ tone, children }: { tone?: "warn"; children: ReactNode }) {
  return <p className={cn("rounded-md px-4 py-3 text-sm", tone === "warn" ? "bg-warn-50 text-warn-600" : "bg-info-50 text-info-600")}>{children}</p>;
}
