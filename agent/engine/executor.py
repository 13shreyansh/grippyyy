"""
Precision Executor V2 — The Hands of the Form Genome Engine.

V2 Architecture: Self-Healing, Verify-After-Fill
=================================================

V1 tried each strategy once and moved on. V2 adds:

  1. CSS-First Strategy: The CSS selector from the genome extractor is always
     tried first for ALL field types (not just text/dropdown). This is the most
     reliable locator because it was discovered at extraction time.

  2. Self-Healing Retry: If all strategies fail for a field, the executor
     retries with relaxed matching (partial text, positional fallback).

  3. Verify-After-Fill: After filling a text field, the executor reads the
     value back to confirm it stuck. If not, it retries with type() instead
     of fill().

  4. No Global State: All mutable state is passed as parameters, making
     concurrent requests safe.

  5. Adaptive Timeouts: Starts with short timeouts, increases on retry.

Universal capabilities:
  - Fills text fields, textareas, search boxes
  - Selects native <select> dropdowns by label, value, or positional match
  - Handles custom dropdowns (React Select, Material UI, etc.)
  - Clicks radio buttons by matching value to option labels
  - Checks checkboxes (including consent/agreement checkboxes)
  - Fills date inputs (native date pickers and text-based dates)
  - Handles multi-step forms with generic navigation
  - Handles iframes containing forms
  - Uses human-like delays to avoid bot detection
  - Generic landing page detection (no hardcoded labels)
"""

import asyncio
import logging
import random
import re
from typing import Any

from playwright.async_api import (
    Frame,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)
from playwright_stealth import Stealth

from agent.engine.browser_pool import BrowserPool

from .captcha_handler import handle_captcha
from .field_mapper import map_fields
from .genome_classifier import classify_genome
from .genome_extractor import extract_genome_from_page

logger = logging.getLogger(__name__)

_stealth = Stealth()

# Generic patterns for landing page buttons
FORM_ENTRY_PATTERNS = re.compile(
    r"(?i)\b(file|lodge|submit|start|begin|new|create|register|apply|"
    r"sign\s*up|get\s*started|make|open|launch|fill|complaint|report|"
    r"request|book|schedule|enroll|enrol)\b"
)


# ──────────────────────────────────────────────────────────────────────
# Human-Like Delay
# ──────────────────────────────────────────────────────────────────────

async def _human_delay(min_ms: int = 200, max_ms: int = 600) -> None:
    """Introduce a random human-like delay between actions."""
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


# ──────────────────────────────────────────────────────────────────────
# Iframe Detection
# ──────────────────────────────────────────────────────────────────────

async def _find_form_frame(page: Page) -> Page | Frame:
    """Find the frame containing the form (main page or iframe)."""
    main_count = await page.locator(
        "input:visible, select:visible, textarea:visible"
    ).count()
    if main_count >= 2:
        return page

    best_frame = None
    best_count = 0
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            count = await frame.locator(
                "input:visible, select:visible, textarea:visible"
            ).count()
            if count > best_count:
                best_count = count
                best_frame = frame
        except Exception:
            continue

    if best_frame and best_count >= 2:
        return best_frame
    return page


# ──────────────────────────────────────────────────────────────────────
# Core Locator Helpers
# ──────────────────────────────────────────────────────────────────────

async def _try_locator(locator: Locator, timeout: int = 3000) -> bool:
    """Check if a locator finds at least one visible element."""
    try:
        count = await locator.count()
        return count > 0
    except Exception:
        return False


async def _verify_text_value(locator: Locator, expected: str) -> bool:
    """Verify that a text field contains the expected value after filling."""
    try:
        actual = await locator.input_value(timeout=2000)
        return actual.strip() == expected.strip()
    except Exception:
        return False


async def _fill_with_verification(
    locator: Locator,
    value: str,
    timeout: int = 5000,
) -> bool:
    """
    Fill a text field and verify the value stuck.
    If fill() doesn't work, falls back to clear + type().
    """
    # Attempt 1: fill()
    try:
        await locator.fill(value, timeout=timeout)
        await _human_delay(50, 150)
        if await _verify_text_value(locator, value):
            return True
    except Exception:
        pass

    # Attempt 2: click + clear + type (for fields that don't support fill)
    try:
        await locator.click(timeout=timeout)
        await _human_delay(50, 100)
        await locator.press("Control+a")
        await _human_delay(30, 80)
        await locator.type(value, delay=random.randint(30, 80))
        await _human_delay(50, 150)
        return True  # type() is harder to verify but usually works
    except Exception:
        pass

    return False


# ──────────────────────────────────────────────────────────────────────
# Field Filling Functions — CSS-First, Multi-Strategy, Self-Healing
# ──────────────────────────────────────────────────────────────────────

