import json
import uuid

from fastapi.testclient import TestClient

import app as grippy_app
from agent.engine.browser_pool import get_browser_launch_options
from agent.engine.chat_orchestrator import (
    get_session,
    process_scout_result,
    reset_session,
)
from agent.engine.escalation_kb import classify_industry_llm, generate_strategy


client = TestClient(grippy_app.app)


def _read_terminal_event(run_id: str) -> dict:
    with client.stream("GET", f"/api/status/{run_id}") as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event.get("type") in {"SCOUT_COMPLETE", "SCOUT_FAILED", "COMPLETE", "FAILED", "ERROR"}:
                    return event
    raise AssertionError(f"No terminal event received for run {run_id}")


def test_generate_strategy_routes_shopee_to_case_singapore() -> None:
    strategy = grippy_app.asyncio.run(
        generate_strategy(
            "Shopee",
            "Shopee Singapore sold me a defective laptop and they are refusing a refund.",
            "John Smith",
        )
    )

    assert strategy["industry"] == "ecommerce_singapore"
    form_steps = [step for step in strategy["escalation_path"] if step["action"] == "form"]
    assert form_steps[0]["target"] == "CASE Singapore"


def test_classify_industry_llm_falls_back_to_general_singapore(monkeypatch) -> None:
    class FailingCompletions:
        async def create(self, *args, **kwargs):
            raise RuntimeError("boom")

    class FakeChat:
        completions = FailingCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr("agent.engine.escalation_kb.AsyncOpenAI", lambda: FakeClient())

    category = grippy_app.asyncio.run(
        classify_industry_llm(
            "Mystery Merchant",
            "A consumer complaint in Singapore about a defective order and refund refusal.",
        )
    )

    assert category == "general_singapore"


def test_chat_fill_form_action_starts_case_scout(monkeypatch) -> None:
    session_id = f"flow-{uuid.uuid4().hex}"
    state = get_session(session_id)
    state.phase = "STRATEGY"
    state.target_company = "Shopee"
    state.complaint_data = {
        "issue": "defective laptop and refund refusal",
        "complaint_description": "Shopee Singapore sold me a defective laptop and they are refusing a refund.",
    }
    state.strategy = {
        "first_step": {"action": "email"},
        "escalation_path": [
            {"step": 1, "action": "email", "target": "Merchant Customer Support"},
            {
                "step": 2,
                "action": "form",
                "target": "CASE Singapore",
                "portal_key": "case singapore",
            },
        ],
    }

    async def fake_resolve_url(company: str) -> dict:
        assert company == "case singapore"
        return {"url": "https://www.case.org.sg/file-a-complaint/"}

    async def fake_scout_form(url: str, **kwargs) -> dict:
        return {
            "url": url,
            "species": "consumer_complaint",
            "total_fields": 2,
            "have": [{"name": "email", "required": True}],
            "missing": [],
            "case_specific": [],
            "questions": [],
        }

    monkeypatch.setattr(grippy_app, "resolve_url", fake_resolve_url)
    monkeypatch.setattr(grippy_app, "scout_form", fake_scout_form)

    response = client.post(
        "/api/v3/chat",
        json={"session_id": session_id, "message": "", "action": "fill_form"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "SCOUT"
    assert payload["live_fill_target"] == "CASE Singapore"
    terminal = _read_terminal_event(payload["run_id"])
    assert terminal["type"] == "SCOUT_COMPLETE"
    assert terminal["url"] == "https://www.case.org.sg/file-a-complaint/"

    reset_session(session_id)


def test_continue_fill_action_starts_engine_run(monkeypatch) -> None:
    session_id = f"fill-{uuid.uuid4().hex}"
    state = get_session(session_id)
    state.phase = "FILL"
    state.target_company = "Shopee"
    state.target_url = "https://www.case.org.sg/file-a-complaint/"
    state.complaint_data = {"issue": "defective laptop"}

    async def fake_run_engine(url: str, user_data: dict, progress_callback=None, **kwargs) -> dict:
        assert url == "https://www.case.org.sg/file-a-complaint/"
        if progress_callback:
            await progress_callback("Executing form fill...")
        return {
            "success": True,
            "success_rate": 100.0,
            "total_successes": 12,
            "total_failures": 0,
            "timing": {"total": 1.23},
        }

    monkeypatch.setattr(grippy_app, "run_engine", fake_run_engine)

    response = client.post(
        "/api/v3/chat",
        json={"session_id": session_id, "message": "", "action": "continue_fill"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "FILL"
    terminal = _read_terminal_event(payload["run_id"])
    assert terminal["type"] == "COMPLETE"
    assert terminal["result"]["success"] is True

    reset_session(session_id)


def test_process_scout_result_auto_continues_when_only_optional_fields() -> None:
    session_id = f"scout-{uuid.uuid4().hex}"
    state = get_session(session_id)
    state.phase = "SCOUT"

    result = grippy_app.asyncio.run(
        process_scout_result(
            session_id,
            {
                "have": [{"name": "email", "required": True}],
                "missing": [],
                "case_specific": [{"name": "optional_note", "label": "Optional note", "required": False}],
                "questions": [{"field_name": "optional_note", "question": "Optional note?"}],
                "total_fields": 2,
                "species": "consumer_complaint",
                "url": "https://www.case.org.sg/file-a-complaint/",
            },
        )
    )

    assert result["phase"] == "FILL"
    assert result["action"] == "start_fill"
    assert "optional field" in result["reply"]

    reset_session(session_id)


def test_process_scout_result_collects_only_required_fields() -> None:
    session_id = f"collect-{uuid.uuid4().hex}"
    state = get_session(session_id)
    state.phase = "SCOUT"

    result = grippy_app.asyncio.run(
        process_scout_result(
            session_id,
            {
                "have": [{"name": "email", "required": True}],
                "missing": [{"name": "invoice", "label": "Invoice number", "required": True}],
                "case_specific": [{"name": "optional_note", "label": "Optional note", "required": False}],
                "questions": [
                    {"field_name": "invoice", "question": "What is the invoice number?"},
                    {"field_name": "optional_note", "question": "Optional note?"},
                ],
                "total_fields": 3,
                "species": "consumer_complaint",
                "url": "https://www.case.org.sg/file-a-complaint/",
            },
        )
    )

    assert result["phase"] == "COLLECT"
    assert "invoice number" in result["reply"].lower()
    assert "optional note" not in result["reply"].lower()

    reset_session(session_id)


def test_browser_launch_options_respect_demo_env(monkeypatch) -> None:
    monkeypatch.setenv("GRIPPY_BROWSER_HEADLESS", "false")
    monkeypatch.setenv("GRIPPY_BROWSER_SLOWMO_MS", "120")

    options = get_browser_launch_options()

    assert options["headless"] is False
    assert options["slow_mo"] == 120
