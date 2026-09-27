# Module 10 — REST API & Configuration

Two distinct API surfaces:

1. **Management API** — JSON, admin-authenticated (see [12](12-security.md)), on the
   control-plane port (default `127.0.0.1:9130`).
2. **Gateway proxy API** — the unchanged application API, on each gateway port,
   no extra auth beyond the application's own (plus optional gateway auth, see
   [12 §2](12-security.md#2-gateway-authentication)).

## 1. Applications API

```text
GET    /api/applications
POST   /api/applications
GET    /api/applications/:id
PUT    /api/applications/:id
DELETE /api/applications/:id

POST   /api/applications/:id/start
POST   /api/applications/:id/stop
POST   /api/applications/:id/stop?force=true
POST   /api/applications/:id/load
POST   /api/applications/:id/unload
```

Behaviour notes:

- `PUT` while the application is LOADED: resource fields (memory, margin) are
  updatable live; script/port changes require the app to be OFF.
- `DELETE` only allowed when `STOPPED` and no gateways reference it (or force).
- `start`/`stop`/`load`/`unload` are synchronous: they return 202 with a transition
  id; the state change is visible via `GET /api/applications/:id` (or a `watch`
  endpoint / SSE stream). Manual unload with active requests returns 202 in DRAINING.
- `start`/`load` enforce the VRAM budget (Invariant 2, [14](14-invariants.md)):
  if the app's required VRAM (incl. safety margin) does not fit alongside the
  current residents, the transition is rejected with **409** and
  `{required_mb, free_mb, residents}`. An already-resident app passes (the
  zero-cost path needs no additional VRAM).

### Application Resource Shape

```json
{
  "id": "qwen38",
  "name": "Qwen 3.8",
  "type": "llama",
  "enabled": true,
  "process": { "state": "RUNNING", "pid": 12345 },
  "residency": "LOADED",
  "ui_state": "PROCESSING",
  "port": 3000,
  "gpu": {
    "memory_required_mb": 18500,
    "memory_observed_max_mb": 19210,
    "memory_current_mb": 18532,
    "usage_pct": 72,
    "power_w": 112,
    "temperature_c": 67
  },
  "cpu": { "usage_pct": 18, "memory_mb": 8396, "threads": 24 },
  "timing": {
    "startup_sec": 7.2, "load_sec": 3.8,
    "unload_sec": 2.1, "stop_sec": 0.4
  },
  "lifecycle": {
    "allow_automatic_unload": true,
    "supports_load_unload": false,
    "idle_unload_timeout_sec": null
  },
  "active_time_sec": 9060,
  "requests": {
    "total": 412, "active": 1, "queued": 0,
    "p50_sec": 1.8, "p95_sec": 6.2, "p99_sec": 11.0
  }
}
```

## 2. Gateways API

```text
GET    /api/gateways
POST   /api/gateways
GET    /api/gateways/:id
PUT    /api/gateways/:id
DELETE /api/gateways/:id

POST   /api/gateways/:id/enable
POST   /api/gateways/:id/disable
POST   /api/gateways/:id/process-now
POST   /api/gateways/:id/clear-queue
```

- `DELETE` only when queue empty and `active_requests = 0`, else `409` (or `?force=true`).
- `process-now` returns the boost expiry.
- `clear-queue` returns the number of cancelled queued requests (active requests untouched).

### Gateway Resource Shape

```json
{
  "id": "gw-qwen38",
  "name": "Qwen 3.8 API",
  "port": 913000,
  "application_id": "qwen38",
  "state": "PROCESSING",
  "enabled": true,
  "priority": 10,
  "queue": {
    "length": 3, "max": 10,
    "oldest_wait_sec": 18, "max_wait_sec": 60
  },
  "active_requests": 1,
  "score": 10.0,
  "demand": {
    "arrival_rate": 8.0, "service_rate": 4.0,
    "demand_ratio": 2.0, "normalized_demand": 0.67
  }
}
```

## 3. Monitoring API

```text
GET /api/system
GET /api/gpu
GET /api/applications/:id/metrics?window=15m
GET /api/gateways/:id/metrics?window=1h
GET /api/requests?status=QUEUED&limit=50
GET /api/events?type=scheduler&limit=100&since=...
GET /api/scheduler/current-decision
GET /api/metrics/prometheus          (optional, see 13 §3)
```

`/api/system`:

```json
{
  "gpu": {
    "total_mb": 24576, "used_mb": 20480, "free_mb": 4096,
    "usable_mb": 22528, "reserve_mb": 2048,
    "vram_pct": 83.4, "gpu_pct": 61,
    "temperature_c": 67, "power_w": 112, "power_limit_w": 250,
    "name": "NVIDIA RTX 5090", "pcie": "PCIe Gen 5 @ 16x",
    "clock_mhz": 2520, "rx_mbps": 512.4, "tx_mbps": 128.9
  },
  "resident_applications": ["qwen38", "comfy"],
  "active_applications": ["qwen38"],
  "cpu": { "usage_pct": 34, "cores": 16, "ram_total_mb": 65536, "ram_used_mb": 28000 },
  "uptime_sec": 86400
}
```

`/api/gpu` and `/api/status` (→ `resources`) expose the same view. Identity and
extra telemetry fields (`name`, `pcie`, `clock_mhz`, `rx_mbps`, `tx_mbps`,
`power_limit_w`) are backend-provided and may be `null` where the backend cannot
supply them (spec 07 §2.2); system fields come from `/proc` on Linux.

## 4. Gateway Proxy API

The actual application API remains unchanged. `localhost:913000` externally maps to
`localhost:3000`. The proxy acts as:

```text
Reverse Proxy + Request Queue + Scheduler Client
```

Client-facing behaviour:

- Ready application → request piped through immediately (streaming preserved).
- Not ready → request queued; the client receives the application's normal response
  once processed (queued wait appears as latency, not as an error).
- Queue full → `429` (or configured `503`).
- Upstream failure → application's error response, or `502`/`503` if the upstream
  is gone (per retry policy, see [14 §4](14-invariants.md#4-retry-policy)).

## 5. Example Configurations

### 5.1 Global Configuration

```yaml
gpu:
  memory_reserve_mb: 2048
  memory_safety_margin: 0.10

scheduler:
  interval_ms: 1000
  min_residency_sec: 30
  switch_margin: 5
  switch_cost_weight: 1.0
  emergency_wait_sec: 300

metrics:
  interval_sec: 1

queue:
  default_max_waiting_requests: 20
  default_max_waiting_time_sec: 300
```

### 5.2 Example Applications

```yaml
applications:

  - id: qwen38
    name: Qwen 3.8
    type: llama

    start_script: /opt/apps/qwen38/start.sh
    stop_script: /opt/apps/qwen38/stop.sh

    load_script:
    unload_script:

    port: 3000

    gpu_memory_mb: 18500
    gpu_usage_percent: 95

    allow_automatic_unload: true

  - id: qwen35
    name: Qwen 3.5
    type: llama

    start_script: /opt/apps/qwen35/start.sh
    stop_script: /opt/apps/qwen35/stop.sh

    port: 3001

    gpu_memory_mb: 14000
    gpu_usage_percent: 95

    allow_automatic_unload: true

  - id: comfy
    name: ComfyUI
    type: comfyui

    start_script: /opt/apps/comfy/start.sh
    stop_script: /opt/apps/comfy/stop.sh

    load_script: /opt/apps/comfy/load.sh
    unload_script: /opt/apps/comfy/unload.sh

    port: 8188

    gpu_memory_mb: 8000
    gpu_usage_percent: 90

    activity_detection_method: gpu
    idle_timeout_sec: 600

    allow_automatic_unload: true
```

### 5.3 Example Gateways

```yaml
gateways:

  - name: Qwen 3.8 API
    port: 913000
    application: qwen38
    priority: 10
    max_waiting_queue: 10
    max_waiting_time: 120
    enabled: true

  - name: Qwen 3.5 API
    port: 913001
    application: qwen35
    priority: 8
    max_waiting_queue: 10
    max_waiting_time: 120
    enabled: true

  - name: TTS API
    port: 913003
    application: breeze
    priority: 7
    max_waiting_queue: 20
    max_waiting_time: 60
    enabled: true
```

## 6. Global Configuration (All Keys)

| Key | Default | Meaning |
| --- | --- | --- |
| `data_path` | `~/.gpusybio` (or derived from `db_path`) | global data path: app log at `<data_path>/logs/<id>.log` (the app's stdout), pid at `<data_path>/pids/<id>.pid`; scripts see `GPUSYB_DATA_PATH` / `GPUSYB_LOG` / `GPUSYB_PID_FILE` (03 §2.5) |
| `gpu.memory_reserve_mb` | 2048 | VRAM never planned for apps |
| `gpu.memory_safety_margin` | 0.10 | required = effective × (1 + margin) |
| `scheduler.interval_ms` | 1000 | reconciliation loop period |
| `scheduler.min_residency_sec` | 30 | no switch within this of load |
| `scheduler.switch_margin` | 5 | candidate must beat current by this |
| `scheduler.switch_cost_weight` | 1.0 | weight on switching penalty |
| `scheduler.emergency_wait_sec` | 300 | overrides min residency |
| `scheduler.emergency_queue_threshold` | (config) | overrides min residency |
| `scheduler.switch_cooldown_sec` | 0 (off) | anti-thrash pair cooldown |
| `metrics.interval_sec` | 1 | sampling period |
| `queue.default_max_waiting_requests` | 20 | gateway default |
| `queue.default_max_waiting_time_sec` | 300 | gateway default |
| `requests.default_request_timeout_sec` | 600 | received→completed cap |
| `load.max_retries` | 2 | load retries on failure |
| `load.retry_delay_sec` | 10 | delay between retries |
| `load.max_consecutive_failures` | 3 | then FAULTED |
| `startup.max_detection_sec` | 60 | ready-detection cap |

### 6.1 Seed Control (spec 16 §2.6)

Per-application and per-gateway seed flags — not global settings:

| Key | Default | Meaning |
| --- | --- | --- |
| `applications[].update` | `false` | update the existing DB row at startup; if `false` the seed creates missing rows only, so `PUT /api/*` changes survive a restart |
| `gateways[].update` | `false` | same, for gateways |
