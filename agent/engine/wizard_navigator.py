"""
Wizard Navigator — LLM-Powered Multi-Step Form Navigation.

This module solves the hardest problem in universal form filling:
navigating through multi-step wizard forms where:

  1. The first page is a category selection (buttons, not fields)
  2. Each step loads dynamically after the previous step
  3. The "next" action isn't always a button labeled "Next"

Architecture:
  - Scans the current page for ALL interactive elements (fields + buttons)
  - If the page has few/no form fields but has clickable elements,
    it asks the LLM: "Given the user's intent, which element should we
    interact with to proceed toward the form?"
  - Supports: clicking buttons, selecting dropdown options, filling
    prerequisite fields (like state selection)
  - Recursively navigates until it reaches a page with actual form fields

This is what makes Grippy work on CFPB, Indian government portals,
and any other wizard-style form on the internet.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional

from openai import OpenAI
from playwright.async_api import Page, Frame

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# LLM Client
# ──────────────────────────────────────────────────────────────────────

def _get_llm_client() -> OpenAI:
    """Get OpenAI-compatible client."""
    return OpenAI()  # Uses OPENAI_API_KEY and pre-configured base_url


def _llm_decide_action(
    page_context: dict[str, Any],
    user_intent: str,
    user_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Ask the LLM to decide what action to take on the current page
    to navigate toward the form.
    
    Returns a dict with:
      - action: "click_button", "select_option", "fill_field", "done", "stuck"
      - target_index: index into the elements list
      - value: value to fill/select (if applicable)
      - reasoning: why this action was chosen
    """
    client = _get_llm_client()
    
    # Build a concise page description
    fields_desc = []
    for i, f in enumerate(page_context.get("fields", [])):
        name = f.get("name", "unnamed")
        role = f.get("role", "unknown")
        options = f.get("options", [])
        opts_str = f" [options: {', '.join(options[:10])}]" if options else ""
        fields_desc.append(f"  [{i}] {role}: \"{name}\"{opts_str}")
    
    buttons_desc = []
    for i, b in enumerate(page_context.get("buttons", [])):
        name = b.get("name", "unnamed")
        role = b.get("role", "button")
        buttons_desc.append(f"  [{i}] {role}: \"{name}\"")
    
    nav_desc = []
    for i, b in enumerate(page_context.get("nav_buttons", [])):
        name = b.get("name", "unnamed")
        nav_desc.append(f"  [{i}] nav: \"{name}\"")
    
    submit_desc = []
    for i, b in enumerate(page_context.get("submit_buttons", [])):
        name = b.get("name", "unnamed")
        submit_desc.append(f"  [{i}] submit: \"{name}\"")
    
    # Build user data summary for context
    data_keys = list(user_data.keys()) if user_data else []
    
    # Build action history for context
    history = page_context.get("action_history", [])
    history_desc = "\n".join(f"  - {h}" for h in history) if history else "  (first step)"
    
    prompt = f"""You are a form navigation assistant. You are looking at a web page and need to decide what to do next to reach the actual form that needs to be filled.

PAGE TITLE: {page_context.get('title', 'Unknown')}
PAGE URL: {page_context.get('url', 'Unknown')}

FORM FIELDS ({len(page_context.get('fields', []))} found):
{chr(10).join(fields_desc) if fields_desc else '  (none)'}

BUTTONS ({len(page_context.get('buttons', []))} found):
{chr(10).join(buttons_desc[:30]) if buttons_desc else '  (none)'}

NAVIGATION BUTTONS:
{chr(10).join(nav_desc) if nav_desc else '  (none)'}

SUBMIT BUTTONS:
{chr(10).join(submit_desc) if submit_desc else '  (none)'}

PREVIOUS ACTIONS TAKEN:
{history_desc}

USER'S INTENT: {user_intent}
USER'S DATA KEYS: {', '.join(data_keys)}

TASK: Decide the SINGLE best action to take to navigate toward the form. 

If there are 3+ form fields visible, the form is already loaded — respond with "done".
If there are few/no fields but there are buttons or dropdowns, decide which one to interact with.

Respond in JSON format ONLY:
{{
  "action": "click_button" | "select_option" | "fill_field" | "done" | "stuck",
  "element_type": "button" | "nav_button" | "submit_button" | "field",
  "target_index": <index number>,
  "value": "<value to fill or option to select, if applicable>",
  "reasoning": "<brief explanation>"
}}

RULES:
- If the page has 3+ form fields, action MUST be "done"
- For category selection pages, pick the category most relevant to the user's intent
- For state/region selection, use the user's state/region from their data
- For dropdown fields with options, pick the best matching option
- NEVER go back to a page you already visited (check PREVIOUS ACTIONS)
- NEVER click "Change State", "Home", "Back" or similar navigation-away buttons
- If you see a "Continue", "Next", "Proceed" button, ALWAYS prefer it over other buttons
- On instruction/information pages, click "Continue" or "Next" to proceed
- If truly stuck with no way forward, use "stuck"
- ONLY respond with valid JSON, nothing else"""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        
        text = response.choices[0].message.content.strip()
        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
            text = text.group(1).strip() if text else "{}"
        
        return json.loads(text)
    except Exception as exc:
        logger.warning("LLM navigation decision failed: %s", exc)
        return {"action": "stuck", "reasoning": str(exc)}


