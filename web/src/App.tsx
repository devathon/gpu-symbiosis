import { useEffect, useRef, useState } from "react";
import { api, AppView, Decision, EventRow, GpuSample, GwView, RequestRow, Status } from "./api";
import AppCards from "./components/AppCards";
import GatewayCards from "./components/GatewayCards";
import EventFeed from "./components/EventFeed";
import RequestsTable from "./components/RequestsTable";
import GpuChart from "./components/GpuChart";
import SchedulerExplainer from "./components/SchedulerExplainer";
import MetricsPanel from "./components/MetricsPanel";

const MAX_SAMPLES = 180;

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [decision, setDecision] = useState<Decision>({});
  const [samples, setSamples] = useState<{ t: number; vram: number; gpu: number }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const sseRef = useRef<EventSource | null>(null);

  // Status + requests + decision polling.
  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const s = await api.status();
        if (live) {
          setStatus(s);
          setError(null);
        }
      } catch (e) {
        if (live) setError(String(e));
      }
      try {
        const r = await api.requests(50);
        if (live) setRequests(r);
      } catch {
        /* non-fatal */
      }
      try {
        const d = await api.decision();
        if (live) setDecision(d);
      } catch {
        /* non-fatal */
      }
      try {
        const g = await api.gpu();
        if (live) {
          setSamples((prev) => [
            ...prev.slice(-(MAX_SAMPLES - 1)),
            { t: Date.now(), vram: g.vram_pct, gpu: g.gpu_pct },
          ]);
        }
      } catch {
        /* non-fatal */
      }
    };
    tick();
    const h = setInterval(tick, 2000);
    return () => {
      live = false;
      clearInterval(h);
    };
  }, []);

  // Live event feed over SSE (spec 11 §1); fall back to polling the event log.
  useEffect(() => {
    const es = new EventSource("/api/events/sse");
    sseRef.current = es;
    es.onmessage = (m) => {
      try {
        const raw = JSON.parse(m.data) as Record<string, unknown> & { type: string; at?: string };
        const data = { ...raw };
        const e: EventRow = {
          kind: data.application_id
            ? "application"
            : data.gateway_id
              ? "gateway"
              : "scheduler",
          event: raw.type,
          trigger: (data.trigger as string) ?? raw.type,
          decision: (data.decision as string) ?? raw.type,
          candidate: (data.candidate_application_id as string) ?? undefined,
          application_id: data.application_id as string | undefined,
          gateway_id: data.gateway_id as string | undefined,
          detail: (data.detail as string) ?? undefined,
          at: raw.at ?? new Date().toISOString(),
        };
        setEvents((prev) => [e, ...prev].slice(0, 200));
      } catch {
        /* ignore keepalive */
      }
    };
    es.onerror = () => {
      /* EventSource auto-reconnects; poll as a safety net */
      api.events(200).then(setEvents).catch(() => undefined);
    };
    return () => es.close();
  }, []);

  const apps = status ? Object.values(status.applications) : [];
  const gws = status ? Object.values(status.gateways) : [];
  const res = status?.resources;

  const act = (fn: () => Promise<void>) => {
    fn().catch((e) => setError(String(e)));
  };

  return (
    <div className="mx-auto max-w-7xl px-4 py-6">
      <header className="mb-6 flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">GPUSymbiosis</h1>
        <span className="text-sm text-zinc-500">
          Phase 1 · {res ? `${(res.used_mb / 1024).toFixed(1)} / ${(res.total_mb / 1024).toFixed(1)} GB` : "connecting…"}
        </span>
      </header>

      {error && (
        <div className="mb-4 rounded border border-red-800 bg-red-950 px-3 py-2 text-sm text-red-300">{error}</div>
      )}

      {res && (
        <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-400">
          <span className="font-medium text-zinc-200">{res.name || "GPU"}</span>
          {res.pcie && <span className="text-zinc-500">{res.pcie}</span>}
          <span>Rx: {fmtRate(res.rx_mbps)}</span>
          <span>Tx: {fmtRate(res.tx_mbps)}</span>
          <span>
            Pow: {fmtPow(res.power_w, res.power_limit_w)}
          </span>
        </div>
      )}

      {res && (
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Widget
            label="VRAM"
            value={`${(res.used_mb / 1024).toFixed(2)} GB`}
            valueCls="text-amber-400"
            corner={`${(res.total_mb / 1024).toFixed(1)} GB total`}
          />
          <Widget
            label="GPU"
            value={`${Math.round(res.gpu_pct)}%`}
            valueCls="text-emerald-400"
            topRight={res.temperature_c != null ? `${Math.round(res.temperature_c)}°C` : undefined}
            corner={res.clock_mhz != null ? `${Math.round(res.clock_mhz)} MHz` : undefined}
          />
          <Widget
            label="RAM"
            value={`${(res.ram_used_mb / 1024).toFixed(2)} GB`}
            corner={res.ram_total_mb > 0 ? `${(res.ram_total_mb / 1024).toFixed(1)} GB total` : undefined}
          />
          <Widget
            label="CPU"
            value={`${Math.round(res.cpu_pct)}%`}
            corner={res.cpu_cores > 0 ? `${res.cpu_cores} cores` : undefined}
          />
        </div>
      )}

      <div className="mb-6">
        <GpuChart samples={samples} />
      </div>

      <div className="mb-6 grid gap-4 lg:grid-cols-2">
        <AppCards apps={apps} act={act} />
        <GatewayCards gws={gws} act={act} />
      </div>

      <div className="mb-6">
        <SchedulerExplainer d={decision} />
      </div>

      <div className="mb-6">
        <MetricsPanel apps={apps.map((a) => ({ id: a.id, name: a.name }))} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <RequestsTable rows={requests} />
        <EventFeed events={events} />
      </div>
    </div>
  );
}

const Widget = ({
  label,
  value,
  valueCls = "text-zinc-200",
  topRight,
  corner,
}: {
  label: string;
  value: string;
  valueCls?: string;
  topRight?: string;
  corner?: string;
}) => (
  <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
    <div className="mb-1 flex items-center justify-between text-xs uppercase tracking-wider text-zinc-500">
      <span>{label}</span>
      {topRight && <span className="normal-case tracking-normal text-zinc-400">{topRight}</span>}
    </div>
    <div className="flex items-end justify-between">
      <span className={`text-lg font-semibold ${valueCls}`}>{value}</span>
      {corner && <span className="text-xs text-zinc-500">{corner}</span>}
    </div>
  </div>
);

const fmtRate = (mbps: number | null) => (mbps == null ? "—" : mbps >= 1 ? `${mbps.toFixed(1)} MB/s` : `${(mbps * 1024).toFixed(0)} KB/s`);

const fmtPow = (w: number | null, limit: number | null) => {
  if (w == null) return "—";
  const cur = `${Math.round(w)} W`;
  return limit != null ? `${cur} / ${Math.round(limit)} W` : cur;
};

export type { AppView, GwView, GpuSample };
