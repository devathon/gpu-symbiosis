# Module 06 — Scheduler

The scheduler is the decision engine. It is a **resource-residency scheduler**, not an
application switcher (see [02 §3](02-architecture.md#3-key-architectural-decision)):
it decides which request is served next, which applications must be resident to serve
it, and which residents can be removed to make room.

## 1. Critical Unloading Rule

> **An application must never be unloaded while any request associated with that
> application is actively processing.**

```text
A1.active_requests = 2   →   A1 cannot unload   →   until active_requests = 0
```

This applies regardless of which gateway generated the request. If
`G1 → A1`, `G2 → A1` and `G1 = PROCESSING`, `G2 = WAITING`, A1 cannot unload
because `G1` is actively processing. General rule:

```text
application.active_request_count > 0   →   application.unload_allowed = false
```

## 2. Draining State

When switching from A1 to A2:

```text
A1 = LOADED
A2 = WAITING
```

Scheduler decides to switch:

```text
A1 → DRAINING
```

During draining:

```text
new A1 requests    → queue
existing A1 requests → continue
```

Once:

```text
A1.active_requests == 0
```

then:

```text
A1 → UNLOADING
A1 → OFF
A2 → LOADING
A2 → LOADED
```

This prevents new work from entering A1 while it is being removed.

## 3. Unload Candidate Selection

If the candidate does not fit alongside current residents, the scheduler selects
resident applications to remove. Prefer applications that:

1. Are idle
2. Allow automatic unloading (`allow_automatic_unload = true`)
3. Have no active requests
4. Have low demand
5. Have low priority
6. Have been resident longest/shortest (policy)
7. Have low switching value
8. Have high unload cost — only if necessary

An **unload score** can rank candidates:

```text
unload_score =
      idle_pressure
    + low_demand_pressure
    + low_priority_pressure
    + residency_pressure
    - unload_cost
```

The application with the **lowest retention value** (i.e. highest unload score) is
unloaded first.

### 3.1 Hard Exclusions (never unload)

```text
active_requests > 0                          → never
allow_automatic_unload = false               → never (automatic path)
pinned = true                                → never (automatic path)
```

Manual unload/stop may override automatic-unload protection (never active-request
protection unless forced). See [14](14-invariants.md).

## 4. Triggers

The scheduler operates periodically and reacts to events:

```text
new request
request completion
application load completion
application unload completion
queue timeout
resource change (VRAM drop/rise)
idle timeout
manual Process Now
```

Model: **event-driven scheduling + 1-second reconciliation loop**. Events wake the
scheduler immediately; the periodic loop catches stale states, GPU changes, dead
processes, and missed events.

## 5. Scheduler Inputs

```text
gateway priority
queue length
oldest request age
recent request rate
service rate
active application(s)
application memory requirement
available VRAM
startup time
load time
unload time
application residency
application activity
switch cost
```

## 6. Gateway Score

```text
gateway_score =
      priority_pressure
    + waiting_pressure
    + recent_demand_pressure
```

### 6.1 Backlog Pressure

```text
queue_pressure =
    queue_length / gateway.max_waiting_queue

age_pressure =
    oldest_wait_time / gateway.max_waiting_time

backlog_pressure =
    max(queue_pressure, age_pressure)
```

`backlog_pressure` is **allowed to exceed 1** — an overdue request should become
increasingly urgent (no clamping).

### 6.2 Priority Pressure

```text
priority_pressure = gateway.priority × backlog_pressure
```

### 6.3 Request Arrival Rate (EWMA)

```text
recent_arrival_rate = EWMA(request_arrival_rate)      # requests/minute
```

EWMA prevents a single burst from permanently dominating scheduling.

### 6.4 Service Rate (EWMA)

```text
service_rate = EWMA(request_service_rate)             # e.g. A1: 4 requests/min
```

### 6.5 Demand Ratio

```text
demand_ratio = recent_arrival_rate / max(service_rate, ε)
```

where `ε` is a small minimum value preventing division by zero.

### 6.6 Normalized Demand

```text
normalized_demand = demand_ratio / (1 + demand_ratio)
```

```text
demand ratio 0    → 0
demand ratio 1    → 0.5
demand ratio 2    → 0.67
demand ratio 5    → 0.83
demand ratio 10   → 0.91
```

### 6.7 Recent Demand Pressure

```text
recent_demand_pressure = gateway.priority × normalized_demand
```

### 6.8 Waiting Pressure

Explicit definition (avoids double-counting priority):

```text
waiting_pressure = oldest_wait_time / gateway.max_waiting_time
```

### 6.9 Final Formulation

```text
gateway_score =
      priority_pressure
    + waiting_pressure
    + recent_demand_pressure
```

where:

```text
backlog_pressure =
    max(
        queue_length / gateway.max_waiting_queue,
        oldest_wait_time / gateway.max_waiting_time
    )

priority_pressure = gateway.priority × backlog_pressure

waiting_pressure = oldest_wait_time / gateway.max_waiting_time

recent_arrival_rate = EWMA(request_arrival_rate)

service_rate = EWMA(request_service_rate)

demand_ratio = recent_arrival_rate / max(service_rate, ε)

normalized_demand = demand_ratio / (1 + demand_ratio)

recent_demand_pressure = gateway.priority × normalized_demand
```

### 6.10 Worked Example

```text
Gateway:  priority = 10, queue = 3/10, oldest wait = 18 s / 60 s max
backlog_pressure       = max(0.30, 0.30) = 0.30
priority_pressure      = 10 × 0.30       = 3.0
waiting_pressure       = 18/60          = 0.30
arrival 8/min, service 4/min → demand_ratio 2 → normalized 0.67
recent_demand_pressure = 10 × 0.67      = 6.7
gateway_score          = 3.0 + 0.3 + 6.7 = 10.0
```

## 7. Coexistence & Application Fit

The scheduler distinguishes **active application** from **resident application**:

```text
A3 = resident, A4 = resident
A3 currently processing, A4 idle
→ a request to A4 does not require loading it
```

Example (24 GB): `A3 = 6 GB`, `A4 = 5 GB` → both potentially active
(`A3 + A4 = 11 GB`), while `A1 = 18 GB` cannot coexist with either if insufficient
VRAM remains.

Before loading an application:

```text
required  = application.effective_gpu_memory × (1 + memory_safety_margin)
available = GPU_total
          - GPU_used_by_other_managed_apps
          - GPU_system_reserve

application fits   ⇔   required <= available
```

**Resident-candidate special case.** If the candidate application is *already*
resident, nothing needs to be loaded:

```text
required_additional_vram = 0
→ the fit check trivially passes
→ the scheduler takes the "activate without switching" path
```

The candidate's own existing residency must not be counted against itself when
computing `available`. This is the **zero-cost path**: no drain, no unload, no
load — even while the same application is actively serving other gateways.

## 8. GPU Reserve

```text
usable_vram = total_vram - gpu_memory_reserve
```

Example: 24 GB total, 2 GB reserve → 22 GB usable. The scheduler never plans
beyond `usable_vram` — this prevents borderline OOM conditions.

## 9. Process Now

`Process Now` must **not** violate request safety. It does **not** mean "immediately
kill the currently active application". It means:

> **Increase the scheduling urgency of this gateway.**

```text
Process Now
     ↓
Gateway gets temporary scheduling boost
     ↓
Current application continues active requests
     ↓
Current application enters draining state
     ↓
Unload
     ↓
Target application loads
     ↓
Request processed
```

Implementation: a time-decaying boost (e.g. priority multiplier that expires after
`boost_ttl`) applied to that gateway's score until the gateway's queue drains.

## 10. Non-API Applications & Idle Unloading

Applications such as ComfyUI may not expose requests through the load balancer. For
them, track activity through:

```text
GPU utilization
GPU memory utilization
process CPU usage
application-specific signals
```

Example — ComfyUI GPU `80% → 75% → 82% → 79%` means it is active. If
`GPU < 5% for 10 minutes`, the application can be considered idle.

Per non-API application:

```text
activity_detection_method
idle_threshold_gpu    e.g. 5%
idle_timeout          e.g. 300 seconds
```

```text
GPU < idle_threshold_gpu for idle_timeout   →   application_idle = true
```

Idle non-API apps become valid unload candidates (subject to `allow_automatic_unload`
and no activity). Example:

```text
ComfyUI loaded, GPU = 0%, idle = 10 minutes
A1 request arrives
→ ComfyUI idle, unloadable, A1 needs its VRAM
→ ComfyUI → UNLOADING, A1 → LOADING
```

### 10.1 The `active` signal (single definition)

The single "is the app doing work" signal is computed once and consumed by the
display, the scheduler, and the idle-unloader (spec 04 §3):

```text
active = serving a request (active_requests > 0)
       OR using the GPU (utilization >= idle_threshold_gpu)
```

If **either** is on, the app is active. The scheduler's eviction guard uses this
so a **RUNNING** no-gateway app (high GPU utilization, no request rows) is never
evicted mid-work — an app that is idle (both signals off) is the only one that
may be unloaded.

### 10.2 Memory-based residency monitor

Some applications (e.g. ComfyUI) load and free their model *outside*
GPUSymbiosis. A background monitor watches each app's per-process GPU memory and
reconciles the residency state (spec 04 §3 — LOADED is based on VRAM):

```text
OFF     and VRAM >= loaded_gpu_memory_mb (per-app threshold, MB)  → LOADED
LOADED  and VRAM <  loaded_gpu_memory_mb                          → UNLOADED
```

`loaded_gpu_memory_mb` is a per-app absolute MB level (default 0 = auto-detection
disabled). A single threshold has no hysteresis band, so the flip debounce
(`min_flip_sec`, default 10s) is the only flapping protection. Any app with a
residency state is tracked — including unload-only apps (no load script), which
load automatically from usage. The monitor publishes the state
change (UNLOADED / LOAD_READY) so the scheduler and UI react.

## 11. Switching Cost

```text
estimated_switch_cost =
      unload_cost
    + startup_cost
    + load_cost
```

For each application being removed:

```text
unload_cost = application.unload_time
```

For the candidate:

```text
load_cost = candidate.load_time

startup_cost = candidate.startup_time    (only if process startup is required)
```

Therefore:

```text
estimated_switch_cost =
    Σ unload_time(active_apps_to_remove)
    + candidate.startup_time
    + candidate.load_time
```

```text
switching_penalty = (estimated_switch_cost / GATEWAY.max_waiting_time_sec)
                    × SWITCH_COST_WEIGHT
```

Costs are measured in seconds, then normalized by the candidate gateway's time
horizon (`max_waiting_time_sec`) — the same scale the score's waiting term
already uses (`oldest_wait / max_wait`, §6.9) — so the penalty lands on the
score's scale. Subtracting raw seconds from a normalized score would inflate
the penalty by roughly `max_wait`× (a 6 s startup against a ~1 score), making
it impossible to ever switch to a stopped application.

**Zero-cost case.** If the candidate application is already resident, no
application is removed and nothing is started or loaded, therefore:

```text
estimated_switch_cost = 0    →    switching_penalty = 0
```

A same-application gateway activation is never penalized, never blocked by
`MIN_RESIDENCY`, and never routed through draining.

## 12. Residency Protection

```text
MIN_RESIDENCY    e.g. 30 seconds
```

If:

```text
current_time - active_application.loaded_at < MIN_RESIDENCY
```

do **not** switch unless an emergency condition occurs.

## 13. Switch Margin

```text
SWITCH_MARGIN    e.g. 5
```

Switch only when the candidate is significantly better.

## 14. Final Switching Algorithm

```text
candidate_score = gateway_score(candidate)

switching_penalty = (estimated_switch_cost(
                        active_applications_to_unload,
                        candidate
                    ) / candidate.max_waiting_time_sec)
                    × SWITCH_COST_WEIGHT          # §11: normalized to the score's scale

effective_candidate_score = candidate_score - switching_penalty

if candidate == active_gateway:
    keep active_gateway

else if candidate_application is already resident:
    activate candidate without switching
    # zero-cost path: required_additional_vram = 0,
    # estimated_switch_cost = 0. Never drain, unload, or reload the
    # same application — even if it is actively serving other gateways.

else if candidate can run concurrently alongside residents:
    load candidate; activate without switching

else if active_gateway_residency < MIN_RESIDENCY:
    keep active_gateway

else if active_gateway is draining/unloading:
    wait

else if active_application has active requests:
    mark active application as DRAINING
    queue new requests

else if effective_candidate_score >
        gateway_score(active_gateway) + SWITCH_MARGIN:
    switch to candidate

else:
    keep active_gateway
```

### 14.1 Worked Example — Two Gateways, One Application

Setup: `gw1 → A1`, `gw2 → A1` (different priorities), A1 resident and actively
serving a gw1 request, `max_concurrent_requests(A1) = 1`.

1. A request arrives at gw2. A1 is ready, but no concurrency slot is free, so
   the request is **queued at gateway level** — it is *not* forwarded
   immediately, regardless of gw2's priority. Priority never bypasses the
   queue; it only affects scheduling order when a slot frees up
   (see [05 §7.1](05-gateway-module.md#71-proxy-behaviour)).
2. Scheduler evaluation: `candidate = gw2`, `candidate_application = A1`.
   A1 is already resident:

   ```text
   required_additional_vram = 0
   estimated_switch_cost    = 0
   switching_penalty        = 0
   ```

3. Decision: **activate gw2 without switching** — no drain, no unload, no
   reload of A1.
4. When gw1's request completes, the slot frees. The scheduler forwards the
   highest-scoring queued request (gw1 vs gw2 by gateway score — the
   higher-priority gateway wins on equal demand).

Net effect: zero application-level context switching. Waiting happens only in
the gateway queue — exactly like the "application not ready" case; the only
difference is that switching cost is 0.

If A1 is **idle** (resident, no active requests) when the gw2 request arrives,
the slot is free and the request is forwarded immediately — there is no
context switch to avoid.

## 15. Emergency Switching

One exception to normal switching. If:

```text
candidate.waiting_time >= EMERGENCY_WAIT_TIME
```

or:

```text
candidate queue >= EMERGENCY_QUEUE_THRESHOLD
```

the scheduler may override `MIN_RESIDENCY`, but **still** must obey:

```text
active requests cannot be terminated
```

(i.e. drain first; the emergency path skips the residency guard, never the drain).

## 16. Thrashing Prevention

Explicit protection against:

```text
A1 → A2 → A1 → A2 → A1
```

Controls:

```text
MIN_RESIDENCY
SWITCH_MARGIN
SWITCH_COST_WEIGHT
```

Additionally tracked:

```text
switch_count
last_switch_time
previous_application
```

Optional `SWITCH_COOLDOWN`: minimum time between two switches involving the same
application pair.

## 17. Graceful Switch Sequence (Complete)

```text
Candidate selected
       ↓
Queue new requests for current app (§2: new requests queue,
existing continue — the drain is never cancelled or delayed)
       ↓
Current app → DRAINING
       ↓
Wait for active requests = 0
       ↓
Unload current app(s)
       ↓
Verify VRAM released
       ↓
Load candidate
       ↓
Verify readiness
       ↓
Candidate → ACTIVE
       ↓
Drain candidate queue
```

## 18. Scheduler State Machine

```text
                    ┌──────────────┐
                    │     IDLE     │
                    └──────┬───────┘
                           │ request
                           ▼
                    ┌──────────────┐
                    │   EVALUATE   │
                    └──────┬───────┘
                           │
                 ┌─────────┴─────────┐
                 │                   │
              fits                 doesn't fit
                 │                   │
                 ▼                   ▼
             LOAD/USE          SELECT UNLOAD
                 │                   │
                 │                   ▼
                 │               DRAINING
                 │                   │
                 │              active=0
                 │                   │
                 │                   ▼
                 │               UNLOADING
                 │                   │
                 │                   ▼
                 │               LOADING
                 │                   │
                 │                   ▼
                 │                READY
                 │                   │
                 │                   ▼
                 │             PROCESSING
                 │                   │
                 │                   ▼
                 └────────────────→  IDLE
```

## 19. Explainability

Every scheduler decision emits a structured log entry and is surfaced in the UI
"Current Decision" panel (see [11 §5](11-web-ui.md#5-scheduler-explainer)):

```json
{
  "event": "scheduler_decision",
  "candidate": "qwen35",
  "candidate_score": 42.2,
  "switching_penalty": 8.1,
  "effective_score": 34.1,
  "current_score": 24.7,
  "decision": "switch"
}
```

Decision values: `keep`, `activate_concurrent`, `switch`, `drain`, `wait`,
`emergency_switch`, `no_candidate`.
