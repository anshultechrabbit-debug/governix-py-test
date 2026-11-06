import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Copy, Download, KeyRound, ShieldCheck, ShieldOff, Smartphone } from "lucide-react";
import QRCode from "qrcode";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, setStepUpHandler } from "../api/client";
import type { MfaSetup, MfaStatus, RecoveryCodes } from "../api/types";
import { copyText } from "../lib/clipboard";
import { formatDate } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";
import { Button, Field, Input, Modal, PasswordInput, Spinner } from "./ui";

type Stage = "status" | "scan" | "codes" | "renew" | "disable";

/** Two-step sign-in with an authenticator app (Google Authenticator, Microsoft Authenticator, Authy, ...):
 * set it up from a QR code, keep the backup codes, renew them, or turn it off. */
export function TwoStepDialog({ onClose }: { onClose: () => void }) {
  const { me, setMfaEnabled } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data: status, isLoading } = useQuery({ queryKey: ["auth", "mfa"], queryFn: () => api.get<MfaStatus>("/auth/mfa") });
  const [stage, setStage] = useState<Stage>("status");
  const [setup, setSetup] = useState<MfaSetup | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [codes, setCodes] = useState<string[]>([]);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!setup) return setQr(null);
    QRCode.toDataURL(setup.otpauth_uri, { margin: 1, width: 200, errorCorrectionLevel: "M" }).then(setQr, () => setQr(null));
  }, [setup]);

  function go(next: Stage) {
    setStage(next);
    setCode("");
    setPassword("");
    setError(null);
  }

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const start = () => run(async () => {
    setSetup(await api.post<MfaSetup>("/auth/mfa/setup"));
    go("scan");
  });
  const enable = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const result = await api.post<RecoveryCodes>("/auth/mfa/enable", { code: code.trim() });
      setCodes(result.codes);
      setSetup(null);
      setMfaEnabled(true);
      void queryClient.invalidateQueries({ queryKey: ["auth", "mfa"] });
      go("codes");
      toast("ok", "Two-step sign-in is on.");
    });
  };
  const renew = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const result = await api.post<RecoveryCodes>("/auth/mfa/recovery-codes", { code: code.trim() });
      setCodes(result.codes);
      void queryClient.invalidateQueries({ queryKey: ["auth", "mfa"] });
      go("codes");
    });
  };
  const disable = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await api.post<MfaStatus>("/auth/mfa/disable", { password, code: code.trim() });
      setMfaEnabled(false);
      void queryClient.invalidateQueries({ queryKey: ["auth", "mfa"] });
      toast("ok", "Two-step sign-in is off. Delete the Governix entry from your authenticator app: it no longer works.");
      onClose();
    });
  };

  const sixDigits = /^\d{6}$/.test(code.replace(/\s/g, ""));
  const errorBox = error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>;
  const codeField = (label: string, backupAllowed: boolean) => (
    <Field label={label} hint={backupAllowed ? "Or one of your backup codes." : undefined}>
      <Input autoFocus required value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code"
        inputMode={backupAllowed ? "text" : "numeric"} maxLength={backupAllowed ? 20 : 7} placeholder="123456"
        className="font-mono tracking-widest" />
    </Field>
  );

  return (
    <Modal open onClose={onClose} icon={<Smartphone className="size-5" />} title="Two-step sign-in"
      description="After your password, Governix asks for a 6-digit code from an authenticator app on your phone.">
      {isLoading || !status ? <div className="flex justify-center py-6"><Spinner /></div>
        : stage === "codes" ? <BackupCodes codes={codes} email={me?.email ?? ""} onDone={() => go("status")} />
        : stage === "scan" && setup ? (
          <form onSubmit={enable} className="space-y-4 text-sm">
            <ol className="list-decimal space-y-1 pl-5 text-ink-soft">
              <li>Install <span className="font-medium">Google Authenticator</span> (or Microsoft Authenticator, Authy) on your phone.</li>
              <li>In the app, tap <span className="font-medium">+</span> and scan this QR code.</li>
              <li>Enter the 6-digit code the app shows.</li>
            </ol>
            <p className="flex gap-1.5 rounded-md bg-warn-50 px-3 py-2 text-xs text-warn-600">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              Set this up before? Delete the old Governix entries for {me?.email} in your app first: this QR code
              replaces them and they no longer work. The new entry shows today's date.
            </p>
            <div className="flex flex-col items-center gap-3 rounded-lg border border-line p-4 sm:flex-row sm:items-start">
              {qr ? <img src={qr} alt="QR code for your authenticator app" className="size-44 rounded bg-white p-1" />
                : <div className="flex size-44 items-center justify-center"><Spinner /></div>}
              <div className="min-w-0 flex-1 text-xs text-muted">
                <p>Can't scan it? Choose "Enter a setup key" in the app and type:</p>
                <div className="mt-2 flex items-center gap-1 rounded-md bg-subtle px-2 py-1.5">
                  <code className="flex-1 break-all font-mono text-sm text-ink">{setup.secret.match(/.{1,4}/g)?.join(" ")}</code>
                  <Button type="button" size="sm" variant="ghost" onClick={() => { void copyText(setup.secret); toast("ok", "Setup key copied."); }}>
                    <Copy className="size-4" />
                  </Button>
                </div>
                <p className="mt-2">Account: {me?.email} · Type: time based</p>
              </div>
            </div>
            {codeField("Code from the app", false)}
            {errorBox}
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => { setSetup(null); go("status"); }}>Cancel</Button>
              <Button type="submit" loading={busy} disabled={!sixDigits}><ShieldCheck className="size-4" />Turn on</Button>
            </div>
          </form>
        ) : stage === "renew" ? (
          <form onSubmit={renew} className="space-y-4 text-sm">
            <p className="text-ink-soft">You get 10 new backup codes, and your old ones stop working.</p>
            {codeField("Code from your authenticator app", false)}
            {errorBox}
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => go("status")}>Back</Button>
              <Button type="submit" loading={busy} disabled={!sixDigits}>Get new codes</Button>
            </div>
          </form>
        ) : stage === "disable" ? (
          <form onSubmit={disable} className="space-y-4 text-sm">
            <p className="text-ink-soft">Signing in will need only your password again, your backup codes stop working, and
              important actions are refused until you set it up again. Your managers are told.</p>
            <Field label="Password"><PasswordInput autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
            {codeField("Code from your authenticator app", false)}
            <p className="text-xs text-muted">Backup codes can't turn it off. Lost your phone? Ask your admin to reset it.</p>
            {errorBox}
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => go("status")}>Back</Button>
              <Button type="submit" variant="danger" loading={busy} disabled={!password || !sixDigits}>
                <ShieldOff className="size-4" />Turn off
              </Button>
            </div>
          </form>
        ) : status.enabled ? (
          <div className="space-y-4 text-sm">
            <div className="flex items-start gap-3 rounded-lg border border-ok-600/30 bg-ok-50 p-3">
              <ShieldCheck className="mt-0.5 size-5 shrink-0 text-ok-600" />
              <div>
                <p className="font-medium text-ink">On{status.enabled_at ? ` since ${formatDate(status.enabled_at)}` : ""}</p>
                <p className={status.recovery_codes_left <= 2 ? "text-warn-600" : "text-muted"}>
                  {status.recovery_codes_left} of 10 backup codes left{status.recovery_codes_left <= 2 ? ": get new ones soon." : "."}
                </p>
              </div>
            </div>
            <p className="text-xs text-muted">New phone? Turn it off here and set it up again on the new phone. Lost your phone? Sign in with a backup code, or ask your admin to reset it.</p>
            {errorBox}
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="secondary" onClick={() => go("renew")}><KeyRound className="size-4" />New backup codes</Button>
              <Button variant="danger" onClick={() => go("disable")}><ShieldOff className="size-4" />Turn off</Button>
            </div>
          </div>
        ) : (
          <div className="space-y-4 text-sm">
            <p className="text-ink-soft">
              Even if someone learns your password, they can't sign in without your phone. You need an authenticator app such as
              Google Authenticator, Microsoft Authenticator or Authy.
            </p>
            {errorBox}
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="secondary" onClick={onClose}>Not now</Button>
              <Button loading={busy} onClick={() => void start()}><Smartphone className="size-4" />Set up</Button>
            </div>
          </div>
        )}
    </Modal>
  );
}

