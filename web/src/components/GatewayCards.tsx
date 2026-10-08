import { api, Decision, GwView, ScoreBreakdown } from "../api";
import { badge, Btn } from "./badges";

function num(v: number | undefined | null) {
  if (v === undefined || v === null) return "—";
  const s = Number(v).toFixed(3);
  return s.replace(/\.?0+$/, "");
}

/** Per-gateway scheduler score with a hover breakup (spec 06 §6.9 / §19).
 * Replaces the old standalone scheduler-explainer section: the score lives on
 * the gateway, and hovering it shows priority, the pressure components, the
 * demand ratio — plus, when this gateway is the current candidate, the
 * switching penalty, effective score and the scheduler's decision. */
function ScoreBadge({ g, decision }: { g: GwView; decision?: Decision }) {
  const s: ScoreBreakdown | null | undefined = g.score;
  if (!s) return <span className="text-xs text-zinc-600">score —</span>;
  const isCandidate = !!decision?.candidate_gateway_id && decision.candidate_gateway_id === g.id;
  return (
    <span className="group relative inline-flex">
      <span className="cursor-help rounded bg-indigo-500/15 px-1.5 py-0.5 text-xs font-semibold text-indigo-300 ring-1 ring-inset ring-indigo-500/30">
        score {num(s.score)}
      </span>
      <span className="pointer-events-none invisible absolute left-0 top-full z-30 mt-1 w-64 rounded-lg border border-zinc-700 bg-zinc-800 p-3 text-xs shadow-xl group-hover:visible">
        <Row k="priority" v={String(g.priority)} />
        <Row k="priority_pressure" v={num(s.priority_pressure)} />
        <Row k="backlog_pressure" v={num(s.backlog_pressure)} />
        <Row k="waiting_pressure" v={num(s.waiting_pressure)} />
        <Row k="recent_demand" v={num(s.recent_demand_pressure)} />
        <Row
          k="demand (arr/svc)"
          v={`${num(s.arrival_rate)} / ${num(s.service_rate)} = ${num(s.demand_ratio)}`}
        />
        {isCandidate && (
          <>
            <div className="my-1.5 border-t border-zinc-700" />
            <Row k="switching penalty" v={num(decision?.switching_penalty)} accent />
            <Row k="effective score" v={num(decision?.effective_score)} accent />
            {decision?.decision && (
              <div className="mt-1 font-medium text-zinc-200">
                → {decision.decision}
                {decision.detail ? <span className="ml-1 font-normal text-zinc-400">({decision.detail})</span> : null}
              </div>
            )}
          </>
        )}
      </span>
    </span>
  );
}

function Row({ k, v, accent }: { k: string; v: string; accent?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className={accent ? "text-indigo-300" : "text-zinc-400"}>{k}</span>
      <span className={accent ? "font-semibold text-indigo-200" : "text-zinc-200"}>{v}</span>
    </div>
  );
}

export default function GatewayCards({
  gws,
  act,
  decision,
  onEdit,
  onDelete,
  onKeys,
  onQueue,
  showAll,
  onToggle,
}: {
  gws: GwView[];
  act: (fn: () => Promise<void>) => void;
  decision?: Decision;
  onEdit?: (g: GwView) => void;
  onDelete?: (g: GwView) => void;
  onKeys?: (g: GwView) => void;
  onQueue?: (g: GwView) => void;
  /** Dashboard: false → hide idle/disabled gateways, true → show all. Undefined
      (the Gateways page) → always show all, no toggle. */
  showAll?: boolean;
  onToggle?: (showAll: boolean) => void;
}) {
  // The score is only meaningful while the scheduler is actively considering a
  // switch (a candidate exists); when idle (no_candidate) it is static noise.
  const switchActive =
    !!decision && !!decision.decision && decision.decision !== "no_candidate";

  // Filter only when the dashboard toggle is present; the Gateways page (no
  // onToggle) always shows every gateway.
  const visible = showAll === false ? gws.filter((g) => g.state !== "IDLE" && g.state !== "DISABLED") : gws;
  const hidden = gws.length - visible.length;

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500">Gateways</h2>
        {onToggle && (
          <button
            onClick={() => onToggle(!showAll)}
            title={showAll ? "Show active only" : hidden > 0 ? `Show all (${hidden} hidden)` : "Show all"}
            aria-label={showAll ? "Show active gateways only" : "Show all gateways"}
            className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-xs ${
              showAll ? "bg-zinc-700 text-zinc-100" : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {showAll ? <path d="m18 15-6-6-6 6" /> : <path d="m6 9 6 6 6-6" />}
            </svg>
            {showAll ? "active" : "all"}
          </button>
        )}
      </div>
      <div className="space-y-3">
        {visible.map((g) => (
          <div key={g.id} className="flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{g.name}</span>
                {g.state !== "IDLE" && badge(g.state)}
                {switchActive && <ScoreBadge g={g} decision={decision} />}
                {!g.enabled && <span className="rounded bg-zinc-800 px-1.5 text-xs text-zinc-500">disabled</span>}
              </div>
              <div className="mt-1 text-xs text-zinc-500">
                :{g.port} → {g.application_id} · priority {g.priority} · queue {g.queue.length}
                {g.queue.length > 0 && ` (oldest ${g.queue.oldest_wait_sec}s)`}
              </div>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {g.queue.length > 0 && <Btn label="Clear" onClick={() => act(() => api.gwClear(g.id))} />}
              <Btn
                label="Process Now"
                onClick={() => act(() => api.gwProcessNow(g.id))}
                title="Raise this gateway's scheduling priority (time-decayed boost; never kills active work)"
              />
              {g.enabled ? (
                <Btn label="Disable" onClick={() => act(() => api.gwDisable(g.id))} />
              ) : (
                <Btn label="Enable" onClick={() => act(() => api.gwEnable(g.id))} />
              )}
              {onEdit && <Btn label="Edit" onClick={() => onEdit(g)} />}
              {onDelete && (
                <Btn
                  label="Delete"
                  onClick={() => {
                    if (window.confirm(`Delete gateway ${g.name} (:${g.port})?`)) onDelete(g);
                  }}
                />
              )}
              {onKeys && <Btn label="Keys" onClick={() => onKeys(g)} />}
              {onQueue && <Btn label="Queue" onClick={() => onQueue(g)} />}
            </div>
          </div>
        ))}
        {visible.length === 0 && (
          <p className="text-sm text-zinc-600">
            {gws.length === 0 ? "no gateways" : `all ${gws.length} idle/disabled — show all`}
          </p>
        )}
      </div>
    </section>
  );
}
