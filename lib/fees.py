"""What a contract actually costs, calibrated against a real receipt.

Kalshi's fee is `ceil(0.07 * contracts * P * (1-P))`, and the rounding happens
ONCE ON THE ORDER. That detail is the whole of this module. Rounding each
contract up to a cent instead -- which is what the first version of this did --
turned a real $0.09 fee on seven contracts at 78c into a modelled $0.21, and
every edge computed from it was understated by 1.7c per contract. At a 5c gate
that silently skipped bets that should have cleared.

Robinhood's commission was observed as ZERO on that fill: the $0.09 charged is
exactly Kalshi's order fee with nothing left over. Published sources claimed
$0.01/contract. The receipt wins. It stays a parameter because it is a business
decision that can change without notice, and a bet's realised fee is stored on
its row so a recalibration can be applied to history.

Two different numbers, and conflating them is the trap:

  order_fee  what you are actually charged, rounded, for a specific order size.
             Use it for cost, P&L and the bankroll.

  rate       the unrounded marginal rate, 0.07 * P * (1-P). Use it for the edge
             gate and for Kelly. Sizing decisions must not be priced off the
             rounded-up fee of a hypothetical one-contract order -- at 78c that
             overstates the true cost by more than the cost itself.
"""
from decimal import Decimal, ROUND_CEILING

CENT = Decimal("0.01")

# Kalshi's published taker rate, applied to the notional of the whole order.
KALSHI_RATE = Decimal("0.07")

# Observed zero on the 2026-09-13 fill. Left configurable rather than removed.
RH_PER_CONTRACT = Decimal("0.00")

MODEL = "rh_kalshi_2026_order"


def _price(value):
    """Coerce to Decimal and reject anything that isn't a probability."""
    p = value if isinstance(value, Decimal) else Decimal(str(value))
    if p < 0 or p > 1:
        raise ValueError(f"price must be within [0, 1], got {p}")
    return p


def rate(price):
    """Unrounded fee per contract. What the gate and the sizer should charge."""
    p = _price(price)
    return KALSHI_RATE * p * (1 - p)


def kalshi_fee(price, contracts):
    """Kalshi's fee for the whole order, rounded up to the cent once."""
    if contracts <= 0:
        return Decimal("0.00")
    return (rate(price) * contracts).quantize(CENT, ROUND_CEILING)


def rh_commission(price, contracts, per_contract=None):
    """Robinhood's commission for the order. Zero as observed."""
    if contracts <= 0:
        return Decimal("0.00")
    _price(price)
    per = RH_PER_CONTRACT if per_contract is None else per_contract
    return (per * contracts).quantize(CENT, ROUND_CEILING)


def order_fee(price, contracts, rh_per_contract=None):
    """Total fees charged on one order."""
    return kalshi_fee(price, contracts) + rh_commission(price, contracts, rh_per_contract)


def order_cost(price, contracts, rh_per_contract=None):
    """Everything that leaves the account: contracts plus fees."""
    if contracts <= 0:
        return Decimal("0.00")
    notional = (_price(price) * contracts).quantize(CENT)
    return notional + order_fee(price, contracts, rh_per_contract)


def realised_per_contract(price, contracts, rh_per_contract=None):
    """The fee actually borne by each contract, for storing on the bet row."""
    if contracts <= 0:
        return Decimal("0.00")
    return order_fee(price, contracts, rh_per_contract) / contracts
