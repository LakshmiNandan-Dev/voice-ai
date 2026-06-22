"""Environment-driven configuration for the voice agent.

Everything that differs between local (CPU, in-Docker) and production (GPU,
cloud) is an env var so the same image runs in both places.
"""

import os

DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, concise voice assistant. "
    "Your responses are spoken aloud, so reply in short, natural, conversational "
    "sentences. Do not use markdown, lists, code blocks, emojis, or special "
    "characters. Keep answers to one or two sentences unless the user asks for more."
)


class Config:
    def __init__(self) -> None:
        # --- LLM brain. Provider is pluggable: Claude (Anthropic API) or a
        # self-hosted OpenAI-compatible server such as Ollama (e.g. Mistral). ---
        self.llm_provider = os.getenv("LLM_PROVIDER", "anthropic")  # anthropic | ollama
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        # claude-opus-4-8 (best), claude-sonnet-4-6 (balanced), claude-haiku-4-5 (fastest);
        # for ollama use a local tag like "mistral:7b".
        self.llm_model = os.getenv("LLM_MODEL", "claude-opus-4-8")
        self.system_prompt = os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
        # Always wrap the role prompt with fixed guardrails (prompt-injection
        # protection). The admin sets the role; guardrails can't be edited away.
        self.enforce_guardrails = os.getenv("ENFORCE_GUARDRAILS", "true").lower() == "true"
        # Ollama (and any OpenAI-compatible endpoint). From inside Docker, the
        # host's Ollama is reachable at host.docker.internal.
        self.ollama_base_url = os.getenv(
            "OLLAMA_BASE_URL", "http://host.docker.internal:11434"
        )

        # --- STT (self-hosted faster-whisper, in-process) ---
        # "base.en" on CPU locally; switch to "small"/"medium" + cuda on GPU nodes.
        self.whisper_model = os.getenv("WHISPER_MODEL", "base.en")
        self.whisper_device = os.getenv("WHISPER_DEVICE", "cpu")  # cpu | cuda | auto
        self.whisper_compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

        # --- TTS (self-hosted Piper HTTP server, separate container) ---
        self.tts_base_url = os.getenv("TTS_BASE_URL", "http://tts:5000")
        self.tts_voice = os.getenv("TTS_VOICE", "en_US-ryan-high")

        # --- Infra (both optional; agent degrades gracefully if unset/unreachable) ---
        self.redis_url = os.getenv("REDIS_URL", "")
        self.database_url = os.getenv("DATABASE_URL", "")

        # --- Call audio recording (stored server-side; metadata in Postgres) ---
        self.record_audio = os.getenv("RECORD_AUDIO", "true").lower() == "true"
        self.recordings_dir = os.getenv("RECORDINGS_DIR", "/data/recordings")

        # --- Verification gate (one-time code before a call can start) ---
        # A caller proves ownership of an email, an order's email, or a phone
        # before the voice session opens. Codes are short-lived and single-use.
        self.app_name = os.getenv("APP_NAME", "Voice AI")
        self.otp_length = int(os.getenv("OTP_LENGTH", "6"))
        self.otp_ttl_seconds = int(os.getenv("OTP_TTL_SECONDS", "600"))  # 10 min
        self.otp_max_attempts = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
        # How long after verifying a user may start their call ("start any time").
        self.ticket_ttl_seconds = int(os.getenv("TICKET_TTL_SECONDS", "3600"))
        # Seed a few demo orders (id:email[:phone],...) so the "order id" channel
        # works out of the box. Real deployments back this with their own data.
        self.orders_seed = os.getenv(
            "ORDERS_SEED",
            "ORD-1001:customer@example.com:+15555550123,"
            "ORD-1002:buyer@example.com",
        )

        # Email delivery (SMTP). If unset, codes are logged instead (dev mode).
        self.smtp_host = os.getenv("SMTP_HOST", "")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
        self.smtp_from = os.getenv("SMTP_FROM", "no-reply@voice.local")

        # SMS delivery (Twilio REST). If unset, codes are logged instead (dev mode).
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.twilio_from_number = os.getenv("TWILIO_FROM_NUMBER", "")

        # --- Admin console (live config, call monitoring, logs) ---
        # Set ADMIN_TOKEN to protect /admin/*. If unset, a dev default is used
        # and a warning is logged — fine for local, set a real one in prod.
        self.admin_token = os.getenv("ADMIN_TOKEN", "")
        self.admin_dev_token = "voice-admin"

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def sms_configured(self) -> bool:
        return bool(
            self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_from_number
        )

    @property
    def dev_mode(self) -> bool:
        """No real delivery configured -> expose codes for local testing."""
        return not (self.email_configured or self.sms_configured)

    @property
    def admin_token_effective(self) -> str:
        return self.admin_token or self.admin_dev_token

    @property
    def admin_token_is_default(self) -> bool:
        return not self.admin_token


config = Config()
