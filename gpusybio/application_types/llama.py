"""The ``llama`` application type (spec 04 §8, 08 §5): everything
llama-related in one module — the TypeSpec (what is recorded/displayed), the
handler (how it is fetched), and the log tailer that accumulates the lifetime
token metrics.

The tailer tails an application log (the app's stdout) for llama.cpp
per-request token metrics. A real ``llama-server`` exposes no cumulative token
totals over HTTP, but it logs each request. The app's stdout is the log (the
start script redirects it to ``<data_path>/logs/<id>.log``). The lines parsed (``llama_perf_context_print``
and slot selection):

    prompt eval time = 1234.56 ms / 100 tokens ( 12.35 ms per token, 81.00 tokens per second)
           eval time = 5678.90 ms / 200 runs   ( 28.39 ms per token, 35.22 tokens per second)
          total time = 6913.46 ms / 300 tokens
    draft acceptance = 0.40944 (  642 accepted /  1568 generated), mean len =  3.87
    selected slot by LCP similarity, f_sim_best = 0.996 (> 0.100 thold), f_keep = 0.997
    stop processing: n_tokens = 62189, truncated = 0

The prompt line only counts the tokens actually evaluated — when the slot
serves the prompt from the KV cache, llama.cpp batches just the suffix after
the cached prefix (or nothing at all for a fully cached prompt, in which case
no prompt line is emitted). The ``stop processing`` line logs the slot's full
context after the request (retained prefix + evaluated + generated), so the
cached input is ``n_tokens - evaluated - generated`` for that request —
scoped per slot id, so interleaved slots (``--parallel N``) attribute
correctly. Lines with ``truncated = 1`` under-count and are skipped.

The tailer tracks a read offset and accumulates LIFETIME totals that persist
across restarts: the lifetime + offset + a session fingerprint are stored
(spec 08 §5.1). The log is truncated on every app start (``>log 2>&1``), so a
session is identified by its first 64 bytes — when they change, the app has
restarted: the lifetime is kept and the fresh session is read from the start.
"""

from __future__ import annotations

import os
import re
import time

import aiohttp

from gpusybio.applications.lifecycle import ProcessState, ResidencyState
from gpusybio.metrics.requests import RequestMetrics
from gpusybio.metrics.type_registry import TypePlugin
from gpusybio.metrics.typespec import MetricDef, TypeSpec

# prompt eval time = 1234.56 ms / 100 tokens ( 12.35 ms per token, 81.00 tokens per second)
_PROMPT_RE = re.compile(
    r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d.]+)\s*tokens per second"
)
#        eval time = 5678.90 ms / 200 runs   ( 28.39 ms per token, 35.22 tokens per second)
_EVAL_RE = re.compile(
    r"(?<!prompt )\beval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*\w+.*?([\d.]+)\s*tokens per second"
)
#      total time = 6913.46 ms / 300 tokens
_TOTAL_RE = re.compile(r"total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens")
#    draft acceptance = 0.40944 (  642 accepted /  1568 generated), mean len =  3.87
_DRAFT_RE = re.compile(
    r"draft acceptance\s*=\s*([\d.]+)\s*\(\s*(\d+)\s*accepted\s*/\s*(\d+)\s*generated\)"
    r".*?mean len\s*=\s*([\d.]+)"
)
# Slot selection: one per request (request-based cache statistic).
_SLOT_RE = re.compile(r"I slot get_availabl")
# The slot was selected by LCP (prefix) similarity — the prompt was largely
# served from the KV cache (request-based cache statistic).
_LCP_RE = re.compile(r"selected slot by LCP similarity")
# stop processing: n_tokens = 62189, truncated = 0
# The slot's full context after the request: retained (cached) prefix +
# evaluated + generated. The source for the cached-input total.
_STOP_RE = re.compile(r"stop processing:\s*n_tokens\s*=\s*(\d+)\s*,\s*truncated\s*=\s*(\d+)")
# Slot id for per-request attribution: "I slot print_timing: id  1 | ..." —
# slots interleave when --parallel N > 1, so the per-request counts are kept
# per slot id. Lines without a slot prefix share the "" bucket.
_SLOT_ID_RE = re.compile(r"I slot\s+\w+:\s*id\s*(\d+)\s*\|")

_FINGERPRINT_BYTES = 64


