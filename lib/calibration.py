"""Recalibrate the model's stated probability before betting on it.

`backtest` reports a league-specific calibration bias: NFL overconfident at the
top of its range, college underconfident at the top of its. The model does not
correct for it, deliberately — a pick record survives a biased p perfectly well.
It just reads a little optimistically.

A sizer does not. Kelly puts p straight into the numerator of the stake, so a few
points of bias near the edge threshold is the difference between a bet and no
bet. So the correction lives here, in the betting path only. `prediction.win_prob`
is never touched and the pick record stays comparable across the whole project.

Method is per-league Platt scaling, logit(p_cal) = a + b*logit(p), fitted
n-weighted over the backtest's calibration buckets. Two parameters, monotone, and
it cannot invent a wiggle the data doesn't support — which matters, because
tested against its own standard error not one NFL bucket clears 2 sigma. Naively
subtracting each bucket's gap would be fitting noise.

b < 1 compresses toward 0.5 (overconfident); b > 1 expands away (underconfident).
Note that neither league is corrected in one direction: each has a crossover
below which the correction goes the other way. NFL's is near 0.56, CFB's 0.64.

HOW BIG IS THIS? Small, as forecasting. Fitting one two-season window and testing
on the other improves held-out log-loss by roughly 0.0005-0.0017 per game against
a base of 0.5-0.6 — real in sign, both directions, both leagues, but a fraction
of a percent. It is kept because its effect on SIZING is not small: a 3-point
shift at p=0.73 is most of a 5c edge threshold, and Kelly amplifies exactly that.
Read it as a sizing correction, never as a better model.

Still provisional. Store the raw probability alongside the corrected one on every
bet so `report calibration` can grade this layer itself, and refit only between
seasons — refitting mid-season fits the games you are betting.
"""
import math

MODEL = "platt_backtest_2022_25"

# logit(p_cal) = a + b*logit(p). Derived by fit(pooled(league)), not transcribed;
# test_calibration proves the two agree.
PLATT = {
    "nfl": (0.05, 0.79),
    "cfb": (-0.11, 1.19),
}

# `gridiron backtest --league all --seasons <window> --warmup-from 2021`, run
# 2026-09-12. (bucket midpoint, n, actual hit rate). Kept as two windows rather
# than one pooled table so the out-of-sample check in test_calibration is
# possible at all — that check is the only reason to believe any of this.
BUCKETS = {
    "nfl": {
        "2022-2023": [
            (0.525, 112, 0.473), (0.575, 106, 0.566), (0.625, 91, 0.659),
            (0.675, 88, 0.625), (0.725, 75, 0.707), (0.775, 46, 0.761),
            (0.825, 35, 0.714),
        ],
        "2024-2025": [
            (0.525, 97, 0.464), (0.575, 105, 0.657), (0.625, 95, 0.611),
            (0.675, 67, 0.716), (0.725, 67, 0.746), (0.775, 65, 0.677),
            (0.825, 38, 0.816), (0.875, 24, 0.750),
        ],
    },
    "cfb": {
        "2022-2023": [
            (0.525, 221, 0.502), (0.575, 225, 0.587), (0.625, 191, 0.592),
            (0.675, 201, 0.672), (0.725, 188, 0.750), (0.775, 192, 0.771),
            (0.825, 185, 0.865), (0.875, 197, 0.909), (0.925, 154, 0.909),
            (0.975, 53, 1.000),
        ],
        "2024-2025": [
            (0.525, 225, 0.564), (0.575, 224, 0.500), (0.625, 201, 0.647),
            (0.675, 212, 0.703), (0.725, 209, 0.699), (0.775, 186, 0.747),
            (0.825, 162, 0.870), (0.875, 175, 0.926), (0.925, 181, 0.972),
            (0.975, 76, 0.987),
        ],
    },
}

_EPS = 1e-9


def _logit(p):
    return math.log(p / (1.0 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z))


def calibrate(p, league):
    """The model's stated probability, corrected. This is what edge and Kelly use."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability must be strictly within (0, 1), got {p}")
    a, b = PLATT[league]
    return _sigmoid(a + b * _logit(p))


def crossover(league):
    """The probability at which the correction changes sign."""
    a, b = PLATT[league]
    if b == 1.0:
        raise ValueError(f"{league} has unit slope; no crossover")
    return _sigmoid(a / (1.0 - b))


def pooled(league):
    """All windows for a league as one n-weighted bucket table."""
    merged = {}
    for rows in BUCKETS[league].values():
        for mid, n, actual in rows:
            total_n, total_hits = merged.get(mid, (0, 0.0))
            merged[mid] = (total_n + n, total_hits + n * actual)
    return sorted((mid, n, hits / n) for mid, (n, hits) in merged.items())


def log_likelihood(buckets, a, b):
    """n-weighted binomial log-likelihood of (a, b) against observed buckets.

    Also the out-of-sample scorer: pass a held-out window to ask whether a fit
    from elsewhere actually helps, and (0.0, 1.0) for the uncorrected baseline.
    """
    total = 0.0
    for mid, n, actual in buckets:
        q = min(max(_sigmoid(a + b * _logit(mid)), _EPS), 1.0 - _EPS)
        total += n * (actual * math.log(q) + (1.0 - actual) * math.log(1.0 - q))
    return total


def fit(buckets, grid=0.01, a_range=(-1.2, 1.2), b_range=(0.2, 2.2)):
    """Maximise the n-weighted binomial log-likelihood over (a, b).

    A grid search rather than Newton: two parameters over a bounded region, run
    once a season, and no dependency to install for it. Exhaustive beats clever
    when the whole search costs a few hundred thousand evaluations.
    """
    def steps(lo, hi):
        return [round(lo + i * grid, 10) for i in range(int(round((hi - lo) / grid)) + 1)]

    best = None
    for a in steps(*a_range):
        for b in steps(*b_range):
            value = log_likelihood(buckets, a, b)
            if best is None or value > best[0]:
                best = (value, a, b)
    return best[1], best[2]


def refit():
    """Regenerate PLATT from every window. Run between seasons, never mid-season."""
    return {league: fit(pooled(league)) for league in BUCKETS}
