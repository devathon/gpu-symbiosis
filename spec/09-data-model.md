# Module 09 — Data Model & Database

A lightweight database (**SQLite**) is sufficient initially. WAL mode; the database
is the source of truth for configuration and history; runtime state (process PIDs,
active connections, in-flight transitions) lives in the in-memory state manager and
is rebuilt/reconciled from SQLite + live probes on startup.

## 1. Core Tables

```text
applications
gateways
requests
application_events
gateway_events
metrics
scheduler_events
settings
```

## 2. Applications Table

```text
applications
-------------
id                      TEXT PRIMARY KEY
name                    TEXT NOT NULL
description             TEXT
process_name            TEXT

start_script            TEXT NOT NULL
stop_script             TEXT
load_script             TEXT
unload_script           TEXT

port                    INTEGER NOT NULL
protocol                TEXT DEFAULT 'http'
health_check_url        TEXT
health_check_method     TEXT DEFAULT 'GET'
health_check_timeout    INTEGER

application_type        TEXT NOT NULL
                        CHECK (application_type IN
                            ('llama','ollama','harness','comfyui','tts',
                             'image_generation','video_generation','embedding','custom'))

configured_gpu_memory_mb    INTEGER NOT NULL
observed_max_gpu_memory_mb  INTEGER
configured_gpu_usage_pct    REAL
observed_max_gpu_usage_pct  REAL

cpu_memory_required_mb      INTEGER
cpu_usage_expected_pct      REAL

gpu_memory_safety_margin    REAL DEFAULT 0.10

startup_time_sec          REAL
load_time_sec             REAL
unload_time_sec           REAL
stop_time_sec             REAL

supports_load_unload      BOOLEAN
allow_automatic_unload    BOOLEAN DEFAULT true
idle_unload_timeout_sec   INTEGER
activity_detection_method TEXT DEFAULT 'gpu_usage'
idle_threshold_gpu_pct    REAL
loaded_gpu_memory_mb      REAL DEFAULT 0.0
pinned                    BOOLEAN DEFAULT false

api_enabled               BOOLEAN DEFAULT true
supports_queueing         BOOLEAN DEFAULT true
max_concurrent_requests   INTEGER DEFAULT 1

consecutive_load_failures INTEGER DEFAULT 0
faulted_until             TIMESTAMP

enabled                   BOOLEAN DEFAULT true
created_at                TIMESTAMP
updated_at                TIMESTAMP
```

## 3. Gateways Table

```text
gateways
--------
id                       TEXT PRIMARY KEY
name                     TEXT NOT NULL
port                     INTEGER UNIQUE NOT NULL
application_id           TEXT NOT NULL REFERENCES applications(id)

priority                 INTEGER DEFAULT 5
max_waiting_queue        INTEGER DEFAULT 20
max_waiting_time_sec     INTEGER DEFAULT 300
wait_timeout_policy      TEXT DEFAULT 'ESCALATE_PRIORITY'
                        CHECK (wait_timeout_policy IN
                            ('REJECT','EXPIRE','ESCALATE_PRIORITY','PROCESS_ANYWAY'))

enabled                  BOOLEAN DEFAULT true
request_timeout_sec      INTEGER
max_concurrent_requests  INTEGER DEFAULT 1

created_at               TIMESTAMP
updated_at               TIMESTAMP
```

## 4. Requests Table

```text
requests
--------
id                       TEXT PRIMARY KEY
gateway_id               TEXT NOT NULL REFERENCES gateways(id)
application_id           TEXT NOT NULL REFERENCES applications(id)

status                   TEXT NOT NULL
                        CHECK (status IN
                            ('QUEUED','WAITING_FOR_RESOURCE','STARTING','PROCESSING',
                             'COMPLETED','FAILED','CANCELLED','EXPIRED'))

received_at              TIMESTAMP NOT NULL
queued_at                TIMESTAMP NOT NULL
started_at               TIMESTAMP
completed_at             TIMESTAMP

priority                 INTEGER NOT NULL
client_metadata          TEXT          -- opaque JSON
input_tokens             INTEGER
output_tokens            INTEGER
queue_wait_sec           REAL
service_time_sec         REAL
error                    TEXT
```

