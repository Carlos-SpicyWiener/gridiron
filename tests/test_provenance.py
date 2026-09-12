"""Which bets §7 is allowed to grade itself on. Spec §2, §6, §7.

A bet the sizer would have declined cannot answer "is the sizer finding edge?",
but dropping such bets after seeing how they landed is cherry-picking. So
provenance is recorded when the bet is placed and never revised.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="gridiron-prov-")
os.environ["GRIDIRON_DB"] = os.path.join(_TMP, "test.db")

from lib import db  # noqa: E402
from lib import betting  # noqa: E402


class ProvenanceTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.conn = db.connect()
        self.conn.execute("DELETE FROM bet")
        self.team, self.game = self._game()

    def tearDown(self):
        self.conn.close()

    def _game(self):
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

    def _bet(self, provenance, close=None, price=0.64):
        return betting.record_bet(
            self.conn, league="nfl", game_id=self.game, side_team_id=self.team,
            market_ticker="KXNFLGAME-26SEP13TESTEST", contracts=10, price=price,
            model_prob_raw=0.73, model_prob=0.698, market_prob=0.70,
            notes="test", provenance=provenance, close_price=close)


class Column(ProvenanceTestCase):

    def test_bet_records_how_it_came_to_be_placed(self):
        self.assertIn("provenance",
                      {r["name"] for r in self.conn.execute("PRAGMA table_info(bet)")})

    def test_defaults_to_sized_so_a_forgotten_argument_is_not_silently_excluded(self):
        """The dangerous default is the one that quietly shrinks the graded pool."""
        cols = {r["name"]: r for r in self.conn.execute("PRAGMA table_info(bet)")}
        self.assertEqual(cols["provenance"]["dflt_value"], "'sized'")

    def test_rejects_an_unknown_provenance(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO bet (placed_at, league, game_id, side_team_id, market_ticker, "
                "contracts, price, fee_per_contract, fee_model, cost, model_prob_raw, "
                "model_prob, calib_model, edge, status, notes, provenance) "
                "VALUES (?, 'nfl', ?, ?, 'X', 1, 0.5, 0.03, 'f', 0.53, 0.7, 0.7, 'c', "
                "0.1, 'open', '', 'guesswork')",
                (db.now(), self.game, self.team))


class Grading(ProvenanceTestCase):
    """§7 criterion 3 reads the sized pool; the ledger reads everything."""

    def test_clv_pool_excludes_manual_bets(self):
        self._bet("sized", close=0.70)
        self._bet("manual", close=0.90)
        rows = betting.clv_pool(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provenance"], "sized")

    def test_a_manual_bet_still_counts_in_the_ledger(self):
        """Excluding it from grading must not hide it from the P&L."""
        self._bet("sized", close=0.70)
        self._bet("manual", close=0.90)
        self.assertEqual(len(betting.ledger(self.conn)), 2)

    def test_clv_pool_ignores_bets_with_no_close_captured(self):
        self._bet("sized", close=None)
        self.assertEqual(betting.clv_pool(self.conn), [])

    def test_a_manual_winner_cannot_flatter_the_clv_pool(self):
        """The whole point: a lucky off-model bet must not read as tool edge."""
        self._bet("sized", close=0.60, price=0.64)   # negative CLV
        self._bet("manual", close=0.99, price=0.64)  # huge CLV
        mean = betting.mean_clv(self.conn)
        self.assertLess(mean, 0.0)


class UnlockCriterion(ProvenanceTestCase):
    """§7.3: n >= 20 sized bets AND mean CLV above zero by at least one SE."""

    def _many(self, n, close, provenance="sized"):
        for _ in range(n):
            self._bet(provenance, close=close)

    def test_not_unlocked_below_twenty_sized_bets(self):
        self._many(19, close=0.80)
        self.assertFalse(betting.clv_criterion_met(self.conn))

    def test_manual_bets_do_not_count_toward_the_twenty(self):
        self._many(19, close=0.80)
        self._many(5, close=0.80, provenance="manual")
        self.assertFalse(betting.clv_criterion_met(self.conn))

    def test_unlocked_on_twenty_sized_bets_with_clear_positive_clv(self):
        self._many(20, close=0.80)
        self.assertTrue(betting.clv_criterion_met(self.conn))

    def test_not_unlocked_when_clv_is_positive_but_within_noise(self):
        """Mean > 0 is not the bar; mean > one standard error is."""
        for i in range(20):
            self._bet("sized", close=0.65 if i % 2 else 0.63)  # mean +0.0
        self.assertFalse(betting.clv_criterion_met(self.conn))


if __name__ == "__main__":
    unittest.main()
