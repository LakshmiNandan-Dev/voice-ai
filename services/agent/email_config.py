"""Runtime-configurable email (SMTP) account for sending invitation codes.

Defaults come from the environment (SMTP_* in config); the admin console can
override them at runtime, stored durably in Postgres (the `app_settings` table,
shared with other settings but a separate key namespace). The password is stored
server-side and never returned by the API.

When no SMTP host is configured (env or runtime), email delivery degrades to
logging the code ("dev mode"), so the flow stays testable with zero setup.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from loguru import logger

from config import config

# Keys persisted in app_settings (distinct from the agent-config keys).
EMAIL_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "smtp_use_tls")


class EmailConfig:
    def __init__(self) -> None:
        self._p = None  # Persistence, bound at startup

    def bind(self, persistence) -> None:
        self._p = persistence

    def _env_defaults(self) -> dict:
        return {
            "smtp_host": config.smtp_host,
            "smtp_port": str(config.smtp_port),
            "smtp_user": config.smtp_user,
            "smtp_password": config.smtp_password,
            "smtp_from": config.smtp_from,
            "smtp_use_tls": "true" if config.smtp_use_tls else "false",
        }

    async def get(self) -> dict:
        """Effective SMTP config: env defaults overlaid with runtime overrides."""
        eff = self._env_defaults()
        if self._p is not None:
            stored = await self._p.settings_get_all()
            for k in EMAIL_KEYS:
                if stored.get(k) not in (None, ""):
                    eff[k] = stored[k]
        return eff

    async def public(self) -> dict:
        """Safe view for the admin UI (password never returned)."""
        eff = await self.get()
        return {
            "smtp_host": eff["smtp_host"],
            "smtp_port": int(eff["smtp_port"] or 0),
            "smtp_user": eff["smtp_user"],
            "smtp_from": eff["smtp_from"],
            "smtp_use_tls": str(eff["smtp_use_tls"]).lower() == "true",
            "password_set": bool(eff["smtp_password"]),
            "configured": bool(eff["smtp_host"]),
        }

    async def update(self, patch: dict) -> dict:
        """Persist provided fields. Empty password is ignored (keeps existing)."""
        if self._p is None:
            raise RuntimeError("settings store unavailable")
        clean: dict[str, str] = {}
        if "smtp_host" in patch:
            clean["smtp_host"] = str(patch["smtp_host"] or "").strip()
        if "smtp_user" in patch:
            clean["smtp_user"] = str(patch["smtp_user"] or "").strip()
        if "smtp_from" in patch:
            clean["smtp_from"] = str(patch["smtp_from"] or "").strip()
        if "smtp_port" in patch and patch["smtp_port"] not in (None, ""):
            clean["smtp_port"] = str(int(patch["smtp_port"]))
        if "smtp_use_tls" in patch and patch["smtp_use_tls"] is not None:
            clean["smtp_use_tls"] = "true" if patch["smtp_use_tls"] else "false"
        # Write-only password: only overwrite when a non-empty value is supplied.
        if patch.get("smtp_password"):
            clean["smtp_password"] = str(patch["smtp_password"])
        await self._p.settings_set(clean)
        logger.info(f"email: SMTP settings updated ({sorted(clean)})")
        return await self.public()

    async def is_configured(self) -> bool:
        return bool((await self.get())["smtp_host"])

    async def dev_mode(self) -> bool:
        """No real delivery (email or SMS) -> codes are surfaced for testing."""
        return not (await self.is_configured() or config.sms_configured)

    async def send(self, to: str, subject: str, body: str) -> None:
        """Send an email via the effective SMTP config, or log it in dev mode."""
        eff = await self.get()
        if not eff["smtp_host"]:
            logger.warning(f"[dev] email to {to}: {body}")
            return

        def _send() -> None:
            msg = EmailMessage()
            msg["From"] = eff["smtp_from"]
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(eff["smtp_host"], int(eff["smtp_port"]), timeout=15) as s:
                if str(eff["smtp_use_tls"]).lower() == "true":
                    s.starttls()
                if eff["smtp_user"]:
                    s.login(eff["smtp_user"], eff["smtp_password"])
                s.send_message(msg)

        await asyncio.to_thread(_send)


email_config = EmailConfig()
