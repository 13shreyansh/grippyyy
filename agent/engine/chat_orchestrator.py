"""
Chat Orchestrator V4 — The Brain of Grippy.

An intelligent complaint advisor that:
  1. Onboards users conversationally (one question at a time)
  2. Understands the problem and suggests the RIGHT course of action
  3. Drafts complaint emails when that's the best first step
  4. Scouts forms and fills them when needed
  5. Learns from every interaction

State Machine:
  ONBOARD → INTAKE → STRATEGY → [EMAIL | SCOUT → COLLECT → FILL] → LEARN → DONE

The key insight: Grippy is NOT just a form filler. It's a complaint ADVISOR.
It knows that for airlines, you email first then escalate to DGCA.
It knows that for banks, you go to the grievance cell then RBI Ombudsman.
"""

import json
import logging
import os
import re
import time
from typing import Any, Optional

from openai import AsyncOpenAI

from .user_store import (
    is_onboarded,
    get_onboarding_questions,
    get_profile,
    get_profile_for_form,
    update_profile,
    bulk_update_profile,
    is_permanent_field,
    PERMANENT_FIELDS,
    FIELD_ALIASES,
)
from .escalation_kb import (
    classify_industry,
    classify_industry_llm,
    get_escalation_path,
    generate_strategy,
    draft_complaint_email,
    ESCALATION_PATHS,
    COMPANY_INDUSTRY,
)
from agent.engine.session_store import (
    save_session as _persist_session,
    load_session as _load_persisted_session,
)
from .email_sender import get_email_status

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Conversation State
# ──────────────────────────────────────────────────────────────────────

class ConversationState:
    """Holds the state of a single conversation session."""

    def __init__(self):
        self.phase = "IDLE"
        self.complaint_data: dict[str, str] = {}
        self.target_url: str | None = None
        self.target_company: str | None = None
        self.scout_result: dict[str, Any] | None = None
        self.missing_fields: list[dict[str, Any]] = []
        self.collected_data: dict[str, str] = {}
        self.fill_result: dict[str, Any] | None = None
        self.pending_profile_updates: list[dict[str, str]] = []
        self.history: list[dict[str, str]] = []
        self.created_at: float = time.time()
        # V4 additions
        self.strategy: dict[str, Any] | None = None
        self.current_step: int = 0  # Which escalation step we're on
        self.drafted_email: str | None = None
        self.onboard_step: int = 0  # Track conversational onboarding progress

    def to_dict(self) -> dict[str, Any]:
        """Serialize all state fields into a JSON-safe dictionary."""

        def _json_safe(value: Any) -> Any:
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            if isinstance(value, list):
                return [_json_safe(item) for item in value]
            if isinstance(value, dict):
                return {str(key): _json_safe(val) for key, val in value.items()}
            return str(value)

        return {key: _json_safe(value) for key, value in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationState":
        """Rebuild a state object from persisted dictionary data."""
        state = cls()
        for key, value in data.items():
            setattr(state, key, value)
        return state


# Global session store (in-memory for MVP)
_sessions: dict[str, ConversationState] = {}


def get_session(session_id: str) -> ConversationState:
    if session_id in _sessions:
        return _sessions[session_id]

    stored = _load_persisted_session(session_id)
    if stored is not None:
        state = ConversationState.from_dict(stored)
        _sessions[session_id] = state
        return state

    _sessions[session_id] = ConversationState()
    return _sessions[session_id]


def reset_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


# ──────────────────────────────────────────────────────────────────────
# LLM Helpers
# ──────────────────────────────────────────────────────────────────────

def _get_llm_client() -> tuple[AsyncOpenAI, str]:
    client = AsyncOpenAI()
    return client, "gpt-4.1-mini"


async def _llm_call(
    system: str,
    user_msg: str,
    history: list[dict] | None = None,
    model: str | None = None,
) -> str:
    """Make a simple LLM call and return the text response."""
    client, default_model = _get_llm_client()
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-10:])  # Keep last 10 for context
    messages.append({"role": "user", "content": user_msg})

    response = await client.chat.completions.create(
        model=model or default_model,
        messages=messages,
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()


async def _llm_json(system: str, user_msg: str) -> dict[str, Any]:
    """Make an LLM call and parse JSON from the response."""
    text = await _llm_call(system, user_msg)
    if "```" in text:
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
    return json.loads(text)


# ──────────────────────────────────────────────────────────────────────
# Phase: ONBOARD (Conversational, one question at a time)
# ──────────────────────────────────────────────────────────────────────

# Onboarding questions in order of importance
_ONBOARD_FLOW = [
    {
        "key": "full_name",
        "ask": "What's your name?",
        "parse_keys": ["full_name", "given_name", "family_name"],
    },
    {
        "key": "email",
        "ask": "And your email address?",
        "parse_keys": ["email"],
    },
    {
        "key": "phone",
        "ask": "What's your phone number? (with country code if possible)",
        "parse_keys": ["phone"],
    },
]

_DISPLAY_COMPANY_NAMES = {
    "indigo": "IndiGo",
    "sbi": "SBI",
    "hdfc bank": "HDFC Bank",
    "icici bank": "ICICI Bank",
    "rbi": "RBI",
    "dbs": "DBS",
    "ocbc": "OCBC",
    "uob": "UOB",
    "vi": "VI",
}


def _extract_onboarding_fallback(message: str) -> dict[str, str]:
    """Parse basic onboarding data without relying on the LLM."""
    result: dict[str, str] = {}
    text = message.strip()
    if not text:
        return result

    email_match = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I)
    if email_match:
        result["email"] = email_match.group(0)

    phone_match = re.search(r"(\+?\d[\d\s().-]{6,}\d)", text)
    if phone_match:
        digits_only = re.sub(r"\D", "", phone_match.group(1))
        if len(digits_only) >= 7:
            result["phone"] = phone_match.group(1).strip()

    name_patterns = [
        r"\bmy name is\s+([A-Za-z][A-Za-z .'-]{1,60})$",
        r"\bi am\s+([A-Za-z][A-Za-z .'-]{1,60})$",
        r"\bi'm\s+([A-Za-z][A-Za-z .'-]{1,60})$",
    ]
    name = ""
    lowered = text.lower()
    for pattern in name_patterns:
        match = re.search(pattern, lowered, re.I)
        if match:
            start = match.start(1)
            end = match.end(1)
            name = text[start:end].strip(" .")
            break

    if not name and not result.get("email") and not result.get("phone"):
        cleaned = re.sub(r"[^A-Za-z .'-]", " ", text).strip()
        tokens = [t for t in cleaned.split() if t]
        if 1 <= len(tokens) <= 4:
            name = " ".join(tokens)

    if name:
        result["full_name"] = name
        parts = name.split(None, 1)
        result["given_name"] = parts[0]
        if len(parts) > 1:
            result["family_name"] = parts[1]

    return result


