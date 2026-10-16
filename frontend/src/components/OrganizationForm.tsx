import { Building2, ChevronDown, ImageUp, Landmark, Network, Palette, Type } from "lucide-react";
import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { api } from "../api/client";
import type { Organization, OrganizationOptions } from "../api/types";
import { fontStack, HEX, loadGoogleFont, logoColors } from "../lib/brand";
import { cn } from "../lib/format";
import { Badge, Field, Input, Textarea } from "./ui";

const SLUG = /^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$/;
const LOGO_TYPES = ["image/png", "image/jpeg", "image/webp"];
const MAX_LOGO_BYTES = 2 * 1024 * 1024;
const DEFAULT_FONT = "Inter";
export const PRIMARY_PRESETS = ["#244BB0", "#2563EB", "#0891B2", "#16A34A", "#7C3AED", "#DC2626", "#EA580C"];
export const SECONDARY_PRESETS = ["#0F1B2D", "#111827", "#1E293B", "#172554", "#1E1B4B", "#052E2B"];

/** The create and edit forms' state. `port` is the input's text; `logo` a file chosen but not yet uploaded. */
export interface OrgForm {
  name: string; slug: string; port: string; primary_color: string; secondary_color: string; brand_font: string;
  contact_email: string; contact_phone: string; website: string; address: string; logo: File | null;
}

