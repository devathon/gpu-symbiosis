import { EventRow } from "../api";

export default function EventFeed({ events }: { events: EventRow[] }) {
  const label = (e: EventRow) => {
    if (e.kind === "scheduler") return `${e.decision}${e.candidate ? ` → ${e.candidate}` : ""}`;
    return e.event ?? e.trigger ?? "?";
  };
  // Hide the per-second no_candidate reconcile spam (spec 06: waiting is the
  // steady state; only meaningful decisions are worth showing).
  const shown = events.filter((e) => !(e.kind === "scheduler" && e.decision === "no_candidate"));
  return (
    <section className="flex max-h-96 flex-col rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-xs uppercase tracking-wider text-zinc-500">Event log</h2>
      <div className="flex-1 space-y-1 overflow-y-auto font-mono text-xs">
        {shown.map((e, i) => (
          <div key={i} className="flex gap-2">
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