# ──────────────────────────────────────────────────────────────────────
# Page Scanner
# ──────────────────────────────────────────────────────────────────────

_JS_SCAN_PAGE = """
() => {
    const fields = [];
    const buttons = [];
    const seenSelectors = new Set();
    
    function buildSelector(el) {
        if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
            return '#' + CSS.escape(el.id);
        }
        if (el.name) {
            const sel = el.tagName.toLowerCase() + '[name="' + el.name.replace(/"/g, '\\\\"') + '"]';
            if (document.querySelectorAll(sel).length === 1) return sel;
        }
        function pathTo(element) {
            if (element.id && document.querySelectorAll('#' + CSS.escape(element.id)).length === 1) {
                return '#' + CSS.escape(element.id);
            }
            if (element === document.body) return 'body';
            const parent = element.parentElement;
            if (!parent) return element.tagName.toLowerCase();
            const siblings = Array.from(parent.children).filter(c => c.tagName === element.tagName);
            const tag = element.tagName.toLowerCase();
            if (siblings.length === 1) return pathTo(parent) + ' > ' + tag;
            const idx = siblings.indexOf(element) + 1;
            return pathTo(parent) + ' > ' + tag + ':nth-of-type(' + idx + ')';
        }
        return pathTo(el);
    }
    
    // Check if an element or any ancestor is hidden
    function isEffectivelyVisible(el) {
        let current = el;
        while (current && current !== document.body) {
            try {
                const style = window.getComputedStyle(current);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            } catch(e) { return false; }
            current = current.parentElement;
        }
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return false;
        return true;
    }
    
    function findLabel(el) {
        let label = '';
        label = (el.getAttribute('aria-label') || '').trim();
        if (label) return label;
        if (el.id) {
            const labelEl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (labelEl) { label = labelEl.textContent.trim(); if (label) return label; }
        }
        label = (el.placeholder || '').trim();
        if (label) return label;
        label = (el.title || '').trim();
        if (label) return label;
        if (el.id) return el.id.replace(/([A-Z])/g, ' $1').replace(/[_\\-\\.]/g, ' ').trim();
        if (el.name) return el.name.replace(/([A-Z])/g, ' $1').replace(/[_\\-\\.]/g, ' ').trim();
        return '';
    }
    
    // Scan form fields — check effective visibility (including ancestor chain)
    document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]), select, textarea').forEach(el => {
        if (!isEffectivelyVisible(el)) return;
        
        const selector = buildSelector(el);
        if (seenSelectors.has(selector)) return;
        seenSelectors.add(selector);
        
        const options = [];
        if (el.tagName === 'SELECT') {
            el.querySelectorAll('option').forEach(opt => {
                const text = opt.textContent.trim();
                if (text && opt.value) options.push(text);
            });
        }
        
        fields.push({
            role: el.tagName === 'SELECT' ? 'combobox' : (el.type === 'checkbox' ? 'checkbox' : (el.type === 'radio' ? 'radio' : 'textbox')),
            name: findLabel(el),
            options: options,
            selector_css: selector,
            input_type: el.type || el.tagName.toLowerCase(),
        });
    });
    
    // Scan buttons and clickable elements — check effective visibility
    document.querySelectorAll('button, a[href], input[type="submit"], input[type="button"], [role="button"], [role="link"], [role="tab"], [role="menuitem"]').forEach(el => {
        if (!isEffectivelyVisible(el)) return;
        
        const text = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
        if (!text || text.length > 100) return;
        
        const selector = buildSelector(el);
        if (seenSelectors.has(selector)) return;
        seenSelectors.add(selector);
        
        buttons.push({
            role: el.getAttribute('role') || (el.tagName === 'A' ? 'link' : 'button'),
            name: text,
            selector_css: selector,
        });
    });
    
    return { fields, buttons, title: document.title || '' };
}
"""