export function slugify(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

export function newOrgForm(): OrgForm {
  return {
    name: "", slug: "", port: "", primary_color: PRIMARY_PRESETS[0], secondary_color: SECONDARY_PRESETS[0],
    brand_font: DEFAULT_FONT, contact_email: "", contact_phone: "", website: "", address: "", logo: null,
  };
}

/** An existing organization as form state. Unset colours start at the look it has now: Governix blue and
 * a white sidebar. */
export function orgFormOf(org: Organization): OrgForm {
  return {
    name: org.name, slug: org.slug, port: org.allowed_port ? String(org.allowed_port) : "",
    primary_color: org.primary_color ?? PRIMARY_PRESETS[0], secondary_color: org.secondary_color ?? "#FFFFFF",
    brand_font: org.brand_font ?? DEFAULT_FONT, contact_email: org.contact_email ?? "",
    contact_phone: org.contact_phone ?? "", website: org.website ?? "", address: org.address ?? "", logo: null,
  };
}

/** The JSON both POST and PATCH take (create adds `slug` and `admin`). */
export function orgBody(form: OrgForm) {
  const text = (value: string) => value.trim() || null;
  return {
    name: form.name.trim(), allowed_port: Number(form.port), primary_color: form.primary_color,
    secondary_color: form.secondary_color, brand_font: form.brand_font, contact_email: text(form.contact_email),
    contact_phone: text(form.contact_phone), website: text(form.website), address: text(form.address),
  };
}

function portProblem(port: string, options: OrganizationOptions | undefined, organizationId?: string): string | null {
  if (!port) return "Choose the organization's port.";
  const value = Number(port);
  const [min, max] = [options?.port_min ?? 7001, options?.port_max ?? 7100];
  if (!Number.isInteger(value) || value < min || value > max) return `Choose a port between ${min} and ${max}.`;
  const owner = options?.taken_ports.find((t) => t.port === value && t.organization_id !== organizationId);
  return owner ? `Port ${value} is used by ${owner.organization_name}.` : null;
}

export function orgFormReady(form: OrgForm, options: OrganizationOptions | undefined, organizationId?: string) {
  return form.name.trim().length >= 2 && SLUG.test(form.slug) && HEX.test(form.primary_color)
    && HEX.test(form.secondary_color) && Boolean(form.brand_font) && !portProblem(form.port, options, organizationId);
}

/** The first port nobody uses, for a new organization. */
export function firstFreePort(options: OrganizationOptions): number | undefined {
  const taken = new Set(options.taken_ports.map((t) => t.port));
  for (let port = options.port_min; port <= options.port_max; port++) if (!taken.has(port)) return port;
}

export function uploadLogo(organizationId: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return api.upload<Organization>(`/organizations/${organizationId}/logo`, form);
}

/** Name, slug, port, logo, brand colours, font and contact details. `org` is set when editing. */
export function OrganizationFields({ value, onChange, options, org }: {
  value: OrgForm; onChange: Dispatch<SetStateAction<OrgForm>>; options?: OrganizationOptions; org?: Organization | null;
}) {
  const set = <K extends keyof OrgForm>(key: K) => (next: OrgForm[K]) => onChange((form) => ({ ...form, [key]: next }));
  const creating = !org;
  // Colours read from a new logo: applied at once when creating, offered when editing.
  const [suggested, setSuggested] = useState<{ primary?: string; secondary?: string } | null>(null);

  async function chooseLogo(file: File | null) {
    onChange((form) => ({ ...form, logo: file }));
    setSuggested(null);
    if (!file) return;
    const colours = await logoColors(file).catch(() => ({} as { primary?: string; secondary?: string }));
    if (!colours.primary && !colours.secondary) return;
    if (creating) applyColours(colours);
    else setSuggested(colours);
  }
  function applyColours(colours: { primary?: string; secondary?: string }) {
    onChange((form) => ({
      ...form, primary_color: colours.primary ?? form.primary_color, secondary_color: colours.secondary ?? form.secondary_color,
    }));
    setSuggested(null);
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-5 sm:grid-cols-2">
        <div className="space-y-4">
          <Field label="Organization name *" htmlFor="org-name">
            <div className="relative">
              <Building2 className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
              <Input id="org-name" className="pl-9" value={value.name} placeholder="e.g. Acme Corporation"
                onChange={(e) => {
                  const name = e.target.value;
                  // The slug follows the name until it is edited by hand.
                  onChange((form) => ({ ...form, name, slug: creating && form.slug === slugify(form.name) ? slugify(name) : form.slug }));
                }} />
            </div>
          </Field>
          {creating ? (
            <Field label="Slug *" htmlFor="org-slug" hint="Lowercase letters, numbers and hyphens. It cannot change later.">
              <Input id="org-slug" className="font-mono" value={value.slug} onChange={(e) => set("slug")(e.target.value)} placeholder="acme-corporation" />
            </Field>
          ) : (
            <p className="text-xs text-muted">Slug <span className="font-mono text-ink-soft">{org.slug}</span></p>
          )}
        </div>
        <PortField value={value.port} onChange={set("port")} options={options} organizationId={org?.id} />
      </div>

      <div className="space-y-2">
        <LogoField file={value.logo} onChange={(file) => void chooseLogo(file)} org={org} />
        {suggested && (
          <div className="flex flex-wrap items-center gap-2 rounded-md bg-subtle px-3 py-2 text-xs text-ink-soft">
            Colours in this logo:
            {[suggested.primary, suggested.secondary].filter(Boolean).map((c) => (
              <span key={c} className="inline-flex items-center gap-1 font-mono"><span className="size-3 rounded-full" style={{ backgroundColor: c }} />{c}</span>
            ))}
            <button type="button" onClick={() => applyColours(suggested)} className="font-semibold text-brand-700 hover:underline">Use them</button>
          </div>
        )}
      </div>

      <section className="space-y-3">
        <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-ink-soft">
          <Palette className="size-4 text-brand-600" />Brand colors
        </h3>
        <div className="grid gap-4 sm:grid-cols-2">
          <ColorCard label="Primary brand color" hint="Buttons, links and highlights" value={value.primary_color} onChange={set("primary_color")} presets={PRIMARY_PRESETS} />
          <ColorCard label="Secondary brand color" hint="The navigation sidebar" value={value.secondary_color} onChange={set("secondary_color")} presets={SECONDARY_PRESETS} />
        </div>
      </section>

      <FontField value={value.brand_font} onChange={set("brand_font")} fonts={options?.fonts ?? []} />

      <details className="group rounded-lg border border-line" open={!creating && Boolean(value.contact_email || value.contact_phone || value.website || value.address)}>
        <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2.5 text-sm font-medium text-ink-soft">
          Contact details <span className="text-xs font-normal text-muted">Optional</span>
        </summary>
        <div className="space-y-4 border-t border-line p-3">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Contact email"><Input type="email" value={value.contact_email} onChange={(e) => set("contact_email")(e.target.value)} /></Field>
            <Field label="Contact phone"><Input value={value.contact_phone} onChange={(e) => set("contact_phone")(e.target.value)} /></Field>
          </div>
          <Field label="Website"><Input value={value.website} onChange={(e) => set("website")(e.target.value)} placeholder="https://" /></Field>
          <Field label="Address"><Textarea rows={2} value={value.address} onChange={(e) => set("address")(e.target.value)} /></Field>
        </div>
      </details>
    </div>
  );
}

/** The port input, the range and free count, and a grid of nearby ports to pick from. */
function PortField({ value, onChange, options, organizationId }: {
  value: string; onChange: (port: string) => void; options?: OrganizationOptions; organizationId?: string;
}) {
  const [min, max] = [options?.port_min ?? 7001, options?.port_max ?? 7100];
  const taken = new Map(options?.taken_ports.filter((t) => t.organization_id !== organizationId).map((t) => [t.port, t.organization_name]));
  const free = max - min + 1 - taken.size;
  let firstFree = min;
  while (firstFree < max && taken.has(firstFree)) firstFree++;
  // A few used ports before the first free one, then free ones: 14 cells.
  const start = Math.max(min, Math.min(firstFree - 4, max - 13));
  const shown = Array.from({ length: Math.min(14, max - start + 1) }, (_, i) => start + i);
  const problem = value ? portProblem(value, options, organizationId) : null;
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor="org-port" className="text-sm font-medium text-ink-soft">Allowed port *</label>
        <span className="flex gap-1"><Badge tone="brand">{min} – {max}</Badge><Badge tone="ok">{free} free</Badge></span>
      </div>
      <div className="relative">
        <Network className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
        <Input id="org-port" type="number" inputMode="numeric" min={min} max={max} className="pl-9 tabular-nums"
          value={value} onChange={(e) => onChange(e.target.value)} placeholder={String(firstFree)} />
      </div>
      <p className="pt-1 text-xs font-medium text-muted">Quick select an available port</p>
      <div className="grid grid-cols-5 gap-1.5 sm:grid-cols-7">
        {shown.map((port) => {
          const owner = taken.get(port);
          const selected = String(port) === value;
          return (
            <button key={port} type="button" disabled={Boolean(owner)} onClick={() => onChange(String(port))}
              title={owner ? `Used by ${owner}` : `Use port ${port}`} aria-pressed={selected}
              className={cn(
                "h-8 rounded-md border text-xs font-medium tabular-nums transition-colors",
                selected ? "border-brand-600 bg-brand-600 text-white"
                  : owner ? "cursor-not-allowed border-line bg-subtle text-muted line-through"
                  : "border-line-strong bg-surface text-ink-soft hover:border-brand-500 hover:text-brand-700",
              )}>
              {port}
            </button>
          );
        })}
      </div>
      <p className={cn("text-xs", problem ? "text-bad-600" : "text-muted")}>
        {problem ?? `Dedicated tenant access port between ${min} and ${max}.`}
      </p>
    </div>
  );
}

