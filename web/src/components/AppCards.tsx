import { api, AppView, ExternalAppView } from "../api";
import { badge, IconBtn, appDisplayState, appResidencyDisplay, showAppResidency } from "./badges";

export default function AppCards({
  apps,
  act,
  onEdit,
  onDelete,
  showAll,
  onToggle,
  externalApps,
  onKillExternal,
}: {
  apps: AppView[];
  act: (fn: () => Promise<void>) => void;
  onEdit?: (a: AppView) => void;
  onDelete?: (a: AppView) => void;
  /** Dashboard: false → hide stopped apps, true → show all. Undefined (the
      Applications page) → always show all, no toggle. */
  showAll?: boolean;
  onToggle?: (showAll: boolean) => void;
  /** External (unmanaged) GPU processes above threshold, with a kill button. */
  externalApps?: ExternalAppView[];
  onKillExternal?: (pid: number) => void;
}) {
  const forceStop = (a: AppView) => {
    if (!window.confirm(`Force-stop ${a.name}? Active requests will be dropped.`)) return;
    act(() => api.appStop(a.id, true));
  };

  const killExternal = (e: ExternalAppView) => {
    if (!window.confirm(`Kill ${e.cmd} (pid ${e.pid})? This is a SIGKILL.`)) return;
    if (onKillExternal) onKillExternal(e.pid);
    else act(() => api.externalKill(e.pid));
  };

  // Filter only when the dashboard toggle is present; the Applications page
  // (no onToggle) always shows every app.
  const visible = showAll === false ? apps.filter((a) => a.process_state !== "STOPPED") : apps;
  const hidden = apps.length - visible.length;

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500">Applications</h2>
        {onToggle && (
          <button
            onClick={() => onToggle(!showAll)}
            title={showAll ? "Show active only" : hidden > 0 ? `Show all (${hidden} hidden)` : "Show all"}
            aria-label={showAll ? "Show active applications only" : "Show all applications"}
            className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-xs ${
              showAll ? "bg-zinc-700 text-zinc-100" : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {showAll ? <path d="m18 15-6-6-6 6" /> : <path d="m6 9 6 6 6-6" />}
            </svg>
            {showAll ? "Show Active" : "Show All"}
          </button>
        )}
      </div>
      <div className="space-y-3">
        {visible.map((a) => (
          <div key={a.id} className="-mx-2 flex items-center gap-3 rounded px-2 transition-colors hover:bg-zinc-800/50">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-medium">
                  {a.name}
                  {a.application_type && <span className="font-normal text-zinc-500"> ({a.application_type})</span>}
                </span>
                {/* Two independent state machines (spec 04 §2): the process state
                    (shown as RUNNING when active, STARTED when idle) and the
                    residency state (shown only when started + load/unload
                    supported; OFF shown as UNLOADED) each get their own badge. */}
                {badge(appDisplayState(a.process_state, a.active))}
                {showAppResidency(a.process_state, a.supports_load_unload) && badge(appResidencyDisplay(a.residency_state))}
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
            <div className="flex flex-wrap gap-1">
              {a.process_state === "STARTED" ? (
                <>
                  {/* Load button only when the app HAS a load script; an
                      unload-only app loads automatically from usage (the
                      residency monitor) and offers no manual Load. Unload is
                      shown when loaded and the app has an unload script.
                      Stop is ALWAYS available (the direct-kill path).
                      Force Stop (immediate kill) is always shown. */}
                  {a.load_script && a.residency_state !== "LOADED" && (
                    <IconBtn label="Load" icon="load" onClick={() => act(() => api.appLoad(a.id))} />
                  )}
                  {a.unload_script && a.residency_state === "LOADED" && (
                    <IconBtn label="Unload" icon="unload" onClick={() => act(() => api.appUnload(a.id))} />
                  )}
                  <IconBtn label="Stop" icon="stop" onClick={() => act(() => api.appStop(a.id))} />
                  <IconBtn
                    label="Force Stop"
                    icon="forceStop"
                    danger
                    title="Kill immediately, dropping active requests"
                    onClick={() => forceStop(a)}
                  />
                </>
              ) : (
                <IconBtn label="Start" icon="play" onClick={() => act(() => api.appStart(a.id))} />
              )}
              {a.pinned ? (
                <IconBtn label="Unpin" icon="pinOff" onClick={() => act(() => api.appUnpin(a.id))} />
              ) : (
                <IconBtn
                  label="Pin"
                  icon="pin"
                  title="Exclude from automatic unloading"
                  onClick={() => act(() => api.appPin(a.id))}
                />
              )}
              {onEdit && <IconBtn label="Edit" icon="edit" onClick={() => onEdit(a)} />}
              {onDelete && (
                <IconBtn
                  label="Delete"
                  icon="trash"
                  danger
                  onClick={() => {
                    if (window.confirm(`Delete ${a.id}? Its gateways and request history are removed.`))
                      onDelete(a);
                  }}
                />
              )}
            </div>
          </div>
        ))}
        {visible.length === 0 && (
          <p className="text-sm text-zinc-600">
            {apps.length === 0 ? "no applications" : `all ${apps.length} stopped — show all`}
          </p>
        )}
      </div>
      {externalApps && externalApps.length > 0 && (
        <div className="mt-3 border-t border-zinc-800 pt-3">
          <div className="mb-2 text-xs uppercase tracking-wider text-zinc-500">External</div>
          <div className="space-y-3">
            {externalApps.map((e) => (
              <div key={e.pid} className="-mx-2 flex items-center gap-3 rounded px-2 transition-colors hover:bg-zinc-800/50">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-medium" title={e.cmd}>
                      {e.cmd} <span className="font-normal text-zinc-500">(external)</span>
                    </span>
                    {badge("RUNNING")}
                  </div>
                  <div className="mt-1 text-xs text-zinc-500">
                    {e.gpu_pct.toFixed(0)}% GPU · {e.vram_mb.toFixed(0)} MB VRAM
                    {!e.auto_unload_ok && (
                      <span className="ml-1 text-zinc-600">(excluded from auto-kill)</span>
                    )}
                  </div>
                </div>
                <div className="flex flex-wrap gap-1">
                  <IconBtn
                    label="Kill"
                    icon="forceStop"
                    danger
                    title="Kill this unmanaged process (SIGKILL)"
                    onClick={() => killExternal(e)}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
