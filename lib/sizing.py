"""How much to bet, given a probability worth betting on.

Kelly for a binary contract bought at all_in = price + fee, paying 1 on a win:

    kelly_full = (p - all_in) / (1 - all_in)

The fee belongs in the denominator as well as the numerator — it is part of what
you pay, so it is part of what is at risk.

Three multipliers stand between that and a stake, and they exist because a Kelly
fraction computed from an unproven model is a number with a false air of
authority. Quarter-Kelly because the probabilities are not yet trustworthy; a 5%
per-bet cap because one confident number should not be able to move the bankroll
much; a 20% cap on total open exposure because a slate's bets correlate more than
they look like they do.

`p` here is ALREADY CALIBRATED — see lib/calibration. Passing a raw win_prob in
would reintroduce exactly the bias the sizer is most sensitive to.
"""
import math

from . import fees

KELLY_MULTIPLIER = 0.25
MAX_STAKE_PCT = 0.05
MAX_OPEN_EXPOSURE_PCT = 0.20


class Size:
    """What the sizer decided, and enough of its working to log."""

    __slots__ = ("p", "price", "fee", "all_in", "edge", "kelly_full",
                 "stake", "contracts", "capped")

    def __init__(self, p, price, fee, all_in, edge, kelly_full, stake, contracts, capped):
        self.p = p
        self.price = price
        self.fee = fee
        self.all_in = all_in
        self.edge = edge
        self.kelly_full = kelly_full
        self.stake = stake
        self.contracts = contracts
        self.capped = capped

    def __repr__(self):
        return (f"Size(p={self.p:.3f}, price={self.price:.2f}, edge={self.edge:+.4f}, "
                f"kelly={self.kelly_full:.4f}, stake={self.stake:.2f}, "
                f"contracts={self.contracts}{', capped' if self.capped else ''})")


def size(p, price, bankroll, multiplier=KELLY_MULTIPLIER, max_stake_pct=MAX_STAKE_PCT,
         gold=False):
    """Stake and contract count for one candidate. Never returns a negative stake."""
    fee = float(fees.fee(price, gold))
    all_in = price + fee
    edge = p - all_in

    if edge <= 0 or all_in >= 1.0:
        return Size(p, price, fee, all_in, edge, min(edge, 0.0), 0.0, 0, False)

    kelly_full = edge / (1.0 - all_in)
    wanted = bankroll * kelly_full * multiplier
    ceiling = bankroll * max_stake_pct
    stake = min(wanted, ceiling)
    return Size(p, price, fee, all_in, edge, kelly_full, stake,
                int(math.floor(stake / all_in)), wanted > ceiling)


def allocate(candidates, bankroll, open_exposure=0.0,
             max_open_exposure_pct=MAX_OPEN_EXPOSURE_PCT, **kw):
    """Size a slate against the open-exposure cap, highest edge first.

    Highest-edge-first is the only ordering that doesn't punish you for scanning
    early in the week. A candidate that doesn't fit the remaining headroom is
    skipped rather than part-filled, and we keep going — a later, smaller one may
    still fit, and leaving headroom unused helps nobody.
    """
    headroom = bankroll * max_open_exposure_pct - open_exposure
    out = []
    for c in sorted(candidates, key=lambda c: c["edge"], reverse=True):
        s = size(c["p"], c["price"], bankroll, **kw)
        cost = s.contracts * s.all_in
        if s.contracts and cost <= headroom + 1e-9:
            headroom -= cost
            result = "candidate"
        else:
            result = "exposure_capped" if s.contracts else "no_edge"
            s = Size(s.p, s.price, s.fee, s.all_in, s.edge, s.kelly_full, 0.0, 0, s.capped)
        row = dict(c)
        row.update(size=s, contracts=s.contracts, stake=s.stake, all_in=s.all_in,
                   kelly_full=s.kelly_full, gate_result=result)
        out.append(row)
    return out
