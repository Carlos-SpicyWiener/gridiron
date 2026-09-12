"""Per-league recalibration of the model's stated probability. Spec §4.0, Appendix C."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import calibration


class Calibrate(unittest.TestCase):
    """Appendix C's fixture table is the contract. Pooled 2022-2025 fit."""

    NFL = {
        0.50: 0.512, 0.55: 0.552, 0.60: 0.592, 0.65: 0.632, 0.70: 0.672,
        0.73: 0.698, 0.80: 0.759, 0.86: 0.815, 0.90: 0.856,
    }
    CFB = {
        0.50: 0.473, 0.55: 0.532, 0.60: 0.592, 0.65: 0.652, 0.70: 0.711,
        0.73: 0.745, 0.80: 0.823, 0.86: 0.886, 0.90: 0.924,
    }

    def test_matches_the_nfl_fixture_table(self):
        for stated, expected in sorted(self.NFL.items()):
            with self.subTest(stated=stated):
                self.assertAlmostEqual(calibration.calibrate(stated, "nfl"), expected, places=3)

    def test_matches_the_cfb_fixture_table(self):
        for stated, expected in sorted(self.CFB.items()):
            with self.subTest(stated=stated):
                self.assertAlmostEqual(calibration.calibrate(stated, "cfb"), expected, places=3)


class TheSignFlip(unittest.TestCase):
    """The whole point of §4.0: neither league is corrected in one direction."""

    def test_nfl_raises_below_its_crossover_and_lowers_above(self):
        self.assertGreater(calibration.calibrate(0.50, "nfl"), 0.50)
        self.assertLess(calibration.calibrate(0.75, "nfl"), 0.75)

    def test_cfb_lowers_below_its_crossover_and_raises_above(self):
        self.assertLess(calibration.calibrate(0.50, "cfb"), 0.50)
        self.assertGreater(calibration.calibrate(0.90, "cfb"), 0.90)

    def test_crossovers_are_where_the_spec_says(self):
        self.assertAlmostEqual(calibration.crossover("nfl"), 0.559, places=2)
        self.assertAlmostEqual(calibration.crossover("cfb"), 0.641, places=2)


class Invariants(unittest.TestCase):

    def test_is_monotone(self):
        for league in ("nfl", "cfb"):
            prev, p = 0.0, 0.01
            while p <= 0.99:
                got = calibration.calibrate(p, league)
                with self.subTest(league=league, p=round(p, 2)):
                    self.assertGreater(got, prev)
                prev, p = got, p + 0.01

    def test_stays_strictly_inside_zero_and_one(self):
        for league in ("nfl", "cfb"):
            for p in (0.001, 0.5, 0.999):
                with self.subTest(league=league, p=p):
                    got = calibration.calibrate(p, league)
                    self.assertGreater(got, 0.0)
                    self.assertLess(got, 1.0)

    def test_rejects_an_unknown_league(self):
        """Phase 2 must add coefficients deliberately, not inherit NFL's by accident."""
        with self.assertRaises(KeyError):
            calibration.calibrate(0.6, "cbb")

    def test_rejects_a_degenerate_probability(self):
        for bad in (0.0, 1.0, -0.1, 1.1):
            with self.subTest(p=bad):
                with self.assertRaises(ValueError):
                    calibration.calibrate(bad, "nfl")


class Refit(unittest.TestCase):
    """The published coefficients must be reproducible from the bucket data.
    A transcription error here silently mis-sizes every bet."""

    def test_fit_recovers_the_published_coefficients_from_pooled_buckets(self):
        for league in ("nfl", "cfb"):
            with self.subTest(league=league):
                a, b = calibration.fit(calibration.pooled(league))
                self.assertAlmostEqual(a, calibration.PLATT[league][0], places=2)
                self.assertAlmostEqual(b, calibration.PLATT[league][1], places=2)

    def test_nfl_compresses_and_cfb_expands(self):
        self.assertLess(calibration.PLATT["nfl"][1], 1.0)
        self.assertGreater(calibration.PLATT["cfb"][1], 1.0)

    def test_a_perfectly_calibrated_league_fits_the_identity(self):
        """Sanity check on fit() itself: no bias in, no correction out."""
        buckets = [(p / 100.0, 500, p / 100.0) for p in range(30, 71, 5)]
        a, b = calibration.fit(buckets)
        self.assertAlmostEqual(a, 0.0, places=1)
        self.assertAlmostEqual(b, 1.0, places=1)

    def test_pooling_preserves_the_total_sample(self):
        for league in ("nfl", "cfb"):
            with self.subTest(league=league):
                windows = calibration.BUCKETS[league]
                total = sum(n for rows in windows.values() for _, n, _ in rows)
                self.assertEqual(sum(n for _, n, _ in calibration.pooled(league)), total)


class CrossValidation(unittest.TestCase):
    """The README's own next-step: fit on one window, validate on the other.
    Fitting and measuring on the same seasons would just be overfitting."""

    def test_the_correction_holds_up_out_of_sample_in_both_directions(self):
        for league in ("nfl", "cfb"):
            windows = sorted(calibration.BUCKETS[league])
            for train, test in ((windows[0], windows[1]), (windows[1], windows[0])):
                with self.subTest(league=league, train=train, test=test):
                    a, b = calibration.fit(calibration.BUCKETS[league][train])
                    held = calibration.BUCKETS[league][test]
                    self.assertGreater(calibration.log_likelihood(held, a, b),
                                       calibration.log_likelihood(held, 0.0, 1.0))

    def test_the_out_of_sample_gain_is_small_and_the_docstring_says_so(self):
        """Guards against anyone later reading this layer as a big win.
        It is a sizing correction, not a forecasting improvement."""
        a, b = calibration.fit(calibration.BUCKETS["nfl"]["2022-2023"])
        held = calibration.BUCKETS["nfl"]["2024-2025"]
        n = sum(row[1] for row in held)
        gain = (calibration.log_likelihood(held, a, b)
                - calibration.log_likelihood(held, 0.0, 1.0)) / n
        self.assertLess(gain, 0.01)


if __name__ == "__main__":
    unittest.main()
