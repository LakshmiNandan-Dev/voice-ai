"""Verification gate: a one-time code a caller must enter before a voice call.

Flow:
  1. POST /auth/request  {method: email|order|phone, value}
       -> resolve a destination, mint a numeric code, deliver it (email/SMS),
          return an opaque `challenge_id` (never the code).
  2. POST /auth/verify   {challenge_id, code}
       -> on match, mint a short-lived `ticket`.
  3. The browser starts the call at POST /start?ticket=...; `AuthGate` (an ASGI
     middleware) rejects /start without a live ticket, so the gate is enforced
     server-side, not just in the UI.

Codes and tickets live in Redis when available, else in an in-process store, so
the gate works locally with zero extra infrastructure. Delivery falls back to
logging the code when no SMTP/Twilio provider is configured ("dev mode").
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time

import aiohttp
from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import config
from email_config import email_config

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")  # loose E.164


# --------------------------------------------------------------------------- #
# Short-lived key/value store (Redis when available, else in-process).
# --------------------------------------------------------------------------- #
class CodeStore:
    """JSON values with TTL. Each value carries its own absolute `exp` so a
    rewrite (e.g. bumping the attempt counter) preserves the original expiry."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis = None
        self._mem: dict[str, tuple[float, dict]] = {}

    async def connect(self) -> None:
        if not self._redis_url:
            logger.info("auth: no REDIS_URL, using in-process code store")
            return
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("auth: Redis code store connected")
        except Exception as e:
            logger.warning(f"auth: Redis unavailable, using in-process store: {e}")
            self._redis = None

    async def put(self, key: str, obj: dict, ttl: int) -> None:
        obj = {**obj, "exp": time.time() + ttl}
        await self._write(key, obj)

    async def rewrite(self, key: str, obj: dict) -> None:
        await self._write(key, obj)

    async def _write(self, key: str, obj: dict) -> None:
        ttl = max(1, int(obj.get("exp", time.time()) - time.time()))
        if self._redis:
            import json

            await self._redis.set(key, json.dumps(obj), ex=ttl)
        else:
            self._mem[key] = (obj["exp"], obj)

    async def get(self, key: str) -> dict | None:
        if self._redis:
            import json

            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        item = self._mem.get(key)
        if not item:
            return None
        exp, obj = item
        if exp < time.time():
            self._mem.pop(key, None)
            return None
        return obj

    async def delete(self, key: str) -> None:
        if self._redis:
            await self._redis.delete(key)
        else:
            self._mem.pop(key, None)


