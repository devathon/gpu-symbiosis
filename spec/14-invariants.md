# Module 14 — Invariants & Error Handling

These invariants are enforced throughout the implementation. They are **hard**
constraints: no configuration, UI action, or scheduler decision may violate one.

## 1. Critical Invariants

### Invariant 1 — No unload with active requests

> Never unload an application with active requests.

```text
active_requests > 0   →   cannot unload
```

Applies to automatic unloading, manual unloading, and switching alike — they all
must drain first. Only an explicit **Force Stop** (administrative emergency,
UI-confirmed, audited) may terminate active requests.

### Invariant 2 — Never exceed the GPU memory budget

> Never exceed the configured GPU memory budget.

```text
reserved_vram + candidate_required_vram   <=   usable_vram
```

where `usable_vram = total_vram - gpu_memory_reserve` and `candidate_required_vram`
includes the safety margin. Enforced at reservation time (§6.6 of
[07](07-resource-manager.md#6-resource-reservation)); the scheduler lock makes
double-booking impossible.

### Invariant 3 — One lifecycle transition per application

> Only one lifecycle transition per application at a time.

```text
LOADING   →   cannot simultaneously UNLOAD
```

The state machine ([04 §2](04-application-manager.md#2-two-independent-state-machines))
rejects any second transition request while one is in flight.

### Invariant 4 — One scheduler transaction at a time

> Only one scheduler resource transition transaction at a time.

Enforced by the scheduler lock / transactional state machine
([07 §7](07-resource-manager.md#7-concurrency--locking)).

### Invariant 5 — No queued request lost on switching

> Queued requests must not be lost during application switching.

A switch only proceeds through drain → unload → load → ready → forward. Queued
requests survive every phase; on failure they re-enter the queue under the retry
policy below (never silently dropped).

### Invariant 6 — No orphaned requests on client disconnect

> A client disconnect should not leave an orphaned request.

Queued → removed from queue; processing → upstream cancellation where supported,
else discard-and-complete. Request state always reaches a terminal state.

### Invariant 7 — Manual actions never kill active requests

> Manual actions cannot terminate active requests unless explicitly forced.

Manual unload/stop with active requests enters DRAINING and waits. `?force=true`
is the only exception and requires explicit UI confirmation + audit event.

### Additional hard rules

- Never automatically unload an application with `allow_automatic_unload = false`.
- Never automatically unload a `pinned` application.
- Never mark an application `LOADED` on script exit alone — readiness is verified
  ([04 §4.3](04-application-manager.md#4-lifecycle-operations)).
- Never present process stop/start as model load/unload when the app lacks
  load/unload scripts ([03 §2.4](03-application-model.md#24-unload-script)).

## 2. Failed Switch Recovery

Suppose:

```text
A1 unloaded
A2 fails to load
```

Do **not** automatically reload A1 blindly. Instead:

```text
A2 → ERROR
A1 → OFF
```

The scheduler re-evaluates from scratch. If A1 still has queued requests:

```text
A1 becomes candidate   →   and can be loaded again
```

A1's queued requests remain queued through the whole episode (Invariant 5); their
clients keep waiting (subject to gateway `max_waiting_time` policy and
`request_timeout`).

## 3. Load Failure Protection

Track:

```text
consecutive_load_failures
```

After `N` consecutive failures (default 3, configurable):

```text
disable automatic loading temporarily
application = FAULTED
require manual intervention or cooldown
```

- FAULTED applications stay visible with failure history (events + script output).
- Manual `load` clears the faulted state if it succeeds.
- A cooldown (configurable) after which automatic loading retries once.

## 4. Retry Policy

Per application/gateway:

```text
max_load_retries
load_retry_delay
request_retry_enabled
```

Rules:

- A failed **load** may be retried up to `max_load_retries` with `load_retry_delay`
  between attempts; queued requests wait across retries.
- A **request** is retried only if `request_retry_enabled = true` **and** the
  failure happened before forwarding (e.g. app died while loading).

> **Important:** a request must not be automatically duplicated if the upstream
> application may have already processed it (post-forward failures return the
> error to the client; no blind resend).

If retries are exhausted:

```text
queued requests → return error (502/503) to clients
application     → ERROR / FAULTED per §3
```

Queued requests should never be silently lost — they are either retried or return
an explicit error.

## 5. Error Detection

Applications can fail to:

```text
start
load
unload
stop
respond
```

The scheduler must detect each (timeouts, non-zero script exit, readiness failure,
process death, health-check failure on a LOADED app). Example:

```text
A2 LOAD
↓
timeout 60 sec
↓
A2 ERROR
```

On a LOADED app whose health check starts failing: mark `ERROR`, stop forwarding
new requests (queue them or reject per gateway state), keep in-flight requests
running, and emit a `PROCESS_DIED`-class event. The scheduler then decides
restart per the retry policy.
