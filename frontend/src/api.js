// Tiny fetch wrapper for the backend. All URLs are relative and proxied to :8000 (see vite.config).

const asJson = (res) => res.json()

export const getRecord = (recordId) => fetch(`/records/${recordId}`).then(asJson)

export const getHistory = (recordId) => fetch(`/records/${recordId}/history`).then(asJson)

export const getMetrics = () => fetch('/metrics').then(asJson)

export const portalEdit = (recordId, changedFields, editedBy) =>
  fetch(`/portal/records/${recordId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ objectType: 'Contact', editedBy, changedFields }),
  }).then(asJson)

export const sendWebhook = (payload) =>
  fetch('/webhooks/salesforce', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(asJson)

export const control = (path, body) =>
  fetch(`/__control/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  }).then(asJson)