class LlamaLogMetrics:
    """Offset-tracked, session-aware tailer for one application log."""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self._offset = 0
        self._buffer = b""
        self._fingerprint = b""  # first 64 bytes of the current session
        # lifetime accumulators (persist across restarts)
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._total_time_ms = 0.0
        # prompt/eval phase times (ms); the rates are the throughput-weighted
        # sum(tokens) / sum(time) — consistent with tokens_per_sec
        self._prompt_time_ms = 0.0
        self._eval_time_ms = 0.0
        self._requests = 0
        self._draft_accepted = 0
        self._draft_generated = 0
        self._draft_mean_len_weighted = 0.0
        self._slot_selections = 0
        self._cache_hit_requests = 0
        # Cached input: lifetime tokens of prompts served from the KV cache
        # (stop line n_tokens minus this request's evaluated + generated).
        self._cached_tokens = 0
        # Per-request evaluated/generated counts, keyed by slot id (cleared on
        # the request's stop line) — the stop line has no prompt/eval numbers.
        self._req_input: dict[str, int] = {}
        self._req_output: dict[str, int] = {}

    # -- persistence (spec 08 §5.1) ------------------------------------------
    def seed(self, state: dict) -> None:
        """Load a persisted lifetime + offset + fingerprint (server restart)."""
        self._input_tokens = int(state.get("input_tokens", 0))
        self._output_tokens = int(state.get("output_tokens", 0))
        self._total_tokens = int(state.get("total_tokens", 0))
        self._total_time_ms = float(state.get("total_time_ms", 0.0))
        self._prompt_time_ms = float(state.get("prompt_time_ms", 0.0))
        self._eval_time_ms = float(state.get("eval_time_ms", 0.0))
        self._requests = int(state.get("requests", 0))
        self._draft_accepted = int(state.get("draft_accepted", 0))
        self._draft_generated = int(state.get("draft_generated", 0))
        self._draft_mean_len_weighted = float(state.get("draft_mean_len_weighted", 0.0))
        self._slot_selections = int(state.get("slot_selections", 0))
        self._cache_hit_requests = int(state.get("cache_hit_requests", 0))
        self._cached_tokens = int(state.get("cached_tokens", 0))
        self._offset = int(state.get("log_offset", 0))
        fp = state.get("log_fingerprint", b"")
        self._fingerprint = fp if isinstance(fp, bytes) else bytes(fp)
        self._buffer = b""

    def state(self) -> dict:
        """The persistable lifetime + offset + fingerprint."""
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "total_tokens": self._total_tokens,
            "total_time_ms": self._total_time_ms,
            "prompt_time_ms": self._prompt_time_ms,
            "eval_time_ms": self._eval_time_ms,
            "requests": self._requests,
            "draft_accepted": self._draft_accepted,
            "draft_generated": self._draft_generated,
            "draft_mean_len_weighted": self._draft_mean_len_weighted,
            "slot_selections": self._slot_selections,
            "cache_hit_requests": self._cache_hit_requests,
            "cached_tokens": self._cached_tokens,
            "log_offset": self._offset,
            "log_fingerprint": self._fingerprint,
        }

    def read(self) -> dict:
        """Read new lines since the offset, update lifetime totals, return a snapshot."""
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return self._snapshot(stale=True)  # log not (yet) present

        # Session detection: the log's first 64 bytes identify the session. The
        # log is truncated on every app start, so a changed head means a fresh
        # session — keep the lifetime, re-read the new content from the start.
        head = self._read_head()
        if head:
            if not self._fingerprint:
                self._fingerprint = head
            elif head != self._fingerprint:
                self._offset = 0
                self._buffer = b""
                self._fingerprint = head
        if size < self._offset:
            # Truncated within the same session: keep the lifetime, re-read from 0.
            self._offset = 0
            self._buffer = b""
        if size == self._offset and not self._buffer:
            return self._snapshot()

        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
            self._offset = size
        except OSError:
            return self._snapshot(stale=True)

        self._buffer += data
        idx = self._buffer.rfind(b"\n")
        if idx == -1:
            return self._snapshot()  # no complete line yet
        complete, self._buffer = self._buffer[:idx], self._buffer[idx + 1:]
        for line in complete.split(b"\n"):
            if line:
                self._consume(line.decode(errors="replace"))
        return self._snapshot()

    def _read_head(self, n: int = _FINGERPRINT_BYTES) -> bytes:
        try:
            with open(self.log_path, "rb") as f:
                return f.read(n)
        except OSError:
            return b""

    def _consume(self, line: str) -> None:
        m = _SLOT_ID_RE.search(line)
        sid = m.group(1) if m else ""
        m = _PROMPT_RE.search(line)
        if m:
            self._input_tokens += int(m.group(2))
            self._prompt_time_ms += float(m.group(1))
            self._req_input[sid] = int(m.group(2))
        m = _EVAL_RE.search(line)
        if m:
            self._output_tokens += int(m.group(2))
            self._eval_time_ms += float(m.group(1))
            self._req_output[sid] = int(m.group(2))
        m = _TOTAL_RE.search(line)
        if m:
            self._total_time_ms += float(m.group(1))
            self._total_tokens += int(m.group(2))
            self._requests += 1
        m = _DRAFT_RE.search(line)
        if m:
            self._draft_accepted += int(m.group(2))
            self._draft_generated += int(m.group(3))
            self._draft_mean_len_weighted += float(m.group(4)) * int(m.group(3))
        if _SLOT_RE.search(line):
            self._slot_selections += 1
        if _LCP_RE.search(line):
            self._cache_hit_requests += 1
        m = _STOP_RE.search(line)
        if m:
            # Full context after the request = retained (cached) prefix +
            # evaluated + generated for THIS request. A fully cached prompt
            # emitted no prompt line (evaluated = 0), which is why the stop
            # line is the only reliable cached-input source.
            if m.group(2) == "0":
                self._cached_tokens += max(
                    0, int(m.group(1)) - self._req_input.get(sid, 0)
                    - self._req_output.get(sid, 0)
                )
            self._req_input.pop(sid, None)
            self._req_output.pop(sid, None)

    def reset(self) -> None:
        """Start a fresh lifetime (explicit; NOT called on app restart — the
        lifetime is kept and only the offset/fingerprint are refreshed)."""
        self._offset = 0
        self._buffer = b""
        self._fingerprint = b""
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._total_time_ms = 0.0
        self._prompt_time_ms = 0.0
        self._eval_time_ms = 0.0
        self._requests = 0
        self._draft_accepted = 0
        self._draft_generated = 0
        self._draft_mean_len_weighted = 0.0
        self._slot_selections = 0
        self._cache_hit_requests = 0
        self._cached_tokens = 0
        self._req_input = {}
        self._req_output = {}

    def _snapshot(self, stale: bool = False) -> dict:
        total_tokens = self._total_tokens or (self._input_tokens + self._output_tokens)
        # Throughput-weighted rates: sum(tokens) / sum(phase time) — a large
        # prompt weighs by its work, not as one vote per request.
        prompt_tps = (self._input_tokens / (self._prompt_time_ms / 1000.0)) if self._prompt_time_ms else 0.0
        eval_tps = (self._output_tokens / (self._eval_time_ms / 1000.0)) if self._eval_time_ms else 0.0
        total_tps = (total_tokens / (self._total_time_ms / 1000.0)) if self._total_time_ms else 0.0
        draft_acceptance = (self._draft_accepted / self._draft_generated) if self._draft_generated else 0.0
        draft_mean_len = (
            self._draft_mean_len_weighted / self._draft_generated
        ) if self._draft_generated else 0.0
        # Token-based: cached input as a share of all input (cached +
        # newly-evaluated).
        cache_denom = self._cached_tokens + self._input_tokens
        cache_hit_pct = (self._cached_tokens / cache_denom * 100.0) if cache_denom else 0.0
        return {
            "input_tokens": self._input_tokens,
            "cached_tokens": self._cached_tokens,
            "output_tokens": self._output_tokens,
            "total_tokens": total_tokens,
            "prompt_tokens_per_sec": prompt_tps,
            "generation_tokens_per_sec": eval_tps,
            "tokens_per_sec": total_tps,
            "requests": self._requests,
            "completed_requests": self._requests,
            # Percentages (0-100) so the UI shows the number directly.
            "draft_acceptance": round(draft_acceptance * 100.0, 1),
            "draft_mean_len": round(draft_mean_len, 2),
            "cache_hit_pct": round(cache_hit_pct, 1),
            "stale": stale,
        }


