from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Browser, Page, async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8000"
HTTPBIN_URL = "https://httpbin.org/forms/post"
CHAT_DEMO_MESSAGE = "Shopee Singapore sold me a defective laptop and they are refusing a refund."
DEMO_USER = {
    "full_name": "Demo User",
    "given_name": "Demo",
    "family_name": "User",
    "email": "demo.user@example.com",
    "phone": "+1 415 555 0123",
}
ENGINE_USER_DATA = {
    "full_name": "John Smith",
    "given_name": "John",
    "family_name": "Smith",
    "email": "john.smith@example.com",
    "phone_mobile": "+6591234567",
}


@dataclass
class CheckResult:
    name: str
    scope: str
    status: str
    details: str = ""
    duration_s: float = 0.0
    artifact: str = ""


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def make_artifact_paths(stamp: str) -> dict[str, Path]:
    root = ROOT / "artifacts" / "demo_check" / stamp
    shots = root / "screenshots"
    logs = root / "logs"
    for path in (root, shots, logs):
        path.mkdir(parents=True, exist_ok=True)
    return {"root": root, "screenshots": shots, "logs": logs}


def append_result(results: list[CheckResult], result: CheckResult) -> None:
    results.append(result)
    print(f"[{result.scope}] {result.name}: {result.status} {result.details}")


def failure_result(
    name: str,
    scope: str,
    exc: Exception,
    artifact: str = "",
) -> CheckResult:
    detail = f"{type(exc).__name__}: {exc}"
    return CheckResult(name, scope, "fail", detail, 0.0, artifact)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def run_command(args: list[str], log_path: Path) -> CheckResult:
    start = time.perf_counter()
    proc = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    write_text(log_path, output)
    status = "pass" if proc.returncode == 0 else "fail"
    detail = f"exit={proc.returncode}"
    return CheckResult(log_path.stem, "P1", status, detail, time.perf_counter() - start, str(log_path))


def run_truthfulness_scan(log_path: Path) -> CheckResult:
    args = [
        "rg",
        "-n",
        "production-ready|production ready|fully autonomous|full autonomy|"
        "guaranteed|works everywhere|automatic escalation|send email automatically|"
        "universal coverage|/api/v5",
        "README.md",
        "templates/index.html",
        "templates/app.html",
        "templates/demo.html",
    ]
    start = time.perf_counter()
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")
    write_text(log_path, output)
    if proc.returncode == 1:
        return CheckResult("truthfulness_scan", "P0", "pass", "No overclaiming copy found", time.perf_counter() - start, str(log_path))
    if proc.returncode == 0:
        return CheckResult("truthfulness_scan", "P0", "fail", "Potential overclaiming copy found", time.perf_counter() - start, str(log_path))
    return CheckResult("truthfulness_scan", "P0", "fail", f"rg exit={proc.returncode}", time.perf_counter() - start, str(log_path))


def start_server(log_path: Path) -> subprocess.Popen[str]:
    log_file = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", "8000"],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_server(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


