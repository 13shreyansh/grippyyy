# Grippyyy Demo Runbook

## Primary Demo Path

Use the chat-to-CASE flow as the main story.

Message to use:

```text
Shopee Singapore sold me a defective laptop and they are refusing a refund.
```

Flow:

1. open `/chat?new=1`
2. send the message above
3. wait for the strategy
4. click `Open CASE Singapore flow`
5. let `/live-fill` run to completion
6. show the CASE reference number

## Demo Claim

Use this framing:

> Grippyyy already has two real working layers today: the complaint-intelligence layer and the universal execution layer. What is not complete yet is the production wrapper around them.

## What The Audience Should See

- complaint understanding in chat
- a real escalation path
- redirect into live filing
- visible multi-step browser execution
- a real CASE confirmation reference number

## Fallback

If the chat-first path fails unexpectedly:

1. open `/demo`
2. click `CASE SG`
3. run the live fill

Low-risk fallback after that:

1. refresh `/demo`
2. click `httpbin.org`
3. run the scan/fill flow

## Do Not Demo

- dashboard
- upload / OCR
- verify page
- unknown-company discovery
- B2B
- scheduler
- real outbound email sending
- any `/api/v5` claim
- any “production-ready” claim

## Pre-Demo Checks

- `/chat?new=1` loads
- the Shopee complaint reaches strategy
- `Open CASE Singapore flow` appears
- `/live-fill` completes
- the final screen shows a CASE reference number
