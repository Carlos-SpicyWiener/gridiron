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
    conn.commit()
    return cur.lastrowid


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
