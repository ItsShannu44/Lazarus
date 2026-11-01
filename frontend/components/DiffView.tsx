/** Unified diff, one file block per file. Long lines scroll sideways inside the block. */
export function DiffView({ diff }: { diff: string }) {
  if (!diff) return null;
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="max-w-full overflow-x-auto rounded-xl border border-line bg-glass py-2.5 font-mono text-[12.5px] leading-[1.7]">
      <div className="w-max min-w-full">
        {lines.map((l, i) => {
          if (l.startsWith("+++")) return null;
          if (l.startsWith("---"))
            return (
              <div key={i} className="mb-1 mt-3 px-3.5 text-xs font-semibold text-soft first:mt-0">
                {l.replace(/^--- a\//, "")}
              </div>
            );
          if (l.startsWith("@@"))
            return (
              <div key={i} className="px-3.5 text-faint">
                {l}
              </div>
            );
          const add = l.startsWith("+");
          const del = l.startsWith("-");
          return (
            <div
              key={i}
              className={`grid grid-cols-[1.25rem_1fr] whitespace-pre px-3.5 ${add ? "bg-alive/[0.07] text-alive" : del ? "bg-dead/[0.06] text-dead" : "text-soft"}`}
            >
              <span className="select-none opacity-70">{add ? "+" : del ? "−" : ""}</span>
              <span>{add || del ? l.slice(1) : l.slice(1) || " "}</span>
            </div>
          );
        })}
      </div>
    </pre>
  );
}
