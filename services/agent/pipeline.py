"""The voice pipeline: VAD -> Whisper STT -> Claude -> Piper TTS.

Sentence-level streaming is handled by Pipecat: as Claude streams tokens, the
TTS service begins synthesizing completed sentences immediately, so audio starts
playing before the full reply is generated. Barge-in (interruption) is driven by
the Silero VAD on the transport input.
"""

import asyncio

import aiohttp
from loguru import logger
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.services.piper.tts import PiperHttpTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport
from pipecat.workers.runner import WorkerRunner

from config import config
from context_window import ContextWindow
from guardrails import wrap as wrap_guardrails
from llm import build_llm
from observer import Observer
from recording import SessionRecorder
from settings import settings


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def run_bot(
    transport: BaseTransport, persistence, session_id: str,
    agent_slug: str | None = None, caller_key: str | None = None,
) -> None:
    """Assemble and run the conversation pipeline for one client session."""
    # Effective settings are read per-call (admin edits apply to new calls) and
    # specialised for the routed agent, if any.
    eff = await settings.get_for_agent(agent_slug)
    memory_on = str(eff.get("memory_enabled", "false")).lower() == "true"
    memory_max = _as_int(eff.get("memory_max_messages"), 20)
    history_max = _as_int(eff.get("history_max_messages"), 0)
    logger.info(
        f"session={session_id} agent={agent_slug or '(default)'} "
        f"settings: {eff['llm_provider']}/{eff['llm_model']} "
        f"memory={'on' if memory_on and caller_key else 'off'} window={history_max or 'unlimited'}"
    )

    async with aiohttp.ClientSession() as session:
        # Speech-to-text: local faster-whisper (CPU here, CUDA on GPU nodes).
        stt = WhisperSTTService(
            model=eff["whisper_model"],
            device=config.whisper_device,
            compute_type=config.whisper_compute_type,
            language=Language.EN,
        )

        # The brain: Claude or a self-hosted OpenAI-compatible model (Ollama).
        # The role prompt is always wrapped with fixed guardrails so the caller
        # can't change the agent's role or extract its instructions mid-call.
        system_text = wrap_guardrails(eff["system_prompt"], config.enforce_guardrails)
        llm, system_in_context = build_llm(eff, system_text)

        # Text-to-speech: self-hosted Piper HTTP server (separate container).
        tts = PiperHttpTTSService(
            base_url=config.tts_base_url,
            aiohttp_session=session,
            settings=PiperHttpTTSService.Settings(voice=eff["tts_voice"]),
        )

        # Conversation memory + user/assistant aggregation, with VAD for endpointing.
        context = LLMContext()
        # OpenAI-compatible models take the system prompt as a context message;
        # Anthropic already has it via system_instruction (see build_llm).
        if system_in_context:
            context.add_message({"role": "system", "content": system_text})
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        )

        observer = Observer(persistence, session_id, caller_key, agent_slug)

        # Long-call handling: cap the live context to the last N messages.
        context_window = ContextWindow(context, history_max) if history_max > 0 else None

        # Records the whole call: sits after transport.output() so it sees both
        # the user's mic audio and the bot's TTS audio, and emits the merged mix.
        # on_audio_data fires once (buffer_size=0) when stop_recording() flushes,
        # as a task — `_saved` lets the disconnect handler await it before teardown.
        audiobuffer = AudioBufferProcessor(num_channels=1)
        recorder = SessionRecorder(session_id, config.recordings_dir)
        _saved: asyncio.Future = asyncio.get_event_loop().create_future()

        @audiobuffer.event_handler("on_audio_data")
        async def on_audio_data(buffer, audio, sample_rate, num_channels):
            try:
                recorder.add(audio, sample_rate, num_channels)
                await recorder.save(persistence)
            finally:
                if not _saved.done():
                    _saved.set_result(True)

        pipeline = Pipeline(
            [
                p
                for p in [
                    transport.input(),
                    stt,
                    user_aggregator,
                    context_window,  # trims context right before the LLM (may be None)
                    llm,
                    tts,
                    observer,
                    transport.output(),
                    audiobuffer,
                    assistant_aggregator,
                ]
                if p is not None
            ]
        )

        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )

        @worker.rtvi.event_handler("on_client_ready")
        async def on_client_ready(rtvi):
            logger.info(f"client ready session={session_id}")
            await persistence.start_session(session_id)
            await persistence.start_call(session_id, caller_key, agent_slug)
            if config.record_audio:
                await audiobuffer.start_recording()
            # Cross-call memory: seed this caller's prior turns before greeting.
            if memory_on and caller_key:
                history = await persistence.history_for_caller(caller_key, agent_slug, memory_max)
                if history:
                    context.add_messages(history)
                    logger.info(f"memory: seeded {len(history)} prior messages for this caller")
            # Kick off the conversation with a greeting (returning callers get a
            # warmer one since the model can see the prior history).
            greeting = (
                "Greet the returning user warmly by referring to your earlier "
                "conversation in one short sentence, then ask how you can help."
                if (memory_on and caller_key)
                else "Greet the user warmly in one short sentence and ask how you can help."
            )
            context.add_message({"role": "user", "content": greeting})
            await worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"client connected session={session_id}")

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"client disconnected session={session_id}")
            await persistence.end_session(session_id)
            # Flush + persist the recording before tearing down the pipeline.
            if config.record_audio and audiobuffer.has_audio():
                try:
                    await audiobuffer.stop_recording()
                    await asyncio.wait_for(_saved, timeout=15)
                except Exception as e:
                    logger.warning(f"recording: save failed: {e}")
            # Store the complete call transcript (built from the turns) as one row.
            await persistence.end_call(session_id)
            await worker.cancel()

        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
