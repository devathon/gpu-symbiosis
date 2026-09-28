import { useEffect, useState } from "react";
import { api, AppFormFields, AppView } from "../api";
import { Btn } from "./badges";

// Application create/edit form (spec 11 §2.3): General / Process / Loading /
// Network / Resource. Timing values are auto-calculated (read-only).
const EMPTY: AppFormFields = {
  id: "",
  name: "",
  description: "",
  application_type: "custom",
  process_name: "",
  start_script: "",
  stop_script: "",
  load_script: "",
  unload_script: "",
  port: 0,
  protocol: "http",
  health_check_url: "",
  ready_check_url: "",
  configured_gpu_memory_mb: 0,
  cpu_memory_required_mb: null,
  gpu_memory_safety_margin: 0.1,
  supports_load_unload: false,
  allow_automatic_unload: true,
  idle_unload_timeout_sec: null,
  activity_detection_method: "gpu_usage",
  idle_threshold_gpu_pct: 5,
};

const fromView = (v: AppView): AppFormFields => ({
  id: v.id,
  name: v.name,
  description: v.description ?? "",
  application_type: v.application_type ?? "custom",
  process_name: v.process_name ?? "",
  start_script: v.start_script ?? "",
  stop_script: v.stop_script ?? "",
  load_script: v.load_script ?? "",
  unload_script: v.unload_script ?? "",
  port: v.port,
  protocol: v.protocol ?? "http",
  health_check_url: v.health_check_url ?? "",
  ready_check_url: v.ready_check_url ?? "",
  configured_gpu_memory_mb: v.gpu_memory_configured_mb,
  cpu_memory_required_mb: v.cpu_memory_required_mb ?? null,
  gpu_memory_safety_margin: v.gpu_memory_safety_margin ?? 0.1,
  supports_load_unload: v.supports_load_unload,
  allow_automatic_unload: v.allow_automatic_unload,
  idle_unload_timeout_sec: v.idle_unload_timeout_sec ?? null,
  activity_detection_method: v.activity_detection_method ?? "gpu_usage",
  idle_threshold_gpu_pct: v.idle_threshold_gpu_pct ?? 5,
});

