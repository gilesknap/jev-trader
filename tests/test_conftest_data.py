"""How tests/conftest.py chooses the data the suite runs against (#169), one case per layout.

`_use_template_data` is called here with its own environment dict and code root, so nothing touches
this process's environment or the data the rest of the suite already loaded.
"""

from pathlib import Path

import pytest

from conftest import TEMPLATE_DATA, _use_template_data


@pytest.fixture
def monorepo(tmp_path) -> Path:
    root = tmp_path / "code"
    root.mkdir()
    (root / "config.yaml").write_text("owner: {}\n")
    return root


@pytest.fixture
def public(tmp_path) -> Path:
    root = tmp_path / "code"
    root.mkdir()
    return root


@pytest.fixture
def data_main(tmp_path) -> Path:
    """A data repo's `main` checkout: config, no state/."""
    data = tmp_path / "data-main"
    (data / "config").mkdir(parents=True)
    (data / "config.yaml").write_text("owner: {}\n")
    (data / "config" / "mode.yaml").write_text("mode: auto\n")
    return data


def test_monorepo_with_nothing_set_changes_nothing(monorepo):
    env: dict = {}
    assert _use_template_data(env, monorepo) is None
    assert env == {}


def test_public_layout_uses_a_copy_of_the_whole_template(public):
    env: dict = {}
    data = _use_template_data(env, public)
    assert data is not None
    assert env == {"TRADER_DATA_ROOT": str(data), "TRADER_STRATEGIST_ROOT": str(data)}
    assert (data / "config.yaml").is_file() and (data / "state" / "classifiers.yaml").is_file()


@pytest.mark.parametrize("layout", ["monorepo", "public"])
def test_test_data_root_is_both_roots(layout, request, tmp_path):
    root = request.getfixturevalue(layout)
    env = {"TRADER_TEST_DATA_ROOT": str(tmp_path / "mine"), "TRADER_STRATEGIST_ROOT": "/elsewhere"}
    assert _use_template_data(env, root) is None
    assert env["TRADER_DATA_ROOT"] == env["TRADER_STRATEGIST_ROOT"] == str(tmp_path / "mine")


@pytest.mark.parametrize("layout", ["monorepo", "public"])
def test_data_main_checkout_gets_the_template_strategist_tree(layout, request, data_main):
    """The rehearsal's F2: trading-deploy and `3-runner.sh install --split` test against a data-main checkout."""
    root = request.getfixturevalue(layout)
    env = {"TRADER_DATA_ROOT": str(data_main)}
    assert _use_template_data(env, root) is None
    assert env["TRADER_DATA_ROOT"] == str(data_main)  # the config under test stays the candidate's
    strategist = Path(env["TRADER_STRATEGIST_ROOT"])
    assert strategist != data_main
    assert (strategist / "state" / "classifiers.yaml").read_text() == \
        (TEMPLATE_DATA / "strategist" / "state" / "classifiers.yaml").read_text()
    assert not (data_main / "state").exists()  # nothing written into the checkout


def test_a_preset_strategist_root_is_respected(public, data_main, tmp_path):
    env = {"TRADER_DATA_ROOT": str(data_main), "TRADER_STRATEGIST_ROOT": str(tmp_path / "theirs")}
    assert _use_template_data(env, public) is None
    assert env["TRADER_STRATEGIST_ROOT"] == str(tmp_path / "theirs")


def test_a_data_root_with_its_own_state_is_its_own_strategist_root(public, data_main):
    """A single data checkout holding config and state side by side (as STRATEGIST_ROOT's default assumes)."""
    (data_main / "state").mkdir()
    env = {"TRADER_DATA_ROOT": str(data_main)}
    assert _use_template_data(env, public) is None
    assert "TRADER_STRATEGIST_ROOT" not in env
