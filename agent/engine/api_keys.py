"""
API Key Management — Phase 8: B2B API.

Provides API key generation, validation, and rate limiting for
external developers and business partners.

Features:
  - Generate API keys tied to user accounts
  - Rate limiting per key (configurable)
  - Usage tracking and analytics
  - Key revocation
"""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, "grippy_api_keys.db")

# Rate limiting defaults
DEFAULT_RATE_LIMIT = int(os.environ.get("API_DEFAULT_RATE_LIMIT", "100"))  # per hour
DEFAULT_DAILY_LIMIT = int(os.environ.get("API_DEFAULT_DAILY_LIMIT", "1000"))


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_api_keys_db() -> None:
    """Initialize the API keys database."""
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                name TEXT DEFAULT '',
                tier TEXT DEFAULT 'free'
                    CHECK(tier IN ('free','pro','enterprise')),
                rate_limit_per_hour INTEGER DEFAULT 100,
                daily_limit INTEGER DEFAULT 1000,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                last_used_at TEXT,
                total_requests INTEGER DEFAULT 0,
                total_successes INTEGER DEFAULT 0,
                total_failures INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS api_usage_log (
                id TEXT PRIMARY KEY,
                api_key_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                method TEXT DEFAULT 'POST',
                status_code INTEGER DEFAULT 200,
                response_time_ms REAL DEFAULT 0,
                request_data_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
            );

            CREATE INDEX IF NOT EXISTS idx_api_usage_key
                ON api_usage_log(api_key_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_api_keys_user
                ON api_keys(user_id);
            CREATE INDEX IF NOT EXISTS idx_api_keys_hash
                ON api_keys(key_hash);
        """)
        conn.commit()
    finally:
        conn.close()


# Initialize on import
init_api_keys_db()


# ── Key Generation ────────────────────────────────────────────────────

def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key(
    user_id: str,
    name: str = "",
    tier: str = "free",
) -> dict[str, Any]:
    """
    Generate a new API key for a user.

    Returns the full key (only shown once) and metadata.
    """
    key_id = str(uuid.uuid4())
    raw_key = f"gpy_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)
    key_prefix = raw_key[:12] + "..."

    rate_limit = DEFAULT_RATE_LIMIT
    daily_limit = DEFAULT_DAILY_LIMIT

    if tier == "pro":
        rate_limit = 500
        daily_limit = 5000
    elif tier == "enterprise":
        rate_limit = 5000
        daily_limit = 50000

    conn = _get_db()
    try:
        conn.execute(
            """INSERT INTO api_keys
               (id, user_id, key_hash, key_prefix, name, tier,
                rate_limit_per_hour, daily_limit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key_id, user_id, key_hash, key_prefix, name, tier,
             rate_limit, daily_limit),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("API key generated: %s for user %s [%s]", key_prefix, user_id, tier)

    return {
        "success": True,
        "api_key": raw_key,  # Only returned once!
        "key_id": key_id,
        "key_prefix": key_prefix,
        "tier": tier,
        "rate_limit_per_hour": rate_limit,
        "daily_limit": daily_limit,
        "message": "Save this API key securely — it will not be shown again.",
    }


# ── Key Validation ────────────────────────────────────────────────────

def validate_api_key(api_key: str) -> dict[str, Any] | None:
    """
    Validate an API key and return its metadata.
    Returns None if the key is invalid or revoked.
    """
    key_hash = _hash_key(api_key)

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (key_hash,),
        ).fetchone()

        if not row:
            return None

        key_data = dict(row)

        # Check rate limit
        one_hour_ago = datetime.utcnow().replace(
            microsecond=0
        ).isoformat().replace("T", " ")
        # Approximate: count requests in the last hour
        hourly_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM api_usage_log
               WHERE api_key_id = ? AND created_at > ?""",
            (key_data["id"], one_hour_ago),
        ).fetchone()["cnt"]

        if hourly_count >= key_data["rate_limit_per_hour"]:
            return {
                **key_data,
                "rate_limited": True,
                "hourly_usage": hourly_count,
            }

        # Check daily limit
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat().replace("T", " ")
        daily_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM api_usage_log
               WHERE api_key_id = ? AND created_at > ?""",
            (key_data["id"], today_start),
        ).fetchone()["cnt"]

        if daily_count >= key_data["daily_limit"]:
            return {
                **key_data,
                "rate_limited": True,
                "daily_usage": daily_count,
            }

        # Update last_used_at
        conn.execute(
            "UPDATE api_keys SET last_used_at = datetime('now'), total_requests = total_requests + 1 WHERE id = ?",
            (key_data["id"],),
        )
        conn.commit()

        key_data["rate_limited"] = False
        key_data["hourly_usage"] = hourly_count
        key_data["daily_usage"] = daily_count
        return key_data

    finally:
        conn.close()


def log_api_usage(
    api_key_id: str,
    endpoint: str,
    method: str = "POST",
    status_code: int = 200,
    response_time_ms: float = 0,
    request_data: dict[str, Any] | None = None,
) -> None:
    """Log an API request for analytics."""
    conn = _get_db()
    try:
        conn.execute(
            """INSERT INTO api_usage_log
               (id, api_key_id, endpoint, method, status_code,
                response_time_ms, request_data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                api_key_id,
                endpoint,
                method,
                status_code,
                response_time_ms,
                json.dumps(request_data or {}),
            ),
        )

        # Update success/failure counts
        if 200 <= status_code < 400:
            conn.execute(
                "UPDATE api_keys SET total_successes = total_successes + 1 WHERE id = ?",
                (api_key_id,),
            )
        else:
            conn.execute(
                "UPDATE api_keys SET total_failures = total_failures + 1 WHERE id = ?",
                (api_key_id,),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("Failed to log API usage: %s", exc)
    finally:
        conn.close()


# ── Key Management ────────────────────────────────────────────────────

def list_api_keys(user_id: str) -> list[dict[str, Any]]:
    """List all API keys for a user (without the actual key)."""
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT id, key_prefix, name, tier, rate_limit_per_hour,
                      daily_limit, is_active, created_at, last_used_at,
                      total_requests, total_successes, total_failures
               FROM api_keys WHERE user_id = ?
               ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_api_key(key_id: str, user_id: str) -> bool:
    """Revoke an API key."""
    conn = _get_db()
    try:
        result = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE id = ? AND user_id = ?",
            (key_id, user_id),
        )
        conn.commit()
        revoked = result.rowcount > 0
        if revoked:
            logger.info("API key revoked: %s", key_id)
        return revoked
    finally:
        conn.close()


def get_api_key_usage(
    key_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get usage history for an API key."""
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT endpoint, method, status_code, response_time_ms, created_at
               FROM api_usage_log
               WHERE api_key_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (key_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_api_stats() -> dict[str, Any]:
    """Get aggregate API statistics."""
    conn = _get_db()
    try:
        total_keys = conn.execute(
            "SELECT COUNT(*) as cnt FROM api_keys"
        ).fetchone()["cnt"]
        active_keys = conn.execute(
            "SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = 1"
        ).fetchone()["cnt"]
        total_requests = conn.execute(
            "SELECT COALESCE(SUM(total_requests), 0) as cnt FROM api_keys"
        ).fetchone()["cnt"]

        return {
            "total_keys": total_keys,
            "active_keys": active_keys,
            "total_requests": total_requests,
        }
    finally:
        conn.close()
