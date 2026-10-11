import { Fragment, useEffect, useState } from "react";
import { api, AppMetrics, MetricItem } from "../api";
import { badge, appDisplayState, appResidencyDisplay, showAppResidency } from "./badges";

// Per-application metrics card. Rendered from the type's spec view (spec 08
// §9): groups in first-appearance order, one tile per field — the
// application type dictates its own layout (spec 04 §8). Values are bare
// numbers (no unit suffix) with k/m/b suffixes for large values; the latency
// percentiles are condensed into "Wait (P50/95/99)" / "Service (P50/95/99)";
// the requests group ends with the state-driven "Active" tile (active +
// queued). GPU memory/utilisation live in the applications list above.

// Latency fields condensed into the composite tiles (not rendered individually).
const LATENCY_COMPOSITE = new Set([
  "p50_wait_sec",
  "p95_wait_sec",
  "p99_wait_sec",
  "p50_service_sec",
  "p95_service_sec",
  "p99_service_sec",
  "avg_wait_sec",
  "avg_service_sec",
]);
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
  const raw = m.raw || {};
  const state = m.state || {};
  const activeTime = m.active_time || {};

  const active = state.active_requests ?? 0;
  const queued = state.queued_requests ?? 0;

  // Groups in first-appearance order of the type's spec view (spec 08 §9).
  const groups: [string, MetricItem[]][] = [];
  for (const item of m.metrics || []) {
    const g = item.group || "metrics";
    let slot = groups.find(([name]) => name === g);
    if (!slot) {
      slot = [g, []];
      groups.push(slot);
    }
    slot[1].push(item);
  }

  return (
    <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2 text-sm">
          <span className="truncate font-medium text-zinc-200">
            {app.name}
            {m.type && <span className="font-normal text-zinc-500"> ({m.type})</span>}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5 text-xs">
          {badge(appDisplayState(state.process_state, state.active))}
          {showAppResidency(state.process_state, state.supports_load_unload) && badge(appResidencyDisplay(state.residency_state))}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-2 text-xs">
        {groups.map(([g, items]) => (
          <Fragment key={g}>
            {g === "latency" && raw.p50_wait_sec != null && (
              <Metric
                label="Wait (P50/95/99)"
                value={`${fmt(raw.p50_wait_sec)} / ${fmt(raw.p95_wait_sec)} / ${fmt(raw.p99_wait_sec)}`}
              />
            )}
            {g === "latency" && raw.p50_service_sec != null && (
              <Metric
                label="Service (P50/95/99)"
                value={`${fmt(raw.p50_service_sec)} / ${fmt(raw.p95_service_sec)} / ${fmt(raw.p99_service_sec)}`}
              />
            )}
            {items.filter((it) => !LATENCY_COMPOSITE.has(it.id)).map((it) => (
              <Metric key={it.id} label={it.name} value={fmt(it.value)} />
            ))}
            {g === "requests" && (
              <Metric
                label="Active Requests"
                value={queued > 0 ? `${active} + ${queued} queued` : `${active}`}
              />
            )}
          </Fragment>
        ))}
      </div>
      {activeTime.total_active_time != null && activeTime.total_active_time > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
          <span>active {fmtDur(activeTime.total_active_time)}</span>
          <span>processing {fmtDur(activeTime.processing_time ?? 0)}</span>
          <span>idle {fmtDur(activeTime.idle_loaded_time ?? 0)}</span>
        </div>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-zinc-900/60 px-2 py-1.5">
      <div className="text-zinc-500">{label}</div>
      <div className="text-sm text-zinc-200">{value}</div>
    </div>
  );
}

// Bare number, no unit suffix. Large values get a k (1e3) / m (1e6) / b (1e9)
// suffix; values under 1000 are shown as-is (integers without decimals, others
// with up to 2 significant decimals).
function fmt(v: unknown): string {
  if (v == null) return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (Number.isNaN(n)) return String(v);
  const abs = Math.abs(n);
  if (abs >= 1e9) return trimZeros((n / 1e9).toFixed(2)) + "b";
  if (abs >= 1e6) return trimZeros((n / 1e6).toFixed(2)) + "m";
  if (abs >= 1000) return trimZeros((n / 1000).toFixed(2)) + "k";
  if (Number.isInteger(n)) return n.toString();
  return trimZeros(n.toFixed(2));
}

function trimZeros(s: string): string {
  return s.replace(/\.?0+$/, "");
}

function fmtDur(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}
