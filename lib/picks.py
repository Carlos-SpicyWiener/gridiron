"""Predictions, grading, and the report renderers shared by the CLI and the MCP."""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, elo, odds

LEAGUE_LABEL = {"nfl": "NFL", "cfb": "CFB"}

# This box runs on UTC, so .astimezone() would render a Sunday 12:00 kickoff as
# Monday. Kickoff times are the whole point of a slate, so the display zone is
# explicit and configurable rather than inherited from the host.
DEFAULT_TZ = "America/Chicago"


def display_tz():
    name = os.environ.get("GRIDIRON_TZ", DEFAULT_TZ)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def tz_label():
    """Short zone name for the moment, e.g. CDT — so a time is never unlabelled."""
    return datetime.now(display_tz()).strftime("%Z")


def _now_dt():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fmt_kick(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(display_tz())
        return dt.strftime("%a %m/%d %I:%M%p").replace(" 0", " ").lstrip("0")
    except (ValueError, AttributeError):
        return iso_str or "?"


def _ratings(conn):
    return {r["team_id"]: r["rating"] for r in conn.execute(
        "SELECT team_id, rating FROM rating_current")}


# --------------------------------------------------------------------------
# predict
# --------------------------------------------------------------------------
def predict(conn, league=None, days=8, model=elo.MODEL_VERSION):
    """Write picks for scheduled games kicking off within `days`.

    Predictions lock at kickoff. A game already under way is skipped rather than
    refreshed, so the graded record can never be improved after the fact.
    """
    ratings = _ratings(conn)
    if not ratings:
        raise RuntimeError("no ratings yet — run `gridiron rate` first")

    now = _now_dt()
    horizon = _iso(now + timedelta(days=days))
    sql = ("SELECT g.*, ht.name AS home_name, at.name AS away_name "
           "FROM game g JOIN team ht ON ht.id = g.home_team_id "
           "JOIN team at ON at.id = g.away_team_id "
           "WHERE g.status = 'scheduled' AND g.kickoff_utc > ? AND g.kickoff_utc <= ?")
    params = [_iso(now), horizon]
    if league:
        sql += " AND g.league = ?"
        params.append(league)
    sql += " ORDER BY g.kickoff_utc"

    written = skipped = locked = unchanged = 0
    for g in conn.execute(sql, params).fetchall():
        r_home = ratings.get(g["home_team_id"])
        r_away = ratings.get(g["away_team_id"])
        if r_home is None or r_away is None:
            skipped += 1
            continue

        existing = conn.execute(
            "SELECT id, made_at FROM prediction WHERE game_id = ? AND model = ?",
            (g["id"], model)).fetchone()
        if existing and g["kickoff_utc"] <= _iso(now):
            locked += 1
            continue

        p_home = elo.matchup(r_home, r_away, g["league"], neutral=bool(g["neutral"]))
        if p_home >= 0.5:
            pick_id, prob = g["home_team_id"], p_home
        else:
            pick_id, prob = g["away_team_id"], 1.0 - p_home

        market_prob_home = odds.consensus(conn, g["id"])
        market_pick_id = market_prob = None
        if market_prob_home is not None:
            if market_prob_home >= 0.5:
                market_pick_id, market_prob = g["home_team_id"], market_prob_home
            else:
                market_pick_id, market_prob = g["away_team_id"], 1.0 - market_prob_home

        # The WHERE clause is the point: an identical re-run must not touch the
        # row. made_at then means "when this pick took its current form", which
        # is the only reading that makes the exported record auditable — and it
        # stops three cycles a day from churning a timestamp into every diff.
        before = conn.total_changes
        conn.execute(
            "INSERT INTO prediction (game_id, model, made_at, pick_team_id, win_prob, "
            "confidence, home_rating, away_rating, market_pick_team_id, market_prob) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id, model) DO UPDATE SET made_at = excluded.made_at, "
            "pick_team_id = excluded.pick_team_id, win_prob = excluded.win_prob, "
            "confidence = excluded.confidence, home_rating = excluded.home_rating, "
            "away_rating = excluded.away_rating, "
            "market_pick_team_id = excluded.market_pick_team_id, "
            "market_prob = excluded.market_prob "
            "WHERE prediction.pick_team_id IS NOT excluded.pick_team_id "
            "   OR abs(prediction.win_prob - excluded.win_prob) > 0.0005 "
            "   OR prediction.market_pick_team_id IS NOT excluded.market_pick_team_id "
            "   OR abs(coalesce(prediction.market_prob, -1.0) "
            "          - coalesce(excluded.market_prob, -1.0)) > 0.0005",
            (g["id"], model, db.now(), pick_id, prob, elo.confidence(prob),
             r_home, r_away, market_pick_id, market_prob))
        if conn.total_changes > before:
            written += 1
        else:
            unchanged += 1
    conn.commit()
    return written, skipped, locked, unchanged


# --------------------------------------------------------------------------
# grade
# --------------------------------------------------------------------------
def grade(conn, model=elo.MODEL_VERSION):
    """Score every ungraded prediction whose game has finished. A tie grades as
    a miss: the pick claimed a winner and there wasn't one."""
    rows = conn.execute(
        "SELECT p.id, p.pick_team_id, g.home_team_id, g.away_team_id, g.home_score, g.away_score "
        "FROM prediction p JOIN game g ON g.id = p.game_id "
        "WHERE p.correct IS NULL AND p.model = ? AND g.status = 'final' "
        "  AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL", (model,)).fetchall()
    stamp = db.now()
    graded = 0
    for r in rows:
        if r["home_score"] > r["away_score"]:
            winner = r["home_team_id"]
        elif r["away_score"] > r["home_score"]:
            winner = r["away_team_id"]
        else:
            winner = None
        conn.execute("UPDATE prediction SET correct = ?, graded_at = ? WHERE id = ?",
                     (1 if winner == r["pick_team_id"] else 0, stamp, r["id"]))
        graded += 1
    conn.commit()
    return graded


# --------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------
def render_slate(conn, league=None, days=8, model=elo.MODEL_VERSION):
    sql = ("SELECT p.*, g.league, g.kickoff_utc, g.neutral, g.week, "
           "       ht.name AS home_name, at.name AS away_name, pt.name AS pick_name, "
           "       mt.name AS market_name "
           "FROM prediction p JOIN game g ON g.id = p.game_id "
           "JOIN team ht ON ht.id = g.home_team_id JOIN team at ON at.id = g.away_team_id "
           "JOIN team pt ON pt.id = p.pick_team_id "
           "LEFT JOIN team mt ON mt.id = p.market_pick_team_id "
           "WHERE p.model = ? AND g.status = 'scheduled' AND g.kickoff_utc > ? "
           "  AND g.kickoff_utc <= ?")
    now = _now_dt()
    params = [model, _iso(now), _iso(now + timedelta(days=days))]
    if league:
        sql += " AND g.league = ?"
        params.append(league)
    sql += " ORDER BY g.league, g.kickoff_utc"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return ("No picks on the board. Run `gridiron sync` for the schedule, then "
                "`gridiron predict`.")

    out, current = [], None
    for r in rows:
        if r["league"] != current:
            current = r["league"]
            out.append(f"\n{LEAGUE_LABEL.get(current, current.upper())} "
                       f"— week {r['week'] or '?'}")
            out.append(f"{'kickoff (' + tz_label() + ')':<18} {'matchup':<46} "
                       f"{'pick':<26} {'conf':<10} market")
            out.append("-" * 112)
        site = " (N)" if r["neutral"] else ""
        matchup = f"{r['away_name']} @ {r['home_name']}{site}"
        pick = f"{r['pick_name']} {r['win_prob'] * 100:.0f}%"
        if r["market_prob"] is None:
            market = "—"
        elif r["market_name"] == r["pick_name"]:
            market = f"agrees {r['market_prob'] * 100:.0f}%"
        else:
            market = f"DISAGREES: {r['market_name']} {r['market_prob'] * 100:.0f}%"
        out.append(f"{_fmt_kick(r['kickoff_utc']):<18} {matchup[:45]:<46} {pick[:25]:<26} "
                   f"{r['confidence']:<10} {market}")
    out.append(f"\n(N) = neutral site. Times are {tz_label()} "
               f"(set GRIDIRON_TZ to change). Percentages are the model's own; "
               f"market column is the de-vigged book consensus when a line was observed.")
    return "\n".join(out)


def render_record(conn, league=None, model=elo.MODEL_VERSION):
    where, params = "p.correct IS NOT NULL AND p.model = ?", [model]
    if league:
        where += " AND g.league = ?"
        params.append(league)

    total = conn.execute(
        f"SELECT COUNT(*) n, SUM(correct) w FROM prediction p JOIN game g ON g.id = p.game_id "
        f"WHERE {where}", params).fetchone()
    if not total["n"]:
        return ("Nothing graded yet. The record fills in as games finish — run "
                "`gridiron sync` then `gridiron grade`.")

    n, w = total["n"], total["w"] or 0
    out = [f"Graded picks: {w}-{n - w}  ({w / n * 100:.1f}%)  model {model}"]

    by_league = conn.execute(
        f"SELECT g.league, COUNT(*) n, SUM(p.correct) w FROM prediction p "
        f"JOIN game g ON g.id = p.game_id WHERE {where} GROUP BY g.league", params).fetchall()
    if len(by_league) > 1:
        out.append("\nBy league")
        for r in by_league:
            rw = r["w"] or 0
            out.append(f"  {LEAGUE_LABEL.get(r['league'], r['league']):<5} "
                       f"{rw}-{r['n'] - rw}  ({rw / r['n'] * 100:.1f}%)")

    out.append("\nBy confidence  (a tier is working if its hit rate tracks its stated band)")
    tiers = conn.execute(
        f"SELECT p.confidence, COUNT(*) n, SUM(p.correct) w, AVG(p.win_prob) avg_p "
        f"FROM prediction p JOIN game g ON g.id = p.game_id WHERE {where} "
        f"GROUP BY p.confidence", params).fetchall()
    order = {"lock": 0, "lean": 1, "coin-flip": 2}
    for r in sorted(tiers, key=lambda x: order.get(x["confidence"], 9)):
        rw = r["w"] or 0
        out.append(f"  {r['confidence']:<10} {rw}-{r['n'] - rw}  "
                   f"actual {rw / r['n'] * 100:5.1f}%   claimed {r['avg_p'] * 100:5.1f}%")

    # Head-to-head against the market, on games where a line was actually seen.
    mkt = conn.execute(
        f"SELECT COUNT(*) n, SUM(p.correct) model_w, "
        f"       SUM(CASE WHEN p.market_pick_team_id IS NOT NULL AND "
        f"            ((p.market_pick_team_id = p.pick_team_id AND p.correct = 1) OR "
        f"             (p.market_pick_team_id <> p.pick_team_id AND p.correct = 0)) "
        f"            THEN 1 ELSE 0 END) market_w "
        f"FROM prediction p JOIN game g ON g.id = p.game_id "
        f"WHERE {where} AND p.market_pick_team_id IS NOT NULL", params).fetchone()
    if mkt and mkt["n"]:
        mw, mn = mkt["market_w"] or 0, mkt["n"]
        mo = mkt["model_w"] or 0
        out.append(f"\nVersus the market  ({mn} games where a line was observed)")
        out.append(f"  model  {mo}-{mn - mo}  ({mo / mn * 100:.1f}%)")
        out.append(f"  market {mw}-{mn - mw}  ({mw / mn * 100:.1f}%)")
        out.append("  Beating the market is the real bar; matching it means the model "
                   "is re-deriving public information.")
    else:
        out.append("\nVersus the market: no lines observed on graded games "
                   "(set GRIDIRON_ODDS_KEY and run `gridiron odds`).")
    return "\n".join(out)


def render_ratings(conn, league="nfl", limit=25):
    rows = conn.execute(
        "SELECT t.name, t.tier, r.rating, r.games FROM rating_current r "
        "JOIN team t ON t.id = r.team_id WHERE r.league = ? AND r.games > 0 "
        "ORDER BY r.rating DESC LIMIT ?", (league, limit)).fetchall()
    if not rows:
        return f"No ratings for {league}. Run `gridiron backfill` then `gridiron rate`."
    out = [f"{LEAGUE_LABEL.get(league, league.upper())} power ratings  "
           f"(Elo, {elo.MODEL_VERSION}; 1500 = average)",
           f"{'#':>3}  {'team':<34} {'rating':>7}  {'gms':>4}"]
    out.append("-" * 54)
    for i, r in enumerate(rows, 1):
        out.append(f"{i:>3}  {r['name'][:33]:<34} {r['rating']:>7.0f}  {r['games']:>4}")
    return "\n".join(out)


def render_matchup(conn, home_query, away_query, league=None, neutral=False):
    """Ad-hoc matchup between any two rated teams, real fixture or not."""
    def find(q):
        sql = ("SELECT t.id, t.name, t.league, r.rating, r.games FROM team t "
               "JOIN rating_current r ON r.team_id = t.id WHERE t.name LIKE ?")
        p = [f"%{q}%"]
        if league:
            sql += " AND t.league = ?"
            p.append(league)
        return conn.execute(sql + " ORDER BY r.games DESC LIMIT 5", p).fetchall()

    home, away = find(home_query), find(away_query)
    for label, hits, q in (("home", home, home_query), ("away", away, away_query)):
        if not hits:
            return f"No rated team matches {label} '{q}'."
        if len(hits) > 1 and hits[0]["name"].lower() != q.lower():
            names = ", ".join(h["name"] for h in hits)
            return f"'{q}' is ambiguous — matches: {names}"
    h, a = home[0], away[0]
    if h["league"] != a["league"]:
        return (f"{h['name']} ({h['league'].upper()}) and {a['name']} ({a['league'].upper()}) "
                f"are in different leagues — their ratings are on separate scales and are "
                f"not comparable.")

    p_home = elo.matchup(h["rating"], a["rating"], h["league"], neutral=neutral)
    winner, prob = (h, p_home) if p_home >= 0.5 else (a, 1.0 - p_home)
    hfa = 0 if neutral else elo.PARAMS[h["league"]]["hfa"]
    return "\n".join([
        f"{a['name']} at {h['name']}" + ("  (neutral site)" if neutral else ""),
        f"  {h['name']:<32} {h['rating']:>7.0f}  ({h['games']} games)",
        f"  {a['name']:<32} {a['rating']:>7.0f}  ({a['games']} games)",
        f"  home-field advantage           {hfa:>7.0f}",
        "",
        f"  PICK: {winner['name']}  {prob * 100:.0f}%   [{elo.confidence(prob)}]",
    ])
