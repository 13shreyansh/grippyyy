"""
Scout Engine V2 — Pre-scan forms BEFORE asking the user for data.

The Scout Engine is the key innovation in Grippy V3. Instead of asking
the user for all their data upfront and hoping it matches the form,
we FIRST visit the form, scan its fields, and then figure out what
data we still need.

V2 Improvements:
  - Genome cache integration: skip 30-second browser launch for known forms
  - Species knowledge base: predict expected fields by form category
  - Smarter field matching with fuzzy scoring
  - Better question generation with permanent field detection

Flow:
  1. Take a URL (or resolve one from a company name)
  2. Check genome cache — if HIT and fresh, use cached genome (instant)
  3. If MISS, navigate to the form (using Wizard Navigator if needed)
  4. Extract the genome (all fields, types, options)
  5. Save genome to cache for future use
  6. Compare against user profile + species knowledge
  7. Return: {have, missing, case_specific} field lists
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from .genome_extractor import extract_genome
from .genome_classifier import classify_genome
from .genome_db import get_cached_genome, save_genome_to_cache
from .user_store import (
    get_profile_for_form,
    is_permanent_field,
    PERMANENT_FIELDS,
    FIELD_ALIASES,
)

logger = logging.getLogger(__name__)

# Cache freshness threshold (seconds) — 24 hours
CACHE_MAX_AGE_SECONDS = 86400


# ──────────────────────────────────────────────────────────────────────
# Species Knowledge Base — Predict expected fields by form category
# ──────────────────────────────────────────────────────────────────────

SPECIES_KNOWLEDGE: dict[str, dict[str, Any]] = {
    "airline_complaint": {
        "expected_fields": [
            "pnr_number", "flight_number", "booking_reference",
            "travel_date", "departure_city", "arrival_city",
            "ticket_amount", "seat_number",
        ],
        "questions": {
            "pnr_number": "What is your PNR or booking reference number?",
            "flight_number": "What was the flight number (e.g., TR123)?",
            "booking_reference": "What is your booking confirmation number?",
            "travel_date": "What was the date of travel?",
            "departure_city": "Which city did you depart from?",
            "arrival_city": "Which city were you flying to?",
            "ticket_amount": "How much did you pay for the ticket?",
            "seat_number": "What was your seat number (if applicable)?",
        },
    },
    "bank_complaint": {
        "expected_fields": [
            "account_number", "transaction_date", "transaction_amount",
            "branch_name", "card_number_last4",
        ],
        "questions": {
            "account_number": "What is your account number?",
            "transaction_date": "What was the date of the transaction?",
            "transaction_amount": "What was the transaction amount?",
            "branch_name": "Which branch is your account with?",
            "card_number_last4": "What are the last 4 digits of your card?",
        },
    },
    "telecom_complaint": {
        "expected_fields": [
            "phone_number", "account_number", "plan_name",
            "billing_period", "amount_disputed",
        ],
        "questions": {
            "phone_number": "What is the phone number associated with the complaint?",
            "account_number": "What is your telecom account number?",
            "plan_name": "What plan are you on?",
            "billing_period": "Which billing period is this about?",
            "amount_disputed": "What is the disputed amount?",
        },
    },
    "ecommerce_complaint": {
        "expected_fields": [
            "order_number", "order_date", "product_name",
            "order_amount", "tracking_number",
        ],
        "questions": {
            "order_number": "What is your order number?",
            "order_date": "When did you place the order?",
            "product_name": "What product is this about?",
            "order_amount": "What was the order amount?",
            "tracking_number": "What is the tracking number (if applicable)?",
        },
    },
    "insurance_complaint": {
        "expected_fields": [
            "policy_number", "claim_number", "incident_date",
            "claim_amount", "policy_type",
        ],
        "questions": {
            "policy_number": "What is your policy number?",
            "claim_number": "What is the claim reference number?",
            "incident_date": "When did the incident occur?",
            "claim_amount": "What is the claim amount?",
            "policy_type": "What type of insurance policy is this?",
        },
    },
    "government_form": {
        "expected_fields": [
            "id_number", "passport_number", "nationality",
            "occupation", "annual_income",
        ],
        "questions": {
            "id_number": "What is your national ID / NRIC / SSN number?",
            "passport_number": "What is your passport number?",
            "nationality": "What is your nationality?",
            "occupation": "What is your occupation?",
            "annual_income": "What is your annual income?",
        },
    },
    "registration_form": {
        "expected_fields": [
            "username", "password",
        ],
        "questions": {
            "username": "What username would you like to use?",
            "password": "What password would you like to set?",
        },
    },
}


# ──────────────────────────────────────────────────────────────────────
# URL Resolution — Find the complaint form for a company
# ──────────────────────────────────────────────────────────────────────

KNOWN_URLS: dict[str, str] = {
    # ── Indian Airlines ──
    "indigo": "https://www.goindigo.in/contact-us.html",
    "6e": "https://www.goindigo.in/contact-us.html",
    "air india": "https://www.airindia.com/in/en/contact-us.html",
    "vistara": "https://www.airvistara.com/in/en/contact-us",
    "spicejet": "https://www.spicejet.com/contact-us",
    "akasa air": "https://www.akasaair.com/contact-us",
    "go first": "https://www.flygofirst.com/contact-us",
    "alliance air": "https://www.allianceair.in/contact-us",
    # ── International Airlines ──
    "scoot": "https://www.flyscoot.com/en/contact-us",
    "scoot airlines": "https://www.flyscoot.com/en/contact-us",
    "singapore airlines": "https://www.singaporeair.com/en_UK/sg/contact-us/",
    "emirates": "https://www.emirates.com/in/english/help/contact-us/",
    "qatar airways": "https://www.qatarairways.com/en/contact-us.html",
    "etihad": "https://www.etihad.com/en/help/contact-us",
    "british airways": "https://www.britishairways.com/travel/customerservice/public/en_in",
    "lufthansa": "https://www.lufthansa.com/in/en/feedback",
    "thai airways": "https://www.thaiairways.com/en/contact_us/index.page",
    "cathay pacific": "https://www.cathaypacific.com/cx/en_IN/contact-us.html",
    "malaysia airlines": "https://www.malaysiaairlines.com/in/en/contact-us.html",
    "united airlines": "https://www.united.com/en/us/customer-care",
    "delta airlines": "https://www.delta.com/us/en/need-help/overview",
    "american airlines": "https://www.aa.com/contact/forms",
    "ryanair": "https://www.ryanair.com/gb/en/useful-info/help-centre/contact-us",
    "easyjet": "https://www.easyjet.com/en/help/contact",
    "air asia": "https://support.airasia.com/s/?language=en_GB",
    "jetstar": "https://www.jetstar.com/au/en/contact-us",
    # ── Indian Regulatory Bodies ──
    "airsewa": "https://airsewa.gov.in/grievance/passgrievance",
    "air sewa": "https://airsewa.gov.in/grievance/passgrievance",
    "dgca": "https://airsewa.gov.in/grievance/passgrievance",
    "dgca airsewa": "https://airsewa.gov.in/grievance/passgrievance",
    "directorate general of civil aviation": "https://airsewa.gov.in/grievance/passgrievance",
    "aviation authority india": "https://airsewa.gov.in/grievance/passgrievance",
    "cpgrams": "https://pgportal.gov.in/",
    "public grievance": "https://pgportal.gov.in/",
    "e-daakhil": "https://edaakhil.nic.in/",
    "edaakhil": "https://edaakhil.nic.in/",
    "consumer court": "https://edaakhil.nic.in/",
    "rbi": "https://cms.rbi.org.in/",
    "reserve bank of india": "https://cms.rbi.org.in/",
    "rbi ombudsman": "https://cms.rbi.org.in/",
    "banking ombudsman": "https://cms.rbi.org.in/",
    "trai": "https://www.trai.gov.in/consumer-info/lodge-complaint",
    "telecom regulatory authority": "https://www.trai.gov.in/consumer-info/lodge-complaint",
    "irdai": "https://igms.irda.gov.in/",
    "insurance regulatory authority": "https://igms.irda.gov.in/",
    "consumer forum": "https://consumerhelpline.gov.in/",
    "consumer helpline": "https://consumerhelpline.gov.in/",
    "national consumer helpline": "https://consumerhelpline.gov.in/",
    "consumer court": "https://consumerhelpline.gov.in/",
    "ncdrc": "https://ncdrc.nic.in/",
    "rera": "https://rera.gov.in/",
    "sebi": "https://scores.gov.in/scores/Welcome.html",
    "securities and exchange board": "https://scores.gov.in/scores/Welcome.html",
    "epfo": "https://epfigms.gov.in/",
    "provident fund": "https://epfigms.gov.in/",
    "income tax": "https://www.incometax.gov.in/iec/foportal/",
    "gst": "https://selfservice.gstsystem.in/",
    "passport": "https://www.passportindia.gov.in/AppOnlineProject/online/procFormSubStg1",
    "passport seva": "https://www.passportindia.gov.in/AppOnlineProject/online/procFormSubStg1",
    # ── Indian Government Services ──
    "sarathi": "https://sarathi.parivahan.gov.in/sarathiservice/stateSelection.do",
    "driving licence": "https://sarathi.parivahan.gov.in/sarathiservice/stateSelection.do",
    "vahan": "https://vahan.parivahan.gov.in/vahan4dashboard/",
    "vehicle registration": "https://vahan.parivahan.gov.in/vahan4dashboard/",
    "digilocker": "https://www.digilocker.gov.in/",
    "aadhaar": "https://uidai.gov.in/en/contact-support.html",
    "pan card": "https://www.onlineservices.nsdl.com/paam/endUserRegisterContact.html",
    # ── US Regulatory Bodies ──
    "cfpb": "https://www.consumerfinance.gov/complaint/",
    "consumer financial protection bureau": "https://www.consumerfinance.gov/complaint/",
    "ftc": "https://reportfraud.ftc.gov/",
    "federal trade commission": "https://reportfraud.ftc.gov/",
    "fcc": "https://consumercomplaints.fcc.gov/hc/en-us",
    "federal communications commission": "https://consumercomplaints.fcc.gov/hc/en-us",
    "bbb": "https://www.bbb.org/file-a-complaint",
    "better business bureau": "https://www.bbb.org/file-a-complaint",
    "faa": "https://hotline.faa.gov/",
    "dot complaint": "https://airconsumer.dot.gov/escomplaint/ConsumerForm.cfm",
    "department of transportation": "https://airconsumer.dot.gov/escomplaint/ConsumerForm.cfm",
    # ── Singapore ──
    "case singapore": "https://crdcomplaints.azurewebsites.net/",
    "case": "https://crdcomplaints.azurewebsites.net/",
    "grab": "https://help.grab.com/passenger/en-sg/",
    "shopee": "https://help.shopee.sg/portal",
    "lazada": "https://www.lazada.sg/contact/",
    "dbs": "https://www.dbs.com.sg/personal/support/contact-us.html",
    "ocbc": "https://www.ocbc.com/group/contact-us",
    "uob": "https://www.uob.com.sg/personal/contact-us.page",
    "singtel": "https://www.singtel.com/personal/support/contact-us",
    "starhub": "https://www.starhub.com/personal/support/contact-us.html",
    "m1": "https://www.m1.com.sg/support/contact-us",
    # ── Indian E-commerce & Tech ──
    "amazon": "https://www.amazon.in/gp/help/customer/contact-us",
    "amazon india": "https://www.amazon.in/gp/help/customer/contact-us",
    "amazon us": "https://www.amazon.com/gp/help/customer/contact-us",
    "flipkart": "https://www.flipkart.com/helpcentre",
    "myntra": "https://www.myntra.com/contactus",
    "swiggy": "https://www.swiggy.com/support",
    "zomato": "https://www.zomato.com/contact",
    "ola": "https://www.olacabs.com/support",
    "uber": "https://help.uber.com/",
    "uber india": "https://help.uber.com/",
    "paytm": "https://paytm.com/care",
    "phonepe": "https://support.phonepe.com/",
    "google pay": "https://support.google.com/googlepay/gethelp",
    "cred": "https://cred.club/support",
    "meesho": "https://www.meesho.com/contact",
    "nykaa": "https://www.nykaa.com/contact-us",
    "bigbasket": "https://www.bigbasket.com/contact/",
    "dunzo": "https://www.dunzo.com/contact",
    "makemytrip": "https://www.makemytrip.com/support/contact-us.html",
    "goibibo": "https://www.goibibo.com/support/",
    "cleartrip": "https://www.cleartrip.com/support",
    "irctc": "https://www.irctc.co.in/nget/train-search",
    # ── Indian Banks ──
    "sbi": "https://crcf.sbi.co.in/ccf/",
    "state bank of india": "https://crcf.sbi.co.in/ccf/",
    "hdfc bank": "https://www.hdfcbank.com/personal/need-help",
    "icici bank": "https://www.icicibank.com/customer-service",
    "axis bank": "https://www.axisbank.com/contact-us",
    "kotak bank": "https://www.kotak.com/en/contact-us.html",
    "pnb": "https://www.pnbindia.in/customer-care.html",
    "bank of baroda": "https://www.bankofbaroda.in/contact-us",
    "canara bank": "https://canarabank.com/pages/contact-us",
    "yes bank": "https://www.yesbank.in/contact-us",
    "idbi bank": "https://www.idbibank.in/contact-us.aspx",
    # ── Indian Telecom ──
    "jio": "https://www.jio.com/selfcare/support/",
    "reliance jio": "https://www.jio.com/selfcare/support/",
    "airtel": "https://www.airtel.in/help",
    "bharti airtel": "https://www.airtel.in/help",
    "vi": "https://www.myvi.in/help-and-support",
    "vodafone idea": "https://www.myvi.in/help-and-support",
    "bsnl": "https://www.bsnl.co.in/opencms/bsnl/BSNL/contact_us.html",
    # ── Indian Insurance ──
    "lic": "https://licindia.in/customer-services/grievance-redressal",
    "life insurance corporation": "https://licindia.in/customer-services/grievance-redressal",
    "star health": "https://www.starhealth.in/contact-us",
    "hdfc ergo": "https://www.hdfcergo.com/contact-us",
    "icici lombard": "https://www.icicilombard.com/contact-us",
    "bajaj allianz": "https://www.bajajallianz.com/contact-us.html",
    # ── Global Tech ──
    "google": "https://support.google.com/",
    "apple": "https://support.apple.com/contact",
    "microsoft": "https://support.microsoft.com/contactus",
    "facebook": "https://www.facebook.com/help/contact/",
    "meta": "https://www.facebook.com/help/contact/",
    "instagram": "https://help.instagram.com/",
    "twitter": "https://help.twitter.com/",
    "x": "https://help.twitter.com/",
    "linkedin": "https://www.linkedin.com/help/linkedin",
    "netflix": "https://help.netflix.com/en/contactus",
    "spotify": "https://support.spotify.com/",
    # ── Test/Demo ──
    "demoqa": "https://demoqa.com/automation-practice-form",
    "parabank": "https://parabank.parasoft.com/parabank/register.htm",
}


# ──────────────────────────────────────────────────────────────────────
# Universal URL Resolver V2
#
# 5-layer pipeline that works for ANY company on Earth:
#   Layer 1: KNOWN_URLS cache (instant)
#   Layer 2: LLM generates smart search queries → web search → LLM picks best
#   Layer 3: LLM constructs URL from domain patterns
#   Layer 4: Validate URL is reachable
# ──────────────────────────────────────────────────────────────────────

_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


async def _web_search(query: str) -> list[dict[str, str]]:
    """
    Search the web using the best available provider.
    Uses search_provider module (SerpAPI > Tavily > DuckDuckGo).
    """
    from .search_provider import web_search as _provider_search

    results = []
    try:
        search_results = await _provider_search(query, num_results=10)
        for sr in search_results:
            results.append(sr.to_dict())
    except Exception as exc:
        logger.debug("Web search failed for '%s': %s", query, exc)

    return results


async def _validate_url(url: str) -> bool:
    """Check if a URL is reachable (returns 200-399)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(
                url,
                headers=_SEARCH_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8),
                allow_redirects=True,
            ) as resp:
                return resp.status < 400
    except Exception:
        # HEAD might be blocked; try GET
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=_SEARCH_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=8),
                    allow_redirects=True,
                ) as resp:
                    return resp.status < 400
        except Exception:
            return False


