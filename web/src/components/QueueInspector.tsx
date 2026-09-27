import { useEffect, useState } from "react";
import { api, QueueRow } from "../api";
import { Btn } from "./badges";

// Queue request inspector (spec 11 §4): per-gateway queue with per-request
// cancel (queued only) and force-process (raise to top of queue).
export default function QueueInspector({
  gwId,
  act,
}: {
  gwId: string;
  act: (fn: () => Promise<void>) => void;
}) {
  const [rows, setRows] = useState<QueueRow[]>([]);

  const refresh = () =>
    api
      .gwQueue(gwId)
      .then((r) => setRows(r.queue))
      .catch(() => undefined);

  useEffect(() => {
    setRows([]);
    refresh();
    const h = setInterval(refresh, 2000);
    return () => clearInterval(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gwId]);

  const cancel = (id: string) => {
    if (!window.confirm(`Cancel request ${id}?`)) return;
    act(async () => {
      await api.reqCancel(id);
      refresh();
    });
  };

  const force = (id: string) =>
    act(async () => {
      await api.gwQueueForce(gwId, id);
      refresh();
    });

  return (
    <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">
        Queue inspector — {gwId}
      </h3>
      {rows.length === 0 ? (
        <p className="text-xs text-zinc-600">queue empty</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-zinc-600">
              <th className="pb-1 pr-2 font-medium">request</th>
              <th className="pb-1 pr-2 font-medium">priority</th>
              <th className="pb-1 pr-2 font-medium">wait</th>
              <th className="pb-1 pr-2 font-medium">received</th>
              <th className="pb-1 font-medium">actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.request_id} className="border-t border-zinc-800/60">
                <td className="py-1.5 pr-2 font-mono text-zinc-300">{r.request_id}</td>
                <td className="py-1.5 pr-2 text-zinc-400">{r.priority}</td>
                <td className="py-1.5 pr-2 text-amber-300">{r.waiting_sec.toFixed(1)}s</td>
                <td className="py-1.5 pr-2 text-zinc-500">
                  {r.received_at ? new Date(r.received_at).toLocaleTimeString() : "—"}
                </td>
                <td className="py-1.5">
                  <div className="flex gap-1.5">
                    <Btn label="Cancel" onClick={() => cancel(r.request_id)} />
                    <Btn label="Force" title="Raise to top of queue" onClick={() => force(r.request_id)} />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
