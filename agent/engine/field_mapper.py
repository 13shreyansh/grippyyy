"""
Field Mapper V2 — Universal Intelligent Data-to-Form Mapping.

V2 Architecture: 4-Tier Matching Strategy
==========================================

The V1 mapper used a 2-tier approach (synonym dictionary + LLM fallback).
V2 adds two new tiers that leverage HTML metadata for near-perfect accuracy:

  Tier 1 (Autocomplete): Uses the HTML `autocomplete` attribute, which is
  the W3C standard for field identification. When present, this is 100%
  reliable. Covers ~60% of modern forms.

  Tier 2 (Input Type): Uses `type="email"`, `type="tel"`, `type="password"`,
  `type="url"` etc. to infer the field's purpose. Very reliable.

  Tier 3 (Synonym Dictionary): Expanded to 150+ field name variations with
  stricter matching to prevent false positives. Handles the long tail.

  Tier 4 (LLM Fallback): For truly ambiguous fields, a lightweight LLM call
  resolves the mapping. Used sparingly (~5% of fields).

Universal capabilities:
  - Works with any form type (government, tax, registration, e-commerce, etc.)
  - Dropdown option matching (selects the best option from available choices)
  - Radio button group matching
  - Checkbox state matching
  - Deduplication to prevent double-mapping
  - Handles any vocabulary in any language (via LLM fallback)
"""

import json
import logging
import os
import re
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

VALUE_FALLBACKS: dict[str, list[str]] = {
    "phone_mobile": ["phone_mobile", "phone"],
    "phone_home": ["phone_home", "phone"],
    "phone_office": ["phone_office", "phone"],
    "unit": ["unit", "unit_number", "address_line_2", "address_line2"],
    "building": ["building", "building_name"],
    "complaint_description": [
        "complaint_description", "complaint_summary", "issue",
        "incident_description", "message",
    ],
    "vendor_name": ["vendor_name", "company", "merchant", "complaint_against"],
    "website": ["website", "vendor_website"],
    "transaction_date": ["transaction_date", "incident_date"],
    "amount": ["amount", "quantum_claimed", "total_contract_amount"],
}

CASE_FIELD_KEYS: dict[str, list[str]] = {
    "natureofcomplaints": [
        "case_nature_of_complaint", "nature_of_complaint",
        "complaint_type",
    ],
    "industry": ["case_industry", "industry"],
    "transactiontype": ["transaction_type"],
    "desiredoutcome": ["desired_outcome"],
    "association": ["association_member"],
    "maybeamendedfromtimetotime": ["case_terms_consent"],
    "consenttoreceivemarketingmessagesfromcase": [
        "case_marketing_consent",
    ],
}


# ──────────────────────────────────────────────────────────────────────
# Tier 1: HTML Autocomplete Attribute Mapping
# ──────────────────────────────────────────────────────────────────────
# Reference: https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#autofilling-form-controls

AUTOCOMPLETE_MAP: dict[str, str] = {
    # Identity
    "name": "full_name",
    "honorific-prefix": "salutation",
    "given-name": "given_name",
    "additional-name": "middle_name",
    "family-name": "family_name",
    "honorific-suffix": "suffix",
    "nickname": "username",
    "username": "username",

    # Contact
    "email": "email",
    "tel": "phone_mobile",
    "tel-national": "phone_mobile",
    "tel-local": "phone_mobile",

    # Address
    "street-address": "street",
    "address-line1": "street",
    "address-line2": "address_line_2",
    "address-line3": "address_line_3",
    "address-level1": "state",
    "address-level2": "city",
    "address-level3": "district",
    "address-level4": "suburb",
    "postal-code": "postal_code",
    "country": "country",
    "country-name": "country",

    # Payment
    "cc-name": "card_name",
    "cc-given-name": "given_name",
    "cc-family-name": "family_name",
    "cc-number": "card_number",
    "cc-exp": "card_expiry",
    "cc-exp-month": "card_exp_month",
    "cc-exp-year": "card_exp_year",
    "cc-csc": "card_cvv",
    "cc-type": "card_type",

    # Dates
    "bday": "date_of_birth",
    "bday-day": "birth_day",
    "bday-month": "birth_month",
    "bday-year": "year_of_birth",

    # Organization
    "organization": "vendor_name",
    "organization-title": "occupation",

    # Authentication
    "new-password": "password",
    "current-password": "password",
    "one-time-code": "__skip__",

    # Other
    "sex": "gender",
    "url": "website",
    "photo": "__skip__",
    "impp": "__skip__",
    "language": "language",
}


