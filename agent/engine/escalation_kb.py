"""
Grippy V4: Escalation Knowledge Base

Knows the right escalation path for every industry and region.
When a user has a problem, this module determines:
1. What industry/category the problem falls into
2. The correct escalation ladder (company → regulator → court)
3. What action to take at each step (email, form, letter)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Escalation Ladders by Industry + Region
# ──────────────────────────────────────────────────────────────────────

ESCALATION_PATHS: dict[str, dict[str, Any]] = {
    # ── Airlines (India) ──
    "airline_india": {
        "label": "Airline Complaint (India)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Airline Customer Care",
                "description": "Send a formal complaint email to the airline's customer care. Include your PNR, booking details, and a clear description of the issue. Give them 7 days to respond.",
                "wait_days": 7,
                "template": "airline_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "DGCA AirSewa",
                "description": "File a complaint on the DGCA AirSewa portal. This is India's aviation regulator. They will contact the airline on your behalf.",
                "url_hint": "airsewa.gov.in",
                "portal_key": "airsewa",
            },
            {
                "step": 3,
                "action": "form",
                "target": "National Consumer Helpline",
                "description": "If the airline and DGCA haven't resolved your issue, file with the National Consumer Helpline (1800-11-4000) or their online portal.",
                "url_hint": "consumerhelpline.gov.in",
                "portal_key": "consumer helpline",
            },
        ],
    },
    # ── Airlines (US/International) ──
    "airline_international": {
        "label": "Airline Complaint (International)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Airline Customer Service",
                "description": "Send a formal complaint to the airline. Reference your booking number, flight details, and the specific regulation violated (e.g., EU261 for European flights).",
                "wait_days": 14,
                "template": "airline_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "Department of Transportation (DOT)",
                "description": "File a complaint with the US DOT if the airline operates in the US. For EU flights, file with the relevant national enforcement body.",
                "url_hint": "airconsumer.dot.gov",
                "portal_key": "dot complaint",
            },
            {
                "step": 3,
                "action": "form",
                "target": "Better Business Bureau / Consumer Protection",
                "description": "File with the BBB or your country's consumer protection agency.",
                "portal_key": "bbb",
            },
        ],
    },
    # ── Banks (India) ──
    "bank_india": {
        "label": "Banking Complaint (India)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Bank Grievance Cell",
                "description": "Write to the bank's grievance redressal officer. Every bank is required to have one. Include your account details and complaint reference.",
                "wait_days": 30,
                "template": "bank_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "RBI Ombudsman",
                "description": "File a complaint with the RBI Banking Ombudsman. This is free and they have the power to order compensation up to Rs. 20 lakhs.",
                "url_hint": "cms.rbi.org.in",
                "portal_key": "rbi ombudsman",
            },
            {
                "step": 3,
                "action": "form",
                "target": "Consumer Forum",
                "description": "If the RBI Ombudsman doesn't resolve it, file in Consumer Court.",
                "portal_key": "consumer forum",
            },
        ],
    },
    # ── Banks (US/International) ──
    "bank_international": {
        "label": "Banking Complaint (International)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Bank Customer Service",
                "description": "Contact the bank's customer service with a formal written complaint. Keep records of all communication.",
                "wait_days": 15,
                "template": "bank_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "CFPB (Consumer Financial Protection Bureau)",
                "description": "File a complaint with the CFPB. They forward it to the bank and require a response within 15 days.",
                "url_hint": "consumerfinance.gov/complaint",
                "portal_key": "cfpb",
            },
        ],
    },
    # ── Telecom (India) ──
    "telecom_india": {
        "label": "Telecom Complaint (India)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Telecom Company Nodal Officer",
                "description": "Every telecom company has a designated Nodal Officer for complaints. Email them with your mobile number, complaint details, and previous complaint references.",
                "wait_days": 7,
                "template": "telecom_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "TRAI / Telecom Appellate Tribunal",
                "description": "If the company doesn't resolve it, file with TRAI or the Telecom Disputes Settlement and Appellate Tribunal.",
                "portal_key": "trai",
            },
            {
                "step": 3,
                "action": "form",
                "target": "Consumer Forum",
                "description": "File in Consumer Court as a last resort.",
                "portal_key": "consumer forum",
            },
        ],
    },
    # ── E-commerce (India) ──
    "ecommerce_india": {
        "label": "E-commerce Complaint (India)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Company Customer Support",
                "description": "Contact the company's customer support. Most e-commerce companies have a grievance officer listed on their website (required by law).",
                "wait_days": 7,
                "template": "ecommerce_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "National Consumer Helpline",
                "description": "File on the National Consumer Helpline portal or call 1800-11-4000. They mediate between you and the company.",
                "portal_key": "consumer helpline",
            },
            {
                "step": 3,
                "action": "form",
                "target": "Consumer Court (e-Daakhil)",
                "description": "File a case in Consumer Court through the e-Daakhil portal for amounts up to Rs. 1 crore.",
                "url_hint": "edaakhil.nic.in",
                "portal_key": "consumer court",
            },
        ],
    },
    # ── E-commerce (Singapore) ──
    "ecommerce_singapore": {
        "label": "E-commerce Complaint (Singapore)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Merchant Customer Support",
                "description": "Raise a formal complaint with the merchant first. Include the order details, what happened, and the refund or replacement you want.",
                "wait_days": 5,
                "template": "ecommerce_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "CASE Singapore",
                "description": "If the merchant does not resolve it, file with CASE Singapore so they can mediate the dispute on your behalf.",
                "url_hint": "case.org.sg",
                "portal_key": "case singapore",
            },
            {
                "step": 3,
                "action": "advise",
                "target": "Small Claims Tribunals",
                "description": "If CASE cannot resolve the issue, the next formal step is Singapore's Small Claims Tribunals.",
            },
        ],
    },
    # ── Insurance (India) ──
    "insurance_india": {
        "label": "Insurance Complaint (India)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Insurance Company Grievance Cell",
                "description": "Write to the insurance company's grievance redressal officer with your policy number and claim details.",
                "wait_days": 15,
                "template": "insurance_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "IRDAI IGMS",
                "description": "File a complaint on the IRDAI Integrated Grievance Management System. IRDAI regulates all insurance companies in India.",
                "portal_key": "irdai",
            },
            {
                "step": 3,
                "action": "form",
                "target": "Insurance Ombudsman",
                "description": "Approach the Insurance Ombudsman for your region. They can award compensation up to Rs. 30 lakhs.",
                "url_hint": "cioins.co.in",
            },
        ],
    },
    # ── General (India) ──
    "general_india": {
        "label": "General Consumer Complaint (India)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Company Customer Care",
                "description": "Send a formal complaint email to the company. Be specific about what happened, when, and what resolution you want.",
                "wait_days": 7,
                "template": "general_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "National Consumer Helpline",
                "description": "File on the National Consumer Helpline portal (consumerhelpline.gov.in) or call 1800-11-4000.",
                "portal_key": "consumer helpline",
            },
            {
                "step": 3,
                "action": "form",
                "target": "Consumer Court (e-Daakhil)",
                "description": "File in Consumer Court through e-Daakhil for legal resolution.",
                "url_hint": "edaakhil.nic.in",
            },
        ],
    },
    # ── General (International) ──
    "general_international": {
        "label": "General Consumer Complaint",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Company Customer Service",
                "description": "Contact the company directly with a formal written complaint. Document everything.",
                "wait_days": 14,
                "template": "general_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "Consumer Protection Agency",
                "description": "File with your country's consumer protection agency (BBB in US, CASE in Singapore, ACCC in Australia, etc.).",
                "portal_key": "bbb",
            },
            {
                "step": 3,
                "action": "advise",
                "target": "Small Claims Court",
                "description": "If all else fails, consider filing in small claims court. This is usually inexpensive and doesn't require a lawyer.",
            },
        ],
    },
    # ── General (Singapore) ──
    "general_singapore": {
        "label": "General Consumer Complaint (Singapore)",
        "steps": [
            {
                "step": 1,
                "action": "email",
                "target": "Company Customer Support",
                "description": "Send a formal complaint to the company first. Be specific about what happened, when it happened, and the resolution you expect.",
                "wait_days": 5,
                "template": "general_complaint_email",
            },
            {
                "step": 2,
                "action": "form",
                "target": "CASE Singapore",
                "description": "If the company does not resolve it, file with CASE Singapore so they can mediate the dispute.",
                "url_hint": "case.org.sg",
                "portal_key": "case singapore",
            },
            {
                "step": 3,
                "action": "advise",
                "target": "Small Claims Tribunals",
                "description": "If CASE cannot resolve the matter, consider filing with Singapore's Small Claims Tribunals.",
            },
        ],
    },
    # ── Government Services (India) ──
    "government_india": {
        "label": "Government Service Issue (India)",
        "steps": [
            {
                "step": 1,
                "action": "form",
                "target": "Relevant Government Portal",
                "description": "Use the official government portal for your specific service (Sarathi for driving licence, Passport Seva for passport, etc.).",
            },
            {
                "step": 2,
                "action": "form",
                "target": "CPGRAMS",
                "description": "File on the Centralized Public Grievance Redress and Monitoring System (CPGRAMS) for any government-related grievance.",
                "url_hint": "pgportal.gov.in",
                "portal_key": "cpgrams",
            },
        ],
    },
}

# ──────────────────────────────────────────────────────────────────────
# Company → Industry Classification
# ──────────────────────────────────────────────────────────────────────

COMPANY_INDUSTRY: dict[str, str] = {
    # Airlines (India)
    "indigo": "airline_india",
    "air india": "airline_india",
    "spicejet": "airline_india",
    "vistara": "airline_india",
    "go first": "airline_india",
    "akasa": "airline_india",
    "alliance air": "airline_india",
    # Airlines (International)
    "scoot": "airline_international",
    "singapore airlines": "airline_international",
    "emirates": "airline_international",
    "qatar airways": "airline_international",
    "etihad": "airline_international",
    "british airways": "airline_international",
    "lufthansa": "airline_international",
    "delta": "airline_international",
    "united airlines": "airline_international",
    "american airlines": "airline_international",
    "ryanair": "airline_international",
    "easyjet": "airline_international",
    "air asia": "airline_international",
    "thai airways": "airline_international",
    "cathay pacific": "airline_international",
    "jetstar": "airline_international",
    # Banks (India)
    "sbi": "bank_india",
    "state bank of india": "bank_india",
    "hdfc bank": "bank_india",
    "icici bank": "bank_india",
    "axis bank": "bank_india",
    "kotak bank": "bank_india",
    "pnb": "bank_india",
    "bank of baroda": "bank_india",
    "canara bank": "bank_india",
    "yes bank": "bank_india",
    "idbi bank": "bank_india",
    # Banks (International)
    "chase": "bank_international",
    "bank of america": "bank_international",
    "wells fargo": "bank_international",
    "citibank": "bank_international",
    "hsbc": "bank_international",
    "barclays": "bank_international",
    "dbs": "bank_international",
    "ocbc": "bank_international",
    "uob": "bank_international",
    # Telecom (India)
    "jio": "telecom_india",
    "reliance jio": "telecom_india",
    "airtel": "telecom_india",
    "bharti airtel": "telecom_india",
    "vi": "telecom_india",
    "vodafone idea": "telecom_india",
    "bsnl": "telecom_india",
    # E-commerce (India)
    "amazon": "ecommerce_india",
    "amazon india": "ecommerce_india",
    "flipkart": "ecommerce_india",
    "myntra": "ecommerce_india",
    "meesho": "ecommerce_india",
    "nykaa": "ecommerce_india",
    "ajio": "ecommerce_india",
    "tata cliq": "ecommerce_india",
    # E-commerce (International)
    "amazon us": "general_international",
    "ebay": "general_international",
    "walmart": "general_international",
    "shopee": "ecommerce_singapore",
    "lazada": "ecommerce_singapore",
    # Food Delivery (India)
    "swiggy": "ecommerce_india",
    "zomato": "ecommerce_india",
    # Ride-hailing (India)
    "ola": "ecommerce_india",
    "uber": "ecommerce_india",
    "uber india": "ecommerce_india",
    "grab": "general_singapore",
    # Fintech (India)
    "paytm": "ecommerce_india",
    "phonepe": "ecommerce_india",
    "google pay": "ecommerce_india",
    "cred": "ecommerce_india",
    # Insurance (India)
    "lic": "insurance_india",
    "star health": "insurance_india",
    "hdfc ergo": "insurance_india",
    "icici lombard": "insurance_india",
    "bajaj allianz": "insurance_india",
    "max life": "insurance_india",
    "tata aia": "insurance_india",
    # Government (India)
    "passport": "government_india",
    "driving licence": "government_india",
    "sarathi": "government_india",
    "aadhaar": "government_india",
    "pan card": "government_india",
    "income tax": "government_india",
    "gst": "government_india",
    "epfo": "government_india",
    "digilocker": "government_india",
}

# ──────────────────────────────────────────────────────────────────────
# Email Templates
# ──────────────────────────────────────────────────────────────────────

EMAIL_TEMPLATES: dict[str, str] = {
    "airline_complaint_email": """Subject: Formal Complaint Regarding {issue_summary} - Booking Reference: {booking_ref}

