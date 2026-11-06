import { Check, X } from "lucide-react";
import { cn } from "../lib/format";

/** The password policy, the same as the server's (app/core/inputs.py): checked as the person types, so they know
 * what is missing before they submit. The server checks again and has the last word. */
export const PASSWORD_MIN = 12;
export const PASSWORD_MAX = 128;

export interface PasswordRule { label: string; ok: boolean }

export function passwordRules(password: string, who?: { email?: string; name?: string }): PasswordRule[] {
  const lowered = password.toLowerCase();
  const local = (who?.email ?? "").split("@")[0].toLowerCase();
  const nameParts = (who?.name ?? "").toLowerCase().split(/[\s.'-]+/).filter((part) => part.length >= 3);
  const rules: PasswordRule[] = [
    { label: `${PASSWORD_MIN} to ${PASSWORD_MAX} characters`, ok: password.length >= PASSWORD_MIN && password.length <= PASSWORD_MAX },
    { label: "A capital letter (A-Z)", ok: /\p{Lu}/u.test(password) },
    { label: "A small letter (a-z)", ok: /\p{Ll}/u.test(password) },
    { label: "A digit (0-9)", ok: /\d/.test(password) },
    { label: "A symbol such as @ # $ % & * !", ok: /[^\p{L}\p{N}\s]/u.test(password) },
    { label: "No space at the start or end", ok: password === password.trim() },
    { label: "No character 4 times in a row", ok: !/(.)\1{3,}/u.test(password) },
  ];
  if (local.length >= 3) rules.push({ label: "Doesn't contain your email address", ok: !lowered.includes(local) });
  if (nameParts.length) rules.push({ label: "Doesn't contain your name", ok: !nameParts.some((part) => lowered.includes(part)) });
  return rules;
}

export function passwordOk(password: string, who?: { email?: string; name?: string }) {
  return passwordRules(password, who).every((rule) => rule.ok);
}

/** The checklist under a new-password box. Rules turn green as they are met. */
export function PasswordRules({ password, email, name }: { password: string; email?: string; name?: string }) {
  const rules = passwordRules(password, { email, name });
  return (
    <ul className="mt-1.5 grid gap-x-4 gap-y-0.5 text-xs sm:grid-cols-2" aria-label="Password rules">
      {rules.map((rule) => (
        <li key={rule.label} className={cn("flex items-center gap-1.5", rule.ok ? "text-ok-600" : password ? "text-bad-600" : "text-muted")}>
          {rule.ok ? <Check className="size-3.5 shrink-0" /> : <X className="size-3.5 shrink-0" />}
          {rule.label}
        </li>
      ))}
    </ul>
  );
}