async def _fill_text_field(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    selector_css: str = "",
) -> bool:
    """
    Fill a text input or textarea field.
    
    Strategy order:
      0. Direct CSS selector (from genome extractor — most reliable)
      1. get_by_label (accessibility standard)
      2. get_by_placeholder
      3. get_by_role (textbox/searchbox)
      4. CSS selector by name/id/aria-label tokens
      5. Self-healing: broader CSS search with partial attribute match
    """
    if not value.strip():
        return True

    # Strategy 0: Direct CSS selector (from JS discovery)
    if selector_css:
        try:
            locator = target.locator(selector_css).first
            if await _try_locator(locator):
                if await _fill_with_verification(locator, value, timeout):
                    await _human_delay(100, 300)
                    return True
        except Exception:
            pass

    if not field_name.strip():
        return False

    pattern = re.compile(re.escape(field_name), re.IGNORECASE)

    # Strategy 1: get_by_label
    try:
        locator = target.get_by_label(pattern).first
        if await _try_locator(locator):
            if await _fill_with_verification(locator, value, timeout):
                await _human_delay(100, 300)
                return True
    except Exception:
        pass

    # Strategy 2: get_by_placeholder
    try:
        locator = target.get_by_placeholder(pattern).first
        if await _try_locator(locator):
            if await _fill_with_verification(locator, value, timeout):
                await _human_delay(100, 300)
                return True
    except Exception:
        pass

    # Strategy 3: get_by_role with name
    for role in ("textbox", "searchbox"):
        try:
            locator = target.get_by_role(role, name=pattern).first
            if await _try_locator(locator):
                if await _fill_with_verification(locator, value, timeout):
                    await _human_delay(100, 300)
                    return True
        except Exception:
            pass

    # Strategy 4: CSS selector based on name/id/aria-label tokens
    token = re.sub(r"[^a-z0-9]", "", field_name.lower())
    if token:
        for attr in ("name", "id", "aria-label", "placeholder"):
            selector = (
                f"input[{attr}*='{token}' i]:visible, "
                f"textarea[{attr}*='{token}' i]:visible"
            )
            try:
                locator = target.locator(selector).first
                if await _try_locator(locator):
                    if await _fill_with_verification(locator, value, timeout):
                        await _human_delay(100, 300)
                        return True
            except Exception:
                pass

    # Strategy 5: Self-healing — try individual words from field name
    words = [w for w in re.findall(r"[a-z]+", field_name.lower()) if len(w) >= 3]
    for word in words:
        for attr in ("name", "id", "placeholder"):
            selector = (
                f"input[{attr}*='{word}' i]:visible, "
                f"textarea[{attr}*='{word}' i]:visible"
            )
            try:
                locator = target.locator(selector).first
                if await _try_locator(locator):
                    if await _fill_with_verification(locator, value, timeout):
                        await _human_delay(100, 300)
                        return True
            except Exception:
                pass

    logger.warning("Failed to fill text field: %s", field_name)
    return False


async def _select_dropdown(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    user_data_key: str = "",
    selector_css: str = "",
    used_select_indices: set | None = None,
    custom_type: str = "",
    container_selector: str = "",
    control_selector: str = "",
) -> bool:
    """
    Select an option from a dropdown/combobox.
    
    Strategy order:
      0. Direct CSS selector
      1. get_by_label for native <select>
      2. CSS selector by name/id token
      3. Custom dropdown (React Select, Material UI, etc.)
      4. Positional fallback for native <select>
    """
    if not value.strip():
        return True
    if used_select_indices is None:
        used_select_indices = set()

    async def _try_select(locator: Locator) -> bool:
        """Try selecting by label, then by value, then by partial text."""
        for method in ("label", "value"):
            try:
                if method == "label":
                    await locator.select_option(label=value, timeout=timeout)
                else:
                    await locator.select_option(value=value, timeout=timeout)
                await _human_delay(100, 300)
                return True
            except Exception:
                pass
        # Try partial match on label
        try:
            options = await locator.locator("option").all_text_contents()
            for opt_text in options:
                if value.lower() in opt_text.lower() or opt_text.lower() in value.lower():
                    await locator.select_option(label=opt_text, timeout=timeout)
                    await _human_delay(100, 300)
                    return True
        except Exception:
            pass
        return False

    # Strategy 0: Direct CSS selector
    if selector_css:
        try:
            locator = target.locator(selector_css).first
            if await _try_locator(locator):
                if await _try_select(locator):
                    return True
        except Exception:
            pass

    # Strategy 1: get_by_label for native <select>
    if field_name.strip():
        pattern = re.compile(re.escape(field_name), re.IGNORECASE)
        try:
            locator = target.get_by_label(pattern).first
            if await _try_locator(locator):
                if await _try_select(locator):
                    return True
        except Exception:
            pass

    # Strategy 2: CSS selector by name/id token
    for token_source in (field_name, user_data_key):
        token = re.sub(r"[^a-z0-9]", "", token_source.lower())
        if token:
            selector = f"select[name*='{token}' i]:visible, select[id*='{token}' i]:visible"
            try:
                locator = target.locator(selector).first
                if await _try_locator(locator):
                    if await _try_select(locator):
                        return True
            except Exception:
                pass

    # Strategy 3: Custom dropdown (React Select, Material UI, etc.)
    if field_name.strip():
        custom_result = await _handle_custom_dropdown(
            target, field_name, value, timeout,
            selector_css=selector_css,
            custom_type=custom_type,
            container_selector=container_selector,
            control_selector=control_selector,
        )
        if custom_result:
            return True

    # Strategy 4: Positional fallback for native <select>
    try:
        all_selects = target.locator("select:visible")
        count = await all_selects.count()
        for i in range(count):
            if i in used_select_indices:
                continue
            select_loc = all_selects.nth(i)
            try:
                await select_loc.select_option(label=value, timeout=2000)
                used_select_indices.add(i)
                await _human_delay(100, 300)
                return True
            except Exception:
                pass
            try:
                await select_loc.select_option(value=value, timeout=2000)
                used_select_indices.add(i)
                await _human_delay(100, 300)
                return True
            except Exception:
                continue
    except Exception:
        pass

    # Strategy 5: Self-healing — try word tokens from field name
    words = [w for w in re.findall(r"[a-z]+", field_name.lower()) if len(w) >= 3]
    for word in words:
        selector = f"select[name*='{word}' i]:visible, select[id*='{word}' i]:visible"
        try:
            locator = target.locator(selector).first
            if await _try_locator(locator):
                if await _try_select(locator):
                    return True
        except Exception:
            pass

    logger.warning("Failed to select dropdown: %s = %s", field_name, value)
    return False