def _display_company_name(company_key: str) -> str:
    return _DISPLAY_COMPANY_NAMES.get(company_key, company_key.title())


def _first_form_step(strategy: dict[str, Any] | None) -> tuple[int, dict[str, Any] | None]:
    """Return the first form step in a strategy, if any."""
    if not strategy:
        return -1, None
    steps = strategy.get("escalation_path", [])
    for index, step in enumerate(steps):
        if step.get("action") == "form":
            return index, step
    return -1, None


def _build_form_flow_response(
    state: ConversationState,
    form_step_index: int,
    form_step: dict[str, Any],
) -> dict[str, Any]:
    """Start scouting the selected form portal."""
    company = state.target_company or "the company"
    state.current_step = form_step_index
    state.phase = "SCOUT"
    portal_key = form_step.get("portal_key", company)
    target = form_step.get("target", company)
    return {
        "reply": f"Let me open the {target} filing flow for you...",
        "phase": "SCOUT",
        "action": "start_scout",
        "scout_params": {
            "company": portal_key,
            "intent": f"File a complaint about {state.complaint_data.get('issue', 'an issue')} with {target}",
            "target": target,
        },
        "live_fill_target": target,
    }


def _category_from_industry(industry: str) -> str:
    if industry.startswith("airline"):
        return "airline"
    if industry.startswith("bank"):
        return "bank"
    if industry.startswith("telecom"):
        return "telecom"
    if industry.startswith("ecommerce"):
        return "ecommerce"
    if industry.startswith("insurance"):
        return "insurance"
    if industry.startswith("government"):
        return "government"
    return "other"


def _extract_desired_outcome(message: str) -> str:
    lowered = message.lower()
    if "refund" in lowered:
        return "Refund"
    if "replacement" in lowered or "replace" in lowered:
        return "Replacement"
    if "compensation" in lowered or "compensate" in lowered:
        return "Compensation"
    if "repair" in lowered or "fix" in lowered:
        return "Repair"
    if "restore" in lowered or "reconnect" in lowered:
        return "Service restoration"
    return "Resolution of the issue"


def _extract_reference_numbers(message: str) -> list[str]:
    matches = re.findall(r"\b[A-Z0-9]{5,}\b", message)
    return matches[:5]


