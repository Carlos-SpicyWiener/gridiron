"""What a contract actually costs, which is not what the price says.

Two fees stack on a Robinhood-routed Kalshi fill: Kalshi's exchange fee, which is
a parabola peaking at 50c, and Robinhood's own commission on top. Both round UP
to the cent per contract, and that rounding is what matters — it flattens the
parabola into a plateau of 3c across the whole middle of the price range.

A 5c gross edge is therefore 40-60% fee, worst exactly where the model has least
to say. Treating the fee as a flat cent understates cost by 2-3x and turns a
losing bet into a winning-looking one.

Decimal throughout, never float. The API quotes prices as strings and the gate
compares against a 5c threshold; binary floating point at cent granularity is
precisely where that comparison goes wrong.
"""
from decimal import Decimal, ROUND_CEILING

CENT = Decimal("0.01")

# Kalshi's published taker rate. Fee is rate * P * (1-P) per contract, per side.
KALSHI_RATE = Decimal("0.07")

# Robinhood's commission is probability-weighted and capped at a cent per
# contract; Gold halves the rate but not the cap. Calibrate against a statement
# rather than an order preview — the preview shows cost basis, not the debit.
RH_RATE = Decimal("0.10")
RH_RATE_GOLD = Decimal("0.05")
RH_CAP = CENT

MODEL = "rh_kalshi_2026"


def _price(value):
    """Coerce to Decimal and reject anything that isn't a probability."""
    p = value if isinstance(value, Decimal) else Decimal(str(value))
    if p < 0 or p > 1:
        raise ValueError(f"price must be within [0, 1], got {p}")
    return p


def kalshi_fee(price):
    """Kalshi's exchange fee for one contract, rounded up to the cent."""
    p = _price(price)
    return (KALSHI_RATE * p * (1 - p)).quantize(CENT, ROUND_CEILING)


def rh_commission(price, gold=False):
    """Robinhood's commission for one contract, rounded up to the cent."""
    p = _price(price)
    rate = RH_RATE_GOLD if gold else RH_RATE
    return min(rate * p * (1 - p), RH_CAP).quantize(CENT, ROUND_CEILING)


def fee(price, gold=False):
    """Total cost per contract on top of the price itself."""
    return kalshi_fee(price) + rh_commission(price, gold)
