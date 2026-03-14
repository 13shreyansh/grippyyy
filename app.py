"""
Grippy V5.5 — The Universal Form & Complaint Solver.

FastAPI application with:
  Phase 1: Routing fix, dead code removal, security hardening
  Phase 2: Browser pool, session store, background tasks
  Phase 3: Document upload, Vision LLM extraction
  Phase 4: Email sending, complaint tracking, dashboard
  Phase 5: Autonomous discovery (SerpAPI/Tavily/DDG search provider)
  Phase 6: Background task queue for long-running operations
  Phase 7: Automated follow-up scheduler with escalation
  Phase 8: Multi-user auth, B2B API with key management
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import uuid
from typing import Any, AsyncGenerator

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# ── V4 Engine Imports (active) ──
from agent.engine.orchestrator import run_engine
from agent.engine.genome_db import (
    get_cache_stats,
    invalidate_cache,
    search_genomes,
    get_fill_history,
    migrate_from_json_cache,
)
from agent.engine.captcha_handler import get_service_balance
from agent.engine.chat_orchestrator import (
    handle_message,
    handle_action,
    process_scout_result,
    process_fill_result,
    get_session,
    reset_session,
    get_merged_user_data,
)
from agent.engine.scout import scout_form, resolve_url
from agent.engine.user_store import (
    is_onboarded,
    get_profile,
    get_profile_for_form,
    update_profile,
    bulk_update_profile,
    clear_profile,
    get_profile_history,
)
from agent.engine.auth import (
    register_user,
    login_user,
    get_user_from_token,
    logout_user,
    get_user_profiles,
    get_profile as get_auth_profile,
    save_profile,
    delete_profile,
    set_default_profile,
    get_auth_stats,
)

# ── New Phase 2-4 Modules ──
from agent.engine.browser_pool import BrowserPool
from agent.engine.session_store import (
    save_session as store_save_session,
    load_session as store_load_session,
    get_store_status,
)
from agent.engine.email_sender import send_email, get_email_status
from agent.engine.complaint_tracker import (
    create_complaint,
    get_complaint,
    list_complaints,
    update_complaint_status,
    resolve_complaint,
    add_action,
    get_actions,
    get_due_complaints,
    get_complaint_stats,
)
from agent.engine.doc_processor import (
    save_upload,
    get_upload,
    extract_data_from_document,
    cleanup_expired,
)

# ── Phase 5-8 Modules ──
from agent.engine.search_provider import get_search_status, web_search, multi_search
from agent.engine.task_queue import (
    submit_task,
    run_task,
    get_task,
    list_tasks,
    get_task_stats,
    TaskStatus,
)
from agent.engine.followup_scheduler import (
    run_followup_check,
    start_scheduler,
    stop_scheduler,
    get_scheduler_status,
)
from agent.engine.url_utils import validate_demo_url
from agent.engine.api_keys import (
    generate_api_key,
    validate_api_key,
    log_api_usage,
    list_api_keys,
    revoke_api_key,
    get_api_key_usage,
    get_api_stats,
)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000"
).split(",")

# ──────────────────────────────────────────────────────────────────────
# App Initialization
# ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Grippy V5",
    description="The Universal Form & Complaint Solver",
    version="5.5.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

templates = Jinja2Templates(directory="templates")
active_runs: dict[str, asyncio.Queue] = {}


# ──────────────────────────────────────────────────────────────────────
# Lifecycle Events
# ──────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    """Initialize browser pool and start background scheduler."""
    try:
        pool = await BrowserPool.get_instance()
        logger.info("Browser pool ready: %s", pool.status())
    except Exception as exc:
        logger.warning("Browser pool init deferred: %s", exc)

    # Start the follow-up scheduler (Phase 7)
    try:
        start_scheduler()
        logger.info("Follow-up scheduler started")
    except Exception as exc:
        logger.warning("Scheduler start deferred: %s", exc)


@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully shut down browser pool and scheduler."""
    try:
        stop_scheduler()
    except Exception:
        pass
    try:
        pool = await BrowserPool.get_instance()
        await pool.shutdown()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# Pydantic Models
# ──────────────────────────────────────────────────────────────────────

class V3ChatPayload(BaseModel):
    session_id: str = ""
    message: str = ""
    action: str | None = None


class ProfileUpdatePayload(BaseModel):
    data: dict[str, str]


class ChatRequest(BaseModel):
    message: str = ""
    history: list[dict[str, str]] = Field(default_factory=list)


class EngineRunPayload(BaseModel):
    url: str
    user_data: dict[str, str]
    dry_run: bool = False
    user_intent: str = ""


