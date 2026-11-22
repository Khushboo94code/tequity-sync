"""Central configuration.

Every knob the design depends on lives here so the numbers (rate cap, clock-skew
window) and the identities (the integration user the loop rule keys on) are in one
place and overridable by environment variable for a demo.
"""
import os


class Config:
    """Runtime configuration, read from the environment with safe defaults.

    Attributes:
        databaseUrl: SQLAlchemy URL. Defaults to a local SQLite file so the demo and
            tests run with zero infrastructure; production sets DATABASE_URL to Postgres.
        salesforceRateCap: Outbound requests/second cap to the Salesforce record API.
            The design states 100; we run under it. A demo may set a low value so the
            drain is visibly paced.
        integrationUser: The dedicated Salesforce user our outbound writes authenticate
            as. The inbound loop rule drops any webhook whose changedBy equals this.
        clockSkewSeconds: Two writes whose timestamps fall within this window are treated
            as a tie and broken by sourcePriority rather than by raw timestamp.
        sourcePriority: Tie-break order; the earlier source in the tuple wins a tie.
            Salesforce wins ties by default (it is the sales system of record).
    """

    databaseUrl: str = os.environ.get("DATABASE_URL", "sqlite:///./tequitySync.db")
    salesforceRateCap: float = float(os.environ.get("SALESFORCE_RATE_CAP", "80"))
    integrationUser: str = os.environ.get("INTEGRATION_USER", "integration@tequity.example")
    clockSkewSeconds: float = float(os.environ.get("CLOCK_SKEW_SECONDS", "2"))
    sourcePriority: tuple = ("salesforce", "portal")
    # Seed a few demo contacts on startup so the record list isn't empty. Off by default so tests
    # stay isolated; the live demo sets SEED_DEMO=1.
    seedDemo: bool = os.environ.get("SEED_DEMO", "0") == "1"


config = Config()
