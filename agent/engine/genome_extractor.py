"""
Genome Extractor — The Foundation of the Form Genome Engine.

V2 Architecture: JS-First Single-Pass Extraction
=================================================

The V1 extractor used a fragile 3-layer merge strategy:
  ARIA snapshot → accessibility.snapshot() → JS DOM query
This broke on forms without proper ARIA labels because the merge
logic couldn't reliably match fields across layers.

V2 inverts the architecture:
  1. PRIMARY: JavaScript DOM injection (works on ANY HTML structure)
  2. ENRICHMENT: ARIA snapshot (adds semantic roles when available)

The JS injection is a single comprehensive pass that:
  - Finds ALL visible form elements (input, select, textarea, contenteditable)
  - Tries 12 label sources in priority order for EACH element
  - Builds unique CSS selectors for EACH element
  - Extracts dropdown options, radio groups, checkbox states
  - Handles shadow DOM (1 level deep)
  - Handles table layouts, div layouts, flex layouts, grid layouts
  - Returns a clean, deduplicated field list

This approach is UNIVERSAL because it operates directly on the DOM,
not on an abstraction layer (ARIA tree) that may be incomplete.

Universal capabilities:
  - Works on any website with any form
  - Handles iframes (detects and switches into form-containing iframes)
  - Handles dynamic/SPA forms (waits for JS-rendered fields)
  - Extracts dropdown options for intelligent mapping
  - Generic landing page detection (no hardcoded button labels)
  - Multi-step form support with duplicate detection
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Page, Frame
from playwright_stealth import Stealth

from agent.engine.browser_pool import BrowserPool

_stealth = Stealth()

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

INTERACTIVE_ROLES = frozenset({
    "textbox", "combobox", "listbox", "checkbox", "radio",
    "spinbutton", "slider", "switch", "searchbox", "textarea",
})

BUTTON_ROLES = frozenset({"button", "link"})

NAVIGATION_PATTERNS = re.compile(
    r"(?i)\b(next\s*step|next|continue|proceed|forward|go\s*to\s*step|step\s*\d+|"
    r"save\s*&?\s*continue|save\s*and\s*next|save\s*&?\s*proceed|go|advance)"
    r"\b"
)

SUBMIT_PATTERNS = re.compile(
    r"(?i)\b(submit|file|lodge|send|confirm|finish|complete|done|save|register|sign\s*up|create|apply)\b"
)

# Generic patterns for landing page buttons that lead to forms
# Covers: consumer complaints, government portals, visa, tax, airline, insurance
FORM_ENTRY_PATTERNS = re.compile(
    r"(?i)\b(file|lodge|submit|start|begin|new|create|register|apply|now|here|"
    r"sign\s*up|get\s*started|make|open|launch|fill|complaint|report|"
    r"request|book|schedule|enroll|enrol|proceed|continue|go|click\s*here|"
    r"e-?service|online|portal|application|renewal|appointment|claim|"
    r"grievance|feedback|enquiry|inquiry|contact\s*us|write\s*to\s*us|"
    r"raise|initiate|generate|download|access)\b"
)

# Login patterns are handled separately — only used when wizard navigator
# decides login is needed, NOT by the generic landing page detector.
_LOGIN_PATTERNS = re.compile(
    r"(?i)\b(login|log\s*in|sign\s*in)\b"
)


# ──────────────────────────────────────────────────────────────────────
# PRIMARY EXTRACTION: Single-Pass JavaScript DOM Injection
# ──────────────────────────────────────────────────────────────────────

# This is the core of V2. A single JS function that discovers every
# form element on the page and extracts everything we need in one pass.

_JS_EXTRACT_ALL = """
() => {
    // ── Helper: Build a unique CSS selector for an element ──
    function buildSelector(el) {
        // Priority 1: ID (most reliable)
        if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
            return '#' + CSS.escape(el.id);
        }
        // Priority 2: name attribute
        if (el.name) {
            const sel = el.tagName.toLowerCase() + '[name="' + el.name.replace(/"/g, '\\\\"') + '"]';
            if (document.querySelectorAll(sel).length === 1) return sel;
        }
        // Priority 3: Build a path from parent
        function pathTo(element) {
            if (element.id && document.querySelectorAll('#' + CSS.escape(element.id)).length === 1) {
                return '#' + CSS.escape(element.id);
            }
            if (element === document.body) return 'body';
            const parent = element.parentElement;
            if (!parent) return element.tagName.toLowerCase();
            const siblings = Array.from(parent.children).filter(c => c.tagName === element.tagName);
            const tag = element.tagName.toLowerCase();
            if (siblings.length === 1) {
                return pathTo(parent) + ' > ' + tag;
            }
            const idx = siblings.indexOf(element) + 1;
            return pathTo(parent) + ' > ' + tag + ':nth-of-type(' + idx + ')';
        }
        return pathTo(el);
    }

    // ── Helper: Find the label for a form element ──
    // 12-source fallback chain, ordered by reliability
    function findLabel(el) {
        let label = '';

        // 1. aria-label (explicit, highest priority)
        label = (el.getAttribute('aria-label') || '').trim();
        if (label) return label;

        // 2. aria-labelledby (explicit reference)
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const parts = labelledBy.split(/\\s+/).map(id => {
                const ref = document.getElementById(id);
                return ref ? ref.textContent.trim() : '';
            }).filter(Boolean);
            if (parts.length) return parts.join(' ');
        }

        // 3. <label for="id"> (standard HTML association)
        if (el.id) {
            const labelEl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (labelEl) {
                label = labelEl.textContent.trim();
                if (label) return label;
            }
        }

        // 4. Wrapping <label> parent (up to 4 levels)
        let parent = el.parentElement;
        for (let d = 0; d < 4 && parent; d++) {
            if (parent.tagName === 'LABEL') {
                // Get label text excluding the input's own value
                const clone = parent.cloneNode(true);
                const inputs = clone.querySelectorAll('input, select, textarea');
                inputs.forEach(inp => inp.remove());
                label = clone.textContent.trim();
                if (label) return label;
            }
            parent = parent.parentElement;
        }

        // 5. placeholder attribute
        label = (el.placeholder || '').trim();
        if (label) return label;

        // 6. title attribute
        label = (el.title || '').trim();
        if (label) return label;

        // 7. Preceding sibling element text (label, span, b, p, td, th, dt)
        const prevSibling = el.previousElementSibling;
        if (prevSibling) {
            const prevTag = prevSibling.tagName;
            if (['LABEL', 'SPAN', 'B', 'STRONG', 'P', 'TD', 'TH', 'DT', 'DIV', 'EM'].includes(prevTag)) {
                label = prevSibling.textContent.trim();
                if (label && label.length < 80) return label.replace(/:$/, '').trim();
            }
        }

        // 8. Table cell: look at the previous <td>/<th> in the same row
        const td = el.closest('td, th');
        if (td) {
            const prevTd = td.previousElementSibling;
            if (prevTd) {
                label = prevTd.textContent.trim();
                if (label && label.length < 80) return label.replace(/:$/, '').trim();
            }
        }

        // 9. Definition list: look at the preceding <dt>
        const dd = el.closest('dd');
        if (dd) {
            const dt = dd.previousElementSibling;
            if (dt && dt.tagName === 'DT') {
                label = dt.textContent.trim();
                if (label) return label.replace(/:$/, '').trim();
            }
        }

        // 10. Nearby text in parent container (div-based forms)
        const container = el.closest('.form-group, .input-group, .field, .form-field, .form-row, .form-control, .field-group, .control-group, .mb-3, .mb-2, p, li');
        if (container) {
            const texts = [];
            for (const child of container.childNodes) {
                if (child === el) continue;
                if (child.nodeType === 3) { // text node
                    const t = child.textContent.trim();
                    if (t && t.length > 0 && t.length < 80) texts.push(t);
                } else if (child.nodeType === 1 && child !== el) {
                    // Element node that's not an input
                    const childTag = child.tagName;
                    if (['LABEL', 'SPAN', 'B', 'STRONG', 'EM', 'I', 'P', 'SMALL', 'DIV'].includes(childTag)) {
                        // Don't include if it contains form elements
                        if (!child.querySelector('input, select, textarea')) {
                            const t = child.textContent.trim();
                            if (t && t.length > 0 && t.length < 80) texts.push(t);
                        }
                    }
                }
            }
            if (texts.length >= 1 && texts.length <= 3) {
                label = texts[0].replace(/:$/, '').trim();
                if (label) return label;
            }
        }

        // 11. Walk up to find any text-bearing ancestor (last resort for deeply nested)
        let ancestor = el.parentElement;
        for (let d = 0; d < 5 && ancestor; d++) {
            // Check for direct text children of this ancestor
            for (const child of ancestor.childNodes) {
                if (child.nodeType === 3) {
                    const t = child.textContent.trim();
                    if (t && t.length > 1 && t.length < 60 && !/^[\\s\\n]*$/.test(t)) {
                        return t.replace(/:$/, '').trim();
                    }
                }
            }
            ancestor = ancestor.parentElement;
        }

        // 12. id/name attribute as last resort (cleaned up)
        if (el.id) {
            return el.id
                .replace(/^.*\\./, '')
                .replace(/([A-Z])/g, ' $1')
                .replace(/[_\\-\\.\\[\\]]/g, ' ')
                .trim();
        }
        if (el.name) {
            return el.name
                .replace(/^.*\\./, '')
                .replace(/([A-Z])/g, ' $1')
                .replace(/[_\\-\\.\\[\\]]/g, ' ')
                .trim();
        }

        return '';
    }

    // ── Helper: Determine the semantic role of an element ──
    function getRole(el) {
        // Explicit ARIA role takes priority
        const ariaRole = el.getAttribute('role');
        if (ariaRole && ['textbox', 'combobox', 'listbox', 'checkbox', 'radio',
            'spinbutton', 'slider', 'switch', 'searchbox'].includes(ariaRole)) {
            return ariaRole;
        }

        const tag = el.tagName.toLowerCase();
        if (tag === 'select') return 'combobox';
        if (tag === 'textarea') return 'textbox';

        const type = (el.type || '').toLowerCase();
        switch (type) {
            case 'checkbox': return 'checkbox';
            case 'radio': return 'radio';
            case 'number': return 'spinbutton';
            case 'range': return 'slider';
            case 'search': return 'searchbox';
            case 'date': return 'date';
            case 'datetime-local': return 'date';
            case 'month': return 'date';
            case 'week': return 'date';
            case 'time': return 'date';
            default: return 'textbox';
        }
    }

    // ── Main extraction ──
    const fields = [];
    const buttons = [];
    const seenSelectors = new Set();

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

    function processElement(el) {
        const tag = el.tagName?.toLowerCase();
        if (!tag) return;

        // Skip hidden/invisible elements (check full ancestor chain)
        if (!isEffectivelyVisible(el)) return;

        if (el.type === 'hidden') return;

        const isFileUpload = (tag === 'input' && el.type === 'file');
        const isInput = (tag === 'input' && !['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(el.type));
        const isSubmitBtn = (tag === 'input' && ['submit', 'button', 'reset', 'image'].includes(el.type));
        const isButton = (tag === 'button');
        const isSelect = tag === 'select';
        const isTextarea = tag === 'textarea';
        const isContentEditable = el.contentEditable === 'true' && tag !== 'body';
        const hasInteractiveRole = ['textbox', 'combobox', 'listbox', 'checkbox', 'radio',
            'spinbutton', 'slider', 'switch', 'searchbox'].includes(el.getAttribute('role'));

        // ── Handle file upload fields ──
        if (isFileUpload) {
            const selector = buildSelector(el);
            if (seenSelectors.has(selector)) return;
            seenSelectors.add(selector);
            const label = findLabel(el);
            const accept = el.accept || '';
            const multiple = el.multiple || false;
            fields.push({
                role: 'file_upload',
                name: label || 'File Upload',
                selector_css: selector,
                tag: tag,
                input_type: 'file',
                accept: accept,
                multiple: multiple,
            });
            return;
        }

        // ── Handle buttons ──
        if (isSubmitBtn || isButton) {
            const text = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
            if (!text) return;
            const selector = buildSelector(el);
            if (seenSelectors.has(selector)) return;
            seenSelectors.add(selector);
            buttons.push({
                role: el.getAttribute('role') || (tag === 'a' ? 'link' : 'button'),
                name: text,
                selector_css: selector,
                tag: tag,
                type: el.type || '',
            });
            return;
        }

        // ── Handle form fields ──
        if (isInput || isSelect || isTextarea || isContentEditable || hasInteractiveRole) {
            const selector = buildSelector(el);
            if (seenSelectors.has(selector)) return;
            seenSelectors.add(selector);

            const label = findLabel(el);
            const role = getRole(el);

            // Extract options for selects
            const options = [];
            if (isSelect) {
                el.querySelectorAll('option').forEach(opt => {
                    const text = opt.textContent.trim();
                    const val = opt.value;
                    // Skip placeholder options
                    if (!text || text === '' || text === '--' || val === '') return;
                    const lower = text.toLowerCase();
                    if (lower === 'select' || lower === 'please select' ||
                        lower === '-- select --' || lower === '- select -' ||
                        lower === 'choose' || lower === 'choose...' ||
                        lower.startsWith('select ') || lower.startsWith('-- ') ||
                        lower.startsWith('choose ')) return;
                    options.push(text);
                });
            }

            // Extract radio group options
            const radioOptions = [];
            if (role === 'radio' && el.name) {
                const radios = document.querySelectorAll('input[type="radio"][name="' + CSS.escape(el.name) + '"]');
                radios.forEach(r => {
                    let rLabel = '';
                    // Check for associated label
                    if (r.id) {
                        const lbl = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
                        if (lbl) rLabel = lbl.textContent.trim();
                    }
                    if (!rLabel) {
                        // Check wrapping label
                        const p = r.parentElement;
                        if (p && p.tagName === 'LABEL') {
                            const clone = p.cloneNode(true);
                            clone.querySelectorAll('input').forEach(i => i.remove());
                            rLabel = clone.textContent.trim();
                        }
                    }
                    if (!rLabel) rLabel = r.value;
                    if (rLabel) radioOptions.push(rLabel);
                });
            }

            fields.push({
                role: role,
                name: label,
                value: el.value || '',
                required: el.required || el.getAttribute('aria-required') === 'true' || false,
                options: options.length > 0 ? options : (radioOptions.length > 0 ? radioOptions : []),
                selector_css: selector,
                selector_id: el.id || '',
                selector_name: el.name || '',
                input_type: el.type || tag,
                tag: tag,
                autocomplete: el.autocomplete || '',
            });
        }
    }

    // ── Also find <a> links and standalone buttons that look like nav/submit ──
    function processLinks() {
        document.querySelectorAll('a[href], button, input[type="submit"], input[type="button"]').forEach(el => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'a' || ((tag === 'input' || tag === 'button') && !seenSelectors.has(buildSelector(el)))) {
                if (!isEffectivelyVisible(el)) return;

                const text = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
                if (!text || text.length > 100) return;

                const selector = buildSelector(el);
                if (seenSelectors.has(selector)) return;
                seenSelectors.add(selector);

                buttons.push({
                    role: tag === 'a' ? 'link' : 'button',
                    name: text,
                    selector_css: selector,
                    tag: tag,
                    type: el.type || '',
                });
            }
        });
    }

    // Process all form elements
    document.querySelectorAll('input, select, textarea, [role="textbox"], [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"], [role="spinbutton"], [role="slider"], [role="switch"], [role="searchbox"], [contenteditable="true"]').forEach(el => processElement(el));

    // Process shadow DOMs (1 level deep)
    document.querySelectorAll('*').forEach(el => {
        if (el.shadowRoot) {
            el.shadowRoot.querySelectorAll('input, select, textarea, [role="textbox"], [role="combobox"], [contenteditable="true"]').forEach(shadowEl => processElement(shadowEl));
        }
    });

    // ── Detect React Select / custom dropdown components ──
    function processCustomDropdowns() {
        // React Select: [class*="-container"] with [class*="-control"] inside
        const reactSelectContainers = document.querySelectorAll(
            '[class*="react-select"], [class*="css-"][class*="-container"]'
        );
        reactSelectContainers.forEach(container => {
            // Find the control div
            const control = container.querySelector('[class*="-control"]');
            if (!control) return;
            if (!isEffectivelyVisible(control)) return;

            // Find the hidden input inside
            const input = container.querySelector('input[id*="react-select"], input[class*="-Input"]');
            const inputSelector = input ? buildSelector(input) : buildSelector(control);

            // Skip if already processed
            if (seenSelectors.has(inputSelector)) return;
            seenSelectors.add(inputSelector);

            // Find label: look for a label element, aria-label, or preceding text
            let label = '';
            if (input && input.id) {
                const lbl = document.querySelector('label[for="' + CSS.escape(input.id) + '"]');
                if (lbl) label = lbl.textContent.trim();
            }
            if (!label) {
                label = container.getAttribute('aria-label') || control.getAttribute('aria-label') || '';
            }
            if (!label) {
                // Check preceding sibling or parent label
                let prev = container.previousElementSibling;
                if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN' || prev.tagName === 'DIV')) {
                    label = prev.textContent.trim();
                }
            }
            if (!label) {
                // Check parent for label
                const parent = container.closest('.form-group, .field, [class*="field"], [class*="form"]');
                if (parent) {
                    const lbl = parent.querySelector('label, .label, [class*="label"]');
                    if (lbl) label = lbl.textContent.trim();
                }
            }
            if (!label) {
                // Use placeholder
                const placeholder = container.querySelector('[class*="-placeholder"]');
                if (placeholder) label = placeholder.textContent.trim();
            }
            if (!label) label = 'Custom Select';

            // Extract current value
            const singleValue = container.querySelector('[class*="-singleValue"]');
            const currentVal = singleValue ? singleValue.textContent.trim() : '';

            // Extract available options (if menu is open or we can read from data)
            const options = [];
            const menuItems = container.querySelectorAll('[class*="-option"]');
            menuItems.forEach(opt => {
                const text = opt.textContent.trim();
                if (text) options.push(text);
            });

            fields.push({
                role: 'combobox',
                name: label,
                value: currentVal,
                required: container.getAttribute('aria-required') === 'true' || false,
                options: options,
                selector_css: inputSelector,
                selector_id: input ? (input.id || '') : '',
                selector_name: input ? (input.name || '') : '',
                input_type: 'react-select',
                tag: 'div',
                autocomplete: '',
                _custom_type: 'react-select',
                _container_selector: buildSelector(container),
                _control_selector: buildSelector(control),
            });
        });

        // Ant Design Select: .ant-select
        document.querySelectorAll('.ant-select:not(.ant-select-disabled)').forEach(container => {
            if (!isEffectivelyVisible(container)) return;
            const input = container.querySelector('.ant-select-selection-search-input');
            const selector = input ? buildSelector(input) : buildSelector(container);
            if (seenSelectors.has(selector)) return;
            seenSelectors.add(selector);

            let label = '';
            const parent = container.closest('.ant-form-item');
            if (parent) {
                const lbl = parent.querySelector('.ant-form-item-label label');
                if (lbl) label = lbl.textContent.trim();
            }
            if (!label) label = container.getAttribute('aria-label') || 'Select';

            const placeholder = container.querySelector('.ant-select-selection-placeholder');
            const selectedItem = container.querySelector('.ant-select-selection-item');

            fields.push({
                role: 'combobox',
                name: label,
                value: selectedItem ? selectedItem.textContent.trim() : '',
                required: false,
                options: [],
                selector_css: selector,
                selector_id: input ? (input.id || '') : '',
                selector_name: '',
                input_type: 'ant-select',
                tag: 'div',
                autocomplete: '',
                _custom_type: 'ant-select',
                _container_selector: buildSelector(container),
            });
        });

        // Material UI Select: .MuiSelect-root or [class*="MuiSelect"]
        document.querySelectorAll('[class*="MuiSelect"], [class*="MuiAutocomplete"]').forEach(container => {
            if (!isEffectivelyVisible(container)) return;
            const input = container.querySelector('input');
            const selector = input ? buildSelector(input) : buildSelector(container);
            if (seenSelectors.has(selector)) return;
            seenSelectors.add(selector);

            let label = '';
            const formControl = container.closest('.MuiFormControl-root, [class*="MuiFormControl"]');
            if (formControl) {
                const lbl = formControl.querySelector('label');
                if (lbl) label = lbl.textContent.trim();
            }
            if (!label) label = container.getAttribute('aria-label') || 'Select';

            fields.push({
                role: 'combobox',
                name: label,
                value: input ? (input.value || '') : '',
                required: false,
                options: [],
                selector_css: selector,
                selector_id: input ? (input.id || '') : '',
                selector_name: input ? (input.name || '') : '',
                input_type: 'mui-select',
                tag: 'div',
                autocomplete: '',
                _custom_type: 'mui-select',
                _container_selector: buildSelector(container),
            });
        });
    }

    // Process buttons and links
    processLinks();

    // Process custom dropdown components (React Select, Ant Design, MUI)
    processCustomDropdowns();

    // Get page title
    const title = document.title || '';

    return { fields, buttons, title };
}
"""


def _classify_button(name: str) -> str:
    """Classify a button as 'nav', 'submit', or 'other'."""
    if NAVIGATION_PATTERNS.search(name):
        return "nav"
    if SUBMIT_PATTERNS.search(name):
        return "submit"
    return "other"


def _normalize_js_results(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize the raw JS extraction results into the Form Genome structure.
    This is a clean, single-pass normalization — no merging needed.
    """
    fields: list[dict[str, Any]] = []
    buttons: list[dict[str, Any]] = []
    nav_buttons: list[dict[str, Any]] = []
    submit_buttons: list[dict[str, Any]] = []
    field_type_counts: dict[str, int] = {}

    # Deduplicate radio buttons: group by name, keep first
    seen_radio_names: set[str] = set()

    for f in raw.get("fields", []):
        role = f.get("role", "textbox")
        name = f.get("name", "")
        selector_css = f.get("selector_css", "")

        # For radio buttons, group by name attribute
        if role == "radio":
            radio_name = f.get("selector_name", "")
            if radio_name and radio_name in seen_radio_names:
                continue
            if radio_name:
                seen_radio_names.add(radio_name)

        entry = {
            "role": role,
            "name": name,
            "value": f.get("value", ""),
            "attributes": {},
            "required": f.get("required", False),
            "options": f.get("options", []),
            "selector_css": selector_css,
            "selector": f"page.locator('{selector_css}')" if selector_css else "",
            "input_type": f.get("input_type", ""),
            "autocomplete": f.get("autocomplete", ""),
        }

        if f.get("required"):
            entry["attributes"]["required"] = "true"

        fields.append(entry)
        field_type_counts[role] = field_type_counts.get(role, 0) + 1

    for b in raw.get("buttons", []):
        name = b.get("name", "")
        if not name:
            continue

        entry = {
            "role": b.get("role", "button"),
            "name": name,
            "value": "",
            "attributes": {},
            "selector_css": b.get("selector_css", ""),
            "selector": f"page.locator('{b.get('selector_css', '')}')" if b.get("selector_css") else "",
        }

        btn_type = _classify_button(name)
        if btn_type == "nav":
            nav_buttons.append(entry)
        elif btn_type == "submit":
            submit_buttons.append(entry)
        else:
            buttons.append(entry)

    return {
        "fields": fields,
        "buttons": buttons,
        "nav_buttons": nav_buttons,
        "submit_buttons": submit_buttons,
        "title": raw.get("title", ""),
        "structure": {
            "total_fields": len(fields),
            "field_types": field_type_counts,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# ENRICHMENT: ARIA Snapshot (optional, adds semantic info)
# ──────────────────────────────────────────────────────────────────────

_ARIA_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s+"
    r"(?P<role>\w+)"
    r'(?:\s+"(?P<name>[^"]*)")?'
    r"(?:\s+\[(?P<attrs>[^\]]*)\])?"
    r"(?::\s*(?P<value>.*))?$"
)


def _parse_aria_snapshot(raw_yaml: str) -> list[dict[str, Any]]:
    """Parse Playwright's ARIA snapshot YAML into a list of element dicts."""
    elements: list[dict[str, Any]] = []
    if not raw_yaml or not raw_yaml.strip():
        return elements

    for line in raw_yaml.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue

        match = _ARIA_LINE_RE.match(line)
        if not match:
            continue

        role = match.group("role").strip().lower()
        name = (match.group("name") or "").strip()
        attrs_raw = (match.group("attrs") or "").strip()
        value = (match.group("value") or "").strip()

        attrs: dict[str, str] = {}
        if attrs_raw:
            for attr_part in attrs_raw.split(","):
                attr_part = attr_part.strip()
                if "=" in attr_part:
                    k, v = attr_part.split("=", 1)
                    attrs[k.strip()] = v.strip()
                elif attr_part:
                    attrs[attr_part] = "true"

        elements.append({
            "role": role,
            "name": name,
            "value": value,
            "attributes": attrs,
        })

    return elements


def _enrich_with_aria(
    genome: dict[str, Any],
    aria_elements: list[dict[str, Any]],
) -> None:
    """
    Enrich JS-extracted genome with ARIA semantic information.
    
    This is a LIGHTWEIGHT enrichment — it only adds information that
    the JS extraction might have missed (like ARIA attributes: checked,
    expanded, disabled). It does NOT replace any JS-discovered data.
    """
    # Build a lookup of ARIA elements by role for quick matching
    aria_by_role: dict[str, list[dict]] = {}
    for el in aria_elements:
        role = el.get("role", "")
        if role in INTERACTIVE_ROLES:
            aria_by_role.setdefault(role, []).append(el)

    # Match JS fields to ARIA elements by role + position
    js_by_role: dict[str, list[dict]] = {}
    for f in genome["fields"]:
        role = f.get("role", "textbox")
        js_by_role.setdefault(role, []).append(f)

    for role, js_list in js_by_role.items():
        aria_list = aria_by_role.get(role, [])
        for i, js_field in enumerate(js_list):
            if i < len(aria_list):
                aria_el = aria_list[i]
                # Enrich with ARIA attributes (don't overwrite existing)
                for attr_key, attr_val in aria_el.get("attributes", {}).items():
                    if attr_key not in js_field.get("attributes", {}):
                        js_field.setdefault("attributes", {})[attr_key] = attr_val

                # If JS found no name but ARIA has one, use ARIA's name
                if not js_field.get("name") and aria_el.get("name"):
                    js_field["name"] = aria_el["name"]


# ──────────────────────────────────────────────────────────────────────
# Iframe Detection
# ──────────────────────────────────────────────────────────────────────

async def _find_form_frame(page: Page) -> Page | Frame:
    """
    Check if the main form is inside an iframe. If so, return the frame.
    Otherwise, return the page itself.
    """
    # Count fields on main page
    main_field_count = await page.locator(
        "input:visible, select:visible, textarea:visible"
    ).count()

    if main_field_count >= 2:
        return page

    # Check iframes
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
        logger.info(
            "Found form in iframe with %d fields (main page had %d)",
            best_count, main_field_count,
        )
        return best_frame

    return page


# ──────────────────────────────────────────────────────────────────────
# Dynamic Form Detection
# ──────────────────────────────────────────────────────────────────────

async def _wait_for_dynamic_form(page_or_frame: Page | Frame) -> None:
    """Wait for dynamically loaded form fields to appear.
    
    Government portals and SPA frameworks can take 10-20 seconds
    to render their forms. We wait generously.
    """
    try:
        await page_or_frame.wait_for_selector(
            "input, select, textarea",
            state="visible",
            timeout=20000,
        )
        # Extra wait for late-loading fields (React hydration, Angular bootstrap)
        await asyncio.sleep(2)
    except Exception:
        logger.debug("No visible form fields found after waiting")


# ──────────────────────────────────────────────────────────────────────
# Generic Landing Page Detection
# ──────────────────────────────────────────────────────────────────────

async def _try_enter_form(page: Page, max_depth: int = 3) -> bool:
    """
    If the page is a landing page with a button/link that leads to a form,
    try to click it. Uses generic pattern matching, not hardcoded labels.
    Supports multi-level navigation (e.g., landing -> category -> form).
    Returns True if a form entry button was found and clicked.
    """
    # Dismiss any modal overlays first (common on government portals)
    try:
        from .wizard_navigator import _dismiss_modals
        await _dismiss_modals(page)
    except Exception:
        pass
    
    for depth in range(max_depth):
        # Count current form fields
        field_count = await page.locator(
            "input:visible, select:visible, textarea:visible"
        ).count()

        # If there are already enough fields, we're on the form
        if field_count >= 3:
            return depth > 0

        found_entry = False

        # Look for buttons/links that match form entry patterns
        for role in ("link", "button"):
            if found_entry:
                break
            try:
                elements = page.get_by_role(role)
                count = await elements.count()
                candidates = []
                for i in range(min(count, 30)):
                    try:
                        el = elements.nth(i)
                        text = (await el.text_content() or "").strip()
                        href = await el.get_attribute("href") or ""
                        if text and FORM_ENTRY_PATTERNS.search(text):
                            priority = 0
                            if "complaint" in text.lower() or "file" in text.lower():
                                priority = 2
                            elif "start" in text.lower() or "begin" in text.lower():
                                priority = 1
                            candidates.append((priority, i, el, text, href))
                    except Exception:
                        continue

                candidates.sort(key=lambda x: -x[0])

                for _, _, el, text, href in candidates:
                    try:
                        logger.info(
                            "Depth %d: Clicking form entry %s: '%s'",
                            depth, role, text,
                        )
                        await el.click(timeout=5000)
                        try:
                            await page.wait_for_load_state(
                                "domcontentloaded", timeout=8000
                            )
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=8000
                            )
                        except Exception:
                            pass

                        await asyncio.sleep(1)

                        new_count = await page.locator(
                            "input:visible, select:visible, textarea:visible"
                        ).count()
                        if new_count > field_count or new_count >= 3:
                            found_entry = True
                            break

                        # Check iframes too
                        for frame in page.frames:
                            if frame == page.main_frame:
                                continue
                            try:
                                iframe_count = await frame.locator(
                                    "input:visible, select:visible, textarea:visible"
                                ).count()
                                if iframe_count >= 3:
                                    found_entry = True
                                    break
                            except Exception:
                                continue
                        if found_entry:
                            break
                    except Exception:
                        continue
            except Exception:
                continue

        if not found_entry:
            return depth > 0

    return True


