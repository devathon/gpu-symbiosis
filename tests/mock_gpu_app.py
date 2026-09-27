"""Mock GPU application for E2E tests.

Simulates an AI app that owns GPU memory:
- /health      — process alive
- /ready       — 200 only when the model is loaded
- /load        — "load the model" (sets loaded=True, optional delay)
- /unload      — "unload the model"
- /generate    — streaming response (SSE-style), tracked as an active request
- /state       — current state (loaded, active)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import StreamingResponse

app = FastAPI()
STATE = {
    "loaded": False,
    "active": 0,
    "name": "mock",
    "chunks_done": 0,
    # cumulative token / request metrics (spec 08 §5)
    "input_tokens": 0,
    "output_tokens": 0,
    "requests": 0,
    "completed_requests": 0,
    "failed_requests": 0,
}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    if not STATE["loaded"]:
        raise HTTPException(503, "model not loaded")
    return {"ready": True}


@app.post("/load")
async def load():
    if not STATE["loaded"]:
        STATE["loaded"] = True
    return {"loaded": True}


@app.post("/unload")
async def unload():
    STATE["loaded"] = False
    return {"loaded": False}


@app.get("/state")
def state():
    return STATE


@app.websocket("/ws")
async def ws_echo(ws: WebSocket):
    """WebSocket echo: replies 'ws-echo:<msg>' to each text message (Phase 5 WS)."""
    await ws.accept()
    while True:
        msg = await ws.receive_text()
        await ws.send_text(f"ws-echo:{msg}")


@app.post("/generate")
async def generate(body: dict):
    if not STATE["loaded"]:
        raise HTTPException(503, "model not loaded")
    chunks = int(body.get("chunks", 3))
    delay = float(body.get("delay", 0.1))
    name = STATE["name"]

    # Simulate token counts: prompt tokens from the request, one token per chunk.
    input_tokens = int(body.get("input_tokens", 16))
    STATE["input_tokens"] += input_tokens
    STATE["output_tokens"] += chunks
    STATE["requests"] += 1
    STATE["completed_requests"] += 1

    # Log llama.cpp-format token lines to stdout (the app's stdout is the log,
    # spec 08 §5) so the metrics handler can tail them like a real llama-server.
    prompt_ms = input_tokens * 12.35
    eval_ms = max(chunks, 1) * 28.39
    total_ms = prompt_ms + eval_ms
    print(
        f"llama_perf_context_print: prompt eval time = {prompt_ms:10.2f} ms / "
        f"{input_tokens:5d} tokens ({prompt_ms / input_tokens:8.2f} ms per token, "
        f"{1000.0 / prompt_ms * input_tokens:8.2f} tokens per second)"
    )
    print(
        f"llama_perf_context_print:        eval time = {eval_ms:10.2f} ms / "
        f"{chunks:5d} runs   ({eval_ms / max(chunks, 1):8.2f} ms per token, "
        f"{1000.0 / eval_ms * chunks:8.2f} tokens per second)"
    )
    print(f"llama_perf_context_print:       total time = {total_ms:10.2f} ms / {input_tokens + chunks:5d} tokens")
    sys.stdout.flush()

    async def gen():
        STATE["active"] += 1
        try:
            for i in range(chunks):
                STATE["chunks_done"] += 1
                yield f"event: chunk\ndata: {json.dumps({'app': name, 'chunk': i})}\n\n"
                if i < chunks - 1:
                    await asyncio.sleep(delay)
        finally:
            STATE["active"] -= 1

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"X-Input-Tokens": str(input_tokens), "X-Output-Tokens": str(chunks)},
    )


@app.get("/stats")
def stats():
    return {
        "loaded": STATE["loaded"],
        "active": STATE["active"],
        "requests": STATE["requests"],
        "completed_requests": STATE["completed_requests"],
        "failed_requests": STATE["failed_requests"],
        "input_tokens": STATE["input_tokens"],
        "output_tokens": STATE["output_tokens"],
        "total_tokens": STATE["input_tokens"] + STATE["output_tokens"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", default="mock")
    parser.add_argument("--start-loaded", action="store_true")
    args = parser.parse_args()
    STATE["name"] = args.name
    STATE["loaded"] = args.start_loaded
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
