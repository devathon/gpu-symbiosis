import { EventRow } from "../api";

export default function EventFeed({ events }: { events: EventRow[] }) {
  const label = (e: EventRow) => {
    if (e.kind === "scheduler") return `${e.decision}${e.candidate ? ` → ${e.candidate}` : ""}`;
    return e.event ?? e.trigger ?? "?";
  };
  // Hide scheduler noise, showing a decision only when the state actually
  // changes (spec 06: waiting is the steady state). Two rules:
  //  - the per-second no_candidate reconcile is pure spam;
  //  - repeated "wait → same model" is spam too — keep only the first one
  //    (the state change), not every re-evaluation of the same wait.
  const isWait = (e: EventRow) => e.kind === "scheduler" && e.decision === "wait";
  const shown = (() => {
    const oldest = events.slice().reverse(); // work oldest→newest
    const kept: EventRow[] = [];
    for (const e of oldest) {
      if (e.kind === "scheduler" && e.decision === "no_candidate") continue;
      const prev = kept[kept.length - 1];
      if (isWait(e) && prev && isWait(prev) && prev.candidate === e.candidate) continue;
      kept.push(e);
    }
    return kept.reverse(); // back to newest-first
  })();
  return (
    <section className="flex max-h-96 flex-col rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-xs uppercase tracking-wider text-zinc-500">Event log</h2>
      <div className="flex-1 space-y-1 overflow-y-auto font-mono text-xs">
        {shown.map((e, i) => (
          <div key={i} className="-mx-1.5 flex gap-2 rounded px-1.5 py-0.5 transition-colors hover:bg-zinc-800/50">
            <span className="shrink-0 text-zinc-600">{new Date(e.at).toLocaleTimeString()}</span>
            <span className={toneFor(e.kind)}>{e.kind}</span>
            <span className="truncate text-zinc-300">{label(e)}</span>
          </div>
        ))}
        {shown.length === 0 && <p className="text-zinc-600">no events yet</p>}
      </div>
    </section>
  );
}

const toneFor = (kind: string) =>
  kind === "scheduler" ? "text-sky-400" : kind === "gateway" ? "text-amber-400" : "text-emerald-400";
