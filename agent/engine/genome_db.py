"""
Form Genome Database — The Persistent Memory of the Form Genome Engine.

This module replaces the file-based JSON cache with a proper SQLite
database for persistent, queryable storage of Form Genomes and their
field mappings across sessions.

Advantages over file-based cache:
  - ACID transactions (no corrupted half-written files)
  - Full-text search across genomes
  - Query by species, domain, date range
  - Statistics and analytics
  - Concurrent access safety (WAL mode)
  - Automatic migration from old JSON cache
  - Versioning: track genome changes over time

Schema:
  genomes        — Extracted form structure (fields, steps, buttons)
  mappings       — Field-to-user-data mapping templates
  fill_history   — Audit log of every form fill attempt
  genome_versions — Track changes to genomes over time
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, "form_genome.db")

# Old JSON cache directories (for migration)
_OLD_GENOME_CACHE_DIR = os.path.join(_BASE_DIR, "cache", "genomes")
_OLD_MAPPING_CACHE_DIR = os.path.join(_BASE_DIR, "cache", "mappings")


# ──────────────────────────────────────────────────────────────────────
# Database Connection
# ──────────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    """Get a database connection with row factory and WAL mode."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ──────────────────────────────────────────────────────────────────────
# Schema Initialization
# ──────────────────────────────────────────────────────────────────────

def init_genome_db() -> None:
    """Initialize the Form Genome Database schema."""
    conn = _get_db()
    try:
        conn.executescript("""
            -- Core genome storage
            CREATE TABLE IF NOT EXISTS genomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                url_hash TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL DEFAULT '',
                species TEXT NOT NULL DEFAULT 'unknown',
                title TEXT DEFAULT '',
                genome_data TEXT NOT NULL DEFAULT '{}',
                field_count INTEGER DEFAULT 0,
                step_count INTEGER DEFAULT 1,
                cached_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 1,
                last_accessed TEXT NOT NULL,
                is_valid INTEGER DEFAULT 1
            );

            -- Mapping templates
            CREATE TABLE IF NOT EXISTS mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                url_hash TEXT NOT NULL UNIQUE,
                species TEXT NOT NULL DEFAULT 'unknown',
                mapping_data TEXT NOT NULL DEFAULT '[]',
                field_count INTEGER DEFAULT 0,
                cached_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 1,
                last_accessed TEXT NOT NULL,
                success_rate REAL DEFAULT 0.0,
                total_fills INTEGER DEFAULT 0,
                successful_fills INTEGER DEFAULT 0
            );

            -- Fill history (audit log)
            CREATE TABLE IF NOT EXISTS fill_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                url_hash TEXT NOT NULL,
                species TEXT DEFAULT 'unknown',
                user_id TEXT DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                success_rate REAL DEFAULT 0.0,
                fields_filled INTEGER DEFAULT 0,
                fields_failed INTEGER DEFAULT 0,
                confirmation_number TEXT DEFAULT '',
                captcha_detected INTEGER DEFAULT 0,
                captcha_solved INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0.0,
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            -- Genome version history
            CREATE TABLE IF NOT EXISTS genome_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                genome_data TEXT NOT NULL DEFAULT '{}',
                mapping_data TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                change_reason TEXT DEFAULT 'initial'
            );

            -- Indexes for performance
            CREATE INDEX IF NOT EXISTS idx_genomes_url_hash ON genomes(url_hash);
            CREATE INDEX IF NOT EXISTS idx_genomes_domain ON genomes(domain);
            CREATE INDEX IF NOT EXISTS idx_genomes_species ON genomes(species);
            CREATE INDEX IF NOT EXISTS idx_mappings_url_hash ON mappings(url_hash);
            CREATE INDEX IF NOT EXISTS idx_fill_history_url ON fill_history(url_hash);
            CREATE INDEX IF NOT EXISTS idx_fill_history_user ON fill_history(user_id);
            CREATE INDEX IF NOT EXISTS idx_fill_history_date ON fill_history(created_at);
            CREATE INDEX IF NOT EXISTS idx_genome_versions_hash ON genome_versions(url_hash);
        """)
        conn.commit()
        logger.info("Form Genome Database initialized at %s", DB_PATH)
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# URL Hashing (same algorithm as old cache for migration compatibility)
# ──────────────────────────────────────────────────────────────────────