class SendEmailPayload(BaseModel):
    session_id: str
    to_email: str
    subject: str = ""
    confirm: bool = False


class ComplaintCreatePayload(BaseModel):
    session_id: str
    company: str
    industry: str = ""
    issue_summary: str = ""


class ApiKeyCreatePayload(BaseModel):
    name: str = ""
    tier: str = "free"


class ApiKeyRevokePayload(BaseModel):
    key_id: str


class B2BComplaintPayload(BaseModel):
    company: str
    issue: str
    user_name: str = ""
    user_email: str = ""
    user_phone: str = ""
    action: str = "auto"  # auto, email, form, escalate


class RegisterPayload(BaseModel):
    email: str
    username: str
    password: str
    display_name: str = ""


class LoginPayload(BaseModel):
    email_or_username: str
    password: str


class ProfilePayload(BaseModel):
    profile_name: str
    profile_data: dict[str, Any]
    profile_id: str | None = None


# ──────────────────────────────────────────────────────────────────────
# Auth Helpers
# ──────────────────────────────────────────────────────────────────────

def _get_current_user(request: Request) -> dict[str, Any] | None:
    """Extract user from JWT token in Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    return get_user_from_token(token)


def _validated_engine_url(raw_url: str) -> str:
    """Validate and normalize a URL before it reaches the engine."""
    try:
        return validate_demo_url(raw_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ──────────────────────────────────────────────────────────────────────
# SSE Helpers
# ──────────────────────────────────────────────────────────────────────

def _is_terminal_event(event: dict[str, Any]) -> bool:
    return str(event.get("type", "")).upper() in {
        "COMPLETE", "ERROR", "FAILED", "SCOUT_COMPLETE", "SCOUT_FAILED"
    }


def _stream_response_for_run(run_id: str) -> StreamingResponse:
    if run_id not in active_runs:
        raise HTTPException(status_code=404, detail="Run not found")
    return StreamingResponse(
        _status_event_stream(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _status_event_stream(run_id: str) -> AsyncGenerator[str, None]:
    queue = active_runs.get(run_id)
    if queue is None:
        return

    saw_terminal = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield 'data: {"type":"HEARTBEAT"}\n\n'
                continue

            if not isinstance(event, dict):
                continue

            yield f"data: {json.dumps(event)}\n\n"
            if _is_terminal_event(event):
                saw_terminal = True
                return
    finally:
        if saw_terminal:
            active_runs.pop(run_id, None)


# ══════════════════════════════════════════════════════════════════════
# PAGE ROUTES (Phase 1: Fixed Routing)
# ══════════════════════════════════════════════════════════════════════

@app.get("/")
async def landing_page(request: Request) -> object:
    """Marketing landing page — the first thing visitors see."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/chat")
async def chat_page(request: Request) -> object:
    """V4/V5 Chat interface — the main product."""
    return templates.TemplateResponse("app.html", {"request": request})


@app.get("/live-fill")
async def live_fill_page(request: Request) -> object:
    """Unified live status page for chat and demo flows."""
    return templates.TemplateResponse("live_fill.html", {"request": request})