async def _scan_current_page(page_or_frame: Page | Frame) -> dict[str, Any]:
    """Scan the current page for all interactive elements."""
    try:
        raw = await page_or_frame.evaluate(_JS_SCAN_PAGE)
        logger.info(
            "Page scan result: %d fields, %d buttons, title='%s', url='%s'",
            len(raw.get("fields", [])),
            len(raw.get("buttons", [])),
            raw.get("title", "")[:50],
            getattr(page_or_frame, 'url', 'frame'),
        )
        for f in raw.get("fields", []):
            logger.debug("  Field: %s (%s) [%s]", f.get('name'), f.get('role'), f.get('selector_css'))
        for b in raw.get("buttons", [])[:5]:
            logger.debug("  Button: %s [%s]", b.get('name', '')[:40], b.get('selector_css'))
        return raw
    except Exception as exc:
        logger.warning("Page scan failed: %s", exc)
        return {"fields": [], "buttons": [], "title": ""}


# ──────────────────────────────────────────────────────────────────────
# Wizard Navigator
# ──────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────
# Modal / Overlay Dismissal
# ──────────────────────────────────────────────────────────────────────

_JS_DISMISS_MODALS = """
() => {
    let dismissed = 0;
    
    // Safety check: don't hide elements that ARE the main page content.
    // If an element has many interactive children (links, buttons), it's
    // likely a page wrapper, not a modal overlay.
    function isSafeToHide(el) {
        // Never hide body or html
        if (el === document.body || el === document.documentElement) return false;
        // Never hide elements that contain the majority of page content
        const links = el.querySelectorAll('a[href], button, [role="button"]');
        if (links.length > 20) return false;  // Too many interactive elements = main content
        // Never hide elements that are very large (likely page wrappers)
        const rect = el.getBoundingClientRect();
        const viewW = window.innerWidth;
        const viewH = window.innerHeight;
        if (rect.width >= viewW * 0.9 && rect.height >= viewH * 0.9 && links.length > 5) return false;
        return true;
    }
    
    // Strategy 1: Click close buttons on visible modals
    const closeSelectors = [
        '.modal .close', '.modal .btn-close', '.modal [aria-label="Close"]',
        '.modal-close', '.popup-close', '.overlay-close',
        '[class*="modal"] button[class*="close"]',
        '[class*="modal"] [class*="dismiss"]',
        '[class*="popup"] button[class*="close"]',
        '[class*="cookie"] button', '[class*="consent"] button',
        '[class*="banner"] button[class*="close"]',
        '[class*="banner"] button[class*="accept"]',
        '.modal .btn-secondary', '.modal .btn-default',
    ];
    
    for (const sel of closeSelectors) {
        try {
            const btns = document.querySelectorAll(sel);
            for (const btn of btns) {
                const style = window.getComputedStyle(btn);
                if (style.display !== 'none' && style.visibility !== 'hidden') {
                    btn.click();
                    dismissed++;
                }
            }
        } catch(e) {}
    }
    
    // Strategy 2: Hide visible modal overlays directly
    // IMPORTANT: Only hide actual modals/overlays, NOT page content wrappers.
    // Some sites (e.g., Sarathi) add 'modal-open' class to body/wrapper,
    // which would match [class*="modal"][class*="open"] and hide the entire page.
    const modalSelectors = [
        '.modal.show', '.modal.in', '.modal[style*="display: block"]',
        '[class*="modal"][class*="show"]',
        '.modal-backdrop', '.modal-overlay', '.overlay.show',
        '[class*="popup"][class*="show"]', '[class*="popup"][class*="open"]',
    ];
    // NOTE: Removed '[class*="modal"][class*="open"]' — it matches body.modal-open
    // which is a Bootstrap class added to <body> when a modal is open, NOT a modal itself.
    
    for (const sel of modalSelectors) {
        try {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (!isSafeToHide(el)) continue;  // Skip page content wrappers
                el.style.display = 'none';
                dismissed++;
            }
        } catch(e) {}
    }
    
    // Strategy 3: Remove any element blocking pointer events
    // (common in government portals with overlay divs)
    try {
        const body = document.body;
        const bodyRect = body.getBoundingClientRect();
        const centerX = bodyRect.width / 2;
        const centerY = bodyRect.height / 2;
        const topEl = document.elementFromPoint(centerX, centerY);
        if (topEl && isSafeToHide(topEl)) {
            const role = topEl.getAttribute('role');
            const ariaModal = topEl.getAttribute('aria-modal');
            if (role === 'dialog' || ariaModal === 'true') {
                topEl.style.display = 'none';
                dismissed++;
            }
        }
    } catch(e) {}
    
    // Strategy 4: Reset body overflow (some modals lock scrolling)
    try {
        document.body.style.overflow = '';
        document.body.style.paddingRight = '';
        document.body.classList.remove('modal-open');
    } catch(e) {}
    
    return dismissed;
}
"""


