"""Shared test scaffolding: one throwaway database, one correct reset.

Every test module used to keep its own list of tables to clear, which meant the
lists drifted apart. A module that forgot kalshi_alias would leave rows pointing
at teams the next module deleted, and the FK error surfaced as "database is
locked" because unittest skips tearDown when setUp raises -- so one leaked
connection locked every test after it. The order below is the single place that
knowledge lives.
"""
import os
import tempfile

# Must be set before lib.db is imported anywhere: DB_PATH is read at import time.
os.environ.setdefault("GRIDIRON_DB",
                      os.path.join(tempfile.mkdtemp(prefix="gridiron-tests-"), "test.db"))

# Children before parents. Anything referencing game or team must precede them.
TABLES = (
    "bankroll_event",     # -> bet
    "bet",                # -> game, team
    "kalshi_snapshot",    # -> game, team
    "kalshi_alias",       # -> team
    "scan_log",           # -> game
    "prediction",         # -> game, team
    "rating_history",     # -> team, game
    "rating_current",     # -> team, game
    "odds_snapshot",      # -> game
    "game",               # -> team
    "team",
)


def reset(conn):
    """Empty every table, in an order the foreign keys accept."""
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
