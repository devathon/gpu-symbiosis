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

## Phase 2 — Intelligent Scheduling

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

## Phase 3 — Metrics

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

## Phase 4 — Advanced Residency

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

## Phase 5 — Advanced Features

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
