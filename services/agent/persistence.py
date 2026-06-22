"""Session state (Redis) and transcript persistence (Postgres).

Both backends are optional and best-effort: if a URL is unset or a backend is
unreachable, the agent logs a warning and the voice loop keeps working. A
persistence bug must never break a live conversation, so every call is guarded.
"""

import asyncio
import hmac
import time
from datetime import datetime, timezone

from loguru import logger


def _iso(v) -> str | None:
    """ISO-8601 string from a datetime or epoch float (or None)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    return v.isoformat()


def _epoch(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return v.timestamp()

try:
    import asyncpg
except Exception:  # pragma: no cover - asyncpg should be installed
    asyncpg = None  # type: ignore

try:
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS transcripts (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    role        TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_transcripts_session ON transcripts (session_id);
-- Tag turns with a stable caller key + agent so a caller's history can be
-- reloaded on their next call (cross-call memory).
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS caller_key TEXT;
ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS agent_slug TEXT;
CREATE INDEX IF NOT EXISTS idx_transcripts_caller ON transcripts (caller_key, agent_slug);

CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    email       TEXT,
    phone       TEXT
);

-- Durable runtime configuration (admin-editable). System of record for settings.
CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Invitations are the entitlement to start a call. A user can only join with one;
-- each grants `calls_allowed` calls (default 1) and is consumed atomically at
-- call start, so once used the user is locked out until an admin re-enables it.
CREATE TABLE IF NOT EXISTS invitations (
    id                 BIGSERIAL PRIMARY KEY,
    token              TEXT UNIQUE NOT NULL,
    channel            TEXT NOT NULL,                 -- email | sms
    destination        TEXT NOT NULL,                 -- raw email/phone (for delivery)
    destination_masked TEXT NOT NULL,
    label              TEXT,                          -- optional (order id, name)
    code               TEXT,                          -- active code; NULL once used/locked
    code_expires_at    TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'sent',  -- sent | verified | consumed | revoked
    calls_allowed      INT  NOT NULL DEFAULT 1,
    calls_used         INT  NOT NULL DEFAULT 0,
    attempts           INT  NOT NULL DEFAULT 0,
    created_by         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at            TIMESTAMPTZ,
    verified_at        TIMESTAMPTZ,
    last_call_at       TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_invitations_destination ON invitations (destination);
CREATE INDEX IF NOT EXISTS idx_invitations_status ON invitations (status);

-- Named agents: a library of call configurations you can add to over time.
-- Each field overrides the global config (NULL = inherit). An invitation routes
-- a caller to one agent (see invitations.agent_slug).
CREATE TABLE IF NOT EXISTS agents (
    id            BIGSERIAL PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT,
    enabled       BOOLEAN NOT NULL DEFAULT true,
    is_default    BOOLEAN NOT NULL DEFAULT false,
    llm_provider  TEXT,
    llm_model     TEXT,
    llm_base_url  TEXT,
    llm_api_key   TEXT,
    agent_persona TEXT,
    system_prompt TEXT,
    tts_voice     TEXT,
    whisper_model TEXT,
    memory_enabled       TEXT,
    memory_max_messages  TEXT,
    history_max_messages TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Memory columns (added if the agents table predates them).
ALTER TABLE agents ADD COLUMN IF NOT EXISTS memory_enabled TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS memory_max_messages TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS history_max_messages TEXT;

-- Route an invitation to a specific agent (added if the table predates this).
ALTER TABLE invitations ADD COLUMN IF NOT EXISTS agent_slug TEXT;

-- Call audio recordings. Bytes live on disk (RECORDINGS_DIR); this is metadata.
CREATE TABLE IF NOT EXISTS recordings (
    id               BIGSERIAL PRIMARY KEY,
    session_id       TEXT NOT NULL,
    path             TEXT NOT NULL,
    sample_rate      INT,
    channels         INT,
    bytes            BIGINT,
    duration_seconds DOUBLE PRECISION,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_recordings_session ON recordings (session_id);

-- One durable row per call holding the complete conversation as text (built at
-- call end from the per-turn transcripts), so admins can review a call's text
-- alongside its audio recording.
CREATE TABLE IF NOT EXISTS calls (
    session_id   TEXT PRIMARY KEY,
    caller_key   TEXT,
    agent_slug   TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ,
    turn_count   INT NOT NULL DEFAULT 0,
    transcript   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls (started_at DESC);
"""


