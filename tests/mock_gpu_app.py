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

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI()
STATE = {"loaded": False, "active": 0, "name": "mock", "chunks_done": 0}


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


@app.post("/generate")
async def generate(body: dict):
    if not STATE["loaded"]:
        raise HTTPException(503, "model not loaded")
    chunks = int(body.get("chunks", 3))
    delay = float(body.get("delay", 0.1))
    name = STATE["name"]

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

    return StreamingResponse(gen(), media_type="text/event-stream")


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
