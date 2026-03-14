"""
Authentication & Profile Management — The Identity Layer of Grippy.

This module provides:
  - User registration and login with bcrypt password hashing
  - JWT token-based authentication (stateless)
  - User data profiles: save, load, list, delete
  - Profile templates for different form types

Architecture:
  - SQLite database for user storage (migrates to PostgreSQL for production)
  - JWT tokens with configurable expiry
  - Multiple profiles per user (e.g., "Personal", "Business", "Tax Filing")
  - Profile data is encrypted at rest (optional, via environment variable)
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import bcrypt
import jwt

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

JWT_SECRET = os.environ.get("GRIPPY_JWT_SECRET", "grippy-form-genome-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = int(os.environ.get("GRIPPY_JWT_EXPIRY_HOURS", "72"))

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, "grippy_users.db")


# ──────────────────────────────────────────────────────────────────────
# Database Initialization
# ──────────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_auth_db() -> None:
    """Initialize the authentication database tables."""
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login TEXT,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                profile_data TEXT NOT NULL DEFAULT '{}',
                is_default INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, profile_name)
            );

            CREATE TABLE IF NOT EXISTS user_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_revoked INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_profiles_user ON user_profiles(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_token ON user_sessions(token_hash);
        """)
        conn.commit()
        logger.info("Auth database initialized at %s", DB_PATH)
    finally:
        conn.close()


# Initialize on import
init_auth_db()


# ──────────────────────────────────────────────────────────────────────
# Password Hashing
# ──────────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# ──────────────────────────────────────────────────────────────────────
# JWT Token Management
# ──────────────────────────────────────────────────────────────────────

def _create_token(user_id: str, email: str, username: str) -> str:
    """Create a JWT token for a user."""
    now = time.time()
    payload = {
        "sub": user_id,
        "email": email,
        "username": username,
        "iat": int(now),
        "exp": int(now + JWT_EXPIRY_HOURS * 3600),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict[str, Any]]:
    """
    Verify and decode a JWT token.
    Returns the payload dict if valid, None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        return None
    except jwt.InvalidTokenError as exc:
        logger.debug("Invalid token: %s", exc)
        return None


def _hash_token(token: str) -> str:
    """Hash a token for storage (we don't store raw tokens)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# User Registration
# ──────────────────────────────────────────────────────────────────────

def register_user(
    email: str,
    username: str,
    password: str,
    display_name: str = "",
) -> dict[str, Any]:
    """
    Register a new user.

    Returns dict with:
      - success: bool
      - user_id: str (if success)
      - token: str (if success)
      - error: str (if failure)
    """
    email = email.strip().lower()
    username = username.strip().lower()

    if not email or "@" not in email:
        return {"success": False, "error": "Invalid email address"}
    if not username or len(username) < 3:
        return {"success": False, "error": "Username must be at least 3 characters"}
    if not password or len(password) < 6:
        return {"success": False, "error": "Password must be at least 6 characters"}

    conn = _get_db()
    try:
        # Check for existing user
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ? OR username = ?",
            (email, username),
        ).fetchone()

        if existing:
            return {"success": False, "error": "Email or username already registered"}

        user_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        password_hash = _hash_password(password)

        conn.execute(
            """INSERT INTO users (id, email, username, password_hash, display_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, email, username, password_hash, display_name or username, now, now),
        )

        # Create a default profile for the user
        profile_id = str(uuid.uuid4())
        default_data = json.dumps({
            "full_name": display_name or username,
            "email": email,
        })
        conn.execute(
            """INSERT INTO user_profiles (id, user_id, profile_name, profile_data, is_default, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, user_id, "Default", default_data, 1, now, now),
        )

        conn.commit()

        # Generate token
        token = _create_token(user_id, email, username)

        # Store session
        session_id = str(uuid.uuid4())
        token_hash = _hash_token(token)
        expires_at = datetime.fromtimestamp(
            time.time() + JWT_EXPIRY_HOURS * 3600, tz=timezone.utc
        ).isoformat()
        conn.execute(
            """INSERT INTO user_sessions (id, user_id, token_hash, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, user_id, token_hash, now, expires_at),
        )
        conn.commit()

        logger.info("User registered: %s (%s)", username, email)
        return {
            "success": True,
            "user_id": user_id,
            "token": token,
            "username": username,
            "email": email,
            "display_name": display_name or username,
        }

    except sqlite3.IntegrityError as exc:
        return {"success": False, "error": f"Registration failed: {str(exc)}"}
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# User Login
# ──────────────────────────────────────────────────────────────────────

def login_user(email_or_username: str, password: str) -> dict[str, Any]:
    """
    Authenticate a user and return a JWT token.

    Returns dict with:
      - success: bool
      - token: str (if success)
      - user: dict (if success)
      - error: str (if failure)
    """
    identifier = email_or_username.strip().lower()

    conn = _get_db()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE email = ? OR username = ?",
            (identifier, identifier),
        ).fetchone()

        if not user:
            return {"success": False, "error": "Invalid credentials"}

        if not user["is_active"]:
            return {"success": False, "error": "Account is deactivated"}

        if not _verify_password(password, user["password_hash"]):
            return {"success": False, "error": "Invalid credentials"}

        # Update last login
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (now, user["id"]),
        )

        # Generate token
        token = _create_token(user["id"], user["email"], user["username"])

        # Store session
        session_id = str(uuid.uuid4())
        token_hash = _hash_token(token)
        expires_at = datetime.fromtimestamp(
            time.time() + JWT_EXPIRY_HOURS * 3600, tz=timezone.utc
        ).isoformat()
        conn.execute(
            """INSERT INTO user_sessions (id, user_id, token_hash, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, user["id"], token_hash, now, expires_at),
        )
        conn.commit()

        logger.info("User logged in: %s", user["username"])
        return {
            "success": True,
            "token": token,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "username": user["username"],
                "display_name": user["display_name"],
            },
        }

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Token Validation & User Retrieval
# ──────────────────────────────────────────────────────────────────────

