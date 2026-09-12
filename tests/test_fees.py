"""Per-contract fee on a Robinhood-routed Kalshi fill. Spec §3.4."""
import os
import sys
import unittest
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import fees


class KalshiExchangeFee(unittest.TestCase):
    """ceil(0.07 * P * (1-P) * 100) / 100 per contract, per side."""

    def test_peaks_at_a_coin_flip(self):
        # 0.07 * 0.25 = 0.0175 -> ceils to 2 cents
        self.assertEqual(fees.kalshi_fee(D("0.50")), D("0.02"))

    def test_falls_to_one_cent_at_the_extremes(self):
        # 0.07 * 0.10 * 0.90 = 0.0063 -> ceils to 1 cent
        self.assertEqual(fees.kalshi_fee(D("0.10")), D("0.01"))
        self.assertEqual(fees.kalshi_fee(D("0.90")), D("0.01"))

    def test_rounds_up_never_down(self):
        # 0.07 * 0.64 * 0.36 = 0.016128 -> 2 cents, not 1
        self.assertEqual(fees.kalshi_fee(D("0.64")), D("0.02"))


class RobinhoodCommission(unittest.TestCase):
    """Probability-weighted, capped at a cent; half rate on Gold."""

    def test_capped_at_one_cent(self):
        # 0.10 * 0.25 = 0.025, above the cap
        self.assertEqual(fees.rh_commission(D("0.50")), D("0.01"))

    def test_still_a_cent_once_rounded_up_at_the_extremes(self):
        # 0.10 * 0.01 * 0.99 = 0.00099 -> ceils to 1 cent
        self.assertEqual(fees.rh_commission(D("0.01")), D("0.01"))

    def test_gold_uses_half_the_rate_but_the_same_cap(self):
        self.assertEqual(fees.rh_commission(D("0.50"), gold=True), D("0.01"))


class TotalFee(unittest.TestCase):
    """Spec §3.4's table. This is the contract the edge engine depends on."""

    TABLE = {
        "0.10": "0.02",
        "0.25": "0.03",
        "0.40": "0.03",
        "0.50": "0.03",
        "0.64": "0.03",
        "0.75": "0.03",
        "0.90": "0.02",
    }

    def test_matches_the_spec_table(self):
        for price, expected in sorted(self.TABLE.items()):
            with self.subTest(price=price):
                self.assertEqual(fees.fee(D(price)), D(expected))

    def test_is_three_cents_across_the_whole_middle(self):
        """The plateau is the point: a 5c gross edge is 60% fee in the middle."""
        p = D("0.20")
        while p <= D("0.80"):
            with self.subTest(price=str(p)):
                self.assertEqual(fees.fee(p), D("0.03"))
            p += D("0.01")

    def test_never_returns_a_float(self):
        """Float rounding at cent granularity is exactly where a 5c gate goes wrong."""
        self.assertIsInstance(fees.fee(D("0.64")), D)

    def test_accepts_a_string_price_because_the_api_returns_strings(self):
        self.assertEqual(fees.fee("0.64"), D("0.03"))

    def test_rejects_a_price_outside_zero_to_one(self):
        for bad in ("-0.01", "1.01"):
            with self.subTest(price=bad):
                with self.assertRaises(ValueError):
                    fees.fee(bad)


if __name__ == "__main__":
    unittest.main()
