"""Tail an application log (the app's stdout) for llama.cpp per-request token
metrics (spec 08 §5).

A real ``llama-server`` exposes no cumulative token totals over HTTP, but it
logs each request. The app's stdout is the log (the start script redirects it
to ``<data_path>/logs/<id>.log``). The lines to parse (llama.cpp
``llama_perf_context_print``):

    prompt eval time = 1234.56 ms / 100 tokens ( 12.35 ms per token, 81.00 tokens per second)
           eval time = 5678.90 ms / 200 runs   ( 28.39 ms per token, 35.22 tokens per second)
          total time = 6913.46 ms / 300 tokens

The tailer tracks a read offset, is rotation-safe (file shrank -> fresh
lifetime), and accumulates lifetime totals: input/output tokens, the average
prompt and generation tokens-per-second, and total time/tokens.
"""

from __future__ import annotations

import os
import re

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


class LlamaLogMetrics:
    """Offset-tracked, rotation-safe tailer for one application log."""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self._offset = 0
        self._buffer = b""
        # lifetime accumulators
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._total_time_ms = 0.0
        self._prompt_tps_sum = 0.0
        self._prompt_tps_n = 0
        self._eval_tps_sum = 0.0
        self._eval_tps_n = 0
        self._requests = 0

    def read(self) -> dict:
        """Read new lines since the offset, update lifetime totals, return a snapshot."""
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return self._snapshot(stale=True)  # log not (yet) present
        if size < self._offset:
            self.reset()  # rotated / truncated: fresh lifetime
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

    def _consume(self, line: str) -> None:
        m = _PROMPT_RE.search(line)
        if m:
            self._input_tokens += int(m.group(2))
            self._prompt_tps_sum += float(m.group(3))
            self._prompt_tps_n += 1
        m = _EVAL_RE.search(line)
        if m:
            self._output_tokens += int(m.group(2))
            self._eval_tps_sum += float(m.group(3))
            self._eval_tps_n += 1
        m = _TOTAL_RE.search(line)
        if m:
            self._total_time_ms += float(m.group(1))
            self._total_tokens += int(m.group(2))
            self._requests += 1

    def reset(self) -> None:
        """Start a fresh lifetime (app start / log truncation)."""
        self._offset = 0
        self._buffer = b""
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._total_time_ms = 0.0
        self._prompt_tps_sum = 0.0
        self._prompt_tps_n = 0
        self._eval_tps_sum = 0.0
        self._eval_tps_n = 0
        self._requests = 0

    def _snapshot(self, stale: bool = False) -> dict:
        total_tokens = self._total_tokens or (self._input_tokens + self._output_tokens)
        prompt_tps = (self._prompt_tps_sum / self._prompt_tps_n) if self._prompt_tps_n else 0.0
        eval_tps = (self._eval_tps_sum / self._eval_tps_n) if self._eval_tps_n else 0.0
        total_tps = (total_tokens / (self._total_time_ms / 1000.0)) if self._total_time_ms else 0.0
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "total_tokens": total_tokens,
            "prompt_tokens_per_sec": prompt_tps,
            "generation_tokens_per_sec": eval_tps,
            "tokens_per_sec": total_tps,
            "requests": self._requests,
            "completed_requests": self._requests,
            "stale": stale,
        }
