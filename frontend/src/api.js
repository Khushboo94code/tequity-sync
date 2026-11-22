// Backend client. All URLs are relative and proxied to the API (see vite.config.js).
// A bearer token (from signup/login) is stored in localStorage and attached to portal calls.

const TOKEN_KEY = 'tequityToken'
export const getToken = () => localStorage.getItem(TOKEN_KEY)
export const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY))

const authHeaders = () => {
  const t = getToken()
  return t ? { Authorization: `Bearer ${t}` } : {}
}
const jsonAuth = () => ({ 'Content-Type': 'application/json', ...authHeaders() })
const asJson = (res) => res.json()

// Auth (return the raw response so callers can read status codes like 401/409).
export const signup = (email, password) =>
  fetch('/auth/signup', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
export const login = (email, password) =>
  fetch('/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
export const me = () => fetch('/auth/me', { headers: authHeaders() })

// Portal (authenticated).
export const listRecords = () => fetch('/records', { headers: authHeaders() }).then(asJson)
export const getRecord = (id) => fetch(`/records/${id}`, { headers: authHeaders() }).then(asJson)
export const getHistory = (id) => fetch(`/records/${id}/history`, { headers: authHeaders() }).then(asJson)
export const portalEdit = (id, changedFields) =>
  fetch(`/portal/records/${id}`, {
    method: 'PUT', headers: jsonAuth(),
    body: JSON.stringify({ objectType: 'Contact', changedFields }), // no actor — the server uses the session user
  }).then(asJson)

// Open (not portal-user actions).
export const getMetrics = () => fetch('/metrics').then(asJson)
export const sendWebhook = (payload) =>
  fetch('/webhooks/salesforce', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }).then(asJson)
export const control = (path, body) =>
  fetch(`/__control/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  }).then(asJson)