Dear {company_name} Customer Care,

I am writing to formally register a complaint regarding my recent experience with {company_name}.

Booking/PNR Details:
- Booking Reference: {booking_ref}
- Flight: {flight_details}
- Date of Travel: {travel_date}
- Passenger Name: {passenger_name}

Issue Description:
{detailed_complaint}

Resolution Requested:
{resolution_requested}

I request that this matter be resolved within 7 working days. If I do not receive a satisfactory response, I will be compelled to escalate this complaint to the Directorate General of Civil Aviation (DGCA) and the Consumer Forum.

Please acknowledge receipt of this complaint and provide a complaint reference number.

Regards,
{user_name}
{user_email}
{user_phone}""",

    "bank_complaint_email": """Subject: Formal Grievance - {issue_summary} - Account: {account_ref}

Dear Grievance Redressal Officer,

I am writing to formally lodge a grievance regarding my account/service with {company_name}.

Account Details:
- Account/Reference: {account_ref}
- Customer Name: {user_name}

Issue Description:
{detailed_complaint}

Resolution Requested:
{resolution_requested}

As per RBI guidelines, I request resolution within 30 days. If unresolved, I will escalate to the RBI Banking Ombudsman under the Reserve Bank - Integrated Ombudsman Scheme, 2021.

Regards,
{user_name}
{user_email}
{user_phone}""",

    "telecom_complaint_email": """Subject: Complaint Regarding {issue_summary} - Mobile: {mobile_number}

