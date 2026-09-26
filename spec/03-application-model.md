# Module 03 — Application Model

Each application represents one GPU workload. This module defines the application
data model, its types, its lifecycle scripts, and its memory model.

## 1. Identity

```text
id            string, unique, stable (used in config, API, logs)
name          string, display name
description   string, optional
enabled       bool
```

## 2. Process Configuration

```text
process_name    string, optional (helps process discovery)
start_script    path, starts the process
stop_script     path, optional (default: graceful SIGTERM kill of PID)
load_script     path, optional (loads GPU model/resources into a running app)
unload_script   path, optional (unloads GPU resources, process stays alive)
pid             int, runtime (owned by Application Manager)
```

### 2.1 Start Script

Starts the application process. Example:

```bash
/home/user/apps/qwen/start.sh
```

### 2.2 Stop Script

Stops the process. If empty, the Application Manager performs graceful termination:

```text
SIGTERM → wait → SIGKILL if necessary
```

### 2.3 Load Script

Loads the GPU model/resources into the running application. Examples:

```bash
curl localhost:3000/load
```

```bash
python load_model.py
```

### 2.4 Unload Script

Unloads GPU resources while leaving the process alive.

**Load/unload support rule:** if either `load_script` or `unload_script` is
unavailable, the application **does not support** the load/unload lifecycle. In that
case, the system may optionally support:

```text
Unload = Stop
Load   = Start
```

as a configurable behaviour, but it must be **distinguished from true model
unloading** (event logs, metrics, and the UI must not present process stop/start as
model load/unload).

### 2.5 Script Execution Contract

All scripts run with a controlled environment (see [12-security.md](12-security.md)):

- configured working directory
- configured environment variables (including `GPUSYB_APP_ID`, `GPUSYB_PORT`, `GPUSYB_PID`)
- execution timeout
- captured stdout / stderr
- recorded exit code

Script failures are recorded as application events.

## 3. Networking

```text
port                    int, the application's own port
protocol                http | https | ws | websocket+http
health_check_url        url, e.g. http://localhost:3000/health
health_check_method     GET (default) | HEAD
health_check_timeout    seconds
```

## 4. Application Type

Fixed enumeration:

```text
llama
ollama
harness
comfyui
tts
image_generation
video_generation
embedding
custom
```

The type selects a **TypeSpec** from the registry (see
[08 §9](08-metrics-module.md#9-type-metrics-contract-extensible-application-types)):
its declarative metric definition plus the optional **type handler plugin** (see
[04 §8](04-application-manager.md#8-type-handler-plugins)) that determines
readiness detection, activity detection, and metric collection. Every application
must also work through the generic spec.

## 5. Resource Requirements

```text
gpu_memory_required          user-supplied VRAM requirement (MB)
gpu_memory_observed_max      learned maximum observed VRAM (MB)

gpu_usage_expected           user-supplied expected GPU utilisation (%)
gpu_usage_observed_max       learned maximum observed GPU utilisation (%)

cpu_memory_required          user-supplied CPU RAM requirement
cpu_usage_expected           user-supplied expected CPU utilisation
```

Initial values:

```text
gpu_memory_observed_max = user supplied value
gpu_usage_observed_max  = user supplied value
```

### 5.1 Memory Learning

Every time an application runs, the observed maximum ratchets upward:

```text
observed_max = max(previous_observed_max, current_usage)
```

Example:

```text
Initial:   configured = 12 GB, observed_max = 12 GB
App later reaches 13.7 GB
Updated:   observed_max = 13.7 GB
```

The system thereby automatically learns actual requirements.

### 5.2 Effective Memory Requirement

```text
effective_gpu_memory =
    max(configured_gpu_memory, observed_gpu_memory_max)
```

with a configurable safety margin:

```text
required_memory = effective_gpu_memory × (1 + MEMORY_SAFETY_MARGIN)
```

Example: `18 GB × 1.10 = 19.8 GB`. Recommended default
`MEMORY_SAFETY_MARGIN = 10%`. The margin is configurable globally and per
application.

## 6. Timing (Measured, Not Configured)

```text
startup_time    time from process start to ready
load_time       time from load script start to ready
unload_time     time from unload start to baseline memory reached
stop_time       time from stop request to process exit
```

These are **automatically measured** (definitions in
[04 §5](04-application-manager.md#5-timing-measurement)) and are read-only in the UI.
They seed the scheduler's switching-cost model.

## 7. Lifecycle Configuration

```text
supports_load_unload     derived: load_script AND unload_script present
                         (or the explicit stop/start fallback is enabled)
allow_automatic_unload   bool
idle_unload_timeout      seconds, for idle-based automatic unloading
```

### 7.1 `allow_automatic_unload` Semantics

The field name is deliberately **`allow_automatic_unload`** (not the ambiguous
`allow_unloading`):

```text
allow_automatic_unload = true
→ the application may be automatically unloaded by the scheduler

allow_automatic_unload = false
→ the application must NEVER be automatically unloaded
```

An administrator can still **manually** unload it at any time. Manual unload overrides
protection but still obeys active-request protection (drain first; see
[14-invariants.md](14-invariants.md)).

## 8. Request Behaviour

```text
api_enabled                bool — requests accepted through a gateway
supports_queueing          bool
max_concurrent_requests    int — default 1; later 2/4/8 for inference servers
```

## 9. Activity Behaviour (Non-API Applications)

Applications such as ComfyUI may not expose requests through the load balancer:

```text
request_activity_supported   bool — false for ComfyUI-style apps
activity_detection_method    gpu_usage | process_activity | external_api | custom
```

When `request_activity_supported = false`, activity is inferred from GPU
utilisation, GPU memory, process CPU, or application-specific signals; idle detection
drives automatic unloading (see [06 §10](06-scheduler.md#10-non-api-applications--idle-unloading)).

## 10. Optional Retention Feature: Pinned Applications

```text
pinned = true
```

A pinned application remains resident unless a manual action is taken. Useful for a
frequently used LLM, TTS, or background service. A pinned application is never
selected as an unload candidate automatically. (Recommended optional feature —
Phase 5.)

## 11. Priority Separation

- **Gateway priority** = request importance (scheduling input).
- **Application retention priority** = resource retention value (future, Phase 5).

Initially these are kept separate; do not add application-level priority until needed
to avoid unnecessary complexity.