async def _llm_pick_best_url(
    company: str,
    complaint: str,
    search_results: list[dict[str, str]],
) -> str | None:
    """
    Use LLM to evaluate search results and pick the best URL
    for filing a complaint or contacting the company.
    """
    if not search_results:
        return None

    results_text = "\n".join(
        f"{i+1}. URL: {r['url']}\n   Title: {r['title']}\n   Snippet: {r['snippet']}"
        for i, r in enumerate(search_results[:8])
    )

    try:
        client = AsyncOpenAI()
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You help users find the right webpage to file complaints or contact companies. "
                        "Given search results, pick the BEST URL that leads to an actual complaint form, "
                        "contact form, feedback form, or support page where the user can submit their issue.\n\n"
                        "Prefer in this order:\n"
                        "1. Direct complaint/grievance form pages\n"
                        "2. Contact us pages with forms\n"
                        "3. Customer support/help center pages\n"
                        "4. General company pages (last resort)\n\n"
                        "Return ONLY a JSON object: {\"url\": \"...\", \"reason\": \"...\"}\n"
                        "If none of the results are useful, return {\"url\": null, \"reason\": \"...\"}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Company: {company}\n"
                        f"User's complaint: {complaint}\n\n"
                        f"Search results:\n{results_text}"
                    ),
                },
            ],
            temperature=0.0,
        )

        text = resp.choices[0].message.content.strip()
        if "```" in text:
            text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")

        data = json.loads(text)
        picked = data.get("url")
        if picked:
            logger.info("LLM picked URL: %s (reason: %s)", picked, data.get("reason", ""))
        return picked

    except Exception as exc:
        logger.warning("LLM URL picker failed: %s", exc)
        # Fallback: return the first result
        return search_results[0]["url"] if search_results else None