def _heuristic_intake_parse(message: str) -> dict[str, Any]:
    """Fallback parser for common complaint statements and demo scenarios."""
    text = message.strip()
    lowered = text.lower()
    url_match = re.search(r"https?://[^\s]+", text)
    company_key = ""

    for key in sorted(COMPANY_INDUSTRY, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", lowered):
            company_key = key
            break

    issue = text
    reference_numbers = _extract_reference_numbers(text)
    industry = classify_industry(company_key) if company_key else None

    return {
        "company": _display_company_name(company_key) if company_key else "",
        "issue": issue,
        "category": _category_from_industry(industry) if industry else "other",
        "desired_outcome": _extract_desired_outcome(text),
        "complaint_description": text,
        "url": url_match.group(0) if url_match else "",
        "reference_numbers": reference_numbers,
        "is_complete": bool(company_key and issue),
        "follow_up_question": (
            "Which company or organization is this about?"
            if not company_key
            else "Could you tell me what happened and what outcome you want?"
        ),
        "is_form_fill": bool(
            url_match and any(word in lowered for word in ("fill", "form", "apply", "submit"))
        ),
    }


def _merge_intake_results(
    primary: dict[str, Any] | None,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Merge LLM extraction with deterministic fallback, preferring real values."""
    merged = dict(fallback)
    for key, value in (primary or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value

    merged["is_complete"] = bool(
        (primary or {}).get("is_complete")
        or (merged.get("company") and merged.get("issue"))
    )
    merged["is_form_fill"] = bool((primary or {}).get("is_form_fill") or merged.get("is_form_fill"))
    if not merged.get("follow_up_question"):
        merged["follow_up_question"] = (
            "Could you tell me which company this is about and what happened?"
        )
    return merged


async def _handle_onboard(state: ConversationState, message: str) -> dict[str, Any]:
    """Conversational onboarding — one question at a time, warm and human."""

    profile = get_profile()

    if not message.strip():
        # First visit — warm welcome
        state.onboard_step = 0
        return {
            "reply": (
                "Hey there! Welcome to **Grippy** — I'm your personal complaint assistant. "
                "I help you file complaints, draft emails, and fill forms so you don't have to.\n\n"
                "Let's set up your profile real quick so I can auto-fill forms for you. "
                f"{_ONBOARD_FLOW[0]['ask']}"
            ),
            "phase": "ONBOARD",
            "action": None,
        }

    # Parse whatever the user said
    parsed_data = _extract_onboarding_fallback(message)
    try:
        result = await _llm_json(
            system=(
                "Extract personal information from the user's message. "
                "Return a JSON object with any of these keys: "
                "full_name, given_name, family_name, email, phone, date_of_birth, "
                "gender, salutation, address_line1, city, state, postal_code, country. "
                "Only include keys where you found clear data. "
                "If the user provides just a name, extract full_name, given_name, family_name. "
                "Return ONLY the JSON object."
            ),
            user_msg=message,
        )

        if result:
            parsed_data.update(
                {key: value for key, value in result.items() if value}
            )
    except Exception as exc:
        logger.warning("Onboarding parse failed: %s", exc)

    if parsed_data:
        bulk_update_profile(parsed_data, source="onboarding")

    # Check what we still need
    profile = get_profile()
    has_name = bool(profile.get("full_name") or profile.get("given_name"))
    has_email = bool(profile.get("email"))
    has_phone = bool(profile.get("phone"))

    if has_name and has_email and has_phone:
        # Onboarding complete!
        name = profile.get("full_name", profile.get("given_name", "there"))
        state.phase = "IDLE"
        return {
            "reply": (
                f"All set, {name}! I've saved your info.\n\n"
                "Now tell me — **what's bothering you?** Just describe your problem "
                "and I'll figure out the best way to resolve it.\n\n"
                "For example:\n"
                "- *\"Indigo Airlines cancelled my flight and won't refund me\"*\n"
                "- *\"Amazon delivered a damaged product\"*\n"
                "- *\"I need to apply for a driving licence\"*"
            ),
            "phase": "IDLE",
            "action": None,
        }
    elif has_name and has_email:
        return {
            "reply": f"Got it! {_ONBOARD_FLOW[2]['ask']}",
            "phase": "ONBOARD",
            "action": None,
        }
    elif has_name:
        name = profile.get("full_name", profile.get("given_name", ""))
        return {
            "reply": f"Nice to meet you, {name}! {_ONBOARD_FLOW[1]['ask']}",
            "phase": "ONBOARD",
            "action": None,
        }
    else:
        return {
            "reply": (
                "I didn't quite catch your name. Could you tell me? "
                "Just type your full name."
            ),
            "phase": "ONBOARD",
            "action": None,
        }


# ──────────────────────────────────────────────────────────────────────
# Phase: INTAKE (Understand the problem with common sense)
# ──────────────────────────────────────────────────────────────────────

async def _handle_intake(state: ConversationState, message: str) -> dict[str, Any]:
    """Understand the user's problem intelligently."""

    # Build context from history
    history_context = ""
    if state.history:
        history_context = "\n".join(
            f"{h['role'].title()}: {h['content']}"
            for h in state.history[-6:]
        )

    fallback_result = _heuristic_intake_parse(message)

    try:
        llm_result = await _llm_json(
            system=(
                "You are Grippy, a smart complaint advisor. Analyze the user's message.\n\n"
                "Extract:\n"
                "- company: the company/organization name (required to proceed)\n"
                "- issue: brief summary of the issue\n"
                "- category: one of [airline, bank, telecom, ecommerce, insurance, government, other]\n"
                "- desired_outcome: what the user wants (refund, replacement, etc.)\n"
                "- complaint_description: detailed description\n"
                "- url: if the user provided a specific URL\n"
                "- reference_numbers: any booking IDs, order numbers, PNRs, etc.\n"
                "- is_complete: true if you have enough to suggest a strategy\n"
                "- follow_up_question: if is_complete is false, what to ask (be specific and helpful)\n"
                "- is_form_fill: true if the user just wants to fill a form (not a complaint)\n\n"
                "Be smart about context:\n"
                "- 'Indigo' means IndiGo Airlines\n"
                "- 'DGCA' means aviation regulator\n"
                "- If the user mentions a portal or form, they might want to fill it directly\n"
                "- If they describe a problem, they need complaint advice\n\n"
                "Return ONLY the JSON object."
            ),
            user_msg=(
                f"Previous conversation:\n{history_context}\n\n"
                f"Current message: {message}"
                if history_context
                else message
            ),
        )
        result = _merge_intake_results(llm_result, fallback_result)

        # Store extracted data
        state.complaint_data.update(
            {k: v for k, v in result.items()
             if v and k not in ("is_complete", "follow_up_question", "is_form_fill")}
        )

        # If it's a direct form fill request (not a complaint)
        if result.get("is_form_fill") and result.get("url"):
            state.target_url = result["url"]
            state.phase = "SCOUT"
            return {
                "reply": f"Let me scan that form for you...",
                "phase": "SCOUT",
                "action": "start_scout",
                "scout_params": {
                    "url": result["url"],
                    "intent": message,
                },
            }

        # If we have a URL but it's a complaint
        if result.get("url") and result.get("company"):
            state.target_company = result["company"]
            state.target_url = result["url"]
            state.phase = "SCOUT"
            return {
                "reply": (
                    f"Got it — I'll look into {result['company']}. "
                    f"Let me scan the form at the URL you provided..."
                ),
                "phase": "SCOUT",
                "action": "start_scout",
                "scout_params": {
                    "url": result["url"],
                    "company": result["company"],
                    "intent": f"File a complaint about {result.get('issue', 'an issue')}",
                },
            }

        # If we have enough info for a strategy
        if result.get("is_complete") or (result.get("company") and result.get("issue")):
            company = result.get("company", "the company")
            state.target_company = company
            state.phase = "STRATEGY"

            # Generate escalation strategy
            profile = get_profile()
            user_name = profile.get("full_name", profile.get("given_name", ""))
            complaint_text = result.get("complaint_description", result.get("issue", message))

            strategy = await generate_strategy(company, complaint_text, user_name)
            state.strategy = strategy

            # Build a helpful response
            reply = strategy["strategy_summary"]

            # Add action buttons hint
            first_action = strategy["first_step"]["action"]
            if first_action == "email":
                reply += "\n\nShould I **draft the complaint email** for you? Or would you prefer to go directly to filing a form?"
            elif first_action == "form":
                reply += "\n\nShall I find and fill the form for you?"

            return {
                "reply": reply,
                "phase": "STRATEGY",
                "action": None,
                "strategy": strategy,
            }

        # Need more info
        follow_up = result.get(
            "follow_up_question",
            "Could you tell me which company this is about and what happened?"
        )
        return {
            "reply": follow_up,
            "phase": "INTAKE",
            "action": None,
        }

    except Exception as exc:
        logger.warning("Intake parsing failed: %s", exc)
        result = fallback_result

    # Store extracted data from fallback or merged parse
    state.complaint_data.update(
        {
            k: v for k, v in result.items()
            if v and k not in ("is_complete", "follow_up_question", "is_form_fill")
        }
    )

    if result.get("is_form_fill") and result.get("url"):
        state.target_url = result["url"]
        state.phase = "SCOUT"
        return {
            "reply": "Let me scan that form for you...",
            "phase": "SCOUT",
            "action": "start_scout",
            "scout_params": {
                "url": result["url"],
                "intent": message,
            },
        }

    if result.get("url") and result.get("company"):
        state.target_company = result["company"]
        state.target_url = result["url"]
        state.phase = "SCOUT"
        return {
            "reply": (
                f"Got it — I'll look into {result['company']}. "
                "Let me scan the form at the URL you provided..."
            ),
            "phase": "SCOUT",
            "action": "start_scout",
            "scout_params": {
                "url": result["url"],
                "company": result["company"],
                "intent": f"File a complaint about {result.get('issue', 'an issue')}",
            },
        }

    if result.get("is_complete") or (result.get("company") and result.get("issue")):
        company = result.get("company", "the company")
        state.target_company = company
        state.phase = "STRATEGY"

        profile = get_profile()
        user_name = profile.get("full_name", profile.get("given_name", ""))
        complaint_text = result.get("complaint_description", result.get("issue", message))

        strategy = await generate_strategy(company, complaint_text, user_name)
        state.strategy = strategy

        reply = strategy["strategy_summary"]
        first_action = strategy["first_step"]["action"]
        if first_action == "email":
            reply += "\n\nShould I **draft the complaint email** for you? Or would you prefer to go directly to filing a form?"
        elif first_action == "form":
            reply += "\n\nShall I find and fill the form for you?"

        return {
            "reply": reply,
            "phase": "STRATEGY",
            "action": None,
            "strategy": strategy,
        }

    follow_up = result.get(
        "follow_up_question",
        "Could you tell me which company this is about and what happened?",
    )
    return {
        "reply": follow_up,
        "phase": "INTAKE",
        "action": None,
    }


# ──────────────────────────────────────────────────────────────────────
# Phase: STRATEGY (Decide what to do — email, form, or advise)
# ──────────────────────────────────────────────────────────────────────

async def _handle_strategy(state: ConversationState, message: str) -> dict[str, Any]:
    """Handle user response to the strategy suggestion."""

    msg_lower = message.lower().strip()

    # Check if user wants to draft an email
    email_triggers = ["email", "draft", "write", "send", "mail", "letter", "yes", "sure", "ok", "yeah", "yep", "y", "do it", "go ahead"]
    form_triggers = ["form", "fill", "portal", "website", "file", "submit", "directly", "skip"]
    escalate_triggers = ["escalate", "next step", "regulator", "dgca", "rbi", "consumer", "ombudsman", "step 2", "step 3"]

    if any(t in msg_lower for t in escalate_triggers):
        # User wants to escalate to next step
        if state.strategy:
            steps = state.strategy.get("escalation_path", [])
            state.current_step = min(state.current_step + 1, len(steps) - 1)
            step = steps[state.current_step]

            if step["action"] == "form":
                return _build_form_flow_response(state, state.current_step, step)
            elif step["action"] == "email":
                return await _draft_email(state)
            else:
                return {
                    "reply": step["description"],
                    "phase": "STRATEGY",
                    "action": None,
                }

    if any(t in msg_lower for t in form_triggers):
        form_step_index, form_step = _first_form_step(state.strategy)
        if form_step:
            return _build_form_flow_response(state, form_step_index, form_step)
        company = state.target_company or "the company"
        state.phase = "SCOUT"
        return {
            "reply": f"Let me find {company}'s complaint form...",
            "phase": "SCOUT",
            "action": "start_scout",
            "scout_params": {
                "company": company,
                "intent": f"File a complaint about {state.complaint_data.get('issue', 'an issue')} with {company}",
                "target": company,
            },
            "live_fill_target": company,
        }

    if any(t in msg_lower for t in email_triggers):
        # Draft the complaint email
        return await _draft_email(state)

    # If user provides additional info, treat as more context
    if len(message) > 20:
        # Might be providing reference numbers or more details
        try:
            result = await _llm_json(
                system=(
                    "The user is providing additional details for their complaint. "
                    "Extract any reference numbers, dates, amounts, or other details. "
                    "Return a JSON object with keys like: booking_ref, pnr, order_id, "
                    "flight, date, amount, policy, account, reference, or any other relevant data. "
                    "Return ONLY the JSON object."
                ),
                user_msg=message,
            )
            if result:
                state.complaint_data.update(result)
        except Exception:
            pass

        # Re-offer the strategy
        return {
            "reply": (
                "Thanks for the details! I've noted that.\n\n"
                "Would you like me to:\n"
                "1. **Draft a complaint email** to send to the company\n"
                "2. **Fill the complaint form** on their portal\n"
                "3. **Escalate** to the regulator"
            ),
            "phase": "STRATEGY",
            "action": None,
        }

    # Default: re-offer options
    return {
        "reply": (
            "What would you like me to do?\n"
            "- **\"Draft an email\"** — I'll write a formal complaint email\n"
            "- **\"Fill the form\"** — I'll find and fill their complaint form\n"
            "- **\"Escalate\"** — I'll help you go to the regulator"
        ),
        "phase": "STRATEGY",
        "action": None,
    }


async def _draft_email(state: ConversationState) -> dict[str, Any]:
    """Draft a complaint email for the user."""

    profile = get_profile()
    user_data = get_profile_for_form()
    company = state.target_company or "the company"
    complaint = state.complaint_data.get(
        "complaint_description",
        state.complaint_data.get("issue", ""),
    )

    # Get the right template
    template_key = "general_complaint_email"
    if state.strategy:
        first_step = state.strategy.get("first_step", {})
        template_key = first_step.get("template", template_key)

    email_text = await draft_complaint_email(
        template_key=template_key,
        company=company,
        complaint=complaint,
        user_data=user_data,
        complaint_data=state.complaint_data,
    )

    state.drafted_email = email_text
    email_status = get_email_status()
    live_send_configured = email_status.get("status") == "configured"

    next_step_lines = [
        "What would you like to do next?",
        "- **\"Fill the form\"** — Let me find their online complaint form instead",
        "- **\"Escalate\"** — Go directly to the regulator",
    ]
    if live_send_configured:
        next_step_lines.insert(
            1,
            "- **\"Send it\"** — If you confirm, I can send it from the configured email backend",
        )

    reply = (
        f"Here's your complaint email:\n\n"
        f"---\n{email_text}\n---\n\n"
        + (
            "Live email sending is configured in this environment. "
            "If you'd rather send it manually, you can also copy the draft.\n\n"
            if live_send_configured
            else "Live email sending is **not configured in this environment**, so this draft is for copy-paste only.\n\n"
        )
        + "\n".join(next_step_lines)
    )

    return {
        "reply": reply,
        "phase": "STRATEGY",
        "action": None,
    }


# ──────────────────────────────────────────────────────────────────────
# Phase: COLLECT (Gather missing data from user)
# ──────────────────────────────────────────────────────────────────────

async def _handle_collect(state: ConversationState, message: str) -> dict[str, Any]:
    """Gather missing form data from the user."""

    if not state.missing_fields:
        state.phase = "FILL"
        return {
            "reply": "I have everything I need! Filling the form now...",
            "phase": "FILL",
            "action": "start_fill",
        }

    # Parse the user's response
    field_descriptions = []
    for f in state.missing_fields:
        desc = f"{f.get('label', f.get('name', 'unknown'))}"
        if f.get("options"):
            opts = []
            for o in f["options"][:8]:
                if isinstance(o, dict):
                    opts.append(o.get("label", o.get("value", str(o))))
                else:
                    opts.append(str(o))
            desc += f" (options: {', '.join(opts)})"
        field_descriptions.append(desc)

    try:
        result = await _llm_json(
            system=(
                "Extract form field values from the user's message. "
                "The fields I'm looking for are:\n"
                + "\n".join(f"- {d}" for d in field_descriptions)
                + "\n\nReturn a JSON object where keys are the field names and values are "
                "what the user provided. Only include fields where the user gave a clear answer. "
                "If a field has options, match to the closest option. "
                "Return ONLY the JSON object."
            ),
            user_msg=message,
        )

        if result:
            for field in list(state.missing_fields):
                field_name = field.get("name", "")
                field_label = field.get("label", "")

                value = None
                for key, val in result.items():
                    if (
                        key.lower() == field_name.lower()
                        or key.lower() == field_label.lower()
                        or field_name.lower() in key.lower()
                        or key.lower() in field_name.lower()
                    ):
                        value = str(val)
                        break

                if value:
                    state.collected_data[field_name] = value
                    state.missing_fields.remove(field)

                    if field.get("is_permanent"):
                        state.pending_profile_updates.append({
                            "key": field.get("save_as", field_name),
                            "value": value,
                            "label": field_label,
                        })

        if not state.missing_fields:
            state.phase = "FILL"
            return {
                "reply": "I have everything I need! Filling the form now...",
                "phase": "FILL",
                "action": "start_fill",
            }
        else:
            remaining = [
                f"- {f.get('label', f.get('name', '?'))}"
                for f in state.missing_fields
            ]
            return {
                "reply": (
                    f"Thanks! I still need:\n"
                    + "\n".join(remaining)
                    + "\n\nCould you provide these?"
                ),
                "phase": "COLLECT",
                "action": None,
            }
    except Exception as exc:
        logger.warning("Collection parsing failed: %s", exc)
        return {
            "reply": "I couldn't quite parse that. Could you try again?",
            "phase": "COLLECT",
            "action": None,
        }


# ──────────────────────────────────────────────────────────────────────
# Phase: LEARN (Save new data to profile)
# ──────────────────────────────────────────────────────────────────────

async def _handle_learn(state: ConversationState, message: str) -> dict[str, Any]:
    """Offer to save new permanent data."""

    msg_lower = message.lower().strip()

    if any(w in msg_lower for w in ["yes", "yeah", "sure", "ok", "save", "yep", "y"]):
        saved = []
        for update in state.pending_profile_updates:
            result = update_profile(update["key"], update["value"], source="form_fill")
            saved.append(result["label"])

        state.pending_profile_updates = []
        state.phase = "DONE"

        return {
            "reply": (
                f"Saved to your profile: {', '.join(saved)}.\n\n"
                "Is there anything else I can help with?"
            ),
            "phase": "DONE",
            "action": None,
        }
    elif any(w in msg_lower for w in ["no", "nah", "nope", "skip", "n"]):
        state.pending_profile_updates = []
        state.phase = "DONE"
        return {
            "reply": "No problem! Is there anything else I can help with?",
            "phase": "DONE",
            "action": None,
        }
    else:
        state.phase = "IDLE"
        return await handle_message("", message, state)


# ──────────────────────────────────────────────────────────────────────
# Profile Update Handler
# ──────────────────────────────────────────────────────────────────────

async def _handle_profile_update(state: ConversationState, message: str) -> dict[str, Any]:
    """Handle profile update requests."""

    try:
        result = await _llm_json(
            system=(
                "The user wants to update their profile information. "
                "Extract the field(s) they want to change. "
                "Return a JSON object with keys: "
                "updates (array of {field, value}), "
                "confirmation_message (natural confirmation to show user). "
                "Valid fields: full_name, given_name, family_name, email, phone, "
                "date_of_birth, gender, salutation, address_line1, address_line2, "
                "city, state, postal_code, country, nationality. "
                "Return ONLY the JSON object."
            ),
            user_msg=message,
        )

        updates = result.get("updates", [])
        if updates:
            saved = []
            for u in updates:
                r = update_profile(u["field"], u["value"], source="user_update")
                change_type = "Updated" if r["is_update"] else "Saved"
                old = f" (was: {r['old_value']})" if r["is_update"] else ""
                saved.append(f"{change_type} **{r['label']}**: {u['value']}{old}")

            return {
                "reply": "\n".join(saved) + "\n\nAnything else?",
                "phase": state.phase,
                "action": None,
            }
    except Exception as exc:
        logger.warning("Profile update parsing failed: %s", exc)

    return {
        "reply": "I couldn't understand what you want to update. Could you be more specific?",
        "phase": state.phase,
        "action": None,
    }


async def handle_action(session_id: str, action: str) -> dict[str, Any]:
    """Execute an explicit UI action without relying on natural-language parsing."""
    state = get_session(session_id)
    action_key = action.strip().lower()

    if action_key == "draft_email":
        if state.phase != "STRATEGY":
            result = {
                "reply": "I can draft the email once we've decided on the complaint strategy.",
                "phase": state.phase,
                "action": None,
            }
        else:
            result = await _draft_email(state)
    elif action_key == "fill_form":
        if state.phase != "STRATEGY":
            result = {
                "reply": "Let's first decide the right filing path before I open a live form flow.",
                "phase": state.phase,
                "action": None,
            }
        else:
            form_step_index, form_step = _first_form_step(state.strategy)
            if form_step:
                result = _build_form_flow_response(state, form_step_index, form_step)
            else:
                result = {
                    "reply": "I don't have a form filing step for this complaint yet. I can still draft the complaint email for you.",
                    "phase": "STRATEGY",
                    "action": None,
                }
    elif action_key == "escalate":
        result = await _handle_strategy(state, "Escalate")
    elif action_key == "continue_fill":
        if state.phase != "FILL":
            result = {
                "reply": "I need to finish scanning the form before I can continue the live filing.",
                "phase": state.phase,
                "action": None,
            }
        else:
            result = {
                "reply": "I have what I need. Starting the live filing now...",
                "phase": "FILL",
                "action": "start_fill",
                "live_fill_target": state.target_company or "Live filing",
            }
    else:
        result = {
            "reply": "I didn't recognize that action.",
            "phase": state.phase,
            "action": None,
        }

    state.phase = result.get("phase", state.phase)
    reply = result.get("reply")
    if reply:
        state.history.append({"role": "assistant", "content": reply})
    _persist_session(session_id, state.to_dict())
    return result


# ──────────────────────────────────────────────────────────────────────
# Intent Detection
# ──────────────────────────────────────────────────────────────────────

def _detect_intent(message: str) -> str:
    """Detect the user's intent from their message."""
    msg = message.lower().strip()

    # Profile update patterns
    update_patterns = [
        r"my (?:new |updated )?(?:address|email|phone|name|number) is",
        r"change my",
        r"update my",
        r"i moved to",
        r"my (?:address|email|phone) changed",
    ]
    for pattern in update_patterns:
        if re.search(pattern, msg):
            return "profile_update"

    # Greeting patterns
    if msg in ("hi", "hello", "hey", "yo", "sup", "start", ""):
        return "greeting"

    # Help patterns
    if msg in ("help", "what can you do", "how does this work", "?"):
        return "help"

    # Form fill with URL
    if re.search(r"https?://", msg):
        return "fill_form"

    # Default: treat as complaint/request
    return "complaint"


# ──────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────

async def handle_message(
    session_id: str,
    message: str,
    state: ConversationState | None = None,
) -> dict[str, Any]:
    """
    Process a user message and return a response.
    This is the main entry point for the chat orchestrator.
    """
    if state is None:
        state = get_session(session_id)

    # Add to history
    if message.strip():
        state.history.append({"role": "user", "content": message})

    # ── Check onboarding first ──
    if not is_onboarded() and state.phase in ("IDLE", "ONBOARD"):
        state.phase = "ONBOARD"
        result = await _handle_onboard(state, message)
    # ── Phase-based routing ──
    elif state.phase == "ONBOARD":
        result = await _handle_onboard(state, message)
    elif state.phase == "INTAKE":
        result = await _handle_intake(state, message)
    elif state.phase == "STRATEGY":
        result = await _handle_strategy(state, message)
    elif state.phase == "COLLECT":
        result = await _handle_collect(state, message)
    elif state.phase == "LEARN":
        result = await _handle_learn(state, message)
    elif state.phase in ("IDLE", "DONE"):
        # Handle empty message (page load for returning users)
        if not message.strip():
            profile = get_profile()
            name = profile.get("full_name", profile.get("given_name", "there"))
            result = {
                "reply": (
                    f"Welcome back, **{name}**! What can I help you with today?\n\n"
                    "Just describe your problem and I'll figure out the best way to resolve it."
                ),
                "phase": "IDLE",
                "action": None,
            }
        else:
            # Detect intent and route
            intent = _detect_intent(message)

            if intent == "profile_update":
                result = await _handle_profile_update(state, message)
            elif intent == "greeting":
                profile = get_profile()
                name = profile.get("full_name", profile.get("given_name", "there"))
                result = {
                    "reply": (
                        f"Hey {name}! What can I help you with?\n\n"
                        "Describe your problem or paste a form URL."
                    ),
                    "phase": "IDLE",
                    "action": None,
                }
            elif intent == "help":
                result = {
                    "reply": (
                        "Here's what I can do:\n\n"
                        "**File Complaints**\n"
                        "Tell me your problem — *\"Indigo cancelled my flight\"* — and I'll:\n"
                        "1. Suggest the best course of action (email first? regulator?)\n"
                        "2. Draft a formal complaint email for you\n"
                        "3. Find and fill the complaint form automatically\n\n"
                        "**Fill Any Form**\n"
                        "Paste a URL — *\"Fill the form at https://...\"* — and I'll scan it, "
                        "ask only for what's missing, and fill it.\n\n"
                        "**Manage Your Profile**\n"
                        "Say *\"My new address is...\"* and I'll update your saved info.\n\n"
                        "What would you like to do?"
                    ),
                    "phase": "IDLE",
                    "action": None,
                }
            elif intent == "fill_form":
                url_match = re.search(r"https?://[^\s]+", message)
                if url_match:
                    state.target_url = url_match.group(0)
                    state.phase = "SCOUT"
                    result = {
                        "reply": f"Let me scan that form...",
                        "phase": "SCOUT",
                        "action": "start_scout",
                        "scout_params": {
                            "url": state.target_url,
                            "intent": message,
                        },
                    }
                else:
                    state.phase = "INTAKE"
                    result = await _handle_intake(state, message)
            else:
                # Complaint flow
                state.phase = "INTAKE"
                result = await _handle_intake(state, message)
    else:
        state.phase = "IDLE"
        result = {
            "reply": "Something went wrong. Let's start over — what can I help you with?",
            "phase": "IDLE",
            "action": None,
        }

    # Update phase
    state.phase = result.get("phase", state.phase)
    state.history.append({"role": "assistant", "content": result["reply"]})
    _persist_session(session_id, state.to_dict())

    return result


# ──────────────────────────────────────────────────────────────────────
# Scout & Fill Result Handlers (called by app.py after async tasks)
# ──────────────────────────────────────────────────────────────────────

async def process_scout_result(
    session_id: str,
    scout_result: dict[str, Any],
) -> dict[str, Any]:
    """Process the scout result and determine what to ask the user."""
    state = get_session(session_id)
    state.scout_result = scout_result

    have = scout_result.get("have", [])
    missing = scout_result.get("missing", [])
    case_specific = scout_result.get("case_specific", [])
    questions = scout_result.get("questions", [])
    required_missing = [field for field in missing + case_specific if field.get("required")]
    optional_missing = [field for field in missing + case_specific if not field.get("required")]
    state.missing_fields = required_missing

    total = len(have) + len(missing) + len(case_specific)

    # Detect login-gated or landing pages
    url = scout_result.get("url", "")
    is_low_field = total <= 3
    login_indicators = any(
        kw in (f.get("label", "") + f.get("name", "")).lower()
        for f in (have + missing + case_specific)
        for kw in ["login", "password", "sign in", "register", "username",
                   "language", "search", "faq", "captcha"]
    )

    if total == 0 or (is_low_field and login_indicators):
        # No real form found — likely a landing/login page
        state.phase = "STRATEGY" if state.strategy else "IDLE"
        portal_link = f"\n\n**Direct link**: [{url}]({url})" if url else ""
        return {
            "reply": (
                "I scanned the page but it looks like a **landing page or login-gated portal** "
                f"(only found {total} fields like language/search).\n\n"
                "This portal likely requires you to **create an account and log in** "
                f"before accessing the complaint form.{portal_link}\n\n"
                "What would you like to do?\n"
                "- **Visit the portal** — I'll give you the link to register/login\n"
                "- **Draft a complaint email** instead (often faster!)\n"
                "- **Try a different portal** for this company"
            ),
            "phase": state.phase,
            "action": None,
        }

    if not state.missing_fields:
        state.phase = "FILL"
        optional_note = ""
        if optional_missing:
            optional_note = (
                f" I found {len(optional_missing)} optional field"
                f"{'s' if len(optional_missing) != 1 else ''} I can safely skip for now."
            )
        return {
            "reply": (
                f"I scanned the form — **{total} fields** found. "
                f"I already have everything required from your profile!{optional_note} "
                "Starting the filing now..."
            ),
            "phase": "FILL",
            "action": "start_fill",
        }

    have_count = len(have)
    need_count = len(required_missing)
    question_fields = [
        question for question in questions
        if any(field.get("name", "") == question.get("field_name", "") for field in required_missing)
    ]

    if question_fields:
        q_text = "\n".join(f"- {q['question']}" for q in question_fields[:10])
        reply = (
            f"I scanned the form — **{total} fields** found. "
            f"I can auto-fill **{have_count}** from your profile. "
            f"I just need **{need_count}** required detail"
            f"{'s' if need_count != 1 else ''}:\n\n{q_text}\n\n"
            "Just answer naturally — you can provide all at once."
        )
    else:
        field_names = [
            f.get("label", f.get("name", "?")) for f in required_missing[:10]
        ]
        reply = (
            f"I scanned the form — **{total} fields**. "
            f"Auto-filling **{have_count}**. Need **{need_count}** required detail"
            f"{'s' if need_count != 1 else ''}: "
            f"{', '.join(field_names)}"
        )

    state.phase = "COLLECT"
    return {
        "reply": reply,
        "phase": "COLLECT",
        "action": None,
    }


async def process_fill_result(
    session_id: str,
    fill_result: dict[str, Any],
) -> dict[str, Any]:
    """Process the form fill result."""
    state = get_session(session_id)
    state.fill_result = fill_result

    success = fill_result.get("success", False)
    rate = fill_result.get("success_rate", 0)

    if success:
        reply = f"**Form submitted successfully!** ({rate:.0f}% of fields filled)"

        if state.pending_profile_updates:
            fields = [u["label"] for u in state.pending_profile_updates]
            reply += (
                f"\n\nI noticed you provided some new info: {', '.join(fields)}. "
                "Should I save these to your profile for next time?"
            )
            state.phase = "LEARN"
        else:
            # Check if there are more escalation steps
            if state.strategy:
                steps = state.strategy.get("escalation_path", [])
                if state.current_step < len(steps) - 1:
                    next_step = steps[state.current_step + 1]
                    reply += (
                        f"\n\nIf this doesn't resolve your issue, the next step would be: "
                        f"**{next_step['target']}** — {next_step['description']}\n\n"
                        "Just say **\"escalate\"** if you need to go further."
                    )
                    state.phase = "STRATEGY"
                else:
                    reply += "\n\nIs there anything else I can help with?"
                    state.phase = "DONE"
            else:
                reply += "\n\nIs there anything else I can help with?"
                state.phase = "DONE"
    else:
        error = fill_result.get("error", "Unknown error")
        reply = (
            f"I ran into an issue: {error}\n\n"
            "Would you like me to:\n"
            "- **Try again** with a different approach\n"
            "- **Draft a complaint email** instead\n"
            "- **Try a different portal**"
        )
        state.phase = "STRATEGY" if state.strategy else "DONE"

    return {
        "reply": reply,
        "phase": state.phase,
        "action": None,
    }


def get_merged_user_data(session_id: str) -> dict[str, str]:
    """Get all user data merged from profile + complaint + collected."""
    state = get_session(session_id)

    data = get_profile_for_form()

    if state.complaint_data:
        data.update(state.complaint_data)

    if state.collected_data:
        data.update(state.collected_data)

    return data
