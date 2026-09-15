"""ESPN ingestion.

ESPN's public JSON endpoints are undocumented and can change without notice, so
every field access here goes through _get() with a default. The rule is that a
shape change should cost us a field, never a crash mid-backfill.

League-specific behaviour lives in lib/leagues.py (paths, calendar shape, which
teams count as 'major'). ingest_events is shared: a scoreboard payload becomes
team/game rows the Elo engine already knows how to replay. Adding a sport is a
config block plus whatever calendar the scoreboard uses — not a new engine.
"""
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db, leagues

SITE = "https://site.api.espn.com/apis/site/v2/sports"
CORE = "https://sports.core.api.espn.com/v2/sports"
UA = "gridiron/1.0 (personal football model; contact via local host)"

# Back-compat alias: football callers and tests can still say espn.LEAGUES.
LEAGUES = leagues.INGEST

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


def scoreboard_url(league, season=None, season_type=None, week=None, dates=None):
    """Build the scoreboard URL. Football keys on week; NBA/MLB/CBB on dates."""
    cfg = LEAGUES[league]
    params = dict(cfg.get("params") or {})
    if dates:
        params["dates"] = dates
    else:
        if season is not None:
            params["dates"] = str(season)
        if season_type is not None:
            params["seasontype"] = str(season_type)
        if week is not None:
            params["week"] = str(week)
    return f"{SITE}/{cfg['sport']}/{cfg['path']}/scoreboard?" + urllib.parse.urlencode(params)


def scoreboard(league, season=None, season_type=None, week=None, dates=None):
    return _fetch(scoreboard_url(league, season=season, season_type=season_type,
                                 week=week, dates=dates))


# --------------------------------------------------------------------------
# major-sport membership — decides a college team's starting rating
# --------------------------------------------------------------------------
def major_team_ids(league, season):
    """Authoritative 'major' roster for a season, via the core API group tree.

    CFB group 80 is FBS; CBB group 50 is Division I. Children are conferences;
    each conference lists its teams. Anything not in the set starts at other_base.
    NBA/MLB return None — every team is major.
    """
    cfg = LEAGUES[league]
    group = cfg.get("major_group")
    if group is None:
        return None
    import re
    out = set()
    kids = _fetch(
        f"{CORE}/{cfg['sport']}/leagues/{cfg['core']}/seasons/{season}/types/2/"
        f"groups/{group}/children?limit=50")
    for item in kids.get("items", []):
        m = re.search(r"/groups/(\d+)", item.get("$ref", ""))
        if not m:
            continue
        time.sleep(THROTTLE_S)
        teams = _fetch(
            f"{CORE}/{cfg['sport']}/leagues/{cfg['core']}/seasons/{season}/types/2/"
            f"groups/{m.group(1)}/teams?limit=50")
        for t in teams.get("items", []):
            tm = re.search(r"/teams/(\d+)", t.get("$ref", ""))
            if tm:
                out.add(tm.group(1))
    return out


def fbs_team_ids(season):
    """Back-compat wrapper. CFB FBS membership is group 80."""
    return major_team_ids("cfb", season)


# --------------------------------------------------------------------------
# scoreboard -> rows
# --------------------------------------------------------------------------
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


def _event_season(event, payload):
    year = _get(event, "season", "year") or _get(payload, "season", "year")
    stype = _get(event, "season", "type")
    if stype is None:
        stype = _get(payload, "season", "type", default=2)
    return year, stype


# --------------------------------------------------------------------------
# odds carried inside the scoreboard payload
# --------------------------------------------------------------------------
# ESPN embeds a book's line (DraftKings, currently) in each competition. That
# makes market data free in the most literal sense: it arrives in a request we
# already make, with no key, no signup and no extra round trip. One book is a
# thinner benchmark than a multi-book consensus, but a major book's closing
# line is the number worth being measured against anyway.
#
# Both the opening and closing price are published, so line movement is stored
# too — as a separate pseudo-book, because an opening price is not a competing
# quote and must never be averaged with the close.
#
# Basketball and baseball payloads often omit this block. NULL stays honest.

def _american(value):
    """ESPN gives prices as strings like '-170' or 'EVEN'."""
    if value is None:
        return None
    text = str(value).strip().upper().replace("+", "")
    if text in ("EVEN", "EV", "PK"):
        return 100
    try:
        return int(float(text))
    except ValueError:
        return None


