# GPUSymbiosis

> Intelligent coexistence of AI workloads on a shared GPU.

GPUSymbiosis is a local GPU resource orchestration system. It manages a limited GPU
VRAM resource by treating GPU applications as **dynamically resident workloads** rather
than permanently running processes: it monitors running applications, accepts requests
through gateway ports, queues requests when the target application is not resident,
decides which applications should be resident, loads/unloads them, forwards requests,
guarantees that no request is ever interrupted mid-flight, tracks historical demand and
performance, and exposes a web UI for configuration and monitoring.

## Target Workloads

- LLM inference servers (llama.cpp / `llama-server`)
- Ollama
- ComfyUI (image generation)
- TTS (e.g. Breeze TTS)
- Video generation
- AI agent harnesses
- Embedding / reranking servers
- Other GPU workloads

## Specification Modules

This repository holds the functional and technical specification, organised module-wise.

| Doc | Module | Scope |
| --- | --- | --- |
| [01-overview.md](spec/01-overview.md) | Overview & Goals | Problem statement, core example, non-goals |
| [02-architecture.md](spec/02-architecture.md) | Architecture | High-level design, component map, key architectural decision, service layout |
| [03-application-model.md](spec/03-application-model.md) | Application Model | Application fields, types, scripts, memory model & learning |
| [04-application-manager.md](spec/04-application-manager.md) | Application Manager | Lifecycle, process states, timing measurement, handlers/plugins, dead process detection |
| [05-gateway-module.md](spec/05-gateway-module.md) | Gateway & Proxy | Gateway model, queues, proxy behaviour, streaming, request lifecycle & cancellation |
| [06-scheduler.md](spec/06-scheduler.md) | Scheduler | Resource-residency scheduling, scoring, switching algorithm, coexistence, emergency switching, state machine |
| [07-resource-manager.md](spec/07-resource-manager.md) | Resource Manager | GPU/CPU monitoring, NVML, process attribution, reservations, concurrency & locking |
| [08-metrics-module.md](spec/08-metrics-module.md) | Metrics & History | Sampling, EWMA demand, latency percentiles, token metrics, retention policy, event history, extensible type-metrics contract (TypeSpec) |
| [09-data-model.md](spec/09-data-model.md) | Data Model & Database | SQLite schema, tables, retention & aggregation |
| [10-api.md](spec/10-api.md) | REST API | Application, gateway, monitoring, proxy APIs; configuration reference |
| [11-web-ui.md](spec/11-web-ui.md) | Web UI | Dashboard, application & gateway management, scheduler explainer, event log |
| [12-security.md](spec/12-security.md) | Security | Bind addresses, authentication, API keys, script sandboxing |
| [13-observability.md](spec/13-observability.md) | Observability | Structured logs, scheduler decision logging, Prometheus metrics |
| [14-invariants.md](spec/14-invariants.md) | Invariants & Error Handling | Critical invariants, failed-switch recovery, load failure protection, retry policy |
| [15-mvp-phases.md](spec/15-mvp-phases.md) | Delivery Plan | MVP phases 1–5 |
| [16-tech-stack.md](spec/16-tech-stack.md) | Tech Stack (Locked) | Python 3.13 / FastAPI + raw-ASGI proxy / aiohttp / nvidia-ml-py / SQLite + SQLAlchemy / React + ECharts + SSE / systemd |

## Fundamental Principle

> **GPU applications should be treated as dynamically resident workloads rather than
> permanently running processes.**

## Core Architectural Decision

The scheduler is **not** an "application switcher" — it is a **resource-residency
scheduler**. It answers three independent questions on every evaluation:

1. Which gateway/request should be served?
2. Which applications need to be resident to serve it?
3. Which resident applications can be removed to make room?

This handles multiple gateways pointing at one application **and** multiple
applications coexisting within VRAM, and scales from a 24 GB GPU today to much larger
GPUs later where several LLMs, TTS, and image models remain resident simultaneously.

## Implementation — Phase 1 (done)

