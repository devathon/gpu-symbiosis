import type { Decision, ScoreBreakdown } from "../api";

// spec 11 §5 — the Scheduler Explainer panel.
// Shows why the scheduler made its current decision: the gateway score, the
// switching penalty, the effective vs current score, and the next switch
// threshold — backed by GET /api/scheduler/current-decision.

const num = (n?: number, d = 2) => {
  if (n === undefined || n === null) return "—";
  const s = n.toFixed(d);
  // Strip trailing zeros only after a decimal point (never from integers:
  // "30" must stay "30", not become "3").
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
};

function Row({ label, value, hint, accent }: { label: string; value: string; hint?: string; accent?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-zinc-800/60 py-1.5 last:border-0">
      <span className="text-xs text-zinc-500">
        {label}
        {hint && <span className="ml-1 text-zinc-600">{hint}</span>}
      </span>
      <span className={`font-mono text-sm ${accent ?? "text-zinc-300"}`}>{value}</span>
    </div>
  );
}

function Col({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <h3 className="mb-1.5 text-[11px] uppercase tracking-wider text-zinc-500">{title}</h3>
      <div>{children}</div>
    </div>
  );
}

export default function SchedulerExplainer({ d }: { d: Decision }) {
  const sc = d.scores as ScoreBreakdown | undefined;
  const v = d.vram;
  const st = d.switch_tracking;
  const g = d.guards;
  const cfg = d.config;

  const decision = d.decision ?? "no_candidate";
  const decColor =
    decision === "switch" || decision === "emergency_switch"
      ? "text-emerald-400"
      : decision === "keep"
        ? "text-amber-400"
        : decision === "drain"
          ? "text-sky-400"
          : decision === "activate_concurrent"
            ? "text-emerald-400"
            : "text-zinc-400";

  const threshold =
    d.current_score !== undefined && cfg?.switch_margin !== undefined
      ? d.current_score + cfg.switch_margin
      : undefined;

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider text-zinc-500">Scheduler Explainer</h2>
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-500">trigger</span>
          <span className="font-mono text-xs text-zinc-400">{d.trigger ?? "—"}</span>
          <span className="text-xs text-zinc-500">decision</span>
          <span className={`font-mono text-sm font-semibold ${decColor}`}>{decision}</span>
        </div>
      </div>

      <div className="grid gap-x-6 gap-y-4 md:grid-cols-3">
        <Col title="Active / Candidate">
          <Row label="Active app" value={d.active_application_id ?? "—"} accent="text-zinc-100" />
          <Row
            label="Candidate app"
            value={d.candidate_application_id ?? "—"}
            hint={d.candidate_gateway_id ? `gw ${d.candidate_gateway_id}` : undefined}
            accent="text-sky-300"
          />
          <Row label="Resident apps" value={(d.residents ?? []).join(", ") || "—"} />
          {Object.keys(d.boosts ?? {}).length > 0 && (
            <Row
              label="Process-Now boost"
              value={Object.entries(d.boosts!).map(([gw, s]) => `${gw} ${num(s, 0)}s`).join(", ")}
              accent="text-purple-300"
            />
          )}
        </Col>

        <Col title="Gateway Score">
          {sc ? (
            <>
              <Row label="Gateway score" value={num(sc.score, 3)} accent="text-zinc-100" />
              <Row
                label="priority_pressure"
                value={num(sc.priority_pressure, 3)}
                hint={`backlog ${num(sc.backlog_pressure, 3)}`}
              />
              <Row label="waiting_pressure" value={num(sc.waiting_pressure, 3)} />
              <Row
                label="recent_demand_pressure"
                value={num(sc.recent_demand_pressure, 3)}
                hint={`d ${num(sc.demand_ratio, 2)}`}
              />
              <Row
                label="arrival / service"
                value={`${num(sc.arrival_rate, 1)} / ${num(sc.service_rate, 1)}`}
                hint="req/min"
              />
            </>
          ) : (
            <p className="py-2 text-sm text-zinc-600">no candidate queued</p>
          )}
        </Col>

        <Col title="Switching">
          <Row
            label="Switching penalty"
            value={num(d.switching_penalty, 2)}
            hint={cfg ? `weight ${num(cfg.switch_cost_weight, 2)}` : undefined}
          />
          <Row label="Effective score" value={num(d.effective_score, 3)} accent="text-zinc-100" />
          <Row label="Current score" value={num(d.current_score, 3)} />
          <Row
            label="Next switch threshold"
            value={threshold !== undefined ? num(threshold, 3) : "—"}
            hint={cfg ? `+ margin ${num(cfg.switch_margin, 2)}` : undefined}
            accent="text-emerald-300"
          />
        </Col>
      </div>

      {(g || cfg) && (
        <div className="mt-4 grid gap-x-6 gap-y-4 border-t border-zinc-800 pt-3 md:grid-cols-2">
          <Col title="Anti-thrash Guards">
            <Row
              label="Min residency"
              value={`${num(g?.min_residency_left, 1)}s / ${num(cfg?.min_residency_sec, 0)}s`}
              hint="remaining / limit"
            />
            <Row
              label="Cooldown"
              value={`${num(g?.cooldown_left, 1)}s / ${num(cfg?.switch_cooldown_sec, 0)}s`}
              hint="remaining / limit"
            />
            <Row
              label="Emergency"
              value={g?.emergency ? "ACTIVE" : "—"}
              hint={cfg?.emergency_wait_sec ? `wait ≥ ${num(cfg.emergency_wait_sec, 0)}s` : undefined}
              accent={g?.emergency ? "text-amber-300" : undefined}
            />
            <Row label="Switches (session)" value={String(st?.switch_count ?? 0)} />
          </Col>
          <Col title="VRAM Accounting">
            <Row label="Total / reserve" value={`${num(v?.total_mb, 0)} / ${num(v?.reserve_mb, 0)}`} hint="MB" />
            <Row label="Resident (apps)" value={num(v?.resident_mb, 0)} hint="MB" />
            <Row label="Reserved (slots)" value={num(v?.reserved_mb, 0)} hint="MB" />
            <Row label="External (other)" value={num(v?.external_mb, 0)} hint="MB" />
            <Row label="Free" value={num(v?.free_mb, 0)} hint="MB" accent="text-emerald-300" />
          </Col>
        </div>
      )}

      {d.detail && (
        <div className="mt-3 rounded bg-zinc-950/60 px-3 py-2 font-mono text-xs text-zinc-400">
          {d.detail}
        </div>
      )}
    </section>
  );
}