@app.get("/dashboard")
async def dashboard_page(request: Request) -> object:
    """Complaint tracking dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ══════════════════════════════════════════════════════════════════════
# V3/V4 UNIFIED API ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v3/chat")
async def v3_chat(payload: V3ChatPayload) -> dict[str, Any]:
    """
    Unified Chat endpoint.
    Handles all conversation phases: onboard, intake, strategy, scout, collect, fill.
    """
    session_id = payload.session_id or uuid.uuid4().hex

    if payload.action:
        result = await handle_action(session_id, payload.action)
    else:
        result = await handle_message(session_id, payload.message)

    # Handle side effects
    action = result.get("action")

    if action == "start_scout":
        scout_params = result.get("scout_params", {})
        if scout_params.get("url"):
            result["live_fill_url"] = scout_params["url"]
        run_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        active_runs[run_id] = queue

        async def _scout_task():
            try:
                url = scout_params.get("url")
                if not url:
                    company = scout_params.get("company", "")
                    await queue.put({
                        "type": "PROGRESS",
                        "message": f"Looking up {company}'s complaint form...",
                    })
                    url_result = await resolve_url(company)
                    url = url_result.get("url")
                    if not url:
                        await queue.put({
                            "type": "SCOUT_FAILED",
                            "message": (
                                f"I couldn't find a complaint form for {company}. "
                                "Could you provide the URL?"
                            ),
                        })
                        return
                    await queue.put({
                        "type": "PROGRESS",
                        "message": f"Found: {url}",
                    })

                state = get_session(session_id)
                await queue.put({
                    "type": "PROGRESS",
                    "message": "Scanning the form...",
                })

                scout_result = await scout_form(
                    url=url,
                    user_intent=scout_params.get("intent", ""),
                    complaint_data=state.complaint_data,
                )

                state.target_url = url

                chat_result = await process_scout_result(session_id, scout_result)

                await queue.put({
                    "type": "SCOUT_COMPLETE",
                    "message": chat_result["reply"],
                    "phase": chat_result["phase"],
                    "url": url,
                    "target": scout_params.get("target") or state.target_company or "",
                    "scout_summary": {
                        "total_fields": scout_result.get("total_fields", 0),
                        "have": len(scout_result.get("have", [])),
                        "missing": len(scout_result.get("missing", [])),
                        "case_specific": len(scout_result.get("case_specific", [])),
                        "species": scout_result.get("species", "unknown"),
                    },
                    "action": chat_result.get("action"),
                })

            except Exception as exc:
                logger.error("Scout task failed: %s", exc)
                await queue.put({
                    "type": "SCOUT_FAILED",
                    "message": f"Scout failed: {str(exc)}. Could you provide the URL directly?",
                })

        asyncio.create_task(_scout_task())
        result["run_id"] = run_id

    elif action == "start_fill":
        state = get_session(session_id)
        result["live_fill_url"] = state.target_url or ""
        run_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        active_runs[run_id] = queue

        async def _fill_task():
            try:
                user_data = get_merged_user_data(session_id)
                url = state.target_url

                if not url:
                    await queue.put({
                        "type": "FAILED",
                        "message": "No target URL. Please provide the form URL.",
                    })
                    return

                async def progress_cb(msg: str):
                    await queue.put({"type": "PROGRESS", "message": msg})

                fill_result = await run_engine(
                    url=url,
                    user_data=user_data,
                    progress_callback=progress_cb,
                    user_intent=state.complaint_data.get("issue", ""),
                )

                chat_result = await process_fill_result(session_id, fill_result)

                event_type = "COMPLETE" if fill_result.get("success") else "FAILED"
                await queue.put({
                    "type": event_type,
                    "message": chat_result["reply"],
                    "phase": chat_result["phase"],
                    "result": {
                        "success": fill_result.get("success", False),
                        "success_rate": fill_result.get("success_rate", 0),
                        "total_successes": fill_result.get("total_successes", 0),
                        "total_failures": fill_result.get("total_failures", 0),
                        "confirmation_number": fill_result.get("confirmation_number", ""),
                        "timing": fill_result.get("timing", {}),
                    },
                })

            except Exception as exc:
                logger.error("Fill task failed: %s", exc)
                await queue.put({
                    "type": "FAILED",
                    "message": f"Form fill failed: {str(exc)}",
                })

        asyncio.create_task(_fill_task())
        result["run_id"] = run_id

    result["session_id"] = session_id
    return result


@app.get("/api/v3/stream/{run_id}")
async def v3_stream(run_id: str) -> StreamingResponse:
    """SSE stream for scout/fill progress."""
    return _stream_response_for_run(run_id)


# ══════════════════════════════════════════════════════════════════════
# PROFILE API
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v3/profile")
async def v3_get_profile() -> dict[str, Any]:
    """Get the user's profile."""
    return {
        "profile": get_profile(),
        "is_onboarded": is_onboarded(),
        "expanded": get_profile_for_form(),
    }


@app.post("/api/v3/profile")
async def v3_update_profile(payload: ProfileUpdatePayload) -> dict[str, Any]:
    """Update the user's profile."""
    results = bulk_update_profile(payload.data, source="manual")
    return {"updates": results, "profile": get_profile()}


@app.delete("/api/v3/profile")
async def v3_clear_profile() -> dict[str, Any]:
    """Clear the user's profile."""
    count = clear_profile()
    return {"cleared": count}


@app.get("/api/v3/profile/history")
async def v3_profile_history() -> dict[str, Any]:
    """Get profile change history."""
    return {"history": get_profile_history()}


# ══════════════════════════════════════════════════════════════════════
# SESSION API
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v3/session/{session_id}")
async def v3_session_state(session_id: str) -> dict[str, Any]:
    """Get the current session state."""
    state = get_session(session_id)
    return {"state": state.to_dict()}


@app.delete("/api/v3/session/{session_id}")
async def v3_reset_session(session_id: str) -> dict[str, Any]:
    """Reset a session."""
    reset_session(session_id)
    return {"success": True}