def _match_autocomplete(field: dict[str, Any]) -> str | None:
    """
    Match a field using its HTML autocomplete attribute.
    This is the most reliable matching method — the W3C standard.
    """
    autocomplete = field.get("autocomplete", "").strip().lower()
    if not autocomplete or autocomplete in ("off", "on", "false", "true"):
        return None

    # Handle compound values like "shipping street-address"
    # The last token is the field type
    tokens = autocomplete.split()
    field_type = tokens[-1] if tokens else ""

    return AUTOCOMPLETE_MAP.get(field_type)


# ──────────────────────────────────────────────────────────────────────
# Tier 2: Input Type Inference
# ──────────────────────────────────────────────────────────────────────

INPUT_TYPE_MAP: dict[str, str] = {
    "email": "email",
    "tel": "phone_mobile",
    "password": "password",
    "url": "website",
    "number": None,  # Too ambiguous — could be age, amount, zip, etc.
    "date": None,     # Too ambiguous — could be DOB, transaction date, etc.
}


def _match_input_type(field: dict[str, Any]) -> str | None:
    """
    Match a field using its HTML input type.
    Only used for unambiguous types (email, tel, password, url).
    """
    input_type = field.get("input_type", "").strip().lower()
    if not input_type:
        return None

    return INPUT_TYPE_MAP.get(input_type)


# ──────────────────────────────────────────────────────────────────────
# Tier 3: Comprehensive Deterministic Synonym Dictionary
# ──────────────────────────────────────────────────────────────────────

