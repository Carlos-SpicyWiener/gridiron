"""Resolving a Kalshi event to one of our games. Spec §3.3.

Pure logic only — no network. Getting this wrong attributes a real-money bet to
the wrong team, so it is the one place in Phase 1 that fails loudly rather than
guessing.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import kalshi


class Game:
    """The columns resolve() needs, without a database."""

    def __init__(self, gid, away_id, away, away_abbrev, home_id, home, home_abbrev):
        self.id, self.away_team_id, self.home_team_id = gid, away_id, home_id
        self.away_name, self.home_name = away, home
        self.away_abbrev, self.home_abbrev = away_abbrev, home_abbrev


COWBOYS_GIANTS = Game(1, 10, "Dallas Cowboys", "DAL", 11, "New York Giants", "NYG")
JETS_TITANS = Game(2, 12, "New York Jets", "NYJ", 13, "Tennessee Titans", "TEN")
CMDRS_EAGLES = Game(3, 14, "Washington Commanders", "WSH", 15, "Philadelphia Eagles", "PHI")
PACK_VIKES = Game(4, 16, "Green Bay Packers", "GB", 17, "Minnesota Vikings", "MIN")


class ParseTicker(unittest.TestCase):

    def test_splits_series_date_and_team_suffix(self):
        got = kalshi.parse_event_ticker("KXNFLGAME-26SEP13DALNYG")
        self.assertEqual(got.series, "KXNFLGAME")
        self.assertEqual(got.suffix, "DALNYG")
        self.assertEqual(got.date.isoformat(), "2026-09-13")

    def test_handles_a_college_ticker(self):
        got = kalshi.parse_event_ticker("KXNCAAFGAME-26SEP12OSUTEX")
        self.assertEqual(got.series, "KXNCAAFGAME")
        self.assertEqual(got.suffix, "OSUTEX")

    def test_rejects_something_that_is_not_an_event_ticker(self):
        for bad in ("KXNFLGAME", "nonsense", "KXNFLGAME-XXXXXXDALNYG"):
            with self.subTest(ticker=bad):
                with self.assertRaises(ValueError):
                    kalshi.parse_event_ticker(bad)


class ResolveByAbbrev(unittest.TestCase):
    """The happy path: Kalshi's suffix is our two abbrevs concatenated."""

    def test_matches_a_game_whose_abbrevs_concatenate_to_the_suffix(self):
        got = kalshi.resolve("DALNYG", ["Dallas", "New York G"], [COWBOYS_GIANTS, JETS_TITANS])
        self.assertEqual(got.game_id, 1)

    def test_learns_the_alias_for_both_sides(self):
        got = kalshi.resolve("DALNYG", ["Dallas", "New York G"], [COWBOYS_GIANTS])
        self.assertEqual(got.aliases, {"DAL": 10, "NYG": 11})

    def test_variable_length_abbrevs_do_not_confuse_the_split(self):
        """GBMIN could split GB|MIN or GBM|IN; only the game table settles it."""
        got = kalshi.resolve("GBMIN", ["Green Bay", "Minnesota"], [PACK_VIKES])
        self.assertEqual(got.game_id, 4)
        self.assertEqual(got.aliases, {"GB": 16, "MIN": 17})


class ResolveWhenAbbrevsDisagree(unittest.TestCase):
    """Kalshi says WAS, ESPN says WSH. Real case, seen on 2026-09-13."""

    def test_falls_back_to_names_when_the_abbrev_does_not_match(self):
        got = kalshi.resolve("WASPHI", ["Washington", "Philadelphia"], [CMDRS_EAGLES])
        self.assertEqual(got.game_id, 3)

    def test_records_kalshis_spelling_not_ours(self):
        got = kalshi.resolve("WASPHI", ["Washington", "Philadelphia"], [CMDRS_EAGLES])
        self.assertEqual(got.aliases, {"WAS": 14, "PHI": 15})


class TruncatedNames(unittest.TestCase):
    """Open markets truncate to 13 chars, so "New York G" and "New York J"
    are 0.50 Jaccard against BOTH Giants and Jets. Only the pair disambiguates."""

    def test_the_opponent_breaks_the_new_york_tie(self):
        got = kalshi.resolve("DALNYG", ["Dallas", "New York G"],
                             [COWBOYS_GIANTS, JETS_TITANS])
        self.assertEqual(got.game_id, 1)
        self.assertEqual(got.aliases["NYG"], 11)

    def test_settled_format_names_also_resolve(self):
        """Settled markets say "CHI Bears" where open ones said "Chicago"."""
        got = kalshi.resolve("WASPHI", ["WSH Commanders", "PHI Eagles"], [CMDRS_EAGLES])
        self.assertEqual(got.game_id, 3)


class RefusesToGuess(unittest.TestCase):

    def test_returns_nothing_when_no_candidate_game_fits(self):
        self.assertIsNone(kalshi.resolve("DALNYG", ["Dallas", "New York G"], [JETS_TITANS]))

    def test_returns_nothing_when_two_games_fit_equally(self):
        """A tie is a bug in the candidate set, not a coin to flip."""
        twin = Game(9, 10, "Dallas Cowboys", "DAL", 11, "New York Giants", "NYG")
        self.assertIsNone(
            kalshi.resolve("DALNYG", ["Dallas", "New York G"], [COWBOYS_GIANTS, twin]))

    def test_returns_nothing_on_an_empty_candidate_set(self):
        self.assertIsNone(kalshi.resolve("DALNYG", ["Dallas", "New York G"], []))


class Quotes(unittest.TestCase):
    """The API returns dollar-denominated strings, and a settled market's book
    is degenerate: bid 0.00 / ask 1.00, whose midpoint is a plausible fake 0.50."""

    def test_parses_string_prices(self):
        q = kalshi.quote({"yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6100"})
        self.assertAlmostEqual(q.bid, 0.60)
        self.assertAlmostEqual(q.ask, 0.61)
        self.assertAlmostEqual(q.mid, 0.605)
        self.assertAlmostEqual(q.spread, 0.01)

    def test_treats_a_settled_book_as_no_quote(self):
        self.assertIsNone(kalshi.quote({"yes_bid_dollars": "0.0000",
                                        "yes_ask_dollars": "1.0000"}))

    def test_treats_a_missing_side_as_no_quote(self):
        self.assertIsNone(kalshi.quote({"yes_bid_dollars": "0.6000"}))


if __name__ == "__main__":
    unittest.main()