# ══════════════════════════════════════════════════════════════════════
# DOCUMENT UPLOAD API (Phase 3)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v3/upload")
async def v3_upload_document(file: UploadFile = File(...)) -> dict[str, Any]:
    """
    Upload a document (PDF, PNG, JPG, JPEG) for data extraction.
    Returns a file_id that can be referenced in chat.
    """
    content = await file.read()
    result = save_upload(file.filename or "unknown", content)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Upload failed"))

    # Automatically extract data using Vision LLM
    file_id = result["file_id"]
    try:
        extraction = await extract_data_from_document(file_id)
        if extraction.get("success"):
            result["extracted_data"] = extraction["extracted_data"]

            # Auto-inject extracted data into the user's profile for form filling
            extracted = extraction["extracted_data"]
            injectable = {}
            for key, value in extracted.items():
                if isinstance(value, str) and value.strip():
                    injectable[key] = value
            if injectable:
                bulk_update_profile(injectable, source="document_upload")
                result["profile_updated"] = True
        else:
            result["extraction_error"] = extraction.get("error", "Extraction failed")
    except Exception as exc:
        logger.warning("Document extraction failed: %s", exc)
        result["extraction_error"] = str(exc)

    return result


# ══════════════════════════════════════════════════════════════════════
# EMAIL SENDING API (Phase 4)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v3/email/send")
async def v3_send_email(payload: SendEmailPayload) -> dict[str, Any]:
    """
    Send a drafted complaint email.
    Requires explicit user confirmation (confirm=True).
    """
    if not payload.confirm:
        return {
            "success": False,
            "error": "Email sending requires explicit confirmation. Set confirm=true.",
            "requires_confirmation": True,
        }

    state = get_session(payload.session_id)
    if not state.drafted_email:
        raise HTTPException(
            status_code=400,
            detail="No email has been drafted in this session. Use the chat to draft one first.",
        )

    # Build subject from complaint data
    company = state.target_company or "Unknown Company"
    subject = payload.subject or f"Formal Complaint — {company}"

    profile = get_profile()
    user_email = profile.get("email", "")
    user_name = profile.get("full_name", profile.get("given_name", "User"))

    result = send_email(
        to_email=payload.to_email,
        subject=subject,
        body_text=state.drafted_email,
        from_name=f"{user_name} via Grippy",
        reply_to=user_email,
    )

    # Track the action
    if state.target_company:
        try:
            complaints = list_complaints(session_id=payload.session_id)
            if complaints:
                complaint_id = complaints[0]["id"]
            else:
                complaint_record = create_complaint(
                    session_id=payload.session_id,
                    company=company,
                    industry=state.complaint_data.get("category", ""),
                    issue_summary=state.complaint_data.get("issue", ""),
                    complaint_data=state.complaint_data,
                )
                complaint_id = complaint_record["id"]

            add_action(
                complaint_id=complaint_id,
                action_type="email_sent" if result["success"] else "email_drafted",
                target=payload.to_email,
                details={
                    "subject": subject,
                    "backend": result.get("backend", "unknown"),
                },
                status="success" if result["success"] else "failed",
            )

            if result["success"]:
                update_complaint_status(
                    complaint_id,
                    status="email_sent",
                    next_action_days=7,
                )
        except Exception as exc:
            logger.warning("Failed to track email action: %s", exc)

    return result


# ══════════════════════════════════════════════════════════════════════
# COMPLAINT TRACKING API (Phase 4)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v3/complaints/due")
async def v3_due_complaints() -> dict[str, Any]:
    """Get all complaints that are due for follow-up."""
    due = get_due_complaints()
    return {"complaints": due, "count": len(due)}