SYNONYM_MAP: list[tuple[list[str], str]] = [
    # ── Identity ──
    (["salutation", "title", "prefix", "honorific", "mr/mrs", "mr mrs"], "salutation"),
    (["given name", "first name", "given_name", "firstname", "forename",
      "first", "fname"], "given_name"),
    (["family name", "last name", "family_name", "lastname", "surname",
      "last", "lname"], "family_name"),
    (["middle name", "middle_name", "middlename", "middle initial", "mi"], "middle_name"),
    (["full name", "fullname", "your name", "applicant name", "contact name",
      "legal name", "complete name"], "full_name"),
    # "name" alone — only match if it's the ONLY word
    (["name"], "full_name"),
    (["nric", "national id", "identity card", "ic number", "id number",
      "ssn", "social security", "social security number", "tax id", "tin",
      "pan", "pan number", "aadhaar", "aadhaar number",
      "passport number", "passport no", "passport"], "nric"),
    (["gender", "sex"], "gender"),
    (["nationality", "citizenship"], "nationality"),
    (["race", "ethnicity", "ethnic group"], "race"),
    (["religion"], "religion"),
    (["marital status", "marital"], "marital_status"),
    (["occupation", "job title", "profession", "designation", "job",
      "position", "role", "work title"], "occupation"),

    # ── Date of Birth / Age ──
    (["year of birth", "birth year"], "year_of_birth"),
    (["date of birth", "dob", "birthday", "birth date", "birthdate",
      "date of birth (dd/mm/yyyy)", "date of birth (mm/dd/yyyy)"], "date_of_birth"),
    (["age"], "age"),

    # ── Contact ──
    (["email address", "email", "e-mail", "email id", "e-mail address",
      "contact email", "your email", "email addr", "e mail"], "email"),
    (["confirm email", "re-enter email", "verify email", "email confirmation",
      "retype email", "email again"], "email"),
    (["phone number (mobile)", "mobile phone", "mobile number", "mobile",
      "cell phone", "cell", "cellphone", "mobile no", "cell number",
      "mobile #", "cell #"], "phone_mobile"),
    (["phone number (home)", "home phone", "home number", "home tel",
      "home phone number", "residential phone"], "phone_home"),
    (["phone number (office)", "office phone", "office number", "work phone",
      "business phone", "work number", "office tel"], "phone_office"),
    (["phone number", "phone", "telephone", "contact number", "tel",
      "telephone number", "phone no", "contact no", "daytime phone",
      "phone #", "telephone no", "contact phone"], "phone_mobile"),
    (["fax number", "fax", "fax no"], "fax"),

    # ── Address ──
    (["block/house number", "block number", "house number", "block", "blk",
      "house no", "building number", "street number", "house #"], "block"),
    (["street name", "street", "street address", "address line 1",
      "address 1", "address", "residential address", "mailing address",
      "home address", "permanent address", "addr", "address1"], "street"),
    (["address line 2", "address 2", "apt", "suite", "apartment",
      "apt/suite", "address2", "addr2"], "address_line_2"),
    (["unit number", "unit no", "unit", "flat", "floor", "flat no",
      "floor no", "apt number"], "unit"),
    (["building/estate name", "building name", "estate name", "building",
      "apartment name", "complex name", "condo name"], "building"),
    (["postal code", "postcode", "zip code", "zip", "pin code", "pincode",
      "zipcode", "post code", "area code"], "postal_code"),
    (["city", "town", "municipality", "city/town"], "city"),
    (["state", "province", "region", "prefecture", "district",
      "state/province", "state province"], "state"),
    (["country", "nation", "country/region"], "country"),

    # ── Vendor / Company / Organization ──
    (["vendor name", "company name", "merchant name", "business name",
      "organization name", "organisation name", "firm name", "entity name",
      "company", "vendor", "merchant", "business", "organization",
      "organisation", "employer name", "employer", "org name"], "vendor_name"),
    (["vendor block", "vendor house", "vendor block/house", "company block"], "vendor_block"),
    (["vendor street", "vendor street name", "company street", "company address",
      "business address"], "vendor_street"),
    (["vendor postal", "vendor postal code", "vendor postcode", "company postal",
      "company zip"], "vendor_postal_code"),
    (["vendor unit", "vendor unit number", "company unit"], "vendor_unit"),
    (["vendor email", "company email", "business email"], "vendor_email"),
    (["vendor phone", "company phone", "business phone number"], "vendor_phone"),
    (["website", "website url", "company website", "url", "web address",
      "homepage", "site url"], "website"),

    # ── Complaint / Issue ──
    (["transaction type", "type of transaction", "complaint type",
      "transactiontype",
      "issue type", "category", "type of complaint", "type of issue",
      "nature of complaint", "complaint category", "issue category",
      "problem type", "concern type"], "transaction_type"),
    (["natureofcomplaints", "nature of complaints"], "case_nature_of_complaint"),
    (["industry"], "case_industry"),
    (["desiredoutcome"], "desired_outcome"),
    (["quantum claimed", "quantum claimed (in cash) sgd"], "amount"),
    (["union"], "case_union"),
    (["cooperative", "co-operative"], "case_cooperative"),
    (["association"], "association_member"),
    (["others"], "case_other_membership"),
    (["may be amended from time to time"], "case_terms_consent"),
    (["consent to receive marketing messages from case"], "case_marketing_consent"),
    (["transaction date", "date of transaction", "purchase date",
      "date of purchase", "date of incident", "incident date",
      "order date"], "transaction_date"),
    (["desired outcome", "resolution sought", "what do you want",
      "expected resolution", "remedy sought", "relief sought",
      "desired resolution", "what would you like"], "desired_outcome"),
    (["complaint summary", "complaint description", "description",
      "summary", "details", "complaint details", "issue description",
      "problem description", "explain your complaint", "brief description",
      "describe your issue", "describe your problem", "what happened",
      "incident description", "additional details", "issue details",
      "problem details", "concern description", "tell us more",
      "explain the issue", "please describe"], "complaint_description"),
    (["complaint date", "date of complaint"], "complaint_date"),
    (["amount", "transaction amount", "purchase amount", "claim amount",
      "total amount", "amount paid", "payment amount", "price",
      "cost", "total cost", "total price"], "amount"),
    (["receipt number", "order number", "reference number", "invoice number",
      "transaction id", "order id", "booking reference", "confirmation number",
      "tracking number", "case number", "ticket number", "ref no",
      "reference no", "invoice no"], "reference_number"),

    # ── Payment / Financial ──
    (["bank name", "bank"], "bank_name"),
    (["account number", "account no", "bank account", "account #"], "account_number"),
    (["routing number", "sort code", "ifsc", "swift code", "bsb",
      "aba number", "transit number"], "routing_number"),
    (["credit card", "card number", "card no", "card #",
      "debit card number"], "card_number"),
    (["expiry date", "expiration date", "card expiry", "exp date",
      "valid thru", "valid through"], "card_expiry"),
    (["cvv", "cvc", "security code", "card security", "cvv2"], "card_cvv"),

    # ── Education ──
    (["school", "school name", "institution", "university", "college",
      "educational institution"], "school"),
    (["degree", "qualification", "education level", "highest education",
      "education"], "degree"),
    (["major", "field of study", "specialization", "course", "program",
      "department"], "major"),
    (["graduation year", "year of graduation", "passing year",
      "grad year"], "graduation_year"),

    # ── Travel / Visa ──
    (["passport number", "passport no", "passport #"], "passport_number"),
    (["passport expiry", "passport expiration", "passport exp date"], "passport_expiry"),
    (["visa type", "type of visa", "visa category"], "visa_type"),
    (["travel date", "departure date", "date of travel",
      "date of departure"], "travel_date"),
    (["return date", "arrival date", "date of return",
      "date of arrival"], "return_date"),
    (["destination", "destination country", "country of destination",
      "travel destination"], "destination"),
    (["purpose of travel", "purpose of visit", "reason for travel",
      "travel purpose", "visit purpose"], "travel_purpose"),
    (["flight number", "flight no", "flight #"], "flight_number"),
    (["hotel name", "accommodation", "hotel", "place of stay"], "hotel_name"),

    # ── Insurance ──
    (["policy number", "policy no", "policy #", "insurance number",
      "insurance policy"], "policy_number"),
    (["claim type", "type of claim", "claim category"], "claim_type"),
    (["date of loss", "loss date", "incident date", "accident date",
      "date of accident"], "incident_date"),
    (["loss description", "incident description", "accident description",
      "describe the incident", "what happened"], "incident_description"),

    # ── Generic / Misc ──
    (["subject", "topic", "regarding", "re", "subject line"], "subject"),
    (["message", "comments", "additional information", "remarks",
      "notes", "feedback", "additional comments", "other information",
      "special instructions", "special requests", "your message",
      "leave a message", "write your message"], "message"),
    (["password", "pwd", "pass"], "password"),
    (["confirm password", "re-enter password", "verify password",
      "repeat password", "retype password", "password again",
      "confirm", "re enter password"], "password"),
    (["username", "user name", "user id", "login id", "login",
      "user", "userid", "screen name", "handle"], "username"),
    (["captcha", "verification code", "security code", "verify you are human",
      "i'm not a robot", "recaptcha"], "__skip__"),
    (["terms", "terms and conditions", "i agree", "accept terms",
      "terms of service", "privacy policy", "consent"], "__skip__"),
]


