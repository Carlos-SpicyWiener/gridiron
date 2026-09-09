"""Walk-forward backtest.

The only honest way to test a picks model: replay history in order, and for each
game make the prediction using ratings built strictly from games that had already
finished. The rating update for a game happens only AFTER its prediction is
recorded, so no result can inform its own forecast.

This deliberately re-implements the replay loop from elo.recompute rather than
reusing it, because the two want different things — recompute wants final
ratings, this wants the rating as it stood at each kickoff. Keeping them separate
means neither has to grow a flag that could silently leak a result into a pick.
"""
from . import elo


def run(conn, league, seasons, warmup_from=None):
    """Backtest `seasons` for `league`, warming up on everything earlier.

    Returns a dict of results, including a calibration table.
    """
    teams = {r["id"]: r for r in conn.execute(
        "SELECT id, league, tier FROM team WHERE league = ?", (league,))}
    ratings = {tid: elo.starting_rating(league, t["tier"]) for tid, t in teams.items()}
    p = elo.PARAMS[league]
    last_season = None

    sql = ("SELECT id, season, kickoff_utc, home_team_id, away_team_id, neutral, "
           "       home_score, away_score FROM game "
           "WHERE league = ? AND status = 'final' AND home_score IS NOT NULL "
           "  AND away_score IS NOT NULL")
    params = [league]
    if warmup_from:
        sql += " AND season >= ?"
        params.append(warmup_from)
    sql += " ORDER BY kickoff_utc, id"

    tested = correct = 0
    ties = 0
    home_baseline = 0
    by_tier = {}
    bins = {}          # decile -> [n, wins]
    brier_sum = 0.0

    for g in conn.execute(sql, params).fetchall():
        if last_season is not None and g["season"] != last_season:
            for tid, t in teams.items():
                base = elo.starting_rating(league, t["tier"])
                ratings[tid] = base + (1.0 - p["revert"]) * (ratings[tid] - base)
        last_season = g["season"]

        home, away = g["home_team_id"], g["away_team_id"]
        if home not in ratings or away not in ratings:
            continue
        hfa = 0.0 if g["neutral"] else p["hfa"]
        r_home, r_away = ratings[home], ratings[away]
        exp_home = elo.win_prob(r_home + hfa, r_away)

        margin = g["home_score"] - g["away_score"]

        # ---- score the prediction BEFORE the rating update ----
        if g["season"] in seasons:
            if margin == 0:
                ties += 1
            else:
                pick_home = exp_home >= 0.5
                prob = exp_home if pick_home else 1.0 - exp_home
                hit = (margin > 0) == pick_home
                tested += 1
                correct += 1 if hit else 0
                home_baseline += 1 if margin > 0 else 0

                tier = elo.confidence(prob)
                slot = by_tier.setdefault(tier, [0, 0, 0.0])
                slot[0] += 1
                slot[1] += 1 if hit else 0
                slot[2] += prob

                # calibration: bucket by the stated probability
                b = min(int(prob * 20) / 20.0, 0.95)   # 5-point buckets
                cell = bins.setdefault(b, [0, 0])
                cell[0] += 1
                cell[1] += 1 if hit else 0

                outcome_home = 1.0 if margin > 0 else 0.0
                brier_sum += (exp_home - outcome_home) ** 2

        # ---- now update ----
        if margin > 0:
            actual, edge = 1.0, (r_home + hfa) - r_away
        elif margin < 0:
            actual, edge = 0.0, r_away - (r_home + hfa)
        else:
            actual, edge = 0.5, 0.0
        mult = 1.0 if margin == 0 else elo._mov_multiplier(margin, max(edge, 0.0))
        delta = p["k"] * mult * (actual - exp_home)
        ratings[home] = r_home + delta
        ratings[away] = r_away - delta

    return {"league": league, "seasons": sorted(seasons), "tested": tested,
            "correct": correct, "ties": ties, "home_baseline": home_baseline,
            "by_tier": by_tier, "bins": bins,
            "brier": brier_sum / tested if tested else None}


def render(res):
    if not res["tested"]:
        return (f"No {res['league'].upper()} games to test in "
                f"{', '.join(str(s) for s in res['seasons'])}.")
    n, c = res["tested"], res["correct"]
    out = [f"{res['league'].upper()} walk-forward backtest — seasons "
           f"{', '.join(str(s) for s in res['seasons'])}",
           f"  Each pick used only games that had already finished at its kickoff.",
           "",
           f"  model         {c}-{n - c}   {c / n * 100:.1f}%",
           f"  always home   {res['home_baseline']}-{n - res['home_baseline']}   "
           f"{res['home_baseline'] / n * 100:.1f}%   <- the baseline to beat",
           f"  Brier score   {res['brier']:.4f}   (lower is better; 0.25 = coin flip)"]
    if res["ties"]:
        out.append(f"  {res['ties']} tie(s) excluded — no winner to pick")

    out.append("\n  By confidence tier")
    order = {"lock": 0, "lean": 1, "coin-flip": 2}
    for tier in sorted(res["by_tier"], key=lambda t: order.get(t, 9)):
        cnt, wins, psum = res["by_tier"][tier]
        out.append(f"    {tier:<10} {wins}-{cnt - wins}  actual {wins / cnt * 100:5.1f}%   "
                   f"claimed {psum / cnt * 100:5.1f}%")

    out.append("\n  Calibration — does a stated 70% actually win 70% of the time?")
    out.append(f"    {'stated':<12} {'games':>6} {'actual':>8}  {'gap':>7}")
    for b in sorted(res["bins"]):
        cnt, wins = res["bins"][b]
        if cnt < 15:
            continue  # too few to say anything
        actual = wins / cnt
        out.append(f"    {b * 100:>3.0f}-{(b + 0.05) * 100:>3.0f}%    {cnt:>6} "
                   f"{actual * 100:>7.1f}%  {(actual - (b + 0.025)) * 100:>+6.1f}")
    return "\n".join(out)
