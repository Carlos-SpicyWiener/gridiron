#!/usr/bin/env python3
"""
gridiron_mcp.py — read-only MCP server over the football DB.

Stdlib only: MCP JSON-RPC over Streamable
HTTP on 127.0.0.1, optional bearer token, no client SQL ever executed.

Security posture:
  * opens the DB read-only (mode=ro) AND sets PRAGMA query_only — two locks.
  * holds no API credential; ingestion owns the odds key, not this.
  * every tool builds parameterized SELECTs from a fixed set of queries.
  * this server never predicts or grades — those write, and writes belong to the
    CLI. It can only report what has already been computed and recorded.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import db, picks  # noqa: E402

HOST = os.environ.get("GRIDIRON_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("GRIDIRON_MCP_PORT", "8914"))
TOKEN = os.environ.get("GRIDIRON_MCP_TOKEN")
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "gridiron", "version": "1.0.0"}


def conn():
    return db.connect(readonly=True)


def _league(args):
    v = (args or {}).get("league")
    return None if v in (None, "", "all") else v


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def tool_slate(args):
    c = conn()
    try:
        return picks.render_slate(c, league=_league(args),
                                  days=int(args.get("days", 8) or 8))
    finally:
        c.close()


def tool_record(args):
    c = conn()
    try:
        return picks.render_record(c, league=_league(args))
    finally:
        c.close()


def tool_ratings(args):
    c = conn()
    try:
        return picks.render_ratings(c, league=(args.get("league") or "nfl"),
                                    limit=min(int(args.get("limit", 25) or 25), 200))
    finally:
        c.close()


def tool_matchup(args):
    if not args.get("home") or not args.get("away"):
        return "matchup needs both `home` and `away` team names."
    c = conn()
    try:
        return picks.render_matchup(c, args["home"], args["away"], league=_league(args),
                                    neutral=bool(args.get("neutral")))
    finally:
        c.close()


def tool_team(args):
    """One team's rating, recent form, and how its picks have graded."""
    q = (args or {}).get("team", "").strip()
    if not q:
        return "team needs a `team` name."
    c = conn()
    try:
        hits = c.execute(
            "SELECT t.id, t.name, t.league, t.tier, r.rating, r.games FROM team t "
            "JOIN rating_current r ON r.team_id = t.id WHERE t.name LIKE ? "
            "ORDER BY r.games DESC LIMIT 6", (f"%{q}%",)).fetchall()
        if not hits:
            return f"No rated team matches '{q}'."
        if len(hits) > 1 and hits[0]["name"].lower() != q.lower():
            return "Ambiguous — matches: " + ", ".join(h["name"] for h in hits)
        t = hits[0]

        rank = c.execute(
            "SELECT COUNT(*) + 1 AS rk FROM rating_current WHERE league = ? AND games > 0 "
            "AND rating > ?", (t["league"], t["rating"])).fetchone()["rk"]
        out = [f"{t['name']}  ({picks.LEAGUE_LABEL.get(t['league'], t['league'].upper())})",
               f"  rating {t['rating']:.0f}  —  #{rank} in league, {t['games']} games rated"]

        recent = c.execute(
            "SELECT g.kickoff_utc, g.home_team_id, g.away_team_id, g.home_score, g.away_score, "
            "       ht.name AS home_name, at.name AS away_name "
            "FROM game g JOIN team ht ON ht.id = g.home_team_id "
            "JOIN team at ON at.id = g.away_team_id "
            "WHERE g.status = 'final' AND ? IN (g.home_team_id, g.away_team_id) "
            "ORDER BY g.kickoff_utc DESC LIMIT 8", (t["id"],)).fetchall()
        if recent:
            out.append("\n  recent results")
            for g in recent:
                at_home = g["home_team_id"] == t["id"]
                us = g["home_score"] if at_home else g["away_score"]
                them = g["away_score"] if at_home else g["home_score"]
                opp = g["away_name"] if at_home else g["home_name"]
                mark = "W" if us > them else ("L" if us < them else "T")
                out.append(f"    {g['kickoff_utc'][:10]}  {mark} {us:>3}-{them:<3} "
                           f"{'vs' if at_home else 'at'} {opp}")

        rec = c.execute(
            "SELECT COUNT(*) n, SUM(p.correct) w FROM prediction p JOIN game g ON g.id = p.game_id "
            "WHERE p.correct IS NOT NULL AND ? IN (g.home_team_id, g.away_team_id)",
            (t["id"],)).fetchone()
        if rec["n"]:
            w = rec["w"] or 0
            out.append(f"\n  picks involving this team have graded {w}-{rec['n'] - w}")
        return "\n".join(out)
    finally:
        c.close()