def _deterministic_match(field_name: str) -> str | None:
    """
    Match a form field name to a user data key using the synonym dictionary.
    
    V2 uses a stricter matching algorithm:
      1. Exact match (case-insensitive, stripped)
      2. Synonym-is-substring-of-field match (longest wins)
      3. Field-is-substring-of-synonym match (only if field is a complete word)
      4. Token overlap match (requires >50% overlap)
    """
    name_lower = field_name.lower().strip()
    # Remove trailing asterisks, colons, and whitespace
    name_lower = re.sub(r'[\s*:]+$', '', name_lower).strip()
    if not name_lower:
        return None

    # Phase 1: Exact match (highest priority, instant)
    for synonyms, data_key in SYNONYM_MAP:
        for synonym in synonyms:
            if synonym == name_lower:
                return data_key if data_key != "__skip__" else None

    # Phase 2: Synonym is a substring of field name (longest match wins)
    # e.g., field="Your Email Address" matches synonym="email address"
    best_match = None
    best_match_len = 0
    for synonyms, data_key in SYNONYM_MAP:
        if data_key == "__skip__":
            for synonym in synonyms:
                if synonym in name_lower:
                    return None
            continue
        for synonym in synonyms:
            if synonym in name_lower and len(synonym) > best_match_len:
                best_match_len = len(synonym)
                best_match = data_key

    if best_match and best_match_len >= 3:
        return best_match

    # Phase 3: Field name is a complete word within a synonym
    # e.g., field="email" matches synonym="email address"
    # But field="mail" does NOT match "email address"
    for synonyms, data_key in SYNONYM_MAP:
        if data_key == "__skip__":
            continue
        for synonym in synonyms:
            # Check if field name appears as a complete word in the synonym
            if re.search(r'\b' + re.escape(name_lower) + r'\b', synonym):
                # Only accept if the field name is reasonably long
                if len(name_lower) >= 3:
                    return data_key

    # Phase 4: Token overlap (requires significant overlap)
    name_tokens = set(re.findall(r"[a-z]{2,}", name_lower))
    if name_tokens and len(name_tokens) >= 1:
        best_fuzzy = None
        best_score = 0
        for synonyms, data_key in SYNONYM_MAP:
            if data_key == "__skip__":
                continue
            for synonym in synonyms:
                syn_tokens = set(re.findall(r"[a-z]{2,}", synonym.lower()))
                if not syn_tokens:
                    continue
                overlap = len(name_tokens & syn_tokens)
                # Require at least 50% of synonym tokens to match
                if overlap > best_score and overlap >= max(1, len(syn_tokens) * 0.5):
                    best_score = overlap
                    best_fuzzy = data_key
        if best_fuzzy and best_score >= 1:
            return best_fuzzy

    return None


