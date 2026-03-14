# Grippy V3: Complete Product Architecture

## The Vision
User opens Grippy. Types: "Scoot Airlines lost my luggage and won't respond."
Grippy says: "I'll handle this. Let me check what Scoot's complaint form needs..."
Grippy scouts the form, comes back: "I need your booking reference and flight date."
User provides them. Grippy fills and submits. Done.

## Current State (V2) — What Exists

| Component | Status | What It Does |
|-----------|--------|-------------|
| `auth.py` | Built | User registration, login, JWT tokens, profile CRUD |
| `intake.py` | Built | Chat LLM that extracts complaint JSON |
| `router.py` | Built | Routes complaints to the right regulator |
| `genome_extractor.py` | Built | Scans any form's DNA (fields, buttons, options) |
| `wizard_navigator.py` | Built | Navigates multi-step wizards to reach forms |
| `field_mapper.py` | Built | Maps user data keys to form field names |
| `executor.py` | Built | Fills forms using Playwright |
| `orchestrator.py` | Built | Ties extract→classify→map→execute pipeline |
| `genome_db.py` | Built | SQLite cache for genomes and mappings |
| `index.html` | Built | Landing page (marketing) |
| `demo.html` | Built | Technical demo (URL + JSON data input) |
| `chat.html` | Built | Chat UI for complaint intake |

## Gap Analysis — What's Missing

### GAP 1: No Onboarding / Persistent User Profile
- User has to manually type all their data every time
- No "remember me" — no persistent identity
- No way to update address and have it stick

### GAP 2: No Scout-First Workflow
- Current flow: user provides ALL data upfront → engine tries to fill
- Missing fields just fail silently
- No intelligence about what the form actually needs BEFORE asking user

### GAP 3: No Intelligent Gap-Fill
- If form needs "booking reference" and user didn't provide it, it just skips
- No mechanism to ask user for missing data mid-flow
- No way to distinguish "permanent data" vs "case-specific data"

### GAP 4: No Unified UX
- Three separate UIs (index, demo, chat) with no connection
- Demo requires technical JSON input
- Chat only handles complaints, not general form filling

### GAP 5: No Natural Language → URL Resolution
- User says "Scoot Airlines complaint" — system doesn't know the URL
- Need to find the right complaint form automatically

## V3 Architecture — The Complete Flow

```
┌─────────────────────────────────────────────────────────┐
│                    GRIPPY V3 FLOW                        │
│                                                          │
│  1. ONBOARD (first time only)                           │
│     → Collect: name, email, phone, address, DOB         │
│     → Save to persistent profile (SQLite)               │
│     → User can update anytime: "new address is..."      │
│                                                          │
│  2. INTAKE (every request)                              │
│     → User: "Scoot lost my luggage"                     │
│     → LLM extracts: company, issue, desired outcome     │
│     → LLM resolves: complaint URL for Scoot             │
│                                                          │
│  3. SCOUT (before asking user anything)                 │
│     → Navigate to form URL                              │
│     → Wizard Navigator handles multi-step               │
│     → Genome Extractor scans ALL fields                 │
│     → Returns: list of required fields + their types    │
│                                                          │
│  4. GAP ANALYSIS (intelligent)                          │
│     → Compare form fields vs user profile               │
│     → Compare form fields vs complaint data             │
│     → Identify MISSING fields                           │
│     → Categorize: permanent (save) vs case-specific     │
│                                                          │
│  5. COLLECT (only missing data)                         │
│     → Ask user ONLY for what's missing                  │
│     → "I need your booking ref and flight date"         │
│     → Smart questions (not a form, a conversation)      │
│                                                          │
│  6. FILL & SUBMIT                                       │
│     → Merge: profile + complaint + collected data       │
│     → Execute form fill                                 │
│     → Report result                                     │
│                                                          │
│  7. LEARN                                               │
│     → If user provided new permanent data, offer to     │
│       save it to profile                                │
│     → Cache genome for faster future fills              │
└─────────────────────────────────────────────────────────┘
```

## Implementation Plan

### Module 1: User Profile Store (`agent/engine/user_store.py`)
- SQLite-backed persistent profile
- No auth required (local-first, single user for MVP)
- Fields: name, email, phone, address, DOB, gender, etc.
- `get_profile()` → returns all saved data
- `update_profile(key, value)` → updates single field
- `is_onboarded()` → checks if basic info exists
- `get_onboarding_questions()` → returns what's still needed

### Module 2: Scout Engine (`agent/engine/scout.py`)
- Takes a URL, runs genome extraction in DRY RUN mode
- Returns: list of field names, types, required status, options
- Categorizes fields: "which of these does the user already have?"
- Returns: `{have: [...], missing: [...], case_specific: [...]}`

### Module 3: Gap Analyzer (`agent/engine/gap_analyzer.py`)
- Input: genome fields + user profile + complaint data
- Output: list of missing fields with human-readable questions
- Uses LLM to generate natural questions for missing fields
- Groups questions intelligently (don't ask 20 questions one by one)

### Module 4: Unified Chat Orchestrator (`agent/engine/chat_orchestrator.py`)
- State machine: ONBOARD → INTAKE → SCOUT → COLLECT → FILL
- Manages the conversation flow
- Calls the right engine at each stage
- Returns SSE events for real-time UI updates

### Module 5: Unified Frontend (`templates/app.html`)
- Single page app with chat interface
- Onboarding flow (first visit)
- Complaint/form-fill flow
- Real-time progress indicators
- Profile management sidebar

## Data Flow Example

```
User: "Scoot Airlines lost my luggage and won't respond"

→ INTAKE LLM extracts:
  {company: "Scoot Airlines", issue: "lost luggage", outcome: "compensation"}

→ URL RESOLVER finds:
  https://www.flyscoot.com/en/contact-us (or complaint form URL)

→ SCOUT scans the form and finds fields:
  [first_name, last_name, email, phone, booking_ref, flight_number,
   flight_date, description, category]

→ GAP ANALYSIS:
  HAVE (from profile): first_name, last_name, email, phone
  HAVE (from complaint): description, category
  MISSING: booking_ref, flight_number, flight_date

→ COLLECT asks user:
  "I need a few more details:
   - Your booking reference number
   - Flight number
   - Date of the flight"

→ User: "Booking ref TR123456, flight TR123, March 1st"

→ FILL merges all data and submits the form
```
