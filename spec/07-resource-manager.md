# Module 07 — Resource Manager

The Resource Manager is the single source of truth for physical resource state. All
capacity decisions by the scheduler go through its numbers.

## 1. Tracked Resources

```text
GPU VRAM
GPU utilization
CPU RAM
CPU utilization
GPU temperature
GPU power
```

Most importantly:

```text
total GPU memory
used GPU memory
free GPU memory
application GPU memory (per managed application)
```

## 2. GPU Backend

NVIDIA implementation initially uses **NVML** (via `pynvml` or equivalent). The
backend is an interface so other vendors can be added later:

```text
GpuBackend
────────
total_memory()
used_memory()
utilization()
temperature()
power()
processes()          → [(pid, gpu_memory, utilization)]
```

Collected:

```text
GPU utilization
memory used
memory total
temperature
power
```

PCIe throughput (`rx_mbps`, `tx_mbps`) is read directly from
`nvmlDeviceGetPcieThroughput`, which returns an **instantaneous rate in KB/s**
— NVML measures a byte counter over a fixed 20 ms window, so the value is
already a rate, *not* a cumulative counter. We convert to MB/s for the API/UI
and read it the same way nvtop does (no diffing, no divide-by-interval).

## 3. Process GPU Attribution

Map `PID → GPU memory` using NVML process information:

```text
PID → GPU memory
```

If an application has multiple processes:

```text
application PIDs = [pid1, pid2, pid3]

application_gpu_memory = Σ process_gpu_memory
```

Process sets are resolved from the tracked root PID plus child discovery (cgroup or
process-tree walk); attribution is refreshed every metrics interval.

### Per-process GPU utilization (the active/RUNNING display signal)

Per-process *compute* utilization — the active/RUNNING display signal (spec 04 §3) — is read with
NVML `nvmlDeviceGetProcessUtilization(device, last_seen_timestamp)`, which returns a
sample per process that used the GPU since the timestamp:
`{pid, timeStamp, smUtil, memUtil, encUtil, decUtil}`. The per-process GPU % is
`smUtil` (the SM / compute rate). The backend makes **one call per sampling period**
(refreshed every tick), keeps a `{pid: smUtil}` map, and advances the timestamp to the
latest sample seen; a process absent from the map is idle (0%).

Notes:
- The API needs a recent driver (525+). On older drivers the call is unavailable and
  the backend reports `supports_process_utilization = False`, so the resource manager
  falls back to the device-level rate (`nvmlDeviceGetUtilizationRates`) as a proxy.
- When per-process is supported, a 0.0 value means *idle*, not *unknown* — so there is
  deliberately no device-level fallback (which would mis-attribute a co-resident app's
  load to an idle app).
- An app with **no PID** (stopped / never started) always reads 0.0 — the device-level
  rate is never attributed to it, so a stopped app can never look "active" because a
  co-resident app is busy (the multi-app mis-attribution guard).
- Samples with `smUtil > 100` or a non-newer timestamp are dropped (NVML bug #110, the
  same filter nvtop applies).

Unattributable GPU memory (other users' processes, desktop, drivers) is accounted
as **external usage** and reduces available VRAM:

```text
available_vram =
    usable_vram
  - Σ reserved_by_managed_apps
  - external_unattributed_usage
```

## 4. CPU Monitoring

Use process-level metrics (`/proc` or `psutil`):

```text
CPU %
RSS
VMS
threads
```

Tracked per application and system-wide.

## 5. Per-Application Metrics

Per application:

GPU:

```text
GPU usage %
GPU memory MB
GPU memory %
GPU power
GPU temperature
```

CPU:

```text
CPU %
CPU memory
CPU threads
```

Each stored as `current`, `average`, `maximum` (windows per
[08 §6](08-metrics-module.md#6-storage--retention)).

## 6. Resource Reservation

When an application starts loading, the scheduler **reserves** its required VRAM
immediately:

```text
reserve required VRAM
```

This prevents another scheduler operation from simultaneously deciding that the same
VRAM is available. Example:

```text
A1 loading (reserved)
A2 request arrives
→ A2 sees A1_reserved_memory as unavailable
```

Reservations are released on:

- successful load (becomes actual attribution),
- load failure / abort,
- unload completion (actual VRAM release verified).

The scheduler's fit check uses:

```text
available =
    usable_vram
  - Σ resident_app_reserved_or_used
```

so two concurrent decisions can never double-book the same VRAM (Invariant 2).

## 7. Concurrency & Locking

- Only **one resource transition** may occur at a time. Use a `scheduler_lock` or a
  transactional state machine.
- Prevents races such as:

  ```text
  A1 unload + A2 load
  ── simultaneously ──
  A3 unload + A4 load
  ```

- The lock is held across the whole drain → unload → load → ready transaction of a
  switch; request forwarding is **not** blocked by the lock (requests in flight
  continue on their own connections).
- The 1 s reconciliation loop takes the same lock; if a transition is in progress it
  defers.

## 8. VRAM Verification

After an unload, the Resource Manager **verifies** VRAM was actually released
(observed attribution dropped below the app's unloaded baseline) before the
scheduler proceeds to load the candidate. If verification fails, the transaction is
aborted and the app moves to ERROR (see [14 §2](14-invariants.md#2-failed-switch-recovery)).
