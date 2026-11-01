"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { ACTIVE, api } from "@/lib/api";
import type { Run, TestCounts } from "@/lib/types";
import { duration } from "@/lib/vitals";
import { DiffView } from "./DiffView";

const EASE = [0.22, 1, 0.36, 1] as const;
const fmt = (t?: TestCounts) => (t && t.total ? `${t.passed}/${t.total} tests pass` : "");

function Section({
  title, children, defaultOpen = false, count,
}: { title: string; children: React.ReactNode; defaultOpen?: boolean; count?: number }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-t border-line pt-3">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-left text-sm font-semibold text-soft transition-colors hover:text-bone"
      >
        <motion.svg viewBox="0 0 12 12" className="h-3 w-3 text-muted" animate={{ rotate: open ? 90 : 0 }} transition={{ duration: 0.2 }} aria-hidden>
          <path d="M4 2.5L7.5 6 4 9.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </motion.svg>
        {title}
        {count !== undefined && <span className="font-normal text-muted">{count}</span>}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.3, ease: EASE }}
            className="overflow-hidden"
          >
            <div className="pt-2.5">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Countdown({ at }: { at: number }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);
  const s = Math.max(0, Math.round(at - now));
  return <>{s > 0 ? `in ${duration(s)}` : "any moment now"}</>;
}

function Notice({ tone, title, children }: { tone: "dead" | "rest" | "work"; title: string; children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: EASE }}
      className={`rounded-xl border p-3.5 text-sm ${tone === "dead" ? "border-dead/35 bg-dead/[0.08]" : tone === "work" ? "border-work/35 bg-work/[0.07]" : "border-line bg-panel-2"}`}
    >
      <div className={`font-semibold ${tone === "dead" ? "text-dead" : tone === "work" ? "text-work" : "text-bone"}`}>{title}</div>
      <div className="mt-1 leading-relaxed text-soft">{children}</div>
    </motion.div>
  );
}

