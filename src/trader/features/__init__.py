"""Feature registry. A feature is a pure function (bars, ctx) -> float.

`bars` holds today's regular-session 1-min bars for one symbol up to and including
the current bar. `ctx` carries prior-day bars, SPY bars and the session clock.
Return NaN when there isn't enough data yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

FeatureFn = Callable[[pd.DataFrame, "FeatureContext"], float]
REGISTRY: dict[str, FeatureFn] = {}
SOURCES: dict[str, str] = {}  # name -> "lib" | custom file name


@dataclass
class FeatureContext:
    prev_day: pd.DataFrame  # previous session's bars for this symbol (may be empty)
    spy: pd.DataFrame  # today's SPY bars up to now (market reference)
    minutes_since_open: float
    minutes_to_close: float
    extra: dict = field(default_factory=dict)


def feature(name: str, source: str = "lib"):
    def wrap(fn: FeatureFn) -> FeatureFn:
        REGISTRY[name] = fn
        SOURCES[name] = source
        return fn

    return wrap


# Set by the runner/CLI to a started trader.features.sandbox.FeatureSandbox. Custom
# (strategist-authored) features are only ever computed through it, never imported here.
SANDBOX = None


def known_features() -> set[str]:
    """Library features plus custom features that passed the sandboxed gate."""
    return set(REGISTRY) | (SANDBOX.names if SANDBOX else set())


def compute_local(names: list[str], bars: pd.DataFrame, ctx: FeatureContext) -> dict[str, float]:
    out: dict[str, float] = {}
    for n in names:
        try:
            out[n] = float(REGISTRY[n](bars, ctx))
        except Exception:
            out[n] = float("nan")
    return out


def compute(names: list[str], bars: pd.DataFrame, ctx: FeatureContext) -> dict[str, float]:
    local = [n for n in names if n in REGISTRY]
    out = compute_local(local, bars, ctx)
    remote = [n for n in names if n not in REGISTRY]
    if remote:
        out.update(SANDBOX.compute(remote, bars, ctx) if SANDBOX else {n: float("nan") for n in remote})
    return {n: out[n] for n in names}


from trader.features import lib  # noqa: E402,F401  (registers the seed library)
