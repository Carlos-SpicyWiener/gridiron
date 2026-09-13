"""Grading bets and reporting the portfolio. Spec §6."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


from tests import support  # noqa: E402

from lib import betting, db  # noqa: E402

_SEQ = iter(range(1, 1_000_000))


class LedgerTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.conn = db.connect()
        # Registered before anything can fail, so a bad setUp cannot leak the
        # connection and lock the database for every test after it.
        self.addCleanup(self.conn.close)
        support.reset(self.conn)
        self.home = self._team("Home Team", "HOM")
        self.away = self._team("Away Team", "AWY")
        self.conn.commit()

    def _team(self, name, abbrev):
        return self.conn.execute(
            "INSERT INTO team (league, espn_id, name, abbrev, tier, first_seen) "
            "VALUES ('nfl', ?, ?, ?, 'major', ?)",
            (f"{abbrev}-{next(_SEQ)}", name, abbrev, db.now())).lastrowid

    def _game(self, home_score=None, away_score=None, status="scheduled",
              kickoff="2026-09-01T17:00:00Z"):
        return self.conn.execute(
            "INSERT INTO game (league, espn_id, season, season_type, kickoff_utc, "
            "home_team_id, away_team_id, status, home_score, away_score, fetched_at) "
            "VALUES ('nfl', ?, 2026, 2, ?, ?, ?, ?, ?, ?, ?)",
            (f"g-{next(_SEQ)}", kickoff, self.home,
             self.away, status, home_score, away_score, db.now())).lastrowid

    def _bet(self, game_id, side, contracts=10, price=0.60, provenance="sized"):
        return betting.record_bet(
            self.conn, league="nfl", game_id=game_id, side_team_id=side,
            market_ticker="KXNFLGAME-26SEP01AWYHOM", contracts=contracts, price=price,
            model_prob_raw=0.70, model_prob=0.68, market_prob=0.62,
            notes="test", provenance=provenance)


class Settlement(LedgerTestCase):

    def test_a_winning_bet_pays_one_per_contract(self):
        g = self._game(home_score=24, away_score=10, status="final")
        self._bet(g, self.home)
        betting.grade(self.conn)
        row = self.conn.execute("SELECT * FROM bet").fetchone()
        self.assertEqual(row["status"], "won")
        self.assertAlmostEqual(row["settlement_value"], 1.0)
        self.assertAlmostEqual(row["payout"], 10.0)
        self.assertAlmostEqual(row["pnl"], 10.0 - row["cost"], places=6)

    def test_a_losing_bet_pays_nothing(self):
        g = self._game(home_score=10, away_score=24, status="final")
        self._bet(g, self.home)
        betting.grade(self.conn)
        row = self.conn.execute("SELECT * FROM bet").fetchone()
        self.assertEqual(row["status"], "lost")
        self.assertAlmostEqual(row["payout"], 0.0)
        self.assertLess(row["pnl"], 0)

    def test_a_tie_returns_half_the_stake_not_zero(self):
        """Kalshi resolves a tie to $0.50 a side. prediction.correct calls it a
        loss, which is fine for a pick record and wrong for money."""
        g = self._game(home_score=17, away_score=17, status="final")
        self._bet(g, self.home)
        betting.grade(self.conn)
        row = self.conn.execute("SELECT * FROM bet").fetchone()
        self.assertEqual(row["status"], "push")
        self.assertAlmostEqual(row["settlement_value"], 0.5)
        self.assertAlmostEqual(row["payout"], 5.0)

    def test_leaves_an_unfinished_game_alone(self):
        g = self._game(status="scheduled")
        self._bet(g, self.home)
        betting.grade(self.conn)
        self.assertEqual(self.conn.execute("SELECT status FROM bet").fetchone()["status"],
                         "open")

    def test_grading_is_idempotent(self):
        g = self._game(home_score=24, away_score=10, status="final")
        self._bet(g, self.home)
        betting.grade(self.conn)
        betting.grade(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM bankroll_event "
                              "WHERE reason = 'settlement'").fetchone()["n"], 1)


class CashLedger(LedgerTestCase):
    """The bankroll is a cash account: a stake leaves it, a payout returns."""

    def test_placing_a_bet_debits_the_bankroll(self):
        g = self._game()
        self._bet(g, self.home)
        row = self.conn.execute(
            "SELECT delta, reason FROM bankroll_event ORDER BY id").fetchone()
        self.assertEqual(row["reason"], "stake")
        self.assertLess(row["delta"], 0)

    def test_settling_credits_the_payout(self):
        g = self._game(home_score=24, away_score=10, status="final")
        self._bet(g, self.home, contracts=10, price=0.60)
        betting.grade(self.conn)
        self.assertAlmostEqual(
            self.conn.execute("SELECT SUM(delta) AS d FROM bankroll_event").fetchone()["d"],
            10.0 - 10 * 0.63, places=6)

    def test_a_deposit_survives_in_the_curve(self):
        """The reason a singleton balance row cannot produce a bankroll curve."""
        betting.deposit(self.conn, 100.0, "initial")
        g = self._game(home_score=10, away_score=24, status="final")
        self._bet(g, self.home)
        betting.grade(self.conn)
        curve = betting.bankroll_curve(self.conn)
        self.assertEqual(len(curve), 3)
        self.assertAlmostEqual(curve[0]["balance"], 100.0, places=6)
        self.assertLess(curve[-1]["balance"], 100.0)


class Performance(LedgerTestCase):

    def _settled(self, won, **kw):
        g = self._game(home_score=24 if won else 10, away_score=10 if won else 24,
                       status="final")
        self._bet(g, self.home, **kw)
        betting.grade(self.conn)

    def test_counts_wins_losses_and_pushes(self):
        self._settled(True)
        self._settled(True)
        self._settled(False)
        perf = betting.performance(self.conn)
        self.assertEqual((perf["won"], perf["lost"], perf["push"]), (2, 1, 0))

    def test_roi_is_profit_over_money_staked(self):
        self._settled(True, contracts=10, price=0.60)   # cost 6.30, payout 10
        perf = betting.performance(self.conn)
        self.assertAlmostEqual(perf["staked"], 6.30, places=6)
        self.assertAlmostEqual(perf["pnl"], 3.70, places=6)
        self.assertAlmostEqual(perf["roi"], 3.70 / 6.30, places=6)

    def test_open_bets_are_counted_but_not_scored(self):
        self._game()
        self._bet(self._game(), self.home)
        perf = betting.performance(self.conn)
        self.assertEqual(perf["open"], 1)
        self.assertEqual(perf["settled"], 0)

    def test_an_empty_ledger_does_not_divide_by_zero(self):
        perf = betting.performance(self.conn)
        self.assertEqual(perf["settled"], 0)
        self.assertEqual(perf["roi"], 0.0)

    def test_reports_the_sized_pool_separately_from_everything(self):
        """§7 grades the sizer on sized bets; the P&L counts them all."""
        self._settled(True, provenance="manual")
        self._settled(False, provenance="sized")
        perf = betting.performance(self.conn)
        self.assertEqual(perf["settled"], 2)
        self.assertEqual(perf["sized_settled"], 1)


if __name__ == "__main__":
    unittest.main()
