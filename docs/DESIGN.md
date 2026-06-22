# Voice AI — Design

A real-time, voice-to-voice conversational agent. You talk, it listens, reasons
with an LLM, and talks back, with natural turn-taking and barge-in. The whole
experience is governed by **end-to-end latency**, so every stage streams and
pipelines.

## Decisions

- **Orchestration:** [Pipecat](https://github.com/pipecat-ai/pipecat) — a
  Python framework purpose-built for real-time voice agents (VAD, barge-in,
  sentence-level TTS pipelining, transports).
- **Speech is self-hosted:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  (STT) and [Piper](https://github.com/OHF-Voice/piper1-gpl) (TTS). No cloud
  speech vendors.
- **LLM brain is Claude** via the Anthropic API (`AnthropicLLMService`),
  abstracted behind config so a self-hosted open model could be swapped in.
- **One topology, two environments:** the same containers run locally
  (Docker Compose) and in production (Kubernetes/Helm).

## The conversation loop

```
  🎤 Mic ─► Silero VAD / endpointing ─► Whisper STT ─► Claude (streaming tokens)
                                                            │
   🔊 Speaker ◄─ Piper TTS ◄─ sentence chunking ◄───────────┘

   barge-in: user speaks while AI is talking → VAD fires → playback stops,
             queued TTS is flushed, the mic re-opens.
```

The latency trick: as Claude streams tokens, Pipecat cuts at sentence
boundaries and sends each finished sentence to Piper immediately, so audio
starts playing while Claude is still generating the rest.

## Components

| Layer | Responsibility | Tech |
|-------|----------------|------|
| **web** | Mic capture, playback, barge-in, transcript + state UI | React + Vite, `@pipecat-ai/client-js` + `websocket-transport` |
| **agent** | VAD, endpointing, STT, dialogue/context, Claude, TTS client, interruption, latency, persistence | Pipecat 1.x on FastAPI (the Pipecat "runner") |
| **tts** | Streaming text→speech | Piper HTTP server (`piper-tts[http]`) |
| **redis** | Live session state, active-call count | Redis |
| **postgres** | Turn-by-turn transcripts | Postgres |

**Why STT is in-process but TTS is a separate container:** Pipecat ships an
in-process Whisper service (lowest latency, simplest) and a *networked* Piper
client (`PiperHttpTTSService`), so the TTS split is free and Piper is
CPU-friendly everywhere. Extracting STT into its own GPU service (for better
GPU pooling at high concurrency) is a future optimization, not required for v1.

### Verification gate (invitation-only)

A user cannot start a call on their own; an **admin issues an invitation** first,
and each invitation grants a bounded number of calls (1 by default). The gate
lives in [services/agent/auth.py](../services/agent/auth.py) (+ admin endpoints
in [admin.py](../services/agent/admin.py)) as routes plus an ASGI middleware:

- `POST /admin/invitations {method, value}` resolves a destination — an email, the
  email/phone on file for an **order id**, or a phone — and creates an invitation
  row in **Postgres** with a fresh code, delivered via SMTP/Twilio. The code is
  never returned to end users.
- `POST /auth/verify {token? | destination, code}` checks the code (single-use,
  TTL-bounded, attempt-capped, bound to the invitation) and issues a short-lived
  Redis `ticket`.
- `AuthGate` guards `POST /start`: it requires a live ticket **and atomically
  spends one call allowance** (`UPDATE invitations SET calls_used = calls_used+1
  WHERE calls_used < calls_allowed RETURNING …`). No allowance → 403. The atomic
  guard makes "one call then locked" race-safe across replicas.
- Re-enable is an explicit admin action that grants one more call and sends a
  **fresh** code (the prior one is dead).

**Datastore roles (polyglot, by access pattern):** Postgres is the *system of
record* — invitations, call entitlements, runtime config (`app_settings`),
transcripts — because the consume must be atomic and durable. Redis holds *hot
ephemeral* state — single-use tickets, active-call counts. Logs go to an
in-memory ring buffer (live admin view) plus stdout for shipping. Each store also
has an in-process fallback so the gate runs locally with no extra infra; delivery
degrades to logging the code ("dev mode"). The order channel reads an `orders`
table (seeded from `ORDERS_SEED`); swap in a real customer datastore for prod.

### Request flow

1. Admin invites the user → code delivered; user verifies → browser holds a `ticket`.
2. Browser POSTs `/start?ticket=…` (`{transport: "websocket"}`); `AuthGate`
   validates the ticket and consumes one call allowance → agent returns a
   `/ws-client` URL + token.
3. Browser opens the WebSocket; Pipecat builds a `FastAPIWebsocketTransport`
   (Protobuf serializer) and starts the pipeline for that session.
4. Pipeline: `transport.input() → stt → user_aggregator → llm → tts → observer
   → transport.output() → assistant_aggregator`.
5. The `observer` (just before output) sees every downstream frame, logs
   per-stage latency, and writes turns to Postgres/Redis.

## Latency budget

| Stage | Local (Mac CPU) | Prod (GPU) |
|-------|-----------------|------------|
| Endpointing (silence) | 200–400ms | 200–400ms |
| Whisper STT (post-endpoint) | 600–1500ms | 80–200ms |
| Claude time-to-first-token | 300–500ms | 300–500ms |
| Piper first audio chunk | 100–250ms | 100–250ms |
| **End-to-end (overlapped)** | **~1.2–2.5s** | **~0.7–0.9s** |

Levers: smaller Whisper model, shorter VAD silence window, Haiku vs Opus,
sentence-level TTS pipelining, Claude prompt caching on the system prompt.

### macOS reality

Docker Desktop on Apple Silicon cannot pass the Metal GPU into Linux
containers, so Whisper runs CPU-only locally. Options:

- **Default (Docker):** accept ~1.2–2.5s; use `tiny.en`/`base.en` + `claude-haiku-4-5`.
- **Fast local dev (native):** run the agent natively on macOS with Pipecat's
  MLX Whisper backend (`WhisperSTTServiceMLX`, uses Metal). Trades
  container-purity for speed. Keep Docker as the default path.
- **Production:** GPU node, `WHISPER_DEVICE=cuda` → hits the target.

## Production (Kubernetes) — see [deploy/helm](../deploy/helm)

- **Deployments + Services** for `web`, `agent`, `tts`. Agent runs `device=cuda`
  on a GPU node pool when available, falls back to CPU otherwise.
- **Autoscaling:** HPA on the agent keyed to **active-session count** (a call is
  a long-lived WebSocket, so CPU alone is a poor signal) plus CPU; Cluster
  Autoscaler for nodes; TTS HPA on request rate.
- **Session affinity:** a live call's WebSocket pins to one agent pod
  (WS-aware sticky ingress). Session state lives in Redis so pods stay
  restart-safe.
- **Graceful drain:** `preStop` hook + long `terminationGracePeriodSeconds` so
  scale-down finishes in-flight calls. Readiness probes gate traffic until
  models are loaded.
- **Stateful deps:** `values.yaml` toggles in-cluster vs managed Redis/Postgres
  (ElastiCache/Memorystore/Azure Cache, RDS/Cloud SQL).
- **Secrets:** `ANTHROPIC_API_KEY` via a Kubernetes Secret; everything else via
  values/ConfigMap. Ingress terminates TLS and upgrades WebSocket.
- **Observability:** Pipecat metrics + the per-turn latency logs; extend to
  OpenTelemetry traces (span per stage) and a Grafana dashboard (latency,
  concurrent calls, cost).

## Known limitations / next steps

- English-only v1 (`base.en` Whisper + an English Piper voice). Multilingual is
  a model/voice swap.
- WebSocket transport v1; Pipecat SmallWebRTC/Daily gives more robust audio
  (jitter buffer, echo cancellation) and is the production upgrade path.
- STT runs in-process in the agent; extract to a dedicated GPU service when
  concurrency makes shared-GPU pooling worthwhile.
