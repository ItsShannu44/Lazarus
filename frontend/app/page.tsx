"use client";

import { MotionConfig, motion } from "motion/react";
import { useEffect, useState } from "react";
import { CountUp } from "@/components/CountUp";
import { ControlBar } from "@/components/DemoBar";
import { FixPanel } from "@/components/FixPanel";
import { Monitor } from "@/components/Monitor";
import { RepoDrawer } from "@/components/RepoDrawer";
import { RunList } from "@/components/RunList";
import { Timeline } from "@/components/Timeline";
import { DEFAULT_REPO } from "@/lib/api";
import { useLazarus } from "@/lib/useLazarus";
import { minutesLabel } from "@/lib/vitals";

export default function Dashboard() {
  const { runs, stats, selectedId, setSelectedId, detail, connected, setDetail, refreshStats, config, reloadConfig } =
    useLazarus();
  const [repo, setRepo] = useState(DEFAULT_REPO);
  const [reposOpen, setReposOpen] = useState(false);

  // keep the active repo one of the watched ones
  useEffect(() => {
    const watched = config?.repos || [];
    if (watched.length && !watched.includes(repo)) setRepo(watched[0]);
  }, [config?.repos, repo]);

  return (
    <MotionConfig reducedMotion="user">
      <main className="mx-auto flex min-h-screen max-w-[1760px] flex-col gap-3 p-3 sm:p-4 lg:h-screen lg:min-h-[680px] lg:gap-3.5 lg:p-5">
        <header className="flex min-w-0 flex-wrap items-center gap-x-6 gap-y-3 px-1">
          <Wordmark />

          <p className="flex min-w-0 items-center gap-2 text-sm text-muted" aria-live="polite">
            <span className={`h-2 w-2 shrink-0 rounded-full ${connected ? "bg-alive breathe" : "bg-dead"}`} />
            <span className="min-w-0 truncate">
              {!connected
                ? "Backend offline. Start it on port 8000."
                : config?.watch.enabled
                  ? config.repos.length === 0
                    ? "Connected. Choose repositories to watch."
                    : `Watching ${config.repos.length === 1 ? config.repos[0] : `${config.repos.length} repositories`}`
                  : "Connected. Watch mode is off."}
            </span>
          </p>

          <dl className="ml-auto flex shrink-0 items-end gap-6">
            <Stat label="Fixed" value={<CountUp value={stats.fixed} />} />
            <Stat label="Merged" value={<CountUp value={stats.merged} />} />
            <Stat label="Debugging saved" value={minutesLabel(stats.minutes_saved)} tone="text-work" />
          </dl>
        </header>

        <ControlBar
          config={config}
          repo={repo}
          setRepo={setRepo}
          onTriggered={setSelectedId}
          onOpenRepos={() => setReposOpen(true)}
        />
        <RepoDrawer open={reposOpen} onClose={() => setReposOpen(false)} onChanged={() => reloadConfig()} />

        <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[250px_minmax(0,1fr)_minmax(0,1.2fr)] lg:grid-rows-[auto_minmax(0,1fr)] lg:gap-3.5 xl:grid-cols-[280px_minmax(0,1fr)_minmax(0,1.25fr)]">
          <div className="min-h-0 lg:row-span-2 lg:flex lg:flex-col [&>*]:lg:flex-1">
            <RunList runs={runs} selectedId={selectedId} onSelect={setSelectedId} />
          </div>
          <div className="min-w-0 lg:col-span-2">
            <Monitor run={detail} />
          </div>
          <Timeline run={detail} />
          <FixPanel
            run={detail}
            onChange={(r) => {
              setDetail((d) => (d && d.id === r.id ? { ...d, ...r } : d));
              refreshStats();
            }}
          />
        </div>
      </main>
    </MotionConfig>
  );
}

/** Wordmark: the name rides a single heartbeat. */
function Wordmark() {
  return (
    <div className="flex shrink-0 items-center gap-2.5">
      <svg viewBox="0 0 40 20" className="h-5 w-10 text-work" aria-hidden>
        <motion.path
          d="M0 12 L12 12 L15 9 L18 12 L20 12 L23 2 L27 18 L30 12 L40 12"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 1.2, ease: "easeInOut" }}
        />
      </svg>
      <span className="wide text-[22px] font-[800] tracking-[-0.02em] text-work">Lazarus</span>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="text-right">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className={`readout text-2xl font-semibold leading-none ${tone || "text-bone"}`}>{value}</dd>
    </div>
  );
}
