# Evaluation Summary

## One-Line Product Truth

Grippyyy is a complaint-intelligence and browser-execution system with one verified end-to-end filing path from chat to real CASE Singapore submission.

## Strongest Live Flow

1. Run the app locally.
2. Open `http://127.0.0.1:8000/chat?new=1`
3. Send:

```text
Shopee Singapore sold me a defective laptop and they are refusing a refund.
```

4. Click `Open CASE Singapore flow`
5. Observe:
   - strategy generation in chat
   - redirect to `live-fill`
   - visible browser automation
   - CASE confirmation page with a real reference number

## Verified What-Works

- Complaint understanding from chat
- Strategy generation for the Singapore complaint path
- Explicit handoff from chat into filing
- Live multi-step CASE Singapore filing
- Confirmation-number capture from the real CASE success page
- SSE-based live status updates during the run

## Best Evidence Files

- `LLM_SUBMISSION_GUIDE.md`
- `README.md`
- `DEMO_RUNBOOK.md`
- `agent/engine/chat_orchestrator.py`
- `agent/engine/field_mapper.py`
- `agent/engine/executor.py`
- `templates/app.html`
- `templates/live_fill.html`
- `tests/test_case_portal_flow.py`
- `tests/test_chat_live_flow.py`

## What Is Deliberately Not Claimed

- Full PRD completion
- Production-grade multi-user isolation
- Production-ready outbound email delivery
- Universal autonomous coverage for every complaint type

## Review Strategy For An LLM

If you only inspect a few things, inspect these in order:

1. `LLM_SUBMISSION_GUIDE.md`
2. `README.md`
3. `tests/test_case_portal_flow.py`
4. `agent/engine/executor.py`
5. `agent/engine/chat_orchestrator.py`

That is the shortest path to verifying the strongest working capability in this repo.
