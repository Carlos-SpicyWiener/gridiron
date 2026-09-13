"""The scan: which games are mispriced, by how much, and what to stake.

Joins three things that disagree with each other -- our model's probability, the
book's de-vigged consensus, and Kalshi's live ask -- and only calls something a
bet when every gate in spec section 4 passes. Everything else is recorded with
the reason it was skipped, so the gates can be graded later like anything else.

The probability used here is the CALIBRATED one. Kelly puts p straight into the
numerator of the stake, so the raw stated number would systematically mis-size.
"""
import datetime as dt

from . import calibration, db, fees, kalshi, sizing

MIN_EDGE = 0.05
MAX_SPREAD = 0.04
MIN_OPEN_INTEREST = 1000.0
SUSPECT_SAME_SIDE = 0.20
SUSPECT_FLIP = 0.10
STALE_RATING_DAYS = 14
MIN_SEASON_GAMES = 3
# Kalshi and the book are both markets. If they disagree by more than this, one
# of them is stale or the market is mismapped -- either way it is a data problem
# wearing an edge's clothing, not an opportunity. Nothing else catches this:
# the other gates compare the model to each market, never the markets to
# each other.
MAX_BOOK_PRICE_GAP = 0.15


class Candidate:
    __slots__ = ("league", "game_id", "kickoff", "pick_name", "opponent", "ticker",
                 "p_raw", "p_cal", "market_prob", "flip", "bid", "ask", "spread",
                 "open_interest", "edge", "size", "gate")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))


def _window(date):
    """Kalshi dates the event by local kickoff; ours is UTC and can roll over."""
    midnight = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc)
    fmt = lambda d: d.isoformat().replace("+00:00", "Z")
    return fmt(midnight - dt.timedelta(hours=12)), fmt(midnight + dt.timedelta(hours=42))


def candidate_games(conn, league, date):
    lo, hi = _window(date)
    return conn.execute(
        "SELECT g.id, g.away_team_id, g.home_team_id, "
        "       at.name AS away_name, at.abbrev AS away_abbrev, "
        "       ht.name AS home_name, ht.abbrev AS home_abbrev "
        "FROM game g "
        "JOIN team at ON at.id = g.away_team_id "
        "JOIN team ht ON ht.id = g.home_team_id "
        "WHERE g.league = ? AND g.kickoff_utc BETWEEN ? AND ? "
        "  AND g.status = 'scheduled' AND g.kickoff_utc > ?",
        (league, lo, hi, db.now())).fetchall()


def _prediction(conn, game_id):
    return conn.execute(
        "SELECT p.pick_team_id, p.win_prob, p.market_prob, p.market_pick_team_id, "
        "       p.confidence, t.name AS pick_name, g.kickoff_utc, g.season, "
        "       g.home_team_id, g.away_team_id, "
        "       ht.name AS home_name, at.name AS away_name, "
        "       pt.tier AS pick_tier, ot.tier AS opp_tier "
        "FROM prediction p "
        "JOIN game g ON g.id = p.game_id "
        "JOIN team t  ON t.id = p.pick_team_id "
        "JOIN team ht ON ht.id = g.home_team_id "
        "JOIN team at ON at.id = g.away_team_id "
        "JOIN team pt ON pt.id = p.pick_team_id "
        "JOIN team ot ON ot.id = CASE WHEN p.pick_team_id = g.home_team_id "
        "                             THEN g.away_team_id ELSE g.home_team_id END "
        "WHERE p.game_id = ? AND p.correct IS NULL "
        "  AND g.status = 'scheduled' AND g.kickoff_utc > ?",
        (game_id, db.now())).fetchone()


