"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL, api } from "./api";
import type { Config, Run, Stats, Step, StreamEvent } from "./types";

const EMPTY_STATS: Stats = { total_runs: 0, fixed: 0, merged: 0, needs_human: 0, minutes_saved: 0 };

/** Live state of all Lazarus runs, kept in sync over Server-Sent Events. */
export function useLazarus() {
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [stats, setStats] = useState<Stats>(EMPTY_STATS);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Run | null>(null);
  const [connected, setConnected] = useState(false);
  const [config, setConfig] = useState<Config | null>(null);
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedId;

  const refreshStats = useCallback(() => {
    api.stats().then(setStats).catch(() => {});
  }, []);
  const reloadConfig = useCallback(() => {
    api.config().then(setConfig).catch(() => {});
  }, []);

  // Live feed
  useEffect(() => {
    const es = new EventSource(`${API_URL}/api/stream`);
    es.onopen = () => {
      setConnected(true);
      api.config().then(setConfig).catch(() => {});
    };
    es.onerror = () => setConnected(false); // EventSource reconnects on its own

    es.addEventListener("snapshot", (e) => {
      const snap = JSON.parse((e as MessageEvent).data) as Extract<StreamEvent, { type: "snapshot" }>;
      setRuns(Object.fromEntries(snap.runs.map((r) => [r.id, r])));
      setStats(snap.stats);
      setSelectedId((cur) => cur ?? snap.runs[0]?.id ?? null);
    });

    es.onmessage = (e) => {
      const ev = JSON.parse(e.data) as StreamEvent;
      if (ev.type === "snapshot") return;
      const run = ev.run;
      let isNew = false;
      setRuns((prev) => {
        isNew = !prev[run.id];
        return { ...prev, [run.id]: run };
      });
      // A brand-new incident takes over the screen: that's the live demo moment.
      if (isNew) setSelectedId(run.id);

      if (ev.type === "step") {
        const step: Step = ev.step;
        setDetail((d) => {
          if (!d || d.id !== run.id) return d;
          const steps = d.steps || [];
          if (steps.some((s) => s.id === step.id)) return { ...d, ...run, steps };
          return { ...d, ...run, steps: [...steps, step] };
        });
      } else {
        setDetail((d) => (d && d.id === run.id ? { ...d, ...run } : d));
        refreshStats();
        // status changed: pull the full record (attempts, CI log) for the open run
        if (selectedRef.current === run.id) {
          api.run(run.id).then((full) => setDetail((d) => (d && d.id === full.id ? full : d))).catch(() => {});
        }
      }
    };
    return () => es.close();
  }, [refreshStats]);

  // Full detail (with steps) for the selected run
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    api
      .run(selectedId)
      .then((r) => !cancelled && setDetail(r))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const runList = Object.values(runs).sort((a, b) => b.created_at - a.created_at);

  return { runs: runList, stats, selectedId, setSelectedId, detail, connected, refreshStats, setDetail, config, reloadConfig };
}
