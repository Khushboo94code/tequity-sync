# Two-way sync of customer and contact records — design

**Scope.** We build Tequity's side of a two-way sync between the client's Salesforce CRM and our
Postgres-backed portal. Salesforce exists and stays as-is. We build two things we own: a **webhook
endpoint** (Salesforce → us) and a **sync worker** (us → Salesforce record API).

**The one hard thing.** The sync is **field-level**, not record-level. Finance's `city` edit must
survive sales' later `phone` edit, and every earlier value — with who set it and when — stays on
record. That single requirement drives the data model, the conflict rule, and the history.

---

## 1. Assumptions, and the questions we would ask first

Each assumption has a **flip-note**: what changes if the real answer is different. (Full reasoning in
`docs/DECISIONS.md`.)

| # | Assumption | Flip-note |
|---|---|---|
| 1 | **Peak 30 edits/s, quiet 3/s** (5M records × a few edits/yr ≈ 1–2/s avg, ~15–20× peak). Used in all arithmetic. | Higher rate → larger outage backlog, longer drain, less headroom under the 100 req/s cap. |
| 2 | **Webhooks carry changed fields only** (matches the sample). | If a full record arrives, we diff it against stored state and apply each differing field independently; a naive "treat as full record" would clear unlisted fields. |
| 3 | **A dedicated Salesforce integration user exists for our outbound writes.** | It is the basis of the loop rule. Without it we fall back to value-equality echo suppression (weaker). |
| 4 | **Salesforce exposes a `SystemModstamp` changed-since query** (`getUpdated` + SOQL/Bulk API 2.0). | If not, webhooks dropped during an outage are unrecoverable and we need a periodic full-reconciliation sweep. |
| 5 | **Portal edit order = DB commit timestamp; actor = authenticated session.** | A client-supplied time is spoofable/drifts — a weaker ordering basis. |
| 6 | **Same-field conflict = latest timestamp wins + full history; ties broken by a fixed source priority (Salesforce wins ties).** | "One side always wins" is simpler but can override a legitimately-later edit. |
| 7 | **One service; Postgres is the boundary.** | Two services + a queue add infra the brief doesn't need and make durability something we build, not inherit. |

**Questions we would ask before locking for production**

- What are the real peak and quiet-hour edit rates?
- Is the webhook an at-least-once channel (with retry), or fire-and-forget? (We design for at-least-once + loss.)
- Does Salesforce give a stable, persistable change cursor (`SystemModstamp` / CDC Replay ID)?
- Is a dedicated integration user available for outbound writes?
- Is any field a system-of-record on one side only? (That field would bypass field-level LWW.)
- For two portal users editing the same field at once: is last-save-wins acceptable, or is a
  warning/merge UI required? (We assume last-save-wins by commit time.)

---

## 2. The systems, who calls whom, the boundary, the interface

Three parties, two directions, HTTPS only between them.

```
 Sales user                              Finance / customer-success user
     |                                                |
     | edits                                          | edits
     v                                                v
 Salesforce CRM                                  Tequity portal (our app)
 (their DB, stays as-is)                               |
     |                                                 v
     |                                           ┌─────────────────────────────┐
     |   (A) Salesforce → us                     │        OUR SERVICE          │
     |       HTTPS webhook                        │                             │
     +──────────────────────────────────────────▶│  webhook endpoint (inbound) │
     |                                            │           │                 │
     |                                            │           ▼                 │
     |                                            │   ┌──────────────────┐      │
     |                                            │   │   Postgres        │      │
     |                                            │   │  inbox / outbox   │  ◀── the boundary
     |                                            │   │  fieldState       │      │
     |                                            │   │  fieldHistory     │      │
     |                                            │   │  syncCursor       │      │
     |                                            │   └──────────────────┘      │
     |                                            │           │                 │
     |   (B) us → Salesforce                      │           ▼                 │
     |       HTTPS record API (create/update)     │   sync worker (outbound)    │
     ◀────────────────────────────────────────── │   + backfill on reconnect   │
           ≤ 100 req/s cap (our calls only)        └─────────────────────────────┘
```

**One service, two logical components** (assumption 7). The webhook endpoint and the sync worker run
in one deployable and share Postgres. **What crosses the boundary between them is durable rows in
Postgres** — never an in-memory handoff. The endpoint's only job is to *durably record* an event and
acknowledge; the worker's job is to *act* on durable rows. This is what makes "pending calls survive a
restart" true by construction.

**Interface on each side**

- **Inbound (we host):** `POST /webhooks/salesforce`. Accepts the sample webhook shape. Validates,
  deduplicates, applies, and *then* acknowledges `200`. Returns `2xx` only after the event is durably
  recorded, so Salesforce's retry (if any) is safe.
