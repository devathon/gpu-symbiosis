"""The ``ollama`` application type (spec 04 §8): Ollama logs the same
llama.cpp per-request lines, so its handler is the llama module's — reused by
explicit import, not duplicated.
"""

from __future__ import annotations

from gpusybio.application_types.llama import LLAMA_SPEC, LlamaHandler
from gpusybio.metrics.type_registry import TypePlugin
from gpusybio.metrics.typespec import TypeSpec

# Ollama logs the identical llama.cpp per-request lines, so it gets the same
# token dashboard (input / cached input / output, cache hit %, prompt &
# generation rates, draft stats, requests) — same spec as ``llama``, built
# from it so the two never drift apart.
OLLAMA_SPEC = TypeSpec("ollama", list(LLAMA_SPEC.metrics))


def register(registry) -> None:
    registry.register(TypePlugin("ollama", OLLAMA_SPEC, LlamaHandler))
