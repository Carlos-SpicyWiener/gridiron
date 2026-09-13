"""Per-order fee on a Robinhood-routed Kalshi fill. Spec §3.4.

Calibrated against a real receipt, which is the only reason the numbers here can
be trusted. See RealFills below.
"""
import os
import sys
import unittest
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import fees


class RealFills(unittest.TestCase):
    """Observed fills. Every other number in this module is theory; these are
    receipts, and they are what the theory has to reproduce."""

    def test_seven_pittsburgh_contracts_at_78c_cost_9c_in_fees(self):
        # 2026-09-13, Robinhood: basis $5.46, commissions and fees $0.09,
        # total $5.55. This single fill is what proved the old per-contract
        # rounding wrong -- it predicted $0.21.
        self.assertEqual(fees.order_fee(D("0.78"), 7), D("0.09"))

    def test_that_fill_totals_five_fifty_five(self):
        self.assertEqual(fees.order_cost(D("0.78"), 7), D("5.55"))


class OrderFee(unittest.TestCase):
    """ceil(0.07 * contracts * P * (1-P)), rounded once on the ORDER."""

    def test_rounds_once_for_the_whole_order_not_once_per_contract(self):
        # 0.07 * 10 * 0.5 * 0.5 = 0.175 -> 18c for the order.
        # Per-contract rounding would give 2c x 10 = 20c.
        self.assertEqual(fees.order_fee(D("0.50"), 10), D("0.18"))

    def test_a_single_contract_still_rounds_up_to_a_cent(self):
        self.assertEqual(fees.order_fee(D("0.50"), 1), D("0.02"))

    def test_costs_less_per_contract_at_the_extremes(self):
        cheap = fees.order_fee(D("0.90"), 100) / 100
        dear = fees.order_fee(D("0.50"), 100) / 100
        self.assertLess(cheap, dear)

    def test_no_contracts_no_fee(self):
        self.assertEqual(fees.order_fee(D("0.50"), 0), D("0.00"))

    def test_rejects_a_price_outside_zero_to_one(self):
        for bad in ("-0.01", "1.01"):
            with self.subTest(price=bad):
                with self.assertRaises(ValueError):
                    fees.order_fee(bad, 10)


class MarginalRate(unittest.TestCase):
    """What the gate and the sizer should charge: the unrounded rate.

    Rounding is a sub-cent artifact spread across the order, so pricing a
    decision off the rounded-up figure of a hypothetical one-contract order
    overstates cost by more than the fee itself.
    """

    def test_is_the_unrounded_kalshi_rate(self):
        self.assertAlmostEqual(float(fees.rate(D("0.50"))), 0.0175, places=6)
        self.assertAlmostEqual(float(fees.rate(D("0.78"))), 0.012012, places=6)

    def test_is_far_below_the_old_flat_three_cents(self):
        """The bug that mattered: 3c assumed, 1.3c real, every edge understated."""
        self.assertLess(float(fees.rate(D("0.78"))), 0.02)

    def test_peaks_at_a_coin_flip(self):
        self.assertGreater(fees.rate(D("0.50")), fees.rate(D("0.30")))
        self.assertGreater(fees.rate(D("0.50")), fees.rate(D("0.70")))

    def test_approaches_the_realised_per_contract_fee_on_a_large_order(self):
        realised = fees.order_fee(D("0.64"), 1000) / 1000
        self.assertAlmostEqual(float(realised), float(fees.rate(D("0.64"))), places=4)


class RobinhoodCommission(unittest.TestCase):
    """Observed as zero. Kept as a knob because it is a business decision that
    can change, and a fee model that cannot be corrected is a liability."""

    def test_defaults_to_zero_because_that_is_what_the_receipt_showed(self):
        self.assertEqual(fees.rh_commission(D("0.78"), 7), D("0.00"))

    def test_can_be_switched_back_on_if_they_start_charging(self):
        self.assertEqual(fees.rh_commission(D("0.78"), 7, per_contract=D("0.01")),
                         D("0.07"))

    def test_a_commission_shows_up_in_the_order_fee(self):
        self.assertEqual(fees.order_fee(D("0.78"), 7, rh_per_contract=D("0.01")),
                         D("0.16"))


class Types(unittest.TestCase):

    def test_never_returns_a_float(self):
        self.assertIsInstance(fees.order_fee(D("0.64"), 10), D)
        self.assertIsInstance(fees.rate(D("0.64")), D)

    def test_accepts_a_string_price_because_the_api_returns_strings(self):
        self.assertEqual(fees.order_fee("0.78", 7), D("0.09"))


if __name__ == "__main__":
    unittest.main()