class Persistence:
    def __init__(self, redis_url: str, database_url: str) -> None:
        self._redis_url = redis_url
        self._database_url = database_url
        self._redis = None
        self._pool = None
        self._connected = False
        # In-memory order book; mirrors Postgres so the "order id" verification
        # channel still works when Postgres is unset/unreachable (local/demo).
        self._orders: dict[str, dict[str, str | None]] = {}
        # In-memory fallbacks so invitations/settings work without Postgres too.
        self._inv_mem: dict[int, dict] = {}
        self._inv_seq = 0
        self._inv_lock = asyncio.Lock()
        self._settings_mem: dict[str, str] = {}
        self._rec_mem: list[dict] = []
        self._rec_seq = 0
        self._agents_mem: dict[int, dict] = {}
        self._agent_seq = 0

    async def connect(self) -> None:
        """Idempotent connect. Safe to call on every new session."""
        if self._connected:
            return
        self._connected = True

        if self._redis_url and aioredis is not None:
            try:
                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
                await self._redis.ping()
                logger.info("Redis connected")
            except Exception as e:
                logger.warning(f"Redis unavailable, continuing without it: {e}")
                self._redis = None

        if self._database_url and asyncpg is not None:
            try:
                self._pool = await asyncpg.create_pool(
                    self._database_url, min_size=1, max_size=5
                )
                async with self._pool.acquire() as conn:
                    await conn.execute(CREATE_SQL)
                logger.info("Postgres connected")
            except Exception as e:
                logger.warning(f"Postgres unavailable, continuing without it: {e}")
                self._pool = None

    async def seed_orders(self, seed: str) -> None:
        """Load demo orders from a `id:email[:phone],...` string.

        Populates the in-memory book always, and upserts into Postgres when it's
        available so the data survives and is shared across replicas.
        """
        for entry in (seed or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = [p.strip() for p in entry.split(":")]
            order_id = parts[0]
            email = parts[1] if len(parts) > 1 and parts[1] else None
            phone = parts[2] if len(parts) > 2 and parts[2] else None
            if not order_id:
                continue
            self._orders[order_id] = {"email": email, "phone": phone}

        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                for oid, c in self._orders.items():
                    await conn.execute(
                        "INSERT INTO orders (order_id, email, phone) VALUES ($1, $2, $3) "
                        "ON CONFLICT (order_id) DO NOTHING",
                        oid,
                        c["email"],
                        c["phone"],
                    )
        except Exception as e:
            logger.warning(f"Postgres seed_orders failed: {e}")

    async def lookup_order(self, order_id: str) -> dict[str, str | None] | None:
        """Return {'email', 'phone'} for an order id, or None if unknown."""
        order_id = (order_id or "").strip()
        if not order_id:
            return None
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT email, phone FROM orders WHERE order_id = $1", order_id
                    )
                if row:
                    return {"email": row["email"], "phone": row["phone"]}
            except Exception as e:
                logger.warning(f"Postgres lookup_order failed: {e}")
        return self._orders.get(order_id)

    async def start_session(self, session_id: str) -> None:
        if not self._redis:
            return
        try:
            await self._redis.hset(f"session:{session_id}", mapping={"status": "active"})
            await self._redis.incr("active_sessions")
        except Exception as e:
            logger.warning(f"Redis start_session failed: {e}")

    async def end_session(self, session_id: str) -> None:
        if not self._redis:
            return
        try:
            await self._redis.hset(f"session:{session_id}", "status", "ended")
            await self._redis.decr("active_sessions")
        except Exception as e:
            logger.warning(f"Redis end_session failed: {e}")

    async def log_turn(
        self, session_id: str, role: str, content: str,
        caller_key: str | None = None, agent_slug: str | None = None,
    ) -> None:
        logger.info(f"[turn] {session_id} {role}: {content[:80]}")
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO transcripts (session_id, role, content, caller_key, agent_slug) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        session_id,
                        role,
                        content,
                        caller_key,
                        agent_slug,
                    )
            except Exception as e:
                logger.warning(f"Postgres log_turn failed: {e}")
        if self._redis:
            try:
                await self._redis.rpush(f"session:{session_id}:turns", f"{role}: {content}")
            except Exception:
                pass

    async def history_for_caller(
        self, caller_key: str, agent_slug: str | None, limit: int = 20
    ) -> list[dict]:
        """Prior user/assistant turns for a caller (+agent), oldest→newest, for
        seeding cross-call memory. Requires Postgres (returns [] otherwise)."""
        if not caller_key or not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT role, content FROM ("
                    "  SELECT id, role, content FROM transcripts "
                    "  WHERE caller_key = $1 AND ($2::text IS NULL OR agent_slug = $2) "
                    "  ORDER BY id DESC LIMIT $3"
                    ") t ORDER BY id ASC",
                    caller_key, agent_slug, limit,
                )
            return [{"role": r["role"], "content": r["content"]} for r in rows]
        except Exception as e:
            logger.warning(f"history_for_caller failed: {e}")
            return []

    async def get_invitation_by_token(self, token: str) -> dict | None:
        """Resolve an invitation's caller identity from its opaque token."""
        if not token or not self._pool:
            # in-memory fallback
            inv = next((i for i in self._inv_mem.values() if i.get("token") == token), None)
            return {"destination": inv["destination"], "agent_slug": inv.get("agent_slug")} if inv else None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT destination, agent_slug FROM invitations WHERE token = $1", token
                )
            return {"destination": row["destination"], "agent_slug": row["agent_slug"]} if row else None
        except Exception as e:
            logger.warning(f"get_invitation_by_token failed: {e}")
            return None

    # --- Read paths for the admin console (best-effort; empty if backend down) ---
    async def active_count(self) -> int:
        if not self._redis:
            return 0
        try:
            return int(await self._redis.get("active_sessions") or 0)
        except Exception:
            return 0

    async def list_sessions(self, limit: int = 50) -> list[dict]:
        """Live/recent sessions from Redis (id + status), newest-ish first."""
        if not self._redis:
            return []
        out: list[dict] = []
        try:
            async for key in self._redis.scan_iter(match="session:*", count=100):
                if key.endswith(":turns"):
                    continue
                data = await self._redis.hgetall(key)
                out.append({"session_id": key.split("session:", 1)[1], **data})
                if len(out) >= limit:
                    break
        except Exception as e:
            logger.warning(f"Redis list_sessions failed: {e}")
        # active first, then by id
        out.sort(key=lambda s: (s.get("status") != "active", s.get("session_id", "")))
        return out

    async def recent_transcripts(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                if session_id:
                    rows = await conn.fetch(
                        "SELECT session_id, role, content, created_at FROM transcripts "
                        "WHERE session_id = $1 ORDER BY id DESC LIMIT $2",
                        session_id,
                        limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT session_id, role, content, created_at FROM transcripts "
                        "ORDER BY id DESC LIMIT $1",
                        limit,
                    )
            return [
                {
                    "session_id": r["session_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Postgres recent_transcripts failed: {e}")
            return []

    # --- Durable runtime settings (app_settings) ---
    async def settings_get_all(self) -> dict[str, str]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch("SELECT key, value FROM app_settings")
                return {r["key"]: r["value"] for r in rows}
            except Exception as e:
                logger.warning(f"settings_get_all failed: {e}")
                return {}
        return dict(self._settings_mem)

    async def settings_set(self, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        for k, v in mapping.items():
                            await conn.execute(
                                "INSERT INTO app_settings (key, value) VALUES ($1, $2) "
                                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                                k,
                                v,
                            )
                return
            except Exception as e:
                logger.warning(f"settings_set failed: {e}")
        self._settings_mem.update(mapping)

    async def settings_delete(self, keys: list[str]) -> None:
        """Delete specific setting keys (e.g. reset only the agent config, leaving
        unrelated keys like SMTP intact)."""
        if not keys:
            return
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute("DELETE FROM app_settings WHERE key = ANY($1)", keys)
                return
            except Exception as e:
                logger.warning(f"settings_delete failed: {e}")
        for k in keys:
            self._settings_mem.pop(k, None)

    # --- Invitations (entitlement to start a call) ---
    def _inv_public(self, row) -> dict:
        """JSON-safe view. `code` is included raw; admin strips it unless dev mode."""
        r = dict(row)
        exp = _epoch(r.get("code_expires_at"))
        return {
            "id": r["id"],
            "token": r["token"],
            "channel": r["channel"],
            "destination_masked": r["destination_masked"],
            "label": r.get("label"),
            "agent_slug": r.get("agent_slug"),
            "status": r["status"],
            "calls_allowed": r["calls_allowed"],
            "calls_used": r["calls_used"],
            "attempts": r["attempts"],
            "has_code": r.get("code") is not None,
            "code": r.get("code"),
            "expired": exp is not None and exp < time.time(),
            "created_at": _iso(r.get("created_at")),
            "sent_at": _iso(r.get("sent_at")),
            "verified_at": _iso(r.get("verified_at")),
            "last_call_at": _iso(r.get("last_call_at")),
        }

    async def create_invitation(
        self, channel, destination, destination_masked, label, token, code,
        ttl_seconds, created_by, agent_slug=None,
    ) -> dict | None:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "INSERT INTO invitations "
                        "(token, channel, destination, destination_masked, label, code, "
                        " code_expires_at, status, sent_at, created_by, agent_slug) "
                        "VALUES ($1,$2,$3,$4,$5,$6, now()+make_interval(secs=>$7), 'sent', now(), $8, $9) "
                        "RETURNING *",
                        token, channel, destination, destination_masked, label, code,
                        float(ttl_seconds), created_by, agent_slug,
                    )
                return self._inv_public(row)
            except Exception as e:
                logger.warning(f"create_invitation failed: {e}")
                return None
        async with self._inv_lock:
            self._inv_seq += 1
            iid = self._inv_seq
            now = time.time()
            self._inv_mem[iid] = {
                "id": iid, "token": token, "channel": channel, "destination": destination,
                "destination_masked": destination_masked, "label": label, "code": code,
                "agent_slug": agent_slug,
                "code_expires_at": now + ttl_seconds, "status": "sent", "calls_allowed": 1,
                "calls_used": 0, "attempts": 0, "created_by": created_by, "created_at": now,
                "sent_at": now, "verified_at": None, "last_call_at": None,
            }
            return self._inv_public(self._inv_mem[iid])

    async def verify_invitation(self, code, token=None, destination=None, max_attempts=5):
        """Returns (result, invitation_public|None). result in
        {ok, not_found, expired, locked, bad_code, error}. On ok the code is
        consumed (single-use) and the invitation becomes 'verified'."""
        code = (code or "").strip()
        if self._pool:
            return await self._verify_pg(code, token, destination, max_attempts)
        return await self._verify_mem(code, token, destination, max_attempts)

    async def _verify_pg(self, code, token, destination, max_attempts):
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    if token:
                        row = await conn.fetchrow(
                            "SELECT * FROM invitations WHERE token=$1 FOR UPDATE", token
                        )
                    else:
                        row = await conn.fetchrow(
                            "SELECT * FROM invitations WHERE destination=$1 AND status='sent' "
                            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
                            destination,
                        )
                    if not row or row["status"] != "sent":
                        return ("not_found", None)
                    exp = row["code_expires_at"]
                    if exp is not None and exp.timestamp() < time.time():
                        return ("expired", None)
                    if row["code"] is None:
                        return ("locked", None)
                    if not hmac.compare_digest(str(row["code"]), code):
                        attempts = row["attempts"] + 1
                        if attempts >= max_attempts:
                            await conn.execute(
                                "UPDATE invitations SET attempts=$2, code=NULL, updated_at=now() WHERE id=$1",
                                row["id"], attempts,
                            )
                            return ("locked", None)
                        await conn.execute(
                            "UPDATE invitations SET attempts=$2, updated_at=now() WHERE id=$1",
                            row["id"], attempts,
                        )
                        return ("bad_code", max_attempts - attempts)
                    upd = await conn.fetchrow(
                        "UPDATE invitations SET status='verified', code=NULL, attempts=0, "
                        "verified_at=now(), updated_at=now() WHERE id=$1 RETURNING *",
                        row["id"],
                    )
                return ("ok", self._inv_public(upd))
        except Exception as e:
            logger.warning(f"verify_invitation pg failed: {e}")
            return ("error", None)

    async def _verify_mem(self, code, token, destination, max_attempts):
        async with self._inv_lock:
            inv = None
            if token:
                inv = next((i for i in self._inv_mem.values() if i["token"] == token), None)
            else:
                cands = [i for i in self._inv_mem.values()
                         if i["destination"] == destination and i["status"] == "sent"]
                inv = max(cands, key=lambda i: i["id"]) if cands else None
            if not inv or inv["status"] != "sent":
                return ("not_found", None)
            if inv["code_expires_at"] is not None and inv["code_expires_at"] < time.time():
                return ("expired", None)
            if inv["code"] is None:
                return ("locked", None)
            if not hmac.compare_digest(str(inv["code"]), code):
                inv["attempts"] += 1
                if inv["attempts"] >= max_attempts:
                    inv["code"] = None
                    return ("locked", None)
                return ("bad_code", max_attempts - inv["attempts"])
            inv["status"] = "verified"
            inv["code"] = None
            inv["attempts"] = 0
            inv["verified_at"] = time.time()
            return ("ok", self._inv_public(inv))

    async def consume_call(self, invitation_id: int) -> bool:
        """Atomically spend one call allowance. False if none left / not verified."""
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "UPDATE invitations SET calls_used=calls_used+1, last_call_at=now(), "
                        "status=CASE WHEN calls_used+1>=calls_allowed THEN 'consumed' ELSE status END, "
                        "updated_at=now() "
                        "WHERE id=$1 AND status='verified' AND calls_used<calls_allowed "
                        "RETURNING id",
                        invitation_id,
                    )
                return row is not None
            except Exception as e:
                logger.warning(f"consume_call failed: {e}")
                return False
        async with self._inv_lock:
            inv = self._inv_mem.get(invitation_id)
            if not inv or inv["status"] != "verified" or inv["calls_used"] >= inv["calls_allowed"]:
                return False
            inv["calls_used"] += 1
            inv["last_call_at"] = time.time()
            if inv["calls_used"] >= inv["calls_allowed"]:
                inv["status"] = "consumed"
            return True

    async def reenable_invitation(self, invitation_id, new_code, ttl_seconds) -> dict | None:
        """Grant one more call with a FRESH code. Returns raw destination+code+channel
        (for re-delivery) plus the public view. None if missing/revoked."""
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "UPDATE invitations SET calls_allowed=calls_used+1, code=$2, "
                        "code_expires_at=now()+make_interval(secs=>$3), status='sent', attempts=0, "
                        "sent_at=now(), updated_at=now() "
                        "WHERE id=$1 AND status<>'revoked' RETURNING *",
                        invitation_id, new_code, float(ttl_seconds),
                    )
                if not row:
                    return None
                res = self._inv_public(row)
                res["destination"] = row["destination"]
                return res
            except Exception as e:
                logger.warning(f"reenable_invitation failed: {e}")
                return None
        async with self._inv_lock:
            inv = self._inv_mem.get(invitation_id)
            if not inv or inv["status"] == "revoked":
                return None
            inv["calls_allowed"] = inv["calls_used"] + 1
            inv["code"] = new_code
            inv["code_expires_at"] = time.time() + ttl_seconds
            inv["status"] = "sent"
            inv["attempts"] = 0
            inv["sent_at"] = time.time()
            res = self._inv_public(inv)
            res["destination"] = inv["destination"]
            return res

    async def revoke_invitation(self, invitation_id: int) -> bool:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "UPDATE invitations SET status='revoked', code=NULL, updated_at=now() "
                        "WHERE id=$1 RETURNING id",
                        invitation_id,
                    )
                return row is not None
            except Exception as e:
                logger.warning(f"revoke_invitation failed: {e}")
                return False
        async with self._inv_lock:
            inv = self._inv_mem.get(invitation_id)
            if not inv:
                return False
            inv["status"] = "revoked"
            inv["code"] = None
            return True

    async def list_invitations(self, limit: int = 100) -> list[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM invitations ORDER BY id DESC LIMIT $1", limit
                    )
                return [self._inv_public(r) for r in rows]
            except Exception as e:
                logger.warning(f"list_invitations failed: {e}")
                return []
        items = sorted(self._inv_mem.values(), key=lambda x: x["id"], reverse=True)[:limit]
        return [self._inv_public(i) for i in items]

    # --- Call recordings (metadata; bytes on disk) ---
    async def add_recording(self, session_id, path, sample_rate, channels, nbytes, duration) -> dict | None:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "INSERT INTO recordings (session_id, path, sample_rate, channels, bytes, duration_seconds) "
                        "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
                        session_id, path, sample_rate, channels, nbytes, duration,
                    )
                return self._rec_public(row)
            except Exception as e:
                logger.warning(f"add_recording failed: {e}")
                return None
        self._rec_seq += 1
        rec = {
            "id": self._rec_seq, "session_id": session_id, "path": path,
            "sample_rate": sample_rate, "channels": channels, "bytes": nbytes,
            "duration_seconds": duration, "created_at": time.time(),
        }
        self._rec_mem.append(rec)
        return self._rec_public(rec)

    def _rec_public(self, row) -> dict:
        r = dict(row)
        return {
            "id": r["id"],
            "session_id": r["session_id"],
            "sample_rate": r.get("sample_rate"),
            "channels": r.get("channels"),
            "bytes": r.get("bytes"),
            "duration_seconds": r.get("duration_seconds"),
            "created_at": _iso(r.get("created_at")),
        }

    async def list_recordings(self, session_id: str | None = None, limit: int = 100) -> list[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    if session_id:
                        rows = await conn.fetch(
                            "SELECT * FROM recordings WHERE session_id=$1 ORDER BY id DESC LIMIT $2",
                            session_id, limit,
                        )
                    else:
                        rows = await conn.fetch(
                            "SELECT * FROM recordings ORDER BY id DESC LIMIT $1", limit
                        )
                return [self._rec_public(r) for r in rows]
            except Exception as e:
                logger.warning(f"list_recordings failed: {e}")
                return []
        items = [r for r in self._rec_mem if not session_id or r["session_id"] == session_id]
        return [self._rec_public(r) for r in sorted(items, key=lambda x: x["id"], reverse=True)[:limit]]

    async def get_recording_path(self, recording_id: int) -> str | None:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT path FROM recordings WHERE id=$1", recording_id)
                return row["path"] if row else None
            except Exception as e:
                logger.warning(f"get_recording_path failed: {e}")
                return None
        rec = next((r for r in self._rec_mem if r["id"] == recording_id), None)
        return rec["path"] if rec else None

    # --- Agents (library of named call configurations) ---
    _AGENT_FIELDS = (
        "llm_provider", "llm_model", "llm_base_url", "llm_api_key",
        "agent_persona", "system_prompt", "tts_voice", "whisper_model",
        "memory_enabled", "memory_max_messages", "history_max_messages",
    )

    def _agent_public(self, row) -> dict:
        r = dict(row)
        out = {
            "id": r["id"], "slug": r["slug"], "name": r["name"],
            "description": r.get("description"), "enabled": r["enabled"],
            "is_default": r["is_default"],
            "created_at": _iso(r.get("created_at")), "updated_at": _iso(r.get("updated_at")),
            "llm_api_key_set": bool(r.get("llm_api_key")),
        }
        for f in self._AGENT_FIELDS:
            if f == "llm_api_key":
                continue  # never expose the key
            out[f] = r.get(f)
        return out

    def _agent_overrides(self, agent: dict) -> dict:
        """Non-empty config fields an agent imposes on the global settings."""
        return {f: agent[f] for f in self._AGENT_FIELDS if agent.get(f) not in (None, "")}

    async def list_agents(self) -> list[dict]:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch("SELECT * FROM agents ORDER BY id")
                return [self._agent_public(r) for r in rows]
            except Exception as e:
                logger.warning(f"list_agents failed: {e}")
                return []
        return [self._agent_public(a) for a in sorted(self._agents_mem.values(), key=lambda x: x["id"])]

    async def get_agent(self, slug: str) -> dict | None:
        """Raw agent row (incl. api key) for building a call. None if unknown."""
        if not slug:
            return None
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT * FROM agents WHERE slug=$1", slug)
                return dict(row) if row else None
            except Exception as e:
                logger.warning(f"get_agent failed: {e}")
                return None
        return next((dict(a) for a in self._agents_mem.values() if a["slug"] == slug), None)

    async def create_agent(self, slug, name, description, fields: dict) -> dict | None:
        cols = {f: fields.get(f) for f in self._AGENT_FIELDS}
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "INSERT INTO agents (slug, name, description, llm_provider, llm_model, "
                        "llm_base_url, llm_api_key, agent_persona, system_prompt, tts_voice, whisper_model, "
                        "memory_enabled, memory_max_messages, history_max_messages) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING *",
                        slug, name, description, cols["llm_provider"], cols["llm_model"],
                        cols["llm_base_url"], cols["llm_api_key"], cols["agent_persona"],
                        cols["system_prompt"], cols["tts_voice"], cols["whisper_model"],
                        cols["memory_enabled"], cols["memory_max_messages"], cols["history_max_messages"],
                    )
                return self._agent_public(row)
            except Exception as e:
                logger.warning(f"create_agent failed: {e}")
                raise
        async with self._inv_lock:
            self._agent_seq += 1
            aid = self._agent_seq
            now = time.time()
            self._agents_mem[aid] = {
                "id": aid, "slug": slug, "name": name, "description": description,
                "enabled": True, "is_default": False, "created_at": now, "updated_at": now,
                **cols,
            }
            return self._agent_public(self._agents_mem[aid])

    async def update_agent(self, agent_id: int, patch: dict) -> dict | None:
        allowed = {"name", "description", "enabled", *self._AGENT_FIELDS}
        sets = {k: v for k, v in patch.items() if k in allowed}
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    if sets:
                        cols = ", ".join(f"{k}=${i+2}" for i, k in enumerate(sets))
                        row = await conn.fetchrow(
                            f"UPDATE agents SET {cols}, updated_at=now() WHERE id=$1 RETURNING *",
                            agent_id, *sets.values(),
                        )
                    else:
                        row = await conn.fetchrow("SELECT * FROM agents WHERE id=$1", agent_id)
                return self._agent_public(row) if row else None
            except Exception as e:
                logger.warning(f"update_agent failed: {e}")
                return None
        async with self._inv_lock:
            a = self._agents_mem.get(agent_id)
            if not a:
                return None
            a.update(sets)
            a["updated_at"] = time.time()
            return self._agent_public(a)

    async def delete_agent(self, agent_id: int) -> bool:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow("DELETE FROM agents WHERE id=$1 RETURNING id", agent_id)
                return row is not None
            except Exception as e:
                logger.warning(f"delete_agent failed: {e}")
                return False
        async with self._inv_lock:
            return self._agents_mem.pop(agent_id, None) is not None

    async def set_default_agent(self, agent_id: int) -> bool:
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("UPDATE agents SET is_default=false WHERE is_default")
                        row = await conn.fetchrow(
                            "UPDATE agents SET is_default=true, updated_at=now() WHERE id=$1 RETURNING id",
                            agent_id,
                        )
                return row is not None
            except Exception as e:
                logger.warning(f"set_default_agent failed: {e}")
                return False
        async with self._inv_lock:
            if agent_id not in self._agents_mem:
                return False
            for a in self._agents_mem.values():
                a["is_default"] = a["id"] == agent_id
            return True

    # --- Call records (one row per call; full conversation as text) ---
    async def start_call(self, session_id, caller_key=None, agent_slug=None) -> None:
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO calls (session_id, caller_key, agent_slug) VALUES ($1,$2,$3) "
                    "ON CONFLICT (session_id) DO NOTHING",
                    session_id, caller_key, agent_slug,
                )
        except Exception as e:
            logger.warning(f"start_call failed: {e}")

    async def end_call(self, session_id) -> None:
        """Assemble the complete transcript from the call's turns and store it."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT role, content FROM transcripts WHERE session_id=$1 ORDER BY id",
                    session_id,
                )
                transcript = "\n".join(
                    f"{'User' if r['role'] == 'user' else 'Agent'}: {r['content']}" for r in rows
                )
                # Upsert in case start_call didn't run (best-effort).
                await conn.execute(
                    "INSERT INTO calls (session_id, ended_at, turn_count, transcript, updated_at) "
                    "VALUES ($1, now(), $2, $3, now()) "
                    "ON CONFLICT (session_id) DO UPDATE SET "
                    "ended_at=now(), turn_count=$2, transcript=$3, updated_at=now()",
                    session_id, len(rows), transcript,
                )
        except Exception as e:
            logger.warning(f"end_call failed: {e}")

    def _call_public(self, row, with_transcript: bool = False) -> dict:
        r = dict(row)
        started, ended = _epoch(r.get("started_at")), _epoch(r.get("ended_at"))
        out = {
            "session_id": r["session_id"],
            "caller_key": r.get("caller_key"),
            "agent_slug": r.get("agent_slug"),
            "started_at": _iso(r.get("started_at")),
            "ended_at": _iso(r.get("ended_at")),
            "duration_seconds": (ended - started) if (started and ended) else None,
            "turn_count": r.get("turn_count") or 0,
            "recording_id": r.get("recording_id"),
        }
        if with_transcript:
            out["transcript"] = r.get("transcript") or ""
        return out

    async def list_calls(self, limit: int = 50) -> list[dict]:
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT c.*, "
                    "(SELECT r.id FROM recordings r WHERE r.session_id=c.session_id "
                    " ORDER BY r.id DESC LIMIT 1) AS recording_id "
                    "FROM calls c ORDER BY c.started_at DESC LIMIT $1",
                    limit,
                )
            return [self._call_public(r) for r in rows]
        except Exception as e:
            logger.warning(f"list_calls failed: {e}")
            return []

    async def get_call(self, session_id) -> dict | None:
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT c.*, "
                    "(SELECT r.id FROM recordings r WHERE r.session_id=c.session_id "
                    " ORDER BY r.id DESC LIMIT 1) AS recording_id "
                    "FROM calls c WHERE c.session_id=$1",
                    session_id,
                )
            return self._call_public(row, with_transcript=True) if row else None
        except Exception as e:
            logger.warning(f"get_call failed: {e}")
            return None

    async def close(self) -> None:
        try:
            if self._redis:
                await self._redis.aclose()
        except Exception:
            pass
        try:
            if self._pool:
                await self._pool.close()
        except Exception:
            pass
