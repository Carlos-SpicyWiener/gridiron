"""Export the pick record to plain text files.

The database is deliberately not tracked in git — it is a rebuildable cache of
ESPN's data, and a 1.6MB binary makes a poor diff. But one thing in it is NOT
rebuildable: what the model predicted, and when. Predictions lock at kickoff by
design, so a lost database cannot be reconstructed by re-running anything. Losing
it would silently reset the record to zero.

So the record is exported to CSV, which is small, diffable, and survives in git.
Every row carries the timestamp the pick was made, which is the field that makes
the record auditable rather than merely plausible.
"""
import csv
import os

from . import elo, picks

HEADER = ["game_id", "league", "season", "week", "kickoff_utc", "away_team", "home_team",
          "neutral_site", "model", "picked_at_utc", "pick", "win_prob", "confidence",
          "home_rating_at_pick", "away_rating_at_pick", "market_pick", "market_prob",
          "home_score", "away_score", "winner", "result", "graded_at_utc"]


def _rows(conn, model):
    sql = ("SELECT p.game_id, g.league, g.season, g.week, g.kickoff_utc, g.neutral, "
           "       at.name AS away_team, ht.name AS home_team, p.model, p.made_at, "
           "       pt.name AS pick, p.win_prob, p.confidence, p.home_rating, p.away_rating, "
           "       mt.name AS market_pick, p.market_prob, g.home_score, g.away_score, "
           "       p.correct, p.graded_at, g.status "
           "FROM prediction p JOIN game g ON g.id = p.game_id "
           "JOIN team ht ON ht.id = g.home_team_id JOIN team at ON at.id = g.away_team_id "
           "JOIN team pt ON pt.id = p.pick_team_id "
           "LEFT JOIN team mt ON mt.id = p.market_pick_team_id "
           "WHERE p.model = ? ORDER BY g.kickoff_utc, p.game_id")
    for r in conn.execute(sql, (model,)):
        if r["status"] == "final" and r["home_score"] is not None:
            if r["home_score"] > r["away_score"]:
                winner = r["home_team"]
            elif r["away_score"] > r["home_score"]:
                winner = r["away_team"]
            else:
                winner = "tie"
        else:
            winner = ""
        # "" means not yet graded, and is written as empty rather than as 0 —
        # an ungraded pick is not a loss.
        result = "" if r["correct"] is None else ("win" if r["correct"] else "loss")
        yield [
            r["game_id"], r["league"], r["season"], r["week"], r["kickoff_utc"],
            r["away_team"], r["home_team"], 1 if r["neutral"] else 0, r["model"],
            r["made_at"], r["pick"], f"{r['win_prob']:.4f}", r["confidence"],
            "" if r["home_rating"] is None else f"{r['home_rating']:.1f}",
            "" if r["away_rating"] is None else f"{r['away_rating']:.1f}",
            r["market_pick"] or "",
            "" if r["market_prob"] is None else f"{r['market_prob']:.4f}",
            "" if r["home_score"] is None else r["home_score"],
            "" if r["away_score"] is None else r["away_score"],
            winner, result, r["graded_at"] or "",
        ]


def export(conn, out_dir, model=elo.MODEL_VERSION):
    """Write predictions.csv, ratings.csv and summary.md. Returns a count dict.

    Output is a pure function of the database. Nothing here stamps wall-clock
    time: the cycle runs three times a day and commits the result, so a
    "generated at" line would make every run a diff and bury the real changes
    under a thousand empty commits a year. The provenance that matters is when
    the DATA was computed, and that comes from the rows themselves.
    """
    os.makedirs(out_dir, exist_ok=True)

    pred_path = os.path.join(out_dir, "predictions.csv")
    rows = list(_rows(conn, model))
    with open(pred_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)

    rate_path = os.path.join(out_dir, "ratings.csv")
    rating_rows = conn.execute(
        "SELECT t.league, t.name, t.tier, r.rating, r.games, r.computed_at "
        "FROM rating_current r JOIN team t ON t.id = r.team_id "
        "WHERE r.games > 0 ORDER BY t.league, r.rating DESC").fetchall()
    with open(rate_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["league", "team", "tier", "rating", "games_rated", "computed_at_utc"])
        for r in rating_rows:
            w.writerow([r["league"], r["name"], r["tier"], f"{r['rating']:.1f}",
                        r["games"], r["computed_at"]])

    graded = sum(1 for r in rows if r[20])
    sum_path = os.path.join(out_dir, "summary.md")
    computed_at = rating_rows[0]["computed_at"] if rating_rows else "never"
    last_graded = conn.execute(
        "SELECT MAX(graded_at) AS at FROM prediction WHERE model = ?", (model,)).fetchone()["at"]
    with open(sum_path, "w") as fh:
        fh.write("# Pick record\n\nWritten by `gridiron export`. "
                 "Every figure below is measured from the database, not derived.\n\n")
        fh.write(f"- predictions: {len(rows)} ({graded} graded, "
                 f"{len(rows) - graded} awaiting a result)\n")
        fh.write(f"- teams rated: {len(rating_rows)}\n")
        fh.write(f"- ratings computed at: {computed_at}\n")
        fh.write(f"- last graded at: {last_graded or 'nothing graded yet'}\n\n")
        fh.write("Source of truth is `data/gridiron.db`, which is not tracked. These files\n"
                 "exist so the record survives losing it — predictions lock at kickoff and\n"
                 "cannot be regenerated.\n\n## Accuracy\n\n```\n")
        fh.write(picks.render_record(conn, model=model))
        fh.write("\n```\n")
    return {"predictions": len(rows), "graded": graded, "ratings": len(rating_rows),
            "dir": out_dir}