async def wait_for_server(base_url: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get("/api/health")
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise RuntimeError("Server did not become healthy in time")


async def api_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    response = await client.request(method, path, **kwargs)
    return response


async def collect_sse(
    client: httpx.AsyncClient,
    path: str,
    timeout_s: float = 120.0,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout_s):
        async with client.stream("GET", path) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                events.append(payload)
                if payload.get("type") in {"COMPLETE", "FAILED", "ERROR", "SCOUT_COMPLETE", "SCOUT_FAILED"}:
                    return events
    return events


async def check_routes(
    client: httpx.AsyncClient,
    log_path: Path,
) -> CheckResult:
    start = time.perf_counter()
    paths = ["/", "/chat", "/demo", "/demo/status", "/status", "/dashboard", "/login", "/api-docs"]
    statuses: dict[str, int] = {}
    for path in paths:
        response = await api_request(client, "GET", path)
        statuses[path] = response.status_code
    write_json(log_path, statuses)
    ok = all(code == 200 for code in statuses.values())
    detail = ", ".join(f"{path}={code}" for path, code in statuses.items())
    return CheckResult("route_loads", "P0", "pass" if ok else "fail", detail, time.perf_counter() - start, str(log_path))


async def snapshot_profile(client: httpx.AsyncClient, path: Path) -> dict[str, Any]:
    response = await api_request(client, "GET", "/api/v3/profile")
    response.raise_for_status()
    snapshot = response.json()
    write_json(path, snapshot)
    return snapshot


async def clear_profile(client: httpx.AsyncClient) -> None:
    response = await api_request(client, "DELETE", "/api/v3/profile")
    response.raise_for_status()


async def restore_profile(client: httpx.AsyncClient, snapshot: dict[str, Any]) -> None:
    await clear_profile(client)
    profile = snapshot.get("profile") or {}
    if profile:
        response = await api_request(client, "POST", "/api/v3/profile", json={"data": profile})
        response.raise_for_status()


async def capture_landing(page: Page, base_url: str, path: Path) -> None:
    await page.goto(f"{base_url}/", wait_until="networkidle")
    await page.screenshot(path=str(path), full_page=True)


async def capture_chat_flow(page: Page, base_url: str, shots: Path) -> dict[str, Any]:
    await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
    await page.wait_for_selector("#chatArea .msg-wrapper")
    await page.screenshot(path=str(shots / "chat_loaded.png"), full_page=True)
    if "what's your name" in (await page.locator("#chatArea").inner_text()).lower():
        await page.fill("#msgInput", "My name is Demo User, demo.user@example.com, +1 415 555 0123")
        await page.click("#sendBtn")
        await page.wait_for_function(
            "() => document.querySelector('#chatArea')?.innerText.toLowerCase().includes('all set')",
            timeout=60000,
        )
    await page.screenshot(path=str(shots / "chat_onboarding_complete.png"), full_page=True)
    await page.fill("#msgInput", CHAT_DEMO_MESSAGE)
    await page.click("#sendBtn")
    await page.wait_for_function(
        "() => document.querySelector('#chatArea')?.innerText.toLowerCase().includes('draft the complaint email')",
        timeout=90000,
    )
    await page.screenshot(path=str(shots / "chat_strategy.png"), full_page=True)
    await page.fill("#msgInput", "Draft the email")
    await page.click("#sendBtn")
    await page.wait_for_function(
        "() => document.querySelector('#chatArea')?.innerText.toLowerCase().includes(\"here's your complaint email\")",
        timeout=90000,
    )
    await page.screenshot(path=str(shots / "chat_email_draft.png"), full_page=True)
    text = await page.locator("#chatArea").inner_text()
    return {
        "onboarding_complete": "all set" in text.lower() or "what's bothering you" in text.lower(),
        "strategy_visible": "draft the complaint email" in text.lower(),
        "draft_visible": "here's your complaint email" in text.lower(),
        "email_send_truthful": "not configured in this environment" in text.lower(),
    }


async def capture_demo_flow(page: Page, base_url: str, shots: Path) -> dict[str, Any]:
    await page.goto(f"{base_url}/demo", wait_until="domcontentloaded")
    await page.wait_for_selector("#urlInput")
    await page.screenshot(path=str(shots / "demo_loaded.png"), full_page=True)
    await page.evaluate("loadPreset('httpbin')")
    await page.get_by_role("button", name="Scan & Map").click()
    await page.wait_for_function(
        "() => document.querySelector('#detectionBadge')?.innerText.toLowerCase().includes('ready to fill')",
        timeout=120000,
    )
    await page.screenshot(path=str(shots / "demo_scan_complete.png"), full_page=True)
    await page.get_by_role("button", name="Fill the Form").click()
    await page.wait_for_url("**/demo/status**", timeout=30000)
    await page.wait_for_function(
        "() => document.querySelector('#logContainer')?.innerText.length > 40",
        timeout=120000,
    )
    await page.screenshot(path=str(shots / "demo_fill_in_progress.png"), full_page=True)
    await page.wait_for_function(
        "() => document.querySelector('#resultTitle')?.innerText.length > 0",
        timeout=180000,
    )
    await page.screenshot(path=str(shots / "demo_fill_complete.png"), full_page=True)
    title = await page.locator("#resultTitle").inner_text()
    subtitle = await page.locator("#resultSub").inner_text()
    logs = await page.locator("#logContainer").inner_text()
    return {"title": title, "subtitle": subtitle, "logs": logs}


async def run_chat_api_flow(
    client: httpx.AsyncClient,
    logs: Path,
) -> CheckResult:
    start = time.perf_counter()
    session_id = uuid.uuid4().hex
    first = await api_request(client, "POST", "/api/v3/chat", json={"session_id": session_id, "message": CHAT_DEMO_MESSAGE})
    first.raise_for_status()
    first_json = first.json()
    write_json(logs / "chat_strategy_response.json", first_json)
    second = await api_request(client, "POST", "/api/v3/chat", json={"session_id": session_id, "message": "Draft the email"})
    second.raise_for_status()
    second_json = second.json()
    write_json(logs / "chat_email_draft_response.json", second_json)
    ok = (
        first_json.get("phase") == "STRATEGY"
        and bool(first_json.get("strategy"))
        and "Here" in second_json.get("reply", "")
    )
    detail = f"phase={first_json.get('phase')} strategy={bool(first_json.get('strategy'))}"
    return CheckResult("chat_api_known_complaint", "P0", "pass" if ok else "fail", detail, time.perf_counter() - start, str(logs / "chat_strategy_response.json"))


async def check_email_fail_closed(
    client: httpx.AsyncClient,
    logs: Path,
) -> CheckResult:
    start = time.perf_counter()
    session_id = uuid.uuid4().hex
    await api_request(client, "POST", "/api/v3/chat", json={"session_id": session_id, "message": CHAT_DEMO_MESSAGE})
    await api_request(client, "POST", "/api/v3/chat", json={"session_id": session_id, "message": "Draft the email"})
    response = await api_request(
        client,
        "POST",
        "/api/v3/email/send",
        json={"session_id": session_id, "to_email": "support@example.com", "confirm": True},
    )
    response.raise_for_status()
    payload = response.json()
    write_json(logs / "email_send_response.json", payload)
    ok = payload.get("success") is False and payload.get("backend") == "dev_mode"
    return CheckResult("email_fail_closed", "P0", "pass" if ok else "fail", f"success={payload.get('success')} backend={payload.get('backend')}", time.perf_counter() - start, str(logs / "email_send_response.json"))


async def run_engine_once(
    client: httpx.AsyncClient,
    dry_run: bool,
    suffix: str,
    logs: Path,
) -> tuple[CheckResult, dict[str, Any], list[dict[str, Any]]]:
    start = time.perf_counter()
    response = await api_request(
        client,
        "POST",
        "/api/engine/run",
        json={"url": HTTPBIN_URL, "user_data": ENGINE_USER_DATA, "dry_run": dry_run, "user_intent": "Fill this public demo form"},
    )
    response.raise_for_status()
    payload = response.json()
    events = await collect_sse(client, f"/api/status/{payload['run_id']}", timeout_s=240.0)
    write_json(logs / f"engine_{suffix}_events.json", {"run_id": payload["run_id"], "events": events})
    terminal = events[-1] if events else {}
    ok = terminal.get("type") == "COMPLETE"
    detail = f"run_id={payload['run_id']} terminal={terminal.get('type')}"
    scope = "P0" if suffix in {"scan_ui", "fill_ui"} else "P1"
    return CheckResult(f"engine_{suffix}", scope, "pass" if ok else "fail", detail, time.perf_counter() - start, str(logs / f"engine_{suffix}_events.json")), terminal, events


async def check_status_alias(client: httpx.AsyncClient, logs: Path) -> CheckResult:
    result, terminal, _ = await run_engine_once(client, True, "status_alias", logs)
    ok = result.status == "pass" and bool(terminal.get("result"))
    return CheckResult("status_alias_stream", "P0", "pass" if ok else "fail", result.details, result.duration_s, result.artifact)


async def check_profile_crud(
    client: httpx.AsyncClient,
    logs: Path,
) -> CheckResult:
    start = time.perf_counter()
    before = (await api_request(client, "GET", "/api/v3/profile")).json()
    update = await api_request(client, "POST", "/api/v3/profile", json={"data": {"full_name": "Audit User", "email": "audit.user@example.com"}})
    update.raise_for_status()
    after_update = (await api_request(client, "GET", "/api/v3/profile")).json()
    cleared = await api_request(client, "DELETE", "/api/v3/profile")
    cleared.raise_for_status()
    after_delete = (await api_request(client, "GET", "/api/v3/profile")).json()
    payload = {"before": before, "after_update": after_update, "after_delete": after_delete}
    write_json(logs / "profile_crud.json", payload)
    ok = bool(after_update.get("profile")) and not after_delete.get("profile")
    return CheckResult("profile_crud", "P1", "pass" if ok else "fail", "GET/POST/DELETE profile", time.perf_counter() - start, str(logs / "profile_crud.json"))


async def smoke_endpoints(client: httpx.AsyncClient, logs: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    simple_paths = {
        "health": "/api/health",
        "cache_stats": "/api/engine/cache-stats",
    }
    for name, path in simple_paths.items():
        start = time.perf_counter()
        response = await api_request(client, "GET", path)
        payload = response.json()
        write_json(logs / f"{name}.json", payload)
        status = "pass" if response.status_code == 200 else "fail"
        results.append(CheckResult(name, "P1", status, f"status={response.status_code}", time.perf_counter() - start, str(logs / f"{name}.json")))
    return results


async def smoke_upload_fail_closed(client: httpx.AsyncClient, logs: Path) -> CheckResult:
    start = time.perf_counter()
    files = {"file": ("demo.txt", b"not a pdf or image", "text/plain")}
    response = await client.post("/api/v3/upload", files=files)
    payload: dict[str, Any] = {"status_code": response.status_code}
    try:
        payload["body"] = response.json()
    except Exception:
        payload["body"] = response.text
    write_json(logs / "upload_smoke.json", payload)
    ok = response.status_code in {200, 400}
    return CheckResult("upload_fail_closed", "P1", "pass" if ok else "fail", f"status={response.status_code}", time.perf_counter() - start, str(logs / "upload_smoke.json"))


async def run_chat_stress(client: httpx.AsyncClient, logs: Path) -> CheckResult:
    start = time.perf_counter()
    durations: list[float] = []
    failures: list[str] = []
    for idx in range(5):
        session_id = uuid.uuid4().hex
        turn_start = time.perf_counter()
        first = await api_request(client, "POST", "/api/v3/chat", json={"session_id": session_id, "message": CHAT_DEMO_MESSAGE})
        second = await api_request(client, "POST", "/api/v3/chat", json={"session_id": session_id, "message": "Draft the email"})
        durations.append(time.perf_counter() - turn_start)
        first_json = first.json()
        second_json = second.json()
        if first_json.get("phase") != "STRATEGY" or "complaint email" not in second_json.get("reply", "").lower():
            failures.append(f"run_{idx+1}")
    payload = {"durations_s": durations, "failures": failures}
    write_json(logs / "chat_stress.json", payload)
    ok = not failures
    detail = f"{5 - len(failures)}/5 passed"
    return CheckResult("chat_stress", "P0", "pass" if ok else "fail", detail, time.perf_counter() - start, str(logs / "chat_stress.json"))


async def run_engine_stress(client: httpx.AsyncClient, logs: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    await api_request(client, "POST", "/api/engine/invalidate-cache", json={"url": HTTPBIN_URL})
    scan_runs: list[dict[str, Any]] = []
    fill_runs: list[dict[str, Any]] = []
    for idx in range(3):
        result, terminal, _ = await run_engine_once(client, True, f"scan_{idx+1}", logs)
        scan_runs.append({"result": asdict(result), "terminal": terminal})
        results.append(CheckResult(f"scan_run_{idx+1}", "P0", result.status, result.details, result.duration_s, result.artifact))
    for idx in range(2):
        result, terminal, _ = await run_engine_once(client, False, f"fill_{idx+1}", logs)
        fill_runs.append({"result": asdict(result), "terminal": terminal})
        results.append(CheckResult(f"fill_run_{idx+1}", "P0", result.status, result.details, result.duration_s, result.artifact))
    parallel = await asyncio.gather(
        run_engine_once(client, True, "parallel_scan_a", logs),
        run_engine_once(client, True, "parallel_scan_b", logs),
    )
    payload = {
        "scan_runs": scan_runs,
        "fill_runs": fill_runs,
        "parallel_runs": [asdict(item[0]) for item in parallel],
    }
    write_json(logs / "engine_stress_summary.json", payload)
    return results


def compute_go(results: list[CheckResult]) -> tuple[bool, list[CheckResult]]:
    blockers = [item for item in results if item.scope == "P0" and item.status != "pass"]
    return (not blockers, blockers)


def build_summary(stamp: str, results: list[CheckResult], blockers: list[CheckResult]) -> str:
    go = "GO" if not blockers else "NO-GO"
    lines = [
        "# Grippy Demo Readiness Summary",
        "",
        f"- Timestamp: `{stamp}`",
        f"- Decision: **{go}**",
        f"- Scope: truthful demo path only (`/chat` + Shopee/CASE, `/demo` + httpbin)",
        "",
        "## P0 Checks",
    ]
    for item in results:
        if item.scope != "P0":
            continue
        lines.append(f"- `{item.status.upper()}` {item.name}: {item.details}")
    lines.extend(["", "## P1 Checks"])
    for item in results:
        if item.scope != "P1":
            continue
        lines.append(f"- `{item.status.upper()}` {item.name}: {item.details}")
    if blockers:
        lines.extend(["", "## Blockers"])
        for item in blockers:
            lines.append(f"- {item.name}: {item.details}")
    lines.extend([
        "",
        "## Artifacts",
        f"- Summary: `artifacts/demo_check/{stamp}/summary.md`",
        f"- Results: `artifacts/demo_check/{stamp}/results.json`",
        f"- Screenshots: `artifacts/demo_check/{stamp}/screenshots/`",
        f"- Logs: `artifacts/demo_check/{stamp}/logs/`",
    ])
    return "\n".join(lines) + "\n"


async def main() -> int:
    stamp = timestamp_slug()
    paths = make_artifact_paths(stamp)
    results: list[CheckResult] = []
    original_profile: dict[str, Any] | None = None

    append_result(results, run_command([sys.executable, "-m", "compileall", "app.py", "agent"], paths["logs"] / "compileall.txt"))
    append_result(results, run_command([sys.executable, "tests/test_all_features.py"], paths["logs"] / "test_all_features.txt"))
    append_result(results, run_truthfulness_scan(paths["logs"] / "truthfulness_scan.txt"))

    server = start_server(paths["logs"] / "server.txt")
    try:
        health = await wait_for_server(BASE_URL, timeout_s=90.0)
        write_json(paths["logs"] / "health_boot.json", health)
        append_result(results, CheckResult("server_boot", "P0", "pass", f"status={health.get('status')}", 0.0, str(paths["logs"] / "health_boot.json")))
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=240.0) as client:
            try:
                append_result(results, await check_routes(client, paths["logs"] / "route_loads.json"))
            except Exception as exc:
                append_result(results, failure_result("route_loads", "P0", exc, str(paths["logs"] / "route_loads.json")))
            try:
                for item in await smoke_endpoints(client, paths["logs"]):
                    append_result(results, item)
            except Exception as exc:
                append_result(results, failure_result("smoke_endpoints", "P1", exc))
            try:
                original_profile = await snapshot_profile(client, paths["logs"] / "original_profile.json")
                await clear_profile(client)
            except Exception as exc:
                append_result(results, failure_result("profile_snapshot", "P1", exc, str(paths["logs"] / "original_profile.json")))

            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    await run_ui_checks(browser, paths["screenshots"], results)
                    await browser.close()
            except Exception as exc:
                append_result(results, failure_result("ui_checks", "P0", exc))

            try:
                await api_request(client, "POST", "/api/v3/profile", json={"data": DEMO_USER})
            except Exception as exc:
                append_result(results, failure_result("set_demo_profile", "P1", exc))
            for name, coroutine, scope in [
                ("chat_api_known_complaint", run_chat_api_flow(client, paths["logs"]), "P0"),
                ("email_fail_closed", check_email_fail_closed(client, paths["logs"]), "P0"),
                ("status_alias_stream", check_status_alias(client, paths["logs"]), "P0"),
                ("chat_stress", run_chat_stress(client, paths["logs"]), "P0"),
                ("upload_fail_closed", smoke_upload_fail_closed(client, paths["logs"]), "P1"),
                ("profile_crud", check_profile_crud(client, paths["logs"]), "P1"),
            ]:
                try:
                    append_result(results, await coroutine)
                except Exception as exc:
                    append_result(results, failure_result(name, scope, exc))
            try:
                for item in await run_engine_stress(client, paths["logs"]):
                    append_result(results, item)
            except Exception as exc:
                append_result(results, failure_result("engine_stress", "P0", exc, str(paths["logs"] / "engine_stress_summary.json")))
    except Exception as exc:
        append_result(results, failure_result("server_boot", "P0", exc, str(paths["logs"] / "server.txt")))
    finally:
        if original_profile is not None:
            try:
                async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
                    await restore_profile(client, original_profile)
            except Exception as exc:
                append_result(results, failure_result("restore_profile", "P1", exc))
        stop_server(server)

    go, blockers = compute_go(results)
    write_json(paths["root"] / "results.json", {"go": go, "results": [asdict(item) for item in results]})
    write_text(paths["root"] / "summary.md", build_summary(stamp, results, blockers))
    print(paths["root"])
    return 0 if go else 1


async def run_ui_checks(browser: Browser, shots: Path, results: list[CheckResult]) -> None:
    context = await browser.new_context(viewport={"width": 1440, "height": 1100})
    page = await context.new_page()
    try:
        start = time.perf_counter()
        await capture_landing(page, BASE_URL, shots / "landing.png")
        append_result(results, CheckResult("landing_page_screenshot", "P1", "pass", "Landing page loaded", time.perf_counter() - start, str(shots / "landing.png")))
    except Exception as exc:
        append_result(results, failure_result("landing_page_screenshot", "P1", exc, str(shots / "landing.png")))
    try:
        start = time.perf_counter()
        chat_state = await capture_chat_flow(page, BASE_URL, shots)
        chat_ok = all(chat_state.values())
        append_result(results, CheckResult("chat_ui_flow", "P0", "pass" if chat_ok else "fail", json.dumps(chat_state), time.perf_counter() - start, str(shots / "chat_email_draft.png")))
    except Exception as exc:
        append_result(results, failure_result("chat_ui_flow", "P0", exc, str(shots / "chat_email_draft.png")))
    try:
        start = time.perf_counter()
        demo_state = await capture_demo_flow(page, BASE_URL, shots)
        demo_ok = demo_state["title"] == "Form Filled Successfully"
        write_json(shots.parent / "logs" / "demo_ui_result.json", demo_state)
        append_result(results, CheckResult("demo_ui_flow", "P0", "pass" if demo_ok else "fail", demo_state["title"], time.perf_counter() - start, str(shots / "demo_fill_complete.png")))
    except Exception as exc:
        append_result(results, failure_result("demo_ui_flow", "P0", exc, str(shots / "demo_fill_complete.png")))
    await context.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
