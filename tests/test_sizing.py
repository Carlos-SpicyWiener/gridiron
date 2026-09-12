"""Fractional-Kelly stake and contract count. Spec §5."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import sizing

BR = 1000.0  # illustrative; every assertion below is really about fractions


class KellyFraction(unittest.TestCase):
    """Spec §5's fixture table. `p` is already calibrated at this point."""

    # Derived from the `p` as written, so the table is self-consistent and can
    # actually serve as a test. Quoting a rounded p next to a full-precision
    # kelly makes a fixture that never reproduces.
    CASES = [
        # label,               p,     price, kelly_full, stake_pct, contracts_per_1k
        ("uncalibrated 0.73", 0.730, 0.64, 0.1818182, 0.045455, 67),
        ("calibrated 0.73",   0.698, 0.64, 0.0848485, 0.021212, 31),
        ("DEN @ KC",          0.592, 0.45, 0.2153846, 0.050000, 104),
        ("DAL @ NYG",         0.512, 0.40, 0.1438596, 0.035965, 83),
        ("small stake",       0.560, 0.50, 0.0638298, 0.015957, 30),
    ]

    def test_matches_the_spec_fixture_table(self):
        for label, p, price, kelly, stake_pct, contracts in self.CASES:
            with self.subTest(case=label):
                got = sizing.size(p, price, BR)
                self.assertAlmostEqual(got.kelly_full, kelly, places=6)
                self.assertAlmostEqual(got.stake / BR, stake_pct, places=6)
                self.assertEqual(got.contracts, contracts)

    def test_charges_the_fee_in_the_denominator_too(self):
        """Kelly is on what you pay, not on the quoted price."""
        got = sizing.size(0.73, 0.64, BR)
        # all_in = 0.64 + 0.03; ignoring the fee would give 0.1818 -> 0.25
        self.assertAlmostEqual(got.all_in, 0.67, places=6)
        self.assertNotAlmostEqual(got.kelly_full, 0.25, places=2)


class Caps(unittest.TestCase):

    def test_the_five_percent_cap_binds_on_a_big_edge(self):
        got = sizing.size(0.606, 0.45, BR)
        self.assertTrue(got.capped)
        self.assertAlmostEqual(got.stake, BR * 0.05, places=6)

    def test_the_cap_does_not_bind_on_a_marginal_edge(self):
        got = sizing.size(0.704, 0.64, BR)
        self.assertFalse(got.capped)

    def test_at_the_edge_floor_the_cap_only_catches_heavy_favourites(self):
        """Spec §5: crossover near c = 0.73."""
        below = sizing.size(0.65 + 0.03 + 0.05, 0.65, BR)
        above = sizing.size(0.80 + 0.03 + 0.05, 0.80, BR)
        self.assertFalse(below.capped)
        self.assertTrue(above.capped)


class NoBet(unittest.TestCase):

    def test_no_edge_means_no_contracts(self):
        got = sizing.size(0.50, 0.50, BR)
        self.assertEqual(got.contracts, 0)
        self.assertEqual(got.stake, 0.0)

    def test_negative_edge_never_returns_a_negative_stake(self):
        got = sizing.size(0.30, 0.70, BR)
        self.assertEqual(got.contracts, 0)
        self.assertGreaterEqual(got.stake, 0.0)

    def test_a_stake_below_one_contract_buys_nothing(self):
        got = sizing.size(0.56, 0.50, 20.0)
        self.assertEqual(got.contracts, 0)

    def test_contracts_never_cost_more_than_the_stake(self):
        for p, price in ((0.606, 0.45), (0.532, 0.40), (0.704, 0.64)):
            with self.subTest(p=p):
                got = sizing.size(p, price, BR)
                self.assertLessEqual(got.contracts * got.all_in, got.stake + 1e-9)


class Allocation(unittest.TestCase):
    """Spec §5: fill highest edge first; the 20% open-exposure cap binds routinely."""

    def test_fills_highest_edge_first(self):
        cands = [
            {"id": "low", "p": 0.532, "price": 0.40, "edge": 0.102},
            {"id": "high", "p": 0.606, "price": 0.45, "edge": 0.126},
        ]
        filled = sizing.allocate(cands, BR)
        self.assertEqual([c["id"] for c in filled if c["contracts"]][0], "high")

    def test_stops_at_the_open_exposure_cap(self):
        cands = [{"id": f"c{i}", "p": 0.606, "price": 0.45, "edge": 0.126 - i * 0.001}
                 for i in range(8)]
        filled = sizing.allocate(cands, BR)
        spent = sum(c["contracts"] * c["all_in"] for c in filled)
        self.assertLessEqual(spent, BR * 0.20 + 1e-9)

    def test_counts_existing_open_exposure_against_the_cap(self):
        cands = [{"id": "a", "p": 0.606, "price": 0.45, "edge": 0.126}]
        filled = sizing.allocate(cands, BR, open_exposure=BR * 0.20)
        self.assertEqual(filled[0]["contracts"], 0)
        self.assertEqual(filled[0]["gate_result"], "exposure_capped")

    def test_a_smaller_candidate_still_fits_after_a_big_one_is_skipped(self):
        """Skipping must not mean stopping — leaving headroom unused helps nobody."""
        cands = [
            {"id": "big", "p": 0.606, "price": 0.45, "edge": 0.126},
            {"id": "small", "p": 0.56, "price": 0.50, "edge": 0.03},
        ]
        filled = sizing.allocate(cands, BR, open_exposure=BR * 0.18)
        by_id = {c["id"]: c for c in filled}
        self.assertEqual(by_id["big"]["gate_result"], "exposure_capped")
        self.assertGreater(by_id["small"]["contracts"], 0)

    def test_marks_filled_candidates_as_candidates(self):
        cands = [{"id": "a", "p": 0.606, "price": 0.45, "edge": 0.126}]
        filled = sizing.allocate(cands, BR)
        self.assertEqual(filled[0]["gate_result"], "candidate")


if __name__ == "__main__":
    unittest.main()
