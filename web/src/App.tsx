import { useEffect, useRef, useState } from "react";
import { api, AppView, Decision, EventRow, GpuSample, GwView, RequestRow, Status, initAuth } from "./api";
import AppCards from "./components/AppCards";
import AppForm from "./components/AppForm";
import GatewayCards from "./components/GatewayCards";
import GatewayForm from "./components/GatewayForm";
import GatewayKeys from "./components/GatewayKeys";
import QueueInspector from "./components/QueueInspector";
import EventFeed from "./components/EventFeed";
import RequestsTable from "./components/RequestsTable";
import GpuChart from "./components/GpuChart";
import MetricsPanel from "./components/MetricsPanel";
import AuthPanel from "./components/AuthPanel";

const MAX_SAMPLES = 180;
const TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "applications", label: "Applications" },
  { id: "gateways", label: "Gateways" },
] as const;

export default function App() {
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("dashboard");
  const [authMode, setAuthMode] = useState<"dev" | "login" | "authed" | "checking">("checking");
  const [username, setUsername] = useState<string>("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [showAppForm, setShowAppForm] = useState(false);
  const [editingApp, setEditingApp] = useState<AppView | null>(null);
  const [showGwForm, setShowGwForm] = useState(false);
  const [editingGw, setEditingGw] = useState<GwView | null>(null);
  const [keysGw, setKeysGw] = useState<GwView | null>(null);
  const [queueGw, setQueueGw] = useState<GwView | null>(null);
  // Dashboard-only: each section defaults to showing active items (running apps,
  // non-idle gateways); its header-row toggle expands to show everything. The
  // Applications / Gateways pages always show all items (no filtering).
  const [showAllApps, setShowAllApps] = useState(false);
  const [showAllGws, setShowAllGws] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [decision, setDecision] = useState<Decision>({});
  const [samples, setSamples] = useState<{ t: number; vram: number; gpu: number; temp: number | null }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const sseRef = useRef<EventSource | null>(null);

  // Authentication bootstrap (spec 12 §3): dev mode is open; once an admin is
  // configured a session is required before the UI is usable.
  useEffect(() => {
    initAuth()
      .then((r) => setAuthMode(r.mode))
      .catch(() => setAuthMode("dev"));
  }, []);

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
            { t: Date.now(), vram: g.vram_pct, gpu: g.gpu_pct, temp: g.temperature_c },
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
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-baseline gap-4">
          <h1 className="text-2xl font-semibold tracking-tight">GPUSymbiosis</h1>
          {authMode === "authed" && (
            <span className="text-xs text-zinc-500">
              signed in as <span className="text-zinc-300">{username}</span>
            </span>
          )}
        </div>
        <nav className="flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`rounded px-3 py-1.5 text-sm ${
                tab === t.id
                  ? "bg-zinc-700 font-medium text-zinc-100"
                  : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      {authMode === "checking" && (
        <p className="text-sm text-zinc-500">checking session…</p>
      )}

      {authMode === "login" ? (
        <AuthPanel
          error={authError}
          onError={setAuthError}
          onAuthed={(u) => {
            setUsername(u);
            setAuthMode("authed");
          }}
        />
      ) : (
        <>
          {error && (
            <div className="mb-4 rounded border border-red-800 bg-red-950 px-3 py-2 text-sm text-red-300">{error}</div>
          )}

          {tab === "dashboard" && (
            <>
              {res && (
                <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-400">
                  <span className="font-medium text-zinc-200">{res.name || "GPU"}</span>
                  {res.pcie && <span className="text-zinc-500">{res.pcie}</span>}
                  <span className="tabular-nums">Rx: {fmtRate(res.rx_mbps)}</span>
                  <span className="tabular-nums">Tx: {fmtRate(res.tx_mbps)}</span>
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
                <AppCards apps={apps} act={act} showAll={showAllApps} onToggle={setShowAllApps} />
                <GatewayCards gws={gws} act={act} decision={decision} showAll={showAllGws} onToggle={setShowAllGws} />
              </div>

              <div className="mb-6 grid gap-4 lg:grid-cols-2">
                <RequestsTable rows={requests} />
                <EventFeed events={events} />
              </div>

              <div>
                <MetricsPanel apps={apps.map((a) => ({ id: a.id, name: a.name }))} />
              </div>
            </>
          )}

          {tab === "applications" && (
            <div className="space-y-4">
              <div className="flex justify-end">
                <button
                  onClick={() => {
                    setEditingApp(null);
                    setShowAppForm(true);
                  }}
                  className="rounded bg-zinc-700 px-3 py-1.5 text-sm font-medium hover:bg-zinc-600"
                >
                  + New Application
                </button>
              </div>
              {showAppForm && (
                <AppForm
                  editing={editingApp}
                  onClose={() => {
                    setShowAppForm(false);
                    setEditingApp(null);
                  }}
                  act={act}
                />
              )}
              <AppCards
                apps={apps}
                act={act}
                onEdit={(a) => {
                  setEditingApp(a);
                  setShowAppForm(true);
                }}
                onDelete={(a) =>
                  act(async () => {
                    await api.appDelete(a.id);
                    setEditingApp(null);
                    setShowAppForm(false);
                  })
                }
              />
            </div>
          )}

          {tab === "gateways" && (
            <div className="space-y-4">
              <div className="flex justify-end">
                <button
                  onClick={() => {
                    setEditingGw(null);
                    setShowGwForm(true);
                  }}
                  className="rounded bg-zinc-700 px-3 py-1.5 text-sm font-medium hover:bg-zinc-600"
                >
                  + New Gateway
                </button>
              </div>
              {showGwForm && (
                <GatewayForm
                  apps={apps}
                  editing={editingGw}
                  onClose={() => {
                    setShowGwForm(false);
                    setEditingGw(null);
                  }}
                  act={act}
                />
              )}
              <GatewayCards
                gws={gws}
                act={act}
                decision={decision}
                onEdit={(g) => {
                  setEditingGw(g);
                  setShowGwForm(true);
                }}
                onDelete={(g) =>
                  act(async () => {
                    await api.gwDelete(g.id);
                    setEditingGw(null);
                    setShowGwForm(false);
                  })
                }
                onKeys={(g) => setKeysGw(g)}
                onQueue={(g) => setQueueGw(g)}
              />
              {keysGw && (
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-medium text-zinc-300">
                      Keys — {keysGw.name} (:{keysGw.port})
                    </h3>
                    <button onClick={() => setKeysGw(null)} className="text-xs text-zinc-500 hover:text-zinc-300">
                      close
                    </button>
                  </div>
                  <GatewayKeys gwId={keysGw.id} act={act} />
                </div>
              )}
              {queueGw && (
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-medium text-zinc-300">
                      Queue — {queueGw.name} (:{queueGw.port})
                    </h3>
                    <button onClick={() => setQueueGw(null)} className="text-xs text-zinc-500 hover:text-zinc-300">
                      close
                    </button>
                  </div>
                  <QueueInspector gwId={queueGw.id} act={act} />
                </div>
              )}
            </div>
          )}
        </>
      )}
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

// Unit-aware with 4 significant digits: "0.000 Kb/s", "12.00 Kb/s",
// "1.123 Mb/s", "23.00 Mb/s", "999.0 Mb/s", "23.00 Gb/s".
const fmtRate = (mbps: number | null) => {
  if (mbps == null) return "—";
  const v = Math.abs(mbps);
  let val: number;
  let unit: string;
  if (v < 1) {
    val = v * 1000;
    unit = "Kb/s";
  } else if (v < 1000) {
    val = v;
    unit = "Mb/s";
  } else {
    val = v / 1000;
    unit = "Gb/s";
  }
  const digits = val > 0 ? Math.floor(Math.log10(val)) : -3;
  const decimals = Math.min(3, Math.max(0, 3 - digits));
  return `${val.toFixed(decimals)} ${unit}`;
};

const fmtPow = (w: number | null, limit: number | null) => {
  if (w == null) return "—";
  const cur = `${Math.round(w)} W`;
  return limit != null ? `${cur} / ${Math.round(limit)} W` : cur;
};

export type { AppView, GwView, GpuSample };
