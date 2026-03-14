# Grippyyy

Grippyyy is a complaint-intelligence and browser-execution system with a verified end-to-end filing path from chat to real CASE Singapore submission.

This repository is organized as an evaluation build around one strong vertical slice. The strongest implemented flow starts in chat, selects the correct escalation path for a Singapore consumer complaint, executes a live multi-step filing on the real CASE Singapore portal, and captures the resulting reference number.

If you want the shortest reviewer path, open:

1. `EVALUATION_SUMMARY.md`
2. `LLM_SUBMISSION_GUIDE.md`
3. this README

## At A Glance

### Primary claim

Grippyyy has one verified chat-to-filing path that reaches a real confirmed submission state on CASE Singapore.

### Exact path

- start at `/chat?new=1`
- send the Shopee Singapore complaint
- click `Open CASE Singapore flow`
- observe `/live-fill`
- confirm a real CASE reference number

### Main evidence surface

- chat strategy generation
- live SSE pipeline updates
- multi-step browser execution
- terminal success card with reference number

### Best files for a quick technical review

- `EVALUATION_SUMMARY.md`
- `LLM_SUBMISSION_GUIDE.md`
- `tests/test_case_portal_flow.py`
- `agent/engine/chat_orchestrator.py`
- `agent/engine/executor.py`

### What this repo does not claim

- full production complaint-platform coverage
- complete PRD implementation
- universal autonomous filing across all complaint classes

## Executive Summary

The highest-signal path in this repository is:

1. open `http://127.0.0.1:8000/chat?new=1`
2. send:

```text
Shopee Singapore sold me a defective laptop and they are refusing a refund.
```

3. wait for strategy generation
4. click `Open CASE Singapore flow`
5. observe the redirect into `/live-fill`
6. observe the multi-step CASE filing complete with a real confirmation reference number

This is not presented as full complaint-platform coverage across every company, regulator, and geography. It is presented as a verified end-to-end filing path with real browser execution and live status streaming.

## What This Repository Actually Demonstrates

### Verified capabilities

- Complaint understanding from chat
- Strategy generation for a supported Singapore complaint path
- Explicit handoff from chat into filing
- Live SSE status updates during scout and fill
- Real browser automation with Playwright
- Real multi-step CASE Singapore form completion
- Real confirmation-number capture from the CASE success page

### Supported primary flow

- Entry surface: `/chat?new=1`
- Complaint example: Shopee Singapore refund dispute
- Filing target: `CASE Singapore`
- Live run surface: `/live-fill`
- Status transport: `GET /api/status/{run_id}`
- Terminal outcome: confirmed submission state with reference number

### Secondary surfaces

- `/demo` remains available as a fallback execution surface
- `/demo/status` remains available as a fallback run-status page
- profile, upload, search, auth, B2B, and scheduler routes exist, but they are not the primary evaluator path

## Product Truth

This repository centers one verified vertical slice rather than claiming full production coverage.

What is implemented and central:

- a working chat-to-strategy path
- a working chat-to-filing handoff
- a working multi-step CASE Singapore filing path
- a working live status surface for filing runs

What is deliberately not claimed as complete:

- full PRD completion
- universal autonomous routing across all complaint classes
- production-grade multi-user isolation
- production-ready outbound email delivery in every environment
- full production infrastructure around queuing, persistence, and delivery

## Strongest Reviewer Flow

Use this exact path for evaluation.

### Step 1. Start the server

```bash
python -m uvicorn app:app --port 8000
```

### Step 2. Open a fresh chat session

```text
http://127.0.0.1:8000/chat?new=1
```

### Step 3. Send the complaint

```text
Shopee Singapore sold me a defective laptop and they are refusing a refund.
```

### Step 4. Observe the strategy

Expected behavior:

- the complaint is understood as a Singapore consumer dispute
- the response explains the near-term path
- the UI renders a specific filing action button
- the button label is `Open CASE Singapore flow`

### Step 5. Start live filing

Click `Open CASE Singapore flow`.

Expected behavior:

- the app redirects to `/live-fill`
- the pipeline shows scout, mapping, and browser-execution progress
- the filing continues through the CASE workflow

