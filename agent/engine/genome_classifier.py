"""
Genome Classifier — Species Identification for Form Genomes.

This module analyzes a Form Genome's field names and structure to
classify it into a "species" (e.g., consumer_complaint, tax_filing,
visa_application). The species provides context for the Field Mapper,
enabling more accurate field-to-data matching.

V1 uses a rule-based keyword approach. V2 will use embeddings.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Species Definitions
# ──────────────────────────────────────────────────────────────────────

SPECIES_KEYWORDS: dict[str, list[str]] = {
    "consumer_complaint": [
        "complaint", "vendor", "transaction", "refund", "dispute",
        "consumer", "case", "grievance", "resolution", "desired outcome",
        "merchant", "purchase", "product", "service", "defective",
    ],
    "government_feedback": [
        "feedback", "agency", "department", "ministry", "government",
        "public", "service", "officer", "incident", "report",
    ],
    "tax_filing": [
        "tax", "income", "deduction", "filing", "assessment",
        "revenue", "iras", "gst", "withholding", "taxable",
        "employer", "employment", "salary", "wages",
    ],
    "visa_application": [
        "visa", "passport", "travel", "nationality", "citizenship",
        "immigration", "entry", "departure", "sponsor", "embassy",
        "consulate", "permit", "stay",
    ],
    "insurance_claim": [
        "claim", "policy", "insurance", "premium", "coverage",
        "accident", "damage", "loss", "beneficiary", "insured",
    ],
    "registration_form": [
        "register", "registration", "sign up", "create account",
        "username", "password", "confirm password", "terms",
    ],
    "contact_form": [
        "contact", "message", "subject", "inquiry", "enquiry",
        "feedback", "comment",
    ],
    "generic_personal_info": [
        "name", "email", "phone", "address", "postal",
        "given name", "family name", "nric", "gender",
        "date of birth", "year of birth",
    ],
}

MIN_CONFIDENCE_THRESHOLD = 2


# ──────────────────────────────────────────────────────────────────────
# Classification Logic
# ──────────────────────────────────────────────────────────────────────

def _extract_field_text(genome: dict[str, Any]) -> str:
    """
    Extract all field names and button names from a genome into
    a single lowercase string for keyword matching.
    """
    parts: list[str] = []

    for field in genome.get("fields", []):
        name = field.get("name", "")
        if name:
            parts.append(name.lower())

    for btn in genome.get("buttons", []):
        name = btn.get("name", "")
        if name:
            parts.append(name.lower())

    for btn in genome.get("submit_buttons", []):
        name = btn.get("name", "")
        if name:
            parts.append(name.lower())

    for btn in genome.get("nav_buttons", []):
        name = btn.get("name", "")
        if name:
            parts.append(name.lower())

    # Also check step-level data if present
    for step in genome.get("steps", []):
        for field in step.get("fields", []):
            name = field.get("name", "")
            if name:
                parts.append(name.lower())

    return " ".join(parts)


def _score_species(field_text: str, keywords: list[str]) -> int:
    """Count how many keywords appear in the field text."""
    score = 0
    for keyword in keywords:
        if keyword.lower() in field_text:
            score += 1
    return score


def classify_genome(genome: dict[str, Any]) -> dict[str, Any]:
    """
    Classify a Form Genome into a species.

    Parameters
    ----------
    genome : dict
        The Form Genome dictionary from the Genome Extractor.

    Returns
    -------
    dict
        A classification result with keys:
        - species: str (e.g., "consumer_complaint")
        - confidence: float (0.0 to 1.0)
        - scores: dict mapping species to their raw scores
    """
    field_text = _extract_field_text(genome)

    if not field_text.strip():
        logger.warning("Empty genome — no fields to classify.")
        return {
            "species": "unknown",
            "confidence": 0.0,
            "scores": {},
        }

    scores: dict[str, int] = {}
    for species, keywords in SPECIES_KEYWORDS.items():
        scores[species] = _score_species(field_text, keywords)

    best_species = max(scores, key=scores.get)
    best_score = scores[best_species]

    # Calculate confidence as a ratio of best score to total keywords
    max_possible = len(SPECIES_KEYWORDS[best_species])
    confidence = min(best_score / max(max_possible, 1), 1.0)

    if best_score < MIN_CONFIDENCE_THRESHOLD:
        logger.info(
            "No species met the confidence threshold (best: %s with %d). "
            "Classifying as 'unknown'.",
            best_species, best_score,
        )
        return {
            "species": "unknown",
            "confidence": confidence,
            "scores": scores,
        }

    logger.info(
        "Genome classified as: %s (confidence: %.2f, score: %d/%d)",
        best_species, confidence, best_score, max_possible,
    )
    return {
        "species": best_species,
        "confidence": confidence,
        "scores": scores,
    }
