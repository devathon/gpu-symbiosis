import { useEffect, useState } from "react";
import { api, KeyRow } from "../api";
import { Btn, IconBtn } from "./badges";

// Per-gateway API key management (spec 12 §2). The plaintext key is shown
// once at creation.
export default function GatewayKeys({
  gwId,
  act,
}: {
  gwId: string;
  act: (fn: () => Promise<void>) => void;
}) {
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [name, setName] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);

  const refresh = () =>
    api
      .gwKeys(gwId)
      .then((r) => setKeys(r.keys))
      .catch(() => undefined);

  useEffect(() => {
    setNewKey(null);
    setName("");
    refresh();
  }, [gwId]);

  const create = () =>
    act(
      async () => {
        const r = await api.gwKeyCreate(gwId, name);
        setNewKey(r.key);
        setName("");
        setKeys((await api.gwKeys(gwId)).keys);
      }
    );

  return (
    <div className="rounded border border-zinc-800 bg-zinc-950/40 p-3">
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">API keys</h3>
      {newKey && (
        <div className="mb-3 rounded border border-amber-800 bg-amber-950 px-3 py-2 text-xs text-amber-200">
          <span className="font-medium">New key (shown once — copy it now):</span>{" "}
          <code className="break-all">{newKey}</code>
          <button className="ml-2 text-amber-400 hover:text-amber-200" onClick={() => setNewKey(null)}>
            dismiss
          </button>
        </div>
      )}
      <div className="mb-2 flex gap-2">
        <input
          className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs outline-none focus:border-zinc-500"
          placeholder="key name (optional)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && create()}
        />
        <Btn label="Create key" onClick={create} />
      </div>
      {keys.length === 0 ? (
        <p className="text-xs text-zinc-600">no keys — all requests are allowed</p>
      ) : (
        <ul className="space-y-1.5">
          {keys.map((k) => (
            <li key={k.id} className="-mx-1.5 flex items-center justify-between rounded px-1.5 py-0.5 text-xs text-zinc-400 transition-colors hover:bg-zinc-800/50">
              <span>
                {k.name || k.id}
                {k.created_at && <span className="ml-2 text-zinc-600">{new Date(k.created_at).toLocaleString()}</span>}
              </span>
              <IconBtn
                label="Delete"
                icon="trash"
                danger
                onClick={() => {
                  if (window.confirm(`Delete key "${k.name || k.id}"?`))
                    act(async () => {
                      await api.gwKeyDelete(gwId, k.id);
                      setKeys((await api.gwKeys(gwId)).keys);
                    });
                }}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
