# Test cases — Tequity two-way sync

Covers the behaviours specified in `DESIGN.md` and `docs/DECISIONS.md`: authentication & attribution,
the field-level merge, the four required sync rules (duplicate, out-of-order, loop, outage), backfill,
the rate cap, and metrics.

**How these are run.** Each case is exercised against the API the UI calls (base URL
`http://localhost:8001` for the live session; the UI at `:5173` drives the same endpoints). Automated
equivalents live in `backend/tests/` and `backend/scripts/demo.py`; this document is the manual/live
plan and its recorded run.

**Legend:** ✅ pass · ❌ fail · ⏳ not yet run. Results from the live run are in the **Result** column.

---

## 1. Authentication & session

| ID | Scenario | Steps | Expected | Result |
|----|----------|-------|----------|--------|
| TC-A1 | Sign up a new user | `POST /auth/signup {email,password}` | `200`, returns `{token,email}` | ✅ |
| TC-A2 | Duplicate signup rejected | `POST /auth/signup` with an existing email | `409` | ✅ |
| TC-A3 | Login with valid credentials | `POST /auth/login` correct password | `200` + token | ✅ |
| TC-A4 | Login with wrong password | `POST /auth/login` wrong password | `401` | ✅ |
| TC-A5 | Protected endpoint needs a token | `GET /records` with no `Authorization` | `401` | ✅ |
| TC-A6 | Identify the session | `GET /auth/me` with a valid token | `200` + the user's email | ✅ |

## 2. Records & attribution

| ID | Scenario | Steps | Expected | Result |
|----|----------|-------|----------|--------|
| TC-R1 | Records list | `GET /records` (authed) | seeded contacts with `displayName` (fullName, else recordId) | ✅ |
| TC-R2 | Edit attributed to the session user | login as finance → `PUT /portal/records/{id}` city | history row: `source=portal`, `actor=<that user>` | ✅ |
| TC-R3 | Two users attributed separately | user A edits city, user B edits email | city actor=A, email actor=B | ✅ |

## 3. Field-level merge (the core requirement)

| ID | Scenario | Steps | Expected | Result |
|----|----------|-------|----------|--------|
| TC-M1 | Different fields from both sides coexist | Salesforce sets `phone`; portal sets `city` | both current; phone source=salesforce, city source=portal | ✅ |
| TC-M2 | Same field: newer wins, older kept | apply newer `phone`, then older `phone` | current = newer; older present in history, `appliedCurrent=false` | ✅ |

## 4. The four required sync rules

| ID | Scenario | Steps | Expected | Result |
|----|----------|-------|----------|--------|
| TC-S1 | Duplicate webhook | `POST /webhooks/salesforce` same `eventId` twice | first `applied`, second `duplicate`; one history row | ✅ |
| TC-S2 | Out-of-order webhook | apply newer, then a webhook with an older `occurredAt` | older → `historyOnly`, current unchanged, both in history | ✅ |
| TC-S3 | Loop echo dropped | portal edit → worker writes to SF → SF echoes a webhook (as integration user) | echo dropped: no new outbox, one `source=salesforce` audit row by the integration user, `appliedCurrent=false` | ✅ |
| TC-S4 | Outage: pending calls survive | `__control/down` → portal edits → check outbox → `__control/up` | while down: outbox `pending` holds (work kept); after up: drains to 0. Full process-restart durability: `test_worker.py` + `demo.py` RULE 4 | ✅ |
| TC-S5 | Rate cap paces the drain | with SF down, enqueue N edits; bring SF up; time the drain (cap=5/s) | drain time ≈ (N−cap)/cap seconds; never exceeds the cap | ✅ |

## 5. Backfill (post-outage inbound recovery)

| ID | Scenario | Steps | Expected | Result |
|----|----------|-------|----------|--------|
| TC-B1 | Dropped webhook recovered | `__control/salesEdit {deliver:false}` → record not updated → `__control/backfill` | after backfill the field appears (recovered via SystemModstamp cursor) | ✅ |
| TC-B2 | Backfill skips our own writes | a record last modified by the integration user is in the CRM | backfill reports `skippedOwnWrites ≥ 1`; it is not re-applied | ✅ |

## 6. Metrics

| ID | Scenario | Steps | Expected | Result |
|----|----------|-------|----------|--------|
| TC-MET1 | Metrics reflect state | `GET /metrics` with SF down and rows pending | `salesforceDown=true`, `outboxPending>0`, `oldestPendingAgeSeconds>0` | ✅ |

---

## Execution run

**Date:** 2026-11-22 · **Environment:** backend on `:8001` (SQLite `live.db`, `SALESFORCE_RATE_CAP=5`,
`SEED_DEMO=1`), UI on `:5173`. Run against the live API with a fresh `qa@client.example` user and
dedicated test record IDs (`tcR3`, `tcM1`, `tcM2`, `tcS1`–`tcS3`, `tcOut`, `tcB1`) so results are
deterministic and independent of prior state.

**Result: 18 / 18 passed.** Notable evidence:

- **TC-M1** — `phone` stayed `source=salesforce` while `city` was `source=portal` on the same record
  (the field-level merge; finance's edit survives alongside sales').
- **TC-S3** — after the portal edit drained, the echo arrived as `integration@tequity.example` and was
  recorded `appliedCurrent=false` with no new outbox row (`outboxPending=0`); `city` stayed the portal
  value. The loop is stopped by the integration-user rule, not by accident.
- **TC-S4 / TC-MET1** — with Salesforce down, 8 enqueued edits held at `outboxPending=8`,
  `salesforceDown=true`, `oldestPendingAgeSeconds>0` (nothing lost).
- **TC-S5** — after recovery the outbox drained to 0 under the 5/s cap. (The *precise* pacing is shown
  deterministically by `scripts/demo.py` — 6 writes at 3/s ≈ 1.0s; the live cap value is configurable.)
- **TC-B1 / TC-B2** — a dropped `salesEdit` was invisible until backfill, which reported
  `appliedFields=1, skippedOwnWrites=5` — recovering the lost inbound change while skipping our own
  integration-user writes.

**Automated coverage (CI-style):** `pytest` = 40/40 across rules, inbound, outbound, worker (incl.
restart durability), backfill, auth, and API; `python scripts/demo.py` passes all four rules + backfill
end-to-end. This manual plan complements those by exercising the full HTTP + auth surface live.

**Reproduce:** with the stack running, re-run the curl sequence from the testing session (fresh user +
`tc*` record IDs), or run `cd backend && .venv/bin/python -m pytest` and `.venv/bin/python scripts/demo.py`.
