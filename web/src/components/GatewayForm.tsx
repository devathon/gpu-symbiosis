import { useEffect, useState } from "react";
import { api, AppView, GatewayFormFields, GwView } from "../api";
import { Btn } from "./badges";

// Gateway create/edit form (spec 11 §3). On create the server assigns the id
// and starts the listener immediately (spec 11 §3.2).
const EMPTY: GatewayFormFields = {
  name: "",
  port: 0,
  application_id: "",
  priority: 5,
  max_waiting_queue: 20,
  max_waiting_time_sec: 300,
  wait_timeout_policy: "ESCALATE_PRIORITY",
  request_timeout_sec: null,
  max_concurrent_requests: 1,
  enabled: true,
};

const fromView = (g: GwView): GatewayFormFields => ({
  name: g.name,
  port: g.port,
  application_id: g.application_id,
  priority: g.priority,
  max_waiting_queue: g.max_waiting_queue,
  max_waiting_time_sec: g.max_waiting_time_sec,
  wait_timeout_policy: g.wait_timeout_policy,
  request_timeout_sec: g.request_timeout_sec ?? null,
  max_concurrent_requests: g.max_concurrent_requests,
  enabled: g.enabled,
});

export default function GatewayForm({
  apps,
  editing,
  onClose,
  act,
}: {
  apps: AppView[];
  editing: GwView | null;
  onClose: () => void;
  act: (fn: () => Promise<void>) => void;
}) {
  const [f, setF] = useState<GatewayFormFields>(editing ? fromView(editing) : EMPTY);

  // Reset only when the edit target changes — the apps list refreshes every
  // poll and must not wipe in-progress form state.
  useEffect(() => {
    setF(editing ? fromView(editing) : { ...EMPTY, application_id: apps[0]?.id ?? "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  const set = <K extends keyof GatewayFormFields>(k: K, v: GatewayFormFields[K]) =>
    setF((p) => ({ ...p, [k]: v }));

  const submit = () => {
    const fields: GatewayFormFields = {
      ...f,
      port: Number(f.port) || 0,
      priority: Number(f.priority) || 5,
      request_timeout_sec: f.request_timeout_sec == null ? null : Number(f.request_timeout_sec),
    };
    if (!fields.port || !fields.application_id) return;
    if (editing) {
      act(() => api.gwUpdate(editing.id, fields));
    } else {
      act(() => api.gwCreate(fields));
    }
    onClose();
  };

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500">
          {editing ? `Edit gateway — ${editing.id} :${editing.port}` : "New gateway"}
        </h2>
        <div className="flex gap-1.5">
          <Btn label="Cancel" onClick={onClose} />
          <button
            onClick={submit}
            className="rounded bg-zinc-700 px-2.5 py-1 text-xs font-medium hover:bg-zinc-600"
          >
            {editing ? "Save" : "Create"}
          </button>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">Routing</h3>
          <div className="space-y-2.5">
            <Row label="Name">
              <input className={inp} value={f.name} onChange={(e) => set("name", e.target.value)} />
            </Row>
            <Row label="Port">
              <input className={inp} type="number" value={f.port || ""} onChange={(e) => set("port", Number(e.target.value))} />
            </Row>
            <Row label="Application">
              <select className={inp} value={f.application_id} onChange={(e) => set("application_id", e.target.value)}>
                {apps.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.id} ({a.name})
                  </option>
                ))}
              </select>
            </Row>
            <Row label="Priority">
              <input className={inp} type="number" value={f.priority} onChange={(e) => set("priority", Number(e.target.value))} />
            </Row>
            <Row label="Enabled">
              <input type="checkbox" checked={f.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            </Row>
          </div>
        </div>

        <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">Queueing</h3>
          <div className="space-y-2.5">
            <Row label="Max waiting queue">
              <input className={inp} type="number" value={f.max_waiting_queue} onChange={(e) => set("max_waiting_queue", Number(e.target.value))} />
            </Row>
            <Row label="Max wait time (s)">
              <input className={inp} type="number" value={f.max_waiting_time_sec} onChange={(e) => set("max_waiting_time_sec", Number(e.target.value))} />
            </Row>
            <Row label="Wait timeout policy">
              <select className={inp} value={f.wait_timeout_policy} onChange={(e) => set("wait_timeout_policy", e.target.value)}>
                <option value="REJECT">REJECT</option>
                <option value="EXPIRE">EXPIRE</option>
                <option value="ESCALATE_PRIORITY">ESCALATE_PRIORITY</option>
                <option value="PROCESS_ANYWAY">PROCESS_ANYWAY</option>
              </select>
            </Row>
            <Row label="Request timeout (s)">
              <input
                className={inp}
                type="number"
                value={f.request_timeout_sec ?? ""}
                onChange={(e) => set("request_timeout_sec", e.target.value === "" ? null : Number(e.target.value))}
              />
            </Row>
            <Row label="Max concurrent requests">
              <input className={inp} type="number" value={f.max_concurrent_requests} onChange={(e) => set("max_concurrent_requests", Number(e.target.value))} />
            </Row>
          </div>
        </div>
      </div>
    </section>
  );
}

const inp =
  "w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm outline-none focus:border-zinc-500";

const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="flex items-center gap-2">
    <span className="w-40 shrink-0 text-xs text-zinc-400">{label}</span>
    <div className="min-w-0 flex-1">{children}</div>
  </div>
);
