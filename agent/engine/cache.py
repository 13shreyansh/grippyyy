"""
Cache Layer — The Memory of the Form Genome Engine.

This module provides persistent caching for Form Genomes and their
field mappings. After the first visit to a form, the genome and
mapping are saved to disk. Subsequent visits skip the extraction
and mapping steps entirely, making them near-instantaneous.

Cache structure:
  cache/genomes/{url_hash}.json   — Form Genome
  cache/mappings/{url_hash}.json  — Field Mapping Template

The cache also supports invalidation (self-healing): if the executor
detects that a cached mapping is failing, it can invalidate the cache
entry and trigger a fresh extraction.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Cache Directories
# ──────────────────────────────────────────────────────────────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_GENOME_CACHE_DIR = os.path.join(_BASE_DIR, "cache", "genomes")
_MAPPING_CACHE_DIR = os.path.join(_BASE_DIR, "cache", "mappings")

os.makedirs(_GENOME_CACHE_DIR, exist_ok=True)
os.makedirs(_MAPPING_CACHE_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────
# URL Hashing
# ──────────────────────────────────────────────────────────────────────

def _url_hash(url: str) -> str:
    """Generate a stable, filesystem-safe hash for a URL."""
    normalized = url.strip().rstrip("/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────
# Genome Cache
# ──────────────────────────────────────────────────────────────────────

def get_cached_genome(url: str) -> dict[str, Any] | None:
    """Retrieve a cached Form Genome for the given URL."""
    key = _url_hash(url)
    path = os.path.join(_GENOME_CACHE_DIR, f"{key}.json")

    if not os.path.exists(path):
        logger.info("No cached genome for %s (key: %s)", url, key)
        return None

    try:
        with open(path, "r") as f:
            genome = json.load(f)
        logger.info(
            "Cache HIT: genome for %s (key: %s, cached at: %s)",
            url, key, genome.get("cached_at", "unknown"),
        )
        return genome
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Corrupted genome cache for %s: %s", url, exc)
        return None


def save_genome_to_cache(url: str, genome: dict[str, Any]) -> str:
    """Save a Form Genome to the cache. Returns the cache file path."""
    key = _url_hash(url)
    path = os.path.join(_GENOME_CACHE_DIR, f"{key}.json")

    genome["cached_at"] = datetime.now(timezone.utc).isoformat()
    genome["cache_key"] = key

    with open(path, "w") as f:
        json.dump(genome, f, indent=2, default=str)

    logger.info("Genome cached for %s (key: %s)", url, key)
    return path


# ──────────────────────────────────────────────────────────────────────
# Mapping Cache
# ──────────────────────────────────────────────────────────────────────

def get_cached_mapping(url: str) -> list[dict[str, Any]] | None:
    """Retrieve a cached field mapping template for the given URL."""
    key = _url_hash(url)
    path = os.path.join(_MAPPING_CACHE_DIR, f"{key}.json")

    if not os.path.exists(path):
        logger.info("No cached mapping for %s (key: %s)", url, key)
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        logger.info(
            "Cache HIT: mapping for %s (key: %s, cached at: %s)",
            url, key, data.get("cached_at", "unknown"),
        )
        return data.get("step_mappings", [])
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Corrupted mapping cache for %s: %s", url, exc)
        return None


def save_mapping_to_cache(
    url: str,
    step_mappings: list[dict[str, Any]],
    species: str = "unknown",
) -> str:
    """
    Save a field mapping template to the cache.
    Strips actual values to create a reusable template.
    Returns the cache file path.
    """
    key = _url_hash(url)
    path = os.path.join(_MAPPING_CACHE_DIR, f"{key}.json")

    template_steps = []
    for step in step_mappings:
        template_mappings = []
        for m in step.get("mappings", []):
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

    data = {
        "url": url,
        "cache_key": key,
        "species": species,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "step_mappings": template_steps,
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    logger.info("Mapping cached for %s (key: %s)", url, key)
    return path


# ──────────────────────────────────────────────────────────────────────
# Cache Invalidation (Self-Healing)
# ──────────────────────────────────────────────────────────────────────

def invalidate_cache(url: str) -> bool:
    """
    Invalidate both genome and mapping caches for a URL.
    Called when the executor detects a cached mapping is failing.
    Returns True if any cache files were removed.
    """
    key = _url_hash(url)
    removed = False

    for cache_dir, label in [
        (_GENOME_CACHE_DIR, "genome"),
        (_MAPPING_CACHE_DIR, "mapping"),
    ]:
        path = os.path.join(cache_dir, f"{key}.json")
        if os.path.exists(path):
            os.remove(path)
            removed = True
            logger.info("Invalidated %s cache for %s (key: %s)", label, url, key)

    if not removed:
        logger.info("No cache to invalidate for %s (key: %s)", url, key)

    return removed


# ──────────────────────────────────────────────────────────────────────
# Hydrate Mapping Template
# ──────────────────────────────────────────────────────────────────────

def hydrate_mapping(
    cached_mappings: list[dict[str, Any]],
    user_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Fill a cached mapping template with actual user data values.
    The template stores field-to-key relationships; this function
    injects the current user's actual values.
    """
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
# Cache Statistics
# ──────────────────────────────────────────────────────────────────────

def get_cache_stats() -> dict[str, Any]:
    """Return statistics about the current cache state."""
    genome_files = [
        f for f in os.listdir(_GENOME_CACHE_DIR)
        if f.endswith(".json")
    ]
    mapping_files = [
        f for f in os.listdir(_MAPPING_CACHE_DIR)
        if f.endswith(".json")
    ]

    return {
        "genome_count": len(genome_files),
        "mapping_count": len(mapping_files),
        "genome_dir": _GENOME_CACHE_DIR,
        "mapping_dir": _MAPPING_CACHE_DIR,
    }
