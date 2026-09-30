# Application-Type Plugins

GPUSymbiosis treats every GPU application as an **application type** (spec 04 §8).
A type is a pair:

- a **`TypeSpec`** — *what* is recorded and displayed (a list of `MetricDef` fields),
- a **handler factory** — *how* those values are fetched each tick.

The registry holds **no type-specific code**. Each type lives in its own module and
registers itself against a small contract. Shipped types (`llama`, `ollama`,
`comfyui`, `tts`) and your drop-in plugins use the **exact same contract**.

The full contract is defined in [`gpusybio/metrics/type_registry.py`](../gpusybio/metrics/type_registry.py)
and the metric data model in [`gpusybio/metrics/typespec.py`](../gpusybio/metrics/typespec.py).

## 1. The contract

A plugin module must expose **one** of:

```python
def register(registry) -> None: ...      # preferred — explicit
# or
PLUGIN = TypePlugin("mytype", MY_SPEC, MyHandler)
```

Both are applied by the loader (`_apply_contract`); a module with neither is logged
and skipped.

### `TypeSpec` / `MetricDef`

```python
from gpusybio.metrics.typespec import TypeSpec, MetricDef

MetricDef(
    id="tokens_per_sec",   # stable key the UI + aggregator use
    name="Tokens t/s",      # human label
    kind="rate",            # gauge | rate | counter | histogram | text
    unit="t/s",
    source="handler",       # sample | handler | config | computed
    display="card",         # card | chart | table | text
    group="rates",          # the card section the UI renders it under
    collect={"field": "tokens_per_sec"},  # handler fetch mapping
    aggregation="avg",      # avg | sum | max | min | last | percentile_95
)

MY_SPEC = TypeSpec("mytype", [MetricDef(...), ...])
```

The UI auto-renders from the spec — there is no per-type UI code. `kind`, `display`,
`group`, and `aggregation` are what the dashboard keys off, so choose them to match
the existing types (`llama`, `tts`) for a consistent layout.

### Handler factory (unified signature)

```python
def MyHandler(resources, states, request_metrics, base_urls, session=None, log_paths=None):
    """Return a handler object."""
    ...
```

- `resources` — the resource monitor (per-app GPU/CPU readings).
- `states` — the state manager (process/residency state per app).
- `request_metrics` — request-completion metrics (tokens, latency).
- `base_urls` — the application's endpoint(s) to fetch from.
- `session` / `log_paths` — optional HTTP session and log file(s) to tail.

### Required vs. optional methods

| Method | Required? | Purpose |
| --- | --- | --- |
| `handler.collect_metrics(app_id)` | **yes** | per-tick values for your spec's `handler`/`computed` fields |
| `handler.get_request_metrics(...)` | no | request-completion metrics (tokens/sec, etc.) |
| `handler.detect_activity(...)` | no | interpret GPU usage as "active" for a no-gateway app |
| `handler.tailer(app_id)` | no | lifetime tailer: any object with `read()`/`state()`/`seed()` |
| `spec.should_schedule(method, path)` | no | which requests are scheduled vs. pass through (default: all) |

`collect_metrics` is the heart of the plugin. The collector calls it once per second
per resident app; return a dict keyed by the `id` of each field you declared with
`source="handler"` or `source="computed"`.

## 2. A complete drop-in plugin

Drop a file into **`<data_path>/plugins/mytype.py`** (the data path is the directory
of your `config.yaml`). It is loaded at startup, after the shipped types:

```python
"""My custom GPU application type."""
from __future__ import annotations

from gpusybio.metrics.type_registry import TypePlugin
from gpusybio.metrics.typespec import MetricDef, TypeSpec

MY_SPEC = TypeSpec("mytype", [
    MetricDef("requests", "Total Requests", "counter", "", "handler", "card",
              "requests", aggregation="sum"),
    MetricDef("queue_len", "Queue", "gauge", "", "handler", "card", "status"),
])


def _make_handler(resources, states, request_metrics, base_urls, session=None, log_paths=None):
    url = base_urls[0] if base_urls else None
    return MyHandler(url, session)


class MyHandler:
    def __init__(self, url, session):
        self._url = url
        self._session = session
        self._total = 0

    async def collect_metrics(self, app_id):
        # Fetch your app's /v1/status (or whatever it exposes) once per tick.
        if self._session is None or self._url is None:
            return {"requests": 0, "queue_len": 0}
        try:
            async with self._session.get(f"{self._url}/v1/status") as r:
                data = await r.json()
            self._total += data.get("requests", 0)
            return {"requests": self._total, "queue_len": data.get("queue", 0)}
        except Exception:  # noqa: BLE001 — a failed tick must never stop the loop
            return {"requests": self._total, "queue_len": 0}


# Preferred: an explicit register() (either form is accepted).
def register(registry) -> None:
    registry.register(TypePlugin("mytype", MY_SPEC, _make_handler))
```

Then reference the type in your config (spec 10 §6):

```yaml
applications:
  - id: my-app
    name: My App
    application_type: mytype        # ← your plugin's type name
    process_name: my-app
    start_script: ...
    stop_script: ...
    port: 12345
```

## 3. Loading order & fallback

Loading is **first-registered name wins** (`make_type_registry`):

```text
1. generic fallback   — always registered first; nothing can shadow it
2. shipped types      — gpusybio/application_types/<type>.py
3. drop-in plugins    — <data_path>/plugins/<name>.py
```

Consequences:

- A drop-in named `generic` is ignored (the fallback is registered first).
- A module that fails to import is **logged and skipped** — it never stops startup.
- An **unknown** `application_type` falls back to the `generic` handler + spec, so
  every application keeps working (spec 04 §8.1).

## 4. The `custom` type

You can declare metrics for an application *without* writing any handler code using
the built-in `custom` type (spec 08 §9.6). Its metric list is read from the
application's config — a dedicated `metrics` column, editable via the API:

```yaml
applications:
  - id: my-app
    application_type: custom
    metrics:
      - id: queue_len
        name: Queue
        unit: ""
        kind: gauge
        source: handler
        display: card
        collect: { field: queue_len }
```

`custom` uses the generic handler for fetching; your `collect` mapping tells it which
of the generic/handler values to surface under your declared ids.

## 5. Testing a plugin

Type plugins are exercised through the same harness as the built-in types — see
`tests/test_type_plugins.py`. Add a test that builds a registry via
`make_type_registry(<tmp data path with your plugin>)` and asserts your spec +
handler behave as expected:

```bash
uv run pytest tests/test_type_plugins.py -q
```

The full suite runs with the test ports/env from `pyproject`'s `[tool.pytest.ini_options]`
and the `GSYBIO_*` port overrides the e2e tests expect:

```bash
GSYBIO_A1_PORT=9571 GSYBIO_A2_PORT=9572 GSYBIO_A1_GW=9573 GSYBIO_A2_GW=9574 \
GSYBIO_ADMIN_PORT=9575 uv run pytest tests/ -q
```

## 6. Conventions

- **Never block the event loop** in `collect_metrics` — use `async` HTTP (the session
  is `aiohttp`). A slow tick must not stall proxy streaming (spec 16 §3.3).
- **Fail soft** — a per-tick exception is swallowed (return your last-known or a zero);
  the collector keeps running.
- **Declare `kind`/`display`/`group`/`aggregation` deliberately** so the dashboard
  renders your fields consistently with the shipped types.
- Keep the module import-light: it is imported at startup and a failure is skipped.
