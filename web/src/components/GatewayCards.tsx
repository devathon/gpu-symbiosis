import { api, GwView } from "../api";
import { badge, Btn } from "./badges";

export default function GatewayCards({
  gws,
  act,
  onEdit,
  onDelete,
  onKeys,
  onQueue,
}: {
  gws: GwView[];
  act: (fn: () => Promise<void>) => void;
  onEdit?: (g: GwView) => void;
  onDelete?: (g: GwView) => void;
  onKeys?: (g: GwView) => void;
  onQueue?: (g: GwView) => void;
}) {
  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-xs uppercase tracking-wider text-zinc-500">Gateways</h2>
      <div className="space-y-3">
        {gws.map((g) => (
          <div key={g.id} className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-medium">{g.name}</span>
                {badge(g.state)}
                {!g.enabled && <span className="rounded bg-zinc-800 px-1.5 text-xs text-zinc-500">disabled</span>}
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                :{g.port} → {g.application_id} · priority {g.priority} · queue {g.queue.length}
                {g.queue.length > 0 && ` (oldest ${g.queue.oldest_wait_sec}s)`}
              </div>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {g.queue.length > 0 && <Btn label="Clear" onClick={() => act(() => api.gwClear(g.id))} />}
              <Btn
                label="Process Now"
                onClick={() => act(() => api.gwProcessNow(g.id))}
                title="Raise this gateway's scheduling priority (time-decayed boost; never kills active work)"
              />
              {g.enabled ? (
                <Btn label="Disable" onClick={() => act(() => api.gwDisable(g.id))} />
              ) : (
                <Btn label="Enable" onClick={() => act(() => api.gwEnable(g.id))} />
              )}
              {onEdit && <Btn label="Edit" onClick={() => onEdit(g)} />}
              {onDelete && (
                <Btn
                  label="Delete"
                  onClick={() => {
                    if (window.confirm(`Delete gateway ${g.name} (:${g.port})?`)) onDelete(g);
                  }}
                />
              )}
              {onKeys && <Btn label="Keys" onClick={() => onKeys(g)} />}
              {onQueue && <Btn label="Queue" onClick={() => onQueue(g)} />}
            </div>
          </div>
        ))}
        {gws.length === 0 && <p className="text-sm text-zinc-600">no gateways</p>}
      </div>
    </section>
  );
}