### Step 6. Confirm the terminal result

Expected behavior:

- the result card becomes visible
- the title reads `Live filing completed`
- the result includes a real CASE reference number

## End-to-End Runtime Flow

The primary path moves through the following stages:

1. The user enters a complaint in `/chat`.
2. `POST /api/v3/chat` routes the message into the complaint orchestrator.
3. The orchestrator extracts the company and complaint context.
4. The escalation knowledge base selects a supported path for the complaint.
5. The chat UI renders action buttons derived from the returned strategy.
6. The user clicks `Open CASE Singapore flow`.
7. Chat calls `POST /api/v3/chat` again with an explicit action instead of free-text intent.
8. The orchestrator resolves the filing portal and starts a scout run.
9. `/live-fill` subscribes to `GET /api/status/{run_id}` and renders progress.
10. The scout step resolves the target URL, extracts the form genome, classifies fields, and computes mappings.
11. If the scout result does not require more mandatory user input, the live-fill page auto-continues into the fill phase.
12. Playwright executes the filing flow across the CASE steps.
13. The executor waits for the actual confirmed submission state.
14. The final page extracts and returns the CASE reference number.
15. `/live-fill` renders the completion card with the final submission result.

## Core User-Facing Surfaces

### `/`

Landing page and top-level product framing.

### `/chat`

Primary complaint-assistant interface.

### `/chat?new=1`

Fresh chat session. This is the recommended start point for evaluation because it clears prior chat state without requiring deeper setup.

### `/live-fill`

Run-status surface for the filing path. This page is where the evaluator sees:

- pipeline stages
- streaming logs
- final success or error state
- confirmation-number capture on successful runs

### `/demo`

Secondary execution surface. It is useful for fallback and engine-level form work, but it is not the primary repository story.

## Main API Surface

### Core routes for the verified path

- `POST /api/v3/chat`
- `GET /api/v3/profile`
- `POST /api/v3/profile`
- `POST /api/engine/run`
- `GET /api/status/{run_id}`
- `GET /api/v3/stream/{run_id}`
- `GET /api/health`

### Additional routes present in the repository

- complaint tracking routes under `/api/v3/complaints`
- upload route at `/api/v3/upload`
- email route at `/api/v3/email/send`
- search routes under `/api/v3/search`
- scheduler routes under `/api/v3/scheduler`
- auth/profile routes under `/api/auth`
- B2B routes under `/api/b2b`

These exist, but they are not the primary basis for evaluating the repository.

## Architecture Overview

### Application layer

- `app.py` hosts the FastAPI application, page routes, API routes, status aliases, and major integration points.

### Complaint orchestration layer

- `agent/engine/chat_orchestrator.py` manages:
  - session state
  - complaint understanding
  - strategy generation
  - explicit action handling
  - scout-result processing
  - transitions between strategy, scout, collect, and fill phases

### Strategy and escalation layer

- `agent/engine/escalation_kb.py` contains:
  - escalation paths
  - company-to-category mappings
  - strategy generation logic
  - LLM-assisted industry classification

This is where the Singapore complaint categories and the CASE Singapore path are defined.

### Engine layer

- `agent/engine/orchestrator.py` coordinates scout and fill runs.
- `agent/engine/executor.py` drives the browser automation and terminal submission logic.
- `agent/engine/field_mapper.py` handles genome-to-user-data mapping, including CASE-specific normalization logic.
- `agent/engine/browser_pool.py` manages Playwright browser resources and supports headful visible execution through:
  - `GRIPPY_BROWSER_HEADLESS`
  - `GRIPPY_BROWSER_SLOWMO_MS`

### Persistence and caching layer

- `agent/engine/genome_db.py` stores:
  - extracted genomes
  - mappings
  - fill history
  - confirmation numbers

### Frontend layer

- `templates/app.html` provides the chat UI and strategy-action rendering.
- `templates/live_fill.html` provides the filing pipeline and terminal result UI.
- `templates/demo.html` provides the fallback engine surface.

## Why CASE Singapore Is The Strongest Path

