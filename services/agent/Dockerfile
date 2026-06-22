FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# libgomp1: OpenMP runtime for ctranslate2 (faster-whisper). ffmpeg: audio decode.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY . .

EXPOSE 7860

# Models download on first request into /models (mount a volume to cache them).
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=10 \
    CMD curl -fsS http://localhost:7860/health || exit 1

CMD ["python", "bot.py", "--host", "0.0.0.0", "--port", "7860"]