Dear Nodal Officer,

I am writing to register a formal complaint regarding my {company_name} service.

Service Details:
- Mobile Number: {mobile_number}
- Customer Name: {user_name}

Issue Description:
{detailed_complaint}

Resolution Requested:
{resolution_requested}

I request resolution within 7 days as per TRAI regulations. If unresolved, I will escalate to TRAI.

Regards,
{user_name}
{user_email}""",

    "ecommerce_complaint_email": """Subject: Complaint - {issue_summary} - Order: {order_ref}

Dear {company_name} Grievance Officer,

I am writing to formally complain about an issue with my recent order/service.

Order Details:
- Order Reference: {order_ref}
- Customer Name: {user_name}

Issue Description:
{detailed_complaint}

Resolution Requested:
{resolution_requested}

As per the Consumer Protection (E-Commerce) Rules, 2020, I request resolution within 48 hours. If unresolved, I will file with the National Consumer Helpline and Consumer Court.

Regards,
{user_name}
{user_email}
{user_phone}""",

    "insurance_complaint_email": """Subject: Grievance - {issue_summary} - Policy: {policy_ref}

Dear Grievance Redressal Officer,

I am writing to formally register a grievance regarding my insurance policy with {company_name}.

Policy Details:
- Policy Number: {policy_ref}
- Policyholder: {user_name}

