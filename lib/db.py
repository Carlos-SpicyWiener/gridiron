"""Database access. Stdlib sqlite3 only — no ORM, no driver to install."""
import os
import sqlite3
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("GRIDIRON_DB", os.path.join(ROOT, "data", "gridiron.db"))
SCHEMA = os.path.join(ROOT, "migrations", "schema.sql")


def now():
    """UTC, second precision, ISO8601 Z. Every stored timestamp uses this."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with open(SCHEMA) as fh:
        sql = fh.read()
    conn = connect()
    conn.executescript(sql)
    conn.commit()
    conn.close()
    return DB_PATH


def upsert_team(conn, league, espn_id, name, abbrev, conference_id, tier):
    """Insert a team, or refresh the mutable bits if we already know it.

    Tier only ever upgrades ('other' -> 'major'): a program's first appearance
    may be as somebody's FCS opponent before we see it in an FBS group listing.
    """
    row = conn.execute(
        "SELECT id, tier FROM team WHERE league = ? AND espn_id = ?", (league, str(espn_id))
    ).fetchone()
    if row:
        new_tier = "major" if (tier == "major" or row["tier"] == "major") else "other"
        conn.execute(
            "UPDATE team SET name = ?, abbrev = ?, conference_id = ?, tier = ? WHERE id = ?",
            (name, abbrev, conference_id, new_tier, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO team (league, espn_id, name, abbrev, conference_id, tier, first_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (league, str(espn_id), name, abbrev, conference_id, tier, now()),
    )
    return cur.lastrowid


def upsert_game(conn, g):
    """Insert or refresh a game. Scores and status are the only fields that move."""
    row = conn.execute(
        "SELECT id FROM game WHERE league = ? AND espn_id = ?", (g["league"], g["espn_id"])
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE game SET season = ?, season_type = ?, week = ?, kickoff_utc = ?, "
            "home_team_id = ?, away_team_id = ?, neutral = ?, status = ?, home_score = ?, "
            "away_score = ?, fetched_at = ? WHERE id = ?",
            (g["season"], g["season_type"], g["week"], g["kickoff_utc"], g["home_team_id"],
             g["away_team_id"], g["neutral"], g["status"], g["home_score"], g["away_score"],
             now(), row["id"]),
        )
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO game (league, espn_id, season, season_type, week, kickoff_utc, "
        "home_team_id, away_team_id, neutral, status, home_score, away_score, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (g["league"], g["espn_id"], g["season"], g["season_type"], g["week"], g["kickoff_utc"],
         g["home_team_id"], g["away_team_id"], g["neutral"], g["status"], g["home_score"],
         g["away_score"], now()),
    )
    return cur.lastrowid, True
