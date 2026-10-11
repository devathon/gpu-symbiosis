import { RequestRow } from "../api";
import { badge } from "./badges";

function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString([], { hour12: false });
}

export default function RequestsTable({ rows }: { rows: RequestRow[] }) {
  return (
    <section className="max-h-96 overflow-auto rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 className="mb-3 text-xs uppercase tracking-wider text-zinc-500">Requests</h2>
      <table className="w-full text-left text-xs">
        <thead className="text-zinc-500">
          <tr>
            <th className="pb-2">received</th>
            <th>gateway</th>
            <th>status</th>
            <th>queue wait</th>
            <th>service</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-zinc-800/60 transition-colors hover:bg-zinc-800/40">
              <td className="py-1.5 font-mono text-zinc-400">{fmtWhen(r.received_at)}</td>
              <td>{r.gateway_id}</td>
              <td>{badge(r.status)}</td>
              <td>{r.queue_wait_sec != null ? `${r.queue_wait_sec.toFixed(2)}s` : "—"}</td>
              <td>{r.service_time_sec != null ? `${r.service_time_sec.toFixed(2)}s` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p className="mt-2 text-sm text-zinc-600">no requests yet</p>}
    </section>
  );
}
