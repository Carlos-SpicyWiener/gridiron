"""ESPN ingestion.

ESPN's public JSON endpoints are undocumented and can change without notice, so
every field access here goes through _get() with a default. The rule is that a
shape change should cost us a field, never a crash mid-backfill.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

SITE = "https://site.api.espn.com/apis/site/v2/sports/football"
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues"
UA = "gridiron/1.0 (personal football model; contact via local host)"

LEAGUES = {
    "nfl": {"path": "nfl", "core": "nfl", "reg_weeks": 18, "post_weeks": 5, "params": {}},
    # groups=80 restricts the scoreboard to FBS games. FCS opponents still show
    # up inside those games, which is what we want: a buy game is real evidence.
    "cfb": {"path": "college-football", "core": "college-football", "reg_weeks": 15,
            "post_weeks": 1, "params": {"groups": "80", "limit": "400"}},
}

# ESPN sometimes rate-limits a fast backfill. One polite pause between calls.
THROTTLE_S = 0.6


def _fetch(url, timeout=30, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))  # linear backoff; ESPN forgives quickly
    raise RuntimeError(f"fetch failed after {retries} tries: {url} ({last})")


def _get(obj, *path, default=None):
    """Walk a nested dict/list path, returning default the moment it breaks."""
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return default
    return cur if cur is not None else default


# --------------------------------------------------------------------------
# FBS membership — decides a college team's starting rating
# --------------------------------------------------------------------------
def fbs_team_ids(season):
    """Authoritative FBS roster for a season, via the core API group tree.

    group 80 is FBS; its children are the conferences; each conference lists its
    teams. Anything not in this set is treated as non-FBS and starts lower.
    """
    import re
    out = set()
    kids = _fetch(f"{CORE}/college-football/seasons/{season}/types/2/groups/80/children?limit=50")
    for item in kids.get("items", []):
        m = re.search(r"/groups/(\d+)", item.get("$ref", ""))
        if not m:
            continue
        time.sleep(THROTTLE_S)
        teams = _fetch(f"{CORE}/college-football/seasons/{season}/types/2/"
                       f"groups/{m.group(1)}/teams?limit=50")
        for t in teams.get("items", []):
            tm = re.search(r"/teams/(\d+)", t.get("$ref", ""))
            if tm:
                out.add(tm.group(1))
    return out


# --------------------------------------------------------------------------
# scoreboard -> rows
# --------------------------------------------------------------------------
def scoreboard(league, season, season_type, week):
    cfg = LEAGUES[league]
    params = dict(cfg["params"])
    params.update({"dates": str(season), "seasontype": str(season_type), "week": str(week)})
    url = f"{SITE}/{cfg['path']}/scoreboard?" + urllib.parse.urlencode(params)
    return _fetch(url)


def _status(event):
    """Map ESPN's status vocabulary onto ours, keeping abandoned games out."""
    state = _get(event, "status", "type", "state", default="pre")
    completed = bool(_get(event, "status", "type", "completed", default=False))
    name = _get(event, "status", "type", "name", default="")
    if name in ("STATUS_CANCELED", "STATUS_POSTPONED"):
        return "canceled"
    if completed:
        return "final"
    return {"pre": "scheduled", "in": "in", "post": "final"}.get(state, "scheduled")


