"""Daily unattended-operation checks, run as trader from a systemd timer. Pings ntfy when:

- OpenRouter credit is low, or will run out within LOW_DAYS at the current burn rate
- the OpenRouter key's spend limit is nearly used, or the key is about to expire
- the GitHub token (used for pushes and PRs) is about to expire
- changes merged to main haven't been deployed to the runner for more than 2 days (in the split layout,
  either repo's main: the public code or the private data repo's deployment config)
- disk space is low

Alerts repeat weekly, then daily once something is within URGENT_DAYS.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from trader import config
from trader.alerts import notify

LOW_CREDIT_USD = 3.0
LOW_DAYS = 21
URGENT_DAYS = 7
UNDEPLOYED_HOURS = 48
LOW_DISK_GB = 3.0
STATE = Path.home() / ".local" / "state" / "trader" / "housekeeping.json"
# The deployed code checkout. Not config.CODE_ROOT: when this runs as trader today, CODE_ROOT is the
# strategist checkout. The trader shim sets TRADER_CODE_ROOT in the split layout (#169 section 14 item 2).
MAIN = Path(os.environ.get("TRADER_CODE_ROOT", "/srv/trading/main"))


def _data_root() -> Path | None:
    """The deployed config checkout in the split layout (#169), or None in the monorepo.
    Read locally for now; the DATA_ROOT PR (A1) adds a config helper. Set to MAIN itself, it is still the
    monorepo."""
    root = os.environ.get("TRADER_DATA_ROOT")
    if not root or Path(root).resolve() == MAIN.resolve():
        return None
    return Path(root)


def _days_until(iso: str | None) -> float | None:
    if not iso:
        return None
    when = dt.datetime.fromisoformat(iso.replace(" UTC", "+00:00").replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return (when - dt.datetime.now(dt.UTC)).total_seconds() / 86400


def check_openrouter(key: str) -> list[tuple[str, str, bool]]:
    """(id, message, urgent) for each problem."""
    h = {"Authorization": f"Bearer {key}"}
    out = []
    c = httpx.get("https://openrouter.ai/api/v1/credits", headers=h, timeout=15).json()["data"]
    k = httpx.get("https://openrouter.ai/api/v1/key", headers=h, timeout=15).json()["data"]
    remaining = float(c["total_credits"]) - float(c["total_usage"])
    burn = max(float(k.get("usage_weekly") or 0) / 7, 1e-6)
    days = remaining / burn
    if remaining < LOW_CREDIT_USD or days < LOW_DAYS:
        out.append(("openrouter_credit",
                    f"OpenRouter credit ${remaining:.2f} (~{min(days, 9999):.0f} days at ${burn:.3f}/day). Top up or enable auto top-up.",
                    remaining < 1 or days < URGENT_DAYS))
    if k.get("limit") is not None and k.get("limit_remaining") is not None and float(k["limit_remaining"]) < LOW_CREDIT_USD:
        out.append(("openrouter_limit", f"OpenRouter key spend limit nearly used: ${float(k['limit_remaining']):.2f} left. Raise it.", True))
    d = _days_until(k.get("expires_at"))
    if d is not None and d < LOW_DAYS:
        out.append(("openrouter_key_expiry", f"OpenRouter API key expires in {d:.0f} days. Create a new key and update both .env files.", d < URGENT_DAYS))
    return out


def check_github() -> list[tuple[str, str, bool]]:
    r = subprocess.run(["gh", "api", "-i", "rate_limit"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return [("github_auth", f"GitHub API call failed (token expired or revoked?): {r.stderr.strip()[:150]}", True)]
    for line in r.stdout.splitlines():
        if line.lower().startswith("github-authentication-token-expiration:"):
            d = _days_until(line.split(":", 1)[1].strip())
            if d is not None and d < LOW_DAYS:
                return [("github_token_expiry",
                         f"GitHub token expires in {d:.0f} days. Regenerate it (repo {config.SETTINGS.owner.github_repo}: Contents + Pull requests + Issues RW) and run `gh auth login` as trader.",
                         d < URGENT_DAYS)]
    return []


def _head(repo: Path) -> str:
    git = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]
    return subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


# Extra `-c` options for the anonymous lookup. Empty in production; tests use it to point an https URL at a
# local repo, since only command-line config applies to that lookup.
_ANON_GIT_CONFIG: tuple[str, ...] = ()


def _anonymous_remote_main(repo: Path) -> str:
    """The sha of main on `repo`'s origin, fetched anonymously over https: the public code repo must never see
    trader's token or SSH identity, and an auth prompt fails instead of hanging. Only command-line config
    applies (no global, system or repo config, so no credential helper, insteadOf or sshCommand)."""
    git = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]
    url = subprocess.run(git + ["config", "--get", "remote.origin.url"], capture_output=True, text=True,
                         check=True).stdout.strip()
    if not url.startswith("https://"):
        raise ValueError(f"the code checkout's origin {url!r} isn't an https URL; the split layout reads the "
                         "public code repo anonymously over https")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-c", "credential.helper=", *_ANON_GIT_CONFIG, "ls-remote", url, "refs/heads/main"],
                          capture_output=True, text=True, check=True, timeout=60, cwd="/", env=env).stdout.split()[0]


def _undeployed_for(state: dict, key: str, deployed: str, remote: str) -> float | None:
    """Hours since `remote` first differed from `deployed`, tracked in state[key]; None when they match."""
    if deployed == remote:
        state.pop(key, None)
        return None
    since = state.setdefault(key, {"sha": remote, "t": time.time()})
    if since["sha"] != remote:
        since.update(sha=remote, t=time.time())
    return (time.time() - since["t"]) / 3600


def check_undeployed(state: dict) -> list[tuple[str, str, bool]]:
    data = _data_root()
    if data is None:  # monorepo: one repo, compared with origin main as seen from the strategist checkout
        try:
            deployed = _head(MAIN)
            remote = subprocess.run(["git", "ls-remote", "origin", "refs/heads/main"], capture_output=True, text=True,
                                    check=True, timeout=60, cwd=config.STRATEGIST_ROOT).stdout.split()[0]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, IndexError) as e:
            return [("deploy_check", f"Couldn't compare deployed code with main: {e}", False)]
        hours = _undeployed_for(state, "undeployed_since", deployed, remote)
        if hours is not None and hours >= UNDEPLOYED_HOURS:
            return [("undeployed", f"main has changes merged {hours / 24:.0f} days ago that aren't deployed. Run: sudo -u runner trading-deploy", False)]
        return []
    # Split layout: the code (MAIN) against the public code repo's main, read anonymously; the deployment
    # config (DATA_ROOT) against the data repo's main, which the strategist checkout's origin also is.
    out = []
    try:
        hours = _undeployed_for(state, "undeployed_since", _head(MAIN), _anonymous_remote_main(MAIN))
        if hours is not None and hours >= UNDEPLOYED_HOURS:
            out.append(("undeployed", f"The code repo's main has changes merged {hours / 24:.0f} days ago that aren't deployed. Run: sudo -u runner trading-deploy", False))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, IndexError, ValueError) as e:
        out.append(("deploy_check", f"Couldn't compare deployed code with the code repo's main: {e}", False))
    try:
        remote = subprocess.run(["git", "ls-remote", "origin", "refs/heads/main"], capture_output=True, text=True,
                                check=True, timeout=60, cwd=config.STRATEGIST_ROOT).stdout.split()[0]
        hours = _undeployed_for(state, "undeployed_config_since", _head(data), remote)
        if hours is not None and hours >= UNDEPLOYED_HOURS:
            out.append(("undeployed_config", f"The data repo's main (deployment config) has changes merged {hours / 24:.0f} days ago that aren't deployed. Run: sudo -u runner trading-deploy", False))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, IndexError) as e:
        out.append(("config_deploy_check", f"Couldn't compare the deployed config with the data repo's main: {e}", False))
    return out


def check_disk() -> list[tuple[str, str, bool]]:
    free = shutil.disk_usage("/").free / 1e9
    if free < LOW_DISK_GB:
        return [("disk", f"Only {free:.1f} GB disk free on the VPS.", free < 1)]
    return []


def run() -> list[str]:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    last = state.setdefault("last_alert", {})
    problems = []
    secrets = config.load_secrets()
    for check in (lambda: check_openrouter(secrets["OPENROUTER_API_KEY"]), check_github,
                  lambda: check_undeployed(state), check_disk):
        try:
            problems += check()
        except Exception as e:  # a broken check must not hide the others
            problems.append((f"check_error_{len(problems)}", f"housekeeping check failed: {e!r}"[:200], False))
    now = time.time()
    sent = []
    for pid, msg, urgent in problems:
        gap = 86400 if urgent else 7 * 86400
        if now - last.get(pid, 0) >= gap - 3600:
            notify("urgent" if urgent else "info", msg, title="trader housekeeping")
            last[pid] = now
            sent.append(msg)
    for pid in list(last):  # forget resolved problems so they alert promptly if they recur
        if pid not in {p[0] for p in problems}:
            last.pop(pid)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    return [p[1] for p in problems] or ["all ok"]
