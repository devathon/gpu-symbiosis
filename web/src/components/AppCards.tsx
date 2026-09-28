import { api, AppView } from "../api";
import { badge, Btn } from "./badges";

export default function AppCards({
  apps,
  act,
  onEdit,
  onDelete,
}: {
  apps: AppView[];
  act: (fn: () => Promise<void>) => void;
  onEdit?: (a: AppView) => void;
  onDelete?: (a: AppView) => void;
}) {
  const forceStop = (a: AppView) => {
    if (!window.confirm(`Force-stop ${a.name}? Active requests will be dropped.`)) return;
    act(() => api.appStop(a.id, true));
  };

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-xs uppercase tracking-wider text-zinc-500">Applications</h2>
      <div className="space-y-3">
        {apps.map((a) => (
          <div key={a.id} className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-medium">{a.name}</span>
                {/* Combined state (spec 04 §3): RUNNING (busy) is one badge;
                    idle-but-loaded renders as TWO badges (IDLE + LOADED), both
                    orange; everything else (IDLE, STOPPED, STARTING, ...) is one. */}
                {a.state === "IDLE & LOADED" ? (
                  <>
                    <span className="rounded bg-orange-900/60 px-1.5 py-0.5 text-xs text-orange-300">IDLE</span>
                    <span className="rounded bg-orange-900/60 px-1.5 py-0.5 text-xs text-orange-300">LOADED</span>
                  </>
                ) : (
                  badge(a.state)
                )}
                {a.pinned && <span className="rounded bg-purple-900/60 px-1.5 text-xs text-purple-300">pinned</span>}
                {a.draining && <span className="rounded bg-amber-900/60 px-1.5 text-xs text-amber-300">draining</span>}
                {!a.enabled && <span className="rounded bg-zinc-800 px-1.5 text-xs text-zinc-500">disabled</span>}
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                {a.gpu_utilization_pct.toFixed(0)}% GPU · {a.gpu_memory_used_mb.toFixed(0)} MB VRAM · {a.active_requests} active · {a.queued_requests} queued
                {a.consecutive_load_failures > 0 && (
                  <span className="text-red-400"> · {a.consecutive_load_failures} load failures</span>
                )}
              </div>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {a.process_state === "RUNNING" ? (
                <>
                  {/* Unloadable app (has an unload script): Load when not
                      loaded, Unload when loaded — and Stop is ALWAYS available
                      (the direct-kill path). A non-unloadable app just offers
                      Stop. Force Stop (immediate kill) is always shown. */}
                  {a.supports_load_unload && a.residency_state !== "LOADED" && (
                    <Btn label="Load" onClick={() => act(() => api.appLoad(a.id))} />
                  )}
                  {a.supports_load_unload && a.residency_state === "LOADED" && (
                    <Btn label="Unload" onClick={() => act(() => api.appUnload(a.id))} />
                  )}
                  <Btn label="Stop" onClick={() => act(() => api.appStop(a.id))} />
                  <Btn label="Force Stop" title="Kill immediately, dropping active requests" onClick={() => forceStop(a)} />
                </>
              ) : (
                <Btn label="Start" onClick={() => act(() => api.appStart(a.id))} />
              )}
              {a.pinned ? (
                <Btn label="Unpin" onClick={() => act(() => api.appUnpin(a.id))} />
              ) : (
                <Btn label="Pin" title="Exclude from automatic unloading" onClick={() => act(() => api.appPin(a.id))} />
              )}
              {onEdit && <Btn label="Edit" onClick={() => onEdit(a)} />}
              {onDelete && (
                <Btn
                  label="Delete"
                  onClick={() => {
                    if (window.confirm(`Delete ${a.id}? Its gateways and request history are removed.`))
                      onDelete(a);
                  }}
                />
              )}
            </div>
          </div>
        ))}
        {apps.length === 0 && <p className="text-sm text-zinc-600">no applications</p>}
      </div>
    </section>
  );
}
