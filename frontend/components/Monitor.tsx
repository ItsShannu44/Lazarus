"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { AgentName, Run } from "@/lib/types";
import { VITAL_HEX, VITAL_TEXT, type Vital, duration, headline, vitalOf } from "@/lib/vitals";
import { CountUp } from "./CountUp";

const W = 1200;
const BASE = 66;
// one PQRST complex: [dx, dy]
const COMPLEX: [number, number][] = [
  [0, 0], [7, -5], [14, 0], [22, 0], [25, 7], [30, -52], [35, 22], [39, 0], [50, 0], [58, -9], [66, 0],
];
const AGENTS: AgentName[] = ["Log Analyst", "Code Locator", "Fixer", "Verifier", "PR Agent"];

function tracePath(beats: number[], amp = 1) {
  let d = `M0 ${BASE}`;
  for (const x of beats) for (const [dx, dy] of COMPLEX) d += ` L${x + dx} ${BASE + dy * amp}`;
  return `${d} L${W} ${BASE}`;
}

function beatsFor(vital: Vital, run: Run | null): { beats: number[]; amp: number } {
  if (!run || vital === "dead") return { beats: [], amp: 1 };
  if (vital === "alive") return { beats: Array.from({ length: 8 }, (_, i) => 40 + i * 150), amp: 1 };
  if (vital === "rest") return { beats: Array.from({ length: 4 }, (_, i) => 120 + i * 300), amp: 0.35 };
  // working: one beat per agent that has finished, then the line runs flat ahead of the sweep
  const done = AGENTS.filter((a) => (run.steps || []).some((s) => s.agent === a && s.status === "done")).length;
  return { beats: Array.from({ length: done }, (_, i) => 40 + i * 170), amp: 0.85 };
}

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