async def resolve_url(
    company_or_query: str,
    complaint: str = "",
    action_type: str = "complaint",
) -> dict[str, Any]:
    """
    Universal URL resolver. Works for ANY company on Earth.

    5-layer pipeline:
      1. KNOWN_URLS cache (instant, 139+ entries)
      2. LLM-guided web search (smart queries + result evaluation)
      3. LLM URL construction from domain patterns
      4. URL validation

    Args:
        company_or_query: Company name or user query
        complaint: The user's complaint text (helps LLM pick the right page)
        action_type: "complaint", "contact", "form" (guides search queries)
    """
    query_lower = company_or_query.lower().strip()

    # ── Layer 1: Known URLs cache ──────────────────────────────────
    for key, url in KNOWN_URLS.items():
        if key in query_lower or query_lower in key:
            logger.info("URL resolved from KNOWN_URLS: %s -> %s", key, url)
            return {
                "url": url,
                "source": "known",
                "company": key.title(),
                "confidence": 1.0,
            }

    # ── Layer 2: LLM-guided web search ─────────────────────────────
    # Step 2a: Generate smart search queries using LLM
    search_queries = []
    try:
        client = AsyncOpenAI()
        resp = await client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate 3 web search queries to find the complaint/contact form "
                        "for a company. Return ONLY a JSON array of 3 strings. "
                        "Make queries specific: include the company name and words like "
                        "'complaint form', 'file complaint', 'contact us', 'grievance', 'support'.\n"
                        "Example: [\"indigo airlines file complaint online\", "
                        "\"indigo 6E customer care complaint form\", "
                        "\"indigo airlines grievance redressal portal\"]"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Company: {company_or_query}\nIssue type: {action_type}\nComplaint: {complaint[:200] if complaint else 'general'}",
                },
            ],
            temperature=0.3,
        )
        text = resp.choices[0].message.content.strip()
        if "```" in text:
            text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
        search_queries = json.loads(text)
        if not isinstance(search_queries, list):
            search_queries = []
    except Exception as exc:
        logger.debug("LLM query generation failed: %s", exc)

    # Fallback queries if LLM fails
    if not search_queries:
        search_queries = [
            f"{company_or_query} complaint form online",
            f"{company_or_query} contact us customer care",
            f"{company_or_query} file complaint grievance",
        ]

    # Step 2b: Search the web with each query
    all_results = []
    seen_urls = set()
    for query in search_queries[:3]:
        results = await _web_search(query)
        for r in results:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_results.append(r)

    logger.info(
        "Web search found %d unique results for '%s'",
        len(all_results),
        company_or_query,
    )

    # Step 2c: LLM evaluates results and picks the best
    if all_results:
        best_url = await _llm_pick_best_url(
            company_or_query, complaint, all_results
        )
        if best_url:
            # Validate the URL is reachable
            is_valid = await _validate_url(best_url)
            if is_valid:
                return {
                    "url": best_url,
                    "source": "web_search",
                    "company": company_or_query.title(),
                    "confidence": 0.85,
                }
            else:
                logger.warning("LLM-picked URL is unreachable: %s", best_url)
                # Try other results
                for r in all_results[:5]:
                    if r["url"] != best_url and await _validate_url(r["url"]):
                        return {
                            "url": r["url"],
                            "source": "web_search",
                            "company": company_or_query.title(),
                            "confidence": 0.7,
                        }

    # ── Layer 3: LLM URL construction ──────────────────────────────
    try:
        client = AsyncOpenAI()
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a URL expert. Given a company name, construct the most likely "
                        "URL for their complaint/contact/support page.\n\n"
                        "Rules:\n"
                        "- Use common patterns: company.com/contact, company.com/support, company.com/complaint\n"
                        "- For Indian companies, try .in domains\n"
                        "- For government portals, try .gov.in or .nic.in domains\n"
                        "- Return ONLY a JSON object: {\"url\": \"...\", \"company\": \"...\", \"confidence\": 0.0-1.0}\n"
                        "- Set confidence to 0.6+ only if you are reasonably sure the URL exists\n"
                        "- Set confidence to 0 and url to null if you have no idea"
                    ),
                },
                {"role": "user", "content": f"{company_or_query} - {action_type}"},
            ],
            temperature=0.0,
        )

        text = resp.choices[0].message.content.strip()
        if "```" in text:
            text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")

        data = json.loads(text)
        constructed_url = data.get("url")
        confidence = float(data.get("confidence", 0.0))

        if constructed_url and confidence >= 0.5:
            # Validate
            is_valid = await _validate_url(constructed_url)
            if is_valid:
                return {
                    "url": constructed_url,
                    "source": "llm_constructed",
                    "company": data.get("company", company_or_query.title()),
                    "confidence": min(confidence, 0.75),
                }
            else:
                logger.info("LLM-constructed URL unreachable: %s", constructed_url)

    except Exception as exc:
        logger.warning("LLM URL construction failed: %s", exc)

    # ── Layer 4: Return best effort from search results ────────────
    # Even if LLM couldn't pick, return the first valid search result
    for r in all_results[:5]:
        if await _validate_url(r["url"]):
            return {
                "url": r["url"],
                "source": "web_search_fallback",
                "company": company_or_query.title(),
                "confidence": 0.5,
            }

    # ── Nothing found ──────────────────────────────────────────────
    logger.warning("URL resolution failed for: %s", company_or_query)
    return {
        "url": None,
        "source": "not_found",
        "company": company_or_query.title(),
        "confidence": 0.0,
    }


