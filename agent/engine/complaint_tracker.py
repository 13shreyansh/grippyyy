"""
Complaint Tracker — Persistent complaint lifecycle management for Grippy.

Tracks every complaint from creation to resolution, including:
  - Escalation path and current step
  - All actions taken (emails sent, forms filled, tweets posted)
  - Automated follow-up scheduling via next_action_date

Uses SQLite for MVP, designed for easy migration to PostgreSQL.
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, "grippy_complaints.db")


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


def init_complaint_db() -> None:
    """Initialize the complaint tracking database."""
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS complaints (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT DEFAULT '',
                company TEXT NOT NULL,
                industry TEXT DEFAULT '',
                issue_summary TEXT DEFAULT '',
                complaint_data_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending'
                    CHECK(status IN ('pending','email_sent','form_filed',
                                     'escalated','follow_up_scheduled',
                                     'resolved','closed')),
                current_step INTEGER DEFAULT 0,
                escalation_path_json TEXT DEFAULT '[]',
                target_url TEXT DEFAULT '',
                next_action_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS complaint_actions (
                id TEXT PRIMARY KEY,
                complaint_id TEXT NOT NULL,
                action_type TEXT NOT NULL
                    CHECK(action_type IN ('email_drafted','email_sent',
                                          'form_scanned','form_filled',
                                          'tweet_posted','escalated',
                                          'follow_up_scheduled','followup_sent','resolved')),
                target TEXT DEFAULT '',
                details_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'success'
                    CHECK(status IN ('success','failed','pending')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_complaints_session
                ON complaints(session_id);
            CREATE INDEX IF NOT EXISTS idx_complaints_status
                ON complaints(status);
            CREATE INDEX IF NOT EXISTS idx_complaints_next_action
                ON complaints(next_action_date);
            CREATE INDEX IF NOT EXISTS idx_actions_complaint
                ON complaint_actions(complaint_id);
        """)
        conn.commit()
        logger.info("Complaint database initialized at %s", DB_PATH)
    finally:
        conn.close()


# Initialize on import
init_complaint_db()


# ──────────────────────────────────────────────────────────────────────
# Complaint CRUD
# ──────────────────────────────────────────────────────────────────────

def create_complaint(
    session_id: str,
    company: str,
    industry: str = "",
    issue_summary: str = "",
    complaint_data: Optional[dict] = None,
    escalation_path: Optional[list] = None,
    target_url: str = "",
    wait_days: int = 7,
    user_id: str = "",
) -> dict[str, Any]:
    """Create a new complaint and return its details."""
    complaint_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    next_action = (datetime.now(timezone.utc) + timedelta(days=wait_days)).isoformat()

    conn = _get_db()
    try:
        conn.execute(
            """INSERT INTO complaints
               (id, session_id, user_id, company, industry, issue_summary,
                complaint_data_json, status, current_step, escalation_path_json,
                target_url, next_action_date, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
            (
                complaint_id, session_id, user_id, company, industry,
                issue_summary,
                json.dumps(complaint_data or {}),
                json.dumps(escalation_path or []),
                target_url, next_action, now, now,
            ),
        )
        conn.commit()
        logger.info("Complaint created: %s against %s", complaint_id, company)
        return {
            "id": complaint_id,
            "company": company,
            "industry": industry,
            "status": "pending",
            "current_step": 0,
            "next_action_date": next_action,
            "created_at": now,
        }
    finally:
        conn.close()


def get_complaint(complaint_id: str) -> Optional[dict[str, Any]]:
    """Get a single complaint by ID."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()
        if not row:
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def list_complaints(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List complaints with optional filters."""
    conn = _get_db()
    try:
        query = "SELECT * FROM complaints WHERE 1=1"
        params: list[Any] = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def update_complaint_status(
    complaint_id: str,
    status: str,
    current_step: Optional[int] = None,
    next_action_days: Optional[int] = None,
) -> bool:
    """Update a complaint's status and optionally advance the step."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_db()
    try:
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]

        if current_step is not None:
            updates.append("current_step = ?")
            params.append(current_step)

        if next_action_days is not None:
            next_date = (datetime.now(timezone.utc) + timedelta(days=next_action_days)).isoformat()
            updates.append("next_action_date = ?")
            params.append(next_date)

        if status == "resolved":
            updates.append("resolved_at = ?")
            params.append(now)

        params.append(complaint_id)
        conn.execute(
            f"UPDATE complaints SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return True
    finally:
        conn.close()


def resolve_complaint(complaint_id: str) -> bool:
    """Mark a complaint as resolved."""
    return update_complaint_status(complaint_id, "resolved")


# ──────────────────────────────────────────────────────────────────────
# Complaint Actions
# ──────────────────────────────────────────────────────────────────────

def add_action(
    complaint_id: str,
    action_type: str,
    target: str = "",
    details: Optional[dict] = None,
    status: str = "success",
) -> dict[str, Any]:
    """Record an action taken on a complaint."""
    action_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_db()
    try:
        conn.execute(
            """INSERT INTO complaint_actions
               (id, complaint_id, action_type, target, details_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                action_id, complaint_id, action_type, target,
                json.dumps(details or {}), status, now,
            ),
        )
        conn.commit()
        logger.info("Action recorded: %s on complaint %s", action_type, complaint_id)
        return {
            "id": action_id,
            "action_type": action_type,
            "target": target,
            "status": status,
            "created_at": now,
        }
    finally:
        conn.close()


def get_actions(complaint_id: str) -> list[dict[str, Any]]:
    """Get all actions for a complaint."""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM complaint_actions WHERE complaint_id = ? ORDER BY created_at ASC",
            (complaint_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Follow-up Scheduler
# ──────────────────────────────────────────────────────────────────────

def get_due_complaints() -> list[dict[str, Any]]:
    """
    Get all complaints where the follow-up date has passed
    and the complaint is not yet resolved.
    Used by the automated follow-up scheduler.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT * FROM complaints
               WHERE next_action_date <= ?
               AND status NOT IN ('resolved', 'closed')
               ORDER BY next_action_date ASC""",
            (now,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_complaint_stats() -> dict[str, Any]:
    """Get aggregate complaint statistics."""
    conn = _get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) as cnt FROM complaints GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["cnt"]

        actions_count = conn.execute(
            "SELECT COUNT(*) FROM complaint_actions"
        ).fetchone()[0]

        return {
            "total": total,
            "total_complaints": total,
            "by_status": by_status,
            "total_actions": actions_count,
        }
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a database row to a dict, parsing JSON fields."""
    d = dict(row)
    for key in ("complaint_data_json", "escalation_path_json", "details_json"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
