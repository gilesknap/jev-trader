"""Small statistics helpers with no other dependencies, shared by the scoreboard and probe-report."""

from __future__ import annotations

import math

# Two-sided 95% Student-t critical values by degrees of freedom; 1.96 beyond the table.
_T95 = {
    1: 12.71,
    2: 4.30,
    3: 3.18,
    4: 2.78,
    5: 2.57,
    6: 2.45,
    7: 2.36,
    8: 2.31,
    9: 2.26,
    10: 2.23,
    12: 2.18,
    15: 2.13,
    20: 2.09,
    25: 2.06,
    30: 2.04,
    60: 2.00,
    120: 1.98,
}


def t95(df: float) -> float:
    if df < 1:
        return math.inf
    keys = [k for k in _T95 if k <= df]
    return _T95[max(keys)] if df <= 120 else 1.96
