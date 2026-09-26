import { useEffect, useState } from "react";
import { api, AppMetrics } from "../api";
import { badge } from "./badges";

// Auto-rendered per-type metrics panel (spec 08 §9): the metric list comes from
// the application's TypeSpec, so new types render without UI changes.
export default function MetricsPanel({ apps }: { apps: { id: string; name: string }[] }) {
  const [data, setData] = useState<Record<string, AppMetrics>>({});
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    const load = async () => {
      const out: Record<string, AppMetrics> = {};
      for (const a of apps) {
        try {
          out[a.id] = await api.appMetrics(a.id);
        } catch {
          /* app may be unknown */
        }
      }
      if (live) {
        setData(out);
        setErr(null);
      }
    };
    load();
    const h = setInterval(load, 3000);
    return () => {
      live = false;
      clearInterval(h);
    };
  }, [apps.map((a) => a.id).join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-zinc-400">
        Application Metrics
      </h2>
      {err && <div className="text-sm text-red-400">{err}</div>}
      <div className="grid gap-4 md:grid-cols-2">
        {apps.map((a) => (
          <AppMetricsCard key={a.id} app={a} m={data[a.id]} />
        ))}
      </div>
    </section>
  );
}

function AppMetricsCard({
  app,
  m,
}: {
  app: { id: string; name: string };
  m?: AppMetrics;
}) {
  if (!m) return null;
  const byId = Object.fromEntries(m.metrics.map((x) => [x.id, x]));
  const latency = [
    byId["p50_latency_sec"] ?? byId["p50_wait_sec"],
    byId["p95_latency_sec"] ?? byId["p95_wait_sec"],
    byId["p99_latency_sec"] ?? byId["p99_wait_sec"],
  ];
  const tokens = byId["total_tokens"]?.value != null;
  const activeTime = m.active_time || {};

  return (
    <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-medium text-zinc-200">{app.name}</span>
        <span className="flex items-center gap-1.5 text-xs">
          {badge(m.state.residency_state)}
          <span className="text-zinc-500">{m.type}</span>
        </span>
      </div>
      <div className="grid grid-cols-3 gap-2 text-xs">
        {m.metrics
          .filter((x) => x.value != null)
          .map((x) => (
            <div key={x.id} className="rounded bg-zinc-900/60 px-2 py-1.5">
              <div className="text-zinc-500">{x.name}</div>
              <div className="text-sm text-zinc-200">
                {fmt(x.value)}
                {x.unit ? <span className="ml-0.5 text-zinc-500">{x.unit}</span> : null}
              </div>
            </div>
          ))}
      </div>
      {latency.some((x) => x?.value != null) && (
        <div className="mt-2 flex gap-4 text-xs text-zinc-400">
          <span>
            Wait P50/P95/P99:{" "}
            <span className="text-zinc-200">
              {[latency[0], latency[1], latency[2]]
                .map((x) => (x?.value != null ? `${(x.value as number).toFixed(2)}s` : "—"))
                .join(" / ")}
            </span>
          </span>
        </div>
      )}
      {tokens && <div className="mt-1 text-xs text-zinc-500">token metrics active</div>}
      {activeTime.total_active_time != null && activeTime.total_active_time > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
          <span>active {fmtDur(activeTime.total_active_time)}</span>
          <span>processing {fmtDur(activeTime.processing_time ?? 0)}</span>
          <span>idle-loaded {fmtDur(activeTime.idle_loaded_time ?? 0)}</span>
        </div>
      )}
    </div>
  );
}

function fmt(v: number | string | null): string {
  if (v == null) return "—";
  if (typeof v === "number") return v >= 100 ? v.toFixed(0) : v.toFixed(1);
  return v;
}

function fmtDur(sec: number): string {
  if (sec < 60) return `${sec.toFixed(0)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}
