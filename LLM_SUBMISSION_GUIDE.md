# Grippyyy Submission Guide

This repository is a clean evaluator-facing copy of the current `grippy` codebase.

## What Is Actually Working

- `/chat` supports a real complaint-intelligence flow.
- The strongest end-to-end path is:
  - open `/chat?new=1`
  - send `Shopee Singapore sold me a defective laptop and they are refusing a refund.`
  - click `Open CASE Singapore flow`
  - watch the live filing continue to the real CASE Singapore confirmation page
- `/live-fill` shows the live pipeline and the final CASE reference number.
- `/demo` remains available as a fallback public form-engine surface.

## What Was Verified

- CASE Singapore now advances through all 4 steps, not just the first page.
- The executor only reports success when CASE reaches a real confirmed submission state.
- The chat-to-CASE flow was re-run successfully on the local machine before this repo copy was created.

## Best Files To Read First

- `README.md`
- `DEMO_RUNBOOK.md`
- `app.py`
- `agent/engine/chat_orchestrator.py`
- `agent/engine/executor.py`
- `agent/engine/field_mapper.py`
- `templates/app.html`
- `templates/live_fill.html`

## Recommended Evaluator Flow

1. Install dependencies from `requirements.txt`
2. Set `OPENAI_API_KEY` in `.env`
3. Run:

```bash
python -m playwright install chromium
python -m uvicorn app:app --port 8000
```

4. Open:
  - `http://127.0.0.1:8000/chat?new=1`

5. Use the CASE filing message:

```text
Shopee Singapore sold me a defective laptop and they are refusing a refund.
```

## Scope Boundaries

- This repository centers one verified vertical slice, not full production coverage across all complaint classes.
- Email delivery, full multi-user isolation, and broader autonomous coverage are not claimed as complete here.
