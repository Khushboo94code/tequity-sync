# tequity-sync

Tequity Round 1 exercise — **two-way, field-level sync of customer and contact records** between a
client's Salesforce CRM and our Postgres-backed portal.

- The brief is in [`docs/brief.html`](docs/brief.html).
- The formal design is in [`DESIGN.md`](DESIGN.md) *(in progress)*.
- The reasoning trail and locked assumptions are in [`docs/DECISIONS.md`](docs/DECISIONS.md).
- A 10-minute walkthrough script is in [`WALKTHROUGH.md`](WALKTHROUGH.md) *(in progress)*.

## What this is

We build **our side only**: a webhook endpoint (Salesforce → us) and a sync worker (us → Salesforce
record API). Salesforce stays as-is and is represented in code by a **fake CRM** we wrote.

The sync is **field-level**: finance's `city` edit survives sales' later `phone` edit, and every
earlier value is kept in an append-only history.

## Deviations from the brief (deliberate — see `docs/DECISIONS.md`)

1. **Backend in Python/FastAPI** instead of the prescribed TypeScript/Node.
2. **A minimal React UI**, though the brief lists a visual UI as out of scope — to make the rules
   observable in a live demo.
3. **camelCase everywhere**, including Python (deviates from PEP8), for parity with the webhook wire
   format (`eventId`, `occurredAt`, `changedFields`).

## Running it

> Full instructions land as the code does. Target: `docker compose up` for Postgres + backend + fake
> Salesforce, `cd frontend && npm run dev` for the UI, and `python backend/scripts/demo.py` for the
> headless four-rule demo.

## Status

Scaffolding. See the task list / commit history for progress.
