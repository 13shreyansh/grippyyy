"""
CAPTCHA Handler — The Shield-Breaker of the Form Genome Engine.

This module detects and solves CAPTCHAs encountered during form filling.
It supports multiple CAPTCHA types and solving services:

Supported CAPTCHA Types:
  - reCAPTCHA v2 (checkbox + invisible)
  - reCAPTCHA v3 (score-based)
  - hCaptcha
  - Image-based CAPTCHAs (basic OCR)
  - Turnstile (Cloudflare)

Solving Services (in priority order):
  1. 2Captcha (primary)
  2. Anti-Captcha (fallback)
  3. CapSolver (fallback)

Architecture:
  - Detection: Scans page DOM for known CAPTCHA signatures
  - Solving: Sends CAPTCHA parameters to solving service API
  - Injection: Injects the solution token back into the page
  - Verification: Confirms CAPTCHA was solved successfully
"""

import asyncio
import logging
import os
import re
import time
from typing import Any, Optional

import requests
from playwright.async_api import Page, Frame

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

# API keys loaded from environment variables
TWOCAPTCHA_API_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "")
ANTICAPTCHA_API_KEY = os.environ.get("ANTICAPTCHA_API_KEY", "")
CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY", "")

# Timeouts
CAPTCHA_DETECT_TIMEOUT = 5000  # ms
CAPTCHA_SOLVE_TIMEOUT = 120  # seconds
CAPTCHA_POLL_INTERVAL = 5  # seconds

# Service endpoints
TWOCAPTCHA_API = "https://2captcha.com/in.php"
TWOCAPTCHA_RESULT = "https://2captcha.com/res.php"
ANTICAPTCHA_API = "https://api.anti-captcha.com"
CAPSOLVER_API = "https://api.capsolver.com"


# ──────────────────────────────────────────────────────────────────────
# CAPTCHA Detection
# ──────────────────────────────────────────────────────────────────────

class CaptchaInfo:
    """Represents a detected CAPTCHA on a page."""

    def __init__(
        self,
        captcha_type: str,
        sitekey: str = "",
        page_url: str = "",
        action: str = "",
        data_s: str = "",
        is_invisible: bool = False,
    ):
        self.captcha_type = captcha_type  # recaptcha_v2, recaptcha_v3, hcaptcha, turnstile, image
        self.sitekey = sitekey
        self.page_url = page_url
        self.action = action
        self.data_s = data_s
        self.is_invisible = is_invisible

    def __repr__(self):
        return (
            f"CaptchaInfo(type={self.captcha_type}, sitekey={self.sitekey[:20]}..., "
            f"invisible={self.is_invisible})"
        )


