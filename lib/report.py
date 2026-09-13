"""Rendering the scan: what to bet, how much, and why everything else was skipped."""
import datetime as dt

from . import betting, engine, sizing

SKIP_REASON = {
    "thin_edge": "edge under 5c after fees",
    "wide_spread": "bid/ask wider than 4c",
    "thin_book": "open interest under 1000",
    "no_market_reference": "no book line to sanity-check against",
    "fbs_vs_fcs": "FCS or non-D1 opponent",
    "stale_rating": "rating older than 14 days",
    "suspect_rating": "model disagrees with the book by more than 20 pts",
    "suspect_direction": "model and book disagree on the WINNER",
    "no_quote": "no live book",
    "stake_below_one_contract": "stake too small for one contract",
    "exposure_capped": "would breach the 20% open-exposure cap",
    "price_disagrees_with_book": "Kalshi price is >15pts off the book - stale or mismapped",
}


def _kick(iso):
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%a %d %b %H:%MZ")
    except (AttributeError, ValueError):
        return "?"


def scan_report(conn, bankroll, leagues=("nfl", "cfb")):
    candidates, skipped, unresolved = [], [], []
    for league in leagues:
        rows, un = engine.scan(conn, league, bankroll)
        unresolved += un
        for row in rows:
            (candidates if row.gate == "candidate" else skipped).append(row)

    allocated = sizing.allocate(
        [{"c": c, "p": c.p_cal, "price": c.ask, "edge": c.edge} for c in candidates],
        bankroll)

    out = []
    out.append(f"BANKROLL ${bankroll:,.2f}   quarter-Kelly, 5% per bet, 20% total exposure")
    out.append("")

    live = [r for r in allocated if r["gate_result"] == "candidate" and r["contracts"]]
    if not live:
        out.append("  No bet. Nothing on the board clears the gates.")
    else:
        out.append(f"  {'BET ON':<24}{'OPPONENT':<24}{'KICKOFF':<18}"
                   f"{'ASK':>5}{'MODEL':>7}{'BOOK':>6}{'EDGE':>7}{'CONTRACTS':>10}{'COST':>8}")
        out.append("  " + "-" * 107)
        total = 0.0
        for row in live:
            c, n = row["c"], row["contracts"]
            cost = n * row["all_in"]
            total += cost
            book = f"{c.market_prob:.0%}" if c.market_prob is not None else "  --"
            out.append(f"  {c.pick_name[:23]:<24}{c.opponent[:23]:<24}{_kick(c.kickoff):<18}"
                       f"{c.ask:>5.2f}{c.p_cal:>7.0%}{book:>6}{c.edge:>+7.3f}"
                       f"{n:>10}{cost:>8.2f}")
        out.append("  " + "-" * 107)
        ev = sum(r["c"].edge * r["contracts"] for r in live)
        out.append(f"  committed ${total:,.2f} = {total / bankroll:.1%} of bankroll"
                   f"    modelled EV ${ev:,.2f} (only as good as the model)")

    capped = [r for r in allocated if r["gate_result"] == "exposure_capped"]
    if capped:
        out.append("")
        out.append(f"  {len(capped)} more cleared the gates but hit the exposure cap: "
                   + ", ".join(r["c"].pick_name for r in capped[:5]))

    out.append("")
    out.append("  SKIPPED")
    counts = {}
    for row in skipped:
        counts[row.gate] = counts.get(row.gate, 0) + 1
    for gate, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append(f"    {n:4d}  {SKIP_REASON.get(gate, gate)}")
    if unresolved:
        out.append(f"    {len(unresolved):4d}  Kalshi events not matched to a game "
                   f"(mostly future weeks and FCS matchups)")
    return "\n".join(out)


