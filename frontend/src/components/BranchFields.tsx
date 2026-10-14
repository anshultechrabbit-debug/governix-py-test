import { Building2, MapPin } from "lucide-react";
import { Field, Input } from "./ui";

/** A branch's name and address, as a new branch requires them (branches/schema.py BranchAddress). */
export interface BranchDraft { name: string; street_address: string; state: string; postal_code: string }

export const EMPTY_BRANCH: BranchDraft = { name: "", street_address: "", state: "", postal_code: "" };

// PIN codes and postal codes: "400001", "94105", "94105-1234", "SW1A 1AA" (POSTAL_CODE_PATTERN).
const POSTAL_CODE = "[A-Za-z0-9][A-Za-z0-9 \\-]{1,18}[A-Za-z0-9]";

export function branchReady(branch: BranchDraft) {
  return branch.name.trim().length >= 2 && branch.street_address.trim().length >= 2
    && branch.state.trim().length >= 2 && new RegExp(`^${POSTAL_CODE}$`).test(branch.postal_code.trim());
}

export function branchBody(branch: BranchDraft) {
  return {
    name: branch.name.replace(/\s+/g, " ").trim(),
    street_address: branch.street_address.trim(),
    state: branch.state.trim(),
    postal_code: branch.postal_code.trim(),
  };
}

const withIcon = "pl-9";

export function BranchFields({ value, onChange, autoFocus }: {
  value: BranchDraft; onChange: (branch: BranchDraft) => void; autoFocus?: boolean;
}) {
  const set = (field: keyof BranchDraft) => (e: { target: { value: string } }) =>
    onChange({ ...value, [field]: e.target.value });
  return (
    <>
      <Field label="Branch name">
        <div className="relative">
          <Building2 className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input required autoFocus={autoFocus} className={withIcon} maxLength={200} placeholder="e.g. Downtown Main Branch"
            value={value.name} onChange={set("name")} />
        </div>
      </Field>
      <Field label="Street address">
        <div className="relative">
          <MapPin className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input required className={withIcon} maxLength={300} placeholder="e.g. 101 MG Road, Suite 400"
            value={value.street_address} onChange={set("street_address")} />
        </div>
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="State">
          <Input required maxLength={100} placeholder="e.g. Maharashtra" value={value.state} onChange={set("state")} />
        </Field>
        <Field label="Pincode / postal code">
          <Input required maxLength={20} pattern={POSTAL_CODE} placeholder="e.g. 400001" value={value.postal_code} onChange={set("postal_code")} />
        </Field>
      </div>
    </>
  );
}

/** "101 MG Road, Maharashtra 400001", or null for a branch made before addresses were kept. */
export function branchAddress(branch: { street_address: string | null; state: string | null; postal_code: string | null }) {
  const place = [branch.state, branch.postal_code].filter(Boolean).join(" ");
  return [branch.street_address, place].filter(Boolean).join(", ") || null;
}
