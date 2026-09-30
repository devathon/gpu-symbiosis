# Contributing to GPUSymbiosis

Thanks for helping. This repo is **spec-first**: the `spec/` directory is the
authoritative description of behaviour, and the implementation + tests follow it.
Read the relevant spec module *before* changing behaviour, and update it *with* your
change.

## Prerequisites

- **Python 3.13+**
- **uv** (dependency + environment manager; the repo ships `uv.lock`)
- **Node 18+** (only for the Web UI)
- An NVIDIA GPU with the driver installed for real NVML monitoring; tests and dev
  mode run against a **fake GPU backend**, so no GPU is required to develop.

## Setup

```bash
uv sync                       # install the locked environment
uv run python -m compileall gpusybio   # optional: sanity-compile
```

## Running (dev)

```bash
# Orchestrator with the dev config (mock GPU apps):
uv run python devrun.py
# or with an explicit config:
uv run python -m gpusybio.server --config config.yaml

# Web UI (optional):
cd web && npm install
npm run build                 # build assets → served by the admin API at http://127.0.0.1:9530/
# or, for dev: npm run dev → http://localhost:5173 (proxies /api to :9530)
```

## Testing

The test suite uses a **fake GPU backend** and real HTTP endpoints on fixed loopback
ports (the e2e tests need the `GSYBIO_*` port overrides):

```bash
# Core loop + invariants:
uv run pytest

# Full suite (what CI / reviewers run):
GSYBIO_A1_PORT=9571 GSYBIO_A2_PORT=9572 GSYBIO_A1_GW=9573 GSYBIO_A2_GW=9574 \
GSYBIO_ADMIN_PORT=9575 uv run pytest tests/ -q

# The tech-stack spikes (spec 16 §4):
cd spikes/phase1 && uv run pytest
```

**Always run the full suite before submitting.** The scheduler is event-driven with a
1-second reconciliation loop and is phase-sensitive — the e2e core-loop tests are the
guard against scheduling regressions.

## Code conventions

Follow the patterns already in the codebase; the spec modules are the reference.

- **Spec-first.** Every module maps to a spec doc (`spec/NN-*.md`). Cite the spec § in
  comments where behaviour is non-obvious. Change the spec and the code together.
- **Single process, one event loop.** All state (queues, reservations, the scheduler
  lock) is in-process by design (spec 16 §3.1). Do **not** introduce background
  threading that touches shared state, and never run the server multi-worker.
- **Async, non-blocking.** Heavy work (metrics aggregation, script execution) stays
  off the event loop (`asyncio.create_subprocess_exec`, batching in a worker).
- **SQLite is the source of truth** after startup (spec 09 §8). YAML/TOML is seed
  only. Add new persisted columns to the model and let `init_db` auto-`ALTER` them —
  there is no alembic; `_migrate_columns` adds missing columns on startup.
- **Naive-UTC datetimes.** The DB stores naive datetimes read as UTC (spec 04 §9).
  Use `gpusybio.database.db.dt_naive()` to convert an epoch to a DB datetime; do not
  reinvent the helper.
- **Event bus for decoupled signalling.** Lifecycle/scheduler effects are published on
  the bus (`gpusybio/events/bus.py`) and fanned out to SSE; do not call consumers
  directly where a bus event already exists.
- **Invariants are load-bearing.** The invariants in `spec/14-invariants.md`
  (no request ever interrupted mid-flight, no slot lost, one transition at a time, …)
  are enforced by the state manager and the scheduler. Guard them in tests.

## Adding a feature

1. **Find or extend the spec.** If the behaviour is new, add it to the right `spec/NN`
   doc first. If it's an application type, see
   [docs/application-type-plugins.md](docs/application-type-plugins.md).
2. **Implement** in the owning module (see the module table in the README's
   Architecture section).
3. **Test.** Unit-test against the fake backend / a temp SQLite; add an e2e test when
   the behaviour crosses module boundaries (gateway → scheduler → app).
4. **Run the full suite** and keep it green.
5. **Document** any new user-facing surface (README quick start, the spec's API table
   in `spec/10-api.md`).

## Commit style

Write a short imperative subject (≤ ~72 chars) and a body that explains *why*,
referencing the spec module/§ when applicable. Keep commits logical (one concern
each) so they are easy to review and squash. Example:

```
Fix queue oldest_wait_sec to the true oldest wait

oldest_wait_sec was the tail slot — the newest in the lowest-priority group —
so it under-reported the wait on multi-slot queues. Use max(waiting_sec) and
add a mixed-priority regression test (spec 05 §3).
```

## Where things live

| Area | Code | Spec |
| --- | --- | --- |
| Config + seed | `gpusybio/config/` | 10 §6, 09 §8 |
| Database (SQLite WAL) | `gpusybio/database/` | 09 |
| Event bus + SSE + logging | `gpusybio/events/` | 13, 11 §1 |
| GPU/CPU monitoring | `gpusybio/resources/` | 07 |
| App lifecycle + manager | `gpusybio/applications/` | 04 |
| Gateway + proxy + queue | `gpusybio/gateway/` | 05 |
| Scheduler + scoring | `gpusybio/scheduler/` | 06 |
| Metrics + history | `gpusybio/metrics/` | 08 |
| Admin REST API | `gpusybio/api/` | 10 |
| Composition root | `gpusybio/server.py` | 02 §5, 16 |
| Web UI | `web/` | 11 |
| Invariants | `gpusybio/state/` + `scheduler/` | 14 |
| Application-type plugins | `gpusybio/application_types/`, drop-ins | 04 §8, 08 §9 |
