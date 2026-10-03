"""Paths, secrets, deployment settings (config.yaml) and run mode.

Four roots, which coincide in development and in today's single-repo layout:
- CODE_ROOT: checkout this code runs from (runner uses /srv/trading/main).
- DATA_ROOT: the human-owned deployment data: config.yaml, config/mode.yaml and the deploy files
  rendered from them ($TRADER_DATA_ROOT, default CODE_ROOT). Setting it to another directory is
  "split mode" (#169): code and data in separate repos.
- STRATEGIST_ROOT: the strategist's checkout holding state/ and features/custom/ (default DATA_ROOT).
- RUNTIME_DIR: runner-owned logs, heartbeats and status (read-only to the strategist).
The universe stays under CODE_ROOT: it must match the allocator's buckets in code.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CODE_ROOT = Path(os.environ.get("TRADER_CODE_ROOT", Path(__file__).resolve().parents[2]))
# Empty = unset, as the wrapper reads it. A relative value is resolved against the cwd once, here, so
# a later chdir can't move the data root (CODE_ROOT and STRATEGIST_ROOT keep their existing semantics).
DATA_ROOT = Path(os.environ["TRADER_DATA_ROOT"]).absolute() if os.environ.get("TRADER_DATA_ROOT") else CODE_ROOT
STRATEGIST_ROOT = Path(os.environ.get("TRADER_STRATEGIST_ROOT", DATA_ROOT))
RUNTIME_DIR = Path(os.environ.get("TRADER_RUNTIME", CODE_ROOT / "runtime"))
# Replays are run by the strategist, so they live somewhere it can write.
REPLAY_DIR = Path(os.environ.get("TRADER_REPLAY_DIR", RUNTIME_DIR / "replay"))
# First existing file wins: $TRADER_SECRETS, the data checkout's .env (dev), ~/.config/trading/env.
SECRETS_FILES = [
    Path(p)
    for p in (
        os.environ.get("TRADER_SECRETS"),
        DATA_ROOT / ".env",
        Path.home() / ".config" / "trading" / "env",
    )
    if p
]
ALIASES = {"OPEN_ROUTER": "OPENROUTER_API_KEY"}

# Touched by the strategist wrapper after each successful run (watchdog + dashboard).
STRATEGIST_STAMP = Path(os.environ.get("TRADER_STRATEGIST_STAMP", STRATEGIST_ROOT / ".last_run"))
# Touched only after a successful post-close run (the watchdog checks this one per session).
POSTCLOSE_STAMP = STRATEGIST_STAMP.with_name(".last_postclose")
# Alerts from a process that can't write RUNTIME_DIR/alerts.log (the strategist wrapper as trader: the runtime
# dir is read-only to it by design). Lives in the strategist checkout (gitignored), which runner can read.
STRATEGIST_ALERTS = Path(os.environ.get("TRADER_STRATEGIST_ALERTS", STRATEGIST_ROOT / "strategist-alerts.log"))

CLASSIFIERS_FILE = STRATEGIST_ROOT / "state" / "classifiers.yaml"
CUSTOM_FEATURES_DIR = STRATEGIST_ROOT / "features" / "custom"
MODE_FILE = DATA_ROOT / "config" / "mode.yaml"
UNIVERSE_FILE = CODE_ROOT / "config" / "universe.yaml"


def split_mode() -> bool:
    """True when TRADER_DATA_ROOT names a directory other than CODE_ROOT (code and data in separate
    repos, #169). In split mode the human override file is mandatory: see require_mode_file()."""
    return not _same_dir(DATA_ROOT, CODE_ROOT)


def _same_dir(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def require_mode_file(data_root: Path | None = None, code_root: Path | None = None) -> None:
    """The one mode-file rule. When data_root is a separate directory from code_root (split mode), a
    missing data_root/config/mode.yaml is an error, not a silent `auto`: the data checkout is
    incomplete (or points at the wrong directory), and the human's paper/live override must never
    vanish without a word. A no-op when they are the same directory. The roots default to DATA_ROOT
    and CODE_ROOT, read at call time."""
    data_root = DATA_ROOT if data_root is None else data_root
    code_root = CODE_ROOT if code_root is None else code_root
    mode_file = data_root / "config" / "mode.yaml"
    if not _same_dir(data_root, code_root) and not mode_file.exists():
        raise SettingsError(f"{mode_file} is missing: a data checkout separate from the code ({data_root}) "
                            "must hold config/mode.yaml (mode: auto | paper | live)")


# ---- deployment settings (config.yaml at the data root) ----------------------------------
# Personal and deployment values: who owns it, when the experiment starts, the operator's clock,
# model ids. Safety rules stay in code. Loaded at import and validated strictly, so a malformed
# file stops every command (the runner must never trade with a wrong start date).
SETTINGS_FILE = Path(os.environ.get("TRADER_CONFIG", DATA_ROOT / "config.yaml"))
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Owner(_Strict):
    name: str = Field(min_length=1)
    github_repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Dashboard(_Strict):
    users: list[str] = []
    tailscale_port: int = Field(ge=1, le=65535)


class Experiment(_Strict):
    start_date: dt.date


STRATEGIST_KINDS = ("premarket", "postclose", "weekly", "housekeeping")


class Strategist(_Strict):
    """When each strategist run fires: systemd OnCalendar specs, without a time zone (the timers
    append schedule.local_tz, which systemd honours; cron's CRON_TZ was silently ignored, #152)."""

    premarket: str
    postclose: str
    weekly: str
    housekeeping: str

    @field_validator("*")
    @classmethod
    def _calendar(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9.,:*/~ -]+", v.strip() or "!"):
            raise ValueError(f"not a systemd OnCalendar spec: {v!r}")
        if re.search(r"[A-Za-z]+/[A-Za-z_]+", v):  # e.g. Europe/London
            raise ValueError(f"leave the time zone out of {v!r}: schedule.local_tz is appended")
        return v


class Schedule(_Strict):
    local_tz: str
    runner_start: str
    postclose_cutoff: str
    strategist: Strategist

    @field_validator("local_tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {v!r}") from e
        return v

    @field_validator("runner_start", "postclose_cutoff")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.local_tz)

    @property
    def postclose_cutoff_time(self) -> dt.time:
        return dt.time.fromisoformat(self.postclose_cutoff)


class Models(_Strict):
    jev: str = Field(min_length=1)
    strategist: str = Field(min_length=1)


class Alerts(_Strict):
    ntfy_server: str = Field(pattern=r"^https://[^\s/]+")


class Capital(_Strict):
    sim_cash: float = Field(gt=0)
    replay_cash: float = Field(gt=0)


class Settings(_Strict):
    owner: Owner
    dashboard: Dashboard
    experiment: Experiment
    schedule: Schedule
    models: Models
    alerts: Alerts
    capital: Capital


class SettingsError(RuntimeError):
    pass


def load_settings(path: Path | None = None) -> Settings:
    """Parse and validate config.yaml. Raises SettingsError naming the file and the problem."""
    path = path or SETTINGS_FILE
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        hint = ""
        if isinstance(e, FileNotFoundError) and not os.environ.get("TRADER_CONFIG"):
            hint = (f"\nconfig.yaml is read from the data root ({DATA_ROOT}): set TRADER_DATA_ROOT to your "
                    "data checkout (or TRADER_CONFIG to the file)")
        raise SettingsError(f"{path}: can't read deployment settings: {e}{hint}") from e
    if not isinstance(raw, dict):
        raise SettingsError(f"{path}: expected a mapping of settings")
    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        raise SettingsError(f"{path}: invalid deployment settings:\n{e}") from e


SETTINGS = load_settings()


def ny_today() -> dt.date:
    """Today's date in New York: sessions, trade times and the go-live record are all New York
    dates. The host's `date.today()` is UTC on the server, already tomorrow on a US evening."""
    return dt.datetime.now(ZoneInfo("America/New_York")).date()


def setting(key: str, settings: Settings | None = None):
    """A dotted key from config.yaml (e.g. `schedule.strategist.premarket`), for the CLI and templates."""
    node = (settings or SETTINGS).model_dump(mode="json")
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def render(template: str, settings: Settings | None = None) -> str:
    """Fill `{{ dotted.key }}` placeholders from config.yaml. Lists join with commas. An unknown
    key, or a value holding a newline, raises: a rendered unit must never be half-filled."""
    def one(m: re.Match) -> str:
        try:
            v = setting(m.group(1), settings)
        except KeyError:
            raise SettingsError(f"unknown setting {{{{ {m.group(1)} }}}} in template") from None
        if isinstance(v, dict):
            raise SettingsError(f"{m.group(1)} is a section, not a value")
        out = ",".join(map(str, v)) if isinstance(v, list) else str(v)
        if "\n" in out or "\r" in out:
            raise SettingsError(f"{m.group(1)} contains a line break")
        return out
    return _PLACEHOLDER.sub(one, template)


# Files that can't read YAML are generated from deploy/templates/ and checked in, so the deploy
# and setup scripts install them as plain files. tests/test_config.py fails (and so does every
# deploy, which runs the tests first) when a generated file disagrees with config.yaml.
DEPLOY_TEMPLATES = {
    "deploy/templates/trader-runner.timer": "deploy/systemd/trader-runner.timer",
    "deploy/templates/trader.env": "deploy/systemd/trader.env",
    # One timer per strategist run, for the trader user (not deploy/systemd/: runner installs that).
    **{f"deploy/templates/trader-strategist.timer#{k}": f"deploy/systemd-trader/trader-strategist-{k}.timer"
       for k in STRATEGIST_KINDS},
}


def render_deploy(code_root: Path = CODE_ROOT, data_root: Path = DATA_ROOT) -> dict[str, str]:
    """Generated path (relative to data_root) -> contents, from code_root's templates and data_root's
    own config.yaml (never another tree's, whatever TRADER_CONFIG says: the files checked in beside
    it must match it). When the two differ (split mode), data_root must also hold config/mode.yaml:
    the deploy's render step is where a missing override is caught."""
    settings = load_settings(data_root / "config.yaml")
    require_mode_file(data_root, code_root)
    out = {}
    for src, dst in DEPLOY_TEMPLATES.items():
        path, _, kind = src.partition("#")  # "#kind": one template rendered per strategist run
        lines = [ln for ln in (code_root / path).read_text().splitlines(keepends=True) if not ln.startswith("# TEMPLATE:")]
        head = f"# GENERATED from {path} and config.yaml by `trader config render-deploy`: edit those, not this file.\n"
        out[dst] = head + render("".join(lines).replace("@KIND@", kind), settings)
    return out


def load_secrets() -> dict[str, str]:
    """KEY=VALUE lines from the secrets file, overridden by the environment."""
    secrets: dict[str, str] = {}
    for path in SECRETS_FILES:
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip().removeprefix("export ").strip()
                    secrets[ALIASES.get(key, key)] = value.strip().strip("\"'")
            break
    for key in list(secrets) + [
        "ALPACA_PAPER_KEY",
        "ALPACA_PAPER_SECRET",
        "ALPACA_LIVE_KEY",
        "ALPACA_LIVE_SECRET",
        "OPENROUTER_API_KEY",
        "NTFY_TOPIC",
    ]:
        if key in os.environ:
            secrets[key] = os.environ[key]
    return secrets


def account_mode() -> str:
    """Effective mode, 'paper' or 'live': the human override in config/mode.yaml
    (auto | paper | live), else the runner's go-live state. No side effects. In split mode a
    missing mode file raises SettingsError rather than reading as `auto`."""
    from trader import golive

    require_mode_file()
    ov = golive.override()
    if ov != "auto":
        return ov
    return "live" if golive.load_state()["status"] == "live" else "paper"


def universe() -> list[str]:
    return list((yaml.safe_load(UNIVERSE_FILE.read_text()) or {}).get("tickers", []))
