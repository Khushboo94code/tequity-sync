# 10-minute walkthrough script

Order follows the brief's "what to submit": assumptions & open questions → diagram → rules
(conflict, order, loop, outage) → arithmetic. Timeboxed to 10 minutes; they stop me at 10.

---

### 0:00–2:00 — Assumptions & the questions still open

- The one hard requirement: **field-level** sync with history. Finance's `city` survives sales'
  `phone`. That drives everything.
- Seven locked assumptions (I'll name the three that matter most):
  - **30 edits/s peak** — every number downstream depends on it.
  - **Changed-fields-only webhooks** — and what we do if a full record ever arrives.
  - **A dedicated integration user** — this is how we break the loop.
- Open questions I'd ask before locking: real edit rates; is the webhook at-least-once or
  fire-and-forget; is there a persistable change cursor; is any field a one-side system-of-record.
- The one place I'd flag I pushed back: I did **not** assume the webhook retries — I designed the
  outage recovery as a **pull**, so correctness doesn't depend on Salesforce's delivery guarantees.

### 2:00–3:30 — The diagram: who calls whom

- Two directions, HTTPS only. **(A)** Salesforce → us is a **webhook** (we host the endpoint).
  **(B)** us → Salesforce is the **record API** (we call it, ≤ 100 req/s — the cap is on *our* calls).
- **One service, two components; Postgres is the boundary.** The endpoint only *durably records* and
  acks; the worker *acts* on durable rows. That's why pending calls survive a restart — for free.

### 3:30–7:00 — The four rules

- **Conflict (same record):** per-field last-writer-wins by a single `ts` (occurredAt for SF, DB
  commit time for portal). Different fields never collide. Same field → newer `ts` wins, loser kept in
  history. Ties within clock skew → fixed priority, **Salesforce wins ties**.
- **Duplicate webhook:** `eventId` is the primary key of the `inbox` table. Second delivery inserts
  zero rows, changes nothing, still returns 200. Idempotent.
- **Out of order:** compare `occurredAt` to the stored `currentTs`. Older doesn't become current but
  is still appended to history — the current value stays right, nothing is dropped.
- **Loop:** outbound writes run as the **integration user**; the echoed webhook carries
  `changedBy = integrationUser` → inbound drops it (history-only, no enqueue). Backstop: inbound
  changes never create outbox rows, so applied values are never sent back.

### 7:00–9:30 — The outage & arithmetic

- On disk before the outage: `outbox` (pending writes), `fieldState`/`fieldHistory`, `syncCursor`.
  Restart-safe via `FOR UPDATE SKIP LOCKED` (in-flight rows roll back to pending).
- Two gaps healed on reconnect: **drain the outbox**, and **backfill** inbound losses via a
  `SystemModstamp` cursor pull (getUpdated + Bulk/SOQL), applied through the same merge rules.
- Arithmetic: 6 h = 21,600 s × 30/s = **648,000** queued. Drain at 80/s (20% headroom) =
  **648,000 ÷ 80 = 135 min**. With new edits still arriving, net 50/s = **216 min ≈ 3.6 h**.
  Quiet-hour outage ≈ 13 min. Inbound recovery is a few Bulk jobs — negligible in request count.

### 9:30–10:00 — Close

- Three numbers I watch: outbox oldest-pending age (page > 5 min), sync lag p95 (page > 30 s),
  Salesforce 429/5xx/hard-4xx. 
- If I had code in front of them: run the four demo scenarios (duplicate, older-after-newer,
  loop-drop, restart-with-SF-down) and show the enforced cap.

### Likely questions to be ready for

- *Why Salesforce-wins-ties and not portal?* It's the sales system of record; it's a config flag, not
  a hard assumption — flip it per field if the client wants.
- *What if there's no integration user?* Fall back to value-equality echo suppression (documented,
  weaker).
- *What if the SystemModstamp query doesn't exist?* Then dropped webhooks are unrecoverable → add a
  periodic full-reconciliation sweep (assumption 4 flip-note).
- *Why Python, and a UI?* Deliberate, documented deviations — the design is language-agnostic; the UI
  is a demo cockpit, not the product.
