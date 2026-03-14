# Grippyyy Setup

## Prerequisites

- Python 3.11+
- Git
- an OpenAI-compatible API key

## Clone

```bash
git clone https://github.com/13shreyansh/grippyyy.git
cd grippyyy
```

## Install

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Configure

Create `.env`:

```env
OPENAI_API_KEY=your_openai_api_key
```

For optional services, use `.env.example`.

## Run

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://127.0.0.1:8000/chat?new=1
```

## Fastest Demo Check

Send:

```text
Shopee Singapore sold me a defective laptop and they are refusing a refund.
```

Then click `Open CASE Singapore flow`.

## Troubleshooting

- If Playwright is missing: run `python -m playwright install chromium`
- If LLM calls fail: check `OPENAI_API_KEY`
- If the CASE run is slow: wait; the portal is multi-step and live
