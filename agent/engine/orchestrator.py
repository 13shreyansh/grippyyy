"""
Orchestrator — The Brain of the Form Genome Engine.

This module ties together all components of the engine into a single,
clean pipeline:

  1. Check Cache → if HIT, hydrate and execute (fast path)
  2. Extract Genome → sequence the form's accessibility tree
  3. Classify Genome → identify the form species
  4. Map Fields → match user data to form fields
  5. Cache → save genome and mapping for future use
  6. Execute → fill the form using Playwright
  7. Self-Heal → if execution fails, invalidate cache and retry

The Orchestrator exposes a single public function: `run_engine()`.
"""

import logging
import time
from typing import Any

from .genome_db import (
    get_cached_genome,
    get_cached_mapping,
    hydrate_mapping,
    invalidate_cache,
    save_genome_to_cache,
    save_mapping_to_cache,
    record_fill,
)
from .executor import execute_form_fill
from .field_mapper import map_fields
from .genome_classifier import classify_genome
from .genome_extractor import extract_genome

logger = logging.getLogger(__name__)

# Maximum number of self-healing retries
MAX_RETRIES = 2

# Failure threshold: if more than this fraction of fields fail,
# the cache is invalidated and the engine retries
FAILURE_THRESHOLD = 0.5


async def run_engine(
    url: str,
    user_data: dict[str, Any],
    progress_callback=None,
    use_cache: bool = True,
    dry_run: bool = False,
    user_intent: str = "",
) -> dict[str, Any]:
    """
    Run the full Form Genome Engine pipeline.

    Parameters
    ----------
    url : str
        The URL of the form to fill.
    user_data : dict
        The user's data to fill into the form.
    progress_callback : callable, optional
        An async function that receives progress messages.
    use_cache : bool
        Whether to use the cache layer.
    dry_run : bool
        If True, extract and map but do not execute (useful for testing).

    Returns
    -------
    dict
        Result with keys: success, confirmation_number, cache_hit,
        species, steps_completed, total_successes, total_failures,
        details, timing.
    """
    start_time = time.time()
    timing: dict[str, float] = {}
    cache_hit = False
    species = "unknown"
    genome = None
    step_mappings = None

    async def _progress(msg: str) -> None:
        if progress_callback:
            await progress_callback(msg)

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            logger.info(
                "Self-healing retry %d/%d for %s",
                attempt, MAX_RETRIES, url,
            )
            await _progress(
                f"Self-healing: retry {attempt}/{MAX_RETRIES}..."
            )

        # ──────────────────────────────────────────────────────────
        # Step 1: Check Cache
        # ──────────────────────────────────────────────────────────
        t0 = time.time()

        if use_cache and attempt == 0:
            cached_genome = get_cached_genome(url)

            if cached_genome:
                cache_hit = True
                genome = cached_genome
                species_result = classify_genome(genome)
                species = species_result["species"]

                timing["cache_lookup"] = time.time() - t0
                await _progress("Cache HIT — using saved form genome")
                logger.info("Cache HIT for %s — skipping extraction", url)

                # Always re-map with fresh user data (mapping is cheap,
                # extraction is expensive — this ensures mappings reflect
                # the latest user data profile)
                t0 = time.time()
                await _progress("Re-mapping fields with your data...")
                step_mappings = await map_fields(
                    user_data, genome, species=species
                )
                timing["mapping"] = time.time() - t0

                # Update cached mapping with fresh results
                save_mapping_to_cache(url, step_mappings, species=species)

        # ──────────────────────────────────────────────────────────
        # Step 2: Extract Genome (if no cache hit)
        # ──────────────────────────────────────────────────────────
        if not cache_hit or attempt > 0:
            t0 = time.time()
            await _progress("Sequencing form genome...")

            try:
                genome = await extract_genome(
                    url,
                    handle_multi_step=True,
                    user_intent=user_intent,
                    user_data=user_data,
                )
            except Exception as exc:
                logger.exception("Genome extraction failed: %s", exc)
                return {
                    "success": False,
                    "error": f"Genome extraction failed: {exc}",
                    "cache_hit": False,
                    "species": "unknown",
                    "steps_completed": 0,
                    "total_successes": 0,
                    "total_failures": 0,
                    "details": [],
                    "timing": timing,
                }

            timing["extraction"] = time.time() - t0

            # ──────────────────────────────────────────────────────
            # Step 3: Classify Genome
            # ──────────────────────────────────────────────────────
            t0 = time.time()
            await _progress("Classifying form species...")

            species_result = classify_genome(genome)
            species = species_result["species"]

            timing["classification"] = time.time() - t0

            # ──────────────────────────────────────────────────────
            # Step 4: Map Fields
            # ──────────────────────────────────────────────────────
            t0 = time.time()
            await _progress("Mapping user data to form fields...")

            step_mappings = await map_fields(
                user_data, genome, species=species
            )

            timing["mapping"] = time.time() - t0

            # ──────────────────────────────────────────────────────
            # Step 5: Cache Results (ALWAYS cache after extraction)
            # We always save to cache because the extraction is expensive.
            # The use_cache flag only controls whether we READ from cache.
            # ──────────────────────────────────────────────────────────
            save_genome_to_cache(url, genome)
            save_mapping_to_cache(url, step_mappings, species=species)
            await _progress("Form genome cached for future use")

        # ──────────────────────────────────────────────────────────
        # Dry Run: Return without executing
        # ──────────────────────────────────────────────────────────
        if dry_run:
            timing["total"] = time.time() - start_time
            return {
                "success": True,
                "dry_run": True,
                "confirmation_number": None,
                "cache_hit": cache_hit,
                "species": species,
                "genome": genome,
                "step_mappings": step_mappings,
                "steps_completed": 0,
                "total_successes": 0,
                "total_failures": 0,
                "details": [],
                "timing": timing,
            }

        # ──────────────────────────────────────────────────────────
        # Step 6: Execute Form Fill
        # ──────────────────────────────────────────────────────────
        t0 = time.time()
        await _progress("Executing form fill...")

        result = await execute_form_fill(
            url, step_mappings, user_data, progress_callback=progress_callback
        )

        timing["execution"] = time.time() - t0

        # ──────────────────────────────────────────────────────────
        # Step 7: Self-Healing Check
        # ──────────────────────────────────────────────────────────
        total_attempted = result["total_successes"] + result["total_failures"]
        failure_rate = (
            result["total_failures"] / max(total_attempted, 1)
        )

        if failure_rate > FAILURE_THRESHOLD and attempt < MAX_RETRIES:
            logger.warning(
                "High failure rate (%.1f%%) for %s. "
                "Invalidating cache and retrying.",
                failure_rate * 100, url,
            )
            await _progress(
                f"High failure rate ({failure_rate:.0%}). "
                f"Self-healing: re-sequencing form..."
            )
            invalidate_cache(url)
            cache_hit = False
            continue

        # ──────────────────────────────────────────────────────────
        # Success (or final attempt)
        # ──────────────────────────────────────────────────────────
        timing["total"] = time.time() - start_time

        # Record fill in history database
        try:
            record_fill(
                url=url,
                success=result.get("success", False),
                success_rate=result.get("success_rate", 0.0),
                fields_filled=result.get("total_successes", 0),
                fields_failed=result.get("total_failures", 0),
                confirmation_number=result.get("confirmation_number", "") or "",
                duration_seconds=timing.get("total", 0.0),
                error=result.get("error", "") or "",
            )
        except Exception as exc:
            logger.warning("Failed to record fill history: %s", exc)

        result.update({
            "cache_hit": cache_hit,
            "species": species,
            "timing": timing,
            "attempt": attempt + 1,
        })

        if result["success"]:
            rate = result.get("success_rate", 100.0)
            await _progress(
                f"Form submitted successfully! ({rate:.0f}% fields filled)"
            )
        else:
            await _progress(
                f"Form fill completed with {result['total_failures']} "
                f"failure(s) out of "
                f"{result['total_successes'] + result['total_failures']} fields."
            )

        return result

    # Should not reach here, but just in case
    timing["total"] = time.time() - start_time
    return {
        "success": False,
        "error": "Max retries exceeded",
        "cache_hit": cache_hit,
        "species": species,
        "steps_completed": 0,
        "total_successes": 0,
        "total_failures": 0,
        "details": [],
        "timing": timing,
    }
