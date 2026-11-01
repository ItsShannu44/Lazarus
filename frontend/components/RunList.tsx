"use client";

import { AnimatePresence, motion } from "motion/react";
import { STATUS_LABEL, errorLabel, timeAgo } from "@/lib/api";
import type { Run } from "@/lib/types";
import { VITAL_BG, VITAL_TEXT, vitalOf } from "@/lib/vitals";

const SPRING = { type: "spring", stiffness: 520, damping: 42 } as const;

export function RunList({
  runs, selectedId, onSelect,
}: { runs: Run[]; selectedId: string | null; onSelect: (id: string) => void }) {
  return (
    <section
      aria-label="Incidents"
      className="flex max-h-[420px] min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-line bg-panel lg:max-h-none"
    >
      <header className="flex shrink-0 items-baseline justify-between border-b border-line px-4 py-3">
        <h2 className="text-[15px] font-semibold">Incidents</h2>
        <span className="text-xs text-muted">{runs.length || "none yet"}</span>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto py-1.5">
        {runs.length === 0 && (
          <p className="px-4 py-6 text-sm leading-relaxed text-muted">
            No broken builds. Choose repositories to watch, or use a Break button to push a bug to the demo repo.
          </p>
        )}
        <ul>
          <AnimatePresence initial={false}>
            {runs.map((r) => {
              const v = vitalOf(r.status);
              const active = r.id === selectedId;
              return (
                <motion.li
                  key={r.id}
                  layout="position"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
                  className="relative px-1.5"
                >
                  <button
                    onClick={() => onSelect(r.id)}
                    aria-current={active ? "true" : undefined}
                    className="relative flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition-colors duration-150 hover:bg-panel-2/70"
                  >
                    {active && (
                      <motion.span
                        layoutId="run-selection"
                        transition={SPRING}
                        className="absolute inset-0 rounded-xl border border-work/40 bg-panel-2"
                      />
                    )}
                    <span className="relative mt-[7px] flex h-2.5 w-2.5 shrink-0">
                      {v === "working" && (
                        <span className={`absolute inset-0 animate-ping rounded-full ${VITAL_BG[v]} opacity-60 motion-reduce:hidden`} />
                      )}
                      <span className={`relative h-2.5 w-2.5 rounded-full ${VITAL_BG[v]}`} />
                    </span>
                    <span className="relative min-w-0 flex-1">
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="font-semibold">Run {r.number}</span>
                        <span className="shrink-0 text-xs text-muted">{timeAgo(r.created_at)}</span>
                      </span>
                      <span className="block truncate text-sm text-soft" title={r.repo}>
                        {r.error_type ? errorLabel(r.error_type) : r.workflow_name || "CI failed"}
                        <span className="text-muted"> in {r.repo.split("/")[1]}</span>
                      </span>
                      <span className="mt-0.5 flex min-w-0 items-center gap-2 text-xs">
                        <span className={`shrink-0 whitespace-nowrap font-medium ${VITAL_TEXT[v]}`}>{STATUS_LABEL[r.status]}</span>
                        {r.trigger === "watch" && <span className="shrink-0 text-faint">found automatically</span>}
                        {r.trigger === "webhook" && <span className="shrink-0 text-faint">via webhook</span>}
                      </span>
                    </span>
                  </button>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
      </div>
    </section>
  );
}
