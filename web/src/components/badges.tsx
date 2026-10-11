// Shared state badges + button.

// State → tone. Process, residency, gateway, and request states share names
// where the meaning is close enough (PROCESSING = busy, etc.).
const TONES: Record<string, string> = {
  RUNNING: "bg-emerald-900/60 text-emerald-300",
  // STARTED (process up, not actively serving): light gray badge with dark
  // green text — clearly distinct from STOPPED (dark/neutral) and RUNNING
  // (bright green), without the "running" highlight.
  STARTED: "bg-[#ccc] text-[#0b7700]",
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
  PAUSED: "bg-violet-900/60 text-violet-300",
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

// Icon-only button for the compact card rows (mobile friendly). The label is
// the aria-label and the hover tooltip, so actions stay discoverable.
export const ICONS = {
  edit: ["M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z", "m15 5 4 4"],
  pause: ["M6 4h4v16H6z", "M14 4h4v16h-4z"],
  play: ["M6 3l14 9-14 9V3z"],
  trash: [
    "M3 6h18",
    "M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6",
    "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
    "M10 11v6",
    "M14 11v6",
  ],
  power: ["M18.36 6.64a9 9 0 1 1-12.73 0", "M12 2v10"],
  pin: [
    "M12 17v5",
    "M15 9.34V7a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v2.34a2 2 0 0 1-.59 1.42L7 13.25a1 1 0 0 0-.29 1.42l.42.57a1 1 0 0 0 1.29.25L11 14.5V17h2v-2.5l2.58 1.99a1 1 0 0 0 1.29-.25l.42-.57a1 1 0 0 0-.29-1.42l-1.41-1.49A2 2 0 0 1 15 9.34z",
  ],
  pinOff: [
    "M12 17v5",
    "M15 9.34V7a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v2.34a2 2 0 0 1-.59 1.42L7 13.25a1 1 0 0 0-.29 1.42l.42.57a1 1 0 0 0 1.29.25L11 14.5V17h2v-2.5l2.58 1.99a1 1 0 0 0 1.29-.25l.42-.57a1 1 0 0 0-.29-1.42l-1.41-1.49A2 2 0 0 1 15 9.34z",
    "M3 3l18 18",
  ],
  stop: ["M5 5h14v14H5z"],
  forceStop: ["M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86L7.86 2z", "m15 9-6 6", "m9 9 6 6"],
  load: ["M12 3v12", "m7 10 5 5 5-5", "M5 21h14"],
  unload: ["M12 21V9", "m7 14 5-5 5 5", "M5 3h14"],
  copy: ["M8 8h14v14H8z", "M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"],
  fastForward: ["m13 19 9-9-9-9", "m2 19 9-9-9-9"],
} as const;

export const IconBtn = ({
  label,
  icon,
  onClick,
  title,
  danger,
}: {
  label: string;
  icon: keyof typeof ICONS;
  onClick: () => void;
  title?: string;
  danger?: boolean;
}) => (
  <button
    type="button"
    aria-label={label}
    title={title ?? label}
    onClick={onClick}
    className={`rounded p-1.5 transition-colors ${
      danger ? "text-red-400 hover:bg-red-950/60" : "text-zinc-400 hover:bg-zinc-700 hover:text-zinc-100"
    }`}
  >
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {ICONS[icon].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  </button>
);