Issue Description:
{detailed_complaint}

Resolution Requested:
{resolution_requested}

I request resolution within 15 days as per IRDAI guidelines. If unresolved, I will escalate to IRDAI IGMS and the Insurance Ombudsman.

Regards,
{user_name}
{user_email}
{user_phone}""",

    "general_complaint_email": """Subject: Formal Complaint - {issue_summary}

Dear {company_name} Customer Care,

I am writing to formally register a complaint regarding my experience with {company_name}.

Details:
- Customer Name: {user_name}
- Reference: {reference_number}

Issue Description:
{detailed_complaint}

Resolution Requested:
{resolution_requested}

I request a response within 7 working days. If unresolved, I will escalate to the appropriate consumer protection authority.

Regards,
{user_name}
{user_email}
{user_phone}""",

    "followup_email": """Subject: Follow-up: Complaint Regarding {issue_summary} — No Response Received

Dear {company_name} Customer Care,

I am writing to follow up on my earlier complaint regarding {issue_summary}.

Original Complaint Details:
- Customer Name: {user_name}
- Reference: {reference_number}

It has been over {resolution_requested} since my original complaint, and I have not received any response or resolution.

I urge you to address this matter immediately. If I do not receive a satisfactory response within 3 working days, I will be compelled to escalate this complaint to the appropriate regulatory authority and consumer protection forum.

