import json

from fastapi.testclient import TestClient

import app as grippy_app
from agent.engine.url_utils import normalize_demo_url


client = TestClient(grippy_app.app)


def _read_terminal_event(run_id: str) -> dict:
    with client.stream("GET", f"/api/status/{run_id}") as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event.get("type") in {"COMPLETE", "FAILED", "ERROR"}:
                    return event
    raise AssertionError(f"No terminal event received for run {run_id}")


def test_normalize_demo_url_accepts_bare_domains() -> None:
    assert normalize_demo_url("httpbin.org/forms/post") == "https://httpbin.org/forms/post"
    assert normalize_demo_url("httpbin.org") == "https://httpbin.org"
    assert normalize_demo_url("localhost:8000/demo") == "http://localhost:8000/demo"
    assert normalize_demo_url("127.0.0.1:8000/demo") == "http://127.0.0.1:8000/demo"


def test_normalize_demo_url_rejects_invalid_inputs() -> None:
    for raw in ("", "foo", "javascript:alert(1)", "file:///tmp/test.html", "mailto:test@example.com"):
        try:
            normalize_demo_url(raw)
        except ValueError as exc:
            assert "Invalid URL" in str(exc)
        else:
            raise AssertionError(f"Expected ValueError for {raw!r}")


def test_engine_run_normalizes_bare_domain(monkeypatch) -> None:
    captured: list[str] = []

    async def fake_run_engine(url: str, user_data: dict, progress_callback=None, **kwargs) -> dict:
        captured.append(url)
        if progress_callback:
            await progress_callback("Opening form...")
        return {
            "success": True,
            "success_rate": 100.0,
            "cache_hit": False,
            "species": "test_form",
            "total_successes": 1,
            "total_failures": 0,
            "details": [],
            "timing": {"total": 0.01},
            "dry_run": kwargs.get("dry_run", False),
            "step_mappings": [],
            "genome": {},
        }

    monkeypatch.setattr(grippy_app, "run_engine", fake_run_engine)

    response = client.post(
        "/api/engine/run",
        json={
            "url": "httpbin.org/forms/post",
            "user_data": {"full_name": "Demo User"},
            "dry_run": True,
            "user_intent": "Fill this form",
        },
    )

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    terminal = _read_terminal_event(run_id)
    assert terminal["type"] == "COMPLETE"
    assert captured == ["https://httpbin.org/forms/post"]


def test_engine_run_rejects_invalid_url() -> None:
    response = client.post(
        "/api/engine/run",
        json={
            "url": "javascript:alert(1)",
            "user_data": {"full_name": "Demo User"},
            "dry_run": True,
            "user_intent": "Fill this form",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Invalid URL. Use http(s):// or enter a bare domain like httpbin.org"
    }


def test_fill_history_normalizes_query_url(monkeypatch) -> None:
    captured: list[str | None] = []

    def fake_get_fill_history(url=None, limit: int = 50):
        captured.append(url)
        return []

    monkeypatch.setattr(grippy_app, "get_fill_history", fake_get_fill_history)

    response = client.get("/api/engine/fill-history", params={"url": "httpbin.org/forms/post"})

    assert response.status_code == 200
    assert captured == ["https://httpbin.org/forms/post"]


def test_invalidate_cache_normalizes_payload_url(monkeypatch) -> None:
    captured: list[str] = []

    def fake_invalidate_cache(url: str) -> bool:
        captured.append(url)
        return True

    monkeypatch.setattr(grippy_app, "invalidate_cache", fake_invalidate_cache)

    response = client.post("/api/engine/invalidate-cache", json={"url": "httpbin.org/forms/post"})

    assert response.status_code == 200
    assert response.json() == {
        "invalidated": True,
        "url": "https://httpbin.org/forms/post",
    }
    assert captured == ["https://httpbin.org/forms/post"]
