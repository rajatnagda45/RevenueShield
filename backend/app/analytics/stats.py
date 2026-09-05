"""Small, dependency-light statistics for the batch scorecard."""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

import numpy as np


def two_proportion_ztest(x1: int, n1: int, x2: int, n2: int) -> Tuple[Optional[float], Optional[float]]:
    """Two-sided pooled z-test for p1 - p2. Returns (z, p_value) or (None, None) if undefined."""
    if n1 <= 0 or n2 <= 0:
        return None, None
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return (0.0, 1.0) if p1 == p2 else (float("inf"), 0.0)
    z = (p1 - p2) / se
    p_value = math.erfc(abs(z) / math.sqrt(2))  # two-sided
    return round(z, 4), round(min(max(p_value, 0.0), 1.0), 6)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)


def bootstrap_rate_difference(
    treatment_recovered: Sequence[float],
    treatment_at_risk: Sequence[float],
    holdout_recovered: Sequence[float],
    holdout_at_risk: Sequence[float],
    iterations: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> Dict[str, Optional[float]]:
    """Bootstrap CI on the difference in rupee-weighted recovery rate (treatment - holdout).

    Rate per arm = sum(recovered) / sum(at_risk). Cases are resampled with replacement inside
    each arm; the seed makes the interval reproducible for the audit trail.
    """
    t_rec, t_risk = np.asarray(treatment_recovered, dtype=float), np.asarray(treatment_at_risk, dtype=float)
    h_rec, h_risk = np.asarray(holdout_recovered, dtype=float), np.asarray(holdout_at_risk, dtype=float)
    if len(t_rec) == 0 or len(h_rec) == 0:
        return {"lower": None, "upper": None, "point": None}

    rng = np.random.RandomState(seed)
    diffs = np.empty(iterations)
    nt, nh = len(t_rec), len(h_rec)
    for i in range(iterations):
        ti = rng.randint(0, nt, nt)
        hi = rng.randint(0, nh, nh)
        t_rate = t_rec[ti].sum() / max(t_risk[ti].sum(), 1e-9)
        h_rate = h_rec[hi].sum() / max(h_risk[hi].sum(), 1e-9)
        diffs[i] = t_rate - h_rate
    point = t_rec.sum() / max(t_risk.sum(), 1e-9) - h_rec.sum() / max(h_risk.sum(), 1e-9)
    lower, upper = np.percentile(diffs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"lower": round(float(lower), 6), "upper": round(float(upper), 6), "point": round(float(point), 6)}


def percentiles(values: Sequence[float], points: Sequence[int] = (50, 90)) -> Dict[str, Optional[float]]:
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    if arr.size == 0:
        return {f"p{p}": None for p in points}
    return {f"p{p}": round(float(np.percentile(arr, p)), 2) for p in points}
