"""In-memory ring buffer of recent log records, for the admin log viewer.

A loguru sink appends structured records to a bounded deque so the admin API can
serve recent logs/debug output without shipping a full logging stack. This is
best-effort observability for one process; production should also scrape stdout
(the sink doesn't replace normal logging — loguru still writes to the console).
"""

from __future__ import annotations

from collections import deque
from threading import Lock

_BUFFER: deque[dict] = deque(maxlen=1000)
_LOCK = Lock()


def _sink(message) -> None:
    r = message.record
    rec = {
        "ts": r["time"].isoformat(timespec="milliseconds"),
        "level": r["level"].name,
        "module": r["name"],
        "message": r["message"],
    }
    with _LOCK:
        _BUFFER.append(rec)


def install(logger) -> None:
    """Attach the ring-buffer sink to loguru. Safe to call once at startup."""
    logger.add(_sink, level="DEBUG", enqueue=False, backtrace=False, diagnose=False)


def get(limit: int = 200, level: str | None = None) -> list[dict]:
    """Most-recent-last records, optionally filtered to a minimum severity."""
    with _LOCK:
        items = list(_BUFFER)
    if level:
        order = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "SUCCESS": 25, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        floor = order.get(level.upper(), 0)
        items = [x for x in items if order.get(x["level"], 0) >= floor]
    return items[-limit:]


def clear() -> None:
    with _LOCK:
        _BUFFER.clear()