Please treat this as urgent.

Regards,
{user_name}
{user_email}
{user_phone}""",
}


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def classify_industry(company: str) -> str | None:
    """
    Classify a company into an industry category.
    Returns the industry key or None if unknown.
    """
    company_lower = company.lower().strip()
    
    # Direct match
    if company_lower in COMPANY_INDUSTRY:
        return COMPANY_INDUSTRY[company_lower]
    
    # Partial match
    for key, industry in COMPANY_INDUSTRY.items():
        if key in company_lower or company_lower in key:
            return industry
    
    return None


def _default_general_category(company: str, complaint: str) -> str:
    """Pick the safest general ladder when direct classification fails."""
    combined = f"{company} {complaint}".lower()

    singapore_tokens = (
        "singapore", "singaporean", " case ", "case singapore", "sg",
        "shopee", "lazada", "grab", "dbs", "ocbc", "uob", "singtel",
        "starhub", "orchard", "nric",
    )
    if any(token in combined for token in singapore_tokens):
        return "general_singapore"

    india_tokens = (
        "india", "indian", "indiago", "indigo", "dgca", "rbi", "trai",
        "consumer helpline", "edaakhil", "aadhaar", "gst", "pan card",
    )
    if any(token in combined for token in india_tokens):
        return "general_india"

    return "general_international"


async def classify_industry_llm(company: str, complaint: str) -> str:
    """
    Use LLM to classify the industry when direct lookup fails.
    Returns the industry key.
    """
    try:
        client = AsyncOpenAI()
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You classify consumer complaints into industry categories. "
                        "Given a company name and complaint, return the most appropriate "
                        "category from this list:\n"
                        + "\n".join(f"- {k}: {v['label']}" for k, v in ESCALATION_PATHS.items())
                        + "\n\nReturn ONLY the category key (e.g., 'airline_india'). "
                        "If the company is Indian or the user seems Indian, prefer the _india variant. "
                        "If the company or complaint is Singapore-related, prefer the _singapore variant. "
                        "If unsure, use 'general_india' for Indian context, 'general_singapore' for Singapore context, "
                        "or 'general_international' otherwise."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Company: {company}\nComplaint: {complaint}",
                },
            ],
            temperature=0.0,
        )
        category = resp.choices[0].message.content.strip().lower()
        # Validate
        if category in ESCALATION_PATHS:
            return category
    except Exception as exc:
        logger.warning("Industry classification LLM failed: %s", exc)

    return _default_general_category(company, complaint)


def get_escalation_path(industry: str) -> dict[str, Any] | None:
    """Get the escalation path for an industry."""
    return ESCALATION_PATHS.get(industry)


def get_email_template(template_key: str) -> str | None:
    """Get an email template by key."""
    return EMAIL_TEMPLATES.get(template_key)


async def generate_strategy(
    company: str,
    complaint: str,
    user_name: str = "",
) -> dict[str, Any]:
    """
    Generate a complete complaint strategy for a user's problem.
    
    Returns:
        {
            "industry": "airline_india",
            "label": "Airline Complaint (India)",
            "company": "Indigo",
            "recommended_action": "email",  # what to do first
            "escalation_path": [...],  # full ladder
            "strategy_summary": "...",  # human-readable summary
        }
    """
    # Step 1: Classify industry
    industry = classify_industry(company)
    if not industry:
        industry = await classify_industry_llm(company, complaint)
    
    # Step 2: Get escalation path
    path = get_escalation_path(industry)
    if not path:
        industry = _default_general_category(company, complaint)
        path = ESCALATION_PATHS[industry]
    
    # Step 3: Determine recommended first action
    first_step = path["steps"][0]
    
    # Step 4: Generate a human-readable strategy summary
    steps_text = []
    for step in path["steps"]:
        steps_text.append(
            f"**Step {step['step']}: {step['target']}**\n{step['description']}"
        )
    
    greeting = f"Hi{' ' + user_name if user_name else ''}! " if user_name else ""
    
    summary = (
        f"{greeting}I understand your issue with {company}. "
        f"Here's what I recommend:\n\n"
        + "\n\n".join(steps_text)
        + "\n\nLet's start with Step 1. "
    )
    
    if first_step["action"] == "email":
        summary += "I'll draft a strong complaint email for you. "
        summary += "Do you have any reference numbers (booking ID, order number, etc.) I should include?"
    elif first_step["action"] == "form":
        summary += f"I'll help you fill the form on {first_step['target']}."
    
    return {
        "industry": industry,
        "label": path["label"],
        "company": company,
        "recommended_action": first_step["action"],
        "escalation_path": path["steps"],
        "strategy_summary": summary,
        "first_step": first_step,
    }


async def draft_complaint_email(
    template_key: str,
    company: str,
    complaint: str,
    user_data: dict[str, str],
    complaint_data: dict[str, str] | None = None,
) -> str:
    """
    Draft a complaint email using templates + LLM enhancement.
    """
    complaint_data = complaint_data or {}
    
    # Merge all available data
    fill_data = {
        "company_name": company,
        "user_name": user_data.get("full_name", user_data.get("first_name", "User")),
        "user_email": user_data.get("email", ""),
        "user_phone": user_data.get("phone", ""),
        "detailed_complaint": complaint,
        "issue_summary": complaint[:80] + ("..." if len(complaint) > 80 else ""),
        "resolution_requested": complaint_data.get("resolution", "Full refund and appropriate compensation"),
        "booking_ref": complaint_data.get("booking_ref", complaint_data.get("pnr", "N/A")),
        "flight_details": complaint_data.get("flight", "N/A"),
        "travel_date": complaint_data.get("date", "N/A"),
        "passenger_name": user_data.get("full_name", "N/A"),
        "account_ref": complaint_data.get("account", "N/A"),
        "mobile_number": user_data.get("phone", "N/A"),
        "order_ref": complaint_data.get("order", complaint_data.get("order_id", "N/A")),
        "policy_ref": complaint_data.get("policy", "N/A"),
        "reference_number": complaint_data.get("reference", "N/A"),
    }
    
    # Get template
    template = get_email_template(template_key)
    if not template:
        template = get_email_template("general_complaint_email")
    
    # Fill template
    try:
        email_text = template.format(**fill_data)
    except KeyError:
        # If template has unknown keys, use LLM to generate
        email_text = None
    
    # If template fill failed or we want to enhance, use LLM
    if not email_text or "N/A" in email_text:
        try:
            client = AsyncOpenAI()
            resp = await client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a consumer rights expert who drafts professional complaint emails. "
                            "Write a formal, firm, polite complaint email. Include relevant consumer protection "
                            "laws and regulations. The email should be ready to send. "
                            "Do NOT include any placeholder text like [brackets] or N/A. "
                            "If information is missing, write around it naturally."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Company: {company}\n"
                            f"Complaint: {complaint}\n"
                            f"User Name: {fill_data['user_name']}\n"
                            f"User Email: {fill_data['user_email']}\n"
                            f"User Phone: {fill_data['user_phone']}\n"
                            f"Reference Numbers: {json.dumps({k: v for k, v in complaint_data.items() if v and v != 'N/A'})}\n"
                            f"Resolution Wanted: {fill_data['resolution_requested']}\n"
                            f"\nDraft a professional complaint email."
                        ),
                    },
                ],
                temperature=0.3,
            )
            email_text = resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("Email drafting LLM failed: %s", exc)
            if template:
                email_text = template.format(**fill_data)
            else:
                email_text = f"Subject: Complaint regarding {company}\n\n{complaint}"
    
    return email_text