- **Outbound (Salesforce hosts, we call):** the documented record API — `create` and `update`. We
  also assume two read capabilities for recovery: `getUpdated(start, end)` and a SOQL/Bulk query on
  `SystemModstamp` (marked as assumption 4).
- **Portal (we host, the UI calls):** `PUT /portal/records/{recordId}` to save an edit;
  `GET /records/{recordId}` and `GET /records/{recordId}/history` to read.

---

## 3. Subproblems, in build order (riskiest first)

We name the subproblems first and add a component only when a subproblem needs one.

1. **Represent field-level state + history** (data model). *Riskiest* because every rule reads and
   writes it; getting the shape wrong forces rework everywhere. → tables `fieldState`, `fieldHistory`.
2. **Decide "which edit is current" deterministically** (the merge rule). The correctness core. →
   `rules.py` pure functions, no I/O, exhaustively testable.
3. **Inbound: apply a webhook safely** — dedup + out-of-order + loop drop. → webhook endpoint + `inbox`.
4. **Outbound: turn a portal edit into a durable, rate-limited Salesforce call.** → portal endpoint +
   `outbox` + worker + token bucket.
5. **Survive a 6-hour outage** — durability we mostly inherit from 3–4, plus backfill on reconnect. →
   worker retry/backoff + `syncCursor` + backfill pull.
6. **Make it observable** — the three numbers we watch.
7. **Make it demonstrable** — fake Salesforce + a headless 4-rule demo + a thin UI.

Why this order: 1–2 are the parts that are hard to change later and where a wrong call is silently
lossy. 3–4 are the two directions of traffic. 5 is a property that falls out of 1–4 done well, plus one
new piece (backfill). 6–7 are last because they observe/exercise what already exists.

---

## 4. The data we store

All timestamps are UTC. `ts` is the single comparable ordering value per write: `occurredAt` for a
Salesforce event, the DB commit time for a portal edit.

**`fieldState`** — the current value of each field. One row per (record, field). This is the
"one current value we can justify."
```
recordId      text     -- Salesforce record id, e.g. 0031A00000Abc
objectType    text     -- 'Customer' | 'Contact'
fieldName     text     -- 'phone' | 'city' | ...
currentValue  text
currentTs     timestamptz
currentActor  text     -- who set it
currentSource text     -- 'salesforce' | 'portal'
PRIMARY KEY (recordId, fieldName)
```

**`fieldHistory`** — append-only audit. Every accepted write lands here, winner or not. Nothing is
ever updated or deleted. This is the "earlier values, who, and when" the client needs.
```
id          bigserial PK
recordId    text
objectType  text
fieldName   text
value       text
ts          timestamptz
actor       text
source      text          -- 'salesforce' | 'portal'
eventId     text null     -- the webhook eventId when source='salesforce'
appliedCurrent bool       -- did this write become the current value?
createdAt   timestamptz default now()
```

**`inbox`** — idempotency + inbound durability. The `eventId` primary key *is* the duplicate-webhook
defense.
```
eventId     text PK
recordId    text
objectType  text
receivedAt  timestamptz default now()
status      text          -- 'processed'
payload     jsonb
```

**`outbox`** — the durable outbound queue. A portal edit is not "sent"; it is *recorded here* in the
same transaction that updates `fieldState`. The worker drains it. Because it is a table, a restart
cannot lose a pending call.
```
id            bigserial PK
recordId      text
objectType    text
fieldName     text
newValue      text
ts            timestamptz
actor         text
status        text        -- 'pending' | 'inflight' | 'done' | 'failed'
attempts      int default 0
nextAttemptAt timestamptz default now()
lastError     text null
createdAt     timestamptz default now()
```

**`syncCursor`** — the high-water mark for backfill after an outage.
```
objectType         text PK
lastSystemModstamp timestamptz
updatedAt          timestamptz
```

---

## 5. The inbound path (webhook → our database)

```
POST /webhooks/salesforce
  1. Validate shape (eventId, recordId, objectType, occurredAt, changedBy, changedFields).
  2. BEGIN TX
     a. INSERT INTO inbox (eventId, ...) ON CONFLICT (eventId) DO NOTHING.
        - 0 rows inserted  → duplicate. COMMIT, return 200. (Rule: duplicate webhook.)
     b. If changedBy == integrationUser → this is our own echo.
        - Record in fieldHistory as source='salesforce', appliedCurrent=false, do NOT touch
          fieldState, do NOT enqueue outbound. COMMIT, return 200. (Rule: loop.)
     c. For each (fieldName, value) in changedFields:
        - Look up fieldState[recordId, fieldName].
        - If no row, or occurredAt > currentTs  → UPSERT fieldState, append fieldHistory
          (appliedCurrent=true).
        - Else (occurredAt <= currentTs)         → append fieldHistory (appliedCurrent=false).
          (Rule: out-of-order — older loses the current slot but is never dropped.)
     d. COMMIT.
  3. Return 200 (only after commit).
```