# --------------------------------------------------------------------------- #
# Delivery. Email goes through email_config (runtime SMTP); SMS via Twilio env.
# Both fall back to logging the code when unconfigured ("dev mode").
# --------------------------------------------------------------------------- #
async def send_sms(to: str, body: str) -> None:
    if not config.sms_configured:
        logger.warning(f"[dev] sms to {to}: {body}")
        return
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{config.twilio_account_sid}/Messages.json"
    )
    data = {"From": config.twilio_from_number, "To": to, "Body": body}
    auth = aiohttp.BasicAuth(config.twilio_account_sid, config.twilio_auth_token)
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=data, auth=auth) as resp:
            if resp.status >= 300:
                text = await resp.text()
                raise RuntimeError(f"Twilio {resp.status}: {text}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gen_code(length: int) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def mask_email(addr: str) -> str:
    try:
        local, domain = addr.split("@", 1)
    except ValueError:
        return "•••"
    if len(local) <= 2:
        shown = local[0] + "•"
    else:
        shown = local[0] + "•" * (len(local) - 2) + local[-1]
    return f"{shown}@{domain}"


def mask_phone(num: str) -> str:
    last4 = num[-4:]
    return "•" * max(0, len(num) - 4) + last4


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #
class VerifyCode(BaseModel):
    # Identify the invitation by its link token, or by the contact it was sent to.
    token: str | None = None
    destination: str | None = None
    code: str


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
_store: CodeStore | None = None
_persistence = None
_started = False
_start_lock = asyncio.Lock()


async def _ensure_started() -> None:
    """Connect the code store and seed demo orders, exactly once. Lazy so it
    never depends on the runner's startup/lifespan style."""
    global _store, _started
    if _started:
        return
    async with _start_lock:
        if _started:
            return
        _store = CodeStore(config.redis_url)
        await _store.connect()
        if _persistence is not None:
            await _persistence.connect()
            await _persistence.seed_orders(config.orders_seed)
        _started = True
        logger.info(
            "auth: verification gate enabled "
            f"(email={'on' if config.email_configured else 'dev-log'}, "
            f"sms={'on' if config.sms_configured else 'dev-log'})"
        )


async def _resolve(method: str, value: str, persistence) -> tuple[str, str]:
    """Map (method, value) -> (channel, destination). Raises HTTPException."""
    method = (method or "").strip().lower()
    value = (value or "").strip()
    if not value:
        raise HTTPException(400, "Please enter a value.")

    if method == "email":
        if not EMAIL_RE.match(value):
            raise HTTPException(400, "That doesn't look like a valid email address.")
        return "email", value

    if method == "phone":
        compact = re.sub(r"[\s()-]", "", value)
        if not PHONE_RE.match(compact):
            raise HTTPException(400, "Enter a phone number in international format, e.g. +15555550123.")
        return "sms", compact

    if method == "order":
        order = await persistence.lookup_order(value)
        if not order:
            raise HTTPException(404, "We couldn't find that order.")
        if order.get("email"):
            return "email", order["email"]
        if order.get("phone"):
            return "sms", order["phone"]
        raise HTTPException(422, "That order has no email or phone on file.")

    raise HTTPException(400, "Unknown verification method.")


def _mask(channel: str, destination: str) -> str:
    return mask_email(destination) if channel == "email" else mask_phone(destination)


async def _deliver(channel: str, destination: str, code: str) -> None:
    text = (
        f"Your {config.app_name} code is {code}. Use it to start your voice session. "
        f"It expires in {config.otp_ttl_seconds // 60} minutes."
    )
    if channel == "email":
        await email_config.send(destination, f"{config.app_name} session invitation", text)
    else:
        await send_sms(destination, text)


async def create_and_send(
    persistence, method: str, value: str, label, created_by: str, agent_slug=None
) -> dict:
    """Admin action: resolve a destination, create an invitation, send the code."""
    await _ensure_started()
    channel, destination = await _resolve(method, value, persistence)
    code = _gen_code(config.otp_length)
    token = secrets.token_urlsafe(16)
    inv = await persistence.create_invitation(
        channel=channel,
        destination=destination,
        destination_masked=_mask(channel, destination),
        label=label,
        token=token,
        code=code,
        ttl_seconds=config.otp_ttl_seconds,
        created_by=created_by,
        agent_slug=agent_slug or None,
    )
    if not inv:
        raise HTTPException(503, "Could not store the invitation (database unavailable).")
    try:
        await _deliver(channel, destination, code)
    except Exception as e:
        logger.error(f"auth: invitation delivery failed: {e}")
        raise HTTPException(502, "Invitation created but delivery failed. Try re-sending.")
    logger.info(f"auth: invitation {inv['id']} sent via {channel}")
    return inv


async def resend(persistence, invitation_id: int) -> dict:
    """Admin action: grant one more call with a FRESH code and re-send it."""
    await _ensure_started()
    code = _gen_code(config.otp_length)
    inv = await persistence.reenable_invitation(invitation_id, code, config.otp_ttl_seconds)
    if not inv:
        raise HTTPException(404, "Invitation not found or revoked.")
    try:
        await _deliver(inv["channel"], inv["destination"], code)
    except Exception as e:
        logger.error(f"auth: invitation resend failed: {e}")
        raise HTTPException(502, "Could not re-send the code. Try again.")
    logger.info(f"auth: invitation {invitation_id} re-enabled + new code sent")
    inv.pop("destination", None)  # don't leak the raw destination back
    return inv


def build_router(persistence) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/verify")
    async def verify_code(body: VerifyCode):
        """Invitation-only: a code must have been sent by an admin first."""
        await _ensure_started()
        token = (body.token or "").strip() or None
        destination = (body.destination or "").strip() or None
        if destination and EMAIL_RE.match(destination) is None:
            destination = re.sub(r"[\s()-]", "", destination)  # normalize phone
        if not token and not destination:
            raise HTTPException(400, "Enter the email or phone your invitation was sent to.")

        result, inv = await persistence.verify_invitation(
            code=re.sub(r"\s", "", body.code or ""),
            token=token,
            destination=destination,
            max_attempts=config.otp_max_attempts,
        )
        if result == "ok":
            ticket = secrets.token_urlsafe(24)
            await _store.put(
                f"ticket:{ticket}",
                {"invitation_id": inv["id"], "token": inv["token"]},
                config.ticket_ttl_seconds,
            )
            logger.info(f"auth: invitation {inv['id']} verified, ticket issued")
            # `agent` tells the client which agent to connect to; `cid` (the
            # invitation token) is passed back on the WS so the bot can resolve a
            # stable caller identity for cross-call memory (see bot.py).
            return {
                "ticket": ticket,
                "expires_in": config.ticket_ttl_seconds,
                "agent": inv.get("agent_slug"),
                "cid": inv.get("token"),
            }

        if result == "bad_code":
            raise HTTPException(401, f"Incorrect code. {max(0, inv)} attempts left.")
        if result == "expired":
            raise HTTPException(410, "This code has expired. Ask the admin to re-send it.")
        if result == "locked":
            raise HTTPException(429, "Too many attempts. Ask the admin to re-send a code.")
        raise HTTPException(404, "No active invitation matches that. Check with the admin.")

    return router


class AuthGate(BaseHTTPMiddleware):
    """Guard POST /start: require a live ticket AND atomically spend one call
    allowance on its invitation. Once spent, the invitation is locked until an
    admin re-enables it, so a user gets exactly the calls they were granted."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/start" and request.method == "POST":
            await _ensure_started()
            ticket = request.query_params.get("ticket", "")
            rec = await _store.get(f"ticket:{ticket}") if (ticket and _store) else None
            if not rec:
                return JSONResponse(
                    {"error": "Verification required before starting a call."},
                    status_code=401,
                )
            # Single-use: drop the ticket, then spend the call allowance atomically.
            await _store.delete(f"ticket:{ticket}")
            consumed = _persistence is not None and await _persistence.consume_call(
                rec["invitation_id"]
            )
            if not consumed:
                return JSONResponse(
                    {"error": "No call remaining on this invitation. Ask the admin to re-enable it."},
                    status_code=403,
                )
        return await call_next(request)


def mount_auth(app, persistence) -> None:
    """Mount routes + the /start gate. Call at import time: middleware can't be
    added once the ASGI app has started serving. The store connects and orders
    seed lazily on the first auth/start request (see `_ensure_started`)."""
    global _persistence
    _persistence = persistence
    app.include_router(build_router(persistence))
    app.add_middleware(AuthGate)
