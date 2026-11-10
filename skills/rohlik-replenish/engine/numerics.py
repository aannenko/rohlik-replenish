"""Rounding, medians and percentiles (spec §6.0)."""

from __future__ import annotations

import math
from collections.abc import Sequence

from .constants import EPS


def round_up(x: float) -> int:
    return math.ceil(x - EPS)


def round_down(x: float) -> int:
    return math.floor(x + EPS)


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5 + EPS)


def median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        raise ValueError("median of nothing")
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def percentile_nearest_rank(values: Sequence[float], p: float) -> float:
    """Rank ceil(p * n), ascending, 1-based."""
    s = sorted(values)
    if not s:
        raise ValueError("percentile of nothing")
    rank = max(1, min(len(s), round_up(p * len(s))))
    return float(s[rank - 1])


def gt(a: float, b: float) -> bool:
    return a > b + EPS


def ge(a: float, b: float) -> bool:
    return a >= b - EPS


def lt(a: float, b: float) -> bool:
    return a < b - EPS


def le(a: float, b: float) -> bool:
    return a <= b + EPS


def eq(a: float, b: float) -> bool:
    return abs(a - b) <= EPS


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def tidy(x: float | None, digits: int = 6) -> float | None:
    """Stable output form for JSON (no float noise such as 0.30000000000000004)."""
    if x is None:
        return None
    r = round(x, digits)
    return 0.0 if r == 0 else r