def _url_hash(url: str) -> str:
    """Generate a stable hash for a URL."""
    normalized = url.strip().rstrip("/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _extract_domain(url: str) -> str:
    """Extract the domain from a URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or parsed.hostname or ""
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────
# Genome CRUD
# ──────────────────────────────────────────────────────────────────────

def get_cached_genome(url: str) -> dict[str, Any] | None:
    """Retrieve a cached Form Genome for the given URL."""
    key = _url_hash(url)
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM genomes WHERE url_hash = ? AND is_valid = 1",
            (key,),
        ).fetchone()

        if not row:
            logger.info("No cached genome for %s (key: %s)", url, key)
            return None

        # Update access stats
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE genomes SET access_count = access_count + 1, last_accessed = ? WHERE url_hash = ?",
            (now, key),
        )
        conn.commit()

        genome = json.loads(row["genome_data"])
        genome["cached_at"] = row["cached_at"]
        genome["cache_key"] = key
        genome["_db_id"] = row["id"]
        genome["_access_count"] = row["access_count"] + 1

        logger.info(
            "DB Cache HIT: genome for %s (key: %s, cached at: %s, accesses: %d)",
            url, key, row["cached_at"], row["access_count"] + 1,
        )
        return genome

    except Exception as exc:
        logger.warning("Error reading genome from DB for %s: %s", url, exc)
        return None
    finally:
        conn.close()


def save_genome_to_cache(url: str, genome: dict[str, Any]) -> str:
    """Save a Form Genome to the database. Returns the cache key."""
    key = _url_hash(url)
    domain = _extract_domain(url)
    now = datetime.now(timezone.utc).isoformat()

    # Count fields
    field_count = 0
    step_count = 0
    for step in genome.get("steps", []):
        step_count += 1
        field_count += len(step.get("fields", []))

    species = genome.get("species", "unknown")
    title = genome.get("title", "")

    genome_json = json.dumps(genome, default=str)

    conn = _get_db()
    try:
        # Upsert
        existing = conn.execute(
            "SELECT id, genome_data FROM genomes WHERE url_hash = ?", (key,)
        ).fetchone()

        if existing:
            # Save old version
            old_version_count = conn.execute(
                "SELECT COUNT(*) FROM genome_versions WHERE url_hash = ?", (key,)
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO genome_versions (url_hash, version, genome_data, created_at, change_reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, old_version_count + 1, existing["genome_data"], now, "updated"),
            )

            conn.execute(
                """UPDATE genomes SET
                    genome_data = ?, species = ?, title = ?, domain = ?,
                    field_count = ?, step_count = ?,
                    updated_at = ?, last_accessed = ?, is_valid = 1
                   WHERE url_hash = ?""",
                (genome_json, species, title, domain, field_count, step_count, now, now, key),
            )
        else:
            conn.execute(
                """INSERT INTO genomes
                    (url, url_hash, domain, species, title, genome_data,
                     field_count, step_count, cached_at, updated_at, last_accessed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, key, domain, species, title, genome_json,
                 field_count, step_count, now, now, now),
            )

            # Save initial version
            conn.execute(
                """INSERT INTO genome_versions (url_hash, version, genome_data, created_at, change_reason)
                   VALUES (?, 1, ?, ?, ?)""",
                (key, genome_json, now, "initial"),
            )

        conn.commit()
        logger.info("Genome saved to DB for %s (key: %s)", url, key)
        return key

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Mapping CRUD
# ──────────────────────────────────────────────────────────────────────

