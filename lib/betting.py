"""Recording bets, and deciding which of them §7 is allowed to grade itself on.

The distinction that matters here is provenance. A bet the sizer produced and a
bet placed by hand — before the tool existed, or against its advice — belong in
the same ledger, because the bankroll does not care which was which. They do not
belong in the same evidence pool: "is the sizer finding real edge?" cannot be
answered with bets the sizer would have declined.

The trap is that dropping those bets AFTER seeing how they landed is
cherry-picking, and it always flatters. So provenance is written when the bet is
placed, from whether the sizer actually produced it, and never revised. The
split is pre-committed; nothing about the outcome can move a bet between pools.

`ledger` sees everything. `clv_pool` sees only sized bets. §7's unlock reads the
second, and the ledger's P&L reads the first.
"""
import math

from . import calibration, db, fees, sizing

SIZED = "sized"      # the sizer produced this bet at this size
MANUAL = "manual"    # placed by hand: pre-tool, or against the tool's advice
PROVENANCE = (SIZED, MANUAL)

# §7 criterion 3: enough placed bets, and a mean beating zero by more than noise.
MIN_CLV_BETS = 20


def record_bet(conn, league, game_id, side_team_id, market_ticker, contracts, price,
               model_prob_raw, model_prob, notes, market_prob=None, provenance=SIZED,
               close_price=None, close_snapshot_at=None, venue="robinhood", gold=False):
    """Insert one bet. Immutable afterwards except for the grading columns."""
    if provenance not in PROVENANCE:
        raise ValueError(f"provenance must be one of {PROVENANCE}, got {provenance!r}")

    fee = float(fees.fee(price, gold))
    sized = sizing.size(model_prob, price, 0.0)  # bankroll-independent parts only
    cur = conn.execute(
        "INSERT INTO bet (placed_at, league, game_id, side_team_id, venue, market_ticker, "
        "contracts, price, fee_per_contract, fee_model, cost, model_prob_raw, model_prob, "
        "calib_model, market_prob, edge, kelly_full, close_price, close_snapshot_at, "
        "status, notes, provenance) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        (db.now(), league, game_id, side_team_id, venue, market_ticker, contracts, price,
         fee, fees.MODEL, contracts * (price + fee), model_prob_raw, model_prob,
         calibration.MODEL, market_prob, model_prob - price - fee,
         sized.kelly_full or None, close_price, close_snapshot_at, notes, provenance))
    bet_id = cur.lastrowid
    # The bankroll is a cash account: the stake leaves it now, the payout (if
    # any) returns at settlement. Netting them would lose the curve in between.
    conn.execute(
        "INSERT INTO bankroll_event (ts, delta, reason, bet_id, note) "
        "VALUES (?, ?, 'stake', ?, ?)",
        (db.now(), -contracts * (price + fee), bet_id,
         f"{contracts} @ {price:.2f} {market_ticker}"))
    conn.commit()
    return bet_id


def deposit(conn, amount, note=""):
    """Money in or out of the account. Negative is a withdrawal."""
    conn.execute(
        "INSERT INTO bankroll_event (ts, delta, reason, note) VALUES (?, ?, ?, ?)",
        (db.now(), amount, "deposit" if amount >= 0 else "withdrawal", note))
    conn.commit()


def balance(conn):
    return conn.execute(
        "SELECT COALESCE(SUM(delta), 0.0) AS b FROM bankroll_event").fetchone()["b"]


def bankroll_curve(conn):
    """Running balance after every cash event, which is what a curve needs."""
    running, out = 0.0, []
    for row in conn.execute(
            "SELECT ts, delta, reason, bet_id, note FROM bankroll_event "
            "ORDER BY id").fetchall():
        running += row["delta"]
        out.append({"ts": row["ts"], "delta": row["delta"], "reason": row["reason"],
                    "bet_id": row["bet_id"], "balance": running})
    return out


def _settlement(bet, game):
    """1.0 win, 0.0 loss, 0.5 tie. Kalshi resolves a tie to 50c a side.

    prediction.correct scores a tie as a loss, which is defensible for a pick
    record and simply wrong for money.
    """
    home, away = game["home_score"], game["away_score"]
    if home is None or away is None:
        return None
    if home == away:
        return 0.5
    winner = game["home_team_id"] if home > away else game["away_team_id"]
    return 1.0 if winner == bet["side_team_id"] else 0.0


STATUS = {1.0: "won", 0.0: "lost", 0.5: "push"}


