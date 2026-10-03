"""Shared fixtures, and the choice of data the suite runs against (#169).

Which data root the tests use, decided here before anything imports `trader` (config.yaml is
loaded at import):
1. TRADER_TEST_DATA_ROOT, if set: a developer's own data checkout (config.yaml, config/mode.yaml,
   state/...), used as both TRADER_DATA_ROOT and TRADER_STRATEGIST_ROOT.
2. An already-set TRADER_DATA_ROOT (e.g. trading-deploy testing candidate code against candidate
   config), left alone. A data repo's `main` checkout holds the config but no state/, so unless
   TRADER_STRATEGIST_ROOT is set too (respected) or that root has a state/ of its own, the strategist
   root becomes a session copy of templates/data/strategist: never the live strategist checkout.
3. Otherwise, if this tree has no config.yaml of its own (the public code repo), a session copy of
   templates/data/main and templates/data/strategist, with the deploy files rendered into it.
4. Otherwise (a monorepo checkout with its own config.yaml, as deployed today), nothing changes.

Whatever the data root, the tests never write outside tmp: TRADER_RUNTIME (with the replay dir and the
strategist stamps under it) is always a session tmp dir, since a developer's shell may export the live ones,
and config.STRATEGIST_ALERTS, which `notify` falls back to when the runtime dir is unwritable, points into tmp
for every test (the `real_strategist_alerts` marker opts out).
"""

import atexit
import datetime as dt
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DATA = ROOT / "templates" / "data"


def _session_tmp(prefix: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    return tmp


def _use_template_data(environ=os.environ, root: Path = ROOT) -> Path | None:
    """Point TRADER_DATA_ROOT/TRADER_STRATEGIST_ROOT (in `environ`) at test data when the environment or
    the tree `root` doesn't supply it. Returns the session copy of the whole template (whose deploy files
    the caller renders), or None when the environment or the tree already supplies the config."""
    if environ.get("TRADER_TEST_DATA_ROOT"):
        data = str(Path(environ["TRADER_TEST_DATA_ROOT"]).absolute())
        environ["TRADER_DATA_ROOT"] = data
        environ["TRADER_STRATEGIST_ROOT"] = data
        return None
    if environ.get("TRADER_DATA_ROOT"):
        # A data repo's main checkout (trading-deploy's candidate data, `3-runner.sh install --split`)
        # has config.yaml but no state/; STRATEGIST_ROOT would default to it and find no classifiers.
        if not environ.get("TRADER_STRATEGIST_ROOT") and not (Path(environ["TRADER_DATA_ROOT"]) / "state").is_dir():
            strategist = _session_tmp("trader-test-strategist-") / "strategist"
            shutil.copytree(TEMPLATE_DATA / "strategist", strategist)
            environ["TRADER_STRATEGIST_ROOT"] = str(strategist)
        return None
    if (root / "config.yaml").exists():
        return None
    data = _session_tmp("trader-test-data-") / "data"
    # One directory for both, as in a single data checkout: config and strategy side by side.
    shutil.copytree(TEMPLATE_DATA / "main", data)
    shutil.copytree(TEMPLATE_DATA / "strategist", data, dirs_exist_ok=True)
    environ["TRADER_DATA_ROOT"] = str(data)
    environ["TRADER_STRATEGIST_ROOT"] = str(data)
    return data


TEST_DATA_ROOT = _use_template_data()
TEST_RUNTIME = _session_tmp("trader-test-runtime-") / "runtime"
TEST_RUNTIME.mkdir()
os.environ["TRADER_RUNTIME"] = str(TEST_RUNTIME)
os.environ.pop("TRADER_REPLAY_DIR", None)  # defaults under TRADER_RUNTIME
os.environ["TRADER_STRATEGIST_STAMP"] = str(TEST_RUNTIME / ".last_run")

# Only now may trader be imported.
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from trader import config as trader_config  # noqa: E402

if TEST_DATA_ROOT is not None:  # render the deploy files a data checkout carries beside config.yaml
    for _rel, _text in trader_config.render_deploy(ROOT, TEST_DATA_ROOT).items():
        (TEST_DATA_ROOT / _rel).parent.mkdir(parents=True, exist_ok=True)
        (TEST_DATA_ROOT / _rel).write_text(_text)

from trader import golive  # noqa: E402
from trader import scoreboard as SB  # noqa: E402
from trader.data import ET  # noqa: E402

# The tests write trades and sessions in October 2026, so they run against a fixed experiment start rather
# than whatever config.yaml says (a new deployment sets its own date, often in the future). Every consumer
# reads one of these two module attributes: golive.START_DATE (the gate, runner, `trader validate`) and
# scoreboard.EXPERIMENT_START (the scoreboard and dashboard).
TEST_START_DATE = dt.date(2026, 10, 5)


def pytest_configure(config):
    config.addinivalue_line("markers", "config_start_date: use config.yaml's experiment.start_date, unpinned")
    config.addinivalue_line("markers", "real_strategist_alerts: leave config.STRATEGIST_ALERTS at its default")


@pytest.fixture(autouse=True)
def pinned_start_date(request, monkeypatch):
    if request.node.get_closest_marker("config_start_date") is None:
        monkeypatch.setattr(golive, "START_DATE", TEST_START_DATE)
        monkeypatch.setattr(SB, "EXPERIMENT_START", TEST_START_DATE)
    return TEST_START_DATE


TEST_STRATEGIST_ALERTS = _session_tmp("trader-test-alerts-") / "strategist-alerts.log"
# The default fallback (in the strategist root), before any test patches it: the outcome the fixture
# below guards, checked at the end of the session.
REAL_STRATEGIST_ALERTS = trader_config.STRATEGIST_ALERTS
_REAL_ALERTS_EXISTED = REAL_STRATEGIST_ALERTS.exists()


def pytest_sessionfinish(session, exitstatus):
    if not _REAL_ALERTS_EXISTED and REAL_STRATEGIST_ALERTS.exists():
        tw = session.config.get_terminal_writer()
        tw.line()
        tw.line(f"FAIL: the suite created the strategist root's alerts log {REAL_STRATEGIST_ALERTS}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def strategist_alerts_in_tmp(request, monkeypatch):
    if request.node.get_closest_marker("real_strategist_alerts") is None:
        monkeypatch.setattr(trader_config, "STRATEGIST_ALERTS", TEST_STRATEGIST_ALERTS)


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
