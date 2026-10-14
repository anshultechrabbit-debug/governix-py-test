import type { NginxState } from "../api/types";
import { Badge } from "./ui";

const STATE: Record<NginxState, ["ok" | "warn" | "neutral", string]> = {
  deployed: ["ok", "Running"],
  outdated: ["warn", "Out of date"],
  disabled: ["neutral", "Disabled"],
  not_deployed: ["neutral", "Not deployed"],
  // NGINX_CONF_DIR is not set on the server: the config is copied into nginx by hand.
  unavailable: ["neutral", "Manual"],
};

/** An organization's nginx server block, as GET /nginx reports it. */
export function NginxBadge({ state }: { state: NginxState }) {
  const [tone, label] = STATE[state];
  return (
    <Badge tone={tone}>
      {state === "deployed" && <span className="size-1.5 rounded-full bg-current" aria-hidden />}
      {label}
    </Badge>
  );
}
