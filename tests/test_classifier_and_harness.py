import textwrap
from pathlib import Path

import pytest
import yaml

from trader import config
from trader import features as F
from trader.classifier import load_specs
from trader.features.harness import evaluate, load_custom_inprocess, static_check

GOOD = """
classifiers:
  - id: t1
    family: novel
    symbols: [SPY]
    features: [vwap_dist_pct]
    entry: {instructions: "go", criteria: {ENTER: "enter now", WAIT: "not yet"}}
    exit: {instructions: "hold", criteria: {HOLD: "keep", EXIT: "sell"}}
"""


def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


PRELAUNCH_PACK = Path(__file__).resolve().parents[1] / "templates/data/strategist/state/classifiers.yaml"


@pytest.mark.parametrize("path", sorted({PRELAUNCH_PACK, config.CLASSIFIERS_FILE}), ids=str)
def test_repo_classifiers_file_is_valid(path):
    """The pre-launch pack every new data repo starts with, and the classifiers file the suite runs
    against (the same file when the tests use the template data; see conftest.py)."""
    specs = load_specs(path, set(F.REGISTRY), set(config.universe()))
    assert any(s.control for s in specs)


def test_good_spec(tmp_path):
    assert load_specs(write(tmp_path, GOOD), set(F.REGISTRY), {"SPY"})[0].id == "t1"


@pytest.mark.parametrize("mutate,msg", [
    (lambda c: c.update(size_fraction=0.5), "size_fraction"),
    (lambda c: c.update(symbols=["GME"]), "not in universe"),
    (lambda c: c.update(features=["nope"]), "unknown or failed feature"),
    (lambda c: c.update(window=["09:00", "10:00"]), "window"),
    (lambda c: c["exit"]["criteria"].update(SELL_SHORT="x"), "HOLD and EXIT"),
    (lambda c: c.update(stop_pct=15), "stop_pct"),
])
def test_bad_specs(tmp_path, mutate, msg):
    raw = yaml.safe_load(GOOD)
    mutate(raw["classifiers"][0])
    with pytest.raises(Exception, match=msg):
        load_specs(write(tmp_path, yaml.safe_dump(raw)), set(F.REGISTRY), {"SPY"})


def test_static_check_blocks_io(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import os\nx = open('/etc/passwd')\n")
    problems = static_check(bad)
    assert any("import os" in p for p in problems) and any("open" in p for p in problems)


def test_custom_feature_loads_and_evaluates(tmp_path, session):
    (tmp_path / "mine.py").write_text(textwrap.dedent('''
        import numpy as np
        from trader.features import feature

        @feature("test_last_bar_range_pct", source="custom")
        def f(bars, ctx):
            b = bars.iloc[-1]
            return float((b.high / b.low - 1) * 100)
    '''))
    names, errors = load_custom_inprocess(tmp_path)
    assert names == ["test_last_bar_range_pct"] and not errors
    s = session()
    errors = evaluate(names, [(s, s, s)])
    # The 5 ms/call budget is wall-clock: under a concurrent load it can trip, and that isn't what this tests.
    assert all(v.startswith("too slow") for v in errors.values())
    F.REGISTRY.pop("test_last_bar_range_pct")


def test_unquoted_yaml_date_is_accepted(tmp_path):
    from trader.classifier import load_specs

    f = tmp_path / "c.yaml"
    f.write_text("date: 2026-10-06\nclassifiers: []\n")  # exactly as the charter's schema example writes it
    assert load_specs(f, set(), set()) == []