export function FixPanel({ run, onChange }: { run: Run | null; onChange: (r: Run) => void }) {
  const [busy, setBusy] = useState<"approve" | "reject" | "retry" | null>(null);
  const [error, setError] = useState("");

  if (!run)
    return (
      <section className="rounded-2xl border border-line bg-panel p-4 text-sm text-muted">
        Pick an incident to see the proposed fix.
      </section>
    );

  const working = ACTIVE.includes(run.status);
  const attempts = run.attempts || [];

  async function act(kind: "approve" | "reject" | "retry") {
    if (!run) return;
    setBusy(kind);
    setError("");
    try {
      onChange(kind === "approve" ? await api.approve(run.id) : kind === "retry" ? await api.retry(run.id) : await api.reject(run.id));
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section aria-label="The fix" className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-line bg-panel">
      <header className="flex shrink-0 items-center gap-3 border-b border-line px-4 py-3">
        <h2 className="shrink-0 text-[15px] font-semibold">The fix</h2>
        {run.files?.length ? (
          <span title={run.files.join("\n")} dir="rtl" className="ml-auto min-w-0 truncate font-mono text-xs text-soft">
            {run.files[0]}
            {run.files.length > 1 ? ` +${run.files.length - 1}` : ""}
          </span>
        ) : null}
      </header>

      <div className="wrap-anywhere min-h-0 min-w-0 flex-1 space-y-3.5 overflow-y-auto p-4 max-lg:max-h-[75vh]">
        {(run.status === "needs_human" || run.status === "error") && run.escalation_reason && (
          <Notice tone="dead" title="Lazarus stopped and flagged a human">
            {run.escalation_reason}
            {run.issue_url && (
              <a href={run.issue_url} target="_blank" className="mt-2 block font-medium text-bone underline decoration-line-strong underline-offset-4 hover:decoration-bone">
                Read the logs and notes in the GitHub issue
              </a>
            )}
          </Notice>
        )}
        {run.status === "waiting" && (
          <Notice
            tone="work"
            title={run.wait_kind === "access" ? "Paused until the token can push. No action needed in Lazarus" : "Paused briefly. Lazarus retries by itself"}
          >
            {run.escalation_reason}
            <span className="mt-2 block text-muted">
              {run.wait_kind === "access"
                ? "Checks again "
                : "Next try "}
              {run.retry_at ? <Countdown at={run.retry_at} /> : "soon"}.
            </span>
          </Notice>
        )}
        {run.status === "rerun" && (
          <Notice tone="rest" title="The code wasn't at fault">
            This failure looks like a network problem, a runner outage or a flaky test, so Lazarus re-ran the failed CI jobs
            instead of changing code. If it fails again, a human gets flagged.
          </Notice>
        )}
        {run.status === "superseded" && (
          <Notice tone="rest" title="No fix needed">
            {run.escalation_reason || "A newer commit already fixed this."}
          </Notice>
        )}

        <AnimatePresence mode="wait" initial={false}>
          {run.diff ? (
            <motion.div key={`diff-${run.id}-${attempts.length}`} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.35, ease: EASE }}>
              <DiffView diff={run.diff} />
            </motion.div>
          ) : (
            <motion.div
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex items-center gap-3 rounded-xl border border-dashed border-line px-4 py-6 text-sm text-muted"
            >
              {working && (
                <span className="flex gap-1" aria-hidden>
                  {[0, 1, 2].map((i) => (
                    <motion.span
                      key={i}
                      className="h-1.5 w-1.5 rounded-full bg-work"
                      animate={{ opacity: [0.25, 1, 0.25] }}
                      transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.18 }}
                    />
                  ))}
                </span>
              )}
              {working ? "Working on a patch" : run.status === "rerun" ? "No code change was needed." : "No patch was produced."}
            </motion.div>
          )}
        </AnimatePresence>

        {run.reasoning && (
          <Section title="Why this fix" defaultOpen>
            <p className="max-w-[72ch] whitespace-pre-wrap text-sm leading-relaxed text-soft">{run.reasoning}</p>
          </Section>
        )}

        {attempts.length > 0 && (
          <Section key={attempts.length > 1 ? "many" : "one"} title="Attempts" count={attempts.length} defaultOpen={attempts.length > 1}>
            <ol className="space-y-2">
              {attempts.map((a) => (
                <li key={a.number} className="rounded-xl bg-panel-2 p-3 text-sm">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                    <span className="font-semibold">Attempt {a.number}</span>
                    <span className={a.passed ? "text-alive" : a.passed === false ? "text-dead" : "text-muted"}>
                      {a.passed ? fmt(a.tests) || "CI passed" : a.passed === false ? (a.tests?.failed ? `${a.tests.failed} failing` : "CI failed") : "Not pushed"}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-muted">
                    {a.confidence}% confident
                    {a.ci_url && (
                      <a href={a.ci_url} target="_blank" className="ml-3 text-soft underline decoration-line-strong underline-offset-4 hover:text-bone">
                        CI run
                      </a>
                    )}
                  </div>
                  {a.passed === false && a.failure_excerpt && (
                    <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-glass p-2.5 font-mono text-[11px] leading-relaxed text-soft">
                      {a.failure_excerpt.slice(-1500)}
                    </pre>
                  )}
                </li>
              ))}
            </ol>
          </Section>
        )}

        {run.log_excerpt && (
          <Section key={`log-${run.status}`} title="CI log the agents read" defaultOpen={run.status === "needs_human"}>
            <pre className="max-h-72 overflow-auto rounded-xl bg-glass p-3 font-mono text-[11px] leading-relaxed text-soft">
              {run.log_excerpt}
            </pre>
          </Section>
        )}

        {(run.pr_url || run.ci_run_url) && (
          <div className="flex flex-wrap gap-x-5 gap-y-1 border-t border-line pt-3 text-sm">
            {run.pr_url && (
              <a href={run.pr_url} target="_blank" className="font-medium text-bone underline decoration-line-strong underline-offset-4 hover:decoration-bone">
                Pull request {run.pr_number}
              </a>
            )}
            {run.ci_run_url && (
              <a href={run.ci_run_url} target="_blank" className="text-soft underline decoration-line-strong underline-offset-4 hover:text-bone">
                Failed CI run
              </a>
            )}
          </div>
        )}
      </div>

      <footer className="shrink-0 border-t border-line p-4">
        <AnimatePresence>
          {error && (
            <motion.p initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="mb-3 text-sm text-dead">
              {error}
            </motion.p>
          )}
        </AnimatePresence>
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={run.status === "awaiting_approval" ? "decide" : run.status}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.22, ease: EASE }}
          >
            {run.status === "awaiting_approval" ? (
              <div className="flex gap-3">
                <motion.button
                  whileTap={{ scale: 0.98 }}
                  disabled={!!busy}
                  onClick={() => act("approve")}
                  className="flex-1 rounded-xl bg-work px-4 py-3 font-semibold text-black transition-[filter] hover:brightness-110 disabled:opacity-60"
                >
                  {busy === "approve" ? "Merging…" : "Approve and merge"}
                </motion.button>
                <motion.button
                  whileTap={{ scale: 0.98 }}
                  disabled={!!busy}
                  onClick={() => act("reject")}
                  className="rounded-xl border border-line-strong px-4 py-3 font-semibold text-soft transition-colors hover:border-dead hover:text-dead disabled:opacity-60"
                >
                  {busy === "reject" ? "Rejecting…" : "Reject"}
                </motion.button>
              </div>
            ) : run.status === "needs_human" || run.status === "error" || run.status === "waiting" ? (
              <div className="flex gap-3">
                <motion.button
                  whileTap={{ scale: 0.98 }}
                  disabled={!!busy}
                  onClick={() => act("retry")}
                  className="flex-1 rounded-xl bg-work px-4 py-3 font-semibold text-black transition-[filter] hover:brightness-110 disabled:opacity-60"
                >
                  {busy === "retry" ? "Retrying…" : "Retry now"}
                </motion.button>
                {run.status !== "waiting" && (
                  <button
                    disabled={!!busy}
                    onClick={() => act("reject")}
                    className="rounded-xl border border-line-strong px-4 py-3 font-semibold text-soft transition-colors hover:text-bone disabled:opacity-60"
                  >
                    {busy === "reject" ? "Dismissing…" : "Dismiss"}
                  </button>
                )}
              </div>
            ) : (
              <p className="text-center text-sm text-muted">
                {working
                  ? "Nothing to decide yet. The agents are working."
                  : run.status === "merged"
                    ? "Merged. The build is alive again."
                    : run.status === "rerun"
                      ? "CI is re-running. Nothing to approve."
                      : run.status === "superseded"
                        ? "Already fixed by a newer commit. Nothing to approve."
                        : "Rejected. The pull request was closed."}
              </p>
            )}
          </motion.div>
        </AnimatePresence>
        {run.finished_at && (run.status === "awaiting_approval" || run.status === "merged") && (
          <p className="mt-2.5 flex justify-between text-xs text-muted">
            <span>Fixed in {duration(run.finished_at - run.created_at)}</span>
            <span className="text-alive">About {run.minutes_saved || 45} minutes of debugging saved</span>
          </p>
        )}
      </footer>
    </section>
  );
}
