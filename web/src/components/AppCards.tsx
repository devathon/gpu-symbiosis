import { api, AppView } from "../api";
import { badge, Btn } from "./badges";

export default function AppCards({
  apps,
  act,
}: {
  apps: AppView[];
  act: (fn: () => Promise<void>) => void;
}) {
  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-xs uppercase tracking-wider text-zinc-500">Applications</h2>
      <div className="space-y-3">
        {apps.map((a) => (
          <div key={a.id} className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-medium">{a.name}</span>
                {badge(a.process_state)}
                {badge(a.residency_state)}
                {a.pinned && <span className="rounded bg-purple-900/60 px-1.5 text-xs text-purple-300">pinned</span>}
                {a.draining && <span className="rounded bg-amber-900/60 px-1.5 text-xs text-amber-300">draining</span>}
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                {a.gpu_memory_used_mb.toFixed(0)} MB VRAM · {a.active_requests} active · {a.queued_requests} queued
                {a.consecutive_load_failures > 0 && (
                  <span className="text-red-400"> · {a.consecutive_load_failures} load failures</span>
                )}
              </div>
            </div>
            <div className="flex gap-1.5">
              {a.process_state === "RUNNING" ? (
                a.supports_load_unload && a.residency_state === "LOADED" ? (
                  <Btn label="Unload" onClick={() => act(() => api.appUnload(a.id))} />
                ) : (
                  <Btn label="Stop" onClick={() => act(() => api.appStop(a.id))} />
                )
              ) : (
                <Btn label="Start" onClick={() => act(() => api.appStart(a.id))} />
              )}
              {a.process_state === "RUNNING" &&
                a.supports_load_unload &&
                a.residency_state !== "LOADED" && <Btn label="Load" onClick={() => act(() => api.appLoad(a.id))} />}
            </div>
          </div>
        ))}
        {apps.length === 0 && <p className="text-sm text-zinc-600">no applications</p>}
      </div>
    </section>
  );
}