# ──────────────────────────────────────────────────────────────────────
# Cache Freshness Check
# ──────────────────────────────────────────────────────────────────────

def _is_cache_fresh(genome: dict[str, Any]) -> bool:
    """Check if a cached genome is fresh enough to use."""
    cached_at = genome.get("cached_at", "")
    if not cached_at:
        return False
    try:
        cached_time = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - cached_time).total_seconds()
        return age < CACHE_MAX_AGE_SECONDS
    except (ValueError, TypeError):
        return False


# ──────────────────────────────────────────────────────────────────────
# Form Scouting — Scan form fields and categorize
# ──────────────────────────────────────────────────────────────────────

def _normalize_field_name(name: str) -> str:
    """Normalize a form field name for matching."""
    return re.sub(r"[^a-z0-9]", "_", name.lower().strip()).strip("_")


def _match_field_to_profile(
    field: dict[str, Any], profile: dict[str, str]
) -> str | None:
    """
    Try to match a form field to a profile key.
    Returns the profile key if matched, None otherwise.
    """
    field_name = field.get("name", "")
    field_label = field.get("label", "")
    field_autocomplete = field.get("autocomplete", "")

    # Try autocomplete attribute first (most reliable)
    if field_autocomplete:
        ac_normalized = _normalize_field_name(field_autocomplete)
        if ac_normalized in profile:
            return ac_normalized
        canonical = FIELD_ALIASES.get(ac_normalized)
        if canonical and canonical in profile:
            return canonical

    # Try field name and label
    for candidate in [field_name, field_label]:
        if not candidate:
            continue
        normalized = _normalize_field_name(candidate)

        # Direct match
        if normalized in profile:
            return normalized

        # Alias match
        canonical = FIELD_ALIASES.get(normalized)
        if canonical and canonical in profile:
            return canonical

        # Partial match (e.g., "complainant_email" contains "email")
        for profile_key in profile:
            if profile_key in normalized or normalized in profile_key:
                return profile_key

    return None