/** The backup codes, shown once: copy, download, and confirm they were saved. */
function BackupCodes({ codes, email, onDone }: { codes: string[]; email: string; onDone: () => void }) {
  const toast = useToast();
  const [saved, setSaved] = useState(false);
  const text = `Governix backup codes for ${email}\nEach code works once, in place of an authenticator code.\n\n${codes.join("\n")}\n`;

  function download() {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: "governix-backup-codes.txt" });
    link.click();
    URL.revokeObjectURL(url);
    setSaved(true);
  }

  return (
    <div className="space-y-4 text-sm">
      <p className="text-ink-soft">
        <span className="font-medium text-ink">Save these backup codes now.</span> If you lose your phone, each one signs you in once.
        They won't be shown again.
      </p>
      <ul className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-lg bg-subtle p-3 font-mono text-sm">
        {codes.map((c) => <li key={c}>{c}</li>)}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" onClick={() => { void copyText(text); setSaved(true); toast("ok", "Backup codes copied."); }}>
          <Copy className="size-4" />Copy
        </Button>
        <Button variant="secondary" size="sm" onClick={download}><Download className="size-4" />Download</Button>
      </div>
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} />
        I have saved my backup codes somewhere safe
      </label>
      <div className="flex justify-end">
        <Button disabled={!saved} onClick={onDone}><Check className="size-4" />Done</Button>
      </div>
    </div>
  );
}