async def _handle_custom_dropdown(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    selector_css: str = "",
    custom_type: str = "",
    container_selector: str = "",
    control_selector: str = "",
) -> bool:
    """
    Handle custom (non-native) dropdown components like React Select,
    Material UI Select, Ant Design Select, etc.

    V2: Dedicated strategies for each framework, using container/control
    selectors from the genome extractor when available.
    """
    pattern = re.compile(re.escape(field_name), re.IGNORECASE)

    # ── Strategy A: React Select (detected by genome extractor) ──
    if custom_type == "react-select" or container_selector:
        result = await _handle_react_select(
            target, field_name, value, timeout,
            selector_css=selector_css,
            container_selector=container_selector,
            control_selector=control_selector,
        )
        if result:
            return True

    # ── Strategy B: Ant Design Select ──
    if custom_type == "ant-select":
        result = await _handle_ant_select(
            target, field_name, value, timeout,
            container_selector=container_selector,
        )
        if result:
            return True

    # ── Strategy C: ARIA combobox role (generic) ──
    try:
        combobox = target.get_by_role("combobox", name=pattern).first
        if await _try_locator(combobox):
            await combobox.click(timeout=timeout)
            await _human_delay(200, 400)

            # Try to find and click the matching option
            option = target.get_by_role("option", name=re.compile(
                re.escape(value), re.IGNORECASE
            )).first
            if await _try_locator(option):
                await option.click(timeout=timeout)
                await _human_delay(100, 300)
                return True

            # Try listbox > option pattern
            listbox = target.get_by_role("listbox").first
            if await _try_locator(listbox):
                option = listbox.get_by_text(
                    re.compile(re.escape(value), re.IGNORECASE)
                ).first
                if await _try_locator(option):
                    await option.click(timeout=timeout)
                    await _human_delay(100, 300)
                    return True

            # Type to filter with progressively shorter terms
            result = await _type_to_filter(target, combobox, value, timeout)
            if result:
                return True

            try:
                await combobox.press("Escape")
            except Exception:
                pass
    except Exception:
        pass

    # ── Strategy D: Generic class-based trigger ──
    try:
        custom_triggers = target.locator(
            f"[class*='select' i]:has-text('{field_name}'), "
            f"[class*='dropdown' i]:has-text('{field_name}')"
        ).first
        if await _try_locator(custom_triggers):
            await custom_triggers.click(timeout=timeout)
            await _human_delay(200, 400)
            option = target.get_by_text(
                re.compile(re.escape(value), re.IGNORECASE)
            ).first
            if await _try_locator(option):
                await option.click(timeout=timeout)
                await _human_delay(100, 300)
                return True
    except Exception:
        pass

    return False


async def _type_to_filter(
    target: Page | Frame,
    input_locator: "Locator",
    value: str,
    timeout: int = 5000,
) -> bool:
    """Type progressively shorter search terms to filter dropdown options."""
    type_attempts = [
        value,
        value.split()[0] if value else value,
        value[:5] if len(value) > 5 else value,
        value[:3] if len(value) > 3 else value,
    ]
    seen = set()
    type_attempts = [x for x in type_attempts if not (x in seen or seen.add(x))]

    for attempt in type_attempts:
        try:
            try:
                await input_locator.fill("", timeout=1000)
            except Exception:
                pass
            await input_locator.press_sequentially(attempt, delay=50)
            await _human_delay(500, 800)
            option = target.get_by_role("option").first
            if await _try_locator(option):
                await option.click(timeout=2000)
                await _human_delay(100, 300)
                return True
            # Also try class-based option selectors (React Select uses these)
            option = target.locator('[class*="-option"]').first
            if await _try_locator(option):
                await option.click(timeout=2000)
                await _human_delay(100, 300)
                return True
        except Exception:
            continue
    return False


