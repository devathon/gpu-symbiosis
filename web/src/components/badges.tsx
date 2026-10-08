// Shared state badges + button.

// State → tone. Process, residency, gateway, and request states share names
// where the meaning is close enough (PROCESSING = busy, etc.).
const TONES: Record<string, string> = {
  RUNNING: "bg-emerald-900/60 text-emerald-300",
  STARTED: "bg-zinc-700 text-zinc-300",
  STARTING: "bg-sky-900/60 text-sky-300",
  STOPPING: "bg-amber-900/60 text-amber-300",
  STOPPED: "bg-zinc-800 text-zinc-400",
  ERROR: "bg-red-900/60 text-red-300",
  LOADED: "bg-[#FEF3C7] text-zinc-900",
  LOADING: "bg-sky-900/60 text-sky-300",
  UNLOADING: "bg-amber-900/60 text-amber-300",
  OFF: "bg-zinc-800 text-zinc-400",
  UNLOADED: "bg-zinc-800 text-zinc-400",
  IDLE: "bg-[#FEF3C7] text-zinc-900",
  PROCESSING: "bg-sky-900/60 text-sky-300",
  WAITING: "bg-amber-900/60 text-amber-300",
  DISABLED: "bg-zinc-800 text-zinc-500",
  QUEUED: "bg-amber-900/60 text-amber-300",
  COMPLETED: "bg-emerald-900/60 text-emerald-300",
  FAILED: "bg-red-900/60 text-red-300",
  CANCELLED: "bg-zinc-800 text-zinc-500",
  EXPIRED: "bg-zinc-800 text-zinc-500",
};

export function badge(state: string) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs ${TONES[state] ?? "bg-zinc-800 text-zinc-400"}`}>
      {state}
    </span>
  );
}

// The process state is now STARTED (process up) vs STOPPED etc. The UI shows a
// started app as RUNNING when it is active (serving / using the GPU), STARTED
// when idle (spec 04 §3). Other states pass through unchanged.
export function appDisplayState(processState: string, active: boolean): string {
  return processState === "STARTED" ? (active ? "RUNNING" : "STARTED") : processState;
}

// Residency OFF is shown as UNLOADED (the model is not in VRAM).
export function appResidencyDisplay(residencyState: string): string {
  return residencyState === "OFF" ? "UNLOADED" : residencyState;
}

// The residency badge is only meaningful when the app is started AND supports
// load/unload (apps without load/unload keep residency OFF — pure noise).
export function showAppResidency(processState: string, supportsLoadUnload: boolean): boolean {
  return processState === "STARTED" && supportsLoadUnload;
}

export const Btn = ({
  label,
  onClick,
  title,
}: {
  label: string;
  onClick: () => void;
  title?: string;
}) => (
  <button
    onClick={onClick}
    title={title}
    className="rounded border border-zinc-700 bg-zinc-800 px-2.5 py-1 text-xs hover:bg-zinc-700"
  >
    {label}
  </button>
);
