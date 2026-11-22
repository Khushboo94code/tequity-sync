"""Error types shared between the sync worker and the (fake) Salesforce client.

Modelling the two failure classes the brief cares about keeps the retry policy explicit:
transient failures keep the work and back off; permanent failures stop and alert.
"""


class TransientError(Exception):
    """Retryable failure: HTTP 429, a 5xx, or Salesforce unreachable. Keep the work, back off."""


class PermanentError(Exception):
    """Non-retryable failure: a hard 4xx (bad data / permissions). Mark failed and alert."""
