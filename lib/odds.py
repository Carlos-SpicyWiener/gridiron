"""Market lines as a benchmark, never as an input.

Two sources, and the free one is the default. ESPN embeds a book's line in the
scoreboard payload the sync already fetches, so market data costs no key and no
extra request; that ingestion lives in espn.py. This module adds The Odds API
on top for a multi-book consensus, which is strictly better but needs a key.
Neither is ever fed to the model.

The model does not read the market. Odds are stored alongside predictions purely
so the record can answer the only question that matters about a picks system:
does it beat the closing line, or is it just re-deriving it more slowly?

Requires GRIDIRON_ODDS_KEY. Without one, the ESPN line still populates the
market columns; a genuinely unobserved line stays NULL, which reads as "no line
observed", not "no line existed".
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import db

API = "https://api.the-odds-api.com/v4"
KEY = os.environ.get("GRIDIRON_ODDS_KEY")
SPORT_KEY = {"nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf"}

# A line and an ESPN game must start within this window to be the same game.
MATCH_WINDOW = timedelta(hours=30)

_STOP = {"the", "university", "of", "state", "st"}


def have_key():
    return bool(KEY)


def _norm(name):
    """Reduce a team name to comparable tokens. 'Ole Miss Rebels' -> {ole, miss, rebels}."""
    name = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return {t for t in name.split() if t and t not in _STOP}


def _similarity(a, b):
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def american_to_prob(price):
    """American moneyline -> implied probability, vig still included."""
    if price is None:
        return None
    price = float(price)
    if price < 0:
        return -price / (-price + 100.0)
    return 100.0 / (price + 100.0)


def devig(p_home, p_away):
    """Strip the book's margin by normalising the pair to sum to 1.

    This is the simple proportional method. It slightly overstates the favourite
    versus a power/Shin devig, so treat the result as close-enough, not exact.
    """
    if p_home is None or p_away is None:
        return None, None
    total = p_home + p_away
    if total <= 0:
        return None, None
    return p_home / total, p_away / total


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "gridiron/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        remaining = resp.headers.get("x-requests-remaining")
        return json.loads(resp.read().decode("utf-8")), remaining


def fetch_lines(league, regions="us", books=None):
    if not KEY:
        raise RuntimeError("GRIDIRON_ODDS_KEY is not set")
    params = {"apiKey": KEY, "regions": regions, "markets": "h2h,spreads",
              "oddsFormat": "american", "dateFormat": "iso"}
    if books:
        params["bookmakers"] = books
    url = f"{API}/sports/{SPORT_KEY[league]}/odds/?" + urllib.parse.urlencode(params)
    try:
        return _fetch(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"odds api {e.code}: {body}") from None


def _parse_kick(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def match_event(conn, league, event):
    """Find the game an odds event refers to, or None.

    Matching is on kickoff proximity plus name similarity for BOTH sides, and the
    combined score has to clear a floor. A wrong match would silently attribute a
    line to the wrong game, so it is better to record nothing.
    """
    kick = _parse_kick(event.get("commence_time"))
    if not kick:
        return None
    lo = (kick - MATCH_WINDOW).isoformat().replace("+00:00", "Z")
    hi = (kick + MATCH_WINDOW).isoformat().replace("+00:00", "Z")
    rows = conn.execute(
        "SELECT g.id, ht.name AS home_name, at.name AS away_name "
        "FROM game g JOIN team ht ON ht.id = g.home_team_id JOIN team at ON at.id = g.away_team_id "
        "WHERE g.league = ? AND g.kickoff_utc BETWEEN ? AND ?", (league, lo, hi)).fetchall()

    best, best_score = None, 0.0
    for r in rows:
        score = (_similarity(event.get("home_team"), r["home_name"])
                 + _similarity(event.get("away_team"), r["away_name"])) / 2.0
        if score > best_score:
            best, best_score = r["id"], score
    return best if best_score >= 0.45 else None


def ingest(conn, league, progress=None):
    """Store one snapshot per book per game. Returns (matched, unmatched, remaining)."""
    events, remaining = fetch_lines(league)
    stamp = db.now()
    matched = unmatched = 0
    for event in events:
        game_id = match_event(conn, league, event)
        if not game_id:
            unmatched += 1
            if progress:
                progress(f"    unmatched: {event.get('away_team')} @ {event.get('home_team')}")
            continue
        matched += 1
        home_name, away_name = event.get("home_team"), event.get("away_team")
        for bk in event.get("bookmakers", []):
            home_price = away_price = home_spread = None
            for market in bk.get("markets", []):
                for out in market.get("outcomes", []):
                    is_home = _similarity(out.get("name"), home_name) >= \
                        _similarity(out.get("name"), away_name)
                    if market.get("key") == "h2h":
                        if is_home:
                            home_price = out.get("price")
                        else:
                            away_price = out.get("price")
                    elif market.get("key") == "spreads" and is_home:
                        home_spread = out.get("point")
            if home_price is None and home_spread is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO odds_snapshot (game_id, book, fetched_at, home_price, "
                "away_price, home_spread) VALUES (?, ?, ?, ?, ?, ?)",
                (game_id, bk.get("key", "?"), stamp, home_price, away_price, home_spread))
    conn.commit()
    return matched, unmatched, remaining


def consensus(conn, game_id):
    """Median de-vigged home probability across the most recent snapshot's books."""
    # Latest price per book, opening lines excluded: an opening number is a
    # historical artefact, not a competing quote, and averaging it with the
    # current line would report a market position nobody is offering.
    rows = conn.execute(
        "SELECT s.home_price, s.away_price FROM odds_snapshot s "
        "JOIN (SELECT book, MAX(fetched_at) AS at FROM odds_snapshot "
        "      WHERE game_id = ? AND book NOT LIKE '%-open' GROUP BY book) latest "
        "  ON latest.book = s.book AND latest.at = s.fetched_at "
        "WHERE s.game_id = ? AND s.home_price IS NOT NULL AND s.away_price IS NOT NULL",
        (game_id, game_id)).fetchall()
    probs = []
    for r in rows:
        ph, _ = devig(american_to_prob(r["home_price"]), american_to_prob(r["away_price"]))
        if ph is not None:
            probs.append(ph)
    if not probs:
        return None
    probs.sort()
    mid = len(probs) // 2
    return probs[mid] if len(probs) % 2 else (probs[mid - 1] + probs[mid]) / 2.0
