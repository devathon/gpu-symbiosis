# Module 13 — Observability

## 1. Structured Logs

JSON-structured logs (stdout + rotating files) covering:

```text
scheduler decisions
application lifecycle events
request events
resource metrics
```

Log levels: `INFO` (state transitions, decisions), `WARN` (timeouts, retries,
near-OOM headroom), `ERROR` (failures, invariants nearly violated), `DEBUG`
(scoring breakdowns).

Example:

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

Every log line carries: timestamp, module, correlation ids where applicable
(`application_id`, `gateway_id`, `request_id`, `transition_id`).

## 2. Scheduler Decision Log

Every scheduler evaluation that produces a decision (not merely a `keep` no-op
below the margin) emits a `scheduler_decision` event with the **full scoring
breakdown** (see [06 §19](06-scheduler.md#19-explainability)). This is what powers
the UI's Scheduler Explainer and post-hoc analysis ("why did it switch at 18:04?").

## 3. Prometheus-Compatible Metrics

Optional but highly useful; exposed at `/api/metrics/prometheus` when enabled.

```text
gpu_load_balancer_gpu_memory_bytes{state="total|used|free|reserved|external"}
gpu_load_balancer_gpu_utilization
gpu_load_balancer_application_memory_bytes{application="qwen38"}
gpu_load_balancer_application_gpu_utilization{application="qwen38"}

gpu_load_balancer_gateway_queue_length{gateway="gw-qwen35"}
gpu_load_balancer_gateway_wait_seconds{gateway="gw-qwen35"}

gpu_load_balancer_request_total{gateway="gw-qwen35",status="completed|failed|cancelled|expired"}
gpu_load_balancer_request_duration_seconds{gateway="gw-qwen35",phase="wait|service"}

gpu_load_balancer_application_load_seconds{application="qwen35"}
gpu_load_balancer_application_unload_seconds{application="qwen35"}
```

Additional useful series:

```text
gpu_load_balancer_switch_total{from_app="...",to_app="..."}
gpu_load_balancer_scheduler_decisions_total{decision="..."}
gpu_load_balancer_application_state{application="...",state="..."}  (0/1 gauge)
gpu_load_balancer_gpu_temperature_celsius
gpu_load_balancer_gpu_power_watts
```