# -- type spec (spec 08 §5) ------------------------------------------------------

LLAMA_SPEC = TypeSpec("llama", [
    MetricDef("input_tokens", "Input tokens", "counter", "", "handler", "card", "tokens"),
    MetricDef("cached_tokens", "Cached Input", "counter", "", "handler", "card", "tokens"),
    MetricDef("output_tokens", "Output tokens", "counter", "", "handler", "card", "tokens"),
    MetricDef("cache_hit_pct", "Cache Hit %", "gauge", "", "handler", "card", "cache",
              aggregation="avg"),
    MetricDef("prompt_tokens_per_sec", "Prompt t/s", "rate", "", "handler", "card", "rates",
              aggregation="avg"),
    MetricDef("generation_tokens_per_sec", "Generation t/s", "rate", "", "handler", "card",
              "rates", aggregation="avg"),
    MetricDef("draft_acceptance", "Draft acceptance", "gauge", "", "handler", "card",
              "speculative", aggregation="avg"),
    MetricDef("requests", "Total Requests", "counter", "", "handler", "card", "requests",
              aggregation="sum"),
])


# -- handler (spec 04 §8) --------------------------------------------------------

class LlamaHandler:
    """Token metrics from the app's log (spec 08 §5); /stats + request-tracked
    as fallbacks.

    A real ``llama-server`` exposes no cumulative token totals over HTTP, but it
    logs each request. The app's stdout is the log (``<data_path>/logs/<id>.log``),
    so the handler tails it. Apps that do expose ``/stats`` (e.g. the mock) still
    work: the first non-zero source wins (log, then /stats, then request-tracked).
    """

    _TOKEN_KEYS = (
        "total_tokens", "input_tokens", "cached_tokens", "output_tokens", "requests",
        "completed_requests",
        "tokens_per_sec", "prompt_tokens_per_sec", "generation_tokens_per_sec",
        "draft_acceptance", "draft_mean_len", "cache_hit_pct",
    )

    def __init__(self, resources, states, request_metrics: RequestMetrics,
                 base_urls: dict[str, str], session: aiohttp.ClientSession | None = None,
                 log_paths: dict[str, str] | None = None) -> None:
        self._resources = resources
        self._states = states
        self._rm = request_metrics
        self._base = base_urls  # app_id -> http://127.0.0.1:<port>
        self._session = session
        self._last: dict[str, tuple[dict, float]] = {}
        # app_id -> tailer of its log (the app's stdout)
        self._tailers = {aid: LlamaLogMetrics(path) for aid, path in (log_paths or {}).items()}

    def _log_metrics(self, app_id: str) -> dict:
        t = self._tailers.get(app_id)
        return t.read() if t is not None else {}

    def tailer(self, app_id: str) -> LlamaLogMetrics | None:
        """The app's log tailer (for lifetime seed/persist), or None."""
        return self._tailers.get(app_id)

    async def _stats(self, app_id: str) -> dict:
        url = self._base.get(app_id)
        if not url or self._session is None:
            return {}
        try:
            async with self._session.get(f"{url}/stats", timeout=aiohttp.ClientTimeout(total=2)) as r:
                cur = await r.json() if r.status == 200 else {}
        except Exception:  # noqa: BLE001 - handler must never break sampling
            cur = {}
        if not cur:
            return {}
        now = time.time()
        prev = self._last.get(app_id)
        self._last[app_id] = (cur, now)
        out = {
            "total_tokens": cur.get("total_tokens", cur.get("input_tokens", 0) + cur.get("output_tokens", 0)),
            "input_tokens": cur.get("input_tokens", 0),
            "output_tokens": cur.get("output_tokens", 0),
            "requests": cur.get("requests", 0),
            "completed_requests": cur.get("completed_requests", 0),
        }
        # Rates from deltas (spec 08 §5: tokens/sec, prompt/generation speed).
        if prev is not None:
            pt, pt_at = prev
            dt = max(1e-6, now - pt_at)
            d_out = max(0, cur.get("output_tokens", 0) - pt.get("output_tokens", 0))
            d_in = max(0, cur.get("input_tokens", 0) - pt.get("input_tokens", 0))
            out["tokens_per_sec"] = round((d_in + d_out) / dt, 2)
            out["generation_tokens_per_sec"] = round(d_out / dt, 2)
            out["prompt_tokens_per_sec"] = round(d_in / dt, 2)
        return out

    async def collect_metrics(self, app_id: str) -> dict:
        # Log-derived (primary for real llama-server), then /stats, then request-tracked.
        sources = (
            self._log_metrics(app_id),
            await self._stats(app_id),
            {
                "total_tokens": self._rm.app(app_id).get("total_tokens", 0),
                "input_tokens": self._rm.app(app_id).get("input_tokens", 0),
                "output_tokens": self._rm.app(app_id).get("output_tokens", 0),
            },
        )
        out: dict = {}
        for key in self._TOKEN_KEYS:
            for src in sources:
                v = src.get(key)
                if v:
                    out[key] = v
                    break
            else:
                out[key] = 0
        return out

    def get_request_metrics(self, app_id: str, active: int = 0, queued: int = 0) -> dict:
        return self._rm.app(app_id, active, queued)

    def detect_activity(self, app_id: str) -> bool:
        st = self._states.get(app_id)
        if st.active_requests > 0:
            return True
        return (
            st.process is ProcessState.STARTED
            and st.residency is ResidencyState.LOADED
            and self._resources.backend.process_utilization_pct(st.pid)
            > float(self._resources.gpu_activity_threshold_pct)
        )


def register(registry) -> None:
    registry.register(TypePlugin("llama", LLAMA_SPEC, LlamaHandler))
