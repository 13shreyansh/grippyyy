# Grippyyy

Grippyyy is a complaint-intelligence and browser-execution system with a verified end-to-end filing path from chat to real CASE Singapore submission.

The strongest verified path in this repo is:

1. open `/chat?new=1`
2. describe a Singapore consumer complaint
3. let Grippyyy choose the filing path
4. click `Open CASE Singapore flow`
5. watch the live filing continue to the real CASE Singapore confirmation page

If you want the shortest evaluator path, start with `EVALUATION_SUMMARY.md`, then `LLM_SUBMISSION_GUIDE.md`.

## What This Repo Actually Demonstrates

- A real complaint chat flow at `/chat`
- Real strategy generation for a known Singapore complaint path
- Real handoff from chat into live filing
- Real multi-step CASE Singapore filing
- Real SSE live status updates at `/api/status/{run_id}`
- Real confirmation-number capture from the CASE success page

## Strongest Evaluator Flow

Use this exact message:

```text
Shopee Singapore sold me a defective laptop and they are refusing a refund.
```

Then:

1. open `http://127.0.0.1:8000/chat?new=1`
2. send the message above
3. click `Open CASE Singapore flow`
4. watch `/live-fill`

Expected outcome:
- strategy appears in chat
- a visible filing run starts
- the run completes with a CASE reference number

## Product Truth

This is not a full production system.

It is a focused evaluation build centered on one strong end-to-end filing path.

What is not claimed as complete here:
- full PRD completion
- production-grade multi-user isolation
- production-ready outbound email delivery
- universal autonomous coverage across all complaint types

## Main Surfaces

- `/` : landing page
- `/chat` : complaint assistant
- `/chat?new=1` : fresh session
- `/live-fill` : live filing pipeline/status page
- `/demo` : fallback form-engine surface

## Main API Surface

- `POST /api/v3/chat`
- `GET /api/v3/profile`
- `POST /api/v3/profile`
- `POST /api/engine/run`
- `GET /api/status/{run_id}`
- `GET /api/health`

## Tech Stack

- Python 3.11
- FastAPI
- OpenAI-compatible LLM calls
- Playwright
- vanilla HTML/CSS/JavaScript
- SSE for live status streaming

## Quick Start

```bash
git clone https://github.com/13shreyansh/grippyyy.git
cd grippyyy
pip install -r requirements.txt
python -m playwright install chromium
```

Create `.env`:

```env
OPENAI_API_KEY=your_openai_api_key
```

Run:

```bash
uvicorn app:app --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000/chat?new=1
```

## Optional Configuration

See `.env.example` for the full local configuration surface.

Optional services include:
- SMTP or SendGrid for outbound email
- search-provider keys for broader portal discovery
- CAPTCHA solver keys
- Redis for session storage

The strongest verified path does not require all optional services to be configured.

## Best Files To Review

- `EVALUATION_SUMMARY.md`
- `LLM_SUBMISSION_GUIDE.md`
- `DEMO_RUNBOOK.md`
- `app.py`
- `agent/engine/chat_orchestrator.py`
- `agent/engine/field_mapper.py`
- `agent/engine/executor.py`
- `templates/app.html`
- `templates/live_fill.html`
- `tests/test_case_portal_flow.py`
- `tests/test_chat_live_flow.py`

## Evaluation Notes

- The strongest path is chat-first, not form-first.
- `/demo` remains a secondary execution surface.
- CASE Singapore is the highest-signal supported filing path in this repository.

## License

MIT