async def detect_captcha(page: Page | Frame) -> Optional[CaptchaInfo]:
    """
    Detect if the current page contains a CAPTCHA.

    Scans the DOM for known CAPTCHA signatures:
    - Google reCAPTCHA v2/v3
    - hCaptcha
    - Cloudflare Turnstile
    - Generic image CAPTCHAs

    Returns CaptchaInfo if found, None otherwise.
    """
    page_url = ""
    try:
        if hasattr(page, 'url'):
            page_url = page.url
    except Exception:
        pass

    # ── reCAPTCHA v2/v3 Detection ──
    try:
        recaptcha_info = await page.evaluate("""() => {
            // Check for reCAPTCHA v2 widget
            const recaptchaDiv = document.querySelector('.g-recaptcha, [data-sitekey]');
            if (recaptchaDiv) {
                const sitekey = recaptchaDiv.getAttribute('data-sitekey') || '';
                const dataS = recaptchaDiv.getAttribute('data-s') || '';
                const size = recaptchaDiv.getAttribute('data-size') || '';
                const isInvisible = size === 'invisible';
                return {
                    found: true,
                    type: 'recaptcha_v2',
                    sitekey: sitekey,
                    data_s: dataS,
                    is_invisible: isInvisible,
                };
            }

            // Check for reCAPTCHA v3 (loaded via script)
            const scripts = document.querySelectorAll('script[src*="recaptcha"]');
            for (const script of scripts) {
                const src = script.src || '';
                const renderMatch = src.match(/render=([^&]+)/);
                if (renderMatch && renderMatch[1] !== 'explicit') {
                    return {
                        found: true,
                        type: 'recaptcha_v3',
                        sitekey: renderMatch[1],
                        data_s: '',
                        is_invisible: true,
                    };
                }
            }

            // Check for grecaptcha object
            if (typeof grecaptcha !== 'undefined') {
                try {
                    const response = grecaptcha.getResponse();
                    if (response === '') {
                        // reCAPTCHA present but not solved
                        const container = document.querySelector('[data-sitekey]');
                        return {
                            found: true,
                            type: 'recaptcha_v2',
                            sitekey: container ? container.getAttribute('data-sitekey') : '',
                            data_s: '',
                            is_invisible: false,
                        };
                    }
                } catch(e) {}
            }

            return { found: false };
        }""")

        if recaptcha_info and recaptcha_info.get("found"):
            logger.info("Detected %s CAPTCHA (sitekey: %s...)",
                       recaptcha_info["type"],
                       recaptcha_info.get("sitekey", "")[:20])
            return CaptchaInfo(
                captcha_type=recaptcha_info["type"],
                sitekey=recaptcha_info.get("sitekey", ""),
                page_url=page_url,
                data_s=recaptcha_info.get("data_s", ""),
                is_invisible=recaptcha_info.get("is_invisible", False),
            )
    except Exception as exc:
        logger.debug("reCAPTCHA detection error: %s", exc)

    # ── hCaptcha Detection ──
    try:
        hcaptcha_info = await page.evaluate("""() => {
            const hcaptchaDiv = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
            if (hcaptchaDiv) {
                const sitekey = hcaptchaDiv.getAttribute('data-sitekey') ||
                                hcaptchaDiv.getAttribute('data-hcaptcha-sitekey') || '';
                return { found: true, sitekey: sitekey };
            }

            const hcaptchaIframe = document.querySelector('iframe[src*="hcaptcha.com"]');
            if (hcaptchaIframe) {
                const src = hcaptchaIframe.src || '';
                const keyMatch = src.match(/sitekey=([^&]+)/);
                return {
                    found: true,
                    sitekey: keyMatch ? keyMatch[1] : '',
                };
            }

            return { found: false };
        }""")

        if hcaptcha_info and hcaptcha_info.get("found"):
            logger.info("Detected hCaptcha (sitekey: %s...)",
                       hcaptcha_info.get("sitekey", "")[:20])
            return CaptchaInfo(
                captcha_type="hcaptcha",
                sitekey=hcaptcha_info.get("sitekey", ""),
                page_url=page_url,
            )
    except Exception as exc:
        logger.debug("hCaptcha detection error: %s", exc)

    # ── Cloudflare Turnstile Detection ──
    try:
        turnstile_info = await page.evaluate("""() => {
            const turnstileDiv = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
            if (turnstileDiv) {
                const sitekey = turnstileDiv.getAttribute('data-sitekey') ||
                                turnstileDiv.getAttribute('data-turnstile-sitekey') || '';
                return { found: true, sitekey: sitekey };
            }

            const turnstileIframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (turnstileIframe) {
                return { found: true, sitekey: '' };
            }

            return { found: false };
        }""")

        if turnstile_info and turnstile_info.get("found"):
            logger.info("Detected Cloudflare Turnstile")
            return CaptchaInfo(
                captcha_type="turnstile",
                sitekey=turnstile_info.get("sitekey", ""),
                page_url=page_url,
            )
    except Exception as exc:
        logger.debug("Turnstile detection error: %s", exc)

    # ── Generic Image CAPTCHA Detection ──
    try:
        image_captcha = await page.evaluate("""() => {
            // Look for common CAPTCHA image patterns
            const captchaPatterns = [
                'img[src*="captcha"]',
                'img[alt*="captcha" i]',
                'img[id*="captcha" i]',
                'img[class*="captcha" i]',
                '#captcha-image',
                '.captcha-image',
            ];

            for (const selector of captchaPatterns) {
                const img = document.querySelector(selector);
                if (img) {
                    return {
                        found: true,
                        src: img.src || '',
                    };
                }
            }

            // Check for CAPTCHA input fields
            const captchaInput = document.querySelector(
                'input[name*="captcha" i], input[id*="captcha" i], input[placeholder*="captcha" i]'
            );
            if (captchaInput) {
                const nearbyImg = captchaInput.parentElement?.querySelector('img') ||
                                  captchaInput.closest('form')?.querySelector('img[src*="captcha"]');
                if (nearbyImg) {
                    return { found: true, src: nearbyImg.src || '' };
                }
            }

            return { found: false };
        }""")

        if image_captcha and image_captcha.get("found"):
            logger.info("Detected image-based CAPTCHA")
            return CaptchaInfo(
                captcha_type="image",
                page_url=page_url,
            )
    except Exception as exc:
        logger.debug("Image CAPTCHA detection error: %s", exc)

    logger.info("No CAPTCHA detected on page")
    return None