function useObjectUrl(file: Blob | null) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!file) return setUrl(null);
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return url;
}

/** Drop zone for the logo. The file is uploaded when the form is saved. */
function LogoField({ file, onChange, org }: { file: File | null; onChange: (file: File | null) => void; org?: Organization | null }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const preview = useObjectUrl(file);
  const choose = (chosen?: File) => {
    if (!chosen) return;
    const problem = !LOGO_TYPES.includes(chosen.type) ? "Upload the logo as PNG, JPG or WebP."
      : chosen.size > MAX_LOGO_BYTES ? "The logo must be 2 MB or smaller." : null;
    setError(problem);
    if (!problem) onChange(chosen);
  };
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-ink-soft">Organization logo file</p>
        {file && <button type="button" onClick={() => onChange(null)} className="text-xs text-muted hover:text-bad-600">Remove</button>}
      </div>
      <div role="button" tabIndex={0} aria-label="Upload organization logo"
        onClick={() => input.current?.click()}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); choose(e.dataTransfer.files[0]); }}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-6 text-center transition-colors",
          dragging ? "border-brand-500 bg-brand-50" : "border-line-strong hover:border-brand-500 hover:bg-subtle",
        )}>
        {file && preview ? (
          <>
            <img src={preview} alt="" className="h-14 max-w-48 object-contain" />
            <p className="max-w-full truncate text-sm font-medium text-ink">{file.name}</p>
            <p className="text-xs text-muted">{(file.size / 1024).toFixed(0)} KB · click or drop another file to replace it</p>
          </>
        ) : org?.has_logo ? (
          <>
            <OrgLogo org={org} size="size-14" />
            <p className="text-xs text-muted">Click or drop a file to replace the current logo</p>
          </>
        ) : (
          <>
            <span className="flex size-10 items-center justify-center rounded-full bg-subtle text-muted"><ImageUp className="size-5" /></span>
            <p className="text-sm"><span className="font-semibold text-brand-700">Click to upload logo</span> <span className="text-muted">or drag and drop</span></p>
            <p className="text-xs text-muted">PNG, JPG or WebP, up to 2 MB. The brand colours are picked from it.</p>
          </>
        )}
      </div>
      {error && <p className="text-xs text-bad-600">{error}</p>}
      <input ref={input} type="file" accept={LOGO_TYPES.join(",")} className="hidden"
        onChange={(e) => { choose(e.target.files?.[0]); e.target.value = ""; }} />
    </div>
  );
}

