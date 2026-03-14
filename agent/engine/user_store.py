"""
User Store — Persistent Profile Memory for Grippy.

This module provides a local-first, SQLite-backed user profile that
remembers the user's basic information across sessions. No authentication
required for MVP — single-user, local storage.

Features:
  - First-visit onboarding detection
  - Persistent storage of personal data (name, email, phone, address, DOB)
  - Profile update with change confirmation
  - Categorization of fields: permanent vs case-specific
  - Natural language profile update parsing
"""

import json
import logging
import os
import sqlite3
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, "grippy_profile.db")

# ──────────────────────────────────────────────────────────────────────
# Profile Field Definitions
# ──────────────────────────────────────────────────────────────────────

# Fields that are "permanent" — saved to profile, reused across forms
PERMANENT_FIELDS = {
    "full_name": {"label": "Full Name", "question": "What's your full name?", "required": True, "order": 1},
    "given_name": {"label": "First Name", "question": "What's your first name?", "required": True, "order": 2},
    "family_name": {"label": "Last Name", "question": "What's your last name?", "required": True, "order": 3},
    "email": {"label": "Email", "question": "What's your email address?", "required": True, "order": 4},
    "phone": {"label": "Phone Number", "question": "What's your phone number?", "required": True, "order": 5},
    "date_of_birth": {"label": "Date of Birth", "question": "What's your date of birth? (DD/MM/YYYY)", "required": False, "order": 6},
    "gender": {"label": "Gender", "question": "What's your gender?", "required": False, "order": 7},
    "salutation": {"label": "Title (Mr/Ms/Mrs/Dr)", "question": "What's your preferred title? (Mr/Ms/Mrs/Dr)", "required": False, "order": 8},
    "address_line1": {"label": "Address Line 1", "question": "What's your street address?", "required": False, "order": 9},
    "address_line2": {"label": "Address Line 2", "question": "Any apartment/unit number?", "required": False, "order": 10},
    "city": {"label": "City", "question": "What city do you live in?", "required": False, "order": 11},
    "state": {"label": "State/Province", "question": "What state or province?", "required": False, "order": 12},
    "postal_code": {"label": "Postal/ZIP Code", "question": "What's your postal or ZIP code?", "required": False, "order": 13},
    "country": {"label": "Country", "question": "What country do you live in?", "required": False, "order": 14},
    "nationality": {"label": "Nationality", "question": "What's your nationality?", "required": False, "order": 15},
    "year_of_birth": {"label": "Year of Birth", "question": "What year were you born?", "required": False, "order": 16},
    "nric_last4": {"label": "NRIC Last 4 Digits", "question": "What are the last 4 digits of your NRIC? (if applicable)", "required": False, "order": 17},
    "middle_name": {"label": "Middle Name", "question": "What's your middle name?", "required": False, "order": 18},
}

# The minimum fields needed for onboarding to be "complete"
ONBOARDING_REQUIRED = ["full_name", "email", "phone"]

# Aliases: form field names that map to our profile keys
FIELD_ALIASES = {
    "first_name": "given_name",
    "firstname": "given_name",
    "last_name": "family_name",
    "lastname": "family_name",
    "surname": "family_name",
    "name": "full_name",
    "fullname": "full_name",
    "full name": "full_name",
    "email_address": "email",
    "emailaddress": "email",
    "phone_number": "phone",
    "phonenumber": "phone",
    "mobile": "phone",
    "mobile_number": "phone",
    "phone_mobile": "phone",
    "telephone": "phone",
    "tel": "phone",
    "dob": "date_of_birth",
    "birthday": "date_of_birth",
    "birth_date": "date_of_birth",
    "birthdate": "date_of_birth",
    "address": "address_line1",
    "street": "address_line1",
    "street_address": "address_line1",
    "apt": "address_line2",
    "apartment": "address_line2",
    "unit": "address_line2",
    "unit_number": "address_line2",
    "zip": "postal_code",
    "zipcode": "postal_code",
    "zip_code": "postal_code",
    "postcode": "postal_code",
    "pincode": "postal_code",
    "pin_code": "postal_code",
    "province": "state",
    "region": "state",
    "title": "salutation",
    "prefix": "salutation",
    "sex": "gender",
    "year_of_birth": "year_of_birth",
    "birth_year": "year_of_birth",
    "nric": "nric_last4",
    "ic_number": "nric_last4",
    "date_of_birth_input": "date_of_birth",
    "dateofbirthinput": "date_of_birth",
    "current_address": "address_line1",
    "currentaddress": "address_line1",
    "permanent_address": "address_line1",
    "mailing_address": "address_line1",
    "home_address": "address_line1",
    "building_name": "building_name",
    "contact_number": "phone",
    "contact_email": "email",
    "user_email": "email",
    "user_name": "full_name",
    "username": "full_name",
    "first": "given_name",
    "last": "family_name",
    "fname": "given_name",
    "lname": "family_name",
    "given": "given_name",
    "family": "family_name",
    "mail": "email",
    "e_mail": "email",
    "cell": "phone",
    "cell_phone": "phone",
    "cellphone": "phone",
    "home_phone": "phone",
    "work_phone": "phone",
    "date_of_birth_input": "date_of_birth",
}


