"""League registry: ingest adapters, Elo config, and betting gates.

Phase 1's rule (docs/phase1-spec.md §0.1): every new sport is an ingest adapter
plus a config block, zero engine changes. This file is that config block.
`lib/espn.py` reads INGEST; `lib/elo.py` reads PARAMS; the betting path reads
BETTING. Flip ingest with GRIDIRON_LEAGUES; sized betting stays per-league and
off for anything that has not cleared §7.
"""
import os

# Every league the code knows how to ingest. Order is the CLI default display.
KNOWN = ("nfl", "cfb", "nba", "mlb", "cbb")

# Football stays the daily default. New leagues join `all` only when listed in
# GRIDIRON_LEAGUES, so a one-off NBA backfill cannot take over the cycle.
DEFAULT_ENABLED = ("nfl", "cfb")

# Spec §4.1 league_enabled. Phase 2 does not flip this — each new sport has to
# earn §7 on its own graded picks, its own calibration, and its own CLV.
BETTING = ("nfl", "cfb")

LABEL = {
    "nfl": "NFL",
    "cfb": "CFB",
    "nba": "NBA",
    "mlb": "MLB",
    "cbb": "CBB",
}

# Elo starting points. NFL/CFB are the production football values. NBA/MLB/CBB
# are provisional — they let `rate` / `predict` / `slate` run, they are not a
# claim that the constants are right. See docs/phase2-spec.md §5.
PARAMS = {
    "nfl": {"k": 20.0, "hfa": 48.0, "revert": 0.25, "base": 1500.0, "other_base": 1500.0},
    "cfb": {"k": 38.0, "hfa": 65.0, "revert": 0.35, "base": 1500.0, "other_base": 1200.0},
    "nba": {"k": 20.0, "hfa": 60.0, "revert": 0.25, "base": 1500.0, "other_base": 1500.0},
    "mlb": {"k": 4.0,  "hfa": 24.0, "revert": 0.25, "base": 1500.0, "other_base": 1500.0},
    "cbb": {"k": 32.0, "hfa": 70.0, "revert": 0.40, "base": 1500.0, "other_base": 1200.0},
}

# ESPN site/core paths. `calendar` is "week" (football scoreboard) or "day"
# (NBA/MLB/CBB). Day-based sports iterate YYYYMMDD, not week numbers.
INGEST = {
    "nfl": {
        "sport": "football",
        "path": "nfl",
        "core": "nfl",
        "calendar": "week",
        "reg_weeks": 18,
        "post_weeks": 5,
        "params": {},
        "season_types": (2, 3),
        "major_group": None,
    },
    "cfb": {
        "sport": "football",
        "path": "college-football",
        "core": "college-football",
        "calendar": "week",
        "reg_weeks": 15,
        "post_weeks": 1,
        "params": {"groups": "80", "limit": "400"},
        "season_types": (2, 3),
        "major_group": 80,          # FBS
    },
    "nba": {
        "sport": "basketball",
        "path": "nba",
        "core": "nba",
        "calendar": "day",
        "params": {},
        "season_types": (2, 3),     # skip preseason
        "major_group": None,
        "horizon_days": 10,
        "chunk_days": 3,
    },
    "mlb": {
        "sport": "baseball",
        "path": "mlb",
        "core": "mlb",
        "calendar": "day",
        "params": {},
        "season_types": (2, 3),     # skip spring training
        "major_group": None,
        "horizon_days": 7,
        "chunk_days": 3,
        # ESPN's MLB scoreboard calendar is sparse (spring / ASG / October).
        # Backfill fills every day in this window instead of trusting it.
        "fill_season_md": ((3, 20), (11, 15)),
    },
    "cbb": {
        "sport": "basketball",
        "path": "mens-college-basketball",
        "core": "mens-college-basketball",
        "calendar": "day",
        "params": {"groups": "50", "limit": "400"},
        "season_types": (2, 3),
        "major_group": 50,          # Division I
        "horizon_days": 7,
        "chunk_days": 1,            # a Saturday can be 80+ games
    },
}

# Documented, not wired into scan. Re-verify at season start before flipping
# BETTING. Neighbours (props, series, totals) must not substring-match.
KALSHI_SERIES = {
    "nfl": "KXNFLGAME",
    "cfb": "KXNCAAFGAME",
    "nba": "KXNBAGAME",
    "mlb": "KXMLBGAME",
    "cbb": "KXNCAAMBGAME",
}

# Optional The Odds API keys. Unused unless GRIDIRON_ODDS_KEY is set.
ODDS_SPORT_KEY = {
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
    "nba": "basketball_nba",
    "cbb": "basketball_ncaab",
    "mlb": "baseball_mlb",
}


def enabled():
    """Leagues `sync` / `predict` / `slate --league all` iterate.

    GRIDIRON_LEAGUES=nfl,cfb,nba  (comma-separated). Unknown tokens are ignored.
    An empty or all-unknown value falls back to football, fail closed.
    """
    raw = os.environ.get("GRIDIRON_LEAGUES", "").strip()
    if not raw:
        return DEFAULT_ENABLED
    out = []
    for part in raw.split(","):
        lg = part.strip().lower()
        if lg in KNOWN and lg not in out:
            out.append(lg)
    return tuple(out) if out else DEFAULT_ENABLED


def betting_allowed(league):
    return league in BETTING


def ingestible(league):
    return league in INGEST