def get_cached_mapping(url: str) -> list[dict[str, Any]] | None:
    """Retrieve a cached field mapping template for the given URL."""
    key = _url_hash(url)
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM mappings WHERE url_hash = ?", (key,)
        ).fetchone()

        if not row:
            logger.info("No cached mapping for %s (key: %s)", url, key)
            return None

        # Update access stats
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE mappings SET access_count = access_count + 1, last_accessed = ? WHERE url_hash = ?",
            (now, key),
        )
        conn.commit()

        mappings = json.loads(row["mapping_data"])

        logger.info(
            "DB Cache HIT: mapping for %s (key: %s, accesses: %d, success_rate: %.1f%%)",
            url, key, row["access_count"] + 1, row["success_rate"],
        )
        return mappings

    except Exception as exc:
        logger.warning("Error reading mapping from DB for %s: %s", url, exc)
        return None
    finally:
        conn.close()


def save_mapping_to_cache(
    url: str,
    step_mappings: list[dict[str, Any]],
    species: str = "unknown",
) -> str:
    """Save a field mapping template to the database. Returns the cache key."""
    key = _url_hash(url)
    now = datetime.now(timezone.utc).isoformat()

    # Create template (strip actual values)
    template_steps = []
    field_count = 0
    for step in step_mappings:
        template_mappings = []
        for m in step.get("mappings", []):
            field_count += 1
            template_mappings.append({
                "field_name": m.get("field_name", ""),
                "field_role": m.get("field_role", ""),
                "user_data_key": m.get("user_data_key", ""),
                "selector": m.get("selector", ""),
                "selector_css": m.get("selector_css", ""),
                "method": m.get("method", ""),
            })
        template_steps.append({
            "step_number": step.get("step_number", 1),
            "mappings": template_mappings,
            "unmapped_fields": step.get("unmapped_fields", []),
            "nav_buttons": step.get("nav_buttons", []),
            "submit_buttons": step.get("submit_buttons", []),
        })

    mapping_json = json.dumps(template_steps, default=str)

    conn = _get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM mappings WHERE url_hash = ?", (key,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE mappings SET
                    mapping_data = ?, species = ?, field_count = ?,
                    updated_at = ?, last_accessed = ?
                   WHERE url_hash = ?""",
                (mapping_json, species, field_count, now, now, key),
            )
        else:
            conn.execute(
                """INSERT INTO mappings
                    (url, url_hash, species, mapping_data, field_count,
                     cached_at, updated_at, last_accessed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, key, species, mapping_json, field_count, now, now, now),
            )

        conn.commit()
        logger.info("Mapping saved to DB for %s (key: %s)", url, key)
        return key

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Cache Invalidation (Self-Healing)
# ──────────────────────────────────────────────────────────────────────

def invalidate_cache(url: str) -> bool:
    """
    Invalidate both genome and mapping caches for a URL.
    Marks genome as invalid rather than deleting (preserves history).
    """
    key = _url_hash(url)
    conn = _get_db()
    removed = False
    try:
        # Mark genome as invalid
        result = conn.execute(
            "UPDATE genomes SET is_valid = 0 WHERE url_hash = ?", (key,)
        )
        if result.rowcount > 0:
            removed = True
            logger.info("Invalidated genome for %s (key: %s)", url, key)

        # Delete mapping (force re-extraction)
        result = conn.execute(
            "DELETE FROM mappings WHERE url_hash = ?", (key,)
        )
        if result.rowcount > 0:
            removed = True
            logger.info("Deleted mapping for %s (key: %s)", url, key)

        conn.commit()

        if not removed:
            logger.info("No cache to invalidate for %s (key: %s)", url, key)

        return removed

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Hydrate Mapping Template
# ──────────────────────────────────────────────────────────────────────