/** For an admin: turn off someone's two-step sign-in (lost or replaced phone). Shown only when it is on. */
export function ResetTwoStepButton({ user }: { user: { id: string; full_name: string; mfa_enabled: boolean } }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!user.mfa_enabled) return null;

  async function reset() {
    setBusy(true);
    try {
      await api.post(`/users/${user.id}/reset-mfa`);
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      toast("ok", `Two-step sign-in reset for ${user.full_name}. They can sign in with their password and set it up again.`);
      setOpen(false);
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not reset two-step sign-in.");
    } finally {
      setBusy(false);
    }
  }

  const label = `Reset two-step sign-in for ${user.full_name}`;
  return (
    <>
      <Button variant="ghost" size="sm" className="px-2.5" title={label} aria-label={label} onClick={() => setOpen(true)}>
        <Smartphone className="size-3.5" />
      </Button>
      <Modal open={open} onClose={() => setOpen(false)} icon={<ShieldOff className="size-5" />}
        title={`Reset two-step sign-in for ${user.full_name}?`}
        description="Use this when they lost or replaced their phone. Make sure it's really them asking (call them back on a number you know)."
        footer={<>
          <Button variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="danger" loading={busy} onClick={() => void reset()}>Reset</Button>
        </>}>
        <p className="text-sm text-ink-soft">
          Their authenticator app and backup codes stop working and they are signed out everywhere. They then sign in with
          just their password and can set up two-step sign-in again. This is recorded in the audit log.
        </p>
      </Modal>
    </>
  );
}

/** Asks for the authenticator code when the server wants one for an important action (see api/client.ts), or
 * explains that two-step sign-in must be turned on first. Mounted once in the signed-in layout. */
export function StepUpPrompt() {
  const [ask, setAsk] = useState<{ reason: string; message: string } | null>(null);
  const [code, setCode] = useState("");
  const [settingUp, setSettingUp] = useState(false);
  const resolver = useRef<((code: string | null) => void) | null>(null);

  useEffect(() => {
    setStepUpHandler((reason, message) => new Promise((resolve) => {
      resolver.current?.(null); // a prompt already open for another request: that one is cancelled
      resolver.current = resolve;
      setCode("");
      setAsk({ reason, message });
    }));
    return () => setStepUpHandler(null);
  }, []);

  function finish(value: string | null) {
    resolver.current?.(value);
    resolver.current = null;
    setAsk(null);
  }

  const setup = ask?.reason === "STEP_UP_SETUP_REQUIRED";
  const ready = /^\d{6}$/.test(code.replace(/\s/g, ""));
  return (
    <>
      <Modal open={Boolean(ask)} onClose={() => finish(null)} icon={<ShieldCheck className="size-5" />}
        title={setup ? "Two-step sign-in needed" : "Confirm with your authenticator"}
        description={setup ? undefined : "This is an important action. Enter the 6-digit code your authenticator app shows now."}
        footer={setup ? <>
          <Button variant="secondary" onClick={() => finish(null)}>Close</Button>
          <Button onClick={() => { finish(null); setSettingUp(true); }}><Smartphone className="size-4" />Set up now</Button>
        </> : <>
          <Button variant="secondary" onClick={() => finish(null)}>Cancel</Button>
          <Button type="submit" form="step-up-form" disabled={!ready}>Confirm</Button>
        </>}>
        {setup ? (
          <p className="text-sm text-ink-soft">
            Important actions (users, deletions, policy withdrawals and similar) are confirmed with a code from an authenticator
            app, so a stolen password alone can't be used for them. Set up two-step sign-in, then do the action again.
          </p>
        ) : (
          <form id="step-up-form" className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (ready) finish(code.replace(/\s/g, "")); }}>
            {ask && ask.reason !== "STEP_UP_REQUIRED" && (
              <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{ask.message}</p>
            )}
            <Field label="Code">
              <Input autoFocus required value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code"
                inputMode="numeric" maxLength={7} placeholder="123456" className="font-mono tracking-widest" />
            </Field>
            <p className="text-xs text-muted">Each code confirms one action. For the next action, wait for a new code.</p>
          </form>
        )}
      </Modal>
      {settingUp && <TwoStepDialog onClose={() => setSettingUp(false)} />}
    </>
  );
}