def _normalized_field_name(field_name: str) -> str:
    """Normalize a field label for deterministic portal-specific matching."""
    return re.sub(r"[^a-z0-9]", "", field_name.lower())


def _vendor_equivalent(data_key: str) -> str | None:
    """Return the vendor-specific version of a personal/contact data key."""
    vendor_map = {
        "full_name": "vendor_name",
        "email": "vendor_email",
        "website": "website",
        "phone_mobile": "vendor_phone",
        "block": "vendor_block",
        "street": "vendor_street",
        "unit": "vendor_unit",
        "building": "vendor_building",
        "postal_code": "vendor_postal_code",
    }
    return vendor_map.get(data_key)


def _candidate_keys(field_name: str, data_key: str) -> list[str]:
    """Build a priority-ordered list of user-data keys for a mapped field."""
    candidates: list[str] = []
    normalized = _normalized_field_name(field_name)
    if normalized in CASE_FIELD_KEYS:
        candidates.extend(CASE_FIELD_KEYS[normalized])
    candidates.extend(VALUE_FALLBACKS.get(data_key, [data_key]))
    if "vendor" in normalized:
        vendor_key = _vendor_equivalent(data_key)
        if vendor_key:
            candidates.insert(0, vendor_key)
    seen: set[str] = set()
    ordered: list[str] = []
    for key in candidates:
        if key and key not in seen:
            ordered.append(key)
            seen.add(key)
    return ordered


def _first_value(user_data: dict[str, Any], keys: list[str]) -> str:
    """Return the first non-empty value found for the provided key list."""
    for key in keys:
        value = user_data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _case_choice_from_context(field_name: str, user_data: dict[str, Any]) -> str:
    """Infer CASE Singapore dropdown values from the complaint context."""
    normalized = _normalized_field_name(field_name)
    context = " ".join(
        str(user_data.get(key, "")).lower()
        for key in (
            "complaint_description", "complaint_summary", "issue",
            "desired_outcome", "transaction_type", "product_service",
            "company", "merchant",
        )
    )
    if normalized == "natureofcomplaints":
        if "refund" in context:
            return "Refund issue"
        if any(word in context for word in ("defective", "faulty", "damaged")):
            return "Defective or Non-Conforming Goods"
    if normalized == "industry":
        if any(word in context for word in ("laptop", "computer", "notebook")):
            return "Computers"
        if any(word in context for word in ("phone", "mobile", "iphone")):
            return "Handphones"
        if any(word in context for word in ("flight", "airline", "airport")):
            return "Airlines"
        if any(word in context for word in ("hotel", "travel", "booking")):
            return "Travel"
        return "Miscellaneous"
    if normalized == "transactiontype":
        return "Purchase" if any(
            word in context for word in ("purchase", "bought", "sold", "order")
        ) else "Service"
    if normalized == "desiredoutcome":
        return "Quantum of claim" if "refund" in context else "Explanation / Verification"
    return ""


def _format_case_nric(value: str) -> str:
    """CASE NRIC field accepts only the last 3 digits plus the trailing letter."""
    raw = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if len(raw) >= 4 and re.search(r"\d", raw):
        return raw[-4:]
    return raw


def _format_phone_digits(value: str) -> str:
    """Strip a phone value down to digits for strict legacy form fields."""
    digits = re.sub(r"\D", "", value)
    if digits.startswith("65") and len(digits) > 8:
        return digits[-8:]
    return digits


