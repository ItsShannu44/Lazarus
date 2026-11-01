import { ACTIVE } from "./api";
import type { Run, RunStatus } from "./types";

/** Every run maps onto one of four vital states. Colour, trace and copy all follow it. */
export type Vital = "dead" | "working" | "alive" | "rest";

export function vitalOf(status: RunStatus | undefined): Vital {
  if (!status) return "rest";
  if (ACTIVE.includes(status)) return "working";
  if (status === "waiting") return "working";
  if (status === "awaiting_approval" || status === "merged") return "alive";
  if (status === "needs_human" || status === "error") return "dead";
  return "rest";
}

export const VITAL_TEXT: Record<Vital, string> = {
  dead: "text-dead",
  working: "text-work",
  alive: "text-alive",
  rest: "text-muted",
};

export const VITAL_BG: Record<Vital, string> = {
  dead: "bg-dead",
  working: "bg-work",
  alive: "bg-alive",
  rest: "bg-faint",
};

export const VITAL_HEX: Record<Vital, string> = {
  dead: "#ff4a3d",
  working: "#ff6a1a",
  alive: "#4ade80",
  rest: "#75706a",
};

/** The big word on the monitor. */
export function headline(run: Run): string {
  switch (run.status) {
    case "queued": return "Waiting for a free slot";
    case "analyzing": return "Reading the CI logs";
    case "locating": return "Finding the broken code";
    case "fixing": return "Writing a fix";
    case "verifying": return "Testing the fix in CI";
    case "opening_pr": return "Opening a pull request";
    case "awaiting_approval": return "Pulse is back";
    case "merged": return "Revived";
    case "needs_human": return "Needs a human";
    case "error": return "Stopped";
    case "rerun": return "Re-running CI";
    case "superseded": return "Already fixed";
    case "rejected": return "Rejected";
    case "waiting": return run.wait_kind === "access" ? "Waiting for write access" : "Retrying shortly";
  }
}

export function duration(seconds: number) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

export function minutesLabel(min: number) {
  if (min < 60) return `${min}m`;
  const h = Math.floor(min / 60);
  return min % 60 ? `${h}h ${min % 60}m` : `${h}h`;
}
