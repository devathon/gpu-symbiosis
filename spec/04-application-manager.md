# Module 04 — Application Manager

The Application Manager owns application processes and their residency transitions:
starting, stopping, loading, unloading, state detection, and timing measurement. It
executes lifecycle actions **only on behalf of** the scheduler (or explicit manual UI
actions) and reports state transitions back to the State Manager.

## 1. Responsibilities

- Starting processes
- Stopping processes
- Loading models
- Unloading models
- Detecting process state
- Measuring startup time, load time, unload time, stop time
- Tracking maximum VRAM, GPU utilisation, CPU utilisation

## 2. Two Independent State Machines

Applications have **two related but distinct** concepts that must **not** be combined
into one state:

### 2.1 Process State

```text
STOPPED
STARTING
RUNNING
STOPPING
ERROR
```

### 2.2 Residency State

```text
OFF
LOADING
LOADED
UNLOADING
```

The combination is meaningful — for example:

```text
Process = RUNNING,  Residency = OFF
→ the application process exists but its model/resources are unloaded
```

### 2.3 Valid State Combinations

| Process \ Residency | OFF | LOADING | LOADED | UNLOADING |
| --- | --- | --- | --- | --- |
| STOPPED | ✓ | — | — | — |
| STARTING | ✓ | ✓ | — | — |
| RUNNING | ✓ | ✓ | ✓ | ✓ |
| STOPPING | ✓ | — | — | — |
| ERROR | ✓ (cleanup) | ✓ (abort) | ✓ (abort) | ✓ (abort) |

Invariant 3 ([14-invariants.md](14-invariants.md)): only **one lifecycle transition
per application** may be in flight at a time — `LOADING` can never overlap `UNLOADING`.

## 3. Combined UI State

The UI exposes a simplified projection:

```text
OFF
STARTING
LOADING
LOADED
PROCESSING
IDLE
DRAINING
UNLOADING
STOPPING
ERROR
```

Where:

- `PROCESSING` = LOADED and `active_requests > 0`
- `IDLE` = LOADED, `active_requests = 0`, and (for non-API apps) activity below threshold
- `DRAINING` = LOADED, no new requests accepted, `active_requests` draining toward 0

Internally, process and residency states remain separate.

## 4. Lifecycle Operations

### 4.1 Start

```text
1. Validate: process STOPPED or ERROR (cleaned)
2. Set Process = STARTING
3. T0 = process start (execute start_script, spawn + track PID)
4. Poll readiness (see §6) every 500 ms
5. On ready:  Process = RUNNING, Residency = LOADED
              startup_time = readiness_time - T0
6. On 60 s timeout: Process = ERROR, error = STARTUP_TIMEOUT
```

### 4.2 Stop

Graceful by default:

```text
STOP requested
      ↓
DRAINING (if active_requests > 0 and not forced)
      ↓
active requests complete
      ↓
STOPPING
      ↓
stop_script (or SIGTERM → wait → SIGKILL)
      ↓
Process = STOPPED, Residency = OFF
```

**Force Stop** (explicit UI/admin action) skips draining; it is reserved for
administrative emergencies and is logged as such.

### 4.3 Load

```text
1. Validate: Process = RUNNING, Residency = OFF (or STOPPED with start/load fallback)
2. Reserve required VRAM with Resource Manager (see 07 §6)
3. Set Residency = LOADING
4. T0 = load script begins
5. Execute load_script
6. Do NOT mark LOADED merely because the script exited successfully —
   perform a readiness check (health/ready endpoint, expected GPU memory, or
   type-handler ready signal)
7. On ready: Residency = LOADED; load_time = T1 - T0
8. On timeout (MAX_LOAD_DETECTION_TIME): Residency = OFF, Process = ERROR
```

### 4.4 Unload

```text
1. Validate: Residency = LOADED and active_requests = 0 (hard invariant)
2. Set Residency = UNLOADING
3. T0 = unload begins
4. Execute unload_script
5. T1 = GPU memory falls below unload threshold
   (the application's unloaded baseline, e.g. process baseline + small epsilon)
6. Residency = OFF; unload_time = T1 - T0
7. Release VRAM reservation
```

Memory drain example:

```text
A1 memory: 18.2 → 17.1 → 12.4 → 4.1 → 2.1 GB
                          ↓
                    reaches unloaded baseline → UNLOADED
```