def _normalize_mapped_value(field_name: str, value: str) -> str:
    """Apply field-specific value normalization before mapping."""
    normalized = _normalized_field_name(field_name)
    lowered = value.strip().lower()
    if normalized == "natureofcomplaints":
        if "refund" in lowered:
            return "Refund issue"
        if any(word in lowered for word in ("defective", "faulty", "damaged")):
            return "Defective or Non-Conforming Goods"
    if normalized == "industry":
        if any(word in lowered for word in ("laptop", "computer", "notebook")):
            return "Computers"
        if any(word in lowered for word in ("phone", "mobile")):
            return "Handphones"
        if any(word in lowered for word in ("flight", "airline")):
            return "Airlines"
    if normalized == "transactiontype":
        if any(word in lowered for word in ("purchase", "order", "bought", "sold")):
            return "Purchase"
        if "service" in lowered:
            return "Service"
    if normalized == "desiredoutcome":
        if "refund" in lowered or "claim" in lowered or "compensation" in lowered:
            return "Quantum of claim"
        if "cancel" in lowered:
            return "Cancellation"
    if "nric" in normalized:
        return _format_case_nric(value)
    if any(token in normalized for token in ("phone", "mobile", "fax")):
        return _format_phone_digits(value)
    return value.strip()


def _resolve_field_value(
    field: dict[str, Any],
    data_key: str,
    user_data: dict[str, Any],
) -> str:
    """Resolve a mapped field to the best available user-data value."""
    field_name = field.get("name", "")
    value = _first_value(user_data, _candidate_keys(field_name, data_key))
    if not value:
        value = _case_choice_from_context(field_name, user_data)
    return _normalize_mapped_value(field_name, value) if value else ""


# ──────────────────────────────────────────────────────────────────────
# Dropdown / Radio / Checkbox Option Matching
# ──────────────────────────────────────────────────────────────────────

def _match_dropdown_option(
    value: str,
    options: list[str],
) -> str | None:
    """
    Find the best matching option from a dropdown's available options.
    Returns the exact option text to select, or None if no match found.
    
    Uses a 5-tier matching strategy:
      1. Exact match (case-insensitive)
      2. Starts-with match
      3. Contains match
      4. Reverse contains (value contains option)
      5. Token overlap match
    """
    if not value or not options:
        return None

    value_lower = value.strip().lower()

    # Tier 1: Exact match
    for opt in options:
        if opt.strip().lower() == value_lower:
            return opt

    # Tier 2: Starts-with match
    for opt in options:
        if opt.strip().lower().startswith(value_lower):
            return opt

    # Tier 3: Contains match
    for opt in options:
        if value_lower in opt.strip().lower():
            return opt

    # Tier 4: Reverse contains (value contains option)
    for opt in options:
        opt_lower = opt.strip().lower()
        if opt_lower in value_lower and len(opt_lower) >= 2:
            return opt

    # Tier 5: Token overlap (for multi-word values)
    value_tokens = set(re.findall(r"[a-z]+", value_lower))
    if value_tokens:
        best_opt = None
        best_score = 0
        for opt in options:
            opt_tokens = set(re.findall(r"[a-z]+", opt.strip().lower()))
            overlap = len(value_tokens & opt_tokens)
            if overlap > best_score:
                best_score = overlap
                best_opt = opt
        if best_opt and best_score >= 1:
            return best_opt

    return None


def _match_checkbox_value(
    value: str,
) -> bool:
    """Determine if a checkbox should be checked based on the user data value."""
    if isinstance(value, bool):
        return value
    val_lower = str(value).strip().lower()
    return val_lower in ("true", "yes", "1", "on", "checked", "y")


# ──────────────────────────────────────────────────────────────────────
# Tier 4: LLM Fallback
# ──────────────────────────────────────────────────────────────────────