async def _dismiss_modals(page: Page) -> int:
    """Dismiss any modal dialogs, popups, cookie banners, or overlays.
    
    Government portals (especially Indian ones) love popup modals on page load.
    This function tries multiple strategies to clear them.
    
    Uses Playwright's native click (not JS click) for close buttons because
    Bootstrap modals require real DOM events to trigger hide animations.
    
    Returns the number of elements dismissed.
    """
    dismissed = 0
    
    # Strategy 0: Use Playwright to click visible close buttons (most reliable)
    close_selectors = [
        '.modal .close', '.modal .btn-close', '.modal [aria-label="Close"]',
        '.modal-close', '.popup-close', '[role="dialog"] .close',
        '[role="dialog"] .btn-close', '[role="dialog"] button[class*="close"]',
        '[class*="modal"] button[class*="close"]',
        '[class*="cookie"] button[class*="accept"]',
        '[class*="consent"] button[class*="accept"]',
    ]
    for sel in close_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click(timeout=2000)
                dismissed += 1
                await asyncio.sleep(0.8)  # Wait for modal animation
                logger.info("Dismissed modal via Playwright click: %s", sel)
                break  # One close is usually enough
        except Exception:
            continue
    
    # Strategy 1: Press Escape key (works for many modal implementations)
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
    except Exception:
        pass
    
    # Strategy 2: JS-based dismissal for anything remaining
    try:
        js_dismissed = await page.evaluate(_JS_DISMISS_MODALS)
        if js_dismissed > 0:
            dismissed += js_dismissed
            logger.info("JS dismissed %d modal/overlay elements", js_dismissed)
            await asyncio.sleep(0.5)
    except Exception as exc:
        logger.debug("JS modal dismissal failed: %s", exc)
    
    if dismissed > 0:
        logger.info("Total dismissed: %d modal/overlay elements", dismissed)
    
    return dismissed


def _page_fingerprint(scan: dict[str, Any]) -> str:
    """
    Create a content fingerprint from a page scan.
    Used for SPA loop detection — same URL but different content = different step.
    Same URL AND same content = loop.
    """
    field_names = sorted(f.get("name", "") for f in scan.get("fields", []))
    button_names = sorted(b.get("name", "")[:30] for b in scan.get("buttons", [])[:20])
    content = f"{len(field_names)}|{'|'.join(field_names[:10])}|{len(button_names)}|{'|'.join(button_names[:10])}"
    return content


