"""Phase 2 league registry: enablement and betting lock.

GRIDIRON_LEAGUES is the ingest switch. BETTING is a separate tuple so adding
NBA to the daily sync cannot silently size NBA contracts.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import support  # noqa: E402  — redirect GRIDIRON_DB before lib.db

from lib import calibration, engine, leagues  # noqa: E402


class Enabled(unittest.TestCase):

    def setUp(self):
        self._prev = os.environ.get("GRIDIRON_LEAGUES")
        os.environ.pop("GRIDIRON_LEAGUES", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("GRIDIRON_LEAGUES", None)
        else:
            os.environ["GRIDIRON_LEAGUES"] = self._prev

    def test_default_is_football(self):
        self.assertEqual(leagues.enabled(), ("nfl", "cfb"))

    def test_env_adds_nba_without_dropping_football(self):
        os.environ["GRIDIRON_LEAGUES"] = "nfl,cfb,nba"
        self.assertEqual(leagues.enabled(), ("nfl", "cfb", "nba"))

    def test_unknown_tokens_are_ignored(self):
        os.environ["GRIDIRON_LEAGUES"] = "nfl,nhl,cfb"
        self.assertEqual(leagues.enabled(), ("nfl", "cfb"))

    def test_empty_or_garbage_falls_back_to_football(self):
        os.environ["GRIDIRON_LEAGUES"] = "  "
        self.assertEqual(leagues.enabled(), ("nfl", "cfb"))
        os.environ["GRIDIRON_LEAGUES"] = "nhl,wnba"
        self.assertEqual(leagues.enabled(), ("nfl", "cfb"))

    def test_duplicates_and_case_are_normalised(self):
        os.environ["GRIDIRON_LEAGUES"] = "NBA, nba, Mlb"
        self.assertEqual(leagues.enabled(), ("nba", "mlb"))


class BettingLock(unittest.TestCase):

    def test_football_is_the_only_betting_league(self):
        self.assertTrue(leagues.betting_allowed("nfl"))
        self.assertTrue(leagues.betting_allowed("cfb"))
        for lg in ("nba", "mlb", "cbb"):
            with self.subTest(league=lg):
                self.assertFalse(leagues.betting_allowed(lg))

    def test_scan_does_not_call_kalshi_for_a_locked_league(self):
        """open_markets would KeyError on SERIES; scan must refuse first."""
        rows, unresolved = engine.scan(conn=None, league="nba", bankroll=1000)
        self.assertEqual(rows, [])
        self.assertEqual(unresolved, [])

    def test_calibration_still_refuses_phase2_leagues(self):
        """Coefficients are added after a backtest, not inherited from NFL."""
        for lg in ("nba", "mlb", "cbb"):
            with self.subTest(league=lg):
                with self.assertRaises(KeyError):
                    calibration.calibrate(0.6, lg)


class ConfigBlock(unittest.TestCase):

    def test_every_known_league_has_ingest_and_elo_params(self):
        for lg in leagues.KNOWN:
            with self.subTest(league=lg):
                self.assertIn(lg, leagues.INGEST)
                self.assertIn(lg, leagues.PARAMS)
                self.assertIn("k", leagues.PARAMS[lg])
                self.assertIn("hfa", leagues.PARAMS[lg])

    def test_football_elo_constants_did_not_move(self):
        self.assertEqual(leagues.PARAMS["nfl"]["k"], 20.0)
        self.assertEqual(leagues.PARAMS["nfl"]["hfa"], 48.0)
        self.assertEqual(leagues.PARAMS["cfb"]["k"], 38.0)
        self.assertEqual(leagues.PARAMS["cfb"]["hfa"], 65.0)

    def test_kalshi_series_are_documented_but_mlb_is_not_a_substring_trap(self):
        """KXNBAGAME must not be confused with a prop series we have not listed."""
        self.assertEqual(leagues.KALSHI_SERIES["nba"], "KXNBAGAME")
        self.assertEqual(leagues.KALSHI_SERIES["mlb"], "KXMLBGAME")
        self.assertEqual(leagues.KALSHI_SERIES["cbb"], "KXNCAAMBGAME")


if __name__ == "__main__":
    unittest.main()