def tool_status(_):
    c = conn()
    try:
        out = [f"db: {db.DB_PATH}"]
        for league in ("nfl", "cfb"):
            g = c.execute(
                "SELECT COUNT(*) n, SUM(status = 'final') fin, MIN(season) lo, MAX(season) hi "
                "FROM game WHERE league = ?", (league,)).fetchone()
            if not g["n"]:
                out.append(f"\n{league.upper()}: no games ingested")
                continue
            r = c.execute("SELECT COUNT(*) n, MAX(computed_at) at FROM rating_current "
                          "WHERE league = ? AND games > 0", (league,)).fetchone()
            p = c.execute("SELECT COUNT(*) n, SUM(p.correct IS NULL) pending FROM prediction p "
                          "JOIN game g ON g.id = p.game_id WHERE g.league = ?",
                          (league,)).fetchone()
            out.append(f"\n{league.upper()}")
            out.append(f"  games   {g['n']} ({g['fin'] or 0} final), seasons {g['lo']}-{g['hi']}")
            out.append(f"  ratings {r['n']} teams, computed {r['at'] or 'never'}")
            out.append(f"  picks   {p['n']} written, {p['pending'] or 0} awaiting a result")
        o = c.execute("SELECT COUNT(*) n, MAX(fetched_at) at FROM odds_snapshot").fetchone()
        out.append(f"\nodds: {o['n']} snapshots" + (f", latest {o['at']}" if o["at"] else ""))
        out.append("\nRatings and picks are recomputed by the CLI; this server only reads. "
                   "If a figure looks stale, the sync is behind, not the query.")
        return "\n".join(out)
    finally:
        c.close()


LEAGUE_PROP = {"type": "string", "enum": ["nfl", "cfb", "all"],
               "description": "nfl, cfb, or all (default all)"}

TOOLS = [
    {"name": "slate",
     "description": "Upcoming games with the model's straight-up pick, win probability, "
                    "confidence tier, and whether the betting market agrees. The main board.",
     "inputSchema": {"type": "object", "properties": {
         "league": LEAGUE_PROP,
         "days": {"type": "integer", "description": "horizon in days (default 8)", "default": 8}}},
     "_fn": tool_slate},
    {"name": "record",
     "description": "How the picks have actually done: win-loss overall, by league, by "
                    "confidence tier (claimed vs actual hit rate), and head-to-head against "
                    "the betting market on games where a line was observed.",
     "inputSchema": {"type": "object", "properties": {"league": LEAGUE_PROP}},
     "_fn": tool_record},
    {"name": "ratings",
     "description": "Power ratings (Elo, 1500 = average) ordered best to worst. Shows who the "
                    "model thinks is good, independent of any particular matchup.",
     "inputSchema": {"type": "object", "properties": {
         "league": {"type": "string", "enum": ["nfl", "cfb"], "default": "nfl"},
         "limit": {"type": "integer", "description": "how many teams (default 25)",
                   "default": 25}}},
     "_fn": tool_ratings},
    {"name": "matchup",
     "description": "Win probability for any two teams, whether or not they actually play. "
                    "Use for hypotheticals: 'who wins if X played Y on a neutral field'.",
     "inputSchema": {"type": "object", "properties": {
         "away": {"type": "string", "description": "away team name (partial ok)"},
         "home": {"type": "string", "description": "home team name (partial ok)"},
         "league": LEAGUE_PROP,
         "neutral": {"type": "boolean", "description": "neutral site: no home-field advantage",
                     "default": False}},
         "required": ["away", "home"]},
     "_fn": tool_matchup},
    {"name": "team",
     "description": "One team's rating and league rank, its last several results, and how "
                    "picks involving it have graded.",
     "inputSchema": {"type": "object", "properties": {
         "team": {"type": "string", "description": "team name (partial ok)"}},
         "required": ["team"]},
     "_fn": tool_team},
    {"name": "status",
     "description": "What the database currently holds: games and seasons ingested, when "
                    "ratings were last computed, picks awaiting results, odds coverage. "
                    "Check this before trusting a stale-looking answer.",
     "inputSchema": {"type": "object", "properties": {}},
     "_fn": tool_status},
]
TOOL_MAP = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# JSON-RPC / MCP
# --------------------------------------------------------------------------
def handle_rpc(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        pub = [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": pub}}
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        tool = TOOL_MAP.get(name)
        if not tool:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            text = tool["_fn"](params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as e:  # surface as a tool error, never crash the server
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"error: {e}"}],
                               "isError": True}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authed(self):
        if not TOKEN:
            return True
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _send(self, code, body_obj):
        body = json.dumps(body_obj).encode() if body_obj is not None else b""
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"ok": True, "server": SERVER_INFO})

    def do_POST(self):
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            msg = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}})
            return
        if isinstance(msg, list):
            out = [r for r in (handle_rpc(m) for m in msg) if r is not None]
            self._send(200 if out else 202, out if out else None)
            return
        resp = handle_rpc(msg)
        self._send(202, None) if resp is None else self._send(200, resp)

    def log_message(self, *a):
        pass


def main():
    if not os.path.exists(db.DB_PATH):
        print(f"DB not found: {db.DB_PATH} — run `gridiron init` first", file=sys.stderr)
        sys.exit(1)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"gridiron-mcp listening on http://{HOST}:{PORT} "
          f"(auth={'on' if TOKEN else 'off'})", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