def ingest_odds(conn, game_id, comp, stamp):
    """Store the book's line for a game, if it moved since the last snapshot.

    Returns the number of snapshot rows written (0, 1 or 2).
    """
    written = 0
    for od in (comp.get("odds") or []):
        provider = _get(od, "provider", "name", default="book")
        spread = od.get("spread")
        try:
            spread = float(spread) if spread is not None else None
        except (TypeError, ValueError):
            spread = None
        ml = od.get("moneyline") or {}
        for phase, suffix in (("close", ""), ("open", "-open")):
            home = _american(_get(ml, "home", phase, "odds"))
            away = _american(_get(ml, "away", phase, "odds"))
            if home is None or away is None:
                continue
            book = f"{provider}{suffix}".lower().replace(" ", "-")
            # Append only when something actually moved. The table is a history
            # of the market, not a log of how often the sync ran.
            last = conn.execute(
                "SELECT home_price, away_price, home_spread FROM odds_snapshot "
                "WHERE game_id = ? AND book = ? ORDER BY fetched_at DESC LIMIT 1",
                (game_id, book)).fetchone()
            this_spread = spread if phase == "close" else None
            if last and last["home_price"] == home and last["away_price"] == away \
                    and last["home_spread"] == this_spread:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO odds_snapshot (game_id, book, fetched_at, "
                "home_price, away_price, home_spread) VALUES (?, ?, ?, ?, ?, ?)",
                (game_id, book, stamp, home, away, this_spread))
            written += 1
        break  # ESPN lists one provider; take the first and stop
    return written