Key properties: the endpoint does **no** outbound calls — an inbound change came *from* Salesforce,
so nothing needs to be sent back. All writes for one webhook are one transaction, so a crash mid-apply
leaves nothing half-done; Salesforce re-delivery (or our backfill) re-runs it idempotently via `inbox`.

---

## 6. The outbound path (portal edit → Salesforce API call)

```
PUT /portal/records/{recordId}   body: { objectType, editedBy, changedFields }
  1. ts := DB commit time (server);  actor := authenticated session user (editedBy).
  2. BEGIN TX
     For each (fieldName, value) in changedFields:
       - Apply the SAME merge rule as inbound (section 7) against fieldState with ts.
         (A concurrent Salesforce edit with a newer ts can still win; the portal write
          is then history-only and we do NOT enqueue it.)
       - If the portal write wins current:
           * UPSERT fieldState (currentSource='portal')
           * append fieldHistory (appliedCurrent=true)
           * INSERT INTO outbox (pending)       ← durable; crosses the boundary
     COMMIT.
  3. Return the merged current record to the UI.

sync worker (loop):
  - Claim due pending rows: SELECT ... WHERE status='pending' AND nextAttemptAt <= now()
    FOR UPDATE SKIP LOCKED, mark 'inflight'.
  - Spend a token from the token bucket (cap, default 80/s under the 100 cap).
  - Call Salesforce create/update as the integrationUser.
      * 2xx        → status='done'.
      * 429 / 5xx  → status='pending', attempts++, nextAttemptAt = now()+backoff. Keep the work.
      * 4xx (hard) → status='failed', lastError set (alert; do not silently drop).
```

The worker is the **only** place we call Salesforce, so the 100 req/s cap is enforced in exactly one
token bucket — including retries. Salesforce will emit a webhook for our write; the inbound loop rule
(section 9) drops it.

---

## 7. The rule when both sides change the same record

**Per-field last-writer-wins by `ts`, with an append-only history.** The record is never the unit of
conflict — the *field* is.

- **Different fields** (sales edits `phone`, finance edits `city`): both are current. They touch
  different `fieldState` rows, so there is no conflict at all. This is the scenario's core requirement
  and it is satisfied structurally.
- **Same field, different times:** the write with the greater `ts` is current; the other is retained
  in `fieldHistory` with `appliedCurrent=false`. Nothing is lost.
- **Same field, equal `ts` or within the clock-skew window** (SF `occurredAt` and portal commit time
  come from different clocks): broken by a **fixed source priority — Salesforce wins ties** (it is the
  sales system of record). Deterministic and documented, so two nodes always decide the same way.

"The current value is the one we can justify": for any field we can point to the `fieldState` row and
the winning `fieldHistory` entry (actor, ts, source). "What the other portal user sees": after their
save, they see the merged current record returned by the API — if a newer edit won, they see that
value, with their own attempt visible in history.

---

## 8. The rule for a duplicate webhook, and for an out-of-order webhook

- **Duplicate** (Salesforce delivers the same webhook twice): `eventId` is the primary key of `inbox`.
  The second delivery inserts zero rows (`ON CONFLICT DO NOTHING`), we change no state, and we still
  return `200`. Idempotent by construction — safe no matter how many times it is re-delivered.
