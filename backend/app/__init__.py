"""Tequity two-way sync service (Salesforce <-> Postgres portal).

See DESIGN.md for the full design. Naming is camelCase throughout (including Python
identifiers and DB columns) for parity with the webhook wire format; this is a
deliberate deviation from PEP8, documented in docs/DECISIONS.md.
"""
