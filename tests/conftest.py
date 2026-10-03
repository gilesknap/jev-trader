import datetime as dt

import numpy as np
import pandas as pd
import pytest

from trader import golive
from trader import scoreboard as SB
from trader.data import ET

# The tests write trades and sessions in October 2026, so they run against a fixed experiment start rather
# than whatever config.yaml says (a new deployment sets its own date, often in the future). Every consumer
# reads one of these two module attributes: golive.START_DATE (the gate, runner, `trader validate`) and
# scoreboard.EXPERIMENT_START (the scoreboard and dashboard).
TEST_START_DATE = dt.date(2026, 10, 5)


def pytest_configure(config):
    config.addinivalue_line("markers", "config_start_date: use config.yaml's experiment.start_date, unpinned")


@pytest.fixture(autouse=True)
def pinned_start_date(request, monkeypatch):
    if request.node.get_closest_marker("config_start_date") is None:
        monkeypatch.setattr(golive, "START_DATE", TEST_START_DATE)
        monkeypatch.setattr(SB, "EXPERIMENT_START", TEST_START_DATE)
    return TEST_START_DATE


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