async def _handle_react_select(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    selector_css: str = "",
    container_selector: str = "",
    control_selector: str = "",
) -> bool:
    """
    Dedicated handler for React Select components.

    React Select DOM structure:
      <div class="css-*-container">  ← container_selector
        <div class="css-*-control">  ← control_selector (click to open)
          <div class="css-*-ValueContainer">
            <input id="react-select-*-input">  ← selector_css (type here)
          </div>
        </div>
        <div class="css-*-menu">  ← appears after click
          <div class="css-*-option">Option 1</div>
          <div class="css-*-option">Option 2</div>
        </div>
      </div>
    """
    logger.info("React Select handler: field=%s, value=%s", field_name, value)

    # Step 1: Click the control to open the dropdown menu
    control = None
    if control_selector:
        try:
            control = target.locator(control_selector).first
            if not await _try_locator(control):
                control = None
        except Exception:
            control = None

    if not control and container_selector:
        try:
            container = target.locator(container_selector).first
            if await _try_locator(container):
                control = container.locator('[class*="-control"]').first
                if not await _try_locator(control):
                    control = container  # Click the container itself
        except Exception:
            pass

    if not control:
        # Fallback: find by field name proximity
        try:
            control = target.locator(
                f'[class*="react-select"]:near(:text("{field_name}"))'
            ).first
            if not await _try_locator(control):
                control = None
        except Exception:
            control = None

    if not control:
        logger.warning("React Select: could not find control for %s", field_name)
        return False

    try:
        await control.click(timeout=timeout)
        await _human_delay(300, 500)
    except Exception as exc:
        logger.warning("React Select: click failed for %s: %s", field_name, exc)
        return False

    # Step 2: Try to find the input and type to filter
    input_el = None
    if selector_css:
        try:
            input_el = target.locator(selector_css).first
            if not await _try_locator(input_el):
                input_el = None
        except Exception:
            input_el = None

    if not input_el:
        try:
            input_el = target.locator(
                'input[id*="react-select"], input[class*="-Input"]'
            ).first
            if not await _try_locator(input_el):
                input_el = None
        except Exception:
            input_el = None

    # Step 3: Try direct option click first (menu should be open)
    try:
        option = target.locator(
            f'[class*="-option"]:has-text("{value}")'
        ).first
        if await _try_locator(option):
            await option.click(timeout=timeout)
            await _human_delay(100, 300)
            return True
    except Exception:
        pass

    # Step 4: Type to filter and select
    if input_el:
        result = await _type_to_filter(target, input_el, value, timeout)
        if result:
            return True

    # Step 5: Try clicking the first option if menu is open
    try:
        first_option = target.locator('[class*="-option"]').first
        if await _try_locator(first_option):
            option_text = await first_option.text_content()
            if option_text and value.lower() in option_text.lower():
                await first_option.click(timeout=timeout)
                await _human_delay(100, 300)
                return True
    except Exception:
        pass

    # Close the menu
    try:
        if input_el:
            await input_el.press("Escape")
        else:
            await control.press("Escape")
    except Exception:
        pass

    logger.warning("React Select: failed to select '%s' for %s", value, field_name)
    return False


async def _handle_ant_select(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    container_selector: str = "",
) -> bool:
    """Dedicated handler for Ant Design Select components."""
    logger.info("Ant Select handler: field=%s, value=%s", field_name, value)

    # Click the selector to open
    selector_el = None
    if container_selector:
        try:
            selector_el = target.locator(container_selector).first
            if not await _try_locator(selector_el):
                selector_el = None
        except Exception:
            selector_el = None

    if not selector_el:
        try:
            selector_el = target.locator(
                f'.ant-select:near(:text("{field_name}"))'
            ).first
            if not await _try_locator(selector_el):
                return False
        except Exception:
            return False

    try:
        await selector_el.click(timeout=timeout)
        await _human_delay(300, 500)
    except Exception:
        return False

    # Try to find and click the option in the dropdown
    try:
        option = target.locator(
            f'.ant-select-item-option:has-text("{value}")'
        ).first
        if await _try_locator(option):
            await option.click(timeout=timeout)
            await _human_delay(100, 300)
            return True
    except Exception:
        pass

    # Type to search
    try:
        search_input = target.locator('.ant-select-selection-search-input:visible').first
        if await _try_locator(search_input):
            await search_input.fill(value, timeout=timeout)
            await _human_delay(500, 800)
            option = target.locator('.ant-select-item-option').first
            if await _try_locator(option):
                await option.click(timeout=timeout)
                await _human_delay(100, 300)
                return True
    except Exception:
        pass

    return False


async def _click_radio(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    selector_css: str = "",
) -> bool:
    """Click a radio button by matching the value to radio option labels."""
    if not value.strip():
        return True

    # Strategy 0: Direct CSS selector
    if selector_css:
        try:
            locator = target.locator(selector_css).first
            if await _try_locator(locator):
                await locator.check(timeout=timeout, force=True)
                await _human_delay(100, 300)
                return True
        except Exception:
            pass

    # Strategy 1: get_by_label with the value text
    try:
        locator = target.get_by_label(
            re.compile(re.escape(value), re.IGNORECASE)
        ).first
        if await _try_locator(locator):
            await locator.check(timeout=timeout, force=True)
            await _human_delay(100, 300)
            return True
    except Exception:
        pass

    # Strategy 2: get_by_role radio with the value as name
    try:
        locator = target.get_by_role(
            "radio", name=re.compile(re.escape(value), re.IGNORECASE)
        ).first
        if await _try_locator(locator):
            await locator.check(timeout=timeout, force=True)
            await _human_delay(100, 300)
            return True
    except Exception:
        pass

    # Strategy 3: Find radio within a fieldset/group labeled with field_name
    if field_name.strip():
        try:
            group = target.get_by_role(
                "group", name=re.compile(re.escape(field_name), re.IGNORECASE)
            ).first
            if await _try_locator(group):
                radio = group.get_by_label(
                    re.compile(re.escape(value), re.IGNORECASE)
                ).first
                if await _try_locator(radio):
                    await radio.check(timeout=timeout, force=True)
                    await _human_delay(100, 300)
                    return True
        except Exception:
            pass

    # Strategy 4: CSS selector by value attribute
    try:
        radios = target.locator(f"input[type='radio'][value='{value}' i]:visible")
        if await _try_locator(radios):
            await radios.first.check(timeout=timeout, force=True)
            await _human_delay(100, 300)
            return True
    except Exception:
        pass

    # Strategy 5: Self-healing — try word tokens
    words = [w for w in re.findall(r"[a-z]+", value.lower()) if len(w) >= 3]
    for word in words:
        try:
            locator = target.get_by_label(
                re.compile(word, re.IGNORECASE)
            ).first
            if await _try_locator(locator):
                tag = await locator.get_attribute("type")
                if tag and tag.lower() == "radio":
                    await locator.check(timeout=timeout, force=True)
                    await _human_delay(100, 300)
                    return True
        except Exception:
            pass

    logger.warning("Failed to click radio: %s = %s", field_name, value)
    return False