# ──────────────────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Always ensure tables exist (handles DB deletion between restarts)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_profile (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL,
            source TEXT DEFAULT 'onboarding'
        );
        CREATE TABLE IF NOT EXISTS profile_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT NOT NULL,
            changed_at REAL NOT NULL,
            source TEXT DEFAULT 'user'
        );
    """)
    return conn


def _init_db() -> None:
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_profile (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                source TEXT DEFAULT 'onboarding'
            );
            
            CREATE TABLE IF NOT EXISTS profile_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT NOT NULL,
                changed_at REAL NOT NULL,
                source TEXT DEFAULT 'user'
            );
        """)
        conn.commit()
    finally:
        conn.close()


# Initialize on import
_init_db()


# ──────────────────────────────────────────────────────────────────────
# Core API
# ──────────────────────────────────────────────────────────────────────

def is_onboarded() -> bool:
    """Check if the user has completed basic onboarding."""
    profile = get_profile()
    for key in ONBOARDING_REQUIRED:
        if not profile.get(key):
            return False
    return True


def get_onboarding_questions() -> list[dict[str, str]]:
    """Return the list of questions still needed for onboarding."""
    profile = get_profile()
    questions = []
    for key in ONBOARDING_REQUIRED:
        if not profile.get(key):
            field_def = PERMANENT_FIELDS.get(key, {})
            questions.append({
                "key": key,
                "label": field_def.get("label", key),
                "question": field_def.get("question", f"What is your {key}?"),
            })
    return questions


def get_profile() -> dict[str, str]:
    """Return the full user profile as a flat dict."""
    conn = _get_db()
    try:
        rows = conn.execute("SELECT key, value FROM user_profile").fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        conn.close()


def get_profile_for_form() -> dict[str, str]:
    """
    Return the user profile with ALL known aliases expanded.
    This makes it easy for the field mapper to find matches.
    """
    profile = get_profile()
    expanded = dict(profile)
    
    # Add common aliases
    if "given_name" in profile and "family_name" in profile:
        expanded.setdefault("full_name", f"{profile['given_name']} {profile['family_name']}")
    if "full_name" in profile and "given_name" not in profile:
        parts = profile["full_name"].split(None, 1)
        expanded.setdefault("given_name", parts[0])
        if len(parts) > 1:
            expanded.setdefault("family_name", parts[1])
    if "date_of_birth" in profile:
        # Try to extract year
        dob = profile["date_of_birth"]
        for sep in ["/", "-", "."]:
            parts = dob.split(sep)
            if len(parts) >= 3:
                year_part = parts[-1] if len(parts[-1]) == 4 else parts[0]
                if year_part.isdigit() and len(year_part) == 4:
                    expanded.setdefault("year_of_birth", year_part)
                    break
    
    # Add reverse aliases so form fields can find our data
    for alias, canonical in FIELD_ALIASES.items():
        if canonical in expanded:
            expanded.setdefault(alias, expanded[canonical])
    
    return expanded


def update_profile(key: str, value: str, source: str = "user") -> dict[str, Any]:
    """
    Update a single profile field. Returns change info.
    """
    # Normalize key
    canonical = FIELD_ALIASES.get(key.lower().strip(), key.lower().strip())
    value = value.strip()
    
    conn = _get_db()
    try:
        # Get old value
        row = conn.execute(
            "SELECT value FROM user_profile WHERE key = ?", (canonical,)
        ).fetchone()
        old_value = row["value"] if row else None
        
        # Save new value
        now = time.time()
        conn.execute(
            """INSERT INTO user_profile (key, value, updated_at, source)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value = excluded.value,
                 updated_at = excluded.updated_at,
                 source = excluded.source""",
            (canonical, value, now, source),
        )
        
        # Record history
        conn.execute(
            """INSERT INTO profile_history (key, old_value, new_value, changed_at, source)
               VALUES (?, ?, ?, ?, ?)""",
            (canonical, old_value, value, now, source),
        )
        
        conn.commit()
        
        return {
            "key": canonical,
            "old_value": old_value,
            "new_value": value,
            "is_update": old_value is not None,
            "label": PERMANENT_FIELDS.get(canonical, {}).get("label", canonical),
        }
    finally:
        conn.close()


def bulk_update_profile(data: dict[str, str], source: str = "onboarding") -> list[dict[str, Any]]:
    """Update multiple profile fields at once."""
    results = []
    for key, value in data.items():
        if value and value.strip():
            result = update_profile(key, value, source=source)
            results.append(result)
    return results


def delete_profile_field(key: str) -> bool:
    """Delete a single profile field."""
    canonical = FIELD_ALIASES.get(key.lower().strip(), key.lower().strip())
    conn = _get_db()
    try:
        cursor = conn.execute("DELETE FROM user_profile WHERE key = ?", (canonical,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def clear_profile() -> int:
    """Clear all profile data. Returns number of fields cleared."""
    conn = _get_db()
    try:
        cursor = conn.execute("DELETE FROM user_profile")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_profile_history(limit: int = 50) -> list[dict[str, Any]]:
    """Get recent profile change history."""
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT key, old_value, new_value, changed_at, source
               FROM profile_history ORDER BY changed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def is_permanent_field(field_name: str) -> bool:
    """Check if a field name is a permanent profile field."""
    normalized = field_name.lower().strip().replace(" ", "_")
    if normalized in PERMANENT_FIELDS:
        return True
    canonical = FIELD_ALIASES.get(normalized)
    return canonical is not None and canonical in PERMANENT_FIELDS


def normalize_field_name(field_name: str) -> str:
    """Normalize a field name to its canonical form."""
    normalized = field_name.lower().strip().replace(" ", "_")
    return FIELD_ALIASES.get(normalized, normalized)
