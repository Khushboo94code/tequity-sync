import React, { useCallback, useEffect, useState } from 'react'
import * as api from './api.js'

// The contact fields this exercise cares about.
const CONTACT_FIELDS = ['fullName', 'phone', 'email', 'city', 'owner']
const SALES_ACTOR = 'sales.user@client.example'

// A monotonic event id for cockpit webhooks, so duplicates can be sent on purpose.
let eventSeq = 1
const nextEventId = () => `ui_evt_${eventSeq++}`
const nowIso = () => new Date().toISOString()

// ---------------------------------------------------------------------------- auth screen

function AuthScreen({ onAuthed }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('finance@client.example')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    setError('')
    const res = await (mode === 'login' ? api.login(email, password) : api.signup(email, password))
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      setError(body.detail || `${mode} failed`)
      return
    }
    const data = await res.json()
    api.setToken(data.token)
    onAuthed(data.email)
  }

  return (
    <div className="authwrap">
      <form className="card authcard" onSubmit={submit}>
        <h1>Tequity portal</h1>
        <p className="muted">
          {mode === 'login' ? 'Log in' : 'Create an account'} — your edits are attributed to you
          (the actor comes from your authenticated session, server-side).
        </p>
        <label className="row"><span>email</span>
          <input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" />
        </label>
        <label className="row"><span>password</span>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password" />
        </label>
        {error && <p className="err">{error}</p>}
        <button className="primary" type="submit">{mode === 'login' ? 'Log in' : 'Sign up'}</button>
        <button type="button" className="link"
          onClick={() => { setMode(mode === 'login' ? 'signup' : 'login'); setError('') }}>
          {mode === 'login' ? 'Need an account? Sign up' : 'Have an account? Log in'}
        </button>
      </form>
    </div>
  )
}

// ---------------------------------------------------------------------------- pieces

