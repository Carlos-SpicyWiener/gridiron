"""Open positions: don't re-recommend them, and don't forget they cost money.

Three ways the same blind spot showed up on 2026-09-13, with one open bet:
`scan` proposed the identical bet again, `ledger` reported staked $0.00 beside a
bankroll down $5.55, and the exposure cap behaved as though nothing was held.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import support  # noqa: E402

from lib import betting, db  # noqa: E402

_SEQ = iter(range(1, 1_000_000))


class PositionsTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        support.reset(self.conn)
        self.home = self._team("Home Team", "HOM")
        self.away = self._team("Away Team", "AWY")
        self.game = self._game()
        self.conn.commit()

    def _team(self, name, abbrev):
        return self.conn.execute(
            "INSERT INTO team (league, espn_id, name, abbrev, tier, first_seen) "
            "VALUES ('nfl', ?, ?, ?, 'major', ?)",
            (f"{abbrev}-{next(_SEQ)}", name, abbrev, db.now())).lastrowid

    def _game(self, status="scheduled", home_score=None, away_score=None):
        return self.conn.execute(
            "INSERT INTO game (league, espn_id, season, season_type, kickoff_utc, "
            "home_team_id, away_team_id, status, home_score, away_score, fetched_at) "
            "VALUES ('nfl', ?, 2026, 2, '2026-09-20T17:00:00Z', ?, ?, ?, ?, ?, ?)",
            (f"g-{next(_SEQ)}", self.home, self.away, status, home_score, away_score,
             db.now())).lastrowid

    def _bet(self, game_id=None, side=None, contracts=7, price=0.78):
        return betting.record_bet(
            self.conn, league="nfl", game_id=game_id or self.game,
            side_team_id=side or self.home, market_ticker="KX-TEST",
            contracts=contracts, price=price, model_prob_raw=0.85,
            model_prob=0.87, market_prob=0.76, notes="x")


class OpenPositions(PositionsTestCase):

    def test_reports_what_is_currently_held(self):
        self._bet()
        self.assertEqual(betting.open_positions(self.conn), {(self.game, self.home)})

    def test_a_settled_bet_is_no_longer_held(self):
        self._bet()
        self.conn.execute("UPDATE game SET status='final', home_score=24, away_score=3 "
                          "WHERE id = ?", (self.game,))
        self.conn.commit()
        betting.grade(self.conn)
        self.assertEqual(betting.open_positions(self.conn), set())

    def test_holding_one_side_does_not_mark_the_other_held(self):
        self._bet(side=self.home)
        self.assertNotIn((self.game, self.away), betting.open_positions(self.conn))

    def test_nothing_held_is_an_empty_set_not_none(self):
        self.assertEqual(betting.open_positions(self.conn), set())


class Committed(PositionsTestCase):
    """`staked $0.00` beside a bankroll down $5.55 reads as a bug even when the
    bankroll is right. Money in an unsettled bet is committed, not absent."""

    def test_counts_the_cost_of_open_bets(self):
        self._bet(contracts=7, price=0.78)
        self.assertAlmostEqual(betting.committed(self.conn), 5.55, places=2)

    def test_a_settled_bet_is_no_longer_committed(self):
        self._bet()
        self.conn.execute("UPDATE game SET status='final', home_score=24, away_score=3 "
                          "WHERE id = ?", (self.game,))
        self.conn.commit()
        betting.grade(self.conn)
        self.assertAlmostEqual(betting.committed(self.conn), 0.0, places=6)

    def test_performance_reports_it_alongside_settled_stake(self):
        self._bet(contracts=7, price=0.78)
        perf = betting.performance(self.conn)
        self.assertAlmostEqual(perf["committed"], 5.55, places=2)
        self.assertEqual(perf["staked"], 0.0)      # staked stays settled-only
        self.assertEqual(perf["open"], 1)

    def test_committed_plus_balance_accounts_for_the_deposit(self):
        betting.deposit(self.conn, 100.0, "seed")
        self._bet(contracts=7, price=0.78)
        perf = betting.performance(self.conn)
        self.assertAlmostEqual(perf["balance"] + perf["committed"], 100.0, places=2)


class ExposureAgainstOpenBets(PositionsTestCase):
    """The 20% cap has to see money already at risk, or it isn't a cap."""

    def test_open_bets_consume_the_exposure_headroom(self):
        from lib import sizing
        self._bet(contracts=7, price=0.78)          # ~$5.55 committed
        cands = [{"id": "a", "p": 0.60, "price": 0.45, "edge": 0.13}]
        loose = sizing.allocate(cands, 30.0, open_exposure=0.0)
        tight = sizing.allocate(cands, 30.0, open_exposure=betting.committed(self.conn))
        self.assertGreater(loose[0]["contracts"], tight[0]["contracts"])


if __name__ == "__main__":
    unittest.main()
