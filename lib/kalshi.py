"""Kalshi market data, and the one hard problem in it: which game is this?

Robinhood routes sports contracts to Kalshi, whose market-data endpoints are
public. Reading prices is easy. Deciding that KXNFLGAME-26SEP13DALNYG is OUR
game 1457 is not, and getting it wrong attributes a real-money bet to the wrong
team, so everything here refuses rather than guesses.

Three traps, all observed live on 2026-09-12:

  Display names truncate at 13 characters while a market is open, so "New York
  G" scores identically against Giants and Jets under the token similarity
  lib/odds.py uses for The Odds API. Never resolve a side on its own name.

  Display names change format once a market settles: "Chicago" becomes "CHI
  Bears". Anything keyed on the open-market spelling stops matching exactly when
  you go to grade.

  The event ticker's team suffix cannot be split by string surgery, because
  abbreviations are variable length -- GBMIN is GB|MIN, but nothing in the string
  says it isn't GBM|IN.

So resolution runs the other way round: take our games on that date, ask which
one explains BOTH of Kalshi's sides at once, and require the answer to be unique.
The pair is what disambiguates; neither side can do it alone. Only then is the
suffix split, and the resulting abbreviation recorded in kalshi_alias so later
runs are an exact lookup rather than a match.

Kalshi's spelling is not ours: their WAS is our WSH. The alias table stores
theirs.
"""
import datetime as dt
import json
import re
import urllib.parse
import urllib.request

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = {"nfl": "KXNFLGAME", "cfb": "KXNCAAFGAME"}
USER_AGENT = "gridiron/1.0"

_TICKER = re.compile(r"^(?P<series>[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
                     r"(?P<suffix>[A-Z0-9]{4,})$")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# A settled market quotes bid 0.00 / ask 1.00, whose midpoint is a very
# plausible-looking 0.50. Treat that book as absent, everywhere.
_DEAD_BOOK = (0.0, 1.0)


class ParsedTicker:
    __slots__ = ("series", "date", "suffix")

    def __init__(self, series, date, suffix):
        self.series, self.date, self.suffix = series, date, suffix


class Quote:
    __slots__ = ("bid", "ask", "mid", "spread")

    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask
        self.mid = (bid + ask) / 2.0
        self.spread = ask - bid


class Resolution:
    __slots__ = ("game_id", "aliases")

    def __init__(self, game_id, aliases):
        self.game_id, self.aliases = game_id, aliases


def parse_event_ticker(ticker):
    """KXNFLGAME-26SEP13DALNYG -> series, date, 'DALNYG'."""
    m = _TICKER.match(ticker or "")
    if not m or m.group("mon") not in _MONTHS:
        raise ValueError(f"not an event ticker: {ticker!r}")
    date = dt.date(2000 + int(m.group("yy")), _MONTHS[m.group("mon")], int(m.group("dd")))
    return ParsedTicker(m.group("series"), date, m.group("suffix"))


def quote(market):
    """Bid/ask from a market payload, or None if there isn't a real book.

    Prices arrive as dollar-denominated STRINGS ("0.2300"); the integer-cent
    fields older docs mention are not on this response.
    """
    try:
        bid = float(market["yes_bid_dollars"])
        ask = float(market["yes_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    if (bid, ask) == _DEAD_BOOK or ask <= bid:
        return None
    return Quote(bid, ask)


def _field(row, name):
    """Read a column off either a sqlite3.Row or a plain object."""
    try:
        return row[name]
    except (TypeError, IndexError, KeyError):
        return getattr(row, name, None)


def _name_score(subtitle, name, abbrev):
    """How well one Kalshi display string explains one of our team names."""
    s = " ".join((subtitle or "").lower().split())
    n = " ".join((name or "").lower().split())
    if not s or not n:
        return 0.0
    if n.startswith(s):                       # "New York G" <- "New York Giants"
        return 1.0
    parts = s.split()
    if abbrev and parts and parts[0] == abbrev.lower():
        return 0.9                            # "CHI Bears", the settled format
    shared = set(parts) & set(n.split())
    return 0.5 * len(shared) / len(parts) if shared else 0.0


def _pair_score(subtitles, game):
    """Best score over both ways of assigning two subtitles to away/home."""
    if len(subtitles) != 2:
        return 0.0
    a, b = subtitles
    away = (_field(game, "away_name"), _field(game, "away_abbrev"))
    home = (_field(game, "home_name"), _field(game, "home_abbrev"))
    return max(_name_score(a, *away) + _name_score(b, *home),
               _name_score(b, *away) + _name_score(a, *home))


def _abbrev_ok(part, name, abbrev):
    """Could this chunk of the ticker be Kalshi's abbreviation for this team?"""
    p = part.lower()
    if abbrev and p == abbrev.lower():
        return True
    words = (name or "").lower().split()
    if not words:
        return False
    if words[0].startswith(p):                # WAS <- Washington, where ours is WSH
        return True
    return "".join(w[0] for w in words).startswith(p)   # OSU <- Ohio State ...


def _split_suffix(suffix, game):
    """The unique way to cut the suffix into this game's two abbreviations."""
    away_name, away_abbrev = _field(game, "away_name"), _field(game, "away_abbrev")
    home_name, home_abbrev = _field(game, "home_name"), _field(game, "home_abbrev")
    exact, plausible = [], []
    for i in range(1, len(suffix)):
        a, b = suffix[:i], suffix[i:]
        if not (_abbrev_ok(a, away_name, away_abbrev)
                and _abbrev_ok(b, home_name, home_abbrev)):
            continue
        if (a.lower() == (away_abbrev or "").lower()
                and b.lower() == (home_abbrev or "").lower()):
            exact.append((a, b))
        else:
            plausible.append((a, b))
    candidates = exact or plausible
    return candidates[0] if len(candidates) == 1 else None


def resolve(suffix, subtitles, candidate_games, floor=1.2):
    """Which candidate game this event is, and the aliases it teaches us.

    Returns None rather than a best guess: an unresolved event means the alias
    table or the candidate window is wrong, which is a bug to fix, not a market
    condition to shrug at.
    """
    scored = sorted(((_pair_score(subtitles, g), g) for g in candidate_games),
                    key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] < floor:
        return None
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-9:
        return None                            # a tie is a bug, not a coin to flip
    game = scored[0][1]
    split = _split_suffix(suffix, game)
    if not split:
        return None
    away_abbrev, home_abbrev = split
    return Resolution(_field(game, "id"),
                      {away_abbrev: _field(game, "away_team_id"),
                       home_abbrev: _field(game, "home_team_id")})


def fetch(path, **params):
    """GET against the public market-data API. No auth; it is genuinely open."""
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def open_markets(league):
    """Every open single-game winner market for a league, following the cursor."""
    out, cursor = [], None
    while True:
        params = {"series_ticker": SERIES[league], "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = fetch("/markets", **params)
        out.extend(page.get("markets", []))
        cursor = page.get("cursor")
        if not cursor or not page.get("markets"):
            return out


def by_event(markets):
    """Group markets into events, which is the unit resolution works on."""
    events = {}
    for market in markets:
        events.setdefault(market.get("event_ticker"), []).append(market)
    return events
