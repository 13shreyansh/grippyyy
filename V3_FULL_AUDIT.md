# Grippy V3 — Full 4-Role Audit & Engineering Plan

## Role 1: CEO — Vision, Business, Universality

### Current State
Grippy V3 has a working pipeline: Onboard → Intake → Scout → Collect → Fill → Learn.
The DemoQA end-to-end test works. But it's NOT production-ready.

### Critical Business Gaps

**1. Scouting is too slow for repeat visits**
- Every scout takes 15-30 seconds (browser launch + page load + JS extraction)
- The genome_db already caches genomes, but the scout engine IGNORES the cache
- `scout.py` calls `extract_genome()` directly every time — never checks `genome_db`
- Fix: Scout should check genome_db FIRST. If cache HIT, skip browser entirely.
- This turns 30-second waits into <1-second responses for known forms.

**2. No common-sense pre-fill for case-specific fields**
- When user says "Scoot Airlines cancelled my flight", we know:
  - Flight forms need: PNR, flight number, booking reference, travel date
  - These are NOT permanent profile fields, but they're predictable by CATEGORY
- Fix: Build a "species knowledge base" — for each form species, define expected fields
  - airline_complaint → PNR, flight_number, booking_date, seat_number
  - bank_complaint → account_number, transaction_date, amount
  - This lets us ask smart questions BEFORE scouting

**3. No URL resolution for most companies**
- KNOWN_URLS has only 11 entries
- LLM URL resolution is unreliable (hallucinated URLs)
- Fix: Build a web search fallback — use search API to find "{company} complaint form"

### Universality Score: 4/10
- Works: Simple HTML forms, government portals with modals
- Fails: React Select, SPA wizards, file uploads, CAPTCHAs
- Missing: 60% of modern web forms use custom components

---

## Role 2: Data Scientist — Failure Pattern Analysis

### Failure Classification

| Failure Type | Frequency | Root Cause | Fix Difficulty |
|---|---|---|---|
| React Select dropdown | HIGH | `_handle_custom_dropdown` can't find the input | MEDIUM |
| SPA wizard steps | MEDIUM | Page doesn't reload, DOM mutates in-place | HARD |
| File upload fields | LOW | No handler exists | MEDIUM |
| CAPTCHA blocking | MEDIUM | 2Captcha integration exists but unreliable | HARD |
| Hidden fields detected | FIXED | `isEffectivelyVisible` added | DONE |
| Modal kills page | FIXED | `isSafeToHide` added | DONE |
| Scout ignores cache | HIGH | scout.py never calls genome_db | EASY |
| Options as strings vs dicts | FIXED | Both formats handled | DONE |

### React Select Failure Analysis
The current `_handle_custom_dropdown` fails because:
1. It searches by `get_by_role("combobox", name=pattern)` — React Select's input has no accessible name
2. React Select uses `class="css-*-control"` containers, not standard ARIA
3. The genome_extractor detects React Select fields but doesn't extract the CSS class path

**Fix Strategy:**
- Detect React Select by its characteristic class pattern: `[class*="react-select"]` or `[class*="-control"]`
- Click the control container to open the menu
- Type into the hidden input to filter
- Select from `[class*="-option"]` elements

### SPA Wizard Failure Analysis
CFPB's complaint form is a React SPA:
- URL doesn't change between steps
- DOM mutates when "Next" is clicked
- Current wizard_navigator only handles page navigations, not DOM mutations

**Fix Strategy:**
- After clicking a navigation button, wait for DOM mutation (not page load)
- Re-scan the page after each mutation
- Detect step indicators (progress bars, step numbers) to track position

---

## Role 3: Engineer — Workflows & Architecture

### Fix Priority Order (by impact × effort)

1. **Genome Cache in Scout** (Impact: HIGH, Effort: LOW) — 30min
   - Modify `scout.py` to check `genome_db.get_cached_genome()` first
   - If HIT and fresh (<24h), use cached genome instead of browser
   - If MISS or stale, do full scout and save to cache
   - This eliminates 90% of wait time for repeat forms

2. **React Select Handler** (Impact: HIGH, Effort: MEDIUM) — 2h
   - Add React Select detection in genome_extractor (class pattern matching)
   - Add dedicated React Select fill strategy in executor
   - Strategy: click container → type in input → wait for menu → click option

3. **SPA Step Detection** (Impact: MEDIUM, Effort: MEDIUM) — 2h
   - Add MutationObserver-based step detection in wizard_navigator
   - After clicking nav button, wait for DOM changes instead of page load
   - Re-scan fields after each mutation

4. **Species Knowledge Base** (Impact: MEDIUM, Effort: LOW) — 1h
   - Create a dict mapping species → expected case-specific fields
   - During intake, pre-ask predictable questions based on complaint type
   - Reduces collect phase to 0 questions for common complaints

5. **Critical Bug Fixes** (Impact: HIGH, Effort: LOW) — 1h
   - Fix: scout.py never uses genome_db cache
   - Fix: executor.py `_select_dropdown` Strategy 1 fails silently on React Select
   - Fix: genome_extractor doesn't capture React Select class selectors
   - Fix: app.py scout task doesn't pass genome to genome_db

---

## Role 4: Programmer — Implementation Plan

### Module Changes

**1. scout.py** — Add genome cache integration
```python
# Before extract_genome, check cache:
from .genome_db import get_cached_genome, save_genome_to_cache
cached = get_cached_genome(url)
if cached and is_fresh(cached):
    genome = cached  # Skip 30-second browser launch
else:
    genome = await extract_genome(...)
    save_genome_to_cache(url, genome)
```

**2. executor.py** — Add React Select handler
```python
async def _handle_react_select(target, field_name, value, selector_css):
    # Strategy 1: Click the react-select container
    # Strategy 2: Type to filter
    # Strategy 3: Click matching option from menu
```

**3. genome_extractor.py** — Detect React Select in JS scanner
```javascript
// Add to _JS_CAPTURE_GENOME:
// Detect React Select containers
const reactSelects = document.querySelectorAll('[class*="react-select"]');
```

**4. wizard_navigator.py** — SPA step detection
```python
# After clicking nav button:
# 1. Record current DOM hash
# 2. Wait for MutationObserver to fire
# 3. Re-scan fields
```

**5. scout.py** — Species knowledge base
```python
SPECIES_EXPECTED_FIELDS = {
    "airline_complaint": ["pnr", "flight_number", "booking_date"],
    "bank_complaint": ["account_number", "transaction_date", "amount"],
    ...
}
```
