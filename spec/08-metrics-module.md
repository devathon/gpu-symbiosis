# Module 08 — Metrics & History

The Metrics module samples applications and resources continuously, maintains rolling
demand history for the scheduler, and persists event and metric history.

## 1. Continuous Sampling

Default interval: **1 second** (configurable):

```text
Every 1 second:

GPU utilization
GPU memory
CPU utilization
CPU memory
process state
```

Application-specific metrics can have their own collectors (type handlers, see
[04 §8](04-application-manager.md#8-type-handler-plugins)).

## 2. Request History

Maintain rolling history for at least:

```text
5 minutes
15 minutes
1 hour
```

The scheduler primarily uses the recent EWMA. Tracked per gateway and per
application:

```text
requests_received
requests_completed
requests_failed
average_wait
max_wait
average_service_time
arrival_rate
service_rate
```

## 3. Historical Demand (EWMA)

For each gateway:

```text
EWMA arrival rate
EWMA service rate
EWMA request duration
EWMA queue length
```

Optionally maintain:

```text
request_count_last_1m
request_count_last_5m
request_count_last_15m
```

This distinguishes a **one-off request** from a **continuing workload** — the
basis of the scheduler's demand pressure ([06 §6.3–6.7](06-scheduler.md#6-gateway-score)).

## 4. Common Application Metrics

```text
total requests
successful requests
failed requests
active requests
queued requests
average latency
P50 latency
P95 latency
P99 latency
```

Latency = `started_at - queued_at` (queue wait) and
`completed_at - started_at` (service time) tracked separately.

## 5. Llama Metrics

For `llama` applications:

```text
input tokens
output tokens

cache hit %         (% of requests served from the KV cache — LCP slot selection)
prompt t/s          (Σ prompt tokens / Σ prompt time — throughput-weighted)
generation t/s      (Σ eval tokens / Σ eval time — throughput-weighted)

draft acceptance    (Σ accepted / Σ generated, speculative decoding)

total requests
failed requests
active requests     (active + queued; "1 + 4 queued" when queued)
```

Values are rendered as bare numbers (no unit suffix) with `k`/`m`/`b` suffixes
(`k` = 1e3, `m` = 1e6, `b` = 1e9; under 1e3 shown as-is). The latency
percentiles are condensed into a single `Wait (P50/95/99)` / `Service (P50/95/99)`
element. **Total tokens** and the overall **tokens t/s** are dropped from the
card (not useful — the per-phase `prompt t/s` / `generation t/s` already convey
throughput); both are still computed and exposed in `raw`.

**Source.** A real `llama-server` exposes no cumulative token totals over HTTP
(`/metrics` is per-request Prometheus gauges, `/state` is model state only), but
it logs each request. The application's stdout **is** the log — the start script
redirects it to `<data_path>/logs/<id>.log` (the global data path, §2.5 of
[03-application-model.md](03-application-model.md)) — and the metrics handler
tails it, parsing the per-request lines (`llama_perf_context_print`):

```text
prompt eval time = 1234.56 ms / 100 tokens ( 12.35 ms per token, 81.00 tokens per second)
       eval time = 5678.90 ms / 200 runs   ( 28.39 ms per token, 35.22 tokens per second)
      total time = 6913.46 ms / 300 tokens
draft acceptance = 0.40944 (  642 accepted /  1568 generated), mean len =  3.87
selected slot by LCP similarity, f_sim_best = 0.996 (> 0.100 thold), f_keep = 0.997
```

**Draft acceptance** is the speculative-decoding ratio (Σ accepted / Σ
generated). **Cache hit** is the fraction of requests whose slot was selected by
LCP (prefix) similarity — i.e. the prompt was largely served from the KV cache —
over all slot selections (an LRU selection is a miss).

**Persistence (§5.1).** The tailer's lifetime totals survive restarts: they are
persisted in the `app_metric_lifetime` table together with the log byte-offset
and a session fingerprint (the log's first 64 bytes). On server start the tailer
is seeded from this state and resumes *after* the stored offset (no double-count).
The log is truncated on every app start (`>log 2>&1`), so a changed fingerprint
means a fresh session: the lifetime is **kept** and the new content is read from
the start. The state is written on a 60 s timer for active (running) apps and
again on app stop.

The persisted fields are the type's *registered* fields (§9) — application-specific
and type-dynamic (spec 04 §8), so they are stored as a **JSON document**
(`lifetime` column), NOT as columns: adding, removing, or renaming a type's
fields never requires a schema change, and a type's tailer persists whatever its
`state()` returns. Only the tailer's *mechanism* state (log offset + session
fingerprint) lives in its own columns. A corrupt document is treated as empty
(fresh lifetime, logged). There are no automatic migrations.

The prompt line's count is the input tokens and its `ms` the prompt phase time;
the eval line's count (runs) is the output tokens and its `ms` the eval phase
time. The rates are throughput-weighted (Σ tokens ÷ Σ time) so a large prompt
weighs by its work, not as one vote per request — the same semantics as the
overall `tokens t/s`. The total line gives the overall throughput. The tailer
tracks a read offset
(rotation-safe) and accumulates lifetime totals, which persist across restarts
(§5.1). Apps that do expose a `/stats` endpoint (e.g. the mock) still work: the
first non-zero source wins (log, then `/stats`, then the request-tracked sums). The gateway also records per-request token counts from
the upstream response headers (`X-Input-Tokens` / `X-Output-Tokens`, §9.2).

Optionally:

```text
context size
KV cache usage
prompt processing time
generation time
```

These feed the gateway's EWMA service rate and the UI.

## 6. Storage & Retention

Do **not** store every one-second sample forever in the primary database. Use
short-term high-resolution metrics plus long-term aggregates:

```text
raw metrics            → retain 24 hours
1-minute aggregates    → retain 30 days
hourly aggregates      → retain indefinitely
```

Aggregation is done by a metrics aggregator (min/max/avg/sum per window); the
aggregator also produces the "current / average / maximum" triplets for the UI.

## 7. Application Active Time

```text
total_active_time
```

where:

```text
active_time = time application is LOADED
```

Optionally tracked separately:

```text
loaded_time
processing_time
idle_loaded_time
```

This is useful for understanding VRAM utilization (how long a model sat in VRAM vs.
how long it actually served).

## 8. Application History (Event Log)

Tracked events:

```text
START
STOP
LOAD
UNLOAD
ERROR
REQUEST_START
REQUEST_END
SWITCH_IN
SWITCH_OUT
```

Example:

```text
18:01:22  A1 LOAD
18:01:25  A1 READY
18:02:11  A1 REQUEST
18:03:02  A1 REQUEST COMPLETE
18:03:20  A1 UNLOAD
18:03:28  A1 OFF
18:03:29  A2 LOAD
```

Events are the audit trail: every scheduler decision, lifecycle transition, request
lifecycle change, and error is one event row (see [09 §5](09-data-model.md#5-event-tables)
and [13](13-observability.md)).

## 9. Type Metrics Contract (Extensible Application Types)

Application types are extensible without changing the scheduler, the recording
pipeline, or the UI. A type is a registered **TypePlugin** — spec *plus* the
handler that collects it; everything downstream consumes the spec's *declarative
metric definition*, never the collection logic.

### 9.1 Type Registry

```text
TypePlugin
──────────────────────
type                 type name
spec                 TypeSpec          ← what to record and display
handler              TypeHandler class ← how to fetch (unified factory signature)
```

```text
TypeSpec
──────────────────────
type                 type name
metrics              list[MetricDef]     ← what to record and display
should_schedule()    (method, path) -> bool  ← routing (spec 05 §6)
```

`should_schedule` (default `True`) declares which requests go through the
scheduler vs pass through directly (spec 05 §6). `comfyui` overrides it: only
`POST /api/prompt` is scheduled; reads / static / the websocket pass through
blindly. The app view also exposes `gpu_utilization_pct` alongside
`gpu_memory_used_mb` (spec 04 §3 — RUNNING is based on GPU utilization).

The registry holds no type-specific code: it resolves type → (spec, handler
factory) from whatever registered itself. The shipped types (`llama`,
`ollama`, `comfyui`, `tts`) are modules under `gpusybio/application_types/`
that load with the same contract as drop-ins (§9.6); the **generic** fallback
is registered first by core (always available, can't be shadowed). Any
unknown type falls back to it. The `custom` type carries a user-declared spec
from config (§9.6). A missing or failing handler degrades to generic
metrics; it never fails the application (see
[04 §8.1](04-application-manager.md#81-generic-handler)).

A type's own lifetime fields persist as a JSON document (§5.1) — the type's
fields are dynamic by design, the schema is not.

### 9.2 MetricDef (the declarative contract)

```text
MetricDef
──────────────────────
id            stable key, e.g. "generation_tps"
name          display name
unit          "tokens", "tokens/sec", "%", "ms", "req", ...
kind          gauge | counter | rate
source        handler | proxy | derived
display       card | chart            (UI hint)
group         display group           (UI section, default "metrics")
```

- `source=handler`  — value comes from `TypeHandler.collect()` (§9.3).
- `source=proxy`    — value comes from what our gateway already observes
  (request lifecycle: queue wait, service time, status, stream size). Type-agnostic.
- `source=derived`  — value is computed by the Metrics module from raw records
  (EWMA rates, percentiles, tokens/sec, §2–3).

The UI renders one panel per application, auto-generated from the `metrics`
list: `card` metrics as stat tiles, `chart` metrics as time series. The card
renders its fields **grouped by `group`, in first-appearance order** — the
type's fields dictate the card layout (spec 04 §8). **Adding a type never
requires UI code** — the panel appears from the definition alone.

### 9.3 TypeHandler (collection logic)

The handler is the type's own recording logic and implements the interface from
[04 §8](04-application-manager.md#8-type-handler-plugins):

```text
collect(app) -> {metric_id: value}
       per sample tick (1 s), only while the app is RUNNING and LOADED;
       for `llama`/`ollama`, tails the app's log (<data_path>/logs/<id>.log, the
       app's stdout) for the per-request token lines (§5) — the primary source —
       and falls back to polling the app's own JSON endpoint on its port
       (llama-server /metrics or /state, Ollama /api/ps, ComfyUI queue stats)
       and to the request-tracked sums, mapping response fields to metric ids.

on_request_complete(request) -> {metric_id: value}
       per-request metrics (e.g. input/output tokens, prompt/generation time)
       extracted from the request record or the app's response.
```

Collection rules:

- A `collect()` failure (endpoint missing, timeout, schema drift) is logged and
  the metric is marked **stale**; it never interrupts the app or the sampler.
- Consecutive failures are counted per metric (exposed, like load failures).
- `on_request_complete()` may be absent: request metrics then fall back to the
  proxy-observed and derived sources only.

### 9.4 Recording

```text
sample tick (1 s)
 ├─ generic metrics (memory, requests, queue)      — all types
 ├─ handler.collect(app) → {metric_id: value}
 └─ request completed → handler.on_request_complete(req)
        │
        ▼
per-tick values   → rolling history (in-memory) + app_metric_samples (SQLite)
                     (app_id, metric_id, value, at) — retention per §6
per-request values → request.metrics (JSON column on the request row)
```

`app_metric_samples` rows are keyed by metric id, so arbitrary type metrics share
one table; the MetricDef list is the schema reference (metric ids are not
free-form in storage — they must exist in the type's spec, or in the `custom`
spec for that application).

### 9.5 Reporting

```text
GET /api/applications/{id}/metrics
→ {
    "type":      type name,
    "spec":      [ MetricDef... ],   ← what this type records (incl. group)
    "metrics":   [ { id, name, unit, kind, display, group, value } ],
                 ← the card view: one entry per field, in spec order
    "state":     { residency_state, process_state, active_requests, ... },
    "raw":       { metric_id: value },
    "active_time": { ... }
  }
```

The `metrics` list **is** the card: the UI renders it directly, grouped by
`group` in first-appearance order (spec 04 §8), so a type's fields dictate the
card layout without UI code. Requests dedupe lives in this view: a type's own
`requests` field (log-derived) wins over the gateway-tracked
`requests_received`, which then never appears twice.

Time series for charts come from the metrics-samples endpoints (rolling
history for short windows, aggregates (§6) for longer ones). The same values
feed the scheduler's EWMA inputs (§2–3) and the event log (§8) — one
pipeline, three consumers.

### 9.6 Extensibility

- **Shipped type modules** — one module per type under
  `gpusybio/application_types/` (llama, ollama, comfyui, tts per
  [04 §8](04-application-manager.md#8-type-handler-plugins)), each holding all
  of its own code (spec + handler + tailer) and registering itself with the
  same contract as a drop-in; loaded first, so they win name collisions. The
  registry itself holds no type-specific code.
- **`custom` type** — the application's config declares its metrics:

  ```yaml
  type: custom
  metrics:
    - { id: queue_depth, name: Queue depth, unit: req, kind: gauge,
        source: handler, display: card,
        collect: { url: /stats, json_path: queue.depth } }
  ```

  The generic handler interprets the `collect` mappings — no code required.
- **Drop-in plugins** — a `.py` module in `<data_path>/plugins/` registers a
  type at server start, no config change, no restart of the process model
  (subject to the script/handler sandboxing rules of [12](12-security.md)):

  ```python
  # <data_path>/plugins/widgets.py
  def register(registry):
      registry.register(TypePlugin("widgets", TypeSpec("widgets", [...]), WidgetsHandler))
  # — or expose PLUGIN = TypePlugin(...) instead of register().
  ```

  First-registered names win collisions — the generic fallback (registered
  first by core) and the shipped type modules (loaded before drop-ins) cannot
  be shadowed; a failing module is skipped with a logged error and never stops
  startup. The UI's type list comes from `GET /api/application-types`, so a
  new type appears in the app form automatically.

## 10. Memory & Timing Learning Hooks

Every sample and transition updates (see [03 §5–6](03-application-model.md#5-resource-requirements)):

```text
gpu_memory_observed_max  = max(previous, current)
gpu_usage_observed_max   = max(previous, current)
startup/load/unload/stop_time   ← measured on each occurrence
```