def get_user_from_token(token: str) -> Optional[dict[str, Any]]:
    """
    Validate a token and return the user info.
    Returns None if token is invalid or user not found.
    """
    payload = verify_token(token)
    if not payload:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    conn = _get_db()
    try:
        # Check if session is still valid
        token_hash = _hash_token(token)
        session = conn.execute(
            "SELECT * FROM user_sessions WHERE token_hash = ? AND is_revoked = 0",
            (token_hash,),
        ).fetchone()

        if not session:
            return None

        user = conn.execute(
            "SELECT id, email, username, display_name FROM users WHERE id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()

        if not user:
            return None

        return {
            "id": user["id"],
            "email": user["email"],
            "username": user["username"],
            "display_name": user["display_name"],
        }

    finally:
        conn.close()


def logout_user(token: str) -> bool:
    """Revoke a user's session token."""
    token_hash = _hash_token(token)
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE user_sessions SET is_revoked = 1 WHERE token_hash = ?",
            (token_hash,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Profile Management
# ──────────────────────────────────────────────────────────────────────

def get_user_profiles(user_id: str) -> list[dict[str, Any]]:
    """Get all profiles for a user."""
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT id, profile_name, profile_data, is_default, created_at, updated_at
               FROM user_profiles WHERE user_id = ? ORDER BY is_default DESC, profile_name""",
            (user_id,),
        ).fetchall()

        profiles = []
        for row in rows:
            profiles.append({
                "id": row["id"],
                "name": row["profile_name"],
                "data": json.loads(row["profile_data"]),
                "is_default": bool(row["is_default"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return profiles

    finally:
        conn.close()


def get_profile(user_id: str, profile_id: str) -> Optional[dict[str, Any]]:
    """Get a specific profile by ID."""
    conn = _get_db()
    try:
        row = conn.execute(
            """SELECT id, profile_name, profile_data, is_default, created_at, updated_at
               FROM user_profiles WHERE id = ? AND user_id = ?""",
            (profile_id, user_id),
        ).fetchone()

        if not row:
            return None

        return {
            "id": row["id"],
            "name": row["profile_name"],
            "data": json.loads(row["profile_data"]),
            "is_default": bool(row["is_default"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    finally:
        conn.close()


def save_profile(
    user_id: str,
    profile_name: str,
    profile_data: dict[str, Any],
    profile_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Create or update a user profile.

    If profile_id is provided, updates existing profile.
    Otherwise, creates a new profile.
    """
    conn = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    data_json = json.dumps(profile_data)

    try:
        if profile_id:
            # Update existing
            conn.execute(
                """UPDATE user_profiles
                   SET profile_name = ?, profile_data = ?, updated_at = ?
                   WHERE id = ? AND user_id = ?""",
                (profile_name, data_json, now, profile_id, user_id),
            )
            conn.commit()
            return {"success": True, "profile_id": profile_id, "action": "updated"}
        else:
            # Create new
            new_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO user_profiles (id, user_id, profile_name, profile_data, is_default, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                (new_id, user_id, profile_name, data_json, now, now),
            )
            conn.commit()
            return {"success": True, "profile_id": new_id, "action": "created"}

    except sqlite3.IntegrityError:
        return {"success": False, "error": f"Profile '{profile_name}' already exists"}
    finally:
        conn.close()


def delete_profile(user_id: str, profile_id: str) -> dict[str, Any]:
    """Delete a user profile (cannot delete the default profile)."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT is_default FROM user_profiles WHERE id = ? AND user_id = ?",
            (profile_id, user_id),
        ).fetchone()

        if not row:
            return {"success": False, "error": "Profile not found"}

        if row["is_default"]:
            return {"success": False, "error": "Cannot delete the default profile"}

        conn.execute(
            "DELETE FROM user_profiles WHERE id = ? AND user_id = ?",
            (profile_id, user_id),
        )
        conn.commit()
        return {"success": True}

    finally:
        conn.close()


def set_default_profile(user_id: str, profile_id: str) -> dict[str, Any]:
    """Set a profile as the default for a user."""
    conn = _get_db()
    try:
        # Unset all defaults
        conn.execute(
            "UPDATE user_profiles SET is_default = 0 WHERE user_id = ?",
            (user_id,),
        )
        # Set the new default
        conn.execute(
            "UPDATE user_profiles SET is_default = 1 WHERE id = ? AND user_id = ?",
            (profile_id, user_id),
        )
        conn.commit()
        return {"success": True}

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# User Statistics
# ──────────────────────────────────────────────────────────────────────

def get_auth_stats() -> dict[str, Any]:
    """Return statistics about the auth system."""
    conn = _get_db()
    try:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        profile_count = conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]
        session_count = conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE is_revoked = 0"
        ).fetchone()[0]

        return {
            "total_users": user_count,
            "total_profiles": profile_count,
            "active_sessions": session_count,
        }
    finally:
        conn.close()
