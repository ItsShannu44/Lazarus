import type { Config, RepoInfo, Run, Stats } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
export const DEFAULT_REPO = process.env.NEXT_PUBLIC_DEFAULT_REPO || "";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let msg = `${res.status}`;
    try {
      const body = await res.json();
      msg = body.detail || JSON.stringify(body);
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

export const api = {
  runs: () => call<Run[]>("/api/runs"),
  run: (id: string) => call<Run>(`/api/runs/${id}`),
  stats: () => call<Stats>("/api/stats"),
  approve: (id: string) => call<Run>(`/api/runs/${id}/approve`, { method: "POST" }),
  reject: (id: string, reason = "") =>
    call<Run>(`/api/runs/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) }),
  config: () => call<Config>("/api/config"),
  repos: (refresh = false) => call<RepoInfo[]>(`/api/repos${refresh ? "?refresh=true" : ""}`),
  watchRepo: (repo: string, watch: boolean) =>
    call<{ repos: string[] }>("/api/repos/watch", { method: "POST", body: JSON.stringify({ repo, watch }) }),
  demoBreak: (bug: string, repo?: string) =>
    call<{ commit: string; repo: string }>("/api/demo/break", { method: "POST", body: JSON.stringify({ bug, repo }) }),
  demoCheck: (repo: string) =>
    call<{ supported: boolean; reason: string }>(`/api/demo/check?repo=${encodeURIComponent(repo)}`),
  repoCi: (repo: string) =>
    call<{ has_ci: boolean; workflows: string[]; lazarus_ci: boolean; disabled?: string[]; can_write?: boolean | null }>(`/api/repos/ci?repo=${encodeURIComponent(repo)}`),
  setupCi: (repo: string) =>
    call<{ pr_url: string; pr_number: number | null; checks: string[]; manual_url: string; reason: string }>("/api/repos/ci/setup", {
      method: "POST",
      body: JSON.stringify({ repo }),
    }),
  demoReset: (repo?: string) =>
    call<{ changed: string[]; repo: string }>("/api/demo/reset", { method: "POST", body: JSON.stringify({ repo }) }),
  retry: (id: string) => call<Run>(`/api/runs/${id}/retry`, { method: "POST" }),
  trigger: (repo: string) =>
    call<Run>("/api/runs/trigger", { method: "POST", body: JSON.stringify({ repo }) }),
};

export const ACTIVE: Run["status"][] = [
  "queued", "analyzing", "locating", "fixing", "verifying", "opening_pr",
];

export const STATUS_LABEL: Record<Run["status"], string> = {
  queued: "Queued",
  analyzing: "Reading logs",
  locating: "Finding files",
  fixing: "Writing fix",
  verifying: "Testing fix",
  opening_pr: "Opening PR",
  awaiting_approval: "Needs approval",
  merged: "Merged",
  rejected: "Rejected",
  needs_human: "Needs human",
  error: "Error",
  rerun: "CI re-run",
  superseded: "Already fixed",
  waiting: "Will retry",
};

export function errorLabel(t: string) {
  return (
    { test_failure: "unit test failed", dependency: "bad dependency", lint: "lint error",
      type_error: "type error", build: "build error", config: "config error", infra: "flaky / infra", other: "CI failed" }[t] ||
    "CI failed"
  );
}

export function timeAgo(ts: number) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function clock(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