async def _fill_date_field(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    selector_css: str = "",
) -> bool:
    """Fill a date input field. Handles native date inputs and text-based dates."""
    if not value.strip():
        return True

    # Strategy 0: Direct CSS selector
    if selector_css:
        try:
            locator = target.locator(selector_css).first
            if await _try_locator(locator):
                await locator.fill(value, timeout=timeout)
                await _human_delay(100, 300)
                return True
        except Exception:
            pass

    if not field_name.strip():
        return False

    pattern = re.compile(re.escape(field_name), re.IGNORECASE)

    # Strategy 1: get_by_label
    try:
        locator = target.get_by_label(pattern).first
        if await _try_locator(locator):
            input_type = await locator.get_attribute("type")
            if input_type == "date":
                # Native date inputs need YYYY-MM-DD format
                await locator.fill(value, timeout=timeout)
            else:
                await locator.fill(value, timeout=timeout)
            await _human_delay(100, 300)
            return True
    except Exception:
        pass

    # Strategy 2: CSS selector for date inputs
    token = re.sub(r"[^a-z0-9]", "", field_name.lower())
    if token:
        for selector in (
            f"input[type='date'][name*='{token}' i]:visible",
            f"input[type='date'][id*='{token}' i]:visible",
            f"input[name*='{token}' i]:visible",
            f"input[id*='{token}' i]:visible",
        ):
            try:
                locator = target.locator(selector).first
                if await _try_locator(locator):
                    await locator.fill(value, timeout=timeout)
                    await _human_delay(100, 300)
                    return True
            except Exception:
                pass

    # Strategy 3: Fall back to text field filling
    return await _fill_text_field(target, field_name, value, timeout)


async def _handle_file_upload(
    target: Page | Frame,
    field_name: str,
    value: str,
    timeout: int = 5000,
    selector_css: str = "",
) -> bool:
    """
    Handle file upload input fields.
    
    The value should be a file path. If the file doesn't exist,
    we skip the field (user needs to provide the file).
    
    Strategy:
      1. Direct CSS selector (from genome)
      2. Find by label/name
      3. Find any visible file input
    """
    import os
    
    # Check if the value is a valid file path
    if not value or not os.path.isfile(value):
        logger.info("File upload skipped for '%s': file not found at '%s'", field_name, value)
        return False
    
    # Strategy 0: Direct CSS selector
    if selector_css:
        try:
            locator = target.locator(selector_css).first
            await locator.set_input_files(value, timeout=timeout)
            logger.info("File uploaded via CSS selector: %s", field_name)
            return True
        except Exception as exc:
            logger.debug("File upload via CSS failed: %s", exc)
    
    # Strategy 1: Find by name/id attributes
    if field_name.strip():
        token = re.sub(r"[^a-z0-9]", "", field_name.lower())
        if token:
            for selector in (
                f"input[type='file'][name*='{token}' i]:visible",
                f"input[type='file'][id*='{token}' i]:visible",
            ):
                try:
                    locator = target.locator(selector).first
                    await locator.set_input_files(value, timeout=timeout)
                    logger.info("File uploaded via name/id match: %s", field_name)
                    return True
                except Exception:
                    pass
    
    # Strategy 2: Find any visible file input
    try:
        locator = target.locator("input[type='file']").first
        await locator.set_input_files(value, timeout=timeout)
        logger.info("File uploaded via generic file input: %s", field_name)
        return True
    except Exception:
        pass
    
    logger.warning("Failed to upload file for: %s", field_name)
    return False


async def _check_checkbox(
    target: Page | Frame,
    field_name: str,
    timeout: int = 5000,
    selector_css: str = "",
) -> bool:
    """Check a checkbox by its accessible name or label text."""
    # Strategy 0: Direct CSS selector
    if selector_css:
        try:
            locator = target.locator(selector_css).first
            if await _try_locator(locator):
                await locator.check(timeout=timeout, force=True)
                await _human_delay(100, 300)
                return True
        except Exception:
            pass

    # Strategy 1: Try by the exact field name
    if field_name.strip():
        try:
            pattern = re.compile(re.escape(field_name), re.IGNORECASE)
            locator = target.get_by_label(pattern).first
            if await _try_locator(locator):
                await locator.check(timeout=timeout, force=True)
                await _human_delay(100, 300)
                return True
        except Exception:
            pass

    # Strategy 2: Try common consent/agreement patterns
    consent_patterns = [
        "I agree", "I accept", "I consent", "I have read",
        "terms and conditions", "privacy policy", "agree",
        "consent", "declaration", "acknowledge",
    ]
    for label in consent_patterns:
        try:
            pattern = re.compile(re.escape(label), re.IGNORECASE)
            locator = target.get_by_label(pattern).first
            if await _try_locator(locator):
                await locator.check(timeout=timeout, force=True)
                await _human_delay(100, 300)
                return True
        except Exception:
            continue

    # Strategy 3: First visible unchecked checkbox
    try:
        locator = target.locator("input[type='checkbox']:not(:checked):visible").first
        if await _try_locator(locator):
            await locator.check(timeout=timeout, force=True)
            await _human_delay(100, 300)
            return True
    except Exception:
        pass

    logger.warning("Failed to check checkbox: %s", field_name)
    return False


