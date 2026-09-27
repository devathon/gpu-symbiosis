# Module 11 — Web UI

The Web UI is the configuration and monitoring surface. It is effectively
**administrative access to the machine** (it can start/stop processes and execute
scripts), so it requires admin authentication — see [12 §3](12-security.md#3-web-ui-security).

Views:

```text
Dashboard (default)
Applications
Gateways
Scheduler
Events
Settings
```

## 1. Dashboard

```text
GPU
─────────────────────────────────

VRAM
████████████████░░░░ 18.4 / 24 GB

GPU
████████████░░░░░░░░ 61%

Temperature
67°C

Power
112 W
```

Then:

```text
ACTIVE APPLICATIONS

Qwen 3.8     LOADED     18.1 GB
ComfyUI      IDLE        6.2 GB
```

And:

```text
QUEUES

Qwen 3.5
████████░░ 8 / 10
Oldest: 42 sec

TTS
██░░░░░░░░ 2 / 20
Oldest: 8 sec
```

The dashboard updates live (SSE or 1 s polling) and always reflects the same state
the scheduler sees.

## 2. Application Management UI

### 2.1 Application List

Columns:

```text
Name
Type
Process
Status

GPU Usage
GPU Memory
CPU Usage
CPU Memory

Active Time

Startup
Load
Unload
Stop

Actions
```

Card example:

```text
┌──────────────────────────────────────────────────────────────┐
│ Qwen 3.8 │ llama │ LOADED                                  │
│ GPU ████████████░░ 72%                                      │
│ VRAM ███████████████░ 18.1 / 24 GB                         │
│ CPU ███░░░░░░░ 18%                                          │
│ RAM █████░░░░░ 8.2 GB                                       │
│                                                              │
│ Active: 2h 31m                                               │
│ Start: 7.2s   Load: 3.8s   Unload: 2.1s                    │
│                                                              │
│ [Unload] [Stop] [Edit]                                      │
└──────────────────────────────────────────────────────────────┘
```

Action availability depends on state (e.g. `[Stop]` when RUNNING, `[Load]` when
RUNNING/OFF, `[Unload]` when LOADED, `[Force Stop]` only after confirmation).

### 2.2 Status Indicator

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

(Projection of the separate process + residency states, see
[04 §3](04-application-manager.md#3-combined-ui-state).)

### 2.3 Add / Edit Application

**General**

```text
Name
Description
Enabled
Application Type
```

**Process**

```text
Process Name
Start Script
Stop Script
```

**Loading**

```text
Load Script
Unload Script
Allow Automatic Unload
Idle Unload Timeout
```

**Network**

```text
Port
Protocol
Health Check
```

**Resource**

```text
GPU Memory
GPU Usage
CPU Memory
GPU Memory Safety Margin
```

**Timing**

```text
Startup Time
Load Time
Unload Time
Stop Time
```

Timing fields are **read-only** and automatically calculated.

## 3. Gateway Management UI

Columns:

```text
Gateway
Port
Application
Status

Queue
Waiting Time

Priority

Enabled

Actions
```

Example:

```text
Qwen 3.8 API
:913000
Qwen 3.8
PROCESSING

Queue:
███░░░░░ 3 / 10

Waiting:
████░░░░ 18 / 60 sec

[Process Now] [Disable] [Edit]
```

### 3.1 Gateway States

```text
DISABLED
IDLE
WAITING
PROCESSING
DRAINING
LOADING
ERROR
```

### 3.2 Gateway Actions

| Action | Behaviour |
| --- | --- |
| **Enable** | Start accepting requests |
| **Disable** | Stop accepting new requests; existing continue |
| **Process Now** | Raises scheduling priority (time-decayed boost, never kills active work) |
| **Clear Queue** | Cancels queued requests; never cancels active requests |
| **Edit** | Modify gateway configuration |
| **Delete** | Only when queue empty and `active_requests = 0`, or with explicit forced deletion |

## 4. Queued Request Inspector

For each gateway queue: list of queued requests with `request_id`,
`received_at`, `queued_at`, wait time, priority, client metadata. Actions:
cancel (queued only), force-process (raises to top of queue).

## 5. Scheduler Score (per gateway)

The scheduler is made explainable **on the gateways themselves**, not in a
separate explainer panel (active / candidate / resident apps are already evident
from the Applications section). Each gateway card shows its live **scheduler
score** (spec 06 §6.9); hovering the score reveals the **breakup**:

```text
score 42.7
  priority          10
  priority_pressure 30.0   (priority × backlog)
  backlog_pressure  3.0    (max(queue fill, oldest wait))
  waiting_pressure  5.2    (oldest wait / max wait)
  recent_demand     7.5    (priority × normalized arrival/service)
  demand (arr/svc)  12.0 / 4.3 = 2.79
  ────────────────
  switching penalty 8.4    ← only when this gateway is the current candidate
  effective score   34.3   (score − penalty)
  → switch (a2 beats a1 by margin 1.0)
```

The score is computed per gateway (`GET /api/status` → `gateways[id].score`);
demand is time-decayed (spec 06 §6.3/§6.4), so it is a live metric, not a cached
candidate value. The candidate context (switching penalty, effective score,
decision) comes from `GET /api/scheduler/current-decision` and is shown only for
the gateway that is the current candidate. The `current-decision` endpoint and
the scheduler_events stream ([06 §19](06-scheduler.md#19-explainability)) also
expose current residents, VRAM accounting (total/reserve/resident/reserved/
external/free), and switch tracking for the Scheduler view and event log.

## 6. Event Log

```text
18:04:12  Request received → Qwen 3.5
18:04:12  Qwen 3.5 queued
18:04:13  Scheduler candidate = Qwen 3.5
18:04:13  Qwen 3.8 active request = 1
18:04:13  Waiting for Qwen 3.8
18:04:27  Qwen 3.8 request complete
18:04:27  Qwen 3.8 draining
18:04:28  Qwen 3.8 unloading
18:04:31  Qwen 3.8 unloaded
18:04:31  Qwen 3.5 loading
18:04:36  Qwen 3.5 ready
18:04:36  Request forwarded
```

Filterable by type (request / lifecycle / scheduler / error) and by application or
gateway; live-tail mode; full detail (JSON) on click.

## 7. Settings UI

Edits all global configuration keys from [10 §6](10-api.md#6-global-configuration)
with validation (e.g. reserve < total VRAM, margins ≥ 0) and a change audit trail
in events.

---

## Implementation Status

Implemented: the dashboard (§1, §5, §6) and the management surface (§2, §3, §4)
are live in the single-page web UI, organised into **Dashboard / Applications /
Gateways** tabs:

- **§2 Application Management** — list as state-dependent cards (Start, Unload,
  Force Stop [confirmed], Pin/Unpin, Edit, Delete [confirmed]); create/edit form
  with the §2.3 sections (General / Process / Loading / Network / Resource,
  read-only Timing). Deleting an application removes its gateways and request
  history.
- **§3 Gateway Management** — list as cards with Process Now (§3.2 boost, never
  kills active work), Disable/Enable, Edit, Delete [confirmed]; create/edit form
  (routing + queueing). Creating a gateway — and changing its port — starts a
  live listener immediately; deleting one tears it down.
- **§4 Queued Request Inspector** — per-gateway live queue (2 s polling):
  request id, priority, wait, received, metadata; **Cancel** (queued only) and
  **Force** (raise to top of queue; served next, never kills in-flight work).
- **§12 §2 API keys** — per-gateway key management panel; a new key is shown in
  full exactly once.
- **§12 §3 auth** — open dev mode until the first admin exists; then a login
  panel gates the UI, carrying the session cookie and the double-submit CSRF
  token on every state-changing call.

Not yet implemented: §7 Settings UI. Covered by `tests/test_admin_ui.py`.
