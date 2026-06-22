"""Caps the live conversation context to the last N messages (long-call handling).

Placed just before the LLM so it trims right before each generation. It keeps all
system messages (guardrails/role) plus the most recent N non-system turns, so a
long call can't grow the prompt unbounded. The trim is idempotent and cheap, so
running it on every frame is safe. 0 = unlimited (processor not added).
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class ContextWindow(FrameProcessor):
    def __init__(self, context, max_messages: int) -> None:
        super().__init__()
        self._ctx = context
        self._max = max_messages

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        try:
            if self._max > 0:
                msgs = self._ctx.get_messages()
                non_system = [m for m in msgs if m.get("role") != "system"]
                if len(non_system) > self._max:
                    system = [m for m in msgs if m.get("role") == "system"]
                    self._ctx.set_messages(system + non_system[-self._max:])
        except Exception as e:  # never break the voice loop
            logger.warning(f"context window error: {e}")
        await self.push_frame(frame, direction)
