"""Does the model anticipate the market, and by enough to pay for itself?

Closing-line value is the fastest honest test of a betting model, but it needs
placed bets with captured closes. This asks the same question of data that
already exists: when the model disagreed with the OPENING line, did the line
subsequently move toward it? The model never reads the market -- see the note in
lib/odds -- so this is not circular.

Two numbers matter and they are easy to conflate.

Whether the effect is REAL is a question about the standard error. Whether it is
USEFUL is a question about magnitude against costs, and that one does not
improve with sample size. A signal of one point of line movement is a signal of
one point however precisely it is measured, while the fee alone is two to three
cents and the edge gate is five. Precision and profitability are different axes.

The third number is the one most likely to be skipped: does the effect get
STRONGER where the model disagrees most? Those are the games it would actually
bet. A signal that is significant across all games but fades in the tail is a
broad nudge, not insight, and betting the tail on the strength of the average is
how a real-but-useless edge turns into a losing season.
"""
import statistics as st

MIN_SAMPLE = 3
REAL_SIGMA = 2.0
SUGGESTIVE_SIGMA = 1.5


class Observation:
    __slots__ = ("league", "disagreement", "movement")

    def __init__(self, league, disagreement, movement):
        self.league = league
        self.disagreement = disagreement      # model prob - opening prob, for our pick
        self.movement = movement              # later prob - opening prob, for our pick


def signed_movement(disagreement, movement):
    """Movement in the direction the model disagreed.

    Signing matters: a model right to fade a team should score exactly as well
    as one right to back it. Measuring raw movement would credit only half its
    opinions.
    """
    if disagreement == 0:
        return 0.0
    return movement if disagreement > 0 else -movement


def summarise(observations, min_disagreement=0.0):
    """Mean signed movement, its standard error, and how seriously to take it."""
    values = [signed_movement(o.disagreement, o.movement) for o in observations
              if abs(o.disagreement) > min_disagreement]
    n = len(values)
    if n < MIN_SAMPLE:
        return {"n": n, "mean": st.mean(values) if values else 0.0,
                "se": 0.0, "t": 0.0, "verdict": "insufficient"}
    mean = st.mean(values)
    se = st.stdev(values) / (n ** 0.5)
    # Zero variance means every observation agreed. That is the strongest
    # possible evidence, not the weakest -- t = mean/0 is infinite, not zero.
    t = (mean / se) if se else (float("inf") if mean > 0 else
                                (float("-inf") if mean < 0 else 0.0))
    if t >= REAL_SIGMA:
        verdict = "real"
    elif t >= SUGGESTIVE_SIGMA:
        verdict = "suggestive"
    else:
        verdict = "noise"
    return {"n": n, "mean": mean, "se": se, "t": t, "verdict": verdict}


def decays_with_conviction(observations, split=0.05):
    """True when the effect is weaker on the model's strong opinions.

    This is the warning that matters. The tool only bets games where the
    disagreement is large, so an effect that lives entirely in the small
    disagreements is not an effect the tool can harvest.
    """
    low = summarise([o for o in observations if abs(o.disagreement) <= split])
    high = summarise([o for o in observations if abs(o.disagreement) > split])
    if low["n"] < MIN_SAMPLE or high["n"] < MIN_SAMPLE:
        return False
    return high["mean"] < low["mean"]


def observations(conn, calibrate):
    """Every game with an opening line, a later line, and a model pick."""
    from . import odds

    rows = conn.execute(
        "SELECT p.pick_team_id, p.win_prob, g.league, g.home_team_id, "
        "       o.home_price o_home, o.away_price o_away, "
        "       c.home_price c_home, c.away_price c_away "
        "FROM prediction p "
        "JOIN game g ON g.id = p.game_id "
        "JOIN (SELECT game_id, home_price, away_price FROM odds_snapshot "
        "      WHERE book LIKE '%-open' GROUP BY game_id) o ON o.game_id = p.game_id "
        "JOIN (SELECT game_id, home_price, away_price, MAX(fetched_at) FROM odds_snapshot "
        "      WHERE book NOT LIKE '%-open' GROUP BY game_id) c ON c.game_id = p.game_id "
        "WHERE o.home_price IS NOT NULL AND o.away_price IS NOT NULL "
        "  AND c.home_price IS NOT NULL AND c.away_price IS NOT NULL").fetchall()

    def prob_for(is_home, home_price, away_price):
        ph, pa = odds.devig(odds.american_to_prob(home_price),
                            odds.american_to_prob(away_price))
        return None if ph is None else (ph if is_home else pa)

    out = []
    for r in rows:
        is_home = r["pick_team_id"] == r["home_team_id"]
        opening = prob_for(is_home, r["o_home"], r["o_away"])
        latest = prob_for(is_home, r["c_home"], r["c_away"])
        if opening is None or latest is None:
            continue
        out.append(Observation(r["league"],
                               calibrate(r["win_prob"], r["league"]) - opening,
                               latest - opening))
    return out
