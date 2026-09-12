"""Rendering the scan: what to bet, how much, and why everything else was skipped."""
import datetime as dt

from . import engine, sizing

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