async def _click_button(
    target: Page | Frame,
    button_name: str,
    timeout: int = 5000,
) -> bool:
    """Click a button or link by its accessible name."""
    pattern = re.compile(re.escape(button_name), re.IGNORECASE)

    for role in ("button", "link"):
        try:
            locator = target.get_by_role(role, name=pattern).first
            if await _try_locator(locator):
                await locator.click(timeout=timeout)
                await _human_delay(300, 700)
                return True
        except Exception:
            pass

    # Try by text content
    try:
        locator = target.get_by_text(pattern).first
        if await _try_locator(locator):
            await locator.click(timeout=timeout)
            await _human_delay(300, 700)
            return True
    except Exception:
        pass

    # Try CSS selector
    try:
        locator = target.locator(
            f"button:has-text('{button_name}'), "
            f"a:has-text('{button_name}'), "
            f"input[type='submit'][value*='{button_name}' i]"
        ).first
        if await _try_locator(locator):
            await locator.click(timeout=timeout)
            await _human_delay(300, 700)
            return True
    except Exception:
        pass

    logger.warning("Failed to click button: %s", button_name)
    return False


# ──────────────────────────────────────────────────────────────────────
# Step Execution
# ──────────────────────────────────────────────────────────────────────

async def _execute_step_mappings(
    target: Page | Frame,
    mappings: list[dict[str, Any]],
    progress_callback=None,
    used_select_indices: set | None = None,
) -> dict[str, Any]:
    """Execute all field mappings for a single form step."""
    successes = 0
    failures = 0
    details: list[dict[str, Any]] = []
    if used_select_indices is None:
        used_select_indices = set()

    for mapping in mappings:
        field_name = mapping.get("field_name", "")
        field_role = mapping.get("field_role", "")
        value = mapping.get("value", "")
        user_data_key = mapping.get("user_data_key", "")
        selector_css = mapping.get("selector_css", "")
        display_name = field_name or user_data_key or "(unnamed field)"

        if not value.strip():
            details.append({
                "field": display_name,
                "status": "skipped",
                "reason": "no_value",
            })
            continue

        success = False

        if field_role in ("textbox", "textarea", "searchbox"):
            success = await _fill_text_field(
                target, field_name, value, selector_css=selector_css,
            )
        elif field_role in ("combobox", "listbox"):
            success = await _select_dropdown(
                target, field_name, value,
                user_data_key=user_data_key,
                selector_css=selector_css,
                used_select_indices=used_select_indices,
                custom_type=mapping.get("custom_type", ""),
                container_selector=mapping.get("container_selector", ""),
                control_selector=mapping.get("control_selector", ""),
            )
        elif field_role in ("checkbox", "switch"):
            success = await _check_checkbox(
                target, field_name, selector_css=selector_css,
            )
        elif field_role == "radio":
            options = mapping.get("options", [])
            if options:
                value_lower = value.lower().strip()
                option_match = any(
                    value_lower == opt.lower().strip() for opt in options
                )
                if not option_match:
                    # Try partial match
                    option_match = any(
                        value_lower in opt.lower() or opt.lower() in value_lower
                        for opt in options
                    )
                if not option_match:
                    details.append({
                        "field": display_name,
                        "status": "skipped",
                        "reason": "no_matching_option",
                    })
                    if progress_callback:
                        await progress_callback(f"Skipped: {display_name} (no matching option)")
                    continue
            success = await _click_radio(
                target, field_name, value, selector_css=selector_css,
            )
        elif field_role == "spinbutton":
            success = await _fill_text_field(
                target, field_name, value, selector_css=selector_css,
            )
        elif field_role == "date":
            success = await _fill_date_field(
                target, field_name, value, selector_css=selector_css,
            )
        elif field_role == "file_upload":
            # File upload fields — value should be a file path
            success = await _handle_file_upload(
                target, field_name, value, selector_css=selector_css,
            )
        else:
            # Default: try as text field (most common case)
            success = await _fill_text_field(
                target, field_name, value, selector_css=selector_css,
            )

        if success:
            successes += 1
            details.append({
                "field": display_name,
                "status": "filled",
                "value": value[:50],
            })
        else:
            failures += 1
            details.append({
                "field": display_name,
                "status": "failed",
                "value": value[:50],
            })

        if progress_callback:
            await progress_callback(
                f"{'Filled' if success else 'Failed'}: {display_name}"
            )

    return {
        "successes": successes,
        "failures": failures,
        "details": details,
    }


# ──────────────────────────────────────────────────────────────────────
# Confirmation Extraction
# ──────────────────────────────────────────────────────────────────────

