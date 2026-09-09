"""Elo rating engine for football.

Elo is the right first model here for an unglamorous reason: it needs only the
final score and it is fully recomputable, so the whole rating table is a pure
function of the game table. That keeps ratings auditable and makes a wrong
number a defect in this file rather than a row to correct by hand.

Three departures from textbook Elo, all standard for football:

  * Home-field advantage is added to the home rating before the expectation,
    and zeroed at neutral sites.
  * A margin-of-victory multiplier scales K by how decisive the win was, damped
    by the rating gap so that a favourite thrashing a minnow gains little. Without
    the damping term, blowouts feed on themselves.
  * Ratings regress toward their tier's mean between seasons, because rosters and
    coaching staffs turn over. College regresses harder than the NFL.
"""
import math

from . import db

MODEL_VERSION = "elo-1.0"

PARAMS = {
    #      k     hfa   revert  base   other_base
    "nfl": {"k": 20.0, "hfa": 48.0, "revert": 0.25, "base": 1500.0, "other_base": 1500.0},
    "cfb": {"k": 38.0, "hfa": 65.0, "revert": 0.35, "base": 1500.0, "other_base": 1200.0},
}

# Where a win probability stops being interesting. Tuned to plain English, not
# to the model: a "lock" should be a game you'd be surprised to lose.
LOCK, LEAN = 0.75, 0.60


def confidence(prob):
    if prob >= LOCK:
        return "lock"
    if prob >= LEAN:
        return "lean"
    return "coin-flip"


def win_prob(rating_a, rating_b):
    """Probability A beats B. Ratings passed in must already include any HFA."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def matchup(home_rating, away_rating, league, neutral=False):
    """Home win probability for a matchup. The one place HFA is applied."""
    hfa = 0.0 if neutral else PARAMS[league]["hfa"]
    return win_prob(home_rating + hfa, away_rating)


def _mov_multiplier(margin, winner_edge):
    """538's margin multiplier. `winner_edge` is the winner's pregame rating
    advantage (HFA included); a large edge shrinks the credit for running it up."""
    return math.log(abs(margin) + 1.0) * (2.2 / (winner_edge * 0.001 + 2.2))


def starting_rating(league, tier):
    p = PARAMS[league]
    return p["other_base"] if tier == "other" else p["base"]


def recompute(conn, progress=None):
    """Replay every final game in kickoff order and rebuild both rating tables.

    Deliberately destructive and deliberately total: there is no incremental
    path, so ratings can never drift away from the games that produced them.
    """
    teams = {r["id"]: r for r in conn.execute("SELECT id, league, tier, name FROM team")}
    ratings = {tid: starting_rating(t["league"], t["tier"]) for tid, t in teams.items()}
    played = {tid: 0 for tid in teams}
    last_game = {}
    last_season = {}          # league -> most recent season replayed
    history = []

    rows = conn.execute(
        "SELECT id, league, season, kickoff_utc, home_team_id, away_team_id, neutral, "
        "       home_score, away_score "
        "FROM game WHERE status = 'final' AND home_score IS NOT NULL AND away_score IS NOT NULL "
        "ORDER BY kickoff_utc, id"
    ).fetchall()

    for g in rows:
        league = g["league"]
        p = PARAMS.get(league)
        if p is None:
            continue

        # Season rollover: regress everyone in this league toward their tier mean.
        if last_season.get(league) is not None and g["season"] != last_season[league]:
            for tid, t in teams.items():
                if t["league"] != league:
                    continue
                base = starting_rating(league, t["tier"])
                ratings[tid] = base + (1.0 - p["revert"]) * (ratings[tid] - base)
        last_season[league] = g["season"]

        home, away = g["home_team_id"], g["away_team_id"]
        hfa = 0.0 if g["neutral"] else p["hfa"]
        r_home, r_away = ratings[home], ratings[away]
        exp_home = win_prob(r_home + hfa, r_away)

        margin = g["home_score"] - g["away_score"]
        if margin > 0:
            actual, winner_edge = 1.0, (r_home + hfa) - r_away
        elif margin < 0:
            actual, winner_edge = 0.0, r_away - (r_home + hfa)
        else:
            actual, winner_edge = 0.5, 0.0

        mult = 1.0 if margin == 0 else _mov_multiplier(margin, max(winner_edge, 0.0))
        delta = p["k"] * mult * (actual - exp_home)

        ratings[home] = r_home + delta
        ratings[away] = r_away - delta
        played[home] += 1
        played[away] += 1
        last_game[home] = last_game[away] = g["id"]
        history.append((home, g["id"], g["season"], ratings[home]))
        history.append((away, g["id"], g["season"], ratings[away]))

    stamp = db.now()
    conn.execute("DELETE FROM rating_history")
    conn.execute("DELETE FROM rating_current")
    conn.executemany(
        "INSERT INTO rating_history (team_id, game_id, season, rating_after) VALUES (?, ?, ?, ?)",
        history)
    conn.executemany(
        "INSERT INTO rating_current (team_id, league, rating, games, last_game_id, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(tid, teams[tid]["league"], ratings[tid], played[tid], last_game.get(tid), stamp)
         for tid in teams])
    conn.commit()
    if progress:
        progress(f"  replayed {len(rows)} final games across {len(teams)} teams")
    return len(rows), len(teams)
