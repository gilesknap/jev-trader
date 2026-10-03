"""The runner never follows symlinks in the strategist's checkout (issue #49), and a refused
file makes that file or classifier invalid, never an exception out of session start."""

import datetime as dt
import os
import shutil
import signal

import pandas as pd
import pytest

from conftest import make_session
from trader import config, golive, runner
from trader import features as F
from trader.classifier import load_specs
from trader.data import ET
from trader.features.sandbox import FeatureSandbox

needs_bwrap = pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")

SECRET = "ALPACA_LIVE_KEY=hunter2secret"
CLASSIFIERS = """
classifiers:
  - id: plain
    family: conventional
    symbols: [SPY]
    window: ["10:00", "15:00"]
    trigger: [{feature: ret_1m_pct, op: ">", value: 0}]
    features: [ret_1m_pct]
    context: "x"
    entry: {instructions: "enter?", criteria: {ENTER: "a", WAIT: "b"}, threshold: 0.6}
    exit: {instructions: "hold?", criteria: {HOLD: "a", EXIT: "b"}, threshold: 0.6}
    size_fraction: 0.1
    stop_pct: 0.5
    target_pct: 1.0
"""
GOOD_FEATURE = '''
from trader.features import feature

@feature("sl_last_close_ratio", source="custom")
def f(bars, ctx):
    """Last close over first open."""
    return float(bars.close.iloc[-1] / bars.open.iloc[0])
'''


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A strategist checkout at tmp_path/strategist, and a runner secret beside it."""
    root = tmp_path / "strategist"
    (root / "state").mkdir(parents=True)
    (root / "features" / "custom").mkdir(parents=True)
    (tmp_path / "secret.env").write_text(f"# runner secrets\n{SECRET}\n")
    monkeypatch.setattr(config, "STRATEGIST_ROOT", root)
    monkeypatch.setattr(config, "CUSTOM_FEATURES_DIR", root / "features" / "custom")
    return root


def test_symlinked_classifiers_file_is_refused_without_quoting_the_target(checkout, tmp_path):
    (checkout / "state" / "classifiers.yaml").symlink_to(tmp_path / "secret.env")
    with pytest.raises(ValueError, match="symlink") as e:
        load_specs(checkout / "state" / "classifiers.yaml", set(F.REGISTRY), {"SPY"})
    assert "hunter2" not in str(e.value)


def test_symlinked_state_directory_is_refused(checkout, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "classifiers.yaml").write_text(CLASSIFIERS)
    shutil.rmtree(checkout / "state")
    (checkout / "state").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="state is a symlink"):
        load_specs(checkout / "state" / "classifiers.yaml", set(F.REGISTRY), {"SPY"})


def test_a_real_classifiers_file_still_loads_and_a_missing_one_is_not_found(checkout):
    f = checkout / "state" / "classifiers.yaml"
    with pytest.raises(FileNotFoundError, match="not found") as e:
        load_specs(f, set(F.REGISTRY), {"SPY"})
    assert "symlink" not in str(e.value)
    f.write_text(CLASSIFIERS)
    assert [s.id for s in load_specs(f, set(F.REGISTRY), {"SPY"})] == ["plain"]


def test_a_fifo_is_refused_instead_of_hanging_the_runner(checkout):
    os.mkfifo(checkout / "state" / "classifiers.yaml")
    old = signal.signal(signal.SIGALRM, lambda *a: pytest.fail("reading a FIFO blocked"))
    signal.alarm(5)
    try:
        with pytest.raises(ValueError, match="not a regular file"):
            load_specs(checkout / "state" / "classifiers.yaml", set(F.REGISTRY), {"SPY"})
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_digest_is_unchanged_for_real_files_and_never_follows_or_raises(checkout, tmp_path):
    import hashlib

    custom = checkout / "features" / "custom"
    (custom / "a.py").write_text(GOOD_FEATURE)
    (custom / "b.py").write_text("# helper\n")
    old = hashlib.sha256()  # the pre-#49 digest: existing promotion records must keep their hash
    for f in sorted(custom.glob("*.py")):
        old.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    assert golive.custom_features_digest(custom) == old.hexdigest()[:16]

    (custom / "evil.py").symlink_to(tmp_path / "secret.env")
    (custom / "gone.py").symlink_to(tmp_path / "missing.py")  # dangling: read_bytes() raised here
    linked = golive.custom_features_digest(custom)
    (tmp_path / "secret.env").write_text("something else\n")
    assert golive.custom_features_digest(custom) == linked  # the target's contents never count

    shutil.rmtree(custom)
    (checkout / "features" / "custom").symlink_to(tmp_path)  # a symlinked directory
    assert golive.custom_features_digest(custom) != golive.custom_features_digest(tmp_path / "nothing-here")


def test_enforce_promotion_survives_a_symlinked_feature(checkout, tmp_path):
    from test_engine import spec

    custom = checkout / "features" / "custom"
    (custom / "gone.py").symlink_to(tmp_path / "missing.py")
    s = spec(id="idea", mode="live")
    out = golive.enforce_promotion(
        [s], lambda *a: None, True, dt.date(2026, 10, 19), tmp_path, tmp_path / "promotion.json", custom
    )
    assert [x.mode for x in out] == ["shadow"]


@needs_bwrap
def test_sandbox_refuses_a_symlinked_feature_file_and_loads_the_rest(checkout, tmp_path, session):
    custom = checkout / "features" / "custom"
    (custom / "good.py").write_text(GOOD_FEATURE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "linked.py").write_text(GOOD_FEATURE.replace("sl_last_close_ratio", "sl_linked"))
    (custom / "linked.py").symlink_to(elsewhere / "linked.py")
    sb = FeatureSandbox(custom)
    try:
        sb.start([(session(), session(day=dt.date(2026, 9, 18)), session())])
        assert sb.broken is None
        assert sb.names == {"sl_last_close_ratio"}
        assert "symlink" in sb.errors["linked.py"]
    finally:
        sb.close()


@needs_bwrap
def test_sandbox_refuses_a_symlinked_features_directory(checkout, tmp_path, session):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "good.py").write_text(GOOD_FEATURE)
    shutil.rmtree(checkout / "features" / "custom")
    (checkout / "features" / "custom").symlink_to(elsewhere)
    alerts = []
    sb = FeatureSandbox(checkout / "features" / "custom", lambda lvl, msg: alerts.append(msg))
    try:
        sb.start([(session(), session(day=dt.date(2026, 9, 18)), session())])
        assert sb.names == set()
        assert "custom is a symlink" in sb.broken and alerts
    finally:
        sb.close()


class _ReachedEngine(Exception):
    pass


def test_runner_session_start_survives_symlinked_features(checkout, tmp_path, monkeypatch):
    """The #80 regression: a symlinked feature made session start raise, so systemd restarted
    the runner every 30 s all day. It must alert, drop what depends on it, and carry on."""
    custom = checkout / "features" / "custom"
    (custom / "evil.py").symlink_to(tmp_path / "secret.env")
    (custom / "gone.py").symlink_to(tmp_path / "missing.py")
    (checkout / "state" / "classifiers.yaml").write_text(CLASSIFIERS)

    now = dt.datetime.now(ET)
    days = [dt.date(2026, 9, d) for d in (21, 22, 23, 24)]
    bars = pd.concat([make_session(day=d) for d in days])
    alerts, reached = [], {}

    class FakeBroker:
        def __init__(self, *a, **k):
            self.client = None

    def fake_engine(specs, *a, **k):
        reached["specs"] = [s.id for s in specs]
        raise _ReachedEngine

    monkeypatch.setattr(config, "load_secrets", lambda: {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"})
    monkeypatch.setattr(runner, "notify", lambda level, msg, **k: alerts.append(msg))
    monkeypatch.setattr(runner, "AlpacaBroker", FakeBroker)
    monkeypatch.setattr(
        runner, "_session_today", lambda client: (now - dt.timedelta(hours=1), now + dt.timedelta(hours=1))
    )
    monkeypatch.setattr(runner, "fetch_alpaca", lambda symbols, *a, **k: {s: bars for s in symbols})
    monkeypatch.setattr(runner, "Book", lambda *a, **k: object())
    monkeypatch.setattr(runner, "reconcile_sim_accounts", lambda *a, **k: None)
    monkeypatch.setattr(runner, "sim_books", lambda *a, **k: {})
    monkeypatch.setattr(runner, "Engine", fake_engine)
    monkeypatch.setattr(golive, "resolve_mode", lambda *a, **k: "paper")
    monkeypatch.setattr(golive, "PROMOTION_FILE", tmp_path / "promotion.json")
    monkeypatch.setattr(F, "SANDBOX", None)
    try:
        with pytest.raises(_ReachedEngine):
            runner.run_session("stub", file=checkout / "state" / "classifiers.yaml")
    finally:
        if F.SANDBOX is not None:
            F.SANDBOX.close()
    assert reached["specs"] == ["plain"]  # library-only classifiers still trade
    assert any("evil.py" in a and "symlink" in a for a in alerts)
    assert not any("hunter2" in a for a in alerts)


def test_a_directory_named_py_is_refused_without_leaking_descriptors(checkout):
    from trader.safeio import read_sources

    custom = checkout / "features" / "custom"
    (custom / "x.py").mkdir()
    (custom / "good.py").write_text(GOOD_FEATURE)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(200):
        read_sources(custom)
        golive.custom_features_digest(custom)
    assert len(os.listdir("/proc/self/fd")) <= before
    sources, refused = read_sources(custom)
    assert list(sources) == ["good.py"]
    assert refused == {"x.py": f"{custom / 'x.py'} is not a regular file"}


def test_an_oversized_file_is_refused(checkout):
    from trader.safeio import MAX_BYTES

    f = checkout / "state" / "classifiers.yaml"
    f.write_text(CLASSIFIERS + "#" * MAX_BYTES)
    with pytest.raises(ValueError, match="classifiers.yaml is too large"):
        load_specs(f, set(F.REGISTRY), {"SPY"})


@needs_bwrap
def test_a_full_tmp_disables_custom_features_but_not_library_classifiers(checkout, monkeypatch, session):
    from trader.features import sandbox
    from trader.features.harness import run_gate

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    (checkout / "features" / "custom" / "good.py").write_text(GOOD_FEATURE)
    (checkout / "state" / "classifiers.yaml").write_text(CLASSIFIERS)
    monkeypatch.setattr(sandbox.tempfile, "TemporaryDirectory", full)
    monkeypatch.setattr(F, "SANDBOX", None)
    alerts = []
    try:
        report = run_gate(
            checkout / "features" / "custom",
            [(session(), session(day=dt.date(2026, 9, 18)), session())],
            alert=lambda lvl, msg: alerts.append(msg),
        )
    finally:
        if F.SANDBOX is not None:
            F.SANDBOX.close()
    assert "No space left" in report.errors["_sandbox"] and alerts
    assert [s.id for s in load_specs(checkout / "state" / "classifiers.yaml", F.known_features(), {"SPY"})] == ["plain"]
