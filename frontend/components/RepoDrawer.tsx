"use client";

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, timeAgo } from "@/lib/api";
import type { RepoInfo } from "@/lib/types";

const EASE = [0.22, 1, 0.36, 1] as const;

function Switch({ on, disabled, onChange, label }: { on: boolean; disabled?: boolean; onChange: () => void; label: string }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onChange}
      className={`relative h-6 w-11 shrink-0 rounded-full border transition-colors duration-200 disabled:cursor-not-allowed disabled:opacity-40 ${
        on ? "border-work bg-work" : "border-line-strong bg-panel-2"
      }`}
    >
      <motion.span
        className={`absolute top-[3px] h-4 w-4 rounded-full ${on ? "bg-black" : "bg-muted"}`}
        animate={{ left: on ? 22 : 3 }}
        transition={{ type: "spring", stiffness: 600, damping: 34 }}
      />
    </button>
  );
}

/** Choose which repositories Lazarus watches. */
export function RepoDrawer({
  open, onClose, onChanged,
}: { open: boolean; onClose: () => void; onChanged: (watched: string[]) => void }) {
  const [repos, setRepos] = useState<RepoInfo[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError("");
    try {
      setRepos(await api.repos(refresh));
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  async function toggle(r: RepoInfo) {
    setPending(r.full_name);
    setError("");
    try {
      const { repos: watched } = await api.watchRepo(r.full_name, !r.watched);
      setRepos((list) => list?.map((x) => ({ ...x, watched: watched.includes(x.full_name) })) || list);
      onChanged(watched);
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setPending(null);
    }
  }

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = (repos || []).filter((r) => !q || r.full_name.toLowerCase().includes(q) || r.description.toLowerCase().includes(q));
    return [...list].sort((a, b) => Number(b.watched) - Number(a.watched));
  }, [repos, query]);
  const watchedCount = (repos || []).filter((r) => r.watched).length;

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="backdrop"
            className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[2px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={onClose}
          />
          <motion.aside
            key="drawer"
            role="dialog"
            aria-modal="true"
            aria-label="Repositories"
            className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[460px] flex-col border-l border-line bg-panel shadow-[-24px_0_60px_-20px_rgb(0_0_0/0.8)]"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: 0.35, ease: EASE }}
          >
            <header className="flex items-start gap-3 border-b border-line px-5 py-4">
              <div className="min-w-0 flex-1">
                <h2 className="text-lg font-semibold">Repositories</h2>
                <p className="mt-1 text-sm leading-relaxed text-muted">
                  Switch on the repositories Lazarus should look after. After every failed CI run in one of them, it wakes
                  up and fixes it on its own.
                </p>
              </div>
              <button onClick={onClose} aria-label="Close" className="rounded-lg px-2 py-1 text-xl leading-none text-muted hover:text-bone">
                ×
              </button>
            </header>

            <div className="flex items-center gap-2 border-b border-line px-5 py-3">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search repositories"
                aria-label="Search repositories"
                autoFocus
                className="min-w-0 flex-1 rounded-lg border border-line bg-glass px-3 py-2 text-sm outline-none placeholder:text-faint focus:border-work"
              />
              <button
                onClick={() => load(true)}
                disabled={loading}
                className="whitespace-nowrap rounded-lg border border-line px-3 py-2 text-sm text-soft hover:text-bone disabled:opacity-50"
              >
                {loading ? "Loading…" : "Refresh"}
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
              {error && <p className="mx-3 my-2 rounded-lg border border-dead/40 bg-dead/[0.08] p-3 text-sm text-bone">{error}</p>}
              {!repos && !error && (
                <ul className="space-y-2 px-3 py-2" aria-hidden>
                  {[0, 1, 2, 3].map((i) => (
                    <motion.li
                      key={i}
                      className="h-14 rounded-xl bg-panel-2"
                      animate={{ opacity: [0.4, 0.8, 0.4] }}
                      transition={{ duration: 1.4, repeat: Infinity, delay: i * 0.12 }}
                    />
                  ))}
                </ul>
              )}
              {repos && shown.length === 0 && (
                <p className="px-3 py-6 text-sm text-muted">
                  {query ? "No repository matches that search." : "This token can't see any repositories yet."}
                </p>
              )}
              <ul>
                <AnimatePresence initial={false}>
                  {shown.map((r) => {
                    const locked = !r.watched && (!r.can_push || r.missing);
                    return (
                      <motion.li
                        key={r.full_name}
                        layout="position"
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        exit={{ opacity: 0 }}
                        transition={{ duration: 0.25, ease: EASE }}
                        className="flex items-center gap-3 rounded-xl px-3 py-3 hover:bg-panel-2/60"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-center gap-2">
                            <span className="truncate font-medium" title={r.full_name}>
                              <span className="text-muted">{r.full_name.split("/")[0]}/</span>
                              {r.full_name.split("/")[1]}
                            </span>
                            {r.private && <span className="shrink-0 rounded border border-line px-1.5 text-[11px] text-muted">private</span>}
                          </div>
                          <div className="mt-0.5 flex min-w-0 gap-3 text-xs text-muted">
                            {r.missing ? (
                              <span>The token can no longer see this repository</span>
                            ) : !r.can_push ? (
                              <span>Read-only: the token needs write access to fix it</span>
                            ) : (
                              <>
                                {r.language && <span className="shrink-0 text-soft">{r.language}</span>}
                                {r.pushed_at && <span className="shrink-0">updated {timeAgo(Date.parse(r.pushed_at) / 1000)}</span>}
                                {r.description && <span className="min-w-0 truncate">{r.description}</span>}
                              </>
                            )}
                          </div>
                        </div>
                        <Switch
                          on={r.watched}
                          disabled={locked || pending === r.full_name}
                          onChange={() => toggle(r)}
                          label={`${r.watched ? "Stop watching" : "Watch"} ${r.full_name}`}
                        />
                      </motion.li>
                    );
                  })}
                </AnimatePresence>
              </ul>
            </div>

            <footer className="border-t border-line px-5 py-3 text-xs leading-relaxed text-muted">
              <span className="text-soft">{watchedCount} watched.</span> Missing a repository? Add it to your GitHub token&apos;s
              repository access (with Contents, Pull requests and Actions permissions), then press Refresh.
            </footer>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
