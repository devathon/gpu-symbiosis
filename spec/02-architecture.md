# Module 02 — Architecture

## 1. High-Level Architecture

```text
                         ┌──────────────────────────┐
                         │        Web UI             │
                         │ Applications / Gateways   │
                         │ Monitoring / Settings     │
                         └────────────┬─────────────┘
                                      │
                                      ▼
                         ┌──────────────────────────┐
                         │      Control Plane       │
                         │                          │
                         │  Application Manager     │
                         │  Gateway Manager         │
                         │  Scheduler               │
                         │  Resource Manager        │
                         │  State Manager           │
                         │  Metrics Collector       │
                         └────────────┬─────────────┘
                                      │
                  ┌───────────────────┼───────────────────┐
                  │                   │                   │
                  ▼                   ▼                   ▼
          ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
          │   Gateway    │    │   Gateway    │    │   Gateway    │
          │   :913000    │    │   :913001    │    │   :913002    │
          └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
                 │                   │                   │
                 └───────────────────┼───────────────────┘
                                     ▼
                              Request Queues
                                     │
                                     ▼
                                Scheduler
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
                 A1 Qwen          A2 Qwen          A3 Comfy
                 :3000            :3001            :8188
```

## 2. Component Responsibilities

| Component | Responsibilities |
| --- | --- |
| **Gateway Manager** | Listen on gateway ports; accept requests; identify destination application; track active requests; queue requests; forward requests; return responses; track request duration, arrival rate, priority |
| **Scheduler** | Decide which gateway is processed next; whether the required app can coexist; whether another app must be unloaded; whether the current app stays resident; when switching happens; whether switching cost justifies switching |
| **Application Manager** | Start/stop processes; load/unload models; detect process state; measure startup/load/unload/stop time; track max VRAM, GPU & CPU utilisation (details in [04](04-application-manager.md)) |
| **Resource Manager** | Track total/used/free GPU memory, per-application GPU memory, GPU utilisation, CPU RAM, CPU utilisation, temperature, power (details in [07](07-resource-manager.md)) |
| **Metrics Collector** | Continuously sample applications (default 1 s): GPU utilisation, GPU memory, CPU utilisation, CPU memory, process state; application-specific collectors (e.g. tokens/sec for llama) (details in [08](08-metrics-module.md)) |
| **State Manager** | Own the application/gateway/request state machines and enforce the invariants in [14](14-invariants.md) |
| **Database** | Persistence of config, requests, events, metrics (details in [09](09-data-model.md)) |

## 3. Key Architectural Decision: Resource-Residency Scheduling

Do **not** make the scheduler fundamentally "application switching". Make it a
**resource-residency scheduler**. It answers three independent questions:

```text
1. Which gateway/request should be served?
2. Which applications need to be resident to serve it?
3. Which resident applications can be removed to make room?
```

This handles the requirement that multiple gateways can point to one application
**and** that multiple applications may coexist.

Resulting data flow:

```text
                   REQUEST DEMAND
                         │
                         ▼
                 ┌───────────────┐
                 │    Gateway    │
                 │    Scoring    │
                 └───────┬───────┘
                         │
                         ▼
                  Candidate Gateway
                         │
                         ▼
                  Candidate App
                         │
                         ▼
                ┌─────────────────┐
                │ Resource Check  │
                └───────┬─────────┘
                        │
              ┌─────────┴─────────┐
              │                   │
             FIT              DOESN'T FIT
              │                   │
              ▼                   ▼
        Keep/Load App       Select Apps to
                            Drain/Unload
                                  │
                                  ▼
                              Free VRAM
                                  │
                                  ▼
                             Load Candidate
                                  │
                                  ▼
                              Serve Queue
```

Consequences of this decision:

- **Active ≠ resident.** An application can be resident (model in VRAM) while idle;
  a request for it then needs no load. Multiple applications can be active
  simultaneously when VRAM permits.
- **Gateway demand vs application residency.** The scheduler operates on *gateway
  demand*; resource management operates on *application residency*. When
  `G1 → A1`, `G2 → A1`, `G3 → A2`, the scheduler never loads A1 twice:
  `application_demand(A1) = demand(G1) + demand(G2)`.
- **Coexistence is the default path**, not a special case: if the candidate fits
  alongside current residents, it is simply loaded — no switching occurs.
- **Future-proof**: on larger GPUs many LLMs/TTS/image models can stay resident
  simultaneously instead of treating the GPU as a single-application slot.

## 4. Scheduling Model

Event-driven scheduling **plus** a 1-second reconciliation loop.

- Events (new request, request completion, load/unload completion, queue timeout,
  resource change, idle timeout, manual Process Now) wake the scheduler immediately.
- The periodic loop catches stale states, GPU changes, dead processes, and missed
  events.
- Only **one resource transition transaction at a time** (`scheduler_lock` /
  transactional state machine). See [07 §7](07-resource-manager.md#7-concurrency--locking).

## 5. Recommended Service Layout

```text
gpu-symbiosis/
│
├── api/
│   ├── applications
│   ├── gateways
│   ├── requests
│   └── system
│
├── gateway/
│   ├── proxy
│   ├── queue
│   └── request_manager
│
├── scheduler/
│   ├── scheduler
│   ├── scoring
│   ├── switching
│   └── residency
│
├── applications/
│   ├── manager
│   ├── lifecycle
│   ├── handlers
│   └── process_monitor
│
├── resources/
│   ├── gpu
│   ├── cpu
│   └── memory
│
├── metrics/
│   ├── collector
│   ├── aggregator
│   └── storage
│
├── database/
│
├── web/
│
└── config/
```

Implementation language and process model (single process vs. split services) is an
implementation choice; the module boundaries above are the contract. All modules run
inside the control plane on the GPU host.
