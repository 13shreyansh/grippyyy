"""
Session Store — Persistent conversation state for Grippy.

Provides a Redis-backed session store with automatic fallback to
in-memory storage when Redis is unavailable (development mode).

Sessions are serialized as JSON and stored with a configurable TTL.
"""

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))  # 24 hours

# ──────────────────────────────────────────────────────────────────────
# Redis Connection (lazy init)
# ──────────────────────────────────────────────────────────────────────

_redis_client = None
_redis_available = None  # None = not checked yet


def _get_redis():
    """Get or create Redis client. Returns None if Redis is unavailable."""
    global _redis_client, _redis_available

    if _redis_available is False:
        return None

    if _redis_client is not None:
        return _redis_client

    try:
        import redis
        client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        client.ping()
        _redis_client = client
        _redis_available = True
        logger.info("Redis connected at %s", REDIS_URL)
        return client
    except Exception as exc:
        _redis_available = False
        logger.warning("Redis unavailable (%s), using in-memory fallback", exc)
        return None


# ──────────────────────────────────────────────────────────────────────
# In-Memory Fallback
# ──────────────────────────────────────────────────────────────────────

_memory_store: dict[str, str] = {}


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def save_session(session_id: str, state_dict: dict[str, Any]) -> None:
    """Save a session state dict."""
    key = f"grippy:session:{session_id}"
    data = json.dumps(state_dict, default=str)

    client = _get_redis()
    if client:
        try:
            client.setex(key, SESSION_TTL, data)
            return
        except Exception as exc:
            logger.warning("Redis save failed: %s, falling back to memory", exc)

    _memory_store[key] = data


def load_session(session_id: str) -> Optional[dict[str, Any]]:
    """Load a session state dict. Returns None if not found."""
    key = f"grippy:session:{session_id}"

    client = _get_redis()
    if client:
        try:
            data = client.get(key)
            if data:
                return json.loads(data)
            return None
        except Exception as exc:
            logger.warning("Redis load failed: %s, falling back to memory", exc)

    data = _memory_store.get(key)
    if data:
        return json.loads(data)
    return None


def delete_session(session_id: str) -> bool:
    """Delete a session. Returns True if it existed."""
    key = f"grippy:session:{session_id}"

    client = _get_redis()
    if client:
        try:
            return bool(client.delete(key))
        except Exception as exc:
            logger.warning("Redis delete failed: %s", exc)

    return _memory_store.pop(key, None) is not None


def session_exists(session_id: str) -> bool:
    """Check if a session exists."""
    key = f"grippy:session:{session_id}"

    client = _get_redis()
    if client:
        try:
            return bool(client.exists(key))
        except Exception:
            pass

    return key in _memory_store


def get_store_status() -> dict[str, Any]:
    """Return store status for health checks."""
    client = _get_redis()
    if client:
        try:
            info = client.info("memory")
            return {
                "backend": "redis",
                "status": "ok",
                "used_memory": info.get("used_memory_human", "unknown"),
            }
        except Exception:
            pass

    return {
        "backend": "memory",
        "status": "ok",
        "active_sessions": len(_memory_store),
    }
