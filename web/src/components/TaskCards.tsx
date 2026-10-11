import { TaskRow } from "../api";
import { badge, IconBtn } from "./badges";

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

// The Tasks listing (the Tasks feature). Columns: Status (ran or not), Run Time,
// Name, and the actions edit (only while un-ran — a ran task is frozen), copy,
// delete.
export default function TaskCards({
  tasks,
  onEdit,
  onCopy,
  onDelete,
}: {
  tasks: TaskRow[];
  onEdit?: (t: TaskRow) => void;
  onCopy?: (t: TaskRow) => void;
  onDelete?: (t: TaskRow) => void;
}) {
  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500">Tasks</h2>
        <span className="text-xs text-zinc-500">
          {tasks.filter((t) => !t.ran).length} pending · {tasks.filter((t) => t.ran).length} ran
        </span>
      </div>

      {tasks.length === 0 ? (
        <p className="text-sm text-zinc-600">no tasks — create one to schedule a command during GPU idle time</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-xs uppercase tracking-wider text-zinc-500">
                <th className="py-1.5 pr-3 font-medium">Status</th>
                <th className="py-1.5 pr-3 font-medium">Run Time</th>
                <th className="py-1.5 pr-3 font-medium">Name</th>
                <th className="py-1.5 pr-3 font-medium">Window</th>
                <th className="py-1.5 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => (
                <tr key={t.id} className="border-b border-zinc-800/60">
                  <td className="py-2 pr-3">{t.ran ? badge("COMPLETED") : badge("QUEUED")}</td>
                  <td className="py-2 pr-3 text-xs text-zinc-400">{fmtTime(t.run_time)}</td>
                  <td className="py-2 pr-3 font-medium text-zinc-200">{t.name}</td>
                  <td className="py-2 pr-3 text-xs text-zinc-500">
                    {t.start_time}–{t.end_time}
                    {t.idle_time > 0 && <span> · idle &gt;{t.idle_time}m</span>}
                    {t.vram_requirement > 0 && <span> · {t.vram_requirement} MB</span>}
                  </td>
                  <td className="py-2">
                    <div className="flex flex-wrap gap-1">
                      {!t.ran && onEdit && <IconBtn label="Edit" icon="edit" onClick={() => onEdit(t)} />}
                      {onCopy && <IconBtn label="Copy" icon="copy" onClick={() => onCopy(t)} />}
                      {onDelete && (
                        <IconBtn
                          label="Delete"
                          icon="trash"
                          danger
                          onClick={() => {
                            if (window.confirm(`Delete task ${t.name}?`)) onDelete(t);
                          }}
                        />
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
