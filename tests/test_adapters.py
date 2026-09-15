"""ESPN ingest adapters: payload parsing, and NFL/CFB isolation.

Network is not called. Fixtures are the scoreboard shape ingest_events walks,
trimmed to the fields _get() actually reads.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import support  # noqa: E402

from lib import db, elo, espn, picks  # noqa: E402


def event(eid, date, home, away, status="STATUS_FINAL", completed=True,
          state="post", season=2025, season_type=2, week=None, neutral=False,
          allstar=False, home_score="100", away_score="90",
          home_conf=None, away_conf=None):
    hid, hname, habbr = home
    aid, aname, aabbr = away
    status_type = {"name": status, "state": state, "completed": completed}
    ev = {
        "id": str(eid),
        "date": date,
        "season": {"year": season, "type": season_type},
        "status": {"type": status_type},
        "competitions": [{
            "neutralSite": neutral,
            "type": {"abbreviation": "ALLSTAR" if allstar else "STD"},
            "competitors": [
                {"homeAway": "home", "score": home_score,
                 "team": {"id": str(hid), "displayName": hname,
                          "abbreviation": habbr, "conferenceId": home_conf}},
                {"homeAway": "away", "score": away_score,
                 "team": {"id": str(aid), "displayName": aname,
                          "abbreviation": aabbr, "conferenceId": away_conf}},
            ],
        }],
    }
    if week is not None:
        ev["week"] = {"number": week}
    return ev


def payload(events, season=2025, season_type=2, week=None):
    out = {"season": {"year": season, "type": season_type}, "events": events}
    if week is not None:
        out["week"] = {"number": week}
    return out


class AdapterTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        support.reset(self.conn)
        self._prev = os.environ.get("GRIDIRON_LEAGUES")
        os.environ.pop("GRIDIRON_LEAGUES", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev is None:
            os.environ.pop("GRIDIRON_LEAGUES", None)
        else:
            os.environ["GRIDIRON_LEAGUES"] = self._prev

    def _games(self, league=None):
        sql = "SELECT * FROM game"
        args = ()
        if league:
            sql += " WHERE league = ?"
            args = (league,)
        return self.conn.execute(sql + " ORDER BY espn_id", args).fetchall()

    def _teams(self, league=None):
        sql = "SELECT * FROM team"
        args = ()
        if league:
            sql += " WHERE league = ?"
            args = (league,)
        return self.conn.execute(sql + " ORDER BY name", args).fetchall()


class ScoreboardUrls(unittest.TestCase):
    """Football URLs must stay the Phase 1 shape so a path typo is a test failure."""

    def test_nfl_week_url_is_the_football_scoreboard(self):
        url = espn.scoreboard_url("nfl", season=2024, season_type=2, week=1)
        self.assertIn("/sports/football/nfl/scoreboard?", url)
        self.assertIn("dates=2024", url)
        self.assertIn("seasontype=2", url)
        self.assertIn("week=1", url)

    def test_cfb_keeps_the_fbs_group_filter(self):
        url = espn.scoreboard_url("cfb", season=2024, season_type=2, week=3)
        self.assertIn("/sports/football/college-football/scoreboard?", url)
        self.assertIn("groups=80", url)
        self.assertIn("limit=400", url)

    def test_nba_is_a_date_query_on_the_basketball_scoreboard(self):
        url = espn.scoreboard_url("nba", dates="20250115")
        self.assertIn("/sports/basketball/nba/scoreboard?", url)
        self.assertIn("dates=20250115", url)
        self.assertNotIn("week=", url)

    def test_mlb_accepts_a_date_range(self):
        url = espn.scoreboard_url("mlb", dates="20250401-20250403")
        self.assertIn("/sports/baseball/mlb/scoreboard?", url)
        self.assertIn("dates=20250401-20250403", url)

    def test_cbb_keeps_the_d1_group_filter(self):
        url = espn.scoreboard_url("cbb", dates="20251115")
        self.assertIn("/sports/basketball/mens-college-basketball/scoreboard?", url)
        self.assertIn("groups=50", url)
        self.assertIn("limit=400", url)


class NflAndCfbUnchanged(AdapterTestCase):

    def test_nfl_final_writes_two_major_teams_and_a_scored_game(self):
        pl = payload([event(
            "401671889", "2024-09-06T00:20Z",
            home=("12", "Kansas City Chiefs", "KC"),
            away=("21", "Baltimore Ravens", "BAL"),
            season=2024, week=1, home_score="27", away_score="20")],
            season=2024, week=1)
        seen, new, odds = espn.ingest_events(self.conn, "nfl", pl)
        self.conn.commit()
        self.assertEqual((seen, new, odds), (1, 1, 0))
        teams = {t["abbrev"]: t for t in self._teams("nfl")}
        self.assertEqual(set(teams), {"KC", "BAL"})
        self.assertTrue(all(t["tier"] == "major" for t in teams.values()))
        g = self._games("nfl")[0]
        self.assertEqual(g["home_score"], 27)
        self.assertEqual(g["away_score"], 20)
        self.assertEqual(g["status"], "final")
        self.assertEqual(g["week"], 1)
        self.assertEqual(g["season_type"], 2)

    def test_cfb_opponent_not_in_the_fbs_set_is_other(self):
        pl = payload([event(
            "401628599", "2024-08-31T16:00Z",
            home=("61", "Georgia Bulldogs", "UGA"),
            away=("2393", "Some FCS", "FCS"),
            season=2024, week=1, home_score="48", away_score="3")],
            season=2024, week=1)
        espn.ingest_events(self.conn, "cfb", pl, major_ids={"61"})
        self.conn.commit()
        teams = {t["abbrev"]: t for t in self._teams("cfb")}
        self.assertEqual(teams["UGA"]["tier"], "major")
        self.assertEqual(teams["FCS"]["tier"], "other")


class NbaAdapter(AdapterTestCase):

    def test_writes_teams_and_a_final(self):
        pl = payload([event(
            "401705127", "2025-01-16T00:00Z",
            home=("20", "Philadelphia 76ers", "PHI"),
            away=("9", "New York Knicks", "NY"),
            home_score="119", away_score="125")])
        seen, new, _ = espn.ingest_events(self.conn, "nba", pl)
        self.conn.commit()
        self.assertEqual((seen, new), (1, 1))
        g = self._games("nba")[0]
        self.assertEqual(g["home_score"], 119)
        self.assertEqual(g["away_score"], 125)
        self.assertIsNone(g["week"])
        self.assertEqual(len(self._teams("nba")), 2)

    def test_scheduled_game_does_not_store_espn_zero_as_a_result(self):
        pl = payload([event(
            "401902644", "2026-10-03T23:00Z",
            home=("28", "Toronto Raptors", "TOR"),
            away=("14", "Miami Heat", "MIA"),
            status="STATUS_SCHEDULED", completed=False, state="pre",
            home_score="0", away_score="0")])
        espn.ingest_events(self.conn, "nba", pl)
        self.conn.commit()
        g = self._games("nba")[0]
        self.assertEqual(g["status"], "scheduled")
        self.assertIsNone(g["home_score"])
        self.assertIsNone(g["away_score"])

    def test_preseason_is_skipped(self):
        pl = payload([event(
            "pre1", "2026-10-03T23:00Z",
            home=("28", "Toronto Raptors", "TOR"),
            away=("14", "Miami Heat", "MIA"),
            season_type=1)])
        seen, new, _ = espn.ingest_events(self.conn, "nba", pl)
        self.assertEqual((seen, new), (0, 0))
        self.assertEqual(self._games("nba"), [])

    def test_postponed_is_canceled_with_no_score(self):
        pl = payload([event(
            "401705098", "2025-01-11T20:00Z",
            home=("1", "Atlanta Hawks", "ATL"),
            away=("14", "Houston Rockets", "HOU"),
            status="STATUS_POSTPONED", completed=False, state="post",
            home_score="0", away_score="0")])
        espn.ingest_events(self.conn, "nba", pl)
        self.conn.commit()
        g = self._games("nba")[0]
        self.assertEqual(g["status"], "canceled")
        self.assertIsNone(g["home_score"])


class MlbAdapter(AdapterTestCase):

    def test_doubleheader_is_two_games_not_an_upsert_collision(self):
        """Same teams, same night, two ESPN ids — UNIQUE(league, espn_id)."""
        home = ("24", "St. Louis Cardinals", "STL")
        away = ("16", "Chicago Cubs", "CHC")
        pl = payload([
            event("401569896", "2024-07-13T18:15Z", home=home, away=away,
                  season=2024, home_score="5", away_score="1"),
            event("401673997", "2024-07-14T00:15Z", home=home, away=away,
                  season=2024, home_score="11", away_score="3"),
        ], season=2024)
        seen, new, _ = espn.ingest_events(self.conn, "mlb", pl)
        self.conn.commit()
        self.assertEqual((seen, new), (2, 2))
        games = self._games("mlb")
        self.assertEqual([g["espn_id"] for g in games], ["401569896", "401673997"])
        self.assertEqual(len(self._teams("mlb")), 2)

    def test_all_star_game_is_not_evidence(self):
        pl = payload([event(
            "asg", "2025-07-15T23:00Z",
            home=("32", "National All-Stars", "NL"),
            away=("31", "American All-Stars", "AL"),
            allstar=True, home_score="7", away_score="3")])
        seen, new, _ = espn.ingest_events(self.conn, "mlb", pl)
        self.assertEqual((seen, new), (0, 0))
        self.assertEqual(self._teams("mlb"), [])

    def test_spring_training_is_skipped(self):
        pl = payload([event(
            "st", "2025-02-20T20:05Z",
            home=("19", "Los Angeles Dodgers", "LAD"),
            away=("16", "Chicago Cubs", "CHC"),
            season_type=1, home_score="4", away_score="12")])
        seen, _, _ = espn.ingest_events(self.conn, "mlb", pl)
        self.assertEqual(seen, 0)

    def test_season_dates_fill_the_window_without_hitting_espn(self):
        days = espn.season_dates("mlb", 2025)
        self.assertEqual(days[0], "20250320")
        self.assertEqual(days[-1], "20251115")
        self.assertEqual(len(days), 241)


class CbbAdapter(AdapterTestCase):

    def test_d1_vs_non_d1_uses_the_membership_set(self):
        pl = payload([event(
            "401812788", "2025-11-16T00:00Z",
            home=("222", "Villanova Wildcats", "VILL"),
            away=("2393", "Some D2", "D2"),
            home_score="86", away_score="54", home_conf="4", away_conf=None,
            neutral=True)])
        espn.ingest_events(self.conn, "cbb", pl, major_ids={"222"})
        self.conn.commit()
        teams = {t["abbrev"]: t for t in self._teams("cbb")}
        self.assertEqual(teams["VILL"]["tier"], "major")
        self.assertEqual(teams["D2"]["tier"], "other")
        g = self._games("cbb")[0]
        self.assertEqual(g["neutral"], 1)

    def test_without_a_membership_set_every_cbb_side_is_other(self):
        """Fail closed: a missing D1 roster must not promote everyone to 1500."""
        pl = payload([event(
            "x", "2025-11-16T00:00Z",
            home=("222", "Villanova Wildcats", "VILL"),
            away=("87", "Notre Dame Fighting Irish", "ND"),
            home_score="80", away_score="70")])
        espn.ingest_events(self.conn, "cbb", pl, major_ids=None)
        self.conn.commit()
        self.assertTrue(all(t["tier"] == "other" for t in self._teams("cbb")))


class RateAndSlate(AdapterTestCase):

    def _final(self, league, eid, date, home, away, hs, aws, season=2024):
        pl = payload([event(
            eid, date, home=home, away=away, season=season,
            home_score=str(hs), away_score=str(aws))], season=season)
        espn.ingest_events(self.conn, league, pl)
        self.conn.commit()

    def test_nfl_ratings_do_not_move_when_an_nba_game_is_added(self):
        self._final("nfl", "nfl-1", "2024-09-08T17:00Z",
                    ("12", "Kansas City Chiefs", "KC"),
                    ("21", "Baltimore Ravens", "BAL"), 27, 20)
        elo.recompute(self.conn)
        before = {r["team_id"]: r["rating"] for r in self.conn.execute(
            "SELECT team_id, rating FROM rating_current WHERE league = 'nfl'")}
        self.assertEqual(len(before), 2)

        self._final("nba", "nba-1", "2024-10-24T23:00Z",
                    ("20", "Philadelphia 76ers", "PHI"),
                    ("9", "New York Knicks", "NY"), 102, 122)
        elo.recompute(self.conn)
        after = {r["team_id"]: r["rating"] for r in self.conn.execute(
            "SELECT team_id, rating FROM rating_current WHERE league = 'nfl'")}
        self.assertEqual(before, after)
        nba_rated = self.conn.execute(
            "SELECT COUNT(*) n FROM rating_current WHERE league = 'nba' AND games > 0"
        ).fetchone()["n"]
        self.assertEqual(nba_rated, 2)

    def test_predict_all_skips_nba_unless_the_league_is_enabled(self):
        future = "2099-01-15T00:00Z"
        self._final("nfl", "nfl-1", "2024-09-08T17:00Z",
                    ("12", "Kansas City Chiefs", "KC"),
                    ("21", "Baltimore Ravens", "BAL"), 27, 20)
        self._final("nba", "nba-1", "2024-10-24T23:00Z",
                    ("20", "Philadelphia 76ers", "PHI"),
                    ("9", "New York Knicks", "NY"), 102, 122)
        # Upcoming fixtures, same teams.
        espn.ingest_events(self.conn, "nfl", payload([event(
            "nfl-up", future,
            home=("12", "Kansas City Chiefs", "KC"),
            away=("21", "Baltimore Ravens", "BAL"),
            status="STATUS_SCHEDULED", completed=False, state="pre",
            season=2099, home_score="0", away_score="0")], season=2099))
        espn.ingest_events(self.conn, "nba", payload([event(
            "nba-up", future,
            home=("20", "Philadelphia 76ers", "PHI"),
            away=("9", "New York Knicks", "NY"),
            status="STATUS_SCHEDULED", completed=False, state="pre",
            season=2099, home_score="0", away_score="0")], season=2099))
        self.conn.commit()
        elo.recompute(self.conn)

        written, skipped, locked, unchanged = picks.predict(self.conn, league=None, days=40000)
        leagues_on_board = {r["league"] for r in self.conn.execute(
            "SELECT g.league FROM prediction p JOIN game g ON g.id = p.game_id")}
        self.assertEqual(leagues_on_board, {"nfl"})
        self.assertGreaterEqual(written, 1)

        written_nba, *_ = picks.predict(self.conn, league="nba", days=40000)
        self.assertGreaterEqual(written_nba, 1)
        leagues_on_board = {r["league"] for r in self.conn.execute(
            "SELECT g.league FROM prediction p JOIN game g ON g.id = p.game_id")}
        self.assertEqual(leagues_on_board, {"nfl", "nba"})

    def test_slate_renders_an_nba_section_when_asked(self):
        future = "2099-01-15T00:00Z"
        self._final("nba", "nba-1", "2024-10-24T23:00Z",
                    ("20", "Philadelphia 76ers", "PHI"),
                    ("9", "New York Knicks", "NY"), 102, 122)
        espn.ingest_events(self.conn, "nba", payload([event(
            "nba-up", future,
            home=("20", "Philadelphia 76ers", "PHI"),
            away=("9", "New York Knicks", "NY"),
            status="STATUS_SCHEDULED", completed=False, state="pre",
            season=2099, home_score="0", away_score="0")], season=2099))
        self.conn.commit()
        elo.recompute(self.conn)
        picks.predict(self.conn, league="nba", days=40000)
        board = picks.render_slate(self.conn, league="nba", days=40000)
        self.assertIn("NBA", board)
        self.assertIn("Philadelphia", board)


class CalendarHelpers(unittest.TestCase):

    def test_calendar_days_dedupes_and_formats_yyyymmdd(self):
        pl = {"leagues": [{"calendar": [
            "2024-10-24T07:00Z", "2024-10-25T07:00Z", "2024-10-24T07:00Z"]}]}
        self.assertEqual(espn._calendar_days(pl), ["20241024", "20241025"])

    def test_date_chunks_join_as_espn_ranges(self):
        days = ["20250401", "20250402", "20250403", "20250404"]
        chunks = list(espn._chunks(days, 3))
        self.assertEqual(espn._dates_param(chunks[0]), "20250401-20250403")
        self.assertEqual(espn._dates_param(chunks[1]), "20250404")


if __name__ == "__main__":
    unittest.main()
