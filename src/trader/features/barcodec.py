"""The bar encoding shared by the feature sandbox's client and its worker (JSON-safe dicts).

Config-free on purpose: the worker (`trader.features.worker`) runs under bwrap with a cleared
environment, and in split mode (#169) the code checkout has no config.yaml, so nothing the worker
imports may load `trader.config`, directly or transitively. Keep this module's imports to the
standard library, numpy and pandas; tests/test_sandbox.py checks the worker's whole import graph.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def encode_bars(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    return {
        # Epoch nanoseconds whatever the index's unit: pandas 3 builds bar indexes in us (the live stream,
        # alpaca-py), and asi8 counts in that unit, which decode_bars would misread as ns (#181).
        "t": [int(x) for x in df.index.as_unit("ns").asi8],
        "o": df.open.tolist(),
        "h": df.high.tolist(),
        "l": df.low.tolist(),
        "c": df.close.tolist(),
        "v": df.volume.tolist(),
    }


def decode_bars(d: dict) -> pd.DataFrame:
    """Inverse of encode_bars: an America/New_York index in ns, float OHLCV columns."""
    idx = pd.to_datetime(np.array(d["t"], dtype="int64"), utc=True).tz_convert("America/New_York")
    return pd.DataFrame(
        {"open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"], "volume": d["v"]}, index=idx, dtype=float
    )