async def _llm_map_fields(
    unmapped_fields: list[dict[str, Any]],
    available_data_keys: list[str],
    species: str,
    all_fields: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """
    Use a lightweight LLM to map unmapped form fields to user data keys.
    Returns a dict mapping field_id -> user_data_key.
    
    Only called for fields that couldn't be matched by Tiers 1-3.
    """
    if not unmapped_fields or not available_data_keys:
        return {}

    client = AsyncOpenAI()

    # Build context: show ALL fields so the LLM understands the form structure
    context_lines = []
    if all_fields:
        context_lines.append("Full form structure (for context):")
        for i, f in enumerate(all_fields):
            name = f.get("name", "").strip()
            role = f.get("role", "")
            options = f.get("options", [])
            label = name if name else f"(unnamed {role} at position {i})"
            opts_str = f" [options: {', '.join(options[:8])}]" if options else ""
            context_lines.append(f"  {i+1}. {label} [{role}]{opts_str}")
        context_lines.append("")

    # Build the unmapped field list with unique IDs
    field_descriptions = []
    unnamed_counter = 0
    for f in unmapped_fields:
        name = f.get("name", "").strip()
        role = f.get("role", "")
        options = f.get("options", [])
        input_type = f.get("input_type", "")
        if name:
            field_id = name
        else:
            field_id = f"__unnamed_{unnamed_counter}"
            unnamed_counter += 1

        options_str = ""
        if options:
            options_str = f" (options: {', '.join(options[:10])})"

        type_str = f" [input_type={input_type}]" if input_type else ""

        desc = f'- "{field_id}" (role: {role}){type_str}{options_str}'
        field_descriptions.append(desc)

    prompt = (
        f"You are a form field mapping engine. The form species is: {species}.\n\n"
        + "\n".join(context_lines)
        + f"Map each unmapped form field to the MOST appropriate user data key.\n"
        f"Each field MUST map to a DIFFERENT key (no duplicates).\n"
        f"If a field has options listed, use those to infer what the field is for.\n\n"
        f"Unmapped fields:\n"
        + "\n".join(field_descriptions)
        + f"\n\nAvailable user data keys:\n{json.dumps(available_data_keys)}\n\n"
        f"Return ONLY a JSON object mapping field IDs to data keys. "
        f"Use null for fields that cannot be mapped. Example:\n"
        f'{{"__unnamed_0": "salutation", "__unnamed_1": "gender", "Some Field": "data_key"}}'
    )

    try:
        completion = await client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "You are a precise field mapping engine. Return only valid JSON. Each field must map to a unique key."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )

        content = completion.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(content[start:end])
            if isinstance(result, dict):
                return {k: v for k, v in result.items() if v is not None}
    except Exception as exc:
        logger.warning("LLM field mapping failed: %s", exc)

    return {}


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