def ledger_report(conn, open_only=False):
    """The portfolio: every bet, the record, and whether §7 has unlocked."""
    perf = betting.performance(conn)
    rows = betting.ledger(conn, status="open" if open_only else None)
    out = []

    if not rows:
        out.append("  No bets recorded yet. `gridiron scan` proposes them; "
                   "`gridiron bet place` records what you actually did.")
        return "\n".join(out)

    out.append(f"  {'#':>3} {'PLACED':<11}{'BET':<22}{'CTR':>4}{'PRICE':>7}{'COST':>8}"
               f"{'STATUS':>8}{'PNL':>9}{'CLV':>7}  SRC")
    out.append("  " + "-" * 92)
    for row in rows:
        team = conn.execute("SELECT name FROM team WHERE id = ?",
                            (row["side_team_id"],)).fetchone()
        clv = ("" if row["close_price"] is None
               else f"{row['close_price'] - row['price']:+.3f}")
        pnl = "" if row["pnl"] is None else f"{row['pnl']:+.2f}"
        out.append(f"  {row['id']:>3} {row['placed_at'][:10]:<11}"
                   f"{(team['name'] if team else '?')[:21]:<22}{row['contracts']:>4}"
                   f"{row['price']:>7.2f}{row['cost']:>8.2f}{row['status']:>8}"
                   f"{pnl:>9}{clv:>7}  {row['provenance']}")
    out.append("  " + "-" * 92)

    out.append("")
    out.append("  PORTFOLIO")
    record = f"{perf['won']}-{perf['lost']}" + (f"-{perf['push']}" if perf["push"] else "")
    out.append(f"    record        {record}   ({perf['settled']} settled, "
               f"{perf['open']} open)")
    out.append(f"    staked        ${perf['staked']:,.2f}")
    out.append(f"    returned      ${perf['returned']:,.2f}")
    out.append(f"    profit/loss   ${perf['pnl']:+,.2f}   ROI {perf['roi']:+.1%}")
    out.append(f"    bankroll      ${perf['balance']:,.2f}")

    out.append("")
    out.append("  IS THE MODEL ACTUALLY FINDING EDGE?")
    if perf["clv_n"] == 0:
        out.append("    No closing prices captured yet, so CLV is unknown. This is the "
                   "fastest")
        out.append("    signal there is -- it grades in days rather than a season -- and it "
                   "needs")
        out.append("    the price poll running before kickoff, not at grade time.")
    else:
        out.append(f"    closing-line value   {perf['clv_mean']:+.4f} "
                   f"over {perf['clv_n']} sized bet(s)")
    need = max(0, betting.MIN_CLV_BETS - perf["clv_n"])
    out.append(f"    §7 sizing unlock     {'MET' if perf['clv_unlocked'] else 'not met'}"
               + (f" — {need} more sized bet(s) with a captured close" if need else ""))
    if perf["settled"] != perf["sized_settled"]:
        out.append(f"    note: {perf['settled'] - perf['sized_settled']} settled bet(s) are "
                   f"'manual' — in the P&L above, out of the edge evidence.")
    return "\n".join(out)


def evidence_report(conn):
    """Does the model anticipate the market, and by enough to pay for itself?"""
    from . import calibration, evidence

    obs = evidence.observations(conn, calibration.calibrate)
    out = []
    if len(obs) < evidence.MIN_SAMPLE:
        out.append("  Not enough games with both an opening and a later line yet.")
        return "\n".join(out)

    out.append(f"  DOES THE LINE MOVE TOWARD THE MODEL?   n = {len(obs)} games")
    out.append("  Measured on games with an opening line, a later line and a pick.")
    out.append("  The model never reads the market, so this is not circular.")
    out.append("")
    out.append(f"    {'disagreement':>16}{'n':>6}{'line moved':>13}{'se':>9}"
               f"{'t':>7}   verdict")
    out.append("    " + "-" * 66)
    for label, floor in (("any", 0.0), ("> 3 pts", 0.03),
                         ("> 5 pts", 0.05), ("> 8 pts", 0.08)):
        s = evidence.summarise(obs, floor)
        out.append(f"    {label:>16}{s['n']:>6}{s['mean']:>+13.4f}{s['se']:>9.4f}"
                   f"{s['t']:>+7.2f}   {s['verdict']}")

    for league in ("nfl", "cfb"):
        sub = [o for o in obs if o.league == league]
        if len(sub) < evidence.MIN_SAMPLE:
            continue
        s = evidence.summarise(sub)
        out.append(f"    {league.upper():>16}{s['n']:>6}{s['mean']:>+13.4f}"
                   f"{s['se']:>9.4f}{s['t']:>+7.2f}   {s['verdict']}")

    out.append("")
    out.append("  READING IT")
    overall = evidence.summarise(obs)
    tail = evidence.summarise(obs, 0.08)
    if overall["verdict"] == "real":
        out.append(f"    The effect is real: the line moves {overall['mean'] * 100:.1f} "
                   f"points toward the model,")
        out.append(f"    at {overall['t']:.1f} sigma. The model is picking up something the "
                   f"market later agrees with.")
    else:
        out.append("    No significant anticipation of the market yet.")

    out.append("")
    if evidence.decays_with_conviction(obs):
        out.append("    BUT IT DECAYS WITH CONVICTION. The effect is weaker on the games "
                   "the model")
        out.append("    feels strongest about — and those are the only games this tool "
                   "bets. A signal")
        out.append("    that lives in the small disagreements is a broad nudge, not "
                   "insight, and it")
        out.append("    is not one the gates can harvest.")
    else:
        out.append("    It holds up on the model's strong opinions, which are the ones "
                   "that get bet.")

    out.append("")
    out.append(f"    Magnitude check: {overall['mean'] * 100:.1f} points of line movement "
               f"against a")
    out.append("    2-3c fee and a 5c edge gate. More data sharpens this estimate; it "
               "does not")
    out.append("    enlarge it. Precision and profitability are different axes.")
    if tail["n"] >= evidence.MIN_SAMPLE:
        out.append(f"    On the games actually bet (>8 pts): {tail['mean'] * 100:+.1f} "
                   f"points, {tail['verdict']}.")
    return "\n".join(out)