@app.get("/api/v3/complaints")
async def v3_list_complaints(
    session_id: str = "",
    status: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """List complaints with optional filters."""
    complaints = list_complaints(
        session_id=session_id or None,
        status=status or None,
        limit=limit,
    )
    return {"complaints": complaints, "count": len(complaints)}


@app.get("/api/v3/complaints/stats/overview")
async def v3_complaint_stats() -> dict[str, Any]:
    """Get aggregate complaint statistics."""
    return get_complaint_stats()


@app.get("/api/v3/complaints/{complaint_id}")
async def v3_get_complaint(complaint_id: str) -> dict[str, Any]:
    """Get a single complaint with its action history."""
    complaint = get_complaint(complaint_id)
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    actions = get_actions(complaint_id)
    return {"complaint": complaint, "actions": actions}


@app.post("/api/v3/complaints/{complaint_id}/resolve")
async def v3_resolve_complaint(complaint_id: str) -> dict[str, Any]:
    """Mark a complaint as resolved."""
    success = resolve_complaint(complaint_id)
    if not success:
        raise HTTPException(status_code=404, detail="Complaint not found")
    add_action(
        complaint_id=complaint_id,
        action_type="resolved",
        target="user",
        details={"resolved_by": "user"},
    )
    return {"success": True, "message": "Complaint marked as resolved"}


# ══════════════════════════════════════════════════════════════════════
# ENGINE API (Form Genome Engine)
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/engine/run")
async def engine_run(payload: EngineRunPayload) -> dict[str, Any]:
    """Run the Form Genome Engine on a URL."""
    normalized_url = _validated_engine_url(payload.url)
    run_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    active_runs[run_id] = queue

    async def _run_engine_task():
        try:
            async def progress_cb(msg: str):
                await queue.put({"type": "PROGRESS", "message": msg})

            result = await run_engine(
                url=normalized_url,
                user_data=payload.user_data,
                progress_callback=progress_cb,
                dry_run=payload.dry_run,
                user_intent=payload.user_intent,
            )

            if result.get("success"):
                await queue.put({
                    "type": "COMPLETE",
                    "message": "Form filled successfully!",
                    "result": {
                        "success": True,
                        "success_rate": result.get("success_rate", 100.0),
                        "cache_hit": result.get("cache_hit", False),
                        "species": result.get("species", "unknown"),
                        "total_successes": result.get("total_successes", 0),
                        "total_failures": result.get("total_failures", 0),
                        "details": result.get("details", []),
                        "timing": result.get("timing", {}),
                        "dry_run": result.get("dry_run", False),
                        "step_mappings": (
                            result.get("step_mappings") if payload.dry_run else None
                        ),
                        "genome": (
                            result.get("genome") if payload.dry_run else None
                        ),
                    },
                })
            else:
                await queue.put({
                    "type": "FAILED",
                    "message": result.get("error") or "Form fill failed",
                    "result": {
                        "success": False,
                        "species": result.get("species", "unknown"),
                        "cache_hit": result.get("cache_hit", False),
                        "total_successes": result.get("total_successes", 0),
                        "total_failures": result.get("total_failures", 0),
                        "success_rate": result.get("success_rate", 0),
                        "details": result.get("details", []),
                    },
                })
        except Exception as exc:
            await queue.put({
                "type": "ERROR",
                "message": f"Engine error: {str(exc)}",
            })

    asyncio.create_task(_run_engine_task())
    return {"run_id": run_id}


@app.get("/api/engine/cache-stats")
async def engine_cache_stats() -> dict[str, Any]:
    return get_cache_stats()


@app.post("/api/engine/invalidate-cache")
async def engine_invalidate_cache(payload: dict[str, str]) -> dict[str, Any]:
    url = payload.get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    normalized_url = _validated_engine_url(url)
    removed = invalidate_cache(normalized_url)
    return {"invalidated": removed, "url": normalized_url}


@app.get("/api/engine/captcha-balance")
async def engine_captcha_balance() -> dict[str, Any]:
    return await get_service_balance()


@app.get("/api/engine/search-genomes")
async def engine_search_genomes(
    q: str = "", species: str = "", domain: str = "", limit: int = 20,
) -> dict[str, Any]:
    results = search_genomes(query=q, species=species, domain=domain, limit=limit)
    return {"results": results, "count": len(results)}


@app.get("/api/engine/fill-history")
async def engine_fill_history(url: str = "", limit: int = 50) -> dict[str, Any]:
    normalized_url = _validated_engine_url(url) if url else None
    history = get_fill_history(url=normalized_url, limit=limit)
    return {"history": history, "count": len(history)}


@app.post("/api/engine/migrate-cache")
async def engine_migrate_cache() -> dict[str, Any]:
    result = migrate_from_json_cache()
    return {"migration": result}


# ══════════════════════════════════════════════════════════════════════
# HEALTH CHECK (Phase 2)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    """System health check — Redis, browser pool, email, complaints."""
    health = {
        "status": "ok",
        "version": "5.5.0",
    }

    # Session store status
    health["session_store"] = get_store_status()

    # Browser pool status
    try:
        pool = await BrowserPool.get_instance()
        health["browser_pool"] = pool.status()
    except Exception as exc:
        health["browser_pool"] = {"status": "error", "error": str(exc)}

    # Email status
    health["email"] = get_email_status()

    # Complaint stats
    try:
        health["complaints"] = get_complaint_stats()
    except Exception:
        health["complaints"] = {"status": "error"}

    # Phase 5: Search provider status
    health["search"] = get_search_status()

    # Phase 6: Task queue stats
    health["task_queue"] = get_task_stats()

    # Phase 7: Scheduler status
    health["scheduler"] = get_scheduler_status()

    # Phase 8: API stats
    try:
        health["api"] = get_api_stats()
    except Exception:
        health["api"] = {"status": "error"}

    # Active runs
    health["active_runs"] = len(active_runs)

    return health


# ══════════════════════════════════════════════════════════════════════
# AUTHENTICATION ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/auth/register")
async def auth_register(payload: RegisterPayload) -> dict[str, Any]:
    result = register_user(
        email=payload.email,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/auth/login")
async def auth_login(payload: LoginPayload) -> dict[str, Any]:
    result = login_user(payload.email_or_username, payload.password)
    if not result["success"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth_header[7:]
    logout_user(token)
    return {"success": True, "message": "Logged out"}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"user": user}


@app.get("/api/auth/profiles")
async def auth_profiles(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    profiles = get_user_profiles(user["id"])
    return {"profiles": profiles}


@app.get("/api/auth/profiles/{profile_id}")
async def auth_get_profile(profile_id: str, request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    profile = get_auth_profile(user["id"], profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"profile": profile}


@app.post("/api/auth/profiles")
async def auth_save_profile(
    payload: ProfilePayload, request: Request,
) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = save_profile(
        user_id=user["id"],
        profile_name=payload.profile_name,
        profile_data=payload.profile_data,
        profile_id=payload.profile_id,
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.delete("/api/auth/profiles/{profile_id}")
async def auth_delete_profile(
    profile_id: str, request: Request,
) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = delete_profile(user["id"], profile_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/auth/profiles/{profile_id}/default")
async def auth_set_default_profile(
    profile_id: str, request: Request,
) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = set_default_profile(user["id"], profile_id)
    return result


@app.get("/api/auth/stats")
async def auth_stats() -> dict[str, Any]:
    return get_auth_stats()


# ══════════════════════════════════════════════════════════════════════
# PHASE 5: AUTONOMOUS SEARCH API
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v3/search")
async def v3_search(q: str, provider: str = "auto") -> dict[str, Any]:
    """Search the web for complaint forms, company info, or regulatory bodies."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    raw = await web_search(q, provider=provider if provider != "auto" else "auto")
    results = [r.to_dict() if hasattr(r, "to_dict") else r for r in raw]
    return {"query": q, "results": results, "count": len(results)}


@app.get("/api/v3/search/multi")
async def v3_multi_search(
    q: str, max_providers: int = 2,
) -> dict[str, Any]:
    """Search across multiple providers and merge results."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    raw = await multi_search(q, max_providers=max_providers)
    results = [r.to_dict() if hasattr(r, "to_dict") else r for r in raw]
    return {"query": q, "results": results, "count": len(results)}


@app.get("/api/v3/search/status")
async def v3_search_status() -> dict[str, Any]:
    """Get search provider availability."""
    return get_search_status()


# ══════════════════════════════════════════════════════════════════════
# PHASE 6: BACKGROUND TASK QUEUE API
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v3/tasks/submit")
async def v3_submit_task(
    payload: EngineRunPayload,
) -> dict[str, Any]:
    """Submit a form-fill task to the background queue. Returns immediately."""
    normalized_url = _validated_engine_url(payload.url)
    task_id = submit_task(
        task_type="form_fill",
        params={
            "url": normalized_url,
            "user_data": payload.user_data,
            "dry_run": payload.dry_run,
            "user_intent": payload.user_intent,
        },
    )

    # Run the task in the background
    async def _bg_run():
        try:
            async def progress_cb(msg: str):
                # Progress is logged but not stored on the dict snapshot
                logger.info("Task %s progress: %s", task_id, msg)

            result = await run_engine(
                url=normalized_url,
                user_data=payload.user_data,
                progress_callback=progress_cb,
                dry_run=payload.dry_run,
                user_intent=payload.user_intent,
            )
            run_task(task_id, result)
        except Exception as exc:
            run_task(task_id, {"success": False, "error": str(exc)})

    asyncio.create_task(_bg_run())
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/v3/tasks/stats/overview")
async def v3_task_stats() -> dict[str, Any]:
    """Get task queue statistics."""
    return get_task_stats()


@app.get("/api/v3/tasks")
async def v3_list_tasks(
    status: str = "", limit: int = 50,
) -> dict[str, Any]:
    """List background tasks."""
    tasks = list_tasks(
        status=status or None,
        limit=limit,
    )
    return {"tasks": tasks, "count": len(tasks)}


@app.get("/api/v3/tasks/{task_id}")
async def v3_get_task(task_id: str) -> dict[str, Any]:
    """Get the status and result of a background task."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ══════════════════════════════════════════════════════════════════════
# PHASE 7: FOLLOW-UP SCHEDULER API
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v3/scheduler/run")
async def v3_run_followups() -> dict[str, Any]:
    """Manually trigger a follow-up check cycle."""
    results = await run_followup_check()
    return {"results": results, "count": len(results)}


@app.get("/api/v3/scheduler/status")
async def v3_scheduler_status() -> dict[str, Any]:
    """Get the follow-up scheduler status."""
    return get_scheduler_status()


# ══════════════════════════════════════════════════════════════════════
# PHASE 8: B2B API (API Key Authentication)
# ══════════════════════════════════════════════════════════════════════


def _validate_api_key(request: Request) -> dict[str, Any]:
    """Validate API key from X-API-Key header."""
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="API key required. Pass it in the X-API-Key header.",
        )
    key_data = validate_api_key(api_key)
    if not key_data:
        raise HTTPException(status_code=403, detail="Invalid or revoked API key.")
    return key_data


@app.post("/api/b2b/keys")
async def b2b_create_key(
    payload: ApiKeyCreatePayload, request: Request,
) -> dict[str, Any]:
    """Create a new API key (requires JWT auth)."""
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = generate_api_key(
        user_id=user["id"],
        name=payload.name or f"{user.get('username', 'user')}_key",
        tier=payload.tier,
    )
    return result


@app.get("/api/b2b/keys")
async def b2b_list_keys(request: Request) -> dict[str, Any]:
    """List all API keys for the authenticated user."""
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    keys = list_api_keys(user["id"])
    return {"keys": keys, "count": len(keys)}


@app.post("/api/b2b/keys/revoke")
async def b2b_revoke_key(
    payload: ApiKeyRevokePayload, request: Request,
) -> dict[str, Any]:
    """Revoke an API key."""
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    success = revoke_api_key(payload.key_id, user["id"])
    if not success:
        raise HTTPException(status_code=404, detail="Key not found or not owned by you")
    return {"success": True, "message": "API key revoked"}


@app.post("/api/b2b/complaint")
async def b2b_submit_complaint(
    payload: B2BComplaintPayload, request: Request,
) -> dict[str, Any]:
    """
    B2B API: Submit a complaint programmatically.
    Requires X-API-Key header.
    Returns a complaint ID and starts processing.
    """
    key_data = _validate_api_key(request)
    log_api_usage(key_data["id"], "/api/b2b/complaint")

    # Create the complaint record
    complaint = create_complaint(
        session_id=f"b2b_{key_data['id']}_{uuid.uuid4().hex[:8]}",
        company=payload.company,
        industry="",
        issue_summary=payload.issue[:200],
        complaint_data={
            "issue": payload.issue,
            "user_name": payload.user_name,
            "user_email": payload.user_email,
            "user_phone": payload.user_phone,
            "action": payload.action,
            "source": "b2b_api",
            "api_key_id": key_data["id"],
        },
        user_id=key_data["user_id"],
    )

    # Start background processing
    task_id = submit_task(
        task_type="b2b_complaint",
        params={
            "complaint_id": complaint["id"],
            "company": payload.company,
            "issue": payload.issue,
            "user_name": payload.user_name,
            "user_email": payload.user_email,
            "user_phone": payload.user_phone,
            "action": payload.action,
        },
    )

    async def _bg_process():
        """Background: resolve URL, draft email, optionally fill form."""
        try:
            # Step 1: Find the complaint form
            url_result = await resolve_url(payload.company)
            url = url_result.get("url", "")

            # Step 2: Draft complaint email
            from agent.engine.escalation_kb import (
                classify_industry,
                classify_industry_llm,
                draft_complaint_email,
                get_escalation_path,
            )

            industry = classify_industry(payload.company)
            if not industry:
                industry = await classify_industry_llm(
                    payload.company, payload.issue
                )

            path = get_escalation_path(industry)
            template_key = "general_complaint_email"
            if path and path["steps"]:
                first_step = path["steps"][0]
                template_key = first_step.get("template", template_key)

            user_data = {
                "full_name": payload.user_name,
                "email": payload.user_email,
                "phone": payload.user_phone,
            }

            email_text = await draft_complaint_email(
                template_key=template_key,
                company=payload.company,
                complaint=payload.issue,
                user_data=user_data,
            )

            result = {
                "success": True,
                "complaint_id": complaint["id"],
                "industry": industry,
                "form_url": url,
                "email_draft": email_text,
                "escalation_path": path["steps"] if path else [],
            }

            # Send email if requested
            if payload.action in ("auto", "email") and payload.user_email:
                email_result = send_email(
                    to_email=payload.user_email,
                    subject=f"Your Grippy Complaint Against {payload.company}",
                    body_text=(
                        f"Hi {payload.user_name},\n\n"
                        f"Here is your drafted complaint email against {payload.company}:\n\n"
                        f"{email_text}\n\n"
                        f"---\nGrippy - The Universal Form & Complaint Solver"
                    ),
                    from_name="Grippy",
                )
                result["email_sent"] = email_result.get("success", False)

            add_action(
                complaint_id=complaint["id"],
                action_type="email_drafted",
                target=payload.user_email,
                details={"industry": industry, "form_url": url},
            )

            run_task(task_id, result)

        except Exception as exc:
            logger.error("B2B complaint processing failed: %s", exc)
            run_task(task_id, {"success": False, "error": str(exc)})

    asyncio.create_task(_bg_process())

    return {
        "success": True,
        "complaint_id": complaint["id"],
        "task_id": task_id,
        "message": "Complaint submitted. Use task_id to check progress.",
    }


@app.get("/api/b2b/complaint/{complaint_id}")
async def b2b_get_complaint(
    complaint_id: str, request: Request,
) -> dict[str, Any]:
    """Get a complaint submitted via B2B API."""
    key_data = _validate_api_key(request)
    log_api_usage(key_data["id"], f"/api/b2b/complaint/{complaint_id}")
    complaint = get_complaint(complaint_id)
    if not complaint:
        raise HTTPException(status_code=404, detail="Complaint not found")
    actions = get_actions(complaint_id)
    return {"complaint": complaint, "actions": actions}


@app.get("/api/b2b/usage")
async def b2b_usage(request: Request) -> dict[str, Any]:
    """Get API usage for the authenticated user's keys."""
    key_data = _validate_api_key(request)
    usage = get_api_key_usage(key_data["id"])
    return {"key_id": key_data["id"], "usage": usage}


# ══════════════════════════════════════════════════════════════════════
# PAGE ROUTES: LOGIN (Phase 8)
# ══════════════════════════════════════════════════════════════════════

@app.get("/login")
async def login_page(request: Request) -> object:
    """Login/Register page."""
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/api-docs")
async def api_docs_page(request: Request) -> object:
    """B2B API documentation page."""
    return templates.TemplateResponse("api_docs.html", {"request": request})


# ══════════════════════════════════════════════════════════════════════
# LEGACY ROUTES (kept for backward compatibility, minimal)
# ══════════════════════════════════════════════════════════════════════

@app.get("/verify")
async def verify(request: Request, data: str) -> object:
    """Legacy verify page."""
    try:
        decoded = base64.b64decode(data).decode("utf-8")
        complaint_data = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=400, detail="Invalid complaint data"
        ) from error

    if not isinstance(complaint_data, dict):
        raise HTTPException(status_code=400, detail="Invalid complaint data")

    fields = {
        "complainant_name": complaint_data.get("complainant_name", ""),
        "complainant_email": complaint_data.get("complainant_email", ""),
        "complainant_phone": complaint_data.get("complainant_phone", ""),
        "complaint_against": complaint_data.get("complaint_against", ""),
        "complaint_type": complaint_data.get("complaint_type", ""),
        "incident_date": complaint_data.get("incident_date", ""),
        "incident_description": complaint_data.get("incident_description", ""),
        "desired_outcome": complaint_data.get("desired_outcome", ""),
        "reference_number": complaint_data.get("reference_number", ""),
        "nric": complaint_data.get("nric", ""),
    }
    return templates.TemplateResponse("verify.html", {"request": request, **fields})


@app.get("/status")
async def status_page(request: Request) -> object:
    return templates.TemplateResponse("status.html", {"request": request})


@app.get("/demo")
async def demo_page(request: Request) -> object:
    return templates.TemplateResponse("demo.html", {"request": request})


@app.get("/demo/status")
async def demo_status_page(request: Request) -> object:
    return templates.TemplateResponse("live_fill.html", {"request": request})


@app.get("/api/status/{run_id}")
async def legacy_status_alias(run_id: str) -> StreamingResponse:
    """Backward-compatible SSE endpoint used by the demo pages."""
    return _stream_response_for_run(run_id)


@app.get("/api/engine/status/{run_id}")
async def engine_status(run_id: str) -> StreamingResponse:
    """Legacy SSE stream endpoint."""
    return _stream_response_for_run(run_id)
