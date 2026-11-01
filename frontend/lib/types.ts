export type RunStatus =
  | "queued" | "analyzing" | "locating" | "fixing" | "verifying" | "opening_pr"
  | "awaiting_approval" | "merged" | "rejected" | "needs_human" | "error"
  | "rerun" | "superseded" | "waiting";

export type AgentName =
  | "Orchestrator" | "Log Analyst" | "Code Locator" | "Fixer" | "Verifier" | "PR Agent" | "Human";

export interface Step {
  id: string;
  run_id: string;
  agent: AgentName;
  status: "running" | "done" | "failed" | "info";
  message: string;
  data: Record<string, any>;
  ts: number;
}

export interface TestCounts {
  passed?: number;
  failed?: number;
  total?: number;
}

export interface Attempt {
  number: number;
  reasoning: string;
  confidence: number;
  diff: string;
  branch: string;
  ci_url: string;
  passed: boolean | null;
  failure_excerpt: string;
  tests: TestCounts;
}

export interface RepoInfo {
  full_name: string;
  private: boolean;
  description: string;
  default_branch: string;
  pushed_at: string;
  language: string;
  can_push: boolean;
  watched: boolean;
  missing?: boolean;
}

export interface Config {
  repos: string[];
  watch: { enabled: boolean; last_poll: number | null; error: string; interval_s: number };
  demo_controls: boolean;
  demo_bugs: { id: string; label: string }[];
  min_confidence: number;
  max_retries: number;
}

export interface Run {
  id: string;
  number: number;
  repo: string;
  ci_run_id: number;
  ci_run_url: string;
  workflow_name: string;
  head_sha: string;
  head_branch: string;
  status: RunStatus;
  error_type: string;
  summary: string;
  failing_test: string;
  files: string[];
  confidence: number;
  diff: string;
  reasoning: string;
  fix_branch: string;
  pr_number: number | null;
  pr_url: string;
  escalation_reason: string;
  wait_kind?: string;
  retry_at?: number | null;
  resumes?: number;
  issue_url: string;
  tests_before: TestCounts;
  tests_after: TestCounts;
  trigger: "webhook" | "watch" | "manual";
  minutes_saved: number;
  created_at: number;
  finished_at: number | null;
  steps?: Step[];
  attempts?: Attempt[];
  log_excerpt?: string;
}

export interface Stats {
  total_runs: number;
  fixed: number;
  merged: number;
  needs_human: number;
  minutes_saved: number;
}

export type StreamEvent =
  | { type: "snapshot"; runs: Run[]; stats: Stats }
  | { type: "step"; run: Run; step: Step }
  | { type: "run"; run: Run };