def ingest_events(conn, league, payload, fbs_ids=None):
    """Write every event in a scoreboard payload. Returns (seen, new)."""
    season = _get(payload, "season", "year")
    season_type = _get(payload, "season", "type", default=2)
    seen = new = 0
    for event in payload.get("events", []):
        comp = _get(event, "competitions", 0)
        if not comp:
            continue
        competitors = comp.get("competitors") or []
        if len(competitors) != 2:
            continue  # nothing to model without exactly two sides

        # Exhibitions are not evidence. The Pro Bowl arrives in the same
        # seasontype=3 feed as the playoffs and would otherwise create two
        # phantom "teams" (AFC, NFC) that rate and rank alongside real ones.
        if _get(comp, "type", "abbreviation", default="") == "ALLSTAR":
            continue

        sides = {}
        for c in competitors:
            tm = c.get("team") or {}
            espn_id = str(tm.get("id", ""))
            if not espn_id:
                break
            if league == "nfl":
                tier = "major"
            else:
                tier = "major" if (fbs_ids and espn_id in fbs_ids) else "other"
            team_id = db.upsert_team(
                conn, league, espn_id,
                tm.get("displayName") or tm.get("name") or f"team {espn_id}",
                tm.get("abbreviation"), str(tm.get("conferenceId") or "") or None, tier)
            score = c.get("score")
            try:
                score = int(score)
            except (TypeError, ValueError):
                score = None
            sides[c.get("homeAway")] = (team_id, score)

        if "home" not in sides or "away" not in sides:
            continue

        status = _status(event)
        home_id, home_score = sides["home"]
        away_id, away_score = sides["away"]
        if status != "final":
            home_score = away_score = None  # never store a partial score as a result

        _, created = db.upsert_game(conn, {
            "league": league,
            "espn_id": str(event.get("id")),
            "season": season or _get(payload, "season", "year", default=0),
            "season_type": season_type,
            "week": _get(event, "week", "number") or _get(payload, "week", "number"),
            "kickoff_utc": event.get("date"),
            "home_team_id": home_id,
            "away_team_id": away_id,
            "neutral": 1 if comp.get("neutralSite") else 0,
            "status": status,
            "home_score": home_score,
            "away_score": away_score,
        })
        seen += 1
        new += 1 if created else 0
    return seen, new


def ingest_season(conn, league, season, progress=None):
    """Pull a whole season, regular then postseason. Returns (seen, new)."""
    cfg = LEAGUES[league]
    fbs = fbs_team_ids(season) if league == "cfb" else None
    if progress and league == "cfb":
        progress(f"    FBS roster for {season}: {len(fbs or [])} teams")
    total_seen = total_new = 0
    for season_type, weeks in ((2, cfg["reg_weeks"]), (3, cfg["post_weeks"])):
        for week in range(1, weeks + 1):
            try:
                payload = scoreboard(league, season, season_type, week)
            except RuntimeError as e:
                if progress:
                    progress(f"    skip {season} t{season_type} w{week}: {e}")
                continue
            seen, new = ingest_events(conn, league, payload, fbs)
            total_seen += seen
            total_new += new
            if progress and seen:
                progress(f"    {season} type{season_type} week {week:>2}: {seen:>3} games "
                         f"({new} new)")
            conn.commit()
            time.sleep(THROTTLE_S)
    return total_seen, total_new


def ingest_current(conn, league, progress=None):
    """Refresh whatever ESPN considers the current week, plus the week before it
    so that results from games that finished since the last sync get picked up."""
    cfg = LEAGUES[league]
    params = dict(cfg["params"])
    payload = _fetch(f"{SITE}/{cfg['path']}/scoreboard?" + urllib.parse.urlencode(params))
    season = _get(payload, "season", "year")
    season_type = _get(payload, "season", "type", default=2)
    week = _get(payload, "week", "number", default=1)
    fbs = fbs_team_ids(season) if league == "cfb" else None

    total_seen = total_new = 0
    for wk in (week - 1, week):
        if wk < 1:
            continue
        try:
            pl = payload if wk == week else scoreboard(league, season, season_type, wk)
        except RuntimeError as e:
            if progress:
                progress(f"    skip week {wk}: {e}")
            continue
        seen, new = ingest_events(conn, league, pl, fbs)
        total_seen += seen
        total_new += new
        if progress:
            progress(f"    {league.upper()} {season} week {wk}: {seen} games ({new} new)")
        conn.commit()
        time.sleep(THROTTLE_S)
    return season, week, total_seen, total_new