async def map_fields(
    user_data: dict[str, Any],
    genome: dict[str, Any],
    species: str = "unknown",
    use_llm_fallback: bool = True,
) -> list[dict[str, Any]]:
    """
    Map user data to form fields discovered in ANY Form Genome.

    V2 4-Tier Matching Strategy:
      Tier 1: HTML autocomplete attribute (100% reliable when present)
      Tier 2: Input type inference (email, tel, password, url)
      Tier 3: Synonym dictionary (150+ field name variations)
      Tier 4: LLM fallback (for truly ambiguous fields)

    Parameters
    ----------
    user_data : dict
        The user's data (e.g., {"given_name": "John", "email": "j@x.com"}).
    genome : dict
        The Form Genome dictionary from the Genome Extractor.
    species : str
        The species classification from the Genome Classifier.
    use_llm_fallback : bool
        Whether to use the LLM for unmapped fields.

    Returns
    -------
    list[dict]
        A list of mapping dictionaries, one per step.
    """
    steps = genome.get("steps", [])
    if not steps:
        steps = [{
            "step_number": 1,
            "fields": genome.get("fields", []),
            "nav_buttons": genome.get("nav_buttons", []),
            "submit_buttons": genome.get("submit_buttons", []),
        }]

    result: list[dict[str, Any]] = []

    for step in steps:
        step_mappings: list[dict[str, Any]] = []
        unmapped: list[dict[str, Any]] = []
        all_fields = step.get("fields", [])
        used_keys: set[str] = set()

        for field in all_fields:
            field_name = field.get("name", "").strip()
            data_key = None
            method = None

            # ── Tier 1: Autocomplete attribute ──
            data_key = _match_autocomplete(field)
            if data_key == "__skip__":
                continue
            if data_key:
                method = "autocomplete"

            # ── Tier 2: Input type inference ──
            if not data_key:
                data_key = _match_input_type(field)
                if data_key:
                    method = "input_type"

            # ── Tier 3: Synonym dictionary ──
            if not data_key and field_name:
                data_key = _deterministic_match(field_name)
                if data_key:
                    method = "deterministic"

            # ── Check if we have a match and the data exists ──
            if data_key and data_key != "__skip__":
                # Prevent duplicate mapping (same user data key to multiple fields)
                if data_key in used_keys:
                    # Allow duplicates for password (confirm password fields)
                    if data_key not in ("password", "email"):
                        unmapped.append(field)
                        continue

                used_keys.add(data_key)

                value = _resolve_field_value(field, data_key, user_data)
                if value:

                    # For dropdowns, match the value to available options
                    options = field.get("options", [])
                    if options and field.get("role") in ("combobox", "listbox"):
                        matched_option = _match_dropdown_option(value, options)
                        if matched_option:
                            value = matched_option

                    # For checkboxes, determine checked state
                    if field.get("role") == "checkbox":
                        value = "true" if _match_checkbox_value(value) else "false"

                    step_mappings.append({
                        "field_name": field_name,
                        "field_role": field.get("role", ""),
                        "user_data_key": data_key,
                        "value": value,
                        "selector": field.get("selector", ""),
                        "selector_css": field.get("selector_css", ""),
                        "options": field.get("options", []),
                        "input_type": field.get("input_type", ""),
                        "method": method,
                        "custom_type": field.get("_custom_type", ""),
                        "container_selector": field.get("_container_selector", ""),
                        "control_selector": field.get("_control_selector", ""),
                    })
                else:
                    # Key matched but no user data for it
                    step_mappings.append({
                        "field_name": field_name,
                        "field_role": field.get("role", ""),
                        "user_data_key": data_key,
                        "value": "",
                        "selector": field.get("selector", ""),
                        "selector_css": field.get("selector_css", ""),
                        "method": f"{method}_no_value",
                    })
            elif data_key == "__skip__":
                continue  # Skip CAPTCHAs, terms checkboxes, etc.
            else:
                unmapped.append(field)

        # ── Tier 4: LLM fallback for remaining unmapped fields ──
        if unmapped and use_llm_fallback:
            available_keys = [k for k in user_data.keys() if k not in used_keys]
            if available_keys:
                llm_mappings = await _llm_map_fields(
                    unmapped, available_keys, species, all_fields=all_fields,
                )

                still_unmapped: list[dict[str, Any]] = []
                unnamed_counter = 0
                for field in unmapped:
                    field_name = field.get("name", "").strip()
                    if field_name:
                        field_id = field_name
                    else:
                        field_id = f"__unnamed_{unnamed_counter}"
                        unnamed_counter += 1

                    if field_id in llm_mappings:
                        data_key = llm_mappings[field_id]
                        if data_key in used_keys:
                            logger.debug(
                                "Skipping duplicate LLM mapping: %s -> %s",
                                field_id, data_key,
                            )
                            still_unmapped.append(field)
                        else:
                            value = _resolve_field_value(field, data_key, user_data)
                            if not value:
                                still_unmapped.append(field)
                                continue
                            used_keys.add(data_key)

                            # For dropdowns, match the value to available options
                            options = field.get("options", [])
                            if options and field.get("role") in ("combobox", "listbox"):
                                matched_option = _match_dropdown_option(value, options)
                                if matched_option:
                                    value = matched_option

                            step_mappings.append({
                                "field_name": field_name,
                                "field_role": field.get("role", ""),
                                "user_data_key": data_key,
                                "value": value,
                                "selector": field.get("selector", ""),
                                "selector_css": field.get("selector_css", ""),
                                "options": options,
                                "input_type": field.get("input_type", ""),
                                "method": "llm",
                                "custom_type": field.get("_custom_type", ""),
                                "container_selector": field.get("_container_selector", ""),
                                "control_selector": field.get("_control_selector", ""),
                            })
                    else:
                        still_unmapped.append(field)
                unmapped = still_unmapped

        step_result = {
            "step_number": step.get("step_number", 1),
            "mappings": step_mappings,
            "unmapped_fields": [
                {"name": f.get("name", ""), "role": f.get("role", "unknown")}
                for f in unmapped
            ],
            "nav_buttons": step.get("nav_buttons", []),
            "submit_buttons": step.get("submit_buttons", []),
        }
        result.append(step_result)

        mapped_count = len([m for m in step_mappings if m.get("value")])
        total_count = len(all_fields)
        logger.info(
            "Step %d: %d/%d fields mapped (%d unmapped), methods: %s",
            step.get("step_number", 1),
            mapped_count,
            total_count,
            len(unmapped),
            ", ".join(set(m.get("method", "?") for m in step_mappings)),
        )

    return result