def grade(conn):
    """Settle every open bet whose game has finished. Idempotent."""
    rows = conn.execute(
        "SELECT b.*, g.home_score, g.away_score, g.home_team_id, g.away_team_id, "
        "       g.status AS game_status, g.kickoff_utc "
        "FROM bet b JOIN game g ON g.id = b.game_id "
        "WHERE b.status = 'open' AND g.status = 'final'").fetchall()
    settled = 0
    for bet in rows:
        value = _settlement(bet, bet)
        if value is None:
            continue
        payout = bet["contracts"] * value
        close, close_at = _close_from_snapshots(conn, bet)
        conn.execute(
            "UPDATE bet SET status = ?, settlement_value = ?, payout = ?, pnl = ?, "
            "settled_at = ?, close_price = COALESCE(close_price, ?), "
            "close_snapshot_at = COALESCE(close_snapshot_at, ?) WHERE id = ?",
            (STATUS[value], value, payout, payout - bet["cost"], db.now(),
             close, close_at, bet["id"]))
        if payout:
            conn.execute(
                "INSERT INTO bankroll_event (ts, delta, reason, bet_id, note) "
                "VALUES (?, ?, 'settlement', ?, ?)",
                (db.now(), payout, bet["id"], STATUS[value]))
        else:
            conn.execute(
                "INSERT INTO bankroll_event (ts, delta, reason, bet_id, note) "
                "VALUES (?, 0.0, 'settlement', ?, 'lost')", (db.now(), bet["id"]))
        settled += 1
    conn.commit()
    return settled


def _close_from_snapshots(conn, bet):
    """The last mid strictly BEFORE kickoff.

    Never the API at grade time: a settled market quotes 0.99/0.01 over a
    0.00/1.00 book, which would turn CLV into a restatement of the win/loss
    record and a midpoint into a plausible fake 0.50.
    """
    row = conn.execute(
        "SELECT k.mid, k.fetched_at FROM kalshi_snapshot k "
        "JOIN game g ON g.id = k.game_id "
        "WHERE k.game_id = ? AND k.team_id = ? AND k.fetched_at < g.kickoff_utc "
        "ORDER BY k.fetched_at DESC LIMIT 1", (bet["game_id"], bet["side_team_id"])
    ).fetchone()
    return (row["mid"], row["fetched_at"]) if row else (None, None)


def performance(conn):
    """The portfolio: record, money, and how the sized pool did on its own."""
    rows = ledger(conn)
    settled = [r for r in rows if r["status"] in ("won", "lost", "push")]
    staked = sum(r["cost"] for r in settled)
    pnl = sum(r["pnl"] or 0.0 for r in settled)
    sized = [r for r in settled if r["provenance"] == SIZED]
    clv = clv_values(conn)
    return {
        "open": sum(1 for r in rows if r["status"] == "open"),
        "settled": len(settled),
        "won": sum(1 for r in settled if r["status"] == "won"),
        "lost": sum(1 for r in settled if r["status"] == "lost"),
        "push": sum(1 for r in settled if r["status"] == "push"),
        "staked": staked,
        "returned": sum(r["payout"] or 0.0 for r in settled),
        "pnl": pnl,
        "roi": (pnl / staked) if staked else 0.0,
        "sized_settled": len(sized),
        "sized_pnl": sum(r["pnl"] or 0.0 for r in sized),
        "balance": balance(conn),
        "clv_n": len(clv),
        "clv_mean": (sum(clv) / len(clv)) if clv else 0.0,
        "clv_unlocked": clv_criterion_met(conn),
    }


def ledger(conn, status=None):
    """Every bet, whatever its provenance. The bankroll does not discriminate."""
    sql = "SELECT * FROM bet"
    args = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    return conn.execute(sql + " ORDER BY placed_at, id", args).fetchall()


def clv_pool(conn):
    """Sized bets with a captured pre-kickoff close — the only evidence §7 may use."""
    return conn.execute(
        "SELECT * FROM bet WHERE provenance = ? AND close_price IS NOT NULL "
        "ORDER BY placed_at, id", (SIZED,)).fetchall()


def clv_values(conn):
    """close - price per sized bet. Positive means the market moved your way."""
    return [row["close_price"] - row["price"] for row in clv_pool(conn)]


def mean_clv(conn):
    values = clv_values(conn)
    return sum(values) / len(values) if values else 0.0


def clv_criterion_met(conn, min_bets=MIN_CLV_BETS):
    """§7.3. Mean CLV above zero by at least one standard error, over enough bets.

    A bare "mean > 0" over the handful of bets quarter-Kelly produces is noise,
    not evidence, which is the whole reason for the standard-error test.
    """
    values = clv_values(conn)
    n = len(values)
    if n < min_bets:
        return False
    mean = sum(values) / n
    if mean <= 0:
        return False
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    if variance == 0:
        return True
    return mean > math.sqrt(variance / n)