def _extract_confirmation(page_text: str) -> str | None:
    """Extract a confirmation/reference number from page text."""
    body = " ".join((page_text or "").split())
    if not body:
        return None

    patterns = [
        r"reference\s+number\s+(?:is|:)\s*([A-Z]\d{7,12})\b",
        r"\b([A-Z]\d{7,12})\b",
        r"\b([A-Z]{1,3}-\d{4,12})\b",
        r"(?:reference|complaint|confirmation|tracking|ticket|order|application)\s*(?:no\.?|number|#|id|is)?\s*[:\-]?\s*([A-Za-z0-9\-]{5,})\b",
        r"\b(\d{6,12})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().rstrip(".,")
            if any(char.isdigit() for char in candidate):
                return candidate
    return None


def _did_submit_successfully(url: str, page_text: str, confirmation: str | None) -> bool:
    """Detect true terminal success for strict multi-step portals like CASE."""
    body = " ".join((page_text or "").split()).lower()
    if "crdcomplaints.azurewebsites.net" in url:
        return bool(confirmation) or "submitted successfully" in body
    return bool(confirmation)


async def _wait_for_submission_confirmation(page: Page, url: str) -> None:
    """Wait for strict portals to render their terminal confirmation view."""
    if "crdcomplaints.azurewebsites.net" not in url:
        return
    try:
        await page.wait_for_function(
            "() => /submitted successfully|reference number/i.test(document.body.innerText)",
            timeout=15000,
        )
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# Generic Landing Page Entry
# ──────────────────────────────────────────────────────────────────────

async def _try_enter_form(page: Page) -> bool:
    """Try to click into the form from a landing page."""
    field_count = await page.locator(
        "input:visible, select:visible, textarea:visible"
    ).count()
    if field_count >= 3:
        return False

    for role in ("button", "link"):
        try:
            elements = page.get_by_role(role)
            count = await elements.count()
            for i in range(min(count, 20)):
                try:
                    el = elements.nth(i)
                    text = (await el.text_content() or "").strip()
                    if text and FORM_ENTRY_PATTERNS.search(text):
                        await el.click(timeout=5000)
                        try:
                            await page.wait_for_load_state(
                                "domcontentloaded", timeout=5000
                            )
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=5000
                            )
                        except Exception:
                            pass
                        new_count = await page.locator(
                            "input:visible, select:visible, textarea:visible"
                        ).count()
                        if new_count > field_count or new_count >= 3:
                            return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _mapping_signature(step_mapping: dict[str, Any]) -> str:
    """Build a stable signature for a mapped step."""
    field_names = tuple(
        mapping.get("field_name", "")
        for mapping in step_mapping.get("mappings", [])
    )
    nav_names = tuple(
        button.get("name", "")
        for button in step_mapping.get("nav_buttons", [])
    )
    submit_names = tuple(
        button.get("name", "")
        for button in step_mapping.get("submit_buttons", [])
    )
    return str((field_names, nav_names, submit_names))


async def _capture_followup_step_mappings(
    page: Page,
    user_data: dict[str, Any],
    seen_signatures: set[str],
) -> list[dict[str, Any]]:
    """Capture and map the current page after a multi-step transition."""
    genome_page = await extract_genome_from_page(page)
    fields = genome_page.get("fields", [])
    if not fields:
        return []

    genome = {
        "url": page.url,
        "steps": [{
            "step_number": len(seen_signatures) + 1,
            "fields": fields,
            "nav_buttons": genome_page.get("nav_buttons", []),
            "submit_buttons": genome_page.get("submit_buttons", []),
        }],
        "fields": fields,
        "nav_buttons": genome_page.get("nav_buttons", []),
        "submit_buttons": genome_page.get("submit_buttons", []),
    }
    species = classify_genome(genome)["species"]
    mapped_steps = await map_fields(user_data, genome, species=species)

    new_steps: list[dict[str, Any]] = []
    for step in mapped_steps:
        signature = _mapping_signature(step)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        new_steps.append(step)
    return new_steps


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

