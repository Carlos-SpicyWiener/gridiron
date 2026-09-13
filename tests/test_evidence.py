"""Does the model anticipate the market? Spec §7.

CLV proper needs twenty graded bets. This is the same question asked of data
that already exists: when the model disagrees with the opening line, does the
line subsequently move toward it?
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import support  # noqa: E402

from lib import evidence  # noqa: E402


class SignedMovement(unittest.TestCase):
    """Movement is signed by the direction of the disagreement, so a model that
    is right to fade a team scores the same as one right to back it."""

    def test_backing_a_team_the_line_then_favours_scores_positive(self):
        got = evidence.signed_movement(disagreement=+0.10, movement=+0.03)
        self.assertAlmostEqual(got, +0.03)

    def test_fading_a_team_the_line_then_drops_also_scores_positive(self):
        got = evidence.signed_movement(disagreement=-0.10, movement=-0.03)
        self.assertAlmostEqual(got, +0.03)

    def test_being_wrong_scores_negative(self):
        self.assertAlmostEqual(evidence.signed_movement(+0.10, -0.03), -0.03)
        self.assertAlmostEqual(evidence.signed_movement(-0.10, +0.03), -0.03)

    def test_no_disagreement_contributes_nothing(self):
        self.assertEqual(evidence.signed_movement(0.0, 0.05), 0.0)


class Summarise(unittest.TestCase):

    def _obs(self, pairs, league="nfl"):
        return [evidence.Observation(league, d, m) for d, m in pairs]

    def test_reports_the_mean_and_its_standard_error(self):
        got = evidence.summarise(self._obs([(0.1, 0.02), (0.1, 0.04), (0.1, 0.03)]))
        self.assertEqual(got["n"], 3)
        self.assertAlmostEqual(got["mean"], 0.03, places=6)
        self.assertGreater(got["se"], 0)

    def test_calls_a_two_sigma_result_real(self):
        """Consistently positive with realistic scatter."""
        pairs = [(0.1, 0.02 + (0.004 if i % 2 else -0.004)) for i in range(30)]
        self.assertEqual(evidence.summarise(self._obs(pairs))["verdict"], "real")

    def test_a_perfectly_consistent_effect_is_the_strongest_evidence_not_the_weakest(self):
        """Zero variance makes t infinite. Reporting it as noise inverts the finding."""
        got = evidence.summarise(self._obs([(0.1, 0.02)] * 30))
        self.assertEqual(got["verdict"], "real")

    def test_calls_a_noisy_result_noise(self):
        pairs = [(0.1, 0.05 if i % 2 else -0.05) for i in range(30)]
        self.assertEqual(evidence.summarise(self._obs(pairs))["verdict"], "noise")

    def test_a_consistently_wrong_model_is_not_reported_as_real_edge(self):
        pairs = [(0.1, -0.02 + (0.004 if i % 2 else -0.004)) for i in range(30)]
        got = evidence.summarise(self._obs(pairs))
        self.assertLess(got["mean"], 0)
        self.assertNotEqual(got["verdict"], "real")

    def test_filters_by_minimum_disagreement(self):
        obs = self._obs([(0.01, 0.05), (0.10, 0.01)])
        self.assertEqual(evidence.summarise(obs, min_disagreement=0.05)["n"], 1)

    def test_too_small_a_sample_is_not_a_verdict(self):
        got = evidence.summarise(self._obs([(0.1, 0.02), (0.1, 0.03)]))
        self.assertEqual(got["verdict"], "insufficient")

    def test_an_empty_sample_does_not_divide_by_zero(self):
        got = evidence.summarise([])
        self.assertEqual(got["n"], 0)
        self.assertEqual(got["verdict"], "insufficient")


class DecayWarning(unittest.TestCase):
    """The check that matters most. Real edge should be STRONGER where the model
    disagrees most, because those are the games it would actually bet. A signal
    that fades as disagreement grows is a broad nudge, not insight."""

    def test_flags_a_signal_that_weakens_where_the_model_is_most_confident(self):
        weak_at_the_top = ([evidence.Observation("nfl", 0.02, 0.03)] * 40
                           + [evidence.Observation("nfl", 0.20, 0.001)] * 40)
        self.assertTrue(evidence.decays_with_conviction(weak_at_the_top))

    def test_does_not_flag_a_signal_that_strengthens(self):
        strong_at_the_top = ([evidence.Observation("nfl", 0.02, 0.001)] * 40
                             + [evidence.Observation("nfl", 0.20, 0.03)] * 40)
        self.assertFalse(evidence.decays_with_conviction(strong_at_the_top))


if __name__ == "__main__":
    unittest.main()
