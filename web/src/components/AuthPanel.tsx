import { useState } from "react";
import { login, logout } from "../api";

// Admin authentication (spec 12 §3): shown only when an admin is configured
// and no session exists. In development mode (no admin) the UI is open.
export default function AuthPanel({
  onAuthed,
  error,
  onError,
}: {
  onAuthed: (username: string) => void;
  error?: string | null;
  onError: (e: string | null) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await login(username, password);
      onError(null);
      onAuthed(username);
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-sm">
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-6">
        <h2 className="mb-4 text-sm font-medium text-zinc-300">Admin sign in</h2>
        {error && <p className="mb-3 text-xs text-red-400">{error}</p>}
        <div className="space-y-3">
          <input
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-zinc-500"
            placeholder="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
          <input
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-zinc-500"
            placeholder="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
          <button
            onClick={submit}
            disabled={busy || !username || !password}
            className="w-full rounded bg-zinc-700 px-3 py-2 text-sm font-medium hover:bg-zinc-600 disabled:opacity-50"
          >
            {busy ? "signing in…" : "Sign in"}
          </button>
        </div>
      </div>
      <div className="mt-3 text-center">
        <button
          onClick={() => {
            logout().catch(() => undefined);
            window.location.reload();
          }}
          className="text-xs text-zinc-500 hover:text-zinc-300"
        >
          (switch account)
        </button>
      </div>
    </div>
  );
}