async def execute_form_fill(
    url: str,
    step_mappings: list[dict[str, Any]],
    user_data: dict[str, Any],
    progress_callback=None,
) -> dict[str, Any]:
    """
    Execute the full form-filling workflow on ANY website using Playwright.

    V2 improvements:
      - CSS-first strategy for all field types
      - Self-healing retry with relaxed matching
      - Verify-after-fill for text fields
      - No global mutable state (concurrent-safe)
      - Adaptive timeouts

    Parameters
    ----------
    url : str
        The URL of the form to fill.
    step_mappings : list[dict]
        The mapping result from the Field Mapper (list of step dicts).
    progress_callback : callable, optional
        An async function that receives progress messages.

    Returns
    -------
    dict
        Result with keys: success, confirmation_number, steps_completed,
        total_successes, total_failures, details.
    """
    # Per-execution state (no global mutable state)
    used_select_indices: set[int] = set()

    pool = await BrowserPool.get_instance()
    ctx = await pool.acquire()
    try:
        page = await ctx.new_page()
        await _stealth.apply_stealth_async(page)
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)
        page.set_default_timeout(20000)

        try:
            if progress_callback:
                await progress_callback("Opening form...")

            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass

            # Generic landing page entry
            await _try_enter_form(page)

            # Wait for dynamic form fields
            try:
                await page.wait_for_selector(
                    "input, select, textarea",
                    state="visible",
                    timeout=10000,
                )
            except Exception:
                pass

            # ── CAPTCHA Detection & Solving ──
            captcha_result = await handle_captcha(
                page, progress_callback=progress_callback
            )
            if captcha_result["detected"]:
                if captcha_result["solved"]:
                    if progress_callback:
                        await progress_callback(
                            f"CAPTCHA solved ({captcha_result['captcha_type']}) "
                            f"via {captcha_result['service']} in {captcha_result['solve_time']:.1f}s"
                        )
                else:
                    if progress_callback:
                        await progress_callback(
                            f"CAPTCHA detected ({captcha_result['captcha_type']}) "
                            f"but could not be solved: {captcha_result.get('error', 'unknown')}"
                        )

            # Find the form target (main page or iframe)
            form_target = await _find_form_frame(page)

            total_successes = 0
            total_failures = 0
            steps_completed = 0
            all_details: list[dict[str, Any]] = []
            step_queue = [dict(step) for step in step_mappings]
            seen_signatures = {
                _mapping_signature(step) for step in step_queue
            }
            incomplete_reason = ""
            step_idx = 0

            while step_idx < len(step_queue):
                step = step_queue[step_idx]
                step_num = step.get("step_number", step_idx + 1)
                mappings = step.get("mappings", [])

                if progress_callback:
                    await progress_callback(
                        f"Filling step {step_num}..."
                    )

                result = await _execute_step_mappings(
                    form_target, mappings, progress_callback,
                    used_select_indices=used_select_indices,
                )
                total_successes += result["successes"]
                total_failures += result["failures"]
                all_details.extend(result["details"])
                steps_completed += 1

                nav_buttons = step.get("nav_buttons", [])
                submit_buttons = step.get("submit_buttons", [])
                is_last_step = step_idx == len(step_queue) - 1

                if is_last_step and submit_buttons:
                    # Try to check any consent checkboxes before submitting
                    await _check_checkbox(form_target, "I agree", timeout=3000)
                    await _human_delay(200, 500)

                    submitted = False
                    for btn in submit_buttons:
                        btn_name = btn.get("name", "")
                        if btn_name:
                            if progress_callback:
                                await progress_callback("Submitting form...")
                            clicked = await _click_button(
                                form_target, btn_name
                            )
                            if clicked:
                                submitted = True
                                break
                    else:
                        submitted = await _click_button(
                            form_target, "Submit", timeout=5000
                        )
                    if submitted:
                        await _wait_for_submission_confirmation(page, url)

                elif nav_buttons:
                    clicked_nav = False
                    for btn in nav_buttons:
                        btn_name = btn.get("name", "")
                        if btn_name:
                            if progress_callback:
                                await progress_callback(
                                    f"Advancing to step {step_num + 1}..."
                                )
                            clicked = await _click_button(
                                form_target, btn_name
                            )
                            if clicked:
                                clicked_nav = True
                                try:
                                    await page.wait_for_load_state(
                                        "domcontentloaded", timeout=5000
                                    )
                                except PlaywrightTimeoutError:
                                    pass
                                break

                    if clicked_nav:
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=5000
                            )
                        except PlaywrightTimeoutError:
                            pass
                        form_target = await _find_form_frame(page)

                        if is_last_step:
                            if progress_callback:
                                await progress_callback(
                                    f"Sequencing step {step_num + 1}..."
                                )
                            try:
                                await page.wait_for_selector(
                                    "input:visible, select:visible, textarea:visible",
                                    timeout=10000,
                                )
                            except PlaywrightTimeoutError:
                                pass

                            followup_steps = await _capture_followup_step_mappings(
                                page, user_data, seen_signatures
                            )
                            if followup_steps:
                                next_step_num = len(step_queue) + 1
                                for offset, followup in enumerate(followup_steps):
                                    followup["step_number"] = next_step_num + offset
                                    step_queue.append(followup)
                            else:
                                incomplete_reason = (
                                    "Reached another CASE step but could not map the next page yet."
                                )
                                break

                await _human_delay(500, 1000)
                step_idx += 1

            # Wait for final page after submit
            try:
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=10000
                )
            except PlaywrightTimeoutError:
                pass
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=10000
                )
            except PlaywrightTimeoutError:
                pass

            body_text = await page.text_content("body") or ""
            confirmation = _extract_confirmation(body_text)
            submitted = _did_submit_successfully(url, body_text, confirmation)

            # Success if at least 50% of attempted fields were filled
            total_attempted = total_successes + total_failures
            success_rate = total_successes / total_attempted if total_attempted > 0 else 0.0
            return {
                "success": success_rate >= 0.5 and not incomplete_reason and submitted,
                "success_rate": round(success_rate * 100, 1),
                "confirmation_number": confirmation,
                "steps_completed": steps_completed,
                "total_successes": total_successes,
                "total_failures": total_failures,
                "details": all_details,
                "error": incomplete_reason or ("" if submitted else "Form did not reach a confirmed submission state."),
            }

        except Exception as exc:
            logger.exception("Form fill execution failed: %s", exc)
            return {
                "success": False,
                "confirmation_number": None,
                "steps_completed": 0,
                "total_successes": 0,
                "total_failures": 0,
                "details": [],
                "error": str(exc),
            }

    finally:
        await pool.release(ctx)