async def _wait_for_spa_transition(
    page: Page,
    max_wait: float = 8.0,
    poll_interval: float = 0.5,
) -> bool:
    """
    Wait for a SPA (Single Page Application) step transition.
    
    SPAs don't trigger navigation events — they just re-render DOM components.
    This function polls for DOM changes by watching:
      1. Number of visible form fields
      2. Number of visible buttons
      3. Text content hash of the main content area
    
    Returns True if a transition was detected, False if timeout.
    """
    # Take initial snapshot
    try:
        initial_state = await page.evaluate("""
            () => {
                const fields = document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="submit"]), select, textarea'
                );
                const buttons = document.querySelectorAll('button, [role="button"]');
                const main = document.querySelector('main, [role="main"], .main-content, #content, #app, #root');
                const text = main ? main.textContent.trim().substring(0, 500) : document.body.textContent.trim().substring(0, 500);
                return {
                    fieldCount: fields.length,
                    buttonCount: buttons.length,
                    textHash: text.length,
                };
            }
        """)
    except Exception:
        await asyncio.sleep(3)
        return False
    
    # Poll for changes
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        
        try:
            current_state = await page.evaluate("""
                () => {
                    const fields = document.querySelectorAll(
                        'input:not([type="hidden"]):not([type="submit"]), select, textarea'
                    );
                    const buttons = document.querySelectorAll('button, [role="button"]');
                    const main = document.querySelector('main, [role="main"], .main-content, #content, #app, #root');
                    const text = main ? main.textContent.trim().substring(0, 500) : document.body.textContent.trim().substring(0, 500);
                    return {
                        fieldCount: fields.length,
                        buttonCount: buttons.length,
                        textHash: text.length,
                    };
                }
            """)
            
            # Check if anything changed
            if (current_state["fieldCount"] != initial_state["fieldCount"] or
                current_state["buttonCount"] != initial_state["buttonCount"] or
                abs(current_state["textHash"] - initial_state["textHash"]) > 50):
                logger.info(
                    "SPA transition detected: fields %d->%d, buttons %d->%d, text %d->%d",
                    initial_state["fieldCount"], current_state["fieldCount"],
                    initial_state["buttonCount"], current_state["buttonCount"],
                    initial_state["textHash"], current_state["textHash"],
                )
                # Wait a bit more for the transition to fully complete
                await asyncio.sleep(1.5)
                return True
        except Exception:
            pass
    
    logger.info("SPA transition timeout (%.1fs) — no DOM changes detected", max_wait)
    return False


