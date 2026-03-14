# Grippyyy Demo Operator Guide

## Start

Run:

```bash
./scripts/start_demo_session.sh
```

That starts the app, seeds the CASE demo profile, and opens fresh demo tabs.

## Exact Demo Path

Use this sequence only:

1. `/`
2. `/chat?new=1`
3. enter: `Shopee Singapore sold me a defective laptop and they are refusing a refund.`
4. click `Open CASE Singapore flow`
5. wait for `/live-fill` to complete
6. stop on the success card with the reference number visible

## What To Say

Opening:

> Filing complaints is fragmented and bureaucratic.
>
> Grippyyy is meant to understand the complaint, choose the right path, and then execute it.

Positioning:

> What I’m showing is the real working core, not the full production wrapper.

During the chat step:

> Grippyyy is deciding the right path first, not just filling forms blindly.

During the filing step:

> This is a live filing run on a real public complaint portal.

Close:

> What is working here is real: complaint understanding, path selection, and live execution.

## Do Not Click

- `/dashboard`
- upload / OCR
- `/verify`
- B2B routes
- scheduler routes
- auth/profile admin surfaces
- anything framed as `/api/v5`

## If Something Goes Wrong

If `/chat` stalls:
- wait a few seconds

If `/chat` fails:
- refresh `/chat?new=1`
- rerun only the Shopee complaint path

If the CASE run fails:
- open `/demo`
- click `CASE SG`
- rerun the live fill from the fallback surface

If that also fails:
- use the `httpbin.org` fallback in `/demo`

## Stop

Run:

```bash
./scripts/stop_demo_session.sh
```
