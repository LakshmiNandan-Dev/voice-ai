"""Per-call audio recording.

Pipecat's AudioBufferProcessor sits at the end of the pipeline and, on
`stop_recording()`, emits the merged user+bot audio (16-bit PCM). We collect it
and write a WAV file to a server-side directory (a mounted volume), then record
metadata in Postgres. Audio bytes live on disk, not in the DB — for production,
point `RECORDINGS_DIR` at an object-store-backed mount (S3/GCS).
"""

from __future__ import annotations

import asyncio
import os
import wave

from loguru import logger


class SessionRecorder:
    def __init__(self, session_id: str, directory: str) -> None:
        self._sid = session_id
        self._dir = directory
        self._chunks = bytearray()
        self._sample_rate: int | None = None
        self._channels = 1

    def add(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        if not audio:
            return
        self._chunks += audio
        self._sample_rate = sample_rate
        self._channels = num_channels or 1

    @property
    def has_audio(self) -> bool:
        return len(self._chunks) > 0 and self._sample_rate is not None

    async def save(self, persistence) -> dict | None:
        """Write the WAV and record metadata. Returns the recording row or None."""
        if not self.has_audio:
            return None
        path = os.path.join(self._dir, f"{self._sid}.wav")
        data = bytes(self._chunks)
        sr, ch = self._sample_rate, self._channels

        def _write() -> None:
            os.makedirs(self._dir, exist_ok=True)
            with wave.open(path, "wb") as w:
                w.setnchannels(ch)
                w.setsampwidth(2)  # 16-bit PCM
                w.setframerate(sr)
                w.writeframes(data)

        try:
            await asyncio.to_thread(_write)
        except Exception as e:
            logger.warning(f"recording: failed to write {path}: {e}")
            return None

        duration = len(data) / float(sr * ch * 2) if sr else 0.0
        logger.info(
            f"recording: saved {path} ({len(data)} bytes, {duration:.1f}s, "
            f"{sr}Hz x{ch})"
        )
        return await persistence.add_recording(
            self._sid, path, sr, ch, len(data), duration
        )
