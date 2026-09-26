# Module 01 — Overview & Goals

## 1. Problem

GPU VRAM is a scarce, shared resource. Today each GPU workload (LLM server, Ollama,
ComfyUI, TTS, etc.) runs as a permanently resident process that pins its model in
VRAM, even when idle. On a 24 GB card, loading an 18 GB LLM leaves no room for an
8 GB image model, so users must manually start/stop applications and guess what fits.

## 2. What GPUSymbiosis Does

The system dynamically manages a limited GPU VRAM resource by:

1. Monitoring running applications (process, GPU, CPU).
2. Receiving API requests through gateway ports.
3. Queuing requests when their application is not currently active.
4. Determining which application should be resident.
5. Loading / unloading applications based on VRAM requirements.
6. Forwarding requests to the appropriate application.
7. Preventing unloading while requests are still executing.
8. Tracking historical demand and application performance.
9. Providing a web UI for configuration and monitoring.

## 3. Core Example

Assume:

```text
GPU VRAM = 24 GB
```

Configured applications:

| Application     |   VRAM | Port | Type    |
| --------------- | -----: | ---: | ------- |
| A1 — Qwen 3.8   | 18 GB  | 3000 | llama   |
| A2 — Qwen 3.5   | 12 GB  | 3001 | llama   |
| A3 — ComfyUI    |  8 GB  | 8188 | comfyui |
| A4 — Breeze TTS |  6 GB  | 5000 | tts     |

External gateway ports:

```text
913000 → A1
913001 → A2
913002 → A3
913003 → A4
```

Suppose A1 is currently loaded:

```text
GPU:  A1 = 18 GB,  Free = 6 GB
```

A request arrives:

```text
POST :913001   →  belongs to A2
```

The scheduler determines:

```text
A2 requires 12 GB;  Free VRAM = 6 GB
→ A2 cannot coexist with A1
```

Gateway flow:

```text
A2 Gateway → Queue request → Scheduler
→ A1 finishes active requests → A1 unloaded
→ A2 loaded → A2 ready → Queued request forwarded
```

## 4. Goals

- **Zero manual juggling**: clients call stable gateway ports; residency is automatic.
- **Zero interrupted work**: a request never dies mid-generation because another app needs VRAM.
- **Coexistence**: any set of applications that fits in usable VRAM may be resident at once.
- **Learned accuracy**: memory and timing figures improve automatically from observation.
- **Explainability**: every scheduling decision is logged and visible in the UI.
- **Local-first**: runs on the GPU machine; defaults to loopback binding.

## 5. Non-Goals (initial)

- Multi-GPU / multi-node orchestration (single local GPU; design must not preclude it).
- Preemption / killing of in-flight requests (explicitly forbidden by Invariant 1).
- GPU virtualisation (MIG, vGPU) — physical VRAM accounting only.
- Cloud / multi-tenant deployment (Phase 5 auth is local admin auth only).
