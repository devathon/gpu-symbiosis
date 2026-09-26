# Module 15 — Delivery Plan (MVP Phases)

The project is implemented in phases. Each phase is independently useful and
shippable; later phases extend, not restructure, earlier ones.

## Phase 1 — Core  ✅ **DONE**

```text
Application management
Gateway management
GPU monitoring
Start / stop
Load / unload
Basic proxy
Request queue
Basic scheduler
```

Delivers the fundamental loop: request arrives → app not resident → queue →
drain/unload → load → forward. Single-app switching with the critical unloading
rule (Invariant 1) enforced from day one.

Phase 1 also includes the mandatory tech-stack spikes (WebSocket proxy, multi-port
ASGI listeners, SSE + long-stream cancellation) — see
[16 §4](16-tech-stack.md#4-phase-1-spikes-mandatory-before-architecture-settles).

**Definition of done:** the core example from [01 §3](01-overview.md#3-core-example)
works end-to-end: A1 loaded on a 24 GB GPU, request to A2's gateway queues, A1
drains and unloads after its active request completes, A2 loads, the request
streams back.

## Phase 2 — Intelligent Scheduling  ✅ **DONE**

Add:

```text
priority
queue pressure
waiting pressure
EWMA demand
service rate
switch cost
minimum residency
switch margin
```

Delivers the full gateway scoring model ([06 §6](06-scheduler.md#6-gateway-score))
and anti-thrash controls ([06 §16](06-scheduler.md#16-thrashing-prevention)), plus
the Scheduler Explainer UI.

**Implementation notes:**

- Scoring lives in `gpusybio/scoring.py` (pure module, unit-tested): gateway
  score = `priority_pressure + waiting_pressure + recent_demand_pressure`
  ([06 §6.9](06-scheduler.md)), backlog = max of queue/wait ratios with no
  clamping (§6.1), demand ratio normalized by `d/(1+d)` (§6.6).
- Demand tracking (spec 06 §6.3/§6.4) is a time-decayed EWMA (τ = 60 s) of
  arrivals per gateway and completions per application, observed in the
  RequestManager (arrival on `receive`, service on COMPLETED).
- Decision order per [06 §14](06-scheduler.md#14-decision-order): zero-cost
  resident path → concurrent load when it fits → eviction path. A busy
  incumbent is **drained, not killed** (spec 06 §2): it is marked DRAINING and
  the switch completes on request completion (§17). New requests to a draining
  app queue instead of forwarding.
- Guards: minimum residency (§12), switch margin (§13), emergency override
  (§15 — overrides residency, never the drain), switch cooldown + full
  switch bookkeeping (§16).
- Process Now (§9) is a time-decaying priority boost (default 2×, TTL 120 s,
  both configurable); it never kills active work.
- The explainability payload (§19) is surfaced by
  `GET /api/scheduler/current-decision` and the Scheduler Explainer UI
  ([11 §5](11-web-ui.md#5-scheduler-explainer)): score breakdown, penalty,
  effective vs current score, next-switch threshold, VRAM accounting,
  residents, guard state, and switch history.

**Decision — manual over-allocation (closed Phase 1 loose end):** admin
`start`/`load` enforce the budget: if the app's required VRAM (incl. safety
margin) does not fit alongside current residents, the transition is rejected
with **409** + `{required_mb, free_mb, residents}`. Invariant 2
([14](14-invariants.md)) therefore holds globally, not only on the automatic
scheduler path; an already-resident app is never blocked (zero-cost pass).

## Phase 3 — Metrics  ✅ **DONE**

Add:

```text
historical metrics
application metrics
type metrics contract   (08 §9)
llama metrics
token metrics
GPU history
CPU history
```

Delivers [08](08-metrics-module.md) fully: retention/aggregation pipeline, latency
percentiles, token metrics, active-time tracking, event log UI — including the
extensible **TypeSpec / type-metrics contract** ([08 §9](08-metrics-module.md#9-type-metrics-contract-extensible-application-types)):
type registry, built-in + custom handlers, `/api/applications/{id}/metrics`, and the
auto-rendered per-type UI panel.

## Phase 4 — Advanced Residency  ✅ **DONE**

Add:

```text
multi-application residency
automatic idle detection
ComfyUI activity detection
automatic unloading
application retention scoring
```

Delivers the full resource-residency model ([02 §3](02-architecture.md#3-key-architectural-decision)):
coexistence, non-API activity detection ([06 §10](06-scheduler.md#10-non-api-applications--idle-unloading)),
unload candidate scoring ([06 §3](06-scheduler.md#3-unload-candidate-selection)).

Done: two-app coexistence (`tests/test_residency.py`), a background `IdleUnloader`
classifying API apps by in-flight requests and non-API apps by GPU utilization,
automatic idle unload (same hard exclusions + min-residency + pending-demand
guards as the eviction path), `GET /api/idle/status`, and the scheduler
knobs `idle_unload_timeout_sec` / `idle_unload_interval_sec` plus per-app
`idle_unload_timeout_sec` / `activity_detection_method` / `idle_threshold_gpu_pct`
(`tests/test_idle.py`).

## Phase 5 — Advanced Features  ✅ **DONE**

Add:

```text
pinned applications
request cancellation
WebSocket
streaming hardening
Prometheus
authentication
load failure recovery
scheduler simulation
```

Note: basic streaming already works from Phase 1 (it is a proxy requirement, see
[05 §8](05-gateway-module.md#8-streaming-requests)); Phase 5 extends it
(WebSocket, multi-app concurrent streaming, cancellation propagation).
Scheduler simulation = what-if mode that evaluates the scoring model against
historical data without acting.

## Cross-Phase Requirements

- Invariants 1–7 ([14](14-invariants.md)) are enforced **from Phase 1** and never
  relaxed.
- Structured logging and the event log exist from Phase 1 (observability is a
  Phase 1 concern, not a Phase 3 one).
- Loopback-default binding and admin authentication are live before any
  non-local binding is enabled (Phase 5 hardening).

## Status

- **Phase 1 — DONE.** The definition of done from §Phase 1 is met end-to-end and
  covered by `tests/test_e2e_core_loop.py` (real HTTP against a fake GPU backend):
  A1 resident → request to A2's gateway queues → A1 drains+unloads after its
  request completes → A2 loads → the request streams back; plus the zero-cost
  resident path. The mandatory tech-stack spikes are green (7/7,
  `spikes/phase1/`). A minimal web UI (dashboard + live SSE event log) serves at
  the admin port. All cross-phase requirements above are in force.

  Known Phase 1 simplifications (deferred, per this plan): WebSocket proxying
  (Phase 5), concurrent multi-app streaming (Phase 5), buffer-less request bodies,
  and live listener spawn for API-created gateways.

- **Phase 2 — DONE.** The full gateway scoring model and anti-thrash controls are
  live and covered by `tests/test_scoring.py` (unit), `tests/test_e2e_core_loop.py`,
  and `tests/test_scheduling_behavior.py`. The Scheduler Explainer UI
  ([11 §5](11-web-ui.md#5-scheduler-explainer)) is served at the admin port.

- **Phase 3 — DONE.** The metrics module ([08](08-metrics-module.md)) is live:
  1-second sampling, retention/aggregation pipeline (raw 24 h, 1-min 30 d, hourly
  indefinite), request & latency percentiles (P50/P95/P99), token metrics,
  active-time tracking, the **TypeSpec / type-metrics contract** ([08 §9]) with
  built-in + generic handlers, `GET /api/applications/{id}/metrics`, and the
  auto-rendered per-type UI panel. Covered by `tests/test_metrics.py` (unit + E2E).

- **Phase 4 — DONE.** The full resource-residency model is live: multi-application
  coexistence ([02 §3]) — `tests/test_residency.py` boots two 8 GB apps on the 24 GB
  fake GPU and asserts both stay resident and each serves its own gateway. A
  background `IdleUnloader` performs automatic idle detection ([06 §10]) — API apps
  by in-flight requests, non-API apps (e.g. ComfyUI) by GPU utilization against a
  per-app threshold — and automatic idle unloading subject to the same hard
  exclusions, minimum-residency and pending-demand guards as the eviction path
  ([06 §3.1]), with `GET /api/idle/status` and the scheduler knobs
  `idle_unload_timeout_sec` / `idle_unload_interval_sec`. Retention scoring
  ([06 §3]) is the Phase 2 eviction scorer. Covered by `tests/test_idle.py` and
  `tests/test_residency.py`.

- **Phase 5 (MVP) — DONE.** The remaining MVP features are live:

  - **Gateway API keys** ([12 §2]): per-gateway `Authorization: Bearer <key>`
    checked before queueing (invalid key → 401, never enqueued); key CRUD +
    rotation via the admin API. Auth is optional in development — no keys
    configured → allow all. Covered by `tests/test_auth.py`.
  - **Admin authentication** ([12 §3]): local admin accounts with salted scrypt
    hashing (`scrypt$<salt>$<hash>`), session-based auth (httpOnly
    SameSite=Strict cookie), CSRF protection (double-submit token) on
    state-changing endpoints, and explicit confirmation for destructive actions
    (Force Stop / Delete / Clear Queue). Auth is optional in development — open
    until the first admin exists. Covered by `tests/test_admin_auth.py`.
  - **Request cancellation** ([05 §6.3]): cancel a queued or in-flight request
    via `POST /api/requests/{id}/cancel` (202); the proxy wakes the client and
    returns 409 "request cancelled" when the request reaches a terminal state.
    Covered by `tests/test_phase5_misc.py`.
  - **Pinned applications** ([06 §3.1]): pinned apps are excluded from automatic
    idle unloading and eviction; `POST/DELETE /api/applications/{id}/pin`.
    Covered by `tests/test_phase5_misc.py`.
  - **Load-failure recovery** ([06 §9]): repeated load failures trip a faulted
    back-off (the app is not retried while faulted) and are surfaced in the
    application status; clearing the fault re-enables loading. Covered by
    `tests/test_phase5_misc.py`.
  - **WebSocket + streaming hardening**: the gateway proxies WebSocket traffic
    to the application, and concurrent multi-application streaming keeps each
    client's stream isolated (no cross-talk). Covered by
    `tests/test_phase5_streaming.py`.
  - **Scheduler simulation (what-if)**: `POST /api/scheduler/simulate` runs the
    scoring model against the current state without acting — it reports the
    decision + scores it *would* make and doubles as an integration harness
    ([16](16-integration.md)). Covered by `tests/test_phase5_simulate.py`.

  The full Phase 5 suite passes alongside the existing tests (54 passing).
