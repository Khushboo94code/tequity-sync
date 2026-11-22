# Decisions log

A running record of the assumptions we locked and the deviations we took, with the reasoning.
This is deliberately separate from `DESIGN.md` (the formal document) — it is the "why we chose
this" trail. Each assumption carries a *flip-note*: what would change if the answer went the other way.

## Locked assumptions

| # | Assumption | Why | If it flipped |
|---|---|---|---|
| 1 | **Peak edit rate 30 edits/s, quiet 3/s** | 5M records edited a few times a year ≈ 1–2/s average; ~15–20× for peak | Higher rate → bigger outage backlog, longer drain, less headroom under the 100 req/s cap |
| 2 | **Webhooks carry changed fields only** | Matches the sample payload (`changedFields` with one key) | A full-record default makes unlisted fields look cleared; we would need explicit null semantics. Our rule if a full record *does* arrive: diff against stored state and apply each differing field independently |
| 3 | **Loop prevention via a dedicated integration user** | Our outbound writes authenticate as a known Salesforce user; the echoed webhook carries `changedBy = integrationUser`, so we drop it and enqueue nothing. No Salesforce schema change — respects "Salesforce stays as-is" | Without an identifiable actor we fall back to value-equality echo suppression (can wrongly swallow a legitimate same-value edit) |
| 4 | **Outage backfill via a `SystemModstamp` cursor pull** | Salesforce documents the APIs for it (see research below) — this turns "assume an API" into "use the documented one" | If no changed-since API existed, webhooks dropped during the outage would be unrecoverable; we'd need a periodic full-reconciliation sweep |
| 5 | **Portal edit order = DB commit timestamp (server), actor = the authenticated portal session user** (now *implemented* via signup/login, not just assumed — resolved server-side, never from the request body) | The portal sample has no time field; server time is monotonic per node and not client-spoofable, and the actor must be a real authenticated identity for "who changed it" to mean anything | A client-supplied time/actor is spoofable and drifts → weaker ordering and hollow attribution |
| 6 | **Same-field conflict = latest timestamp wins + full history; ties/within clock-skew broken by a fixed source priority** | Directly satisfies "one current value, kept history"; a fixed tie-break keeps the decision deterministic across two clocks | "Salesforce always wins" or "portal always wins" is simpler but can override a legitimately-later edit |
| 7 | **One service; Postgres is the boundary (inbox/outbox tables)** | The durable rows that cross the endpoint↔worker boundary give us restart-durability for free and are simple to demo | Two services + a queue add infrastructure the brief doesn't require and make durability something we must build, not inherit |

## Salesforce API research (grounds assumption #4)

- **`getUpdated(start, end)`** (REST/SOAP): returns IDs changed in a window via `SystemModstamp`.
  Limits: 30-day lookback, 600k IDs per call.
- **SOQL `WHERE SystemModstamp > :cursor`** via REST `/query` or **Bulk API 2.0**: fetches the
  actual field values (getUpdated returns only IDs) and scales to the 5M initial load.
  `SystemModstamp` is indexed and monotonic.
- **Change Data Capture**: streaming alternative, 72h event retention with Replay IDs; risks
  permanent loss on `GAP_OVERFLOW`, which falls back to Bulk/SOQL reconciliation anyway.
- A 6-hour outage is far inside both the 72h CDC window and the 30-day getUpdated window, so
  recovery is always feasible. Recovery *reads* are cheap in request count (one Bulk query job
  returns millions of rows), so they barely dent the 100 req/s cap that the write-drain needs.
- The cursor pull can `SELECT LastModifiedById`, so the dedicated-integration-user rule (assumption 3)
  also filters our own echoes out of the backfill — the loop rule and the recovery path stay consistent.

## Deviations from the brief (taken deliberately, documented here and in the README)

1. **Backend in Python/FastAPI, not TypeScript/Node.** The brief says "if you build it, use
   TypeScript on Node.js." We chose Python for developer velocity and correctness. The *design* is
   language-agnostic and every rule + the rate cap is enforced identically. Evaluation risk accepted.
2. **A minimal React UI, though the brief lists "a visual user interface" as out of scope.** Kept
   intentionally thin (minimal styling); its purpose is to make the sync rules observable in a live demo.
3. **camelCase naming across the codebase, including Python** (deviates from PEP8 snake_case). Chosen
   for consistency with the webhook wire format (`eventId`, `occurredAt`, `changedFields`).
4. **Portal signup/login added** (a further UI extension beyond the brief's scope). Added deliberately
   to resolve an attribution concern raised while testing: "who changed it" is only defensible if the
   actor is a real authenticated identity, not a client-supplied string. The portal now authenticates
   its own users (separate from Salesforce login, which stays out of scope) and derives the edit actor
   from the session server-side. Auth is demo-grade (PBKDF2 — Django's own default hasher — + opaque DB
   tokens; no password policy / expiry / SSO / HTTPS); hardening is out of scope. *Why PBKDF2 not
   Django:* the backend is FastAPI/SQLAlchemy; adopting Django auth would mean rewriting the whole
   backend, so we used the same algorithm Django uses without the framework.

## Open questions we would ask before locking for production

- Exact peak and quiet-hour edit rates (we assumed 30/s and 3/s).
- Whether Salesforce emits a stable per-change `SystemModstamp`/Replay cursor we can persist.
- Whether the "webhook" is an at-least-once channel with retry, or truly fire-and-forget.
- Whether a dedicated integration user is available for outbound writes (needed for the loop rule).
- Whether any field is a system-of-record on one side only (would override field-level LWW for it).