Indexes: `(gateway_id, status)`, `(application_id, status)`, `(queued_at)`.
Completed requests older than the retention window are archived to a request history
table (or pruned) — hot table stays small.

## 5. Event Tables

```text
application_events
------------------
id                       INTEGER PRIMARY KEY
application_id           TEXT NOT NULL
event_type               TEXT NOT NULL
                         -- START, START_TIMEOUT, STOP, STOPPED, LOAD, LOAD_READY,
                         -- LOAD_FAILED, UNLOAD, UNLOADED, UNLOAD_FAILED,
                         -- PROCESS_DIED, SWITCH_IN, SWITCH_OUT, FAULTED, FAULT_CLEARED
detail                   TEXT          -- JSON payload
at                       TIMESTAMP NOT NULL

gateway_events
--------------
id                       INTEGER PRIMARY KEY
gateway_id               TEXT NOT NULL
event_type               TEXT NOT NULL
                         -- REQUEST_RECEIVED, REQUEST_QUEUED, REQUEST_FORWARD,
                         -- REQUEST_COMPLETED, REQUEST_FAILED, REQUEST_CANCELLED,
                         -- REQUEST_EXPIRED, QUEUE_FULL, PROCESS_NOW, ENABLED, DISABLED
detail                   TEXT
at                       TIMESTAMP NOT NULL

scheduler_events
----------------
id                       INTEGER PRIMARY KEY
trigger                  TEXT NOT NULL
                         -- new_request, request_complete, load_complete,
                         -- unload_complete, queue_timeout, resource_change,
                         -- idle_timeout, process_now, reconcile
candidate_gateway_id     TEXT
candidate_application_id TEXT
candidate_score          REAL
switching_penalty        REAL
effective_score          REAL
current_score            REAL
decision                 TEXT NOT NULL
                         -- keep, activate_concurrent, switch, drain, wait,
                         -- emergency_switch, no_candidate
detail                   TEXT          -- full scoring breakdown JSON
at                       TIMESTAMP NOT NULL
```

## 6. Metrics Tables

Per [08 §6](08-metrics-module.md#6-storage--retention):

```text
metrics_raw             -- 1 s samples: application_id, gpu_pct, gpu_mem_mb,
                        -- gpu_temp, gpu_power_w, cpu_pct, cpu_rss_mb, threads,
                        -- active_requests, at            → retain 24 h
metrics_1m              -- 1-minute aggregates (min/max/avg/sum)  → retain 30 days
metrics_hourly          -- hourly aggregates                      → retain indefinitely
```

Request-level latency percentiles are computed from the requests table for the hot
window and rolled into aggregates.

## 7. Settings Table

Key/value store for global configuration (defaults shown are the recommended
defaults from [10 §6](10-api.md#6-global-configuration)):

```text
settings
--------
key         TEXT PRIMARY KEY
value       TEXT NOT NULL          -- JSON scalar
updated_at  TIMESTAMP
```

Keys: `gpu.memory_reserve_mb`, `gpu.memory_safety_margin`,
`scheduler.interval_ms`, `scheduler.min_residency_sec`, `scheduler.switch_margin`,
`scheduler.switch_cost_weight`, `scheduler.emergency_wait_sec`,
`scheduler.emergency_queue_threshold`, `scheduler.switch_cooldown_sec`,
`metrics.interval_sec`, `queue.default_max_waiting_requests`,
`queue.default_max_waiting_time_sec`, `requests.default_request_timeout_sec`,
`load.max_retries`, `load.retry_delay_sec`, `load.max_consecutive_failures`,
`security.*`.

## 8. Startup Reconciliation

On service start the state manager:

1. Loads configuration from SQLite.
2. Probes each `enabled` application: does the process exist (PID file / process
   name)? What is its GPU attribution?
3. Rebuilds process/residency states from observation (an app found running with
   VRAM is `STARTED/LOADED`; running without VRAM is `STARTED/OFF`; else
   `STOPPED/OFF`).
4. Requeues nothing (requests are not persisted across restarts; clients retry),
   but logs all observed states as events.