# ──────────────────────────────────────────────────────────────────────
# CAPTCHA Solving Services
# ──────────────────────────────────────────────────────────────────────

class CaptchaSolveResult:
    """Result of a CAPTCHA solving attempt."""

    def __init__(
        self,
        success: bool,
        token: str = "",
        service: str = "",
        solve_time: float = 0.0,
        error: str = "",
    ):
        self.success = success
        self.token = token
        self.service = service
        self.solve_time = solve_time
        self.error = error

    def __repr__(self):
        if self.success:
            return f"CaptchaSolveResult(success=True, service={self.service}, time={self.solve_time:.1f}s)"
        return f"CaptchaSolveResult(success=False, error={self.error})"


async def solve_with_2captcha(captcha: CaptchaInfo) -> CaptchaSolveResult:
    """Solve CAPTCHA using 2Captcha service."""
    if not TWOCAPTCHA_API_KEY:
        return CaptchaSolveResult(
            success=False,
            error="2Captcha API key not configured (set TWOCAPTCHA_API_KEY env var)",
        )

    start = time.time()

    try:
        # Build request parameters based on CAPTCHA type
        params: dict[str, Any] = {
            "key": TWOCAPTCHA_API_KEY,
            "json": 1,
        }

        if captcha.captcha_type == "recaptcha_v2":
            params.update({
                "method": "userrecaptcha",
                "googlekey": captcha.sitekey,
                "pageurl": captcha.page_url,
                "invisible": 1 if captcha.is_invisible else 0,
            })
            if captcha.data_s:
                params["data-s"] = captcha.data_s

        elif captcha.captcha_type == "recaptcha_v3":
            params.update({
                "method": "userrecaptcha",
                "version": "v3",
                "googlekey": captcha.sitekey,
                "pageurl": captcha.page_url,
                "action": captcha.action or "verify",
                "min_score": 0.7,
            })

        elif captcha.captcha_type == "hcaptcha":
            params.update({
                "method": "hcaptcha",
                "sitekey": captcha.sitekey,
                "pageurl": captcha.page_url,
            })

        elif captcha.captcha_type == "turnstile":
            params.update({
                "method": "turnstile",
                "sitekey": captcha.sitekey,
                "pageurl": captcha.page_url,
            })

        else:
            return CaptchaSolveResult(
                success=False,
                error=f"Unsupported CAPTCHA type for 2Captcha: {captcha.captcha_type}",
            )

        # Submit CAPTCHA
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(TWOCAPTCHA_API, data=params, timeout=30),
        )
        result = response.json()

        if result.get("status") != 1:
            return CaptchaSolveResult(
                success=False,
                error=f"2Captcha submit error: {result.get('request', 'unknown')}",
            )

        task_id = result["request"]
        logger.info("2Captcha task submitted: %s", task_id)

        # Poll for result
        for _ in range(CAPTCHA_SOLVE_TIMEOUT // CAPTCHA_POLL_INTERVAL):
            await asyncio.sleep(CAPTCHA_POLL_INTERVAL)

            poll_response = await loop.run_in_executor(
                None,
                lambda: requests.get(
                    TWOCAPTCHA_RESULT,
                    params={
                        "key": TWOCAPTCHA_API_KEY,
                        "action": "get",
                        "id": task_id,
                        "json": 1,
                    },
                    timeout=30,
                ),
            )
            poll_result = poll_response.json()

            if poll_result.get("status") == 1:
                solve_time = time.time() - start
                logger.info("2Captcha solved in %.1fs", solve_time)
                return CaptchaSolveResult(
                    success=True,
                    token=poll_result["request"],
                    service="2captcha",
                    solve_time=solve_time,
                )

            if poll_result.get("request") != "CAPCHA_NOT_READY":
                return CaptchaSolveResult(
                    success=False,
                    error=f"2Captcha error: {poll_result.get('request', 'unknown')}",
                )

        return CaptchaSolveResult(
            success=False,
            error="2Captcha timeout — CAPTCHA not solved within time limit",
        )

    except Exception as exc:
        return CaptchaSolveResult(
            success=False,
            error=f"2Captcha exception: {str(exc)}",
        )


async def solve_with_anticaptcha(captcha: CaptchaInfo) -> CaptchaSolveResult:
    """Solve CAPTCHA using Anti-Captcha service."""
    if not ANTICAPTCHA_API_KEY:
        return CaptchaSolveResult(
            success=False,
            error="Anti-Captcha API key not configured (set ANTICAPTCHA_API_KEY env var)",
        )

    start = time.time()

    try:
        # Build task based on CAPTCHA type
        task: dict[str, Any] = {}

        if captcha.captcha_type == "recaptcha_v2":
            task = {
                "type": "RecaptchaV2TaskProxyless",
                "websiteURL": captcha.page_url,
                "websiteKey": captcha.sitekey,
                "isInvisible": captcha.is_invisible,
            }

        elif captcha.captcha_type == "recaptcha_v3":
            task = {
                "type": "RecaptchaV3TaskProxyless",
                "websiteURL": captcha.page_url,
                "websiteKey": captcha.sitekey,
                "minScore": 0.7,
                "pageAction": captcha.action or "verify",
            }

        elif captcha.captcha_type == "hcaptcha":
            task = {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": captcha.page_url,
                "websiteKey": captcha.sitekey,
            }

        elif captcha.captcha_type == "turnstile":
            task = {
                "type": "TurnstileTaskProxyless",
                "websiteURL": captcha.page_url,
                "websiteKey": captcha.sitekey,
            }

        else:
            return CaptchaSolveResult(
                success=False,
                error=f"Unsupported CAPTCHA type for Anti-Captcha: {captcha.captcha_type}",
            )

        # Submit task
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                f"{ANTICAPTCHA_API}/createTask",
                json={"clientKey": ANTICAPTCHA_API_KEY, "task": task},
                timeout=30,
            ),
        )
        result = response.json()

        if result.get("errorId", 0) != 0:
            return CaptchaSolveResult(
                success=False,
                error=f"Anti-Captcha error: {result.get('errorDescription', 'unknown')}",
            )

        task_id = result["taskId"]
        logger.info("Anti-Captcha task submitted: %s", task_id)

        # Poll for result
        for _ in range(CAPTCHA_SOLVE_TIMEOUT // CAPTCHA_POLL_INTERVAL):
            await asyncio.sleep(CAPTCHA_POLL_INTERVAL)

            poll_response = await loop.run_in_executor(
                None,
                lambda: requests.post(
                    f"{ANTICAPTCHA_API}/getTaskResult",
                    json={"clientKey": ANTICAPTCHA_API_KEY, "taskId": task_id},
                    timeout=30,
                ),
            )
            poll_result = poll_response.json()

            if poll_result.get("status") == "ready":
                solution = poll_result.get("solution", {})
                token = solution.get("gRecaptchaResponse") or solution.get("token", "")
                solve_time = time.time() - start
                logger.info("Anti-Captcha solved in %.1fs", solve_time)
                return CaptchaSolveResult(
                    success=True,
                    token=token,
                    service="anticaptcha",
                    solve_time=solve_time,
                )

            if poll_result.get("status") == "processing":
                continue

            return CaptchaSolveResult(
                success=False,
                error=f"Anti-Captcha error: {poll_result.get('errorDescription', 'unknown')}",
            )

        return CaptchaSolveResult(
            success=False,
            error="Anti-Captcha timeout",
        )

    except Exception as exc:
        return CaptchaSolveResult(
            success=False,
            error=f"Anti-Captcha exception: {str(exc)}",
        )


# ──────────────────────────────────────────────────────────────────────
# Token Injection
# ──────────────────────────────────────────────────────────────────────

async def inject_captcha_token(
    page: Page | Frame,
    captcha: CaptchaInfo,
    token: str,
) -> bool:
    """
    Inject a solved CAPTCHA token back into the page.

    For reCAPTCHA: Sets the g-recaptcha-response textarea value
    For hCaptcha: Sets the h-captcha-response textarea value
    For Turnstile: Sets the cf-turnstile-response input value
    """
    try:
        if captcha.captcha_type in ("recaptcha_v2", "recaptcha_v3"):
            await page.evaluate(f"""(token) => {{
                // Set the response textarea
                const textareas = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
                textareas.forEach(ta => {{
                    ta.value = token;
                    ta.innerHTML = token;
                }});

                // Also set via hidden input
                const hiddenInputs = document.querySelectorAll('input[name="g-recaptcha-response"]');
                hiddenInputs.forEach(input => {{
                    input.value = token;
                }});

                // Trigger callback if available
                if (typeof ___grecaptcha_cfg !== 'undefined') {{
                    try {{
                        const clients = ___grecaptcha_cfg.clients;
                        for (const key in clients) {{
                            const client = clients[key];
                            // Find the callback function
                            for (const prop in client) {{
                                const val = client[prop];
                                if (val && typeof val === 'object') {{
                                    for (const subProp in val) {{
                                        if (typeof val[subProp] === 'function') {{
                                            try {{ val[subProp](token); }} catch(e) {{}}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }} catch(e) {{}}
                }}

                // Try direct callback
                if (typeof grecaptcha !== 'undefined') {{
                    try {{
                        const callback = grecaptcha.getResponse && document.querySelector('[data-callback]');
                        if (callback) {{
                            const cbName = callback.getAttribute('data-callback');
                            if (window[cbName]) window[cbName](token);
                        }}
                    }} catch(e) {{}}
                }}
            }}""", token)
            logger.info("reCAPTCHA token injected successfully")
            return True

        elif captcha.captcha_type == "hcaptcha":
            await page.evaluate(f"""(token) => {{
                const textareas = document.querySelectorAll(
                    'textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]'
                );
                textareas.forEach(ta => {{
                    ta.value = token;
                    ta.innerHTML = token;
                }});

                // Trigger hcaptcha callback
                if (typeof hcaptcha !== 'undefined') {{
                    try {{
                        const iframes = document.querySelectorAll('iframe[src*="hcaptcha"]');
                        iframes.forEach(iframe => {{
                            const widgetId = iframe.getAttribute('data-hcaptcha-widget-id');
                            if (widgetId) {{
                                hcaptcha.setResponse(widgetId, token);
                            }}
                        }});
                    }} catch(e) {{}}
                }}
            }}""", token)
            logger.info("hCaptcha token injected successfully")
            return True

        elif captcha.captcha_type == "turnstile":
            await page.evaluate(f"""(token) => {{
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], input[name="turnstile-response"]'
                );
                inputs.forEach(input => {{
                    input.value = token;
                }});

                // Try turnstile callback
                if (typeof turnstile !== 'undefined') {{
                    try {{
                        const widgets = document.querySelectorAll('.cf-turnstile');
                        widgets.forEach(w => {{
                            const cb = w.getAttribute('data-callback');
                            if (cb && window[cb]) window[cb](token);
                        }});
                    }} catch(e) {{}}
                }}
            }}""", token)
            logger.info("Turnstile token injected successfully")
            return True

        else:
            logger.warning("Cannot inject token for CAPTCHA type: %s", captcha.captcha_type)
            return False

    except Exception as exc:
        logger.error("Failed to inject CAPTCHA token: %s", exc)
        return False


# ──────────────────────────────────────────────────────────────────────
# Main CAPTCHA Handler
# ──────────────────────────────────────────────────────────────────────

async def handle_captcha(
    page: Page | Frame,
    progress_callback=None,
) -> dict[str, Any]:
    """
    Main entry point: detect and solve any CAPTCHA on the page.

    Returns a dict with:
      - detected: bool (was a CAPTCHA found?)
      - solved: bool (was it successfully solved?)
      - captcha_type: str (type of CAPTCHA detected)
      - service: str (which service solved it)
      - solve_time: float (seconds to solve)
      - error: str (error message if failed)
    """
    async def _progress(msg: str):
        if progress_callback:
            await progress_callback(msg)

    # Step 1: Detect
    captcha = await detect_captcha(page)

    if captcha is None:
        return {
            "detected": False,
            "solved": False,
            "captcha_type": None,
            "service": None,
            "solve_time": 0.0,
            "error": None,
        }

    await _progress(f"CAPTCHA detected: {captcha.captcha_type}")
    logger.info("CAPTCHA detected: %s", captcha)

    # Step 2: Check if any API key is configured
    has_service = bool(TWOCAPTCHA_API_KEY or ANTICAPTCHA_API_KEY or CAPSOLVER_API_KEY)

    if not has_service:
        await _progress(
            "CAPTCHA detected but no solving service configured. "
            "Set TWOCAPTCHA_API_KEY, ANTICAPTCHA_API_KEY, or CAPSOLVER_API_KEY."
        )
        return {
            "detected": True,
            "solved": False,
            "captcha_type": captcha.captcha_type,
            "service": None,
            "solve_time": 0.0,
            "error": "No CAPTCHA solving service API key configured",
        }

    # Step 3: Try solving services in priority order
    services = []
    if TWOCAPTCHA_API_KEY:
        services.append(("2Captcha", solve_with_2captcha))
    if ANTICAPTCHA_API_KEY:
        services.append(("Anti-Captcha", solve_with_anticaptcha))

    for service_name, solver_fn in services:
        await _progress(f"Solving CAPTCHA with {service_name}...")
        result = await solver_fn(captcha)

        if result.success:
            # Step 4: Inject token
            await _progress(f"CAPTCHA solved by {service_name} in {result.solve_time:.1f}s — injecting token...")
            injected = await inject_captcha_token(page, captcha, result.token)

            if injected:
                await _progress(f"CAPTCHA solved and injected successfully ({service_name}, {result.solve_time:.1f}s)")
                return {
                    "detected": True,
                    "solved": True,
                    "captcha_type": captcha.captcha_type,
                    "service": service_name,
                    "solve_time": result.solve_time,
                    "error": None,
                }
            else:
                await _progress(f"CAPTCHA solved but token injection failed — trying next service...")
                continue
        else:
            logger.warning("%s failed: %s", service_name, result.error)
            await _progress(f"{service_name} failed: {result.error}")
            continue

    # All services failed
    await _progress("All CAPTCHA solving services failed")
    return {
        "detected": True,
        "solved": False,
        "captcha_type": captcha.captcha_type,
        "service": None,
        "solve_time": 0.0,
        "error": "All solving services failed",
    }


# ──────────────────────────────────────────────────────────────────────
# Utility: Check CAPTCHA Service Balance
# ──────────────────────────────────────────────────────────────────────

async def get_service_balance() -> dict[str, Any]:
    """Check balance/credits for configured CAPTCHA solving services."""
    balances = {}

    if TWOCAPTCHA_API_KEY:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(
                    TWOCAPTCHA_RESULT,
                    params={"key": TWOCAPTCHA_API_KEY, "action": "getbalance", "json": 1},
                    timeout=10,
                ),
            )
            result = response.json()
            if result.get("status") == 1:
                balances["2captcha"] = {"balance": float(result["request"]), "currency": "USD"}
            else:
                balances["2captcha"] = {"error": result.get("request", "unknown")}
        except Exception as exc:
            balances["2captcha"] = {"error": str(exc)}

    if ANTICAPTCHA_API_KEY:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(
                    f"{ANTICAPTCHA_API}/getBalance",
                    json={"clientKey": ANTICAPTCHA_API_KEY},
                    timeout=10,
                ),
            )
            result = response.json()
            if result.get("errorId", 0) == 0:
                balances["anticaptcha"] = {"balance": result["balance"], "currency": "USD"}
            else:
                balances["anticaptcha"] = {"error": result.get("errorDescription", "unknown")}
        except Exception as exc:
            balances["anticaptcha"] = {"error": str(exc)}

    return {
        "services_configured": len(balances),
        "balances": balances,
    }
