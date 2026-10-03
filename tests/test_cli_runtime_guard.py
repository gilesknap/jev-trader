"""State-changing CLI commands refuse to write to a stray runtime dir (#108)."""

import pytest

from trader import cli, compact, config, golive, runner

# argv, and the function it ends up calling (stubbed: these tests never touch real state)
COMMANDS = [
    (["stop"], runner, "request_stop"),
    (["rebase-paper"], runner, "rebase_paper"),
    (["clear-halt", "paper"], runner, "clear_halt"),
    (["watchdog"], runner, "watchdog"),
    (["run"], runner, "run_session"),
    (["hold-live"], golive, "hold"),
    (["release-live"], golive, "release"),
    (["compact", "--scope", "runtime"], compact, "compact"),
]


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """No TRADER_RUNTIME, a fallback runtime dir that doesn't exist, no services.env, stubbed commands."""
    monkeypatch.delenv("TRADER_RUNTIME", raising=False)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "code" / "runtime")
    monkeypatch.setattr(cli, "SERVICES_ENV", tmp_path / "home" / ".config" / "trading" / "services.env", raising=False)
    made = []
    for _, mod, name in COMMANDS:
        monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: made.append(_n) or 0)
    return made


@pytest.mark.parametrize("argv,mod,name", COMMANDS, ids=[c[0][0] for c in COMMANDS])
def test_refuses_when_fallback_runtime_missing(calls, argv, mod, name, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert "REFUSING" in str(e.value) and "TRADER_RUNTIME" in str(e.value) and "services.env" in str(e.value)
    assert calls == [] and not config.RUNTIME_DIR.exists()


@pytest.mark.parametrize("argv,mod,name", COMMANDS, ids=[c[0][0] for c in COMMANDS])
def test_refuses_on_production_account_even_if_fallback_exists(calls, argv, mod, name):
    # The 2026-09-28 case, second time round: the stray dir now exists, but services.env
    # marks this as the production account, so it still refuses.
    config.RUNTIME_DIR.mkdir(parents=True)
    cli.SERVICES_ENV.parent.mkdir(parents=True)
    cli.SERVICES_ENV.write_text("TRADER_RUNTIME=/srv/trading/runtime\n")
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert "REFUSING" in str(e.value)
    assert calls == []


@pytest.mark.parametrize("argv,mod,name", COMMANDS, ids=[c[0][0] for c in COMMANDS])
def test_runs_with_trader_runtime_set(calls, argv, mod, name, monkeypatch):
    cli.SERVICES_ENV.parent.mkdir(parents=True)
    cli.SERVICES_ENV.write_text("TRADER_RUNTIME=/srv/trading/runtime\n")
    monkeypatch.setenv("TRADER_RUNTIME", str(config.RUNTIME_DIR))
    try:
        cli.main(argv)
    except SystemExit as e:  # `run` exits with the session's return code
        assert e.code == 0
    assert calls == [name]


def test_development_with_existing_runtime_unchanged(calls):
    config.RUNTIME_DIR.mkdir(parents=True)
    cli.main(["stop"])
    assert calls == ["request_stop"]


def test_read_only_and_repo_commands_not_guarded(calls):
    cli.main(["compact", "--scope", "repo", "--dry-run"])  # stubbed; must not refuse
    assert calls == ["compact"]


def test_empty_trader_runtime_is_refused(calls, monkeypatch):
    monkeypatch.setenv("TRADER_RUNTIME", "")
    with pytest.raises(SystemExit) as e:
        cli.main(["hold-live"])
    assert "REFUSING" in str(e.value) and calls == []


def test_stop_refusal_points_at_the_dashboard_button(calls):
    with pytest.raises(SystemExit) as e:
        cli.main(["stop"])
    assert "dashboard's STOP button" in str(e.value)
