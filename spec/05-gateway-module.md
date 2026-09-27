# Module 05 — Gateway & Proxy

A gateway is an externally accessible endpoint associated with an application.
Multiple gateways may point to the same application. The gateway module owns
request intake, queuing, proxying, and the request lifecycle.

## 1. Gateway Fields

```text
id                       string, unique
name                     string
port                     int, external gateway port (e.g. 913000)
application_id           string, target application

priority                 int, scheduling priority (see §3)

max_waiting_queue        int, max queued requests
max_waiting_time         seconds, max time a request may wait

enabled                  bool
request_timeout          seconds, max time from received to completed

max_concurrent_requests  int, forwarded concurrently (default 1)
```

Example:

```text
Gateway G1 → A1
Gateway G2 → A1
Gateway G3 → A2
```

## 2. Gateway Priority

Configurable numeric value; higher number = higher priority:

```text
1    low
5    normal
10   high
20   critical
```

Gateway priority represents **request importance**. It is distinct from any
future application retention priority (see [03 §11](03-application-model.md#11-priority-separation)).

## 3. Gateway Queue

Each gateway owns its own queue:

```text
G1
 ├── Request 101
 ├── Request 104
 └── Request 109

G2
 ├── Request 102
 └── Request 110
```

The scheduler selects **between** gateway queues; within a queue, ordering is:

```text
effective_request_priority
      ↓ (tie)
FIFO by queued_at
```

A queue item is therefore ordered by `(effective_request_priority, queued_at)`.

## 4. Queue Limits

- If `queue_length >= max_waiting_queue`, new requests receive:

  ```text
  HTTP 429
  ```

  or a configurable response, e.g. `503 Service Unavailable`.

- Every request records `queued_at`. Then:

  ```text
  waiting_time = now - queued_at
  ```

  If `waiting_time > gateway.max_waiting_time`, the request is handled per
  configured policy:

  ```text
  REJECT             return error to client
  EXPIRE             mark EXPIRED, drop from queue
  ESCALATE_PRIORITY  raise effective priority, keep waiting   ← recommended default
  PROCESS_ANYWAY     treat as top-of-queue candidate
  ```

## 5. Gateway States

```text
DISABLED      gateway not accepting new requests
IDLE          enabled, no queued requests
WAITING       requests exist but application is not currently processing them
PROCESSING    application is actively serving requests for this gateway
DRAINING      application draining; new requests queued, existing continue
LOADING       target application is being loaded
UNLOADING     application being unloaded because another gateway needs VRAM
ERROR         load failed / process died / forwarding errors
```

Note the asymmetry with [04 §3](04-application-manager.md#3-combined-ui-state): gateway
states describe the *request path* for one port; application states describe the
*process and residency*. A gateway is DRAINING/UNLOADING/LOADING because of what its
application is doing.

## 6. Request Lifecycle

```text
REQUEST RECEIVED
       ↓
Validate gateway (enabled? queue full? → 429/503)
       ↓
Create request (QUEUED)
       ↓
Queue request
       ↓
Scheduler evaluates
       ↓
Application available?
    /       \
   yes       no
   |          |
Forward      Determine switch
   |          |
Processing    ↓
   |       Load/unload (drain first)
   |          |
   └──────→ Forward
              ↓
           Complete
              ↓
           Metrics
```

### 6.1 Request Object

```text
request_id          string, unique
gateway_id
application_id

received_at
queued_at
started_at
completed_at

priority            effective (may be escalated per §4)
status
client_metadata     opaque, echoed/recorded as configured
input_tokens        from the upstream response headers (X-Input-Tokens)
output_tokens       from the upstream response headers (X-Output-Tokens)
error
```

`input_tokens` / `output_tokens` are captured from the upstream response headers
(`X-Input-Tokens` / `X-Output-Tokens`) when the gateway completes a request, and
feed the per-request token sums in [08 §9.2](08-metrics-module.md). They are a
complementary source: the app's log (§5) is the primary token-metrics source.

### 6.2 Request States

```text
QUEUED
WAITING_FOR_RESOURCE   app not resident; load in progress or pending
STARTING               app process being started
PROCESSING
COMPLETED
FAILED
CANCELLED
EXPIRED
```

### 6.3 Request Cancellation

- Client disconnects while request is **queued** → remove from queue.
- Client disconnects while request is **processing** → cancel the upstream request
  where the protocol supports it (e.g. HTTP client-abort propagation; cannot abort
  a mid-generation token stream for protocols without abort — in that case the
  request runs to completion but its output is discarded).
- Cancellation must never leave an orphaned request (Invariant 6).

## 7. Proxy Layer

The proxy exposes `<gateway-port>` and internally routes to `<application-port>`:

```text
Client
  |
  | POST :913000
  v
Gateway Proxy
  |
  | Application A1
  v
localhost:3000
```

The proxy acts as:

```text
Reverse Proxy + Request Queue + Scheduler Client
```

The actual application API remains unchanged — clients of a managed application see
exactly the same API on the gateway port.

### 7.1 Proxy Behaviour

The proxy must **not** forward immediately unless:

```text
application is ready (LOADED and health OK)
AND
gateway is allowed to process (enabled, concurrency slot free)
```

Otherwise: `request → queue`.

"Concurrency slot free" refers to the **application-level** cap
(`max_concurrent_requests` on the application), which is the hard cap across
*all* of the application's gateways. If the application is ready but busy
serving another gateway's request and the cap is reached, the new request is
queued at gateway level — it is **not** forwarded immediately, and gateway
priority does **not** bypass the queue. Priority only decides scheduling order
when a slot frees up. The net effect for two gateways on one application is:
identical waiting logic as when the application is not ready, with the only
difference being zero switching cost
([06 §14.1](06-scheduler.md#141-worked-example--two-gateways-one-application)).

### 7.2 Concurrency

Requests are processed concurrently when:

```text
application supports it (max_concurrent_requests)
AND
GPU memory permits it
AND
max_concurrent_requests permits it
```

Initial default: `max_concurrent_requests = 1` per application; later 2/4/8 for
inference servers.

The effective cap is:

```text
effective_concurrency =
    min(application.max_concurrent_requests,
        Σ gateway.max_concurrent_requests over the app's gateways)
```

i.e. the application-level limit always wins. Note the interaction with
context switching: `max_concurrent_requests = 1` is what serializes requests
across gateways of the same application; raising it to 2/4/8 *allows*
concurrent requests to interleave in the application (intended for inference
servers, not for context-sensitive single-stream workloads).

## 8. Streaming Requests

Critical for LLMs. The proxy must support:

```text
streaming HTTP responses
SSE (server-sent events)
chunked transfer encoding
WebSocket where required
```

Flow for a streamed LLM request:

```text
request
 ↓
queue
 ↓
application loaded
 ↓
stream tokens
 ↓
client receives tokens immediately
```

**Do not buffer the entire response.** The proxy pipes the upstream byte stream
through as it arrives, while the request remains counted as PROCESSING until the
stream ends.

## 9. Application-Level Scheduling (Multiple Gateways per Application)

Since multiple gateways can point to the same application, the scheduler operates on
**gateway demand**, while resource management operates on **application residency**.
This separation is the core of the architecture ([02 §3](02-architecture.md#3-key-architectural-decision)).

Example — `G1 → A1`, `G2 → A1`, `G3 → A2`; A1 has `G1 queue = 5`, `G2 queue = 3`:

```text
the scheduler does NOT load A1 twice.

application_demand(A1) = demand(G1) + demand(G2)
```

The scheduler then selects which gateway/request is served while A1 is resident.

```text
application_demand = Σ gateway_score(g)   for all gateways g → that application
```

Candidate selection:

```text
1. Compute gateway_score for all gateways with queued requests
2. candidate_gateway     = argmax(gateway_score)
3. candidate_application  = candidate_gateway.application
```

## 10. Gateway Actions (UI/API)

| Action | Behaviour |
| --- | --- |
| **Enable** | Start accepting requests |
| **Disable** | Stop accepting new requests; existing requests continue |
| **Process Now** | Raise scheduling urgency (see [06 §9](06-scheduler.md#9-process-now)) |
| **Clear Queue** | Cancel queued requests; never cancels active requests |
| **Edit** | Modify gateway configuration (port changes require gateway restart) |
| **Delete** | Only when `queue empty AND active_requests = 0`, or with explicit forced deletion |

## 11. Health Checks

Applications optionally expose:

```text
health_check    GET :3000/health
ready_check     GET :3000/ready
```

The scheduler prefers **readiness checks** over merely checking process existence
(see [04 §6](04-application-manager.md#6-readiness-detection)).
