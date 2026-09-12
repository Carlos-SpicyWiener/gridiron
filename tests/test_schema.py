"""The betting tables. Spec §2.

Runs against a throwaway database — GRIDIRON_DB is redirected before lib.db is
imported, so this can never touch data/gridiron.db.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="gridiron-test-")
os.environ["GRIDIRON_DB"] = os.path.join(_TMP, "test.db")

from lib import db  # noqa: E402  (must follow the env redirect)


class SchemaTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()

    def columns(self, table):
        return {r["name"]: r for r in
                self.conn.execute(f"PRAGMA table_info({table})").fetchall()}


class TablesExist(SchemaTestCase):

    def test_creates_every_betting_table(self):
        got = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        for table in ("bankroll_event", "kalshi_snapshot", "kalshi_alias", "bet", "scan_log"):
            with self.subTest(table=table):
                self.assertIn(table, got)

    def test_init_is_idempotent(self):
        """schema.sql is re-run on every init; a second run must not raise."""
        db.init()
        db.init()


class BetColumns(SchemaTestCase):

    def test_stores_raw_and_calibrated_probability_side_by_side(self):
        """report calibration has to be able to grade the correction itself."""
        cols = self.columns("bet")
        self.assertIn("model_prob_raw", cols)
        self.assertIn("model_prob", cols)
        self.assertIn("calib_model", cols)

    def test_records_which_fee_model_priced_it(self):
        self.assertIn("fee_model", self.columns("bet"))

    def test_close_price_carries_its_snapshot_time(self):
        """A stale close must be visible, not silent."""
        cols = self.columns("bet")
        self.assertIn("close_price", cols)
        self.assertIn("close_snapshot_at", cols)

    def test_market_prob_is_nullable(self):
        """42% of open predictions have no observed line. NULL is honest."""
        self.assertEqual(self.columns("bet")["market_prob"]["notnull"], 0)

    def test_model_prob_is_not_nullable(self):
        self.assertEqual(self.columns("bet")["model_prob"]["notnull"], 1)

    def test_settlement_value_can_express_a_tie(self):
        self.assertIn("settlement_value", self.columns("bet"))


class Bankroll(SchemaTestCase):

    def test_balance_is_the_sum_of_events(self):
        self.conn.execute("DELETE FROM bankroll_event")
        for delta, reason in ((100.0, "deposit"), (25.5, "deposit"), (-30.25, "withdrawal")):
            self.conn.execute(
                "INSERT INTO bankroll_event (ts, delta, reason) VALUES (?, ?, ?)",
                (db.now(), delta, reason))
        balance = self.conn.execute(
            "SELECT COALESCE(SUM(delta), 0.0) AS b FROM bankroll_event").fetchone()["b"]
        self.assertAlmostEqual(balance, 95.25, places=6)

    def test_an_empty_ledger_is_zero_not_null(self):
        self.conn.execute("DELETE FROM bankroll_event")
        balance = self.conn.execute(
            "SELECT COALESCE(SUM(delta), 0.0) AS b FROM bankroll_event").fetchone()["b"]
        self.assertEqual(balance, 0.0)


class KalshiSnapshot(SchemaTestCase):

    def _game(self):
        """A minimal team/game pair to hang a snapshot off."""
        t = self.conn.execute(
            "INSERT INTO team (league, espn_id, name, tier, first_seen) "
            "VALUES ('nfl', ?, 'Test', 'major', ?)",
            (f"t{id(self)}", db.now())).lastrowid
        g = self.conn.execute(
            "INSERT INTO game (league, espn_id, season, season_type, kickoff_utc, "
            "home_team_id, away_team_id, status, fetched_at) "
            "VALUES ('nfl', ?, 2026, 2, ?, ?, ?, 'scheduled', ?)",
            (f"g{id(self)}", db.now(), t, t, db.now())).lastrowid
        return t, g

    def test_rejects_a_duplicate_quote_for_the_same_ticker_and_time(self):
        """Append-only, but the same poll must not double-write."""
        t, g = self._game()
        stamp = db.now()
        args = (g, t, "KXNFLGAME-26SEP13TESTEST", stamp, 0.60, 0.61, 0.605, 1000.0)
        sql = ("INSERT INTO kalshi_snapshot (game_id, team_id, market_ticker, fetched_at, "
               "yes_bid, yes_ask, mid, open_interest) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
        self.conn.execute(sql, args)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, args)

    def test_keeps_open_interest_because_the_gate_reads_it(self):
        self.assertIn("open_interest", self.columns("kalshi_snapshot"))


class KalshiAlias(SchemaTestCase):

    def test_one_team_per_abbreviation_per_league(self):
        t, _ = KalshiSnapshot._game(self)
        sql = ("INSERT INTO kalshi_alias (league, kalshi_abbrev, team_id, first_seen) "
               "VALUES ('nfl', 'ZZZ', ?, ?)")
        self.conn.execute(sql, (t, db.now()))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (t, db.now()))


if __name__ == "__main__":
    unittest.main()
