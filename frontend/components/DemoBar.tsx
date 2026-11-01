"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Config } from "@/lib/types";

const BREAK_LABEL: Record<string, string> = {
  test: "Fail a test",
  dep: "Pin a bad dependency",
  lint: "Add a lint error",
};

type Toast = { ok: boolean; text: string; id: number; href?: string; linkText?: string; sticky?: boolean };

/** Which watched repo the buttons act on, a manual "fix the latest failure", and the stage-demo buttons. */
export function ControlBar({
  config, repo, setRepo, onTriggered, onOpenRepos,
}: {
  config: Config | null;
  repo: string;
  setRepo: (r: string) => void;
  onTriggered: (runId: string) => void;
  onOpenRepos: () => void;
}) {
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState<Toast | null>(null);
  const target = repo || config?.repos[0] || "";
  const watching = !!config?.watch.enabled;
  // the Break buttons only make sense on a copy of demo-app
  const [demo, setDemo] = useState<{ repo: string; ok: boolean; reason: string } | null>(null);
  useEffect(() => {
    if (!target || !config?.demo_controls) return;
    let live = true;
    api
      .demoCheck(target)
      .then((r) => live && setDemo({ repo: target, ok: r.supported, reason: r.reason }))
      .catch(() => live && setDemo({ repo: target, ok: true, reason: "" }));
    return () => {
      live = false;
    };
  }, [target, config?.demo_controls]);
  const demoReady = demo?.repo === target && demo.ok;

  // Lazarus only sees failures through GitHub Actions: warn when the repo has no CI
  const [ci, setCi] = useState<{ repo: string; has: boolean; names: string[]; disabled: string[]; write: boolean | null } | null>(null);
  useEffect(() => {
    if (!target) return;
    let live = true;
    api
      .repoCi(target)
      .then((r) => live && setCi({ repo: target, has: r.has_ci, names: r.workflows, disabled: r.disabled || [], write: r.can_write ?? null }))
      .catch(() => live && setCi(null));
    return () => {
      live = false;
    };
  }, [target]);
  const noCi = ci?.repo === target && !ci.has;
  const actionsOff = noCi && (ci?.disabled.length || 0) > 0;
  const readOnly = ci?.repo === target && ci.write === false;

  useEffect(() => {
    if (!toast || toast.sticky) return;
    const t = setTimeout(() => setToast(null), toast.ok ? 5000 : 9000);
    return () => clearTimeout(t);
  }, [toast]);

  const say = (ok: boolean, text: string, href?: string, linkText?: string, sticky?: boolean) =>
    setToast({ ok, text, id: Date.now(), href, linkText, sticky });

  async function act(kind: string) {
    setBusy(kind);
    try {
      if (kind === "trigger") {
        if (!target) throw new Error("Choose a repository to watch first.");
        const run = await api.trigger(target);
        onTriggered(run.id);
        say(true, `Started run ${run.number} on the latest failure.`);
      } else if (kind === "setup-ci") {
        const r = await api.setupCi(target);
        if (r.pr_url) {
          say(
            true,
            `Opened a pull request that adds CI (${r.checks.join("; ")}). Merge it, then push a change: Lazarus watches from there.`,
            r.pr_url,
            "Open the pull request",
          );
        } else {
          say(
            true,
            `${r.reason} You can add it yourself in one click: GitHub opens with the workflow already filled in. Press "Commit changes", then push a change.`,
            r.manual_url,
            "Create the file on GitHub",
            true,
          );
        }
      } else if (kind === "reset") {
        const r = await api.demoReset(target);
        say(true, r.changed.length ? "Reset the demo repo. CI will go green in about a minute." : "The demo repo is already green.");
      } else {
        await api.demoBreak(kind, target);
        say(
          true,
          watching
            ? "Bug pushed. CI fails in about a minute, then Lazarus wakes up on its own."
            : "Bug pushed. When CI turns red, press Fix latest failure.",
        );
      }
    } catch (e: any) {
      say(false, e.message || String(e));
    } finally {
      setBusy("");
    }
  }

  const ghost =
    "whitespace-nowrap rounded-lg border border-line px-3 py-1.5 text-[13px] text-soft transition-colors duration-150 disabled:opacity-40";

  return (
    <>
      <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-2xl border border-line bg-panel px-3 py-2.5 sm:px-4">
        <motion.button
          whileTap={{ scale: 0.97 }}
          onClick={onOpenRepos}
          className={`whitespace-nowrap rounded-lg border px-3 py-1.5 text-[13px] font-medium transition-colors ${
            config?.repos.length ? "border-line text-soft hover:text-bone" : "border-work text-work hover:bg-work/10"
          }`}
        >
          {config?.repos.length ? `Repositories (${config.repos.length})` : "Choose repositories"}
        </motion.button>
        {!!config?.repos.length && (
          <>
            <label className="sr-only" htmlFor="repo">Repository the buttons act on</label>
            <select
              id="repo"
              value={target}
              onChange={(e) => setRepo(e.target.value)}
              className="w-full min-w-0 cursor-pointer rounded-lg border border-line bg-glass px-2.5 py-1.5 font-mono text-[13px] text-bone outline-none transition-colors focus:border-work sm:w-auto sm:max-w-[16rem]"
            >
              {config.repos.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </>
        )}
        <motion.button
          whileTap={{ scale: 0.97 }}
          onClick={() => act("trigger")}
          disabled={!!busy}
          className="whitespace-nowrap rounded-lg bg-work px-3.5 py-1.5 text-[13px] font-semibold text-black transition-[filter] hover:brightness-110 disabled:opacity-50"
        >
          {busy === "trigger" ? "Starting…" : "Fix latest failure"}
        </motion.button>

        {readOnly && !noCi && (
          <a
            href="https://github.com/settings/personal-access-tokens"
            target="_blank"
            className="text-[13px] text-work underline-offset-4 hover:underline sm:ml-auto"
            title="Edit the token: Repository access → add this repo; Contents and Pull requests → Read and write. Paused fixes continue by themselves."
          >
            The token can read this repo but not push to it. Add it to the token&apos;s repository access.
          </a>
        )}
        {actionsOff && (
          <a
            href={`https://github.com/${target}/settings/actions`}
            target="_blank"
            className="text-[13px] text-work underline-offset-4 hover:underline sm:ml-auto"
            title={`Disabled workflows: ${ci?.disabled.join(", ")}`}
          >
            GitHub Actions is turned off in this repo, so CI never runs. Turn it on in Settings → Actions.
          </a>
        )}
        {noCi && !actionsOff && (
          <div className="flex min-w-0 flex-wrap items-center gap-2 sm:ml-auto">
            <span className="text-[13px] text-work" title="Lazarus wakes up when GitHub Actions fails. This repository has no workflow, so failures are invisible.">
              No CI in this repo, so Lazarus can&apos;t see its failures.
            </span>
            <motion.button
              whileTap={{ scale: 0.97 }}
              disabled={!!busy}
              onClick={() => act("setup-ci")}
              className="whitespace-nowrap rounded-lg border border-work px-3 py-1.5 text-[13px] font-semibold text-work transition-colors hover:bg-work/10 disabled:opacity-50"
            >
              {busy === "setup-ci" ? "Opening PR…" : "Set up CI"}
            </motion.button>
          </div>
        )}
        {!noCi && !readOnly && config?.demo_controls && demo?.repo === target && !demo.ok && (
          <span className="text-[13px] text-muted sm:ml-auto" title={demo.reason}>
            Stage demo needs a copy of demo-app. Failures here are still fixed automatically.
          </span>
        )}
        {!noCi && config?.demo_controls && demoReady && (
          <div className="flex min-w-0 flex-wrap items-center gap-2 sm:ml-auto">
            <span className="text-[13px] text-muted">Stage demo</span>
            {config.demo_bugs.map((b) => (
              <motion.button
                key={b.id}
                whileTap={{ scale: 0.97 }}
                disabled={!!busy || !target}
                onClick={() => act(b.id)}
                title={`Push a commit with a ${b.label.toLowerCase()} to ${target}`}
                className={`${ghost} hover:border-dead/50 hover:text-dead`}
              >
                {busy === b.id ? "Pushing…" : BREAK_LABEL[b.id] || b.label}
              </motion.button>
            ))}
            <motion.button
              whileTap={{ scale: 0.97 }}
              disabled={!!busy || !target}
              onClick={() => act("reset")}
              className={`${ghost} hover:border-work/60 hover:text-work`}
            >
              {busy === "reset" ? "Resetting…" : "Reset to green"}
            </motion.button>
          </div>
        )}
      </div>

      <div aria-live="polite" className="pointer-events-none fixed inset-x-3 bottom-4 z-50 flex justify-center sm:inset-x-auto sm:right-5">
        <AnimatePresence>
          {toast && (
            <motion.div
              key={toast.id}
              initial={{ opacity: 0, y: 16, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98 }}
              transition={{ type: "spring", stiffness: 420, damping: 32 }}
              className={`pointer-events-auto flex max-w-md items-start gap-3 rounded-xl border px-4 py-3 text-sm shadow-[0_16px_40px_-12px_rgb(0_0_0/0.8)] ${
                toast.ok ? "border-line-strong bg-panel-2 text-bone" : "border-dead/40 bg-[#1f1110] text-bone"
              }`}
            >
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${toast.ok ? "bg-work" : "bg-dead"}`} />
              <span className="wrap-anywhere leading-snug">
                {toast.text}
                {toast.href && (
                  <a href={toast.href} target="_blank" className="mt-1 block font-semibold text-work underline-offset-4 hover:underline">
                    {toast.linkText || "Open"}
                  </a>
                )}
              </span>
              <button onClick={() => setToast(null)} className="ml-1 shrink-0 text-muted hover:text-bone" aria-label="Dismiss">
                ×
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </>
  );
}
