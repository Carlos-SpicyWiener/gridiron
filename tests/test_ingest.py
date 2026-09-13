"""Persisting Kalshi quotes so a closing price exists at grade time. Spec §3.5, §3.6.

Without this the close is never captured, CLV is permanently unknown, and §7's
fastest unlock criterion can never fire. Network is injected, not called.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


from tests import support  # noqa: E402

from lib import betting, db, engine  # noqa: E402

_SEQ = iter(range(1, 1_000_000))
KICKOFF = "2026-09-20T17:00:00Z"


def market(ticker, event, team, bid, ask, oi=5000.0):
    return {"ticker": ticker, "event_ticker": event, "no_sub_title": team,
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
            "open_interest_fp": str(oi), "volume": "100"}


class IngestTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        support.reset(self.conn)
        self.away = self._team("Cincinnati Bengals", "CIN")
        self.home = self._team("Houston Texans", "HOU")
        self.game = self._game()
        self.conn.commit()

    def _team(self, name, abbrev):
        return self.conn.execute(
            "INSERT INTO team (league, espn_id, name, abbrev, tier, first_seen) "
            "VALUES ('nfl', ?, ?, ?, 'major', ?)",
            (f"{abbrev}-{next(_SEQ)}", name, abbrev, db.now())).lastrowid

    def _game(self, kickoff=KICKOFF):
        return self.conn.execute(
            "INSERT INTO game (league, espn_id, season, season_type, kickoff_utc, "
            "home_team_id, away_team_id, status, fetched_at) "
            "VALUES ('nfl', ?, 2026, 2, ?, ?, ?, 'scheduled', ?)",
            (f"g-{next(_SEQ)}", kickoff, self.home, self.away, db.now())).lastrowid

    def _event(self, bid=0.55, ask=0.56):
        ev = "KXNFLGAME-26SEP20CINHOU"
        return [market(f"{ev}-HOU", ev, "Houston", bid, ask),
                market(f"{ev}-CIN", ev, "Cincinnati", 1 - ask, 1 - bid)]


class Snapshots(IngestTestCase):

    def test_writes_a_row_for_each_side(self):
        engine.ingest_snapshots(self.conn, "nfl", self._event())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM kalshi_snapshot").fetchone()["n"], 2)

    def test_stores_bid_ask_mid_and_open_interest(self):
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.55, 0.56))
        row = self.conn.execute(
            "SELECT * FROM kalshi_snapshot WHERE team_id = ?", (self.home,)).fetchone()
        self.assertAlmostEqual(row["yes_bid"], 0.55)
        self.assertAlmostEqual(row["yes_ask"], 0.56)
        self.assertAlmostEqual(row["mid"], 0.555)
        self.assertAlmostEqual(row["open_interest"], 5000.0)

    def test_links_each_row_to_the_right_team(self):
        engine.ingest_snapshots(self.conn, "nfl", self._event())
        rows = {r["market_ticker"]: r["team_id"] for r in self.conn.execute(
            "SELECT market_ticker, team_id FROM kalshi_snapshot")}
        self.assertEqual(rows["KXNFLGAME-26SEP20CINHOU-HOU"], self.home)
        self.assertEqual(rows["KXNFLGAME-26SEP20CINHOU-CIN"], self.away)

    def test_a_second_poll_appends_rather_than_overwrites(self):
        """Line movement between open and kickoff is the signal."""
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.55, 0.56))
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.60, 0.61),
                                stamp="2026-09-19T12:00:00Z")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM kalshi_snapshot").fetchone()["n"], 4)

    def test_the_same_poll_twice_does_not_duplicate(self):
        markets = self._event()
        engine.ingest_snapshots(self.conn, "nfl", markets, stamp="2026-09-19T12:00:00Z")
        engine.ingest_snapshots(self.conn, "nfl", markets, stamp="2026-09-19T12:00:00Z")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM kalshi_snapshot").fetchone()["n"], 2)

    def test_skips_a_settled_book(self):
        ev = "KXNFLGAME-26SEP20CINHOU"
        dead = [market(f"{ev}-HOU", ev, "Houston", 0.0, 1.0),
                market(f"{ev}-CIN", ev, "Cincinnati", 0.0, 1.0)]
        engine.ingest_snapshots(self.conn, "nfl", dead)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM kalshi_snapshot").fetchone()["n"], 0)

    def test_skips_an_event_it_cannot_resolve(self):
        ev = "KXNFLGAME-26SEP20XXXYYY"
        unknown = [market(f"{ev}-XXX", ev, "Nowhere", 0.5, 0.51),
                   market(f"{ev}-YYY", ev, "Elsewhere", 0.49, 0.5)]
        engine.ingest_snapshots(self.conn, "nfl", unknown)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM kalshi_snapshot").fetchone()["n"], 0)


class Aliases(IngestTestCase):

    def test_learns_kalshis_abbreviations(self):
        engine.ingest_snapshots(self.conn, "nfl", self._event())
        rows = {r["kalshi_abbrev"]: r["team_id"] for r in self.conn.execute(
            "SELECT kalshi_abbrev, team_id FROM kalshi_alias")}
        self.assertEqual(rows, {"CIN": self.away, "HOU": self.home})

    def test_relearning_the_same_alias_is_harmless(self):
        engine.ingest_snapshots(self.conn, "nfl", self._event())
        engine.ingest_snapshots(self.conn, "nfl", self._event(),
                                stamp="2026-09-19T12:00:00Z")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM kalshi_alias").fetchone()["n"], 2)


class ClosingPrice(IngestTestCase):
    """The whole reason this exists: a graded bet must have a close to compare to."""

    def _bet(self):
        return betting.record_bet(
            self.conn, league="nfl", game_id=self.game, side_team_id=self.home,
            market_ticker="KXNFLGAME-26SEP20CINHOU-HOU", contracts=10, price=0.56,
            model_prob_raw=0.75, model_prob=0.71, market_prob=0.57, notes="x")

    def _finish(self):
        self.conn.execute(
            "UPDATE game SET status = 'final', home_score = 24, away_score = 10 "
            "WHERE id = ?", (self.game,))
        self.conn.commit()

    def test_grading_picks_up_the_last_pre_kickoff_mid(self):
        self._bet()
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.55, 0.56),
                                stamp="2026-09-18T12:00:00Z")
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.63, 0.65),
                                stamp="2026-09-20T16:45:00Z")
        self._finish()
        betting.grade(self.conn)
        row = self.conn.execute("SELECT * FROM bet").fetchone()
        self.assertAlmostEqual(row["close_price"], 0.64)
        self.assertEqual(row["close_snapshot_at"], "2026-09-20T16:45:00Z")

    def test_ignores_a_snapshot_taken_after_kickoff(self):
        """A post-kickoff quote is a live in-game price, not a close."""
        self._bet()
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.55, 0.56),
                                stamp="2026-09-20T16:45:00Z")
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.90, 0.92),
                                stamp="2026-09-20T18:30:00Z")
        self._finish()
        betting.grade(self.conn)
        self.assertAlmostEqual(
            self.conn.execute("SELECT close_price FROM bet").fetchone()["close_price"],
            0.555)

    def test_clv_becomes_measurable_once_a_close_exists(self):
        self._bet()
        engine.ingest_snapshots(self.conn, "nfl", self._event(0.63, 0.65),
                                stamp="2026-09-20T16:45:00Z")
        self._finish()
        betting.grade(self.conn)
        self.assertEqual(betting.performance(self.conn)["clv_n"], 1)
        self.assertAlmostEqual(betting.mean_clv(self.conn), 0.64 - 0.56, places=6)


if __name__ == "__main__":
    unittest.main()
