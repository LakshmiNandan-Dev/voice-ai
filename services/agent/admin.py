"""Admin console API: live configuration, call monitoring, logs.

Mounted under /admin on the agent's FastAPI app. Every route requires the admin
token, passed as a `?token=` query param (uniform across GET/PUT/POST, so no
custom CORS headers and no preflight on reads). Set ADMIN_TOKEN in the
environment; a dev default is used otherwise (and flagged in /admin/config).

Config changes are written to the runtime settings store and take effect on the
next call (see settings.py / pipeline.py).
"""

from __future__ import annotations

import secrets

import os
import re

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel

import logbuffer
from auth import EMAIL_RE, create_and_send, mask_email, mask_phone, resend
from config import config
from email_config import email_config
from personas import PERSONAS
from providers import (
    PROVIDERS,
    effective_base_url,
    env_key_present,
    preset,
)
from settings import EDITABLE_KEYS, settings


def require_admin(token: str = Query("")) -> None:
    if not secrets.compare_digest(token, config.admin_token_effective):
        raise HTTPException(401, "Invalid admin token.")


class ConfigPatch(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    agent_persona: str | None = None
    system_prompt: str | None = None
    tts_voice: str | None = None
    whisper_model: str | None = None
    memory_enabled: str | None = None
    memory_max_messages: str | None = None
    history_max_messages: str | None = None


class CreateInvitation(BaseModel):
    method: str  # "email" | "order" | "phone"
    value: str
    label: str | None = None
    agent_slug: str | None = None


class AgentBody(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    agent_persona: str | None = None
    system_prompt: str | None = None
    tts_voice: str | None = None
    whisper_model: str | None = None
    memory_enabled: str | None = None
    memory_max_messages: str | None = None
    history_max_messages: str | None = None


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "agent"


def _mask_caller(caller_key: str | None) -> str | None:
    if not caller_key:
        return None
    return mask_email(caller_key) if "@" in caller_key else mask_phone(caller_key)


def _call_view(c: dict) -> dict:
    c = {**c}
    c["caller"] = _mask_caller(c.pop("caller_key", None))
    return c


class EmailSettings(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool | None = None


class TestEmail(BaseModel):
    to: str


def _invitation_view(inv: dict, dev_mode: bool) -> dict:
    """Hide the raw code unless we're in dev mode (no real delivery configured)."""
    out = {**inv}
    if not dev_mode:
        out.pop("code", None)
    return out


async def _ollama_models() -> dict:
    """Best-effort list of models on the configured Ollama server."""
    url = f"{config.ollama_base_url.rstrip('/')}/api/tags"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200:
                    return {"reachable": False, "models": []}
                data = await r.json()
        return {
            "reachable": True,
            "models": [m.get("name") for m in data.get("models", []) if m.get("name")],
        }
    except Exception:
        return {"reachable": False, "models": []}


def build_admin_router(persistence) -> APIRouter:
    async def ensure_db() -> None:
        # Idempotent: connects Postgres/Redis (and creates tables) on first use,
        # so admin reads work even if no call/verify has happened yet.
        await persistence.connect()

    router = APIRouter(
        prefix="/admin",
        tags=["admin"],
        dependencies=[Depends(require_admin), Depends(ensure_db)],
    )

    @router.get("/ping")
    async def ping():
        return {"ok": True, "admin_token_is_default": config.admin_token_is_default}

    def _safe_settings(eff: dict) -> dict:
        """Never return the raw API key; expose only whether one is set."""
        safe = {**eff}
        safe["llm_api_key"] = ""  # write-only
        return safe

    @router.get("/config")
    async def get_config():
        eff = await settings.get()
        active = eff.get("llm_provider", "anthropic")
        override_set = bool(eff.get("llm_api_key"))
        ollama = await _ollama_models()

        def configured(pid: str) -> bool:
            p = preset(pid)
            if pid == "ollama":
                return ollama["reachable"]
            if p["env_key"]:
                return env_key_present(pid) or (pid == active and override_set)
            return True  # keyless OpenAI-compatible server (vllm/custom)

        providers = []
        for pid, p in PROVIDERS.items():
            providers.append(
                {
                    "id": pid,
                    "label": p["label"],
                    "kind": p["kind"],
                    "configured": configured(pid),
                    "needs_base_url": p["needs_base_url"],
                    "base_url": effective_base_url(pid, "") if pid != "ollama" else config.ollama_base_url,
                    "key_env": p["env_key"],
                    "models": ollama["models"] if pid == "ollama" else p["models"],
                }
            )

        personas = [
            {"id": pid, "label": p["label"], "description": p["description"], "prompt": p["prompt"]}
            for pid, p in PERSONAS.items()
        ]

        return {
            "settings": _safe_settings(eff),
            "llm_api_key_set": override_set,
            "editable_keys": list(EDITABLE_KEYS),
            "guardrails_enabled": config.enforce_guardrails,
            "providers": providers,
            "personas": personas,
            "whisper": {
                "device": config.whisper_device,
                "compute_type": config.whisper_compute_type,
            },
            "admin_token_is_default": config.admin_token_is_default,
        }

    @router.post("/config")
    async def put_config(patch: ConfigPatch):
        data = {k: v for k, v in patch.model_dump().items() if v is not None}
        if not data:
            raise HTTPException(400, "No editable fields provided.")

        # Validate the *resulting* config so we don't persist something unusable:
        # an OpenAI-compatible provider that needs a base URL must have one.
        merged = {**(await settings.get()), **data}
        pid = merged.get("llm_provider", "anthropic")
        if preset(pid)["needs_base_url"] and not effective_base_url(pid, merged.get("llm_base_url", "")):
            raise HTTPException(400, f"Provider '{pid}' needs a base URL (e.g. http://host:port/v1).")

        try:
            eff = await settings.update(data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"settings": _safe_settings(eff), "llm_api_key_set": bool(eff.get("llm_api_key"))}

    @router.post("/config/reset")
    async def reset_config():
        return {"settings": _safe_settings(await settings.reset()), "llm_api_key_set": False}

    @router.get("/invitations")
    async def list_invitations():
        dev = await email_config.dev_mode()
        invites = await persistence.list_invitations(limit=200)
        return {"invitations": [_invitation_view(i, dev) for i in invites], "dev_mode": dev}

    @router.post("/invitations")
    async def create_invitation(body: CreateInvitation):
        inv = await create_and_send(
            persistence, body.method, body.value, body.label, "admin", body.agent_slug
        )
        return {"invitation": _invitation_view(inv, await email_config.dev_mode())}

    @router.post("/invitations/{invitation_id}/reenable")
    async def reenable_invitation(invitation_id: int):
        inv = await resend(persistence, invitation_id)
        return {"invitation": _invitation_view(inv, await email_config.dev_mode())}

    @router.post("/invitations/{invitation_id}/revoke")
    async def revoke_invitation(invitation_id: int):
        ok = await persistence.revoke_invitation(invitation_id)
        if not ok:
            raise HTTPException(404, "Invitation not found.")
        return {"ok": True}

    # --- Agents: a library of call configurations you can grow over time ---
    @router.get("/agents")
    async def list_agents():
        return {"agents": await persistence.list_agents()}

    @router.post("/agents")
    async def create_agent(body: AgentBody):
        if not body.name:
            raise HTTPException(400, "Agent needs a name.")
        # Unique-ish slug.
        base = _slugify(body.name)
        existing = {a["slug"] for a in await persistence.list_agents()}
        slug, n = base, 2
        while slug in existing:
            slug, n = f"{base}-{n}", n + 1
        fields = {k: v for k, v in body.model_dump().items() if k not in ("name", "description", "enabled")}
        try:
            agent = await persistence.create_agent(slug, body.name, body.description, fields)
        except Exception:
            raise HTTPException(500, "Could not create agent (database unavailable?).")
        if not agent:
            raise HTTPException(503, "Could not create agent.")
        return {"agent": agent}

    @router.post("/agents/{agent_id}")
    async def update_agent(agent_id: int, body: AgentBody):
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        if "agent_persona" in patch and patch["agent_persona"] not in PERSONAS:
            raise HTTPException(400, "Unknown agent_persona.")
        agent = await persistence.update_agent(agent_id, patch)
        if not agent:
            raise HTTPException(404, "Agent not found.")
        return {"agent": agent}

    @router.post("/agents/{agent_id}/default")
    async def set_default_agent(agent_id: int):
        if not await persistence.set_default_agent(agent_id):
            raise HTTPException(404, "Agent not found.")
        return {"ok": True}

    @router.post("/agents/{agent_id}/delete")
    async def delete_agent(agent_id: int):
        if not await persistence.delete_agent(agent_id):
            raise HTTPException(404, "Agent not found.")
        return {"ok": True}

    # --- Email account (SMTP) used to send invitation codes ---
    @router.get("/email")
    async def get_email():
        return await email_config.public()

    @router.post("/email")
    async def set_email(body: EmailSettings):
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            return await email_config.update(data)
        except Exception as e:
            raise HTTPException(400, f"Could not save email settings: {e}")

    @router.post("/email/test")
    async def test_email(body: TestEmail):
        if not await email_config.is_configured():
            raise HTTPException(400, "Set and save an SMTP host first.")
        if not EMAIL_RE.match((body.to or "").strip()):
            raise HTTPException(400, "Enter a valid recipient email.")
        try:
            await email_config.send(
                body.to.strip(),
                f"{config.app_name} test email",
                "This is a test from your Voice AI admin console. SMTP is working.",
            )
        except Exception as e:
            raise HTTPException(502, f"Send failed: {e}")
        return {"ok": True, "sent_to": body.to.strip()}

    @router.get("/sessions")
    async def get_sessions():
        return {
            "active": await persistence.active_count(),
            "sessions": await persistence.list_sessions(limit=100),
        }

    @router.get("/transcripts")
    async def get_transcripts(
        session_id: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ):
        return {"turns": await persistence.recent_transcripts(session_id, limit)}

    @router.get("/calls")
    async def get_calls():
        calls = await persistence.list_calls(limit=100)
        return {"calls": [_call_view(c) for c in calls], "active": await persistence.active_count()}

    @router.get("/calls/{session_id}")
    async def get_call(session_id: str):
        call = await persistence.get_call(session_id)
        if not call:
            raise HTTPException(404, "Call not found.")
        return {"call": _call_view(call)}

    @router.get("/recordings")
    async def get_recordings(session_id: str | None = Query(None)):
        return {"recordings": await persistence.list_recordings(session_id, limit=200)}

    @router.get("/recordings/{recording_id}/audio")
    async def get_recording_audio(recording_id: int):
        path = await persistence.get_recording_path(recording_id)
        if not path or not os.path.exists(path):
            raise HTTPException(404, "Recording not found.")
        return FileResponse(path, media_type="audio/wav", filename=os.path.basename(path))

    @router.get("/logs")
    async def get_logs(
        limit: int = Query(200, ge=1, le=1000),
        level: str | None = Query(None),
    ):
        return {"logs": logbuffer.get(limit=limit, level=level)}

    @router.get("/stats")
    async def get_stats():
        eff = await settings.get()
        return {
            "active_sessions": await persistence.active_count(),
            "provider": eff["llm_provider"],
            "model": eff["llm_model"],
        }

    return router


def mount_admin(app, persistence) -> None:
    if config.admin_token_is_default:
        logger.warning(
            "admin: ADMIN_TOKEN not set — using dev default 'voice-admin'. "
            "Set ADMIN_TOKEN to secure the admin console."
        )
    app.include_router(build_admin_router(persistence))
    logger.info("admin: console API mounted at /admin")
