"""Pipeline observer: per-turn latency instrumentation + transcript capture.

Placed just before ``transport.output()`` so every downstream frame in a turn
(user-stopped-speaking, transcription, first LLM token, first TTS audio) passes
through it. From those we derive the per-stage latency breakdown promised in the
design and persist each turn. All logic is wrapped so it can never break the
voice loop.
"""

import time

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class Observer(FrameProcessor):
    def __init__(
        self, persistence, session_id: str,
        caller_key: str | None = None, agent_slug: str | None = None,
    ) -> None:
        super().__init__()
        self._p = persistence
        self._sid = session_id
        self._caller_key = caller_key  # tags turns for cross-call memory
        self._agent_slug = agent_slug
        self._t_user_stop: float | None = None
        self._got_stt = False
        self._got_llm = False
        self._got_tts = False
        self._assistant_buf: list[str] = []

    async def _log(self, role: str, content: str) -> None:
        await self._p.log_turn(self._sid, role, content, self._caller_key, self._agent_slug)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        try:
            await self._observe(frame)
        except Exception as e:  # never break the pipeline
            logger.warning(f"observer error: {e}")
        await self.push_frame(frame, direction)

    async def _observe(self, frame: Frame) -> None:
        now = time.monotonic()

        if isinstance(frame, UserStoppedSpeakingFrame):
            # Start of a new turn's latency clock.
            self._t_user_stop = now
            self._got_stt = self._got_llm = self._got_tts = False

        elif isinstance(frame, TranscriptionFrame):
            if self._t_user_stop is not None and not self._got_stt:
                self._got_stt = True
                logger.info(
                    f"[latency] STT {(now - self._t_user_stop) * 1000:.0f}ms "
                    f"-> '{frame.text}'"
                )
            if frame.text and frame.text.strip():
                await self._log("user", frame.text.strip())

        elif isinstance(frame, LLMFullResponseStartFrame):
            self._assistant_buf = []

        elif isinstance(frame, LLMTextFrame):
            if self._t_user_stop is not None and not self._got_llm:
                self._got_llm = True
                logger.info(
                    f"[latency] LLM TTFT {(now - self._t_user_stop) * 1000:.0f}ms"
                )
            if frame.text:
                self._assistant_buf.append(frame.text)

        elif isinstance(frame, LLMFullResponseEndFrame):
            text = "".join(self._assistant_buf).strip()
            if text:
                await self._log("assistant", text)

        elif isinstance(frame, TTSAudioRawFrame):
            if self._t_user_stop is not None and not self._got_tts:
                self._got_tts = True
                logger.info(
                    f"[latency] TTS first-audio {(now - self._t_user_stop) * 1000:.0f}ms "
                    f"(end-to-end since user stopped speaking)"
                )