def hydrate_mapping(
    cached_mappings: list[dict[str, Any]],
    user_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fill a cached mapping template with actual user data values."""
    hydrated_steps = []

    for step in cached_mappings:
        hydrated_mappings = []
        for m in step.get("mappings", []):
            data_key = m.get("user_data_key", "")
            value = str(user_data.get(data_key, "")) if data_key else ""
            hydrated_mappings.append({
                **m,
                "value": value,
            })
        hydrated_steps.append({
            **step,
            "mappings": hydrated_mappings,
        })

    return hydrated_steps


# ──────────────────────────────────────────────────────────────────────
# Fill History
# ──────────────────────────────────────────────────────────────────────

def record_fill(
    url: str,
    success: bool,
    success_rate: float = 0.0,
    fields_filled: int = 0,
    fields_failed: int = 0,
    confirmation_number: str = "",
    captcha_detected: bool = False,
    captcha_solved: bool = False,
    duration_seconds: float = 0.0,
    error: str = "",
    user_id: str = "",
) -> int:
    """Record a form fill attempt in the history. Returns the record ID."""
    key = _url_hash(url)
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_db()
    try:
        # Get species from genome
        genome_row = conn.execute(
            "SELECT species FROM genomes WHERE url_hash = ?", (key,)
        ).fetchone()
        species = genome_row["species"] if genome_row else "unknown"

        cursor = conn.execute(
            """INSERT INTO fill_history
                (url, url_hash, species, user_id, success, success_rate,
                 fields_filled, fields_failed, confirmation_number,
                 captcha_detected, captcha_solved, duration_seconds,
                 error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (url, key, species, user_id, int(success), success_rate,
             fields_filled, fields_failed, confirmation_number,
             int(captcha_detected), int(captcha_solved), duration_seconds,
             error, now),
        )

        # Update mapping success stats
        if success:
            conn.execute(
                """UPDATE mappings SET
                    total_fills = total_fills + 1,
                    successful_fills = successful_fills + 1,
                    success_rate = CAST(successful_fills + 1 AS REAL) / (total_fills + 1) * 100
                   WHERE url_hash = ?""",
                (key,),
            )
        else:
            conn.execute(
                """UPDATE mappings SET
                    total_fills = total_fills + 1,
                    success_rate = CAST(successful_fills AS REAL) / (total_fills + 1) * 100
                   WHERE url_hash = ?""",
                (key,),
            )

        conn.commit()
        return cursor.lastrowid

    finally:
        conn.close()


def get_fill_history(
    url: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get fill history, optionally filtered by URL or user."""
    conn = _get_db()
    try:
        query = "SELECT * FROM fill_history"
        params: list[Any] = []
        conditions = []

        if url:
            conditions.append("url_hash = ?")
            params.append(_url_hash(url))
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Database Statistics
# ──────────────────────────────────────────────────────────────────────

def get_cache_stats() -> dict[str, Any]:
    """Return comprehensive statistics about the Form Genome Database."""
    conn = _get_db()
    try:
        genome_count = conn.execute(
            "SELECT COUNT(*) FROM genomes WHERE is_valid = 1"
        ).fetchone()[0]
        mapping_count = conn.execute(
            "SELECT COUNT(*) FROM mappings"
        ).fetchone()[0]
        fill_count = conn.execute(
            "SELECT COUNT(*) FROM fill_history"
        ).fetchone()[0]
        version_count = conn.execute(
            "SELECT COUNT(*) FROM genome_versions"
        ).fetchone()[0]

        # Species distribution
        species_rows = conn.execute(
            "SELECT species, COUNT(*) as count FROM genomes WHERE is_valid = 1 GROUP BY species ORDER BY count DESC"
        ).fetchall()
        species_dist = {row["species"]: row["count"] for row in species_rows}

        # Top domains
        domain_rows = conn.execute(
            "SELECT domain, COUNT(*) as count FROM genomes WHERE is_valid = 1 AND domain != '' GROUP BY domain ORDER BY count DESC LIMIT 10"
        ).fetchall()
        top_domains = {row["domain"]: row["count"] for row in domain_rows}

        # Success rate
        success_row = conn.execute(
            "SELECT AVG(success_rate) as avg_rate, COUNT(*) as total FROM fill_history WHERE success = 1"
        ).fetchone()

        # DB file size
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0

        return {
            "genome_count": genome_count,
            "mapping_count": mapping_count,
            "fill_history_count": fill_count,
            "version_count": version_count,
            "species_distribution": species_dist,
            "top_domains": top_domains,
            "avg_success_rate": round(success_row["avg_rate"] or 0, 1),
            "total_successful_fills": success_row["total"],
            "database_path": DB_PATH,
            "database_size_bytes": db_size,
            "database_size_mb": round(db_size / (1024 * 1024), 2),
            "storage_type": "SQLite (WAL mode)",
        }

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Search & Query
# ──────────────────────────────────────────────────────────────────────

def search_genomes(
    query: str = "",
    species: str = "",
    domain: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the genome database by keyword, species, or domain."""
    conn = _get_db()
    try:
        sql = "SELECT id, url, url_hash, domain, species, title, field_count, step_count, cached_at, access_count, last_accessed FROM genomes WHERE is_valid = 1"
        params: list[Any] = []

        if query:
            sql += " AND (url LIKE ? OR title LIKE ? OR genome_data LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like])

        if species:
            sql += " AND species = ?"
            params.append(species)

        if domain:
            sql += " AND domain LIKE ?"
            params.append(f"%{domain}%")

        sql += " ORDER BY access_count DESC, last_accessed DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Migration from JSON Cache
# ──────────────────────────────────────────────────────────────────────

def migrate_from_json_cache() -> dict[str, int]:
    """
    Migrate existing JSON cache files to the SQLite database.
    Returns counts of migrated genomes and mappings.
    """
    migrated = {"genomes": 0, "mappings": 0, "errors": 0}

    # Migrate genomes
    if os.path.isdir(_OLD_GENOME_CACHE_DIR):
        for filename in os.listdir(_OLD_GENOME_CACHE_DIR):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(_OLD_GENOME_CACHE_DIR, filename)
            try:
                with open(filepath, "r") as f:
                    genome = json.load(f)
                url = genome.get("url", "")
                if url:
                    save_genome_to_cache(url, genome)
                    migrated["genomes"] += 1
            except Exception as exc:
                logger.warning("Failed to migrate genome %s: %s", filename, exc)
                migrated["errors"] += 1

    # Migrate mappings
    if os.path.isdir(_OLD_MAPPING_CACHE_DIR):
        for filename in os.listdir(_OLD_MAPPING_CACHE_DIR):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(_OLD_MAPPING_CACHE_DIR, filename)
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                url = data.get("url", "")
                species = data.get("species", "unknown")
                step_mappings = data.get("step_mappings", [])
                if url and step_mappings:
                    save_mapping_to_cache(url, step_mappings, species)
                    migrated["mappings"] += 1
            except Exception as exc:
                logger.warning("Failed to migrate mapping %s: %s", filename, exc)
                migrated["errors"] += 1

    logger.info(
        "Migration complete: %d genomes, %d mappings, %d errors",
        migrated["genomes"], migrated["mappings"], migrated["errors"],
    )
    return migrated


# ──────────────────────────────────────────────────────────────────────
# Initialize DB and auto-migrate
# ──────────────────────────────────────────────────────────────────────

init_genome_db()

# Auto-migrate on first run if old cache exists
if os.path.isdir(_OLD_GENOME_CACHE_DIR) or os.path.isdir(_OLD_MAPPING_CACHE_DIR):
    conn = _get_db()
    count = conn.execute("SELECT COUNT(*) FROM genomes").fetchone()[0]
    conn.close()
    if count == 0:
        logger.info("Old JSON cache detected — running auto-migration...")
        migrate_from_json_cache()
