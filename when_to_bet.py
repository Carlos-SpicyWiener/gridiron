"""When should the bet actually go on: now, or nearer kickoff?

Two forces pull opposite ways and both are measurable from kalshi_snapshot.

Price: report movement says the line drifts TOWARD the model. If that holds, the
model's pick gets more expensive as kickoff approaches, and waiting costs money.

Liquidity: early markets are thin. A wide spread means paying well above mid,
which is a real cost that does not show up in the quoted edge.
"""
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db  # noqa: E402

conn = db.connect(readonly=True)

rows = conn.execute("""
    SELECT k.yes_bid, k.yes_ask, k.mid, k.open_interest, k.team_id, k.game_id,
           (julianday(g.kickoff_utc) - julianday(k.fetched_at)) * 24.0 AS hours_out
    FROM kalshi_snapshot k
    JOIN game g ON g.id = k.game_id
    WHERE k.yes_bid IS NOT NULL AND k.yes_ask IS NOT NULL
""").fetchall()

print(f"n = {len(rows)} quotes\n")

BUCKETS = [(0, 6, "under 6h"), (6, 24, "6-24h"), (24, 72, "1-3 days"),
           (72, 168, "3-7 days"), (168, 10000, "over a week")]

print("LIQUIDITY as kickoff approaches")
print(f"  {'time to kickoff':>18}{'quotes':>8}{'median spread':>15}{'median OI':>12}")
for lo, hi, label in BUCKETS:
    sub = [r for r in rows if lo <= r["hours_out"] < hi]
    if len(sub) < 10:
        continue
    spreads = [(r["yes_ask"] - r["yes_bid"]) * 100 for r in sub]
    ois = [r["open_interest"] or 0 for r in sub]
    print(f"  {label:>18}{len(sub):>8}{st.median(spreads):>14.1f}c{st.median(ois):>12,.0f}")

# Price drift: for each market, compare its earliest and latest quote.
print("\nPRICE DRIFT per market (earliest quote -> latest quote)")
by_market = {}
for r in rows:
    key = (r["game_id"], r["team_id"])
    cur = by_market.setdefault(key, [None, None])
    if cur[0] is None or r["hours_out"] > cur[0]["hours_out"]:
        cur[0] = r
    if cur[1] is None or r["hours_out"] < cur[1]["hours_out"]:
        cur[1] = r

moves, widths = [], []
for first, last in by_market.values():
    if first is None or last is None or first is last:
        continue
    span = first["hours_out"] - last["hours_out"]
    if span < 2:
        continue
    moves.append(last["mid"] - first["mid"])
    widths.append(((first["yes_ask"] - first["yes_bid"]) -
                   (last["yes_ask"] - last["yes_bid"])) * 100)

if moves:
    print(f"  markets tracked over >2h: {len(moves)}")
    print(f"  mean |mid move|          : {st.mean([abs(m) for m in moves]) * 100:.2f}c")
    print(f"  mean spread tightening   : {st.mean(widths):+.2f}c "
          f"(positive = tighter by kickoff)")

print("\nCOST OF THE SPREAD RIGHT NOW, on the live candidates")
cands = conn.execute("""
    SELECT t.name, k.yes_bid, k.yes_ask, k.mid, k.open_interest,
           (julianday(g.kickoff_utc) - julianday(k.fetched_at)) * 24.0 AS hours_out
    FROM kalshi_snapshot k
    JOIN team t ON t.id = k.team_id
    JOIN game g ON g.id = k.game_id
    WHERE k.fetched_at = (SELECT MAX(fetched_at) FROM kalshi_snapshot)
      AND t.name IN ('Houston Texans', 'Atlanta Falcons', 'Pittsburgh Panthers')
""").fetchall()
for c in cands:
    over_mid = (c["yes_ask"] - c["mid"]) * 100
    print(f"  {c['name']:22} bid {c['yes_bid']:.2f} ask {c['yes_ask']:.2f}  "
          f"paying {over_mid:.1f}c over mid   {c['hours_out']:.0f}h to kickoff")

conn.close()
