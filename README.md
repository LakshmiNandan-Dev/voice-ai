# Voice AI — real-time voice-to-voice conversational agent platform

You talk, it listens, thinks with a **pluggable LLM**, and talks back — with
natural turn-taking and barge-in (interrupt it mid-sentence by speaking). Speech
is **fully self-hosted**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
for STT and [Piper](https://github.com/OHF-Voice/piper1-gpl) for TTS. The
real-time pipeline is built on [Pipecat](https://github.com/pipecat-ai/pipecat).

It's a small platform, not just the loop:

- **Pluggable brain** — Claude, OpenAI, Google Gemini, or any self-hosted
  OpenAI-compatible model (Ollama/Mistral, vLLM, Triton, …), switchable at runtime.
- **Invitation-only access** — callers can't self-start; an admin sends a one-time
  code, and each invitation grants a limited number of calls (single-use lock,
  admin re-enable).
- **Admin console** — manage a **library of agents** (each its own type, prompt,
  model, voice), monitor live calls/transcripts/logs, and play back recordings.
- **Call recording** — every call is captured (merged user + agent) and stored.
- **Guardrails** — fixed rules wrap each agent so a caller can't change its role
  or extract its instructions.

```
  Browser (React) ──WS(PCM)──►  agent (Pipecat + FastAPI)         Admin console (/admin)
        ▲                         Silero VAD ─► Whisper STT          invitations · agents
        │                              │                             calls · logs · recordings
        │                         [ guardrails ─► LLM ]  ◄── Claude / OpenAI / Gemini / Ollama / …
        │                              │
        └──── Piper TTS ◄────── recorder (WAV)
                                       │
   Postgres: invitations · agents · config · transcripts · recordings meta
   Redis: one-time tickets · active-call counts        ·   tts container
```

The same services run locally via Docker Compose and in production via the
Helm chart in [deploy/helm](deploy/helm) — only the wiring differs.

## Quickstart (local, Docker)

Requires Docker Desktop and an LLM: an **Anthropic API key**, a local **Ollama**
(reachable at `host.docker.internal:11434`), or any other supported provider.

```bash
cp .env.example .env        # set ANTHROPIC_API_KEY (or LLM_PROVIDER=ollama + LLM_MODEL=mistral:7b)
docker compose up --build   # first build downloads torch + models; be patient
```

Calls are **invitation-only** — a user can't start one on their own. First, an
admin sends an invitation: open **http://localhost:3000/admin** (token
`voice-admin`) → **Invitations** → send one to an email / order ID / phone. The
recipient gets a one-time code, opens **http://localhost:3000**, enters the code
(plus the email/phone it went to), clicks **Start conversation**, and talks.

> **No email/SMS provider needed to try it.** Out of the box the agent runs in
> *dev mode*: the generated code is shown right in the admin Invitations table
> (and logged), so the whole flow is testable locally. Order ID `ORD-1001` is
> seeded. Wire up SMTP / Twilio in `.env` to send real codes (see
> [Verification gate](#verification-gate-invitation-only)).

Ports: web `:3000` · agent `:7860` · tts `:5001` · postgres `:5432` · redis `:6379`.

> First run is slow: the agent image pulls PyTorch, and Whisper/Silero models
> download on the first call (cached in the `models` volume afterward).

## ⚠️ macOS latency note (read this)

Docker Desktop on Apple Silicon **cannot** pass the Metal GPU into Linux
containers, so Whisper runs on **CPU** here. Expect end-to-end response latency
of roughly **1.2–2.5s** locally — fully functional, just not snappy. On a
production GPU node (`WHISPER_DEVICE=cuda`) the same images hit **~0.7–0.9s**.

To tune local latency: set `WHISPER_MODEL=tiny.en` and/or `LLM_MODEL=claude-haiku-4-5`
in `.env`. For genuinely fast local dev, run the agent natively on macOS with
Pipecat's MLX Whisper backend (uses Metal) instead of in Docker — see
[docs/DESIGN.md](docs/DESIGN.md).

## What's where

| Path | What |
|------|------|
| [services/agent/pipeline.py](services/agent/pipeline.py) | The voice pipeline: VAD → Whisper STT → LLM → Piper TTS, barge-in, recording, per-turn latency |
| [services/agent/bot.py](services/agent/bot.py) | Pipecat runner entrypoint; mounts auth + admin, routes the call to the chosen agent |
| [services/agent/auth.py](services/agent/auth.py) | Invitation gate: code verify, email/SMS delivery, order lookup, atomic call-consume `/start` guard |
| [services/agent/admin.py](services/agent/admin.py) | Admin console API: agents, invitations, config, calls, recordings, logs |
| [services/agent/providers.py](services/agent/providers.py) · [llm.py](services/agent/llm.py) | LLM provider registry (Claude/OpenAI/Gemini/Ollama/vLLM/custom) + factory |
| [services/agent/personas.py](services/agent/personas.py) · [guardrails.py](services/agent/guardrails.py) | Agent-type presets + fixed prompt-injection guardrails |
| [services/agent/settings.py](services/agent/settings.py) · [persistence.py](services/agent/persistence.py) | Durable runtime config + Postgres/Redis (agents, invitations, transcripts, recordings) |
| [services/agent/recording.py](services/agent/recording.py) | Per-call audio capture → WAV on a server-side volume |
| [services/tts](services/tts) | Piper HTTP server (self-contained, voice baked in) |
| [web](web) | React + Vite client: the call UI plus the `/admin` console |
| [deploy/helm](deploy/helm) | Production Kubernetes chart (Deployments, HPA, ingress, secrets) |
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture, latency budget, scaling, deployment |

## Verification gate (invitation-only)

A user **cannot start a call on their own** — an admin must invite them first,
and each invitation is good for a limited number of calls (1 by default). Once
used, the user is locked out until an admin re-enables it.

**Admin sends the invite** (admin console → Invitations, or the API). The channel:

| Channel | Admin enters | Code goes to |
|---------|--------------|--------------|
| **Email** | an email address | that address |
| **Order ID** | an order number | the email (or phone) on file for that order |
| **Phone** | a phone number (E.164) | that number via SMS |

Flow (server-enforced — see [services/agent/auth.py](services/agent/auth.py)):

1. `POST /admin/invitations {method, value}` → resolves a destination, creates an
   invitation in **Postgres**, mints a code, and delivers it (email/SMS).
2. `POST /auth/verify {token?|destination, code}` → on match returns a short-lived
   Redis `ticket` bound to the invitation. Codes are single-use, expire after
   `OTP_TTL_SECONDS`, and lock out after `OTP_MAX_ATTEMPTS`.
3. `POST /start?ticket=…` → `AuthGate` validates the ticket **and atomically
   spends one call allowance** (`UPDATE … WHERE calls_used < calls_allowed`). No
   allowance left → 403. This is the durable, race-safe "one call then locked" rule.
4. **Re-enable:** admin → *Allow again* → grants one more call and sends a **fresh
   code** (the old one is dead).

Invitations and call entitlements are the **system of record in Postgres**
(atomic, durable); tickets and active-call counts live in **Redis** (hot,
ephemeral). **Delivery:** set `SMTP_*` / `TWILIO_*` to send real codes; with
neither set the agent runs in **dev mode** and shows the code in the admin
Invitations table. Demo orders come from `ORDERS_SEED`.

## Admin console & model providers

Open **http://localhost:3000/admin** (token-gated). Four tabs:

- **Invitations** — issue a code to an email / order / phone, **routed to a chosen
  agent**; see every invitation's status (sent / verified / consumed / revoked)
  and calls used; **Allow again** (re-enable with a fresh code) or **Revoke**.
- **Agents** — a library of named call configurations you can grow over time.
  Each agent overrides the global Defaults selectively (blank = inherit): type
  (persona), role prompt, provider/model/base-URL/key, TTS voice, Whisper model.
  Create / edit / make-default / enable / delete. Invitations route a caller to a
  specific agent; the call loads that agent's config (guardrails still applied).
- **Defaults** — the global config used when an invitation isn't tied to an agent:
  **LLM provider & model**, agent type + prompt, TTS voice, Whisper model.
  Persisted (Postgres), applies to the **next** call.
- **Email** — the **SMTP account** invitation codes are sent from (host, port,
  user, password, from, TLS), editable at runtime with a **Send test** button.
  Overrides the `SMTP_*` env defaults; password is write-only. With no host set,
  the agent stays in dev mode (codes shown in Invitations instead of emailed).
- **Calls** — a durable list of every call (caller, agent, time, duration, turns);
  open one to see its **complete transcript text** (start to end) next to the
  **audio recording** (play/download the merged user+agent audio).
- **Logs** — recent agent logs/debug incl. the per-turn latency lines, with a
  level filter.

**Recording.** Every completed call is recorded server-side: a Pipecat
`AudioBufferProcessor` at the end of the pipeline captures the merged user + agent
audio and the agent writes a WAV to `RECORDINGS_DIR` (a Docker volume; an object
store in production) with metadata in Postgres. Toggle with `RECORD_AUDIO`. The
live user+agent transcript also renders in the call UI as you speak.

**Sign-in:** the `ADMIN_TOKEN` env var (defaults to `voice-admin` locally, with a
warning — set a real one in production).

**LLM providers — any model.** The brain is pluggable across vendors
([services/agent/providers.py](services/agent/providers.py),
[llm.py](services/agent/llm.py)). Pick a provider, model, and (where relevant) a
base URL and API key in the admin **Defaults** tab (or per agent in **Agents**):

| Provider | Service | Needs |
|----------|---------|-------|
| `anthropic` | native Claude (prompt caching on) | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI API | `OPENAI_API_KEY` |
| `gemini` | Google Gemini (its OpenAI-compatible endpoint) | `GEMINI_API_KEY` |
| `ollama` | self-hosted (e.g. **Mistral**) | reachable Ollama; keyless |
| `vllm` | self-hosted vLLM | base URL (key optional) |
| `custom` | **any** OpenAI-compatible server (Triton, Groq, Together, LM Studio, …) | base URL + key |

Everything except Anthropic runs through one OpenAI-compatible path, so "any
model" really means any server that speaks `/v1/chat/completions`. API keys may
be set per-provider in the env **or** entered in the admin form (stored
server-side, never shown back). Provider/model/base-URL/key changes apply to the
**next** call.

To use your local **Ollama + Mistral**: admin → **Defaults** (or a new **Agent**)
→ provider **Ollama**, model **`mistral:7b`** (the dropdown auto-lists installed
models). The container reaches the host's Ollama at `host.docker.internal:11434`
(`OLLAMA_BASE_URL`).

**Guardrails.** Each agent's role prompt is always wrapped by fixed guardrails
([services/agent/guardrails.py](services/agent/guardrails.py)) before it reaches
the model: the caller can't change the agent's role, override its instructions, or
extract its configuration mid-call. The admin sets the *role*; the guardrails
can't be edited away. Toggle with `ENFORCE_GUARDRAILS` (default on).

## Configuration

All via `.env` (see [.env.example](.env.example)):

- **LLM:** `LLM_PROVIDER`, `LLM_MODEL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `GEMINI_API_KEY`, `OLLAMA_BASE_URL`.
- **Speech:** `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE`, `TTS_VOICE`.
- **Access & admin:** `ADMIN_TOKEN`, the verification gate (`SMTP_*`, `TWILIO_*`,
  `ORDERS_SEED`, `OTP_*`, `TICKET_TTL_SECONDS`).
- **Calls:** `RECORD_AUDIO`, `RECORDINGS_DIR`, `ENFORCE_GUARDRAILS`.

Provider/model, agent type & prompt, TTS voice, and Whisper model are **also
editable at runtime** from the admin console (global **Defaults** or per **Agent**)
— no restart needed; changes apply to the next call.

## Verifying it works

- **Invitation gate**: `/admin` → Invitations → send one (dev mode shows the code) →
  enter it at `/`. A second call without an *Allow again* is blocked.
- **Conversation**: speak → hear a streamed spoken reply; transcript updates live.
- **Barge-in**: talk over the AI → its audio stops and it listens.
- **Latency**: `docker compose logs -f agent` shows per-stage timing per turn
  (`[latency] STT … / LLM TTFT … / TTS first-audio …`).
- **Recording**: after a call, `/admin` → Calls → Recordings (or
  `docker compose exec agent ls -la /data/recordings`).
- **Persistence**: `docker compose exec postgres psql -U voice -d voice -c 'select role, content from transcripts order by id desc limit 10;'`

## TLS / HTTPS

TLS is **offloaded at the edge** by your load balancer / ingress — the app
containers run plain HTTP behind it (tts/redis/postgres stay internal). Terminate
on a single origin that routes both the web app and the agent, and forward
WebSocket upgrades.

- **Local dev** — no TLS needed: `http://localhost` is already a secure context,
  so the mic works. Just `docker compose up` (web `:3000`, agent `:7860`).
- **Production (Helm)** — [the ingress](deploy/helm/voice-ai/templates/ingress.yaml)
  does the TLS offload. In `values.yaml`: set `ingress.host`,
  `ingress.tls.enabled: true`, and either `ingress.tls.clusterIssuer` (cert-manager
  auto-provisions the cert) or supply your own Secret named `ingress.tls.secretName`.
  **Build the web image with `VITE_AGENT_URL=https://<host>`** so fetches and
  `wss://` target the TLS host. Any external LB that terminates TLS and forwards
  WebSocket upgrades works the same way.

The ingress routing splits the **`/admin` page** (web) from the **`/admin/*` API**
(agent) so the admin console works behind one TLS origin. Off `localhost`, HTTPS is
required — browsers block mic access outside a secure context, and an HTTPS page
can't open an insecure `ws://`.

## Production

See [docs/DESIGN.md](docs/DESIGN.md) and [deploy/helm](deploy/helm) for the
cloud-agnostic Kubernetes deployment (autoscaling on active calls, WebSocket
sticky ingress, graceful drain, GPU node pool toggle, managed vs in-cluster
Redis/Postgres). Set real secrets (`ADMIN_TOKEN`, provider API keys, `SMTP_*` /
`TWILIO_*`) via the chart's Secret. **Recordings** use a per-pod volume by default
(ephemeral); for durable, multi-replica audio point `RECORDINGS_DIR` at an
object-store-backed mount (S3/GCS).
