"""`trader validate` fetches sample bars only when there are custom features to gate."""

import pytest

from trader import cli, config


@pytest.fixture
def custom(tmp_path, monkeypatch):
    d = tmp_path / "custom"
    monkeypatch.setattr(config, "CUSTOM_FEATURES_DIR", d)
    return d


@pytest.fixture
def classifiers(tmp_path):
    f = tmp_path / "classifiers.yaml"
    f.write_text("classifiers: []\n")
    return str(f)


def no_fetch(*a, **k):
    raise AssertionError("fetched sample bars with no custom features to gate")


@pytest.mark.parametrize("files", [[], ["_helpers.py"]])
def test_no_custom_features_means_no_fetch(custom, classifiers, monkeypatch, capsys, files):
    if files:
        custom.mkdir()
    for name in files:
        (custom / name).write_text("x = 1\n")
    monkeypatch.setattr(cli, "_gate_samples", no_fetch)
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--file", classifiers])
    out = capsys.readouterr().out
    assert e.value.code == 0 and '"custom_ok": true' in out and "classifiers OK" in out


def test_a_failed_fetch_is_one_line_and_still_fails(custom, classifiers, monkeypatch, capsys):
    custom.mkdir()
    (custom / "mine.py").write_text("x = 1\n")

    def down(*a, **k):
        raise ConnectionError("alpaca unreachable")

    monkeypatch.setattr(cli, "_gate_samples", down)
    monkeypatch.setattr("trader.features.harness.run_gate", no_fetch)  # never gated on no samples
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--file", classifiers])
    msg = str(e.value.code)
    assert msg.startswith("custom features NOT gated") and "ConnectionError: alpaca unreachable" in msg
    assert "\n" not in msg and "Traceback" not in capsys.readouterr().out


def test_custom_features_are_gated_on_the_fetched_samples(custom, classifiers, monkeypatch):
    custom.mkdir()
    (custom / "mine.py").write_text("x = 1\n")
    seen = []
    monkeypatch.setattr(cli, "_gate_samples", lambda source: ["sample"])

    def gate(directory, samples):
        seen.append(samples)
        return type("R", (), {"ok": False, "features": [], "errors": {"mine.py": "bad"}})()

    monkeypatch.setattr("trader.features.harness.run_gate", gate)
    monkeypatch.setattr(cli, "_specs", lambda *a, **k: [])
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--file", classifiers])
    assert seen == [["sample"]] and e.value.code == 1  # a rejected feature still fails validate


def _fake_load_sessions(trading_days, calls):
    """load_sessions over a fixed set of trading days, honouring its [start - 6 days, end] window."""
    import datetime as dt

    def load(symbols, start, end, secrets, source="alpaca"):
        calls.append((start, end))
        days = [d for d in trading_days if start - dt.timedelta(days=6) <= d <= end]
        return {s: {d: f"{s}-{d}" for d in days} for s in symbols}

    return load


def test_gate_samples_widen_past_a_run_of_closed_days(monkeypatch):
    import datetime as dt

    today = dt.date.today()
    # The last three sessions are a month back: the default window holds none of them.
    trading = [today - dt.timedelta(days=n) for n in (32, 31, 30)]
    calls = []
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    monkeypatch.setattr("trader.replay.load_sessions", _fake_load_sessions(trading, calls))
    samples = cli._gate_samples()
    assert len(calls) > 1  # it widened rather than returning nothing
    d0, d1, d2 = sorted(trading)
    assert samples == [
        (f"SPY-{d1}", f"SPY-{d0}", f"SPY-{d1}"),
        (f"SPY-{d2}", f"SPY-{d1}", f"SPY-{d2}"),
        (f"QQQ-{d1}", f"QQQ-{d0}", f"SPY-{d1}"),
        (f"QQQ-{d2}", f"QQQ-{d1}", f"SPY-{d2}"),
    ]


def test_gate_samples_raise_rather_than_return_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    monkeypatch.setattr("trader.replay.load_sessions", _fake_load_sessions([], calls))
    with pytest.raises(RuntimeError, match="SPY session"):
        cli._gate_samples()
    assert len(calls) == 3


def test_gate_samples_use_one_fetch_in_a_normal_week(monkeypatch):
    import datetime as dt

    today = dt.date.today()
    trading = [today - dt.timedelta(days=n) for n in (5, 4, 3, 2, 1)]
    calls = []
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    monkeypatch.setattr("trader.replay.load_sessions", _fake_load_sessions(trading, calls))
    assert len(cli._gate_samples()) == 4 and len(calls) == 1
