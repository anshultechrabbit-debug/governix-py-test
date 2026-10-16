import { ArrowLeft, CheckCircle2, MailCheck } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { api, ApiError } from "../api/client";
import { Button, Field, Input, PasswordInput } from "../components/ui";
import { sessionExpired } from "../store/authSlice";
import { useAppDispatch, useAuth } from "../store/hooks";
import { AuthShell } from "./LoginPage";

const MIN_PASSWORD = 12;

function BackToSignIn() {
  return (
    <Link to="/" className="inline-flex items-center gap-1 text-sm font-medium text-brand-600 hover:underline">
      <ArrowLeft className="size-3.5" />Back to sign in
    </Link>
  );
}

/** Ask for a reset link. The answer is the same whether or not the email has an account. */
export function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { message } = await api.post<{ message: string }>("/auth/forgot-password", { email });
      setSent(message);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send the reset link. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <AuthShell>
        <div className="flex gap-3">
          <MailCheck className="size-6 shrink-0 text-ok-600" />
          <div className="space-y-1">
            <h1 className="text-base font-semibold">Check your email</h1>
            <p className="text-sm text-ink-soft">{sent}</p>
            <p className="text-xs text-muted">The link works once. If nothing arrives in a few minutes, check spam or ask again.</p>
          </div>
        </div>
        <BackToSignIn />
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <h1 className="text-base font-semibold">Forgot your password?</h1>
          <p className="text-sm text-muted">Enter your email and we'll send you a link to choose a new one.</p>
        </div>
        <Field label="Email" htmlFor="forgot-email">
          <Input id="forgot-email" type="email" autoComplete="username" required autoFocus value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </Field>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
        <Button type="submit" className="w-full" loading={busy}>Send reset link</Button>
        <BackToSignIn />
      </form>
    </AuthShell>
  );
}

/** Choose a new password from the emailed link (/reset-password?token=...). */
export function ResetPasswordPage() {
  const dispatch = useAppDispatch();
  const { me } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // Kept in state, then dropped from the address bar so it stays out of history and screenshots.
  const [token] = useState(() => params.get("token") ?? "");
  useEffect(() => {
    if (params.has("token")) navigate("/reset-password", { replace: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<{ message: string; expired: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const tooShort = password.length > 0 && password.length < MIN_PASSWORD;
  const mismatch = confirm.length > 0 && confirm !== password;
  const ready = password.length >= MIN_PASSWORD && confirm === password;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const { message } = await api.post<{ message: string }>("/auth/reset-password", { token, new_password: password });
      setDone(message);
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "";
      setError({
        message: err instanceof ApiError ? err.message : "Could not reset the password. Please try again.",
        expired: code === "INVALID_RESET_TOKEN" || code === "RESET_TOKEN_EXPIRED",
      });
    } finally {
      setBusy(false);
    }
  }

  // Every session ended with the reset, this browser's too: sign in again with the new password.
  const signIn = () => {
    if (me) dispatch(sessionExpired());
    navigate("/", { replace: true });
  };

  if (!token) {
    return (
      <AuthShell>
        <h1 className="text-base font-semibold">This link is incomplete</h1>
        <p className="text-sm text-ink-soft">Open the link from your email again, or ask for a new one.</p>
        <Link to="/forgot-password" className="text-sm font-medium text-brand-600 hover:underline">Send a new reset link</Link>
      </AuthShell>
    );
  }

  if (done) {
    return (
      <AuthShell>
        <div className="flex gap-3">
          <CheckCircle2 className="size-6 shrink-0 text-ok-600" />
          <div className="space-y-1">
            <h1 className="text-base font-semibold">Password changed</h1>
            <p className="text-sm text-ink-soft">{done}</p>
          </div>
        </div>
        <Button className="w-full" onClick={signIn}>Sign in</Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <h1 className="text-base font-semibold">Choose a new password</h1>
          <p className="text-sm text-muted">You'll be signed out everywhere and can sign in with the new one.</p>
        </div>
        <Field label="New password" htmlFor="new-password"
          hint={tooShort ? `At least ${MIN_PASSWORD} characters (${password.length}/${MIN_PASSWORD})` : `At least ${MIN_PASSWORD} characters`}>
          <PasswordInput id="new-password" autoComplete="new-password" required autoFocus value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Confirm new password" htmlFor="confirm-password" hint={mismatch ? "The passwords don't match." : undefined}>
          <PasswordInput id="confirm-password" autoComplete="new-password" required value={confirm}
            onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        {error && (
          <div className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">
            {error.message}
            {error.expired && <> <Link to="/forgot-password" className="font-medium underline">Send a new link</Link></>}
          </div>
        )}
        <Button type="submit" className="w-full" loading={busy} disabled={!ready}>Reset password</Button>
        <BackToSignIn />
      </form>
    </AuthShell>
  );
}