async def navigate_wizard(
    page: Page,
    user_intent: str,
    user_data: dict[str, Any],
    max_steps: int = 8,
    min_fields_for_form: int = 2,
) -> dict[str, Any]:
    """
    Navigate through a multi-step wizard until we reach the actual form.
    
    This is the key innovation that makes Grippy work on:
    - CFPB (category selection -> form)
    - Indian DL (state selection -> form)
    - US DS-160 (security questions -> form)
    - Any wizard-style form on the internet
    
    Parameters
    ----------
    page : Page
        The Playwright page to navigate.
    user_intent : str
        What the user wants to do (e.g., "file a credit card complaint").
    user_data : dict
        The user's data for context (e.g., state, country).
    max_steps : int
        Maximum navigation steps before giving up.
    min_fields_for_form : int
        Minimum number of form fields to consider the form "reached".
    
    Returns
    -------
    dict with:
        - reached_form: bool
        - steps_taken: int
        - actions_log: list of actions taken
        - final_url: str
    """
    actions_log = []
    visited_urls = []  # Track visited URLs to detect loops (list for counting)
    visited_fingerprints = []  # Track content fingerprints for SPA loop detection
    action_history = []   # Track actions for LLM context (prevent repeats)
    
    # First, dismiss any modal overlays that might block interaction
    dismissed = await _dismiss_modals(page)
    if dismissed > 0:
        actions_log.append({
            "step": 0,
            "action": "dismiss_modals",
            "target": f"{dismissed} modals/overlays",
            "reasoning": "Clearing blocking overlays before navigation",
        })
    
    for step in range(max_steps):
        # Dismiss any modals/overlays at EACH step (new modals appear after navigation)
        modal_count = await _dismiss_modals(page)
        if modal_count > 0:
            await asyncio.sleep(1)  # Extra wait after modal dismissal
            actions_log.append({
                "step": step + 1,
                "action": "dismiss_modals",
                "target": f"{modal_count} modals/overlays",
                "reasoning": "Clearing blocking overlays before scanning",
            })
        
        # Scan the current page
        logger.debug("Wizard step %d: scanning page %s", step + 1, page.url)
        scan = await _scan_current_page(page)
        scan["url"] = page.url
        
        field_count = len(scan.get("fields", []))
        button_count = len(scan.get("buttons", []))
        
        # If scan found nothing, wait longer and retry once (JS-heavy pages)
        if field_count == 0 and button_count == 0:
            logger.info("Wizard step %d: 0 elements found, retrying after wait...", step + 1)
            await asyncio.sleep(5)  # Wait for slow JS rendering
            await _dismiss_modals(page)  # Try dismissing again
            await asyncio.sleep(1)
            scan = await _scan_current_page(page)
            scan["url"] = page.url
            field_count = len(scan.get("fields", []))
            button_count = len(scan.get("buttons", []))
            logger.info("Wizard step %d (retry): %d fields, %d buttons", step + 1, field_count, button_count)
        
        logger.info(
            "Wizard step %d: %d fields, %d buttons on %s",
            step + 1, field_count, button_count, page.url,
        )
        
        # If we have enough form fields, we've reached the form
        if field_count >= min_fields_for_form:
            logger.info(
                "Form reached after %d wizard steps (%d fields found)",
                step, field_count,
            )
            return {
                "reached_form": True,
                "steps_taken": step,
                "actions_log": actions_log,
                "final_url": page.url,
            }
        
        # If no fields AND no buttons after retry, we're stuck
        if field_count == 0 and button_count == 0:
            logger.warning("Wizard stuck: 0 fields, 0 buttons on %s", page.url)
            return {
                "reached_form": False,
                "steps_taken": step,
                "actions_log": actions_log,
                "final_url": page.url,
                "error": "No interactive elements found",
            }
        
        # Classify buttons into nav/submit/other for the LLM
        nav_buttons = []
        submit_buttons = []
        other_buttons = []
        
        nav_pattern = re.compile(
            r"(?i)\b(next|continue|proceed|forward|go|step\s*\d+)\b"
        )
        submit_pattern = re.compile(
            r"(?i)\b(submit|file|lodge|send|confirm|finish|complete|done|save|register|sign\s*up|create|apply)\b"
        )
        
        for b in scan.get("buttons", []):
            name = b.get("name", "")
            if nav_pattern.search(name):
                nav_buttons.append(b)
            elif submit_pattern.search(name):
                submit_buttons.append(b)
            else:
                other_buttons.append(b)
        
        # Track visited URLs AND content fingerprints for loop detection
        # SPAs may have the same URL but different content (different step)
        current_url = page.url
        current_fingerprint = _page_fingerprint(scan)
        url_visit_count = visited_urls.count(current_url)
        fingerprint_visit_count = visited_fingerprints.count(current_fingerprint)
        visited_urls.append(current_url)
        visited_fingerprints.append(current_fingerprint)
        
        # SPA loop: same URL AND same content fingerprint = true loop
        # Same URL but different fingerprint = SPA step transition (OK)
        if fingerprint_visit_count >= 2:
            logger.warning(
                "Wizard loop detected: same content seen %d times on %s",
                fingerprint_visit_count + 1, current_url,
            )
        elif url_visit_count >= 3:
            logger.warning("Wizard URL loop detected: visited %s %d times", current_url, url_visit_count + 1)
        
        page_context = {
            "title": scan.get("title", ""),
            "url": page.url,
            "fields": scan.get("fields", []),
            "buttons": other_buttons,
            "nav_buttons": nav_buttons,
            "submit_buttons": submit_buttons,
            "action_history": action_history[-5:],  # Last 5 actions for context
        }
        
        # Ask the LLM what to do
        decision = _llm_decide_action(page_context, user_intent, user_data)
        action = decision.get("action", "stuck")
        reasoning = decision.get("reasoning", "")
        
        logger.info(
            "Wizard LLM decision: %s (reason: %s)",
            action, reasoning,
        )
        
        if action == "done":
            return {
                "reached_form": True,
                "steps_taken": step,
                "actions_log": actions_log,
                "final_url": page.url,
            }
        
        if action == "stuck":
            return {
                "reached_form": False,
                "steps_taken": step,
                "actions_log": actions_log,
                "final_url": page.url,
                "error": f"LLM stuck: {reasoning}",
            }
        
        # Execute the decided action
        try:
            element_type = decision.get("element_type", "button")
            target_index = decision.get("target_index", 0)
            value = decision.get("value", "")
            
            if action == "click_button":
                # Determine which button list to use
                if element_type == "nav_button":
                    btn_list = nav_buttons
                elif element_type == "submit_button":
                    btn_list = submit_buttons
                else:
                    btn_list = other_buttons
                
                if target_index < len(btn_list):
                    btn = btn_list[target_index]
                    css = btn.get("selector_css", "")
                    btn_name = btn.get("name", "")
                    
                    logger.info(
                        "Wizard clicking button: '%s' (css: %s)",
                        btn_name, css[:60],
                    )
                    
                    if css:
                        await page.locator(css).first.click(timeout=8000)
                    else:
                        await page.get_by_text(btn_name, exact=False).first.click(timeout=8000)
                    
                    actions_log.append({
                        "step": step + 1,
                        "action": "click_button",
                        "target": btn_name,
                        "reasoning": reasoning,
                    })
                    action_history.append(f"Clicked '{btn_name}' on {page.url.split('/')[-1]}")
                else:
                    logger.warning(
                        "Button index %d out of range (list has %d)",
                        target_index, len(btn_list),
                    )
                    # Try clicking by text from the decision
                    if value:
                        await page.get_by_text(value, exact=False).first.click(timeout=8000)
                        actions_log.append({
                            "step": step + 1,
                            "action": "click_button",
                            "target": value,
                            "reasoning": reasoning,
                        })
                        action_history.append(f"Clicked '{value}' on {page.url.split('/')[-1]}")
            
            elif action == "select_option":
                fields = scan.get("fields", [])
                if target_index < len(fields):
                    field = fields[target_index]
                    css = field.get("selector_css", "")
                    
                    logger.info(
                        "Wizard selecting option: '%s' in field '%s'",
                        value, field.get("name", ""),
                    )
                    
                    if css:
                        await page.locator(css).select_option(label=value, timeout=5000)
                    
                    actions_log.append({
                        "step": step + 1,
                        "action": "select_option",
                        "target": field.get("name", ""),
                        "value": value,
                        "reasoning": reasoning,
                    })
                    action_history.append(f"Selected '{value}' in '{field.get('name', '')}' on {page.url.split('/')[-1]}")
            
            elif action == "fill_field":
                fields = scan.get("fields", [])
                if target_index < len(fields):
                    field = fields[target_index]
                    css = field.get("selector_css", "")
                    
                    logger.info(
                        "Wizard filling field: '%s' with '%s'",
                        field.get("name", ""), value,
                    )
                    
                    if css:
                        await page.locator(css).first.fill(value, timeout=5000)
                    
                    actions_log.append({
                        "step": step + 1,
                        "action": "fill_field",
                        "target": field.get("name", ""),
                        "value": value,
                        "reasoning": reasoning,
                    })
                    action_history.append(f"Filled '{field.get('name', '')}' with '{value}' on {page.url.split('/')[-1]}")
            
            # Wait for page to update after action — SPA-aware
            # For traditional pages: wait for navigation
            # For SPAs: wait for DOM mutations (React/Angular step transitions)
            pre_action_url = page.url
            
            try:
                # First try traditional navigation wait (short timeout)
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            
            # SPA detection: if URL didn't change, wait for DOM mutations
            if page.url == pre_action_url:
                logger.info("SPA detected: URL unchanged after action, waiting for DOM update")
                # Wait for DOM to settle — SPAs re-render components
                await _wait_for_spa_transition(page)
            else:
                # Traditional navigation — wait for full load
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(2)
            
            # Wait for any visible interactive elements to appear
            try:
                await page.wait_for_selector(
                    "input:visible, select:visible, textarea:visible, button:visible",
                    timeout=10000,
                )
            except Exception:
                pass
            
            await asyncio.sleep(1)
            
            # Dismiss any new modals that appeared after the action
            await _dismiss_modals(page)
            
        except Exception as exc:
            logger.warning(
                "Wizard action failed at step %d: %s", step + 1, exc,
            )
            actions_log.append({
                "step": step + 1,
                "action": action,
                "error": str(exc),
                "reasoning": reasoning,
            })
            # Don't give up immediately — try the next iteration
            continue
    
    # Exhausted max steps
    return {
        "reached_form": False,
        "steps_taken": max_steps,
        "actions_log": actions_log,
        "final_url": page.url,
        "error": f"Exhausted {max_steps} navigation steps without reaching form",
    }
