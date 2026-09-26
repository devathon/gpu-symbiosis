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