export function Monitor({ run }: { run: Run | null }) {
  const reduce = useReducedMotion();
  const vital: Vital = run ? vitalOf(run.status) : "rest";
  const color = VITAL_HEX[vital];
  const { beats, amp } = beatsFor(vital, run);
  const d = useMemo(() => tracePath(beats, amp), [beats.join(","), amp]); // eslint-disable-line react-hooks/exhaustive-deps
  const now = useNow(vital === "working");
  const elapsed = run ? (run.finished_at ?? now) - run.created_at : 0;
  const attempts = run?.attempts?.length || (run?.steps || []).filter((s) => s.agent === "Fixer" && s.status === "done").length;
  const tests = run?.tests_after?.total ? run.tests_after : null;

  // one celebratory pulse when a run is merged while you watch
  const prev = useRef<string | undefined>(run?.status);
  const [revived, setRevived] = useState(0);
  useEffect(() => {
    if (prev.current && prev.current !== "merged" && run?.status === "merged") setRevived((n) => n + 1);
    prev.current = run?.status;
  }, [run?.status]);

  const sweeping = !reduce && (vital === "working" || vital === "alive");

  return (
    <section
      aria-label="Run vitals"
      className="relative min-w-0 overflow-hidden rounded-2xl border border-line bg-panel"
    >
      <div className="flex flex-wrap items-end gap-x-8 gap-y-4 px-5 pt-4">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1 text-sm text-muted">
            {run ? (
              <>
                <span className="font-semibold text-soft">Run {run.number}</span>
                <span className="min-w-0 truncate" title={run.repo}>{run.repo}</span>
                {run.head_branch && (
                  <span className="rounded-md bg-panel-2 px-1.5 py-0.5 font-mono text-xs text-soft">{run.head_branch}</span>
                )}
              </>
            ) : (
              <span>No run selected</span>
            )}
          </div>
          <AnimatePresence mode="wait" initial={false}>
            <motion.h1
              key={run ? `${run.id}-${run.status}` : "none"}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
              className={`mt-1 text-[clamp(1.5rem,2.4vw,2.25rem)] font-[750] leading-tight tracking-[-0.015em] ${run ? VITAL_TEXT[vital] : "text-muted"}`}
            >
              {run ? headline(run) : "Waiting for a broken build"}
            </motion.h1>
          </AnimatePresence>
          {run && (run.status === "needs_human" || run.status === "error" || run.status === "waiting") && run.escalation_reason ? (
            <p
              className={`mt-1 line-clamp-2 max-w-[80ch] text-[15px] ${run.status === "waiting" ? "text-work/90" : "text-dead/90"}`}
              title={run.escalation_reason}
            >
              {run.status === "waiting" ? "Paused: " : "Why: "}
              {run.escalation_reason}
            </p>
          ) : run?.summary ? (
            <p className="mt-1 line-clamp-1 max-w-[70ch] text-[15px] text-soft" title={run.summary}>
              {run.summary}
            </p>
          ) : null}
        </div>

        {run && (
          <dl className="flex shrink-0 gap-6 pb-0.5">
            <Readout label="Elapsed" value={duration(elapsed)} />
            <Readout
              label="Confidence"
              value={run.confidence ? <CountUp value={run.confidence} suffix="%" /> : "–"}
            />
            <Readout
              label="Tests"
              value={tests ? <><CountUp value={tests.passed || 0} />/{tests.total}</> : "–"}
              tone={tests ? "text-alive" : undefined}
            />
            <Readout label="Attempts" value={attempts || "–"} />
          </dl>
        )}
      </div>

      {/* the trace */}
      <div className="relative mt-3 h-[96px] monitor-grid">
        <svg viewBox={`0 0 ${W} 110`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full" aria-hidden>
          <defs>
            <linearGradient id="eraser" x1="0" x2="1">
              <stop offset="0" stopColor="#141414" stopOpacity="0" />
              <stop offset="0.35" stopColor="#141414" stopOpacity="1" />
              <stop offset="1" stopColor="#141414" stopOpacity="1" />
            </linearGradient>
          </defs>
          <g className={vital === "dead" ? "breathe" : undefined}>
            <motion.path
              key={`glow-${d}-${vital}`}
              d={d}
              fill="none"
              stroke={color}
              strokeOpacity={0.22}
              strokeWidth={7}
              initial={reduce ? false : { pathLength: 0 }}
              animate={{ pathLength: 1 }}
              transition={{ duration: 1.1, ease: "easeInOut" }}
              vectorEffect="non-scaling-stroke"
            />
            <motion.path
              key={`line-${d}-${vital}`}
              d={d}
              fill="none"
              stroke={color}
              strokeWidth={1.75}
              strokeLinejoin="round"
              strokeLinecap="round"
              initial={reduce ? false : { pathLength: 0 }}
              animate={{ pathLength: 1 }}
              transition={{ duration: 1.1, ease: "easeInOut" }}
              vectorEffect="non-scaling-stroke"
            />
          </g>
          {sweeping && (
            <motion.g
              key={vital}
              initial={{ x: -120 }}
              animate={{ x: W + 20 }}
              transition={{ duration: vital === "working" ? 2.2 : 3.6, ease: "linear", repeat: Infinity }}
            >
              <rect x={0} y={0} width={90} height={110} fill="url(#eraser)" />
              <rect x={30} y={0} width={1} height={110} fill={color} opacity={0.35} />
            </motion.g>
          )}
        </svg>
        {vital === "dead" && run && (
          <span className="absolute right-5 top-2 text-xs font-medium text-dead">No pulse</span>
        )}
        <AnimatePresence>
          {revived > 0 && (
            <motion.div
              key={revived}
              className="pointer-events-none absolute inset-0 bg-alive"
              initial={{ opacity: 0.22 }}
              animate={{ opacity: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 1.4, ease: "easeOut" }}
            />
          )}
        </AnimatePresence>
      </div>
    </section>
  );
}

function Readout({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="min-w-[3.5rem]">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className={`readout text-[1.75rem] font-semibold leading-none ${tone || "text-bone"}`}>{value}</dd>
    </div>
  );
}