function CurrentState({ record }) {
  const fields = record?.fields || {}
  const names = Object.keys(fields)
  return (
    <div className="card">
      <h2>Current record — {record?.recordId || '—'}</h2>
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

// ---------------------------------------------------------------------------- portal

function Portal({ email, onLogout }) {
  const [records, setRecords] = useState([])
  const [recordId, setRecordId] = useState(null)
  const [record, setRecord] = useState(null)
  const [history, setHistory] = useState([])
  const [metrics, setMetrics] = useState({})
  const [draft, setDraft] = useState({})
  const [log, setLog] = useState([])

  const note = (m) => setLog((p) => [`${new Date().toLocaleTimeString()}  ${m}`, ...p].slice(0, 12))

  const refreshList = useCallback(async () => {
    const data = await api.listRecords()
    const list = data.records || []
    setRecords(list)
    setRecordId((prev) => prev || (list[0] && list[0].recordId) || null)
  }, [])

  const refreshSelected = useCallback(async () => {
    if (!recordId) { setRecord(null); setHistory([]); return }
    const [rec, hist] = await Promise.all([api.getRecord(recordId), api.getHistory(recordId)])
    setRecord(rec)
    setHistory(hist.history || [])
  }, [recordId])

  const refreshMetrics = useCallback(async () => setMetrics(await api.getMetrics()), [])

  useEffect(() => { refreshList() }, [refreshList])
  useEffect(() => { refreshSelected() }, [refreshSelected])
  useEffect(() => {
    const t = setInterval(() => { refreshSelected(); refreshMetrics(); refreshList() }, 1500)
    return () => clearInterval(t)
  }, [refreshSelected, refreshMetrics, refreshList])

  const save = async () => {
    const changed = Object.fromEntries(
      Object.entries(draft).filter(([k, v]) => !k.startsWith('__') && v !== '')
    )
    if (!recordId) return note('select a record first')
    if (Object.keys(changed).length === 0) return note('nothing to save')
    const res = await api.portalEdit(recordId, changed)
    note(`saved ${JSON.stringify(res.appliedFields)} as ${email}; enqueued ${res.enqueuedOutboxIds?.length ?? 0}`)
    setDraft({})
    refreshSelected()
    refreshList()
  }

  const salesEdit = async (deliver) => {
    if (!recordId) return note('select a record first')
    const field = draft.__sfField || 'phone'
    const value = draft.__sfValue || `+91${Math.floor(Math.random() * 10000)}`
    const changedBy = draft.__sfChangedBy || SALES_ACTOR
    await api.control('salesEdit', { recordId, fieldName: field, value, changedBy, deliver })
    note(`salesforce edit ${field}=${value} by ${changedBy} (${deliver ? 'webhook delivered' : 'webhook DROPPED'})`)
    refreshSelected()
  }

  const sendDuplicate = async () => {
    if (!recordId) return note('select a record first')
    const payload = {
      eventId: nextEventId(), recordId, objectType: 'Contact', occurredAt: nowIso(),
      changedBy: SALES_ACTOR, changedFields: { phone: '+91DUPLICATE' },
    }
    const a = await api.sendWebhook(payload)
    const b = await api.sendWebhook(payload)
    note(`duplicate test: first=${a.status}, second=${b.status}`)
    refreshSelected()
  }

  const sendOutOfOrder = async () => {
    if (!recordId) return note('select a record first')
    const res = await api.sendWebhook({
      eventId: nextEventId(), recordId, objectType: 'Contact',
      occurredAt: '2000-01-01T00:00:00.000Z', changedBy: SALES_ACTOR,
      changedFields: { phone: '+91ANCIENT' },
    })
    note(`out-of-order test: historyOnly=${JSON.stringify(res.historyOnlyFields)} (did not overwrite)`)
    refreshSelected()
  }

  const runBackfill = async () => {
    const res = await api.control('backfill')
    note(`backfill: applied=${res.appliedFields} skippedOwn=${res.skippedOwnWrites}`)
    refreshSelected()
    refreshList()
  }

  const toggleSalesforce = async () => {
    const res = await api.control(metrics.salesforceDown ? 'up' : 'down')
    note(`salesforce ${res.salesforceDown ? 'DOWN' : 'UP'}`)
    refreshMetrics()
  }

  return (
    <div className="wrap">
      <header>
        <h1>Tequity portal <span className="muted">— two-way sync demo</span></h1>
        <div className="statusbar">
          <span className="pill">signed in: <strong>{email}</strong></span>
          <button className="link" onClick={onLogout}>Log out</button>
          <span className={`pill ${metrics.salesforceDown ? 'bad' : 'good'}`}>
            Salesforce {metrics.salesforceDown ? 'DOWN' : 'up'}
          </span>
          <span className="pill">outbox pending: {metrics.outboxPending ?? '—'}</span>
          <span className="pill">oldest pending:&nbsp;
            {metrics.oldestPendingAgeSeconds == null ? '—' : `${metrics.oldestPendingAgeSeconds.toFixed(1)}s`}
          </span>
        </div>
      </header>

      <div className="layout">
        <aside className="card sidebar">
          <h2>Records</h2>
          {records.length === 0 ? (
            <p className="muted">None yet — simulate a Salesforce edit to create one.</p>
          ) : (
            <ul className="recordlist">
              {records.map((r) => (
                <li key={r.recordId}
                  className={r.recordId === recordId ? 'selected' : ''}
                  onClick={() => { setRecordId(r.recordId); setDraft({}) }}>
                  <strong>{r.displayName}</strong>
                  <span className="muted"> · {r.recordId}</span>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <div className="grid">
          <div className="col">
            <CurrentState record={record} />

            <div className="card">
              <h2>Edit in the portal</h2>
              <p className="muted">
                Saved as <strong>{email}</strong> (from your session) and stamped with server time —
                then enqueued to Salesforce.
              </p>
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
              <button className="primary" onClick={save} disabled={!recordId}>Save edit</button>
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
              <label className="row"><span>changed by</span>
                <input value={draft.__sfChangedBy || ''} placeholder={SALES_ACTOR}
                  onChange={(e) => setDraft({ ...draft, __sfChangedBy: e.target.value })} />
              </label>
              <button onClick={() => salesEdit(true)}>Sales edit → deliver webhook</button>
              <button onClick={() => salesEdit(false)}>Sales edit → DROP webhook (outage loss)</button>

              <h3>Rule probes</h3>
              <button onClick={sendDuplicate}>Send a duplicate webhook (x2)</button>
              <button onClick={sendOutOfOrder}>Send an out-of-order (ancient) webhook</button>
            </div>

            <div className="card">
              <h2>Activity</h2>
              <ul className="log">{log.map((l, i) => <li key={i}>{l}</li>)}</ul>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------- root

export default function App() {
  const [email, setEmail] = useState(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!api.getToken()) { setReady(true); return }
    // Validate a stored token on load; drop it if the server rejects it.
    api.me()
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d) setEmail(d.email); else api.setToken(null) })
      .finally(() => setReady(true))
  }, [])

  if (!ready) return null
  if (!email) return <AuthScreen onAuthed={setEmail} />
  return <Portal email={email} onLogout={() => { api.setToken(null); setEmail(null) }} />
}