Phase 1 is implemented per [15 §Phase 1](spec/15-mvp-phases.md) and the definition of
done from [01 §3](spec/01-overview.md#3-core-example) is met end-to-end: A1 resident,
a request to A2's gateway queues, A1 drains and unloads after its request completes,
A2 loads, and the request streams back — enforced by
[tests/test_e2e_core_loop.py](tests/test_e2e_core_loop.py) against real HTTP
endpoints with a fake GPU backend.

### Quick start

```bash
# Python (uv) — run the orchestrator with a config file (see spec/10 §6):
uv run python devrun.py            # dev config with mock GPU apps
# or:  uv run python -m gpusybio.server --config config.yaml

# Web UI (optional):
cd web && npm install && npm run build
# the admin API serves the bundle at http://127.0.0.1:9530/
# (dev mode: npm run dev → http://localhost:5173, proxies /api to :9530)

# Tests:
uv run pytest                      # core loop + zero-cost invariants
cd spikes/phase1 && uv run pytest  # tech-stack spikes (7/7)
```

### Layout

```
gpusybio/
  config/        YAML config + pydantic models (spec 10 §6)
  database/      SQLite (WAL) + SQLAlchemy 2.0 (spec 09)
  events/        event bus, structured logging (spec 13), SSE hub (spec 11 §1)
  resources/     GPU backend (NVML / fake) + resource manager (spec 07)
  applications/  process lifecycle + app manager (spec 04)
  gateway/       per-gateway queue + request manager + ASGI proxy (spec 05)
  scheduler/     resource-residency scheduler (spec 06)
  metrics/       sampling + history (spec 08)
  api/           admin REST API (spec 10)
  server.py      composition root: one process, one loop, many listeners (spec 02 §5)
web/             React + TS + Vite + Tailwind + ECharts dashboard (spec 11)
spikes/phase1/   locked-stack spikes: WebSocket proxy, multi-port ASGI, SSE
tests/           E2E core-loop tests + mock GPU app
```

Key Phase 1 behaviours (see the spec for details):

- **Zero-cost resident path** — a request to a resident, idle app is forwarded
  immediately; the scheduler never drains/unloads/reloads an app that is already
  resident ([14](spec/14-invariants.md), invariant 1).
- **Queueing** — requests to a non-resident app wait in the gateway's queue
  `(priority, queued_at)`; full queue → HTTP 429.
- **Safe switching** — an evicted app is only drained when it has no active
  requests; in-flight requests are never interrupted.
- **Gateway-level waiting** — two gateways may point at one app; the second
  gateway's request waits at the gateway even when the app is active
  ([05 §7](spec/05-gateway-module.md)).
- **Structured event log** — every lifecycle/scheduler decision is logged and
  persisted; the dashboard streams it live over SSE.

## Implementation — Phase 2 (done): Intelligent Scheduling

Phase 2 implements the full gateway scoring model
([06 §6](spec/06-scheduler.md#6-gateway-score)) and anti-thrash controls
([06 §16](spec/06-scheduler.md#16-thrashing-prevention)), per
[15 §Phase 2](spec/15-mvp-phases.md#phase-2--intelligent-scheduling--done).

Every scheduler evaluation now scores each queued gateway:

```
gateway_score = priority_pressure + waiting_pressure + recent_demand_pressure
```

with EWMA arrival rate (per gateway) and service rate (per application,
completed requests/min), the demand ratio normalized by `d / (1 + d)`
([06 §6.6](spec/06-scheduler.md)), and a backlog that is the max of the queue
and wait ratios — **allowed to exceed 1** so overdue requests stay urgent
([06 §6.1](spec/06-scheduler.md)). The candidate is the argmax; the decision
then follows the order in
[06 §14](spec/06-scheduler.md#14-decision-order):

1. **Zero-cost resident path** — candidate already resident → activate, no
   switch, no cost.
2. **Concurrent load** — candidate fits alongside residents → load + activate.
3. **Eviction path** — a victim is selected (spec 06 §3); a *busy* victim is
   **drained, not killed**: it is marked DRAINING and the switch completes only
   after its active requests finish ([06 §17](spec/06-scheduler.md)).

Guards and controls:

- **Minimum residency** ([06 §12](spec/06-scheduler.md)) — a young resident
  can't be evicted yet; the switch waits for the window to elapse.
- **Switch margin** ([06 §13](spec/06-scheduler.md)) — the candidate's
  effective score (score − switching cost) must exceed the incumbent's score
  plus a margin before it switches.
- **Emergency override** ([06 §15](spec/06-scheduler.md)) — a very old wait or
  an oversized queue overrides minimum residency, but never the drain.
- **Switch cooldown + bookkeeping** ([06 §16](spec/06-scheduler.md)) —
  re-switching the same pair within the cooldown is suppressed; every switch is
  counted and its pair/previous-application recorded.
- **Process Now** ([06 §9](spec/06-scheduler.md)) — a time-decaying priority
  boost for one gateway; it changes which request is picked next but never
  kills in-flight work.

The **Scheduler Explainer** panel (spec 11 §5) shows why the current decision
was made: score breakdown, switching penalty, effective vs current score, the
next-switch threshold, VRAM accounting, residents, guard state, and switch
history — backed by `GET /api/scheduler/current-decision`.

**Over-allocation is now rejected.** Admin `start`/`load` enforce Invariant 2
(never exceed the VRAM budget): if an app's required VRAM (incl. safety
margin) doesn't fit alongside the current residents, the transition returns
**409** with `{required_mb, free_mb, residents}`. An already-resident app is
never blocked (the zero-cost path adds no VRAM). This closes the Phase 1 loose
end where a manual `start`/`load` could silently over-commit the GPU.

### Tests

```bash
uv run pytest tests/test_scoring.py                 # scoring model + guards (unit)
uv run pytest tests/test_e2e_core_loop.py           # core loop under the full model
uv run pytest tests/test_scheduling_behavior.py     # residency / margin / emergency / boost / cooldown / 409
```
