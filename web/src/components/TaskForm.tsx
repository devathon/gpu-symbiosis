import { useEffect, useState } from "react";
import { api, TaskFormFields, TaskRow } from "../api";
import { Btn } from "./badges";

// Task create/edit form (the Tasks feature). On create the server assigns the id;
// a "copy" (prefill) opens the form pre-filled but still creates a NEW task. A
// ran task can never be edited (only copied) — the server rejects it.
const EMPTY: TaskFormFields = {
  name: "",
  start_time: "00:00",
  end_time: "23:59",
  idle_time: 10,
  vram_requirement: 0,
  command: "",
};

const fromView = (t: TaskRow): TaskFormFields => ({
  name: t.name,
  start_time: t.start_time,
  end_time: t.end_time,
  idle_time: t.idle_time,
  vram_requirement: t.vram_requirement,
  command: t.command,
});

export default function TaskForm({
  editing,
  prefill,
  onClose,
  act,
}: {
  editing: TaskRow | null;
  prefill: TaskRow | null;
  onClose: () => void;
  act: (fn: () => Promise<void>) => void;
}) {
  const [f, setF] = useState<TaskFormFields>(editing ? fromView(editing) : prefill ? fromView(prefill) : EMPTY);

  // Reset when the edit/copy target changes.
  useEffect(() => {
    setF(editing ? fromView(editing) : prefill ? fromView(prefill) : EMPTY);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing, prefill]);

  const set = <K extends keyof TaskFormFields>(k: K, v: TaskFormFields[K]) =>
    setF((p) => ({ ...p, [k]: v }));

  const submit = () => {
    const fields: TaskFormFields = {
      ...f,
      idle_time: Math.max(0, Number(f.idle_time) || 0),
      vram_requirement: Math.max(0, Number(f.vram_requirement) || 0),
    };
    if (!fields.name.trim() || !fields.command.trim() || !fields.start_time || !fields.end_time) return;
    if (editing) {
      act(async () => {
        await api.taskUpdate(editing.id, fields);
      });
    } else {
      act(async () => {
        await api.taskCreate(fields);
      });
    }
    onClose();
  };

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500">
          {editing ? `Edit task — ${editing.name}` : prefill ? `New task (copied from ${prefill.name})` : "New task"}
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
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">When to run</h3>
          <div className="space-y-2.5">
            <Row label="Name">
              <input className={inp} value={f.name} onChange={(e) => set("name", e.target.value)} />
            </Row>
            <Row label="Window start">
              <input className={inp} type="time" value={f.start_time} onChange={(e) => set("start_time", e.target.value)} />
            </Row>
            <Row label="Window end">
              <input className={inp} type="time" value={f.end_time} onChange={(e) => set("end_time", e.target.value)} />
            </Row>
            <Row label="GPU idle for (minutes)">
              <input className={inp} type="number" min="0" value={f.idle_time} onChange={(e) => set("idle_time", Number(e.target.value) || 0)} />
            </Row>
          </div>
        </div>

        <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">What to run</h3>
          <div className="space-y-2.5">
            <Row label="Required VRAM (MB)">
              <input className={inp} type="number" min="0" value={f.vram_requirement} onChange={(e) => set("vram_requirement", Number(e.target.value) || 0)} />
            </Row>
            <div>
              <span className="mb-1 block text-xs text-zinc-400">Command</span>
              <textarea
                className={`${inp} h-28 resize-y font-mono text-xs`}
                value={f.command}
                placeholder="e.g. python train.py --epochs 10"
                onChange={(e) => set("command", e.target.value)}
              />
              <p className="mt-1 text-[11px] text-zinc-600">
                Runs detached when the GPU is idle; output goes to the task log. If the VRAM
                requirement is unmet, idle unloadable apps are freed first and it runs on the next check.
              </p>
            </div>
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
    <span className="w-36 shrink-0 text-xs text-zinc-400">{label}</span>
    <div className="min-w-0 flex-1">{children}</div>
  </div>
);
