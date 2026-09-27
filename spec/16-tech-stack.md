# Module 16 — Tech Stack (Locked)

This document locks the technology decisions for GPUSymbiosis. Changes require
re-evaluation against the risks noted here.

## 1. Locked Stack

| Layer | Technology | Notes |
| --- | --- | --- |
| Core | **Python 3.13+** | Single control-plane process, asyncio-based |
| Admin API | **FastAPI** | Typed API, Pydantic models, on `127.0.0.1:9130` default |
| Gateway / proxy | **Raw ASGI apps (Starlette-level `receive`/`send`)** | One ASGI app per gateway port; multiple uvicorn programmatic servers on one event loop |
| Upstream client | **aiohttp** | Streaming request/response + first-class WebSocket client; lowest overhead |
| Scheduler | **asyncio** | Event-driven (task/queue wakes) + 1 s reconciliation loop; `asyncio.Lock` as the scheduler lock |
| GPU monitoring | **`nvidia-ml-py`** (NVIDIA official NVML bindings) | Behind the `GpuBackend` interface ([07 §2](07-resource-manager.md#2-gpu-backend)); community `pynvml` not used |
| Process monitoring | **psutil** | CPU/RAM/threads, process-tree walk for PID attribution |
| Database | **SQLite (WAL mode) + SQLAlchemy** | DB is source of truth post-startup ([09 §8](09-data-model.md#8-startup-reconciliation)); YAML = seed/import only |
| DB migrations | Alembic | Optional; schema evolution for a single local DB |
| Frontend | **React + TypeScript + Vite** | Local dashboard, built assets served by the control plane |
| UI | **Tailwind CSS** | |
| Charts | **ECharts** | Chosen over Recharts: dense streaming time series (1 s resolution, zoom, downsample) |
| Realtime UI | **SSE** (not WebSocket) | Dashboard is one-way push; actions via POST; auto-reconnect; native in FastAPI/Starlette. WebSocket reserved for proxied application traffic only |
| Config | **YAML/TOML + Pydantic** | Validated; DB authoritative after startup |
| Logging | **structlog** | JSON structured logs ([13](13-observability.md)) |
| Packaging | **uv** | Dependency/environment management |
| Deployment | **systemd** | `Restart=always`; Fedora host |
| Auth | **argon2-cffi + signed sessions** | Admin UI per [12 §3](12-security.md#3-web-ui-security) |
| Prometheus (optional) | **prometheus-client** | Enabled via config, per [13 §3](13-observability.md#3-prometheus-compatible-metrics) |
| Testing | **pytest + pytest-asyncio** | Scheduler unit-tested against a fake `GpuBackend`; Phase 5 simulation mode doubles as integration harness |

## 2. Locked Decisions (with rationale)

### 2.1 Proxy on raw ASGI, not FastAPI's request abstraction

The gateway layer is a **pipe**, not a handler:

- stream request bodies through (large ComfyUI uploads),
- stream / SSE / chunked responses through without buffering,
- bridge WebSockets bidirectionally,
- detect client disconnect (`http.disconnect`) and cancel upstream ([05 §6.3](05-gateway-module.md#63-request-cancellation)),
- queue instead of forward when no slot is free.

FastAPI's `Request`/`Response` model fights all of these (body buffering, hand-rolled
WS bridging). Therefore:

- **Gateway ports** → raw ASGI apps built on Starlette primitives, one per port.
- **Admin API** (`/api/*`) → FastAPI, same process, same event loop.

Multi-port serving: uvicorn programmatic `Server` instances, multiple listeners,
**one process, one event loop**. Validated in the Phase 1 spike (§4).

### 2.2 `nvidia-ml-py` over `pynvml`

Same NVML ctypes bindings; `nvidia-ml-py` is the official NVIDIA-maintained
distribution. The `GpuBackend` interface keeps non-NVIDIA backends possible later.

### 2.3 SSE over WebSocket for the dashboard

The dashboard is one-way (state push; actions are POSTs). SSE is simpler,
auto-reconnects, and works through any proxy. WebSocket is used **only** in the
proxied application traffic path.

### 2.4 ECharts over Recharts

1-second-resolution time series for GPU/queue/scheduler graphs need zoom,
downsampling, and dense rendering — ECharts handles this natively; Recharts
degrades on dense streaming data.

### 2.5 Metrics ingestion bypasses the ORM

1-second samples × N applications, plus request/gateway/scheduler events, must not
churn the ORM. Rules:

- SQLAlchemy: configuration, requests, events — fine at their volume.
- `metrics_raw`: **`executemany` bulk inserts in batches** (SQLAlchemy Core or raw
  `sqlite3`), WAL mode on.
- Aggregator runs in a **worker thread/task**, batching; never blocks the event
  loop (a slow SQL aggregation must not stall proxy streaming).

### 2.6 Config source of truth

SQLite is authoritative after startup ([09 §8](09-data-model.md#8-startup-reconciliation)).
YAML/TOML is the **seed/import** format only. Seeding semantics:

- **Applications & gateways** — the seed **creates missing rows only**. An
  existing DB row is updated at startup **only if its spec sets `update: true`**
  (default `false`); otherwise the row is left untouched, so runtime changes
  made via `PUT /api/*` survive a restart. The seed never deletes rows absent
  from the document.
- **Settings** — the seed **always overwrites** (`SETTING_KEYS` upserted
  unconditionally). There is no settings API, so the YAML is the only way to
  change settings.
- `PUT /api/*` writes to the DB (and the settings audit trail).

## 3. Operational Constraints (locked)

1. **Single process, single uvicorn worker — never multi-worker.** All state
   (queues, reservations, scheduler lock) is in-process by design; the spec is one
   control plane. systemd `Restart=always` on top.
2. **No dev-mode auto-reload in production** — it breaks long-lived streams and
   in-flight generations.
3. **Heavy work off the event loop** — metrics aggregation in a worker thread/task
   with batching; script execution via `asyncio.create_subprocess_exec` (non-blocking).
4. **Client disconnect is a first-class signal** — every proxied connection watches
   `http.disconnect`; queued requests are removed, in-flight requests cancelled
   upstream where supported (Invariant 6).

## 4. Phase 1 Spikes (mandatory, before architecture settles)

| Spike | Validates | Fail condition / fallback |
| --- | --- | --- |
| **WebSocket proxy** through raw ASGI + aiohttp (ComfyUI-style WS progress stream) | highest-risk item in the whole build | if bridging proves fragile, isolate the proxy behind the module interface and evaluate a dedicated proxying dependency — do not silently drop WS support |
| **Multi-port ASGI listeners** in one process/loop (3 gateway ports + admin API) | the serving model of §2.1 | fallback: separate uvicorn processes per gateway is *not* acceptable (state must be shared); rework serving model instead |
| **SSE + long LLM stream** (multi-minute generation through the proxy, client disconnect mid-stream) | streaming + cancellation paths | adjust timeout handling; must not buffer |

**Status: all three spikes PASS** — see `spikes/phase1/RESULTS.md` (7/7 tests,
run `uv run pytest -v` in `spikes/phase1/`). The spikes surfaced six defects
recorded there (notably: `uvicorn[standard]` is required for WS; the ASGI
query string lives in `scope["query_string"]` as bytes; clean-stream-end races
in the pump/disconnect wait; WS close-initiator is unobservable at the proxy).

## 5. Explicitly Not Chosen (for the record)

- Go/Rust core — rejected: slower development, weaker GPU/ML ecosystem on the host.
- `mitmproxy` as the proxy engine — rejected: CA-certificate interception model is
  wrong for a localhost transparent pipe; adds TLS complexity.
- Separate proxy service (Caddy/nginx in front) — rejected: the queue and
  scheduler must own the connection lifecycle; splitting the proxy out breaks
  Invariant 5/6 ownership.
- PostgreSQL — rejected: zero-dependency is a goal; SQLite WAL is sufficient for a
  single-host control plane.
- WebSocket for the dashboard — rejected: one-way traffic, SSE is simpler
  (§2.3).