function ColorCard({ label, hint, value, onChange, presets }: {
  label: string; hint: string; value: string; onChange: (color: string) => void; presets: string[];
}) {
  const valid = HEX.test(value);
  return (
    <div className="space-y-3 rounded-lg border border-line p-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium text-ink-soft">{label} *</p>
          <p className="text-xs text-muted">{hint}</p>
        </div>
        <span className="size-4 shrink-0 rounded-full ring-1 ring-line-strong" style={{ backgroundColor: valid ? value : "transparent" }} />
      </div>
      <div className="flex items-center gap-2">
        <input type="color" aria-label={`${label} picker`} value={valid ? value.toLowerCase() : "#244bb0"}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          className="h-9 w-11 shrink-0 cursor-pointer rounded-md border border-line-strong bg-surface p-0.5" />
        <Input aria-label={`${label} hex`} value={value} maxLength={7} className="font-mono uppercase"
          onChange={(e) => onChange(e.target.value)} />
      </div>
      {!valid && <p className="text-xs text-bad-600">Use a hex colour such as #1E3A8A.</p>}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-xs text-muted">Presets</span>
        {presets.map((preset) => {
          const selected = value.toUpperCase() === preset;
          return (
            <button key={preset} type="button" onClick={() => onChange(preset)} title={preset} aria-label={`Use ${preset}`} aria-pressed={selected}
              className={cn("size-5 rounded-full ring-offset-2 ring-offset-surface transition-transform",
                selected ? "ring-2 ring-brand-500" : "ring-1 ring-line-strong hover:scale-110")}
              style={{ backgroundColor: preset }} />
          );
        })}
      </div>
    </div>
  );
}

/** The font list as a native select laid over a preview of the chosen font. */
function FontField({ value, onChange, fonts }: { value: string; onChange: (font: string) => void; fonts: string[] }) {
  useEffect(() => { if (value) loadGoogleFont(value); }, [value]);
  const choices = fonts.includes(value) ? fonts : [value, ...fonts];
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor="org-font" className="flex items-center gap-1.5 text-sm font-medium text-ink-soft">
          <Type className="size-4 text-brand-600" />Organization brand typography (Google Font) *
        </label>
        <span className="text-xs text-muted">Applied across the tenant portal</span>
      </div>
      <div className="relative flex items-center gap-3 rounded-lg border border-line-strong bg-surface px-3 py-2 focus-within:border-brand-500 focus-within:ring-2 focus-within:ring-brand-100">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-brand-50 text-base font-semibold text-brand-700" style={{ fontFamily: fontStack(value) }}>Aa</span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-ink" style={{ fontFamily: fontStack(value) }}>{value}</p>
          <p className="truncate text-xs text-muted" style={{ fontFamily: fontStack(value) }}>Google Font · Policies, versions and answers read like this</p>
        </div>
        <ChevronDown className="size-4 shrink-0 text-muted" />
        <select id="org-font" value={value} onChange={(e) => onChange(e.target.value)} className="absolute inset-0 cursor-pointer opacity-0">
          {choices.map((font) => <option key={font} value={font}>{font}</option>)}
        </select>
      </div>
    </div>
  );
}

/** The organization's logo (its `logo_url`, sent with the session cookie), or a placeholder mark. */
export function OrgLogo({ org, size = "size-9" }: { org: Organization; size?: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [org.logo_url]);
  return org.logo_url && !failed
    ? <img src={org.logo_url} alt="" onError={() => setFailed(true)} className={`${size} rounded border border-line bg-surface object-contain`} />
    : <span className={`${size} flex items-center justify-center rounded border border-line bg-subtle text-muted`}><Landmark className="size-4" /></span>;
}
