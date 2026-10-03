"""`trader replay --cash` defaults to config.yaml's capital.replay_cash."""

import json

import pytest

from test_engine import spec
from trader import cli, config
from trader import replay as R


@pytest.fixture
def replay_cli(tmp_path, monkeypatch, session, capsys):
    bars = session(path=[100.0] * 30)
    day = bars.index[0].date().isoformat()
    settings = config.SETTINGS.model_copy(
        update={"capital": config.SETTINGS.capital.model_copy(update={"replay_cash": 1234.0})})
    monkeypatch.setattr(config, "SETTINGS", settings)
    monkeypatch.setattr(config, "REPLAY_DIR", tmp_path / "replays")
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    monkeypatch.setattr(config, "universe", lambda: ["SPY"])
    monkeypatch.setattr(cli, "_specs", lambda *a, **k: [spec()])
    monkeypatch.setattr(R, "load_sessions", lambda *a, **k: {"SPY": {bars.index[0].date(): bars}})

    def run(*extra):
        cli.main(["replay", "--decider", "stub", "--name", "t", "--start", day, "--end", day, *extra])
        out = capsys.readouterr().out
        return json.loads(out[:out.rindex("}") + 1])["start_equity"]

    return run


def test_replay_starts_with_the_configured_replay_cash(replay_cli):
    assert replay_cli() == 1234.0


def test_replay_cash_flag_still_overrides_it(replay_cli):
    assert replay_cli("--cash", "300") == 300.0