# ──────────────────────────────────────────────────────────────────────
# Page-Level Capture (V2: JS-First)
# ──────────────────────────────────────────────────────────────────────

async def _capture_page_genome(
    page_or_frame: Page | Frame,
) -> dict[str, Any]:
    """
    Capture the Form Genome of the current page/frame state.
    
    V2 Architecture:
      1. Run JS extraction (PRIMARY — works on any HTML)
      2. Run ARIA snapshot (ENRICHMENT — adds semantic attributes)
      3. Return clean, deduplicated genome
    """
    # ── PRIMARY: JavaScript DOM extraction ──
    try:
        raw_data = await page_or_frame.evaluate(_JS_EXTRACT_ALL)
    except Exception as exc:
        logger.warning("JS extraction failed: %s", exc)
        raw_data = {"fields": [], "buttons": [], "title": ""}

    genome = _normalize_js_results(raw_data)

    # ── ENRICHMENT: ARIA snapshot (optional) ──
    try:
        raw_snapshot = await page_or_frame.locator("body").aria_snapshot()
        if raw_snapshot:
            aria_elements = _parse_aria_snapshot(raw_snapshot)
            _enrich_with_aria(genome, aria_elements)
    except Exception:
        logger.debug("ARIA enrichment skipped (snapshot failed)")

    return genome


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

