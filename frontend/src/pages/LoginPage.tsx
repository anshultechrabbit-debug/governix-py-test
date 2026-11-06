import { ArrowLeft, ShieldCheck, Smartphone } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";
import { ApiError } from "../api/client";
import { Button, Field, Input, PasswordInput } from "../components/ui";
import { useAuth } from "../store/hooks";

/** The signed-out frame: brand on the dark page, one card (sign in, forgot and reset password). */
export function AuthShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-brand-900 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-2 text-white">
          <ShieldCheck className="size-7 text-brand-100" />
          <span className="text-lg font-semibold tracking-wide">Governix</span>
        </div>
        <div className="space-y-4 rounded-lg bg-surface p-5 shadow-xl sm:p-6">{children}</div>
      </div>
    </div>
  );
}

export function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The password was right and the account uses two-step sign-in: ask for the code next.
  const [codeStep, setCodeStep] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const me = await login(email, password);
      if (!me) {
        setPassword("");
        setCodeStep(true);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to sign in. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (codeStep) {
    return <CodeStep email={email} onRestart={(message) => { setCodeStep(false); setError(message ?? null); }} />;
  }

  return (
    <AuthShell>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <h1 className="text-base font-semibold">Sign in</h1>
          <p className="text-sm text-muted">Policy intelligence for your organization</p>
        </div>
        <Field label="Email" htmlFor="email">
          <Input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Password" htmlFor="password">
          <PasswordInput id="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <div className="-mt-2 text-right">
          <Link to="/forgot-password" className="text-sm font-medium text-brand-600 hover:underline">Forgot password?</Link>
        </div>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
        <Button type="submit" className="w-full" loading={busy}>Sign in</Button>
      </form>
    </AuthShell>
  );
}

/** The second step of a two-step sign-in: the 6-digit code from the authenticator app, or a backup code. */
function CodeStep({ email, onRestart }: { email: string; onRestart: (message?: string) => void }) {
  const { verifyMfa } = useAuth();
  const [backup, setBackup] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await verifyMfa(code.trim());
    } catch (err) {
      // The step expired, or the account was locked or reset meanwhile: back to the password.
      if (err instanceof ApiError && ["MFA_CHALLENGE_EXPIRED", "ACCOUNT_LOCKED", "ACCOUNT_SUSPENDED", "ORGANIZATION_SUSPENDED"].includes(err.code)) {
        return onRestart(err.message);
      }
      setError(err instanceof ApiError ? err.message : "Unable to sign in. Please try again.");
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  const ready = backup ? code.replace(/[^a-z0-9]/gi, "").length === 12 : /^\d{6}$/.test(code.replace(/\s/g, ""));
  return (
    <AuthShell>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <h1 className="flex items-center gap-2 text-base font-semibold"><Smartphone className="size-4 text-brand-600" />Two-step sign-in</h1>
          <p className="text-sm text-muted">
            {backup ? "Enter one of the backup codes you saved when you set up two-step sign-in. Each works once."
              : <>Open your authenticator app (Google Authenticator or similar) and enter the 6-digit code for Governix ({email}).</>}
          </p>
        </div>
        <Field label={backup ? "Backup code" : "Code"} htmlFor="code">
          <Input id="code" autoFocus required value={code} onChange={(e) => setCode(e.target.value)}
            autoComplete="one-time-code" inputMode={backup ? "text" : "numeric"} maxLength={backup ? 20 : 7}
            placeholder={backup ? "xxxx-xxxx-xxxx" : "123456"} className="font-mono tracking-widest" />
        </Field>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
        <Button type="submit" className="w-full" loading={busy} disabled={!ready}>Verify</Button>
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <button type="button" className="flex items-center gap-1 text-muted hover:text-ink" onClick={() => onRestart()}>
            <ArrowLeft className="size-3.5" />Back
          </button>
          <button type="button" className="font-medium text-brand-600 hover:underline"
            onClick={() => { setBackup((b) => !b); setCode(""); setError(null); }}>
            {backup ? "Use the authenticator app" : "Use a backup code"}
          </button>
        </div>
        {backup && <p className="text-xs text-muted">No phone and no backup codes? Ask your admin to reset your two-step sign-in.</p>}
      </form>
    </AuthShell>
  );
}
