# Grippy Deep Architectural Audit

## Audit Date: March 13, 2026
## Scope: Every Python file in the engine pipeline

---

## CRITICAL ISSUES (Must Fix — These Break Universality)

### 1. genome_extractor.py — Field Discovery is Fragile

**Problem**: The 3-layer extraction (ARIA → CSS → JS) is clever but has fundamental gaps:

- **Layer 1 (ARIA)**: Uses `page.accessibility.snapshot()` which depends on the browser's accessibility tree. Many real-world forms have BROKEN accessibility — no ARIA roles, no labels, no `for` attributes. The ARIA layer returns empty names for these fields.
- **Layer 2 (CSS)**: Only finds fields by CSS selectors (`input`, `select`, `textarea`). It gets the selector but NOT the field name. It's just a supplement to Layer 1.
- **Layer 3 (JS)**: This is the real workhorse — it reads `aria-label`, `label[for]`, `placeholder`, `name` attribute, `title`, and now (after my fix) preceding text. BUT it runs as a separate pass and the merge logic is fragile.
- **The merge logic (lines 657-724)**: Tries to match ARIA fields to JS fields by name similarity. If names don't match (because ARIA returned empty names), fields can be duplicated or lost.

**Root Cause**: The 3-layer approach is a HACK. It was designed for well-structured forms and patched for broken ones. A truly universal extractor should have ONE extraction path that handles ALL cases.

**Fix Required**: Rewrite extraction as a single Playwright JS injection that:
1. Finds ALL visible form elements (`input`, `select`, `textarea`)
2. For EACH element, tries every possible name source in priority order:
   - `aria-label` → `label[for=id]` → `placeholder` → `title` → `name` attr → `id` attr → preceding text → parent label text → nearby text
3. Gets the element's role from its tag/type/attributes
4. Gets a unique CSS selector for each element
5. Returns a clean, deduplicated field list in ONE pass

### 2. field_mapper.py — Synonym Dictionary is Finite

**Problem**: The `FIELD_SYNONYMS` dictionary (lines 50-140) maps field names to user data keys. It has ~200 entries. But the internet has INFINITE form field names. Examples that would fail:
- "Telephone" (not in synonyms, "phone" is)
- "Postcode" (not in synonyms, "postal_code" is)
- "DOB" (not in synonyms, "date_of_birth" is)
- "Surname" (not in synonyms, "family_name" is)
- "Mobile No." (not in synonyms)
- "Apt/Suite" (not in synonyms)

**Root Cause**: A static dictionary can NEVER be universal. It's a hack that works for common forms but fails on edge cases.

**Fix Required**: 
1. Keep the synonym dictionary as a FAST PATH (deterministic, zero latency)
2. Add a FUZZY MATCHING layer using string similarity (Levenshtein, token overlap)
3. Add a SEMANTIC MATCHING layer using the LLM as LAST RESORT only
4. The priority should be: exact match → synonym match → fuzzy match → LLM match

### 3. executor.py — Field Location Strategy is Brittle

**Problem**: The `_fill_text_field()` function (lines 200-300) tries to find fields by:
1. CSS selector (if available)
2. ARIA role + name
3. Label text
4. Placeholder text

But it uses `page.get_by_role()` and `page.get_by_label()` which depend on proper ARIA/label structure. For forms without proper labels (like Parabank before the fix), these all fail.

**Root Cause**: The executor should use the CSS selector FIRST (it's the most reliable — it was extracted from the actual DOM), then fall back to other strategies.

**Fix Required**: 
1. Always try CSS selector first (it's a direct pointer to the element)
2. Fall back to `get_by_label` → `get_by_placeholder` → `get_by_role` → XPath
3. Add a VISUAL fallback: if all selectors fail, use the field's position on the page

### 4. executor.py — Dropdown Handling is Weak

**Problem**: `_select_dropdown()` (lines 320-400) only handles `<select>` elements. Many modern forms use custom dropdowns (div-based, React Select, Material UI, etc.). These are invisible to `select_option()`.

**Root Cause**: Only handles native HTML `<select>`. Custom dropdowns require click → search → click-option.

**Fix Required**: Add custom dropdown detection and handling:
1. Try native `select_option()` first
2. If that fails, detect if it's a custom dropdown (has `role="listbox"` or `role="combobox"`)
3. Click to open → type to filter → click matching option

### 5. genome_extractor.py — Multi-Step Forms Only Detect "Next" Buttons

**Problem**: `_detect_multi_step()` (lines 400-500) looks for buttons with text like "Next", "Continue", "Proceed". But many multi-step forms use:
- Tab navigation
- Accordion sections
- Progress bar clicks
- URL-based steps (page1.html → page2.html)

**Root Cause**: Only handles button-click navigation. Not universal.

**Fix Required**: This is a V2 concern. For now, document the limitation. The current approach works for 80% of multi-step forms.

---

## MODERATE ISSUES (Should Fix — These Reduce Quality)

### 6. field_mapper.py — LLM Fallback is Slow and Unreliable

**Problem**: When deterministic matching fails, the mapper calls the LLM (lines 200-280). This adds 2-5 seconds per call and the LLM sometimes maps incorrectly (e.g., mapping "SSN" to "phone").

**Fix**: Reduce LLM dependency by making fuzzy matching much better. LLM should only be called for truly ambiguous fields.

### 7. orchestrator.py — Self-Healing is Naive

**Problem**: If >50% of fields fail, it invalidates cache and retries. But it retries with the SAME extraction logic. If the extraction was wrong, retrying won't help.

**Fix**: On self-heal, use a DIFFERENT extraction strategy (e.g., force JS-only extraction, or use a different viewport size).

### 8. captcha_handler.py — No Real Testing

**Problem**: The CAPTCHA handler was written but never tested against a real CAPTCHA. It depends on external API keys that aren't configured.

**Fix**: This is acceptable for now — it's properly gated behind API key checks. Document that it requires `TWOCAPTCHA_API_KEY`.

### 9. auth.py — SQLite for Auth in Production

**Problem**: Using SQLite for user authentication. This works for a demo but won't scale to concurrent users.

**Fix**: This is acceptable for the current stage. The schema is clean and can be migrated to PostgreSQL later.

---

## MINOR ISSUES (Nice to Fix)

### 10. Hardcoded User-Agent String
The executor uses a hardcoded Chrome 120 user-agent. Should rotate or use a more recent version.

### 11. No Rate Limiting on API Endpoints
The auth endpoints have no rate limiting. Should add basic rate limiting for production.

### 12. No Input Validation on URLs
The engine accepts any string as a URL. Should validate URL format before starting extraction.

---

## SUMMARY OF REQUIRED REBUILDS

| Priority | Component | Current State | Required Fix |
|----------|-----------|---------------|--------------|
| P0 | genome_extractor.py | 3-layer merge hack | Single-pass JS extraction |
| P0 | field_mapper.py | Static synonym dict | Fuzzy + semantic matching |
| P0 | executor.py | ARIA-dependent field location | CSS-first with fallbacks |
| P1 | executor.py | Native select only | Custom dropdown support |
| P1 | orchestrator.py | Naive self-heal | Strategy-based retry |
| P2 | captcha_handler.py | Untested | Acceptable for now |
| P2 | auth.py | SQLite | Acceptable for now |
