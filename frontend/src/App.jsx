import React, { useCallback, useEffect, useState } from 'react'
import { control, getHistory, getMetrics, getRecord, portalEdit, sendWebhook } from './api.js'

// The contact fields this exercise cares about (DESIGN.md section: records).
const CONTACT_FIELDS = ['fullName', 'phone', 'email', 'city', 'owner']
const PORTAL_ACTOR = 'finance.user@client.example'
const SALES_ACTOR = 'sales.user@client.example'

// A monotonic-ish event id for cockpit webhooks, so duplicates can be sent on purpose.
let eventSeq = 1
const nextEventId = () => `ui_evt_${eventSeq++}`
const nowIso = () => new Date().toISOString()

function CurrentState({ record }) {
  const fields = record?.fields || {}
  const names = Object.keys(fields)
  return (
    <div className="card">
      <h2>Current record — {record?.recordId}</h2>
      {names.length === 0 ? (
        <p className="muted">No fields yet. Edit below, or simulate a Salesforce change.</p>
      ) : (
        <table>
          <thead>
            <tr><th>Field</th><th>Value</th><th>Set by</th><th>Source</th><th>When</th></tr>
          </thead>
          <tbody>
            {names.map((name) => (
              <tr key={name}>
                <td>{name}</td>
                <td><strong>{fields[name].value}</strong></td>
                <td>{fields[name].actor}</td>
                <td><span className={`tag tag-${fields[name].source}`}>{fields[name].source}</span></td>
                <td className="muted">{fields[name].ts?.replace('T', ' ').slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function History({ history }) {
  return (
    <div className="card">
      <h2>History (append-only)</h2>
      {history.length === 0 ? (
        <p className="muted">Nothing yet.</p>
      ) : (
        <ul className="history">
          {history.map((h, i) => (
            <li key={i} className={h.appliedCurrent ? 'current' : 'superseded'}>
              <span className={`tag tag-${h.source}`}>{h.source}</span>{' '}
              <strong>{h.fieldName}</strong> = {h.value}{' '}
              <span className="muted">by {h.actor} · {h.ts?.replace('T', ' ').slice(0, 19)}</span>{' '}
              {h.appliedCurrent ? <em className="ok">current</em> : <em className="muted">superseded</em>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default function App() {
  const [recordId, setRecordId] = useState('0031')
  const [record, setRecord] = useState(null)
  const [history, setHistory] = useState([])
  const [metrics, setMetrics] = useState({})
  const [draft, setDraft] = useState({})
  const [log, setLog] = useState([])

  const note = (msg) => setLog((prev) => [`${new Date().toLocaleTimeString()}  ${msg}`, ...prev].slice(0, 12))

  const refresh = useCallback(async () => {
    const [rec, hist, met] = await Promise.all([
      getRecord(recordId), getHistory(recordId), getMetrics(),
    ])
    setRecord(rec)
    setHistory(hist.history || [])
    setMetrics(met)
  }, [recordId])

  useEffect(() => { refresh() }, [refresh])
  // Light polling so the worker's background drain and inbound webhooks show up live.
  useEffect(() => {
    const t = setInterval(refresh, 1500)
    return () => clearInterval(t)
  }, [refresh])

  const saveDraft = async () => {
    const changed = Object.fromEntries(Object.entries(draft).filter(([, v]) => v !== ''))
    if (Object.keys(changed).length === 0) return note('nothing to save')
    const res = await portalEdit(recordId, changed, PORTAL_ACTOR)
    note(`portal save: applied=${JSON.stringify(res.appliedFields)} enqueued=${res.enqueuedOutboxIds?.length ?? 0}`)
    setDraft({})
    refresh()
  }

  const salesEdit = async (deliver) => {
    const field = draft.__sfField || 'phone'
    const value = draft.__sfValue || `+91${Math.floor(Math.random() * 10000)}`
    await control('salesEdit', { recordId, fieldName: field, value, changedBy: SALES_ACTOR, deliver })
    note(`salesforce edit ${field}=${value} (${deliver ? 'webhook delivered' : 'webhook DROPPED'})`)
    refresh()
  }

  const sendDuplicate = async () => {
    const payload = {
      eventId: nextEventId(), recordId, objectType: 'Contact', occurredAt: nowIso(),
      changedBy: SALES_ACTOR, changedFields: { phone: '+91DUPLICATE' },
    }
    const a = await sendWebhook(payload)
    const b = await sendWebhook(payload) // same eventId again
    note(`duplicate test: first=${a.status}, second=${b.status}`)
    refresh()
  }

  const sendOutOfOrder = async () => {
    const old = {
      eventId: nextEventId(), recordId, objectType: 'Contact',
      occurredAt: '2000-01-01T00:00:00.000Z', changedBy: SALES_ACTOR,
      changedFields: { phone: '+91ANCIENT' },
    }
    const res = await sendWebhook(old)
    note(`out-of-order test: historyOnly=${JSON.stringify(res.historyOnlyFields)} (did not overwrite)`)
    refresh()
  }

  const runBackfill = async () => {
    const res = await control('backfill')
    note(`backfill: applied=${res.appliedFields} skippedOwn=${res.skippedOwnWrites}`)
    refresh()
  }

  const toggleSalesforce = async () => {
    const res = await control(metrics.salesforceDown ? 'up' : 'down')
    note(`salesforce ${res.salesforceDown ? 'DOWN' : 'UP'}`)
    refresh()
  }

  return (
    <div className="wrap">
      <header>
        <h1>Tequity portal <span className="muted">— two-way sync demo</span></h1>
        <div className="statusbar">
          <label>Record&nbsp;
            <input value={recordId} onChange={(e) => setRecordId(e.target.value)} size={10} />
          </label>
          <span className={`pill ${metrics.salesforceDown ? 'bad' : 'good'}`}>
            Salesforce {metrics.salesforceDown ? 'DOWN' : 'up'}
          </span>
          <span className="pill">outbox pending: {metrics.outboxPending ?? '—'}</span>
          <span className="pill">oldest pending:&nbsp;
            {metrics.oldestPendingAgeSeconds == null ? '—' : `${metrics.oldestPendingAgeSeconds.toFixed(1)}s`}
          </span>
        </div>
      </header>

      <div className="grid">
        <div className="col">
          <CurrentState record={record} />

          <div className="card">
            <h2>Edit in the portal (finance / CS)</h2>
            <p className="muted">Saves stamp server time + the session actor, then enqueue to Salesforce.</p>
            {CONTACT_FIELDS.map((f) => (
              <label key={f} className="row">
                <span>{f}</span>
                <input
                  value={draft[f] ?? ''}
                  placeholder={record?.fields?.[f]?.value || ''}
                  onChange={(e) => setDraft({ ...draft, [f]: e.target.value })}
                />
              </label>
            ))}
            <button className="primary" onClick={saveDraft}>Save edit</button>
          </div>

          <History history={history} />
        </div>

        <div className="col">
          <div className="card cockpit">
            <h2>Demo cockpit <span className="muted">(drives the fake Salesforce)</span></h2>

            <h3>Outage</h3>
            <button onClick={toggleSalesforce}>
              {metrics.salesforceDown ? 'Bring Salesforce UP' : 'Take Salesforce DOWN'}
            </button>
            <button onClick={runBackfill}>Run backfill (recover dropped webhooks)</button>

            <h3>Simulate a Salesforce (sales user) edit</h3>
            <label className="row"><span>field</span>
              <select value={draft.__sfField || 'phone'}
                onChange={(e) => setDraft({ ...draft, __sfField: e.target.value })}>
                {CONTACT_FIELDS.map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
            </label>
            <label className="row"><span>value</span>
              <input value={draft.__sfValue || ''} placeholder="(random if blank)"
                onChange={(e) => setDraft({ ...draft, __sfValue: e.target.value })} />
            </label>
            <button onClick={() => salesEdit(true)}>Sales edit → deliver webhook</button>
            <button onClick={() => salesEdit(false)}>Sales edit → DROP webhook (outage loss)</button>

            <h3>Rule probes</h3>
            <button onClick={sendDuplicate}>Send a duplicate webhook (x2)</button>
            <button onClick={sendOutOfOrder}>Send an out-of-order (ancient) webhook</button>
          </div>

          <div className="card">
            <h2>Activity</h2>
            <ul className="log">
              {log.map((l, i) => <li key={i}>{l}</li>)}
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}