- **Out of order** (an older edit's webhook arrives after a newer one): we compare the webhook's
  `occurredAt` to `fieldState.currentTs`. If it is **not** newer, it does not become current — but it
  is still appended to `fieldHistory` (`appliedCurrent=false`). The current value stays correct and the
  history stays complete. The same comparison handles a *newer* event arriving first and an older one
  arriving later, symmetrically.

---

## 9. The rule that stops a loop

A portal edit → `outbox` → worker writes to Salesforce → **Salesforce emits a webhook for that very
change** → it arrives back at our endpoint. Left alone, that could re-apply and (if it re-enqueued)
bounce forever.

**Primary rule:** our outbound writes authenticate as a **dedicated integration user**. The echoed
webhook therefore carries `changedBy == integrationUser`. The inbound path detects this and treats it
as a no-op for state and for the outbox: history-only, nothing enqueued (section 5, step b). The write
crosses each boundary at most once, then stops.

**Why not re-enqueue anyway:** inbound changes *never* create outbox rows (section 5) — only portal
edits do. So even without the integration-user check, an applied inbound value is not sent back. The
integration-user rule is the primary, explicit guard; the "inbound never enqueues" invariant is the
structural backstop. **Secondary guard:** if an echo ever lacked the integration-user marker, a
value-equality check (incoming value == the value we last wrote for that field) still suppresses it.

---

## 10. The 6-hour Salesforce outage

**What is on disk.** Everything needed to resume is already in Postgres before the outage and survives
a process restart:
- `outbox` — every portal edit we could not send is a `pending` row. The worker's `FOR UPDATE SKIP
  LOCKED` claim means an in-flight row whose process died returns to `pending` (its TX rolled back),
  so nothing is lost or double-sent on restart.
- `fieldState` / `fieldHistory` — portal edits were applied locally at save time; the outage only
  blocks *sending* them onward.
- `syncCursor` — the high-water `SystemModstamp` of inbound changes we have already applied.

**Two gaps to heal on reconnect.**
1. *Outbound backlog:* drain the `outbox` (below).
2. *Inbound loss:* webhooks Salesforce tried to send during the outage may never arrive. We recover
   them with a **backfill pull**: `getUpdated(cursor, now)` for changed IDs, then a SOQL/Bulk query on
   `SystemModstamp > cursor` for the values, apply each through the same inbound merge (so dedup and
   out-of-order rules still hold), and advance `syncCursor`. The query selects `LastModifiedById`, so
   our own writes (integration user) are filtered out — the loop rule holds through backfill too.

**The arithmetic (show the division).** Assumed peak = **30 edits/s**; window = 6 h = **21,600 s**.

- Worst case — treat the whole peak as outbound for the entire window:
  `30 × 21,600 = 648,000` queued writes.
- Drain at **80 req/s** (20% headroom under the 100 cap for retries + the backfill reads):
  `648,000 ÷ 80 = 8,100 s = 135 min ≈ 2.25 h` — if no new edits arrived during the drain.
- Realistic — new edits keep arriving at 30/s while we drain, so the net drain rate is `80 − 30 = 50/s`:
  `648,000 ÷ 50 = 12,960 s = 216 min ≈ 3.6 h`.
- Sensitivity: a 50/50 inbound/outbound split halves the backlog (`324,000 ÷ 80 = 4,050 s ≈ 68 min`);
  an outage confined to quiet hours (3/s) is ~1/10th (`64,800 ÷ 80 ≈ 13.5 min`). The **inbound**
  recovery is a handful of Bulk query jobs (millions of rows per job), so in *request count* it is
  negligible against the cap — the write-drain dominates the catch-up time.

Conclusion: a 6-hour outage at peak is recoverable in well under 4 hours with 20% cap headroom; if
catch-up time must be bounded tighter, raise the drain toward the 100 cap or shard across more
integration users (each with its own cap) — a known lever, out of scope to build this weekend.

---

## 11. Three numbers we watch (and when we call someone)

| Metric | Why it matters | Page when |
|---|---|---|
| **Outbox depth & oldest-pending age** | Rising depth / old pending = Salesforce unreachable or we're falling behind the edit rate | oldest `pending` > **5 min**, or depth climbing for > 10 min while SF is reachable |
| **End-to-end sync lag (p95)** | This is the 30-second SLA, measured from `ts` to applied-on-the-other-side | p95 > **30 s** sustained for 5 min (both systems up) |
| **Salesforce 429 / 5xx / hard-4xx rate** | 429 = we're at the cap; 5xx = outage onset; hard-4xx = writes being rejected (data/perms) | any `failed` (hard-4xx) row, or 5xx > threshold for 5 min |

(Secondary, worth dashboarding: inbox dedup rate, loop-drop count, backfill rows pulled per reconnect.)

---

## 12. What we are leaving out of this weekend

- **Deletes** (the brief excludes them) — our model is upsert/append only.
- **Salesforce auth/login screens** and real OAuth — the integration user is assumed/configured.
- **The one-time 5M initial backfill** — same cursor/Bulk mechanism as outage recovery; we describe it,
  we don't run it.
- **A second region / HA topology**, and any load test against real Salesforce (we use a fake CRM).
- **Sharding across multiple integration users** to exceed one 100 req/s cap — named as a lever only.
- **Hardening**: webhook signature verification is stubbed; no rate-limiting of inbound, no authn on
  the portal endpoints beyond a passed actor. Production would add all three.
- **Rich UI**: the React app is a thin demo cockpit, not the real portal.