async def extract_genome(
    url: str,
    handle_multi_step: bool = True,
    max_steps: int = 10,
    user_intent: str = "",
    user_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Navigate to ANY URL, extract the form genome, and return a
    normalized Form Genome dictionary.

    V2 uses JS-first single-pass extraction for universal coverage.

    Parameters
    ----------
    url : str
        The URL of the form to sequence.
    handle_multi_step : bool
        Whether to attempt to detect and navigate multi-step forms.
    max_steps : int
        Maximum number of form steps to capture.

    Returns
    -------
    dict
        The Form Genome with keys: url, timestamp, title, steps, fields,
        buttons, submit_buttons, structure.
    """
    pool = await BrowserPool.get_instance()
    ctx = await pool.acquire()

    try:
        page = await ctx.new_page()
        await _stealth.apply_stealth_async(page)
        # Additional stealth: override navigator.webdriver
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)
        page.set_default_timeout(20000)

        # Try loading the page — some government sites reject headless
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as nav_err:
            logger.warning(
                "First navigation attempt failed (%s), retrying with longer timeout",
                str(nav_err)[:80],
            )
            # Retry once — some sites need a second attempt
            await page.goto(url, wait_until="commit", timeout=30000)

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # Step A: Try to enter the form from a landing page
        await _try_enter_form(page)

        # Step B: Wait for dynamic form fields
        await _wait_for_dynamic_form(page)

        # Step B2: Check if we have enough fields. If not, use
        # the Wizard Navigator (LLM-powered) to navigate through
        # category selection pages, state dropdowns, etc.
        initial_field_count = await page.locator(
            "input:visible, select:visible, textarea:visible"
        ).count()

        wizard_log = []
        print(f"[EXTRACTOR] initial_field_count={initial_field_count}, user_intent={user_intent!r}, user_data keys={list((user_data or {}).keys())}")
        if initial_field_count < 2 and (user_intent or user_data):
            logger.info(
                "Only %d fields found after landing page detection. "
                "Engaging Wizard Navigator with intent: '%s'",
                initial_field_count, user_intent[:80],
            )
            try:
                from agent.engine.wizard_navigator import navigate_wizard
                wizard_result = await navigate_wizard(
                    page=page,
                    user_intent=user_intent or "fill out this form",
                    user_data=user_data or {},
                    max_steps=6,
                    min_fields_for_form=2,
                )
                wizard_log = wizard_result.get("actions_log", [])
                if wizard_result.get("reached_form"):
                    logger.info(
                        "Wizard Navigator reached form after %d steps",
                        wizard_result["steps_taken"],
                    )
                else:
                    logger.warning(
                        "Wizard Navigator could not reach form: %s",
                        wizard_result.get("error", "unknown"),
                    )
            except Exception as exc:
                logger.warning("Wizard Navigator failed: %s", exc)

        # Step C: Check for iframes containing the form
        form_target = await _find_form_frame(page)

        # ── Multi-step genome capture ──
        all_steps: list[dict[str, Any]] = []
        all_fields: list[dict[str, Any]] = []
        all_buttons: list[dict[str, Any]] = []
        all_submit_buttons: list[dict[str, Any]] = []
        total_field_types: dict[str, int] = {}
        seen_fingerprints: set[str] = set()
        genome_title = ""

        for step_num in range(max_steps):
            logger.info(
                "Capturing genome for step %d", step_num + 1,
            )
            step_genome = await _capture_page_genome(form_target)

            if step_num == 0:
                genome_title = step_genome.get("title", "")

            # Build a fingerprint from field names to detect duplicates
            field_names = tuple(
                f.get("name", "") for f in step_genome["fields"]
            )
            fingerprint = str(field_names)

            if fingerprint in seen_fingerprints:
                logger.info(
                    "Step %d has identical fields to a previous step. "
                    "Stopping multi-step capture.",
                    step_num + 1,
                )
                break
            seen_fingerprints.add(fingerprint)

            step_data = {
                "step_number": step_num + 1,
                "url": page.url,
                "fields": step_genome["fields"],
                "buttons": step_genome["buttons"],
                "nav_buttons": step_genome["nav_buttons"],
                "submit_buttons": step_genome["submit_buttons"],
            }
            all_steps.append(step_data)
            all_fields.extend(step_genome["fields"])
            all_buttons.extend(step_genome["buttons"])
            all_submit_buttons.extend(step_genome["submit_buttons"])

            for role, count in step_genome["structure"]["field_types"].items():
                total_field_types[role] = (
                    total_field_types.get(role, 0) + count
                )

            if not handle_multi_step:
                break

            nav_btns = step_genome["nav_buttons"]
            if not nav_btns:
                logger.info(
                    "No navigation buttons found on step %d. "
                    "Genome complete.",
                    step_num + 1,
                )
                break

            # Click the first navigation button to advance
            nav_btn = nav_btns[0]
            try:
                nav_css = nav_btn.get("selector_css", "")
                if nav_css:
                    await form_target.locator(nav_css).first.click(timeout=5000)
                else:
                    name_pattern = re.compile(
                        re.escape(nav_btn["name"]), re.IGNORECASE
                    )
                    if isinstance(form_target, Frame):
                        await form_target.get_by_role(
                            nav_btn["role"], name=name_pattern
                        ).first.click(timeout=5000)
                    else:
                        await page.get_by_role(
                            nav_btn["role"], name=name_pattern
                        ).first.click(timeout=5000)

                await page.wait_for_load_state(
                    "domcontentloaded", timeout=5000
                )
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=5000
                    )
                except Exception:
                    pass
                logger.info("Advanced to step %d", step_num + 2)
            except Exception as exc:
                logger.warning(
                    "Failed to advance past step %d: %s",
                    step_num + 1, exc,
                )
                break

        genome = {
            "url": url,
            "title": genome_title,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "steps": all_steps,
            "fields": all_fields,
            "buttons": all_buttons,
            "submit_buttons": all_submit_buttons,
            "structure": {
                "total_fields": len(all_fields),
                "field_types": total_field_types,
                "has_multi_step": len(all_steps) > 1,
                "step_count": len(all_steps),
            },
            "wizard_log": wizard_log,
        }

        logger.info(
            "Genome extraction complete: %d fields across %d steps",
            len(all_fields), len(all_steps),
        )
        return genome
    finally:
        await pool.release(ctx)

async def extract_genome_from_page(page: Page) -> dict[str, Any]:
    """
    Capture the Form Genome from an already-open Playwright page.

    This is used by the Precision Executor when it already has a
    browser session open and needs to re-sequence a genome for
    self-healing.
    """
    form_target = await _find_form_frame(page)
    return await _capture_page_genome(form_target)