def ingest_events(conn, league, payload, major_ids=None):
    """Write every event in a scoreboard payload. Returns (seen, new, odds_rows)."""
    cfg = LEAGUES[league]
    allowed_types = set(cfg.get("season_types") or (2, 3))
    seen = new = odds_rows = 0
    stamp = db.now()
    for event in payload.get("events", []):
        season, season_type = _event_season(event, payload)
        try:
            season_type = int(season_type)
        except (TypeError, ValueError):
            season_type = 2
        if season_type not in allowed_types:
            continue

        comp = _get(event, "competitions", 0)
        if not comp:
            continue
        competitors = comp.get("competitors") or []
        if len(competitors) != 2:
            continue  # nothing to model without exactly two sides

        # Exhibitions are not evidence. The Pro Bowl / All-Star Game arrives in
        # the same feed as real games and would otherwise create phantom teams
        # (AFC, NFC, AL, NL) that rate alongside real ones.
        if _get(comp, "type", "abbreviation", default="") == "ALLSTAR":
            continue

        sides = {}
        for c in competitors:
            tm = c.get("team") or {}
            espn_id = str(tm.get("id", ""))
            if not espn_id:
                break
            if cfg.get("major_group") is not None:
                tier = "major" if (major_ids and espn_id in major_ids) else "other"
            else:
                tier = "major"
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

        game_id, created = db.upsert_game(conn, {
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
        if status == "scheduled":
            # Only pre-game lines are meaningful; a book's number on a finished
            # game is not a forecast of anything.
            odds_rows += ingest_odds(conn, game_id, comp, stamp)
    return seen, new, odds_rows


def _calendar_days(payload):
    """YYYYMMDD strings from an ESPN scoreboard calendar, if it has one."""
    leagues_block = payload.get("leagues") or []
    cal = (leagues_block[0] or {}).get("calendar") if leagues_block else None
    if not isinstance(cal, list):
        return []
    out = []
    for item in cal:
        text = str(item)
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            ymd = text[:10].replace("-", "")
            if ymd.isdigit() and ymd not in out:
                out.append(ymd)
    return out


def _ymd_range(year, start_md, end_md):
    start = dt.date(year, start_md[0], start_md[1])
    end = dt.date(year, end_md[0], end_md[1])
    out = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y%m%d"))
        cur += dt.timedelta(days=1)
    return out


def season_dates(league, season):
    """Days to fetch for a date-based backfill. No network for fill-window leagues."""
    cfg = LEAGUES[league]
    fill = cfg.get("fill_season_md")
    if fill:
        return _ymd_range(season, fill[0], fill[1])
    payload = scoreboard(league, dates=str(season))
    days = _calendar_days(payload)
    if days:
        return days
    # Fail closed on an empty calendar rather than walking 365 empty days.
    raise RuntimeError(f"no ESPN calendar for {league} {season}")


def _chunks(items, n):
    n = max(int(n or 1), 1)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _dates_param(days):
    if not days:
        return None
    if len(days) == 1:
        return days[0]
    return f"{days[0]}-{days[-1]}"


def ingest_season(conn, league, season, progress=None):
    """Pull a whole season. Returns (seen, new)."""
    cfg = LEAGUES[league]
    majors = major_team_ids(league, season) if cfg.get("major_group") else None
    if progress and majors is not None:
        label = "FBS" if league == "cfb" else "D1"
        progress(f"    {label} roster for {season}: {len(majors)} teams")

    if cfg.get("calendar") == "week":
        return _ingest_season_weeks(conn, league, season, majors, progress)
    return _ingest_season_days(conn, league, season, majors, progress)


def _ingest_season_weeks(conn, league, season, majors, progress):
    """Football: regular then postseason, one week at a time."""
    cfg = LEAGUES[league]
    total_seen = total_new = 0
    for season_type, weeks in ((2, cfg["reg_weeks"]), (3, cfg["post_weeks"])):
        for week in range(1, weeks + 1):
            try:
                payload = scoreboard(league, season, season_type, week)
            except RuntimeError as e:
                if progress:
                    progress(f"    skip {season} t{season_type} w{week}: {e}")
                continue
            seen, new, _ = ingest_events(conn, league, payload, majors)
            total_seen += seen
            total_new += new
            if progress and seen:
                progress(f"    {season} type{season_type} week {week:>2}: {seen:>3} games "
                         f"({new} new)")
            conn.commit()
            time.sleep(THROTTLE_S)
    return total_seen, total_new


def _ingest_season_days(conn, league, season, majors, progress):
    """NBA/MLB/CBB: walk the season's dates, chunked to stay under ESPN's cap."""
    cfg = LEAGUES[league]
    days = season_dates(league, season)
    total_seen = total_new = 0
    for chunk in _chunks(days, cfg.get("chunk_days", 1)):
        try:
            payload = scoreboard(league, dates=_dates_param(chunk))
        except RuntimeError as e:
            if progress:
                progress(f"    skip {league} {chunk[0]}: {e}")
            continue
        seen, new, _ = ingest_events(conn, league, payload, majors)
        total_seen += seen
        total_new += new
        if progress and seen:
            label = chunk[0] if len(chunk) == 1 else f"{chunk[0]}-{chunk[-1]}"
            progress(f"    {season} {label}: {seen:>3} games ({new} new)")
        conn.commit()
        time.sleep(THROTTLE_S)
    return total_seen, total_new


def ingest_current(conn, league, progress=None):
    """Refresh recently finished games and the upcoming board.

    Football: previous / current / next week, same as Phase 1.
    Day-based sports: yesterday through +horizon_days, so `slate` has something
    to show on a sport that plays every night.
    """
    cfg = LEAGUES[league]
    if cfg.get("calendar") == "week":
        return _ingest_current_weeks(conn, league, progress)
    return _ingest_current_days(conn, league, progress)


def _ingest_current_weeks(conn, league, progress):
    cfg = LEAGUES[league]
    params = dict(cfg.get("params") or {})
    payload = _fetch(f"{SITE}/{cfg['sport']}/{cfg['path']}/scoreboard?"
                     + urllib.parse.urlencode(params))
    season = _get(payload, "season", "year")
    season_type = _get(payload, "season", "type", default=2)
    week = _get(payload, "week", "number", default=1)
    majors = major_team_ids(league, season) if cfg.get("major_group") else None

    total_seen = total_new = total_odds = 0
    for wk in (week - 1, week, week + 1):
        if wk < 1:
            continue
        try:
            pl = payload if wk == week else scoreboard(league, season, season_type, wk)
        except RuntimeError as e:
            if progress:
                progress(f"    skip week {wk}: {e}")
            continue
        seen, new, odds_rows = ingest_events(conn, league, pl, majors)
        total_seen += seen
        total_new += new
        total_odds += odds_rows
        if progress:
            progress(f"    {league.upper()} {season} week {wk}: {seen} games ({new} new"
                     + (f", {odds_rows} odds moves" if odds_rows else "") + ")")
        conn.commit()
        time.sleep(THROTTLE_S)
    return season, week, total_seen, total_new, total_odds


def _ingest_current_days(conn, league, progress):
    cfg = LEAGUES[league]
    today = dt.datetime.now(dt.timezone.utc).date()
    horizon = int(cfg.get("horizon_days") or 7)
    days = [(today + dt.timedelta(days=i)).strftime("%Y%m%d")
            for i in range(-1, horizon + 1)]

    # Membership is keyed on ESPN's season year (NBA 2024-25 is year 2025).
    # Probe today so we don't walk the conference tree for the wrong year.
    try:
        probe = scoreboard(league, dates=today.strftime("%Y%m%d"))
    except RuntimeError:
        probe = {}
    season = _get(probe, "season", "year")
    if season is None:
        for event in probe.get("events") or []:
            season = _get(event, "season", "year")
            if season:
                break
    if season is None:
        season = today.year

    majors = major_team_ids(league, season) if cfg.get("major_group") else None
    total_seen = total_new = total_odds = 0
    for chunk in _chunks(days, cfg.get("chunk_days", 1)):
        try:
            pl = scoreboard(league, dates=_dates_param(chunk))
        except RuntimeError as e:
            if progress:
                progress(f"    skip {league} {chunk[0]}: {e}")
            continue
        seen, new, odds_rows = ingest_events(conn, league, pl, majors)
        total_seen += seen
        total_new += new
        total_odds += odds_rows
        if progress and seen:
            label = chunk[0] if len(chunk) == 1 else f"{chunk[0]}-{chunk[-1]}"
            progress(f"    {league.upper()} {label}: {seen} games ({new} new"
                     + (f", {odds_rows} odds moves" if odds_rows else "") + ")")
        conn.commit()
        time.sleep(THROTTLE_S)
    return season, None, total_seen, total_new, total_odds