export default function AppForm({
  editing,
  onClose,
  act,
}: {
  editing: AppView | null;
  onClose: () => void;
  act: (fn: () => Promise<void>) => void;
}) {
  const [f, setF] = useState<AppFormFields>(editing ? fromView(editing) : EMPTY);
  // Registered application types (spec 04 §8): built-ins + drop-in plugins.
  const [types, setTypes] = useState<string[]>(["custom", "llama"]);

  useEffect(() => {
    setF(editing ? fromView(editing) : EMPTY);
  }, [editing]);

  useEffect(() => {
    api.appTypes().then((r) => {
      const list = r.types.length ? r.types : [];
      setTypes((p) => {
        const next = new Set(p);
        list.forEach((t) => next.add(t));
        return [...next];
      });
    }).catch(() => {});
  }, []);

  const set = <K extends keyof AppFormFields>(k: K, v: AppFormFields[K]) =>
    setF((p) => ({ ...p, [k]: v }));
  const num = (v: string) => (v === "" ? null : Number(v));

  const submit = () => {
    const fields: AppFormFields = {
      ...f,
      port: Number(f.port) || 0,
      configured_gpu_memory_mb: Number(f.configured_gpu_memory_mb) || 0,
      stop_script: f.stop_script || null,
      load_script: f.load_script || null,
      unload_script: f.unload_script || null,
      health_check_url: f.health_check_url || null,
      ready_check_url: f.ready_check_url || null,
      cpu_memory_required_mb: num(f.cpu_memory_required_mb ? String(f.cpu_memory_required_mb) : ""),
      idle_unload_timeout_sec: num(f.idle_unload_timeout_sec ? String(f.idle_unload_timeout_sec) : ""),
    };
    if (editing) {
      act(() => api.appUpdate(editing.id, fields));
    } else {
      if (!f.id.trim() || !f.name.trim() || !fields.port || !fields.configured_gpu_memory_mb) return;
      act(() => api.appCreate(fields));
    }
    onClose();
  };

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500">
          {editing ? `Edit application — ${editing.id}` : "New application"}
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
        <Fieldset title="General">
          {!editing && (
            <Row label="ID">
              <input
                className={inp}
                placeholder="my-model"
                value={f.id}
                onChange={(e) => set("id", e.target.value.trim().toLowerCase().replace(/[^a-z0-9-]/g, "-"))}
              />
            </Row>
          )}
          <Row label="Name">
            <input className={inp} value={f.name} onChange={(e) => set("name", e.target.value)} />
          </Row>
          <Row label="Description">
            <input className={inp} value={f.description} onChange={(e) => set("description", e.target.value)} />
          </Row>
          <Row label="Application type">
            <select className={inp} value={f.application_type} onChange={(e) => set("application_type", e.target.value)}>
              {!types.includes(f.application_type) && f.application_type !== "" && (
                <option value={f.application_type}>{f.application_type}</option>
              )}
              {types.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </Row>
        </Fieldset>

        <Fieldset title="Process">
          <Row label="Process name">
            <input className={inp} value={f.process_name} onChange={(e) => set("process_name", e.target.value)} />
          </Row>
          <Row label="Start script">
            <textarea className={inp} rows={2} value={f.start_script} onChange={(e) => set("start_script", e.target.value)} />
          </Row>
          <Row label="Stop script">
            <textarea className={inp} rows={2} value={f.stop_script ?? ""} onChange={(e) => set("stop_script", e.target.value || null)} />
          </Row>
        </Fieldset>

        <Fieldset title="Loading">
          <Row label="Load script">
            <input className={inp} value={f.load_script ?? ""} onChange={(e) => set("load_script", e.target.value || null)} />
          </Row>
          <Row label="Unload script">
            <input className={inp} value={f.unload_script ?? ""} onChange={(e) => set("unload_script", e.target.value || null)} />
          </Row>
          <Row label="Allow automatic unload">
            <input
              type="checkbox"
              checked={f.allow_automatic_unload}
              onChange={(e) => set("allow_automatic_unload", e.target.checked)}
            />
          </Row>
          <Row label="Idle unload timeout (s)">
            <input
              className={inp}
              type="number"
              value={f.idle_unload_timeout_sec ?? ""}
              onChange={(e) => set("idle_unload_timeout_sec", e.target.value === "" ? null : Number(e.target.value))}
            />
          </Row>
          <Row label="Activity detection">
            <select className={inp} value={f.activity_detection_method} onChange={(e) => set("activity_detection_method", e.target.value)}>
              <option value="gpu_usage">gpu_usage</option>
              <option value="api_requests">api_requests</option>
            </select>
          </Row>
          <Row label="Idle threshold (GPU %)">
            <input
              className={inp}
              type="number"
              step="0.5"
              value={f.idle_threshold_gpu_pct}
              onChange={(e) => set("idle_threshold_gpu_pct", Number(e.target.value))}
            />
          </Row>
        </Fieldset>

        <Fieldset title="Network">
          <Row label="Port">
            <input className={inp} type="number" value={f.port || ""} onChange={(e) => set("port", Number(e.target.value))} />
          </Row>
          <Row label="Protocol">
            <select className={inp} value={f.protocol} onChange={(e) => set("protocol", e.target.value)}>
              <option value="http">http</option>
              <option value="https">https</option>
              <option value="ws">ws</option>
            </select>
          </Row>
          <Row label="Health check URL">
            <input className={inp} value={f.health_check_url ?? ""} onChange={(e) => set("health_check_url", e.target.value || null)} />
          </Row>
          <Row label="Ready check URL">
            <input className={inp} value={f.ready_check_url ?? ""} onChange={(e) => set("ready_check_url", e.target.value || null)} />
          </Row>
        </Fieldset>

        <Fieldset title="Resource">
          <Row label="GPU memory (MB)">
            <input
              className={inp}
              type="number"
              value={f.configured_gpu_memory_mb || ""}
              onChange={(e) => set("configured_gpu_memory_mb", Number(e.target.value))}
            />
          </Row>
          <Row label="CPU memory (MB)">
            <input
              className={inp}
              type="number"
              value={f.cpu_memory_required_mb ?? ""}
              onChange={(e) => set("cpu_memory_required_mb", e.target.value === "" ? null : Number(e.target.value))}
            />
          </Row>
          <Row label="GPU memory safety margin">
            <input
              className={inp}
              type="number"
              step="0.01"
              value={f.gpu_memory_safety_margin}
              onChange={(e) => set("gpu_memory_safety_margin", Number(e.target.value))}
            />
          </Row>
        </Fieldset>

        <Fieldset title="Timing (auto-calculated)">
          <Row label="Startup">
            <span className="text-sm text-zinc-500">{editing?.startup_time_sec != null ? `${editing.startup_time_sec}s` : "—"}</span>
          </Row>
          <Row label="Load">
            <span className="text-sm text-zinc-500">{editing?.load_time_sec != null ? `${editing.load_time_sec}s` : "—"}</span>
          </Row>
          <Row label="Unload">
            <span className="text-sm text-zinc-500">{editing?.unload_time_sec != null ? `${editing.unload_time_sec}s` : "—"}</span>
          </Row>
          <Row label="Stop">
            <span className="text-sm text-zinc-500">{editing?.stop_time_sec != null ? `${editing.stop_time_sec}s` : "—"}</span>
          </Row>
        </Fieldset>
      </div>
    </section>
  );
}

const inp =
  "w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm outline-none focus:border-zinc-500";

const Fieldset = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
    <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">{title}</h3>
    <div className="space-y-2.5">{children}</div>
  </div>
);

const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="flex items-center gap-2">
    <span className="w-36 shrink-0 text-xs text-zinc-400">{label}</span>
    <div className="min-w-0 flex-1">{children}</div>
  </div>
);