async def scout_form(
    url: str,
    user_intent: str = "",
    complaint_data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Scout a form: visit it, scan its fields, and categorize them.

    V2: Checks genome cache first. If the form was previously scanned
    and the cache is fresh (<24h), uses the cached genome instantly
    instead of launching a browser (saves 15-30 seconds).
    """
    start_time = time.time()

    # Get user profile
    profile = get_profile_for_form()
    all_data = dict(profile)
    if complaint_data:
        all_data.update(complaint_data)

    # ── Step 1: Check genome cache ──────────────────────────────────
    cache_hit = False
    genome = None

    cached_genome = get_cached_genome(url)
    if cached_genome and _is_cache_fresh(cached_genome):
        cache_hit = True
        genome = cached_genome
        logger.info(
            "Scout CACHE HIT for %s (age: %s, accesses: %d)",
            url,
            genome.get("cached_at", "unknown"),
            genome.get("_access_count", 0),
        )
    else:
        # ── Step 2: Full extraction (slow path) ────────────────────
        logger.info("Scout CACHE MISS for %s — launching browser", url)
        genome = await extract_genome(
            url=url,
            handle_multi_step=True,
            user_intent=user_intent,
            user_data=all_data,
        )

        # Save to cache for future use
        save_genome_to_cache(url, genome)
        logger.info("Scout saved genome to cache for %s", url)

    # ── Step 3: Classify ────────────────────────────────────────────
    species_result = classify_genome(genome)
    species = species_result["species"]

    # ── Step 4: Categorize fields ───────────────────────────────────
    have = []
    missing = []
    case_specific = []

    for field in genome.get("fields", []):
        field_name = field.get("name", "")
        field_type = field.get("type", "text")

        # Skip hidden fields, submit buttons, etc.
        if field_type in ("hidden", "submit", "button", "reset"):
            continue

        # Try to match to profile/complaint data
        matched_key = _match_field_to_profile(field, all_data)

        field_info = {
            "name": field_name,
            "label": field.get("label", field_name),
            "type": field_type,
            "required": field.get("required", False),
            "options": field.get("options", []),
            "selector": field.get("selector_css", ""),
        }

        if matched_key:
            field_info["matched_to"] = matched_key
            field_info["value"] = all_data.get(matched_key, "")
            have.append(field_info)
        elif is_permanent_field(field_name):
            field_info["is_permanent"] = True
            missing.append(field_info)
        else:
            field_info["is_permanent"] = False
            case_specific.append(field_info)

    # ── Step 5: Generate questions ──────────────────────────────────
    questions = await _generate_questions(
        missing + case_specific, species, all_data
    )

    elapsed = time.time() - start_time
    logger.info(
        "Scout complete for %s: %d fields (%d have, %d missing, %d case-specific) "
        "in %.1fs [cache=%s]",
        url,
        len(genome.get("fields", [])),
        len(have),
        len(missing),
        len(case_specific),
        elapsed,
        "HIT" if cache_hit else "MISS",
    )

    return {
        "url": genome.get("url", url),
        "title": genome.get("title", ""),
        "species": species,
        "total_fields": len(genome.get("fields", [])),
        "fields": genome.get("fields", []),
        "have": have,
        "missing": missing,
        "case_specific": case_specific,
        "questions": questions,
        "genome": genome,
        "wizard_log": genome.get("wizard_log", []),
        "cache_hit": cache_hit,
        "scout_time_seconds": elapsed,
    }


async def _generate_questions(
    fields: list[dict[str, Any]],
    species: str,
    existing_data: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Generate human-readable questions for missing fields.
    Uses species knowledge base for predictable fields, LLM for the rest.
    """
    if not fields:
        return []

    existing_data = existing_data or {}
    questions = []
    llm_fields = []

    # Get species-specific knowledge
    species_info = SPECIES_KNOWLEDGE.get(species, {})
    species_questions = species_info.get("questions", {})

    for field in fields:
        field_name = field.get("name", "")
        field_label = field.get("label", field_name)
        normalized = _normalize_field_name(field_name)
        label_normalized = _normalize_field_name(field_label)
        canonical = FIELD_ALIASES.get(
            normalized, FIELD_ALIASES.get(label_normalized, normalized)
        )

        # Priority 1: Permanent field with predefined question
        if canonical in PERMANENT_FIELDS:
            questions.append({
                "field_name": field_name,
                "question": PERMANENT_FIELDS[canonical]["question"],
                "type": field.get("type", "text"),
                "options": field.get("options", []),
                "is_permanent": True,
                "save_as": canonical,
            })
            continue

        # Priority 2: Species knowledge base question
        matched_species_q = None
        for sk, sq in species_questions.items():
            if sk in normalized or normalized in sk or sk in label_normalized:
                matched_species_q = sq
                break

        if matched_species_q:
            questions.append({
                "field_name": field_name,
                "question": matched_species_q,
                "type": field.get("type", "text"),
                "options": field.get("options", []),
                "is_permanent": False,
                "save_as": None,
            })
            continue

        # Priority 3: LLM-generated question (batched below)
        llm_fields.append(field)

    # Use LLM for remaining fields
    if llm_fields:
        try:
            field_descriptions = []
            for f in llm_fields:
                desc = f"{f.get('label', f.get('name', 'unknown'))} ({f.get('type', 'text')})"
                if f.get("options"):
                    opts = []
                    for o in f["options"][:5]:
                        if isinstance(o, dict):
                            opts.append(o.get("label", o.get("value", str(o))))
                        else:
                            opts.append(str(o))
                    desc += f" [options: {', '.join(opts)}]"
                field_descriptions.append(desc)

            client = AsyncOpenAI()
            response = await client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You generate friendly, conversational questions to ask a user "
                            "for form field data. The form type is: " + species + ". "
                            "Return a JSON array of objects with keys: field_name, question. "
                            "Questions should be natural and brief. "
                            "If a field has options, mention the key options. "
                            "Group related fields into single questions where possible. "
                            "Return ONLY the JSON array, no other text."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "Generate questions for these fields:\n"
                        + "\n".join(field_descriptions),
                    },
                ],
                temperature=0.3,
            )

            text = response.choices[0].message.content.strip()
            if "```" in text:
                text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")

            llm_questions = json.loads(text)
            for q in llm_questions:
                matching_field = None
                for f in llm_fields:
                    if f.get("name", "") == q.get(
                        "field_name", ""
                    ) or f.get("label", "") == q.get("field_name", ""):
                        matching_field = f
                        break

                questions.append({
                    "field_name": q.get("field_name", ""),
                    "question": q.get("question", ""),
                    "type": (
                        matching_field.get("type", "text")
                        if matching_field
                        else "text"
                    ),
                    "options": (
                        matching_field.get("options", [])
                        if matching_field
                        else []
                    ),
                    "is_permanent": False,
                    "save_as": None,
                })
        except Exception as exc:
            logger.warning("LLM question generation failed: %s", exc)
            # Fallback: generate simple questions
            for f in llm_fields:
                label = f.get("label", f.get("name", "this field"))
                questions.append({
                    "field_name": f.get("name", ""),
                    "question": f"What should I enter for '{label}'?",
                    "type": f.get("type", "text"),
                    "options": f.get("options", []),
                    "is_permanent": False,
                    "save_as": None,
                })

    return questions
