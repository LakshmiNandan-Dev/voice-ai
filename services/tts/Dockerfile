FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# espeak-ng: phonemizer backend Piper uses for text normalization.
RUN apt-get update && apt-get install -y --no-install-recommends \
    espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "piper-tts[http]"

# Bake the voice model into the image so the container is self-contained.
ARG PIPER_VOICE=en_US-ryan-high
ENV PIPER_VOICE=${PIPER_VOICE}
RUN python -m piper.download_voices ${PIPER_VOICE} --data-dir /voices

EXPOSE 5000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -fsS http://localhost:5000/voices || exit 1

# JSON {"text","voice"} POST -> WAV, exactly what Pipecat's PiperHttpTTSService expects.
CMD ["sh", "-c", "python -m piper.http_server -m \"$PIPER_VOICE\" --data-dir /voices --host 0.0.0.0 --port 5000"]
