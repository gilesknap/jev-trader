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
