# AGENTS.md — Grippyyy

## Stack

- Python 3.11
- FastAPI
- OpenAI-compatible LLM calls
- Playwright
- Frontend: plain HTML + CSS + vanilla JavaScript
- Streaming: SSE

## Commands

- Install dependencies: `pip install -r requirements.txt`
- Install browser runtime: `python -m playwright install chromium`
- Run server: `uvicorn app:app --reload --port 8000`
- Run tests: `pytest tests/ -v`

## Project Structure

- `app.py` — FastAPI server and routes
- `agent/engine/chat_orchestrator.py` — complaint conversation flow
- `agent/engine/escalation_kb.py` — escalation paths and drafting templates
- `agent/engine/scout.py` — target resolution and scout logic
- `agent/engine/field_mapper.py` — field-to-user-data mapping
- `agent/engine/executor.py` — browser execution engine
- `agent/engine/orchestrator.py` — extraction, mapping, cache, execution pipeline
- `templates/` — HTML templates
- `tests/` — tests

## Code Style

- type hints on public function signatures
- keep functions compact where practical
- prefer f-strings
- async for network-bound API paths

## Constraints

- never hardcode API keys
- never add a database for new product requirements unless explicitly asked
- never add frontend frameworks
- never replace SSE with WebSockets

## Current Strongest Demo Path

- `/chat?new=1`
- message: `Shopee Singapore sold me a defective laptop and they are refusing a refund.`
- click `Open CASE Singapore flow`
- watch `/live-fill` continue to a real CASE confirmation number
