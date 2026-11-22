# tequity-sync

Tequity Round 1 exercise — **two-way, field-level sync of customer and contact records** between a
client's Salesforce CRM and our Postgres-backed portal.

We build **our side only**: a webhook endpoint (Salesforce → us) and a sync worker (us → Salesforce
record API). Salesforce stays as-is and is represented by a **fake CRM we wrote** (the brief requires
this). The sync is **field-level**: finance's `city` edit survives sales' later `phone` edit, and every
earlier value — with who set it and when — is kept in an append-only history.

| Read this | For |
|---|---|
| [`DESIGN.md`](DESIGN.md) | The formal design — the 12-point outline, the four rules, the outage arithmetic |
| [`WALKTHROUGH.md`](WALKTHROUGH.md) | The 10-minute presentation script |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | The reasoning trail: locked assumptions (with flip-notes) + the Salesforce API research |
| [`docs/brief.html`](docs/brief.html) | The original brief |

## The four rules and where each is proven

| Rule | In code | Proven by |
|---|---|---|
| Duplicate webhook applied once | `inbox` PK + `applyInboundWebhook` | `test_inbound.py`, `demo.py` RULE 1, cockpit "duplicate" |
| Out-of-order kept but not current | `resolveWrite` on `occurredAt` | `test_inbound.py`, `demo.py` RULE 2, cockpit "out-of-order" |
| Our write doesn't loop back | integration-user rule + inbound-never-enqueues | `test_backfill.py` (end-to-end), `demo.py` RULE 3 |
| Pending calls survive a restart | durable `outbox` + `requeueStaleInflight` | `test_worker.py`, `demo.py` RULE 4 |

## Quick start

### Option A — zero-infra proof of the four rules (SQLite, no Docker)

```bash
cd backend
uv venv --python 3.12 .venv && uv pip install -r requirements.txt   # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest          # the full test suite
.venv/bin/python scripts/demo.py    # the four rules + backfill, end-to-end, self-verifying
```

`demo.py` is headless, deterministic, and offline — it drives the fake CRM through all four rules and
prints `ALL SCENARIOS PASSED`, with the rate cap visibly pacing the restart drain.

### Option B — full stack on Postgres + the React cockpit

```bash
docker compose up --build        # Postgres + backend (fake Salesforce runs in-process) on :8000
# in another terminal:
cd frontend && npm install && npm run dev   # UI on :5173, proxies the API to :8000
```

Open http://localhost:5173. Left panel is the portal (current record, a field-level edit form, the
history timeline). Right panel is a **demo cockpit** that drives the fake Salesforce so you can trigger
every rule live: take Salesforce down/up, run backfill, simulate a sales edit with its webhook
delivered or **dropped**, and fire duplicate / out-of-order probes. The status bar shows the outbox
depth and oldest-pending age (the watch metrics). `SALESFORCE_RATE_CAP` is set low (5/s) in compose so
the drain is visibly paced.

## Layout

```
DESIGN.md  WALKTHROUGH.md  docs/            the documents
docker-compose.yml                          Postgres + backend
backend/
  app/        config, db, models, rules, sync, worker, ratelimit, fakeSalesforce, backfill, main
  tests/      pytest per flow (rules, inbound, outbound, worker, backfill, api)
  scripts/    demo.py (the four-rule demo)
frontend/     Vite + React portal + cockpit
```

## Conventions

- **camelCase everywhere**, including Python identifiers and DB columns, for parity with the webhook
  wire format (`eventId`, `occurredAt`, `changedFields`). This is a deliberate deviation from PEP8.
- Docstrings on every module, function, and endpoint; each endpoint states why it exists.
- One commit per feature; messages carry Feature / Tested / Risks-future.

## Deviations from the brief (deliberate — see `docs/DECISIONS.md`)

1. **Backend in Python/FastAPI** instead of the prescribed TypeScript/Node.
2. **A minimal React UI**, though the brief lists a visual UI as out of scope — to make the rules
   observable in a live demo.
3. **camelCase everywhere** (incl. Python), deviating from PEP8.

The brief mandates a fake CRM; the appendix in `DESIGN.md` records how a real Salesforce Developer org
would drop into the existing `crm` interface seam later.

## Verified

`pytest` (33 tests) green on SQLite; the same service layer verified end-to-end on Postgres (dialect
`SKIP LOCKED` path + timestamp handling); `demo.py` passes all four rules; `npm run build` compiles the
UI; and the live API was smoke-tested over HTTP (`uvicorn` + `curl`).