def _season_games(conn, team_id, season):
    """Graded games THIS season. rating_current.games is cumulative since 2021."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM game WHERE season = ? AND status = 'final' "
        "AND (home_team_id = ? OR away_team_id = ?)", (season, team_id, team_id)
    ).fetchone()["n"]


def _rating_age_days(conn, team_id):
    row = conn.execute(
        "SELECT computed_at FROM rating_current WHERE team_id = ?", (team_id,)).fetchone()
    if not row or not row["computed_at"]:
        return None
    computed = dt.datetime.fromisoformat(row["computed_at"].replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - computed).total_seconds() / 86400.0


def evaluate(conn, league, game_id, ticker, pick_market, bankroll):
    """Run every gate for one resolved game. Always returns a Candidate."""
    pred = _prediction(conn, game_id)
    if not pred:
        return None

    q = kalshi.quote(pick_market)
    p_raw = pred["win_prob"]
    p_cal = calibration.calibrate(p_raw, league)
    flip = (pred["market_pick_team_id"] is not None
            and pred["market_pick_team_id"] != pred["pick_team_id"])
    # prediction.market_prob is the book's confidence in ITS OWN pick. When the
    # book likes the other side, its probability for OUR side is the complement.
    market_prob = pred["market_prob"]
    if market_prob is not None and flip:
        market_prob = 1.0 - market_prob
    opponent = (pred["away_name"] if pred["pick_team_id"] == pred["home_team_id"]
                else pred["home_name"])

    c = Candidate(league=league, game_id=game_id, kickoff=pred["kickoff_utc"],
                  pick_name=pred["pick_name"], opponent=opponent, ticker=ticker,
                  p_raw=p_raw, p_cal=p_cal, market_prob=market_prob, flip=flip,
                  bid=q.bid if q else None, ask=q.ask if q else None,
                  spread=q.spread if q else None,
                  open_interest=float(pick_market.get("open_interest_fp") or 0.0))

    if q is None:
        c.gate = "no_quote"
        return c

    c.edge = p_cal - q.ask - float(fees.fee(q.ask))
    c.size = sizing.size(p_cal, q.ask, bankroll)

    # Artifact filter first: these say the number itself is untrustworthy.
    if pred["pick_tier"] == "other" or pred["opp_tier"] == "other":
        c.gate = "fbs_vs_fcs"
    elif (_rating_age_days(conn, pred["pick_team_id"]) or 0) > STALE_RATING_DAYS:
        c.gate = "stale_rating"
    elif market_prob is None:
        c.gate = "no_market_reference"       # fails closed: the sanity check can't run
    elif flip and abs(p_cal - market_prob) > SUSPECT_FLIP:
        c.gate = "suspect_direction"
    elif not flip and abs(p_cal - market_prob) > SUSPECT_SAME_SIDE:
        c.gate = "suspect_rating"
    elif (market_prob is not None
          and abs(q.mid - market_prob) > MAX_BOOK_PRICE_GAP):
        c.gate = "price_disagrees_with_book"
    elif c.spread > MAX_SPREAD:
        c.gate = "wide_spread"
    elif c.open_interest < MIN_OPEN_INTEREST:
        c.gate = "thin_book"
    elif c.edge < MIN_EDGE:
        c.gate = "thin_edge"
    elif c.size.contracts < 1:
        c.gate = "stake_below_one_contract"
    else:
        c.gate = "candidate"
        if _season_games(conn, pred["pick_team_id"], pred["season"]) < MIN_SEASON_GAMES:
            c.gate = "candidate"             # advisory only; carryover rating in use
            c.p_raw = p_raw
    return c


def scan(conn, league, bankroll):
    """Fetch live Kalshi markets, resolve them, and gate every one."""
    events = kalshi.by_event(kalshi.open_markets(league))
    out, unresolved = [], []
    for event_ticker, markets in events.items():
        if len(markets) != 2:
            continue
        try:
            parsed = kalshi.parse_event_ticker(event_ticker)
        except ValueError:
            continue
        games = candidate_games(conn, league, parsed.date)
        res = kalshi.resolve(parsed.suffix, [m.get("no_sub_title") for m in markets], games)
        if not res:
            unresolved.append(event_ticker)
            continue
        pred = _prediction(conn, res.game_id)
        if not pred:
            continue
        pick_market = _market_for_team(markets, res, pred["pick_team_id"])
        if pick_market is None:
            unresolved.append(event_ticker)
            continue
        got = evaluate(conn, league, res.game_id, event_ticker, pick_market, bankroll)
        if got:
            out.append(got)
    return out, unresolved


def _market_for_team(markets, resolution, team_id):
    """Which of the event's two markets is the YES side for our pick."""
    wanted = [abbrev for abbrev, tid in resolution.aliases.items() if tid == team_id]
    if not wanted:
        return None
    for market in markets:
        ticker = market.get("ticker", "")
        if ticker.rsplit("-", 1)[-1].upper() == wanted[0].upper():
            return market
    return None


def _remember_aliases(conn, league, aliases):
    """Record Kalshi's spelling. Theirs is not ours -- their WAS is our WSH."""
    for abbrev, team_id in aliases.items():
        conn.execute(
            "INSERT OR IGNORE INTO kalshi_alias (league, kalshi_abbrev, team_id, "
            "first_seen) VALUES (?, ?, ?, ?)", (league, abbrev, team_id, db.now()))


def ingest_snapshots(conn, league, markets=None, stamp=None):
    """Persist one quote per side per poll.

    This is what makes closing-line value knowable. The close cannot be fetched
    at grade time -- a settled market quotes 0.99/0.01 over a 0.00/1.00 book, so
    asking then would turn CLV into a restatement of the win/loss record. It has
    to have been written down before kickoff, which means this has to be running
    on a timer whether or not anyone is looking.

    Append-only, like odds_snapshot: the movement between open and kickoff is
    itself the signal, so nothing here overwrites.
    """
    if markets is None:
        markets = kalshi.open_markets(league)
    stamp = stamp or db.now()
    resolved = 0
    before = conn.total_changes
    for event_ticker, pair in kalshi.by_event(markets).items():
        if len(pair) != 2:
            continue
        try:
            parsed = kalshi.parse_event_ticker(event_ticker)
        except ValueError:
            continue
        res = kalshi.resolve(parsed.suffix,
                             [m.get("no_sub_title") for m in pair],
                             candidate_games(conn, league, parsed.date))
        if not res:
            continue
        resolved += 1
        _remember_aliases(conn, league, res.aliases)
        for m in pair:
            q = kalshi.quote(m)
            if q is None:
                continue
            abbrev = (m.get("ticker") or "").rsplit("-", 1)[-1].upper()
            team_id = res.aliases.get(abbrev)
            if team_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO kalshi_snapshot (game_id, team_id, market_ticker, "
                "fetched_at, yes_bid, yes_ask, mid, open_interest, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (res.game_id, team_id, m.get("ticker"), stamp, q.bid, q.ask, q.mid,
                 float(m.get("open_interest_fp") or 0.0),
                 float(m.get("volume") or 0.0)))
    conn.commit()
    # Count rows THIS call inserted. Counting by timestamp instead would double
    # count whenever two leagues are polled within the same second.
    return {"events_resolved": resolved,
            "snapshots": conn.total_changes - before}
