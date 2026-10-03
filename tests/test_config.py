"""Deployment settings (config.yaml): loading, strict validation, and the generated deploy files."""

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from trader import config, golive
from trader import scoreboard as SB

ROOT = Path(__file__).resolve().parents[1]  # the tree under test, even if TRADER_CODE_ROOT points elsewhere
# The data under test (#169): this tree's own config.yaml and rendered files in a monorepo checkout; else
# the data root conftest.py chose (a copy of templates/data, or TRADER_TEST_DATA_ROOT/TRADER_DATA_ROOT).
DATA = config.DATA_ROOT if os.environ.get("TRADER_DATA_ROOT") else ROOT


def _write(tmp_path, mutate):
    raw = yaml.safe_load((DATA / "config.yaml").read_text())
    mutate(raw)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


@pytest.mark.config_start_date
def test_the_checked_in_config_loads_and_feeds_the_code():
    s = config.load_settings(DATA / "config.yaml")
    assert isinstance(s.experiment.start_date, dt.date)
    assert golive.START_DATE == s.experiment.start_date == SB.EXPERIMENT_START
    assert s.schedule.tz.key == s.schedule.local_tz


@pytest.mark.parametrize("mutate, why", [
    (lambda r: r["experiment"].pop("start_date"), "start_date"),
    (lambda r: r["experiment"].update(start_date="next monday"), "start_date"),
    (lambda r: r["schedule"].update(local_tz="Mars/Olympus"), "unknown time zone"),
    (lambda r: r["schedule"].update(runner_start=770), "runner_start"),  # YAML reads an unquoted 12:50 as 770
    (lambda r: r["schedule"].update(postclose_cutoff="25:00"), "HH:MM"),
    (lambda r: r["schedule"]["strategist"].update(weekly="Sat 10:00 Europe/London"), "time zone"),
    (lambda r: r["schedule"]["strategist"].update(weekly="Sat 10:00; rm"), "OnCalendar"),
    (lambda r: r["owner"].update(github_repo="not a slug"), "github_repo"),
    (lambda r: r["alerts"].update(ntfy_server="http://ntfy.sh"), "ntfy_server"),
    (lambda r: r["capital"].update(sim_cash=0), "sim_cash"),
    (lambda r: r.update(surprise=1), "surprise"),  # a typo'd key is an error, not silently ignored
], ids=["missing", "not-a-date", "tz", "unquoted-time", "bad-time", "calendar-tz", "calendar-junk", "repo", "http", "cash", "extra"])
def test_invalid_settings_fail_loudly(tmp_path, mutate, why):
    with pytest.raises(config.SettingsError, match=why):
        config.load_settings(_write(tmp_path, mutate))


def test_malformed_or_missing_file_fails(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("owner: [unclosed\n")
    with pytest.raises(config.SettingsError, match="can't read"):
        config.load_settings(bad)
    bad.write_text("- just a list\n")
    with pytest.raises(config.SettingsError, match="expected a mapping"):
        config.load_settings(bad)
    with pytest.raises(config.SettingsError, match="can't read"):
        config.load_settings(tmp_path / "absent.yaml")


def _py(code: str, **env) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if k not in ("TRADER_DASHBOARD_USERS", "TRADER_CONFIG")} | env
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e)


