import datetime as dt

import numpy as np
import pandas as pd
import pytest

from trader.data import ET


def make_session(day=dt.date(2026, 9, 21), start=100.0, drift=0.0, n=390, seed=0, path=None):
    """Synthetic 1-min RTH bars. `path` (list of closes) overrides the random walk."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(dt.datetime.combine(day, dt.time(9, 30), ET), periods=n, freq="1min")
    closes = np.array(path, float) if path is not None else start * np.exp(np.cumsum(rng.normal(drift, 0.0005, n)))
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) * 1.0002,
        "low": np.minimum(opens, closes) * 0.9998, "close": closes, "volume": 1000.0,
    }, index=idx[: len(closes)])


@pytest.fixture
def session():
    return make_session
