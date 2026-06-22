"""Voice agent entrypoint.

Uses Pipecat's runner, which provides the FastAPI server with the `/start` and
`/ws-client` endpoints that the browser WebSocket transport talks to. We import
the runner's `app` to add our own Kubernetes-friendly `/health` route, and wire
the WebSocket transport to the cascade pipeline in `pipeline.py`.

Run: `python bot.py --host 0.0.0.0 --port 7860`
"""

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.runner.run import app, main
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

import logbuffer
from admin import mount_admin
from auth import mount_auth
from config import config
from persistence import Persistence
from pipeline import run_bot

load_dotenv(override=True)

# Capture recent logs into a ring buffer the admin console can read.
logbuffer.install(logger)

persistence = Persistence(config.redis_url, config.database_url)

# Durable runtime settings live in Postgres (via persistence).
from email_config import email_config  # noqa: E402
from settings import settings  # noqa: E402

settings.bind(persistence)
email_config.bind(persistence)

# Verification gate: routes (/auth/*) + the middleware that guards /start. Added
# at import time because middleware can't be attached once the app is serving;
# the code store connects and demo orders seed lazily on first use.
mount_auth(app, persistence)

# Admin console API: /admin/* (live config, call monitoring, logs).
mount_admin(app, persistence)

# Transport for the browser WebSocket client. VAD on the input drives
# endpointing and barge-in; Protobuf serializer matches the JS client.
transport_params = {
    "websocket": lambda: FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        add_wav_header=False,
        vad_analyzer=SileroVADAnalyzer(),
        serializer=ProtobufFrameSerializer(),
    ),
}


@app.get("/health")
async def health():
    """Liveness/readiness probe for Kubernetes."""
    return {"status": "ok", "service": "voice-agent", "model": config.llm_model}


async def bot(runner_args: RunnerArguments):
    """Entry point invoked by the runner when a client connects to /ws-client."""
    await persistence.connect()
    transport = await create_transport(runner_args, transport_params)
    session_id = getattr(runner_args, "session_id", None) or "unknown"
    # The client passes ?agent=<slug> (which agent to run) and ?cid=<token> (the
    # caller's invitation token) on the WebSocket. cid resolves server-side to a
    # stable caller identity for cross-call memory — it's never trusted as PII.
    agent_slug = None
    caller_key = None
    ws = getattr(runner_args, "websocket", None)
    if ws is not None:
        try:
            agent_slug = ws.query_params.get("agent")
            cid = ws.query_params.get("cid")
            if cid:
                inv = await persistence.get_invitation_by_token(cid)
                if inv:
                    caller_key = inv.get("destination")
                    agent_slug = agent_slug or inv.get("agent_slug")
        except Exception as e:
            logger.warning(f"caller resolve failed: {e}")
    logger.info(f"starting bot session={session_id} agent={agent_slug or '(default)'}")
    try:
        await run_bot(transport, persistence, session_id, agent_slug, caller_key)
    except Exception as e:
        logger.exception(f"bot error: {e}")
        raise


if __name__ == "__main__":
    main()