def test_every_command_refuses_to_start_on_a_broken_config(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("experiment: {start_date: soon}\n")
    r = _py("import trader.golive", TRADER_CONFIG=str(bad))
    assert r.returncode != 0 and "invalid deployment settings" in r.stderr


def test_dashboard_users_come_from_config_env_overrides_and_empty_means_nobody(tmp_path):
    show = "from trader import dashboard; print(sorted(dashboard.USERS))"
    assert _py(show).stdout.strip() == str(sorted(config.SETTINGS.dashboard.users))
    assert _py(show, TRADER_DASHBOARD_USERS="a@x, b@y").stdout.strip() == "['a@x', 'b@y']"
    empty = _write(tmp_path, lambda r: r["dashboard"].update(users=[]))
    assert _py(show, TRADER_CONFIG=str(empty)).stdout.strip() == "[]"


def test_generated_deploy_files_match_config():
    """The deploy runs the tests first, so a unit, timer or services.env that disagrees with
    config.yaml can never be deployed. Fix: `uv run trader config render-deploy`."""
    for path, text in config.render_deploy(ROOT, DATA).items():
        assert (DATA / path).read_text() == text, f"{path} is out of date: run `uv run trader config render-deploy`"
        assert "{{" not in text and "}}" not in text


def test_generated_files_carry_the_settings():
    files = config.render_deploy(ROOT, DATA)
    s = config.load_settings(DATA / "config.yaml")
    assert f"OnCalendar=Mon..Fri {s.schedule.runner_start} {s.schedule.local_tz}" in files["deploy/systemd/trader-runner.timer"]
    assert f"TRADER_DASHBOARD_USERS={','.join(s.dashboard.users)}" in files["deploy/systemd/trader.env"]
    for k in config.STRATEGIST_KINDS:
        t = files[f"deploy/systemd-trader/trader-strategist-{k}.timer"]
        assert f"OnCalendar={getattr(s.schedule.strategist, k)} {s.schedule.local_tz}\n" in t
        assert f"Unit=trader-strategist@{k}.service\n" in t and "@KIND@" not in t


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
def test_strategist_calendars_are_valid_systemd_specs():
    s = config.load_settings(DATA / "config.yaml")
    for k in config.STRATEGIST_KINDS:
        spec = f"{getattr(s.schedule.strategist, k)} {s.schedule.local_tz}"
        r = subprocess.run(["systemd-analyze", "calendar", spec], capture_output=True, text=True)
        assert r.returncode == 0, f"{k}: {spec!r}: {r.stderr}"


def test_strategist_units_stay_out_of_runners_install_dir():
    # trading-deploy installs deploy/systemd/* into runner's account: the strategist's timers
    # must never land there (they'd run strategist.sh as runner).
    assert not list((ROOT / "deploy" / "systemd").glob("trader-strategist*"))
    unit = (ROOT / "deploy" / "systemd-trader" / "trader-strategist@.service").read_text()
    assert "UMask=0022" in unit


def test_strategist_unit_runs_the_deployed_wrapper_in_split_mode():
    """The cutover (#169 C2): the wrapper runs from the deployed code, never from the checkout the
    strategist can write, and TRADER_DATA_ROOT (the wrapper's split-mode switch) points at the
    deployed config. PATH carries ~/.local/bin for the `trader` shim."""
    unit = (ROOT / "deploy" / "systemd-trader" / "trader-strategist@.service").read_text()
    lines = unit.splitlines()
    assert [ln for ln in lines if ln.startswith("ExecStart=")] == ["ExecStart=/srv/trading/main/scripts/strategist.sh %i"]
    env = dict(ln.removeprefix("Environment=").split("=", 1) for ln in lines if ln.startswith("Environment="))
    assert env["TRADER_CODE_ROOT"] == "/srv/trading/main"
    assert env["TRADER_DATA_ROOT"] == "/srv/trading/config"
    assert env["PATH"].split(":")[0] == "%h/.local/bin"
    assert env["TRADER_STRATEGIST_ROOT"] == "/srv/trading/strategist"
    assert env["TRADER_RUNTIME"] == "/srv/trading/runtime"
    assert env["TRADER_REPLAY_DIR"] == "/srv/trading/strategist/replays"
    assert env["TRADER_STRATEGIST_STAMP"] == "/srv/trading/strategist/.last_run"


def test_runner_services_env_points_at_the_deployed_config():
    """The runner's services.env (template and rendered) reads config from /srv/trading/config (#169 C2)."""
    for text in ((ROOT / "deploy" / "templates" / "trader.env").read_text(),
                 config.render_deploy(ROOT, DATA)["deploy/systemd/trader.env"]):
        assert "\nTRADER_CODE_ROOT=/srv/trading/main\n" in text
        assert "\nTRADER_DATA_ROOT=/srv/trading/config\n" in text


def test_render_refuses_unknown_keys_sections_and_line_breaks(monkeypatch):
    with pytest.raises(config.SettingsError, match="unknown setting"):
        config.render("x={{ owner.nickname }}")
    with pytest.raises(config.SettingsError, match="section"):
        config.render("x={{ owner }}")
    monkeypatch.setattr(config, "SETTINGS", config.SETTINGS.model_copy(
        update={"owner": config.Owner(name="a\nb", github_repo="o/r")}))
    with pytest.raises(config.SettingsError, match="line break"):
        config.render("x={{ owner.name }}")


def test_cli_get_and_render_check(tmp_path):
    r = subprocess.run([sys.executable, "-m", "trader.cli", "config", "get", "owner.github_repo"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and r.stdout.strip() == config.load_settings(DATA / "config.yaml").owner.github_repo
    r = subprocess.run([sys.executable, "-m", "trader.cli", "config", "render-deploy", "--check"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("section, key", [("owner", "github_repo"), ("dashboard", "tailscale_port"),
                                          ("owner", "name"), ("experiment", "start_date")])
def test_setup_scripts_read_the_same_values(section, key):
    """deploy/setup/cfg.sh parses config.yaml without Python: it must agree with the loader."""
    if DATA != ROOT and "TRADER_DATA_ROOT" not in (ROOT / "deploy" / "setup" / "cfg.sh").read_text():
        pytest.skip("this cfg.sh reads only the checkout's own config.yaml, and this tree has none")
    r = subprocess.run(["bash", "-c", f'. deploy/setup/cfg.sh && cfg {section} {key}'],
                       capture_output=True, text=True, cwd=ROOT, check=True)
    assert r.stdout.strip() == str(config.setting(f"{section}.{key}", config.load_settings(DATA / "config.yaml")))


def test_setup_helper_fails_on_a_missing_key():
    r = subprocess.run(["bash", "-c", ". deploy/setup/cfg.sh && cfg owner nickname"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode != 0 and "no value for owner.nickname" in r.stderr


def test_github_token_alert_names_the_configured_repo(monkeypatch):
    from types import SimpleNamespace as NS

    from trader import housekeeping

    soon = (dt.datetime.now(dt.UTC) + dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S UTC")
    monkeypatch.setattr(housekeeping.subprocess, "run", lambda *a, **k: NS(
        returncode=0, stdout=f"HTTP/2.0 200 OK\ngithub-authentication-token-expiration: {soon}\n", stderr=""))
    settings = config.SETTINGS.model_copy(update={"owner": config.SETTINGS.owner.model_copy(update={"github_repo": "someone/their-copy"})})
    monkeypatch.setattr(config, "SETTINGS", settings)
    ((_, msg, urgent),) = housekeeping.check_github()
    assert "repo someone/their-copy:" in msg and urgent
