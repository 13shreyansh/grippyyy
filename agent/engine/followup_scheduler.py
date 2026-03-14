"""
Follow-up Scheduler — Phase 7: Automated Escalation.

Runs periodic checks on all pending complaints and automatically:
  1. Sends follow-up emails if companies haven't responded
  2. Escalates to regulators after configurable timeouts
  3. Updates complaint status and logs all actions

Can be triggered by:
  - A cron job calling POST /api/v3/scheduler/run
  - The startup background loop (runs every SCHEDULER_INTERVAL seconds)
  - Manual trigger from the dashboard
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────
SCHEDULER_INTERVAL = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "86400"))  # 24h
FOLLOWUP_DAYS = int(os.environ.get("FOLLOWUP_DAYS", "7"))  # Days before follow-up
MAX_AUTO_ESCALATIONS = int(os.environ.get("MAX_AUTO_ESCALATIONS", "3"))
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true"

_scheduler_running = False
_last_run: float | None = None
_run_count = 0


async def run_followup_check() -> dict[str, Any]:
    """
    Check all pending complaints and trigger follow-ups or escalations.

    Returns a summary of actions taken.
    """
    global _last_run, _run_count

    from .complaint_tracker import (
        get_due_complaints,
        update_complaint_status,
        add_action,
        get_actions,
        get_complaint,
    )
    from .email_sender import send_email
    from .escalation_kb import draft_complaint_email, get_escalation_path
    from .user_store import get_profile, get_profile_for_form

    _last_run = time.time()
    _run_count += 1

    summary = {
        "run_at": datetime.now().isoformat(),
        "run_number": _run_count,
        "complaints_checked": 0,
        "followups_sent": 0,
        "escalations_triggered": 0,
        "errors": [],
    }

    try:
        due_complaints = get_due_complaints()
        summary["complaints_checked"] = len(due_complaints)

        if not due_complaints:
            logger.info("Scheduler: No complaints due for follow-up")
            return summary

        profile = get_profile()
        user_data = get_profile_for_form()
        user_name = profile.get("full_name", profile.get("given_name", "User"))
        user_email = profile.get("email", "")

        for complaint in due_complaints:
            try:
                complaint_id = complaint["id"]
                company = complaint.get("company", "Unknown")
                status = complaint.get("status", "pending")
                current_step = complaint.get("current_step", 0)
                industry = complaint.get("industry", "general_india")

                # Get the escalation path for this complaint
                escalation_path = []
                try:
                    import json
                    path_json = complaint.get("escalation_path_json", "[]")
                    if isinstance(path_json, str):
                        escalation_path = json.loads(path_json)
                    elif isinstance(path_json, list):
                        escalation_path = path_json
                except Exception:
                    pass

                # Get action history
                actions = get_actions(complaint_id)
                email_count = sum(
                    1 for a in actions if a.get("action_type") == "email_sent"
                )
                followup_count = sum(
                    1 for a in actions if a.get("action_type") == "followup_sent"
                )

                # Decide action: follow-up or escalate
                if followup_count >= MAX_AUTO_ESCALATIONS:
                    # Too many follow-ups, escalate to next step
                    if escalation_path and current_step < len(escalation_path) - 1:
                        next_step = escalation_path[current_step + 1]
                        update_complaint_status(
                            complaint_id,
                            status="escalated",
                            next_action_days=FOLLOWUP_DAYS,
                        )
                        add_action(
                            complaint_id=complaint_id,
                            action_type="escalated",
                            target=next_step.get("target", "regulator"),
                            details={
                                "reason": f"No response after {followup_count} follow-ups",
                                "next_step": next_step,
                                "auto_escalated": True,
                            },
                        )
                        summary["escalations_triggered"] += 1
                        logger.info(
                            "Scheduler: Escalated complaint %s to step %d",
                            complaint_id,
                            current_step + 1,
                        )
                    else:
                        # No more escalation steps — mark as needing manual review
                        update_complaint_status(
                            complaint_id,
                            status="follow_up_scheduled",
                            next_action_days=FOLLOWUP_DAYS * 2,
                        )
                        add_action(
                            complaint_id=complaint_id,
                            action_type="followup_sent",
                            target=company,
                            details={
                                "reason": "Max auto-escalations reached, needs manual review",
                                "auto": True,
                            },
                        )
                else:
                    # Send a follow-up email
                    complaint_data = {}
                    try:
                        import json
                        data_json = complaint.get("complaint_data_json", "{}")
                        if isinstance(data_json, str):
                            complaint_data = json.loads(data_json)
                        elif isinstance(data_json, dict):
                            complaint_data = data_json
                    except Exception:
                        pass

                    issue = complaint.get("issue_summary", "my previous complaint")

                    followup_email = await draft_complaint_email(
                        template_key="followup_email",
                        company=company,
                        complaint=issue,
                        user_data=user_data,
                        complaint_data={
                            **complaint_data,
                            "followup_number": followup_count + 1,
                            "original_date": complaint.get("created_at", ""),
                            "days_waiting": FOLLOWUP_DAYS,
                        },
                    )

                    # Try to find the company's email from previous actions
                    to_email = ""
                    for action in actions:
                        if action.get("action_type") in ("email_sent", "followup_sent"):
                            to_email = action.get("target", "")
                            if to_email and "@" in to_email:
                                break

                    if to_email and "@" in to_email:
                        result = send_email(
                            to_email=to_email,
                            subject=f"Follow-up: Complaint Against {company} — No Response Received",
                            body_text=followup_email,
                            from_name=f"{user_name} via Grippy",
                            reply_to=user_email,
                        )

                        action_status = "success" if result.get("success") else "failed"
                    else:
                        # No email address — just log the follow-up
                        action_status = "pending"

                    add_action(
                        complaint_id=complaint_id,
                        action_type="followup_sent",
                        target=to_email or company,
                        details={
                            "followup_number": followup_count + 1,
                            "email_body": followup_email[:500],
                            "auto": True,
                            "send_status": action_status,
                        },
                        status=action_status,
                    )

                    update_complaint_status(
                        complaint_id,
                        status="follow_up_scheduled",
                        next_action_days=FOLLOWUP_DAYS,
                    )

                    summary["followups_sent"] += 1
                    logger.info(
                        "Scheduler: Sent follow-up #%d for complaint %s",
                        followup_count + 1,
                        complaint_id,
                    )

            except Exception as exc:
                error_msg = f"Error processing complaint {complaint.get('id', '?')}: {exc}"
                summary["errors"].append(error_msg)
                logger.error("Scheduler: %s", error_msg)

    except Exception as exc:
        summary["errors"].append(f"Scheduler run failed: {exc}")
        logger.error("Scheduler run failed: %s", exc)

    return summary


async def _scheduler_loop() -> None:
    """Background loop that runs the follow-up check periodically."""
    global _scheduler_running

    if not SCHEDULER_ENABLED:
        logger.info("Scheduler is disabled (SCHEDULER_ENABLED=false)")
        return

    _scheduler_running = True
    logger.info(
        "Scheduler started: checking every %d seconds (%d hours)",
        SCHEDULER_INTERVAL,
        SCHEDULER_INTERVAL // 3600,
    )

    while _scheduler_running:
        try:
            await asyncio.sleep(SCHEDULER_INTERVAL)
            logger.info("Scheduler: Running periodic follow-up check...")
            summary = await run_followup_check()
            logger.info("Scheduler: Run complete — %s", summary)
        except asyncio.CancelledError:
            logger.info("Scheduler: Shutting down")
            break
        except Exception as exc:
            logger.error("Scheduler: Unexpected error: %s", exc)
            await asyncio.sleep(60)  # Wait a minute before retrying


_scheduler_task: asyncio.Task | None = None


def start_scheduler() -> None:
    """Start the background scheduler loop."""
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("Scheduler task created")


def stop_scheduler() -> None:
    """Stop the background scheduler loop."""
    global _scheduler_running, _scheduler_task
    _scheduler_running = False
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()


def get_scheduler_status() -> dict[str, Any]:
    """Get the current scheduler status."""
    return {
        "enabled": SCHEDULER_ENABLED,
        "running": _scheduler_running,
        "interval_seconds": SCHEDULER_INTERVAL,
        "interval_hours": SCHEDULER_INTERVAL / 3600,
        "followup_days": FOLLOWUP_DAYS,
        "max_auto_escalations": MAX_AUTO_ESCALATIONS,
        "last_run": (
            datetime.fromtimestamp(_last_run).isoformat() if _last_run else None
        ),
        "total_runs": _run_count,
    }