The repository contains more than one surface, but CASE Singapore is the highest-signal supported path because it combines:

- complaint understanding
- deterministic escalation selection
- explicit chat-to-filing handoff
- live browser automation
- multi-step form completion
- real terminal confirmation capture

That makes it the strongest path for a technical evaluator trying to distinguish a static UI from a working end-to-end flow.

## Setup

### Prerequisites

- Python 3.11
- Chromium runtime for Playwright
- an OpenAI-compatible API key in `.env`

### Install

```bash
git clone https://github.com/13shreyansh/grippyyy.git
cd grippyyy
pip install -r requirements.txt
python -m playwright install chromium
```

### Environment

Create `.env`:

```env
OPENAI_API_KEY=your_openai_api_key
```

### Run locally

```bash
python -m uvicorn app:app --port 8000
```

Open:

```text
http://127.0.0.1:8000/chat?new=1
```

## Optional Configuration

The strongest verified path does not require every optional service to be configured.

Optional configuration surfaces include:

- SMTP or SendGrid for outbound email
- search-provider keys for broader portal discovery
- CAPTCHA solver keys
- Redis-backed session storage
- browser presentation controls for live runs

Environment variables used for visible browser runs include:

```env
GRIPPY_BROWSER_HEADLESS=false
GRIPPY_BROWSER_SLOWMO_MS=120
```

## Testing And Verification

### Targeted checks

```bash
python -m compileall app.py agent
pytest tests/test_case_portal_flow.py tests/test_chat_live_flow.py tests/test_demo_url_normalization.py -q
```

These targeted checks validate the strongest supported path and its supporting pieces:

- CASE portal flow completion
- chat-to-live-fill behavior
- URL normalization for the execution surface

### What the tests are intended to prove

- the Singapore strategy path routes correctly
- chat action handling works
- the CASE-specific filing path reaches a terminal success state
- the public execution surface does not fail on bare-domain input

## Repository Map

### Primary files for evaluators

- `EVALUATION_SUMMARY.md`
- `LLM_SUBMISSION_GUIDE.md`
- `README.md`
- `app.py`
- `agent/engine/chat_orchestrator.py`
- `agent/engine/escalation_kb.py`
- `agent/engine/field_mapper.py`
- `agent/engine/executor.py`
- `agent/engine/browser_pool.py`
- `templates/app.html`
- `templates/live_fill.html`
- `tests/test_case_portal_flow.py`
- `tests/test_chat_live_flow.py`

### Supporting files

- `DEMO_RUNBOOK.md`
- `DEMO_OPERATOR_GUIDE.md`
- `SETUP.md`
- `scripts/start_demo_session.sh`
- `scripts/stop_demo_session.sh`

## Secondary And Legacy Surfaces

The repository still includes some secondary or compatibility surfaces.

### Secondary

- `/demo`
- `/demo/status`

### Legacy compatibility screens

- `/verify`
- `/status`

These are not the main evaluator story. The repository positions `/chat?new=1` to `/live-fill` as the primary path.

## Scope Boundaries

This repository does not claim:

- complete coverage for every complaint class
- production-grade identity and tenant isolation
- guaranteed live email delivery in every environment
- a fully productized background-worker and infrastructure stack

It does claim:

- a working complaint-intelligence layer
- a working browser-execution layer
- a verified chat-to-filing path for CASE Singapore
- a confirmed submission state with reference-number capture on the primary path

## Best Files To Inspect First

If you are reviewing quickly, inspect these in order:

1. `EVALUATION_SUMMARY.md`
2. `LLM_SUBMISSION_GUIDE.md`
3. `README.md`
4. `tests/test_case_portal_flow.py`
5. `tests/test_chat_live_flow.py`
6. `agent/engine/chat_orchestrator.py`
7. `agent/engine/executor.py`
8. `templates/app.html`
9. `templates/live_fill.html`

## Evaluation Notes

- The strongest path is chat-first, not form-first.
- The strongest path is Shopee Singapore to CASE Singapore.
- `/demo` remains useful, but it is not the main repository narrative.
- The repository is positioned for technical evaluation through one verified vertical slice.

## License

MIT
