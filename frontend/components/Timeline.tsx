"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef } from "react";
import { clock } from "@/lib/api";
import type { AgentName, Run, Step } from "@/lib/types";

const PIPELINE: { name: AgentName; job: string }[] = [
  { name: "Log Analyst", job: "Reads the CI logs and picks the error to chase" },
  { name: "Code Locator", job: "Chooses which files to open" },
  { name: "Fixer", job: "Writes the smallest patch that should work" },
  { name: "Verifier", job: "Runs your tests on the fix in GitHub Actions" },
  { name: "PR Agent", job: "Opens a pull request for review" },
  { name: "Human", job: "You approve or reject the merge" },
];

type State = "waiting" | "running" | "done" | "failed";
const ACTIVE_STATUSES = ["queued", "analyzing", "locating", "fixing", "verifying", "opening_pr"];

function agentState(steps: Step[], agent: AgentName): { state: State; last?: Step } {
  const mine = steps.filter((s) => s.agent === agent && s.status !== "info");
  const last = mine[mine.length - 1];
  if (!last) return { state: "waiting" };
  return { state: last.status as State, last };
}

const RING: Record<State, string> = {
  waiting: "border-line-strong",
  running: "border-work",
  done: "border-alive/70 bg-alive/10",
  failed: "border-dead/70 bg-dead/10",
};

const POP = {
  initial: { scale: 0.4, opacity: 0 },
  animate: { scale: 1, opacity: 1 },
  exit: { scale: 0.4, opacity: 0 },
  transition: { type: "spring" as const, stiffness: 600, damping: 30 },
};

function Glyph({ state }: { state: State }) {
  if (state === "done")
    return (
      <motion.svg {...POP} viewBox="0 0 16 16" className="h-3.5 w-3.5 text-alive" aria-hidden>
        <motion.path
          d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.35, delay: 0.05 }}
        />
      </motion.svg>
    );
  if (state === "failed")
    return (
      <motion.svg {...POP} viewBox="0 0 16 16" className="h-3.5 w-3.5 text-dead" aria-hidden>
        <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </motion.svg>
    );
  if (state === "running")
    return (
      <motion.svg {...POP} viewBox="0 0 16 16" className="spin h-4 w-4 text-work" aria-hidden>
        <circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
        <path d="M8 2.5a5.5 5.5 0 0 1 5.5 5.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </motion.svg>
    );
  return <motion.span {...POP} className="h-1.5 w-1.5 rounded-full bg-faint" />;
}

export function Timeline({ run }: { run: Run | null }) {
  const steps = run?.steps || [];
  const feed = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = feed.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [steps.length, run?.id]);
  const finished = run && !ACTIVE_STATUSES.includes(run.status);

  return (
    <section aria-label="Agents" className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-line bg-panel">
      <header className="shrink-0 border-b border-line px-4 py-3">
        <h2 className="text-[15px] font-semibold">Agents</h2>
      </header>
      {!run ? (
        <p className="px-4 py-6 text-sm text-muted">Pick an incident to see what each agent did.</p>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          <ol className="shrink-0 px-4 pt-3.5">
            {PIPELINE.map((a, i) => {
              let { state, last } = agentState(steps, a.name);
              if (finished && state === "running") state = "failed";
              const next = i < PIPELINE.length - 1 ? agentState(steps, PIPELINE[i + 1].name).state : "waiting";
              const text = last ? last.message : a.job;
              return (
                <li key={a.name} className="relative flex min-w-0 gap-3 pb-3.5">
                  {i < PIPELINE.length - 1 && (
                    <span className="absolute bottom-0 left-[13px] top-[30px] w-[2px] overflow-hidden rounded-full bg-line">
                      <motion.span
                        className="block h-full w-full origin-top bg-alive/60"
                        initial={false}
                        animate={{ scaleY: state === "done" ? (next !== "waiting" ? 1 : 0.5) : 0 }}
                        transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
                      />
                    </span>
                  )}
                  <span className={`relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-[1.5px] bg-panel transition-colors duration-300 ${RING[state]}`}>
                    <AnimatePresence mode="wait" initial={false}>
                      <Glyph key={state} state={state} />
                    </AnimatePresence>
                  </span>
                  <div className="min-w-0 flex-1 pt-[3px]">
                    <div className={`text-[15px] font-semibold leading-tight transition-colors duration-300 ${state === "waiting" ? "text-muted" : "text-bone"}`}>
                      {a.name}
                    </div>
                    <AnimatePresence mode="wait" initial={false}>
                      <motion.p
                        key={text}
                        initial={{ opacity: 0, y: 3 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -3 }}
                        transition={{ duration: 0.2 }}
                        title={text}
                        className={`wrap-anywhere mt-0.5 line-clamp-2 text-sm leading-snug ${state === "waiting" ? "text-faint" : "text-soft"}`}
                      >
                        {text}
                      </motion.p>
                    </AnimatePresence>
                  </div>
                </li>
              );
            })}
          </ol>

          <div className="flex min-h-[160px] flex-1 flex-col border-t border-line">
            <h3 className="shrink-0 px-4 pb-1 pt-3 text-sm font-semibold text-soft">Activity</h3>
            <div ref={feed} className="max-h-72 min-h-0 flex-1 overflow-y-auto px-4 pb-3 lg:max-h-none">
              <ul className="space-y-1.5 font-mono text-[12px] leading-relaxed">
                <AnimatePresence initial={false}>
                  {steps.map((s) => (
                    <motion.li
                      key={s.id}
                      initial={{ opacity: 0, x: -6 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ duration: 0.25, ease: "easeOut" }}
                      className="wrap-anywhere grid grid-cols-[auto_1fr] gap-x-3"
                    >
                      <span className="text-faint">{clock(s.ts)}</span>
                      <span>
                        <span className={s.status === "failed" ? "text-dead" : s.status === "running" ? "text-work" : s.agent === "Human" ? "text-alive" : "text-muted"}>
                          {s.agent}
                        </span>{" "}
                        <span className="text-soft">{s.message}</span>
                      </span>
                    </motion.li>
                  ))}
                </AnimatePresence>
              </ul>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