If the unload threshold is not reached within a timeout, the unload is marked
failed: Residency stays LOADED, app moves to ERROR, and the scheduler re-evaluates
(see [14 §2](14-invariants.md#2-failed-switch-recovery)).

## 5. Timing Measurement

### 5.1 Startup Time

```text
T0 = process start
```

The application is considered **ready** when the **first** of these occurs:

```text
application health check succeeds
OR
expected GPU memory is reached
OR
application-specific ready signal occurs
```

with:

```text
MAX_STARTUP_DETECTION_TIME = 60 seconds
```

Therefore:

```text
startup_time = readiness_time - start_time
```

If readiness is not detected within 60 seconds:

```text
startup_time = 60 sec
state = ERROR / STARTUP_TIMEOUT
```

Measured startup times update the application's `startup_time` (ratchet: keep the
best observed value, or rolling average — implementation choice; must be visible).

### 5.2 Load Time

For load/unload-capable applications:

```text
T0 = load script begins
T1 = application reaches ready state
load_time = T1 - T0
```

The application must not become `LOADED` merely because the load script exited
successfully; a readiness check is required.

### 5.3 Unload Time

```text
T0 = unload begins
T1 = GPU memory falls below unload threshold
unload_time = T1 - T0
```

### 5.4 Stop Time

```text
T0 = stop request accepted (or stop script begins)
T1 = process fully exited
stop_time = T1 - T0
```

## 6. Readiness Detection

Preferred order:

1. **Ready endpoint** (`ready_check` if configured, else `health_check`)
2. **Expected GPU memory reached** (observed memory ≥ `effective_gpu_memory`
   within tolerance — good for model loads)
3. **Type-handler ready signal** (e.g. Ollama `/api/version`, llama server
   first-token endpoint, ComfyUI `/system_stats`)

Process existence alone is **never** sufficient readiness.

## 7. Dead Process Detection

The process monitor polls each tracked PID (each metrics interval):

```text
PID no longer exists
        ↓
Process = ERROR (error = PROCESS_DIED)
        ↓
release its VRAM reservation immediately
        ↓
queued requests for it enter WAITING_FOR_RESOURCE / retry policy
        ↓
scheduler re-evaluates (may decide to restart per retry policy)
```

A process death is a hard event: it wakes the scheduler immediately.

## 8. Type Handler Plugins

The application type is extensible via a **TypeSpec** — a registered metric
definition plus an optional handler (see the contract in
[08 §9](08-metrics-module.md#9-type-metrics-contract-extensible-application-types):
what is recorded and displayed is declared by the spec; *how* it is fetched is
the handler's job). The handler implements the `ApplicationTypeHandler` interface:

```text
ApplicationTypeHandler
──────────────────────
detect_ready()
collect_metrics()
detect_activity()
get_request_metrics()
prepare_load()
prepare_unload()
```

Built-in handlers:

```text
LlamaHandler        token metrics, prompt/generation speed, request metrics
OllamaHandler       library API metrics
ComfyUIHandler      queue/progress API, GPU-activity interpretation
TTSHandler          request metrics
GenericHandler      fallback for everything
```

This keeps application-specific logic out of the scheduler.

### 8.1 Generic Handler

Every application must work even without a specialized handler. The generic handler
supports:

```text
process monitoring
GPU monitoring
start
stop
load
unload
health check
```

Specialized handlers add:

```text
token metrics
request metrics
GPU activity interpretation
```

## 9. Load Failure Protection

Track `consecutive_load_failures` per application. After `N` consecutive failures
(default N = 3, configurable):

```text
application = FAULTED
→ automatic loading disabled
→ manual intervention or cooldown required
```

Failures reset on a successful load. FAULTED apps remain visible in the UI with the
failure history. See [14 §3](14-invariants.md#3-load-failure-protection).

## 10. Events Emitted

All transitions emit structured events (consumed by [08](08-metrics-module.md) and
[13](13-observability.md)):

```text
START, START_TIMEOUT
STOP, STOPPED
LOAD, LOAD_READY, LOAD_FAILED
UNLOAD, UNLOADED, UNLOAD_FAILED
PROCESS_DIED
REQUEST_START, REQUEST_END
SWITCH_IN, SWITCH_OUT
FAULTED, FAULT_CLEARED
```
