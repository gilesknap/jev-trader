"""Daily unattended-operation checks, run as trader from a systemd timer. Pings ntfy when:

- OpenRouter credit is low, or will run out within LOW_DAYS at the current burn rate
- the OpenRouter key's spend limit is nearly used, or the key is about to expire
- the GitHub token (used for pushes and PRs) is about to expire
- changes merged to main haven't been deployed to the runner for more than 2 days
- disk space is low

Alerts repeat weekly, then daily once something is within URGENT_DAYS.
"""

from __future__ import annotations

import datetime as dt
import json
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
MAIN = Path("/srv/trading/main")


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


def check_undeployed(state: dict) -> list[tuple[str, str, bool]]:
    git = ["git", "-c", f"safe.directory={MAIN}", "-C", str(MAIN)]
    try:
        deployed = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        remote = subprocess.run(["git", "ls-remote", "origin", "refs/heads/main"], capture_output=True, text=True,
                                check=True, cwd=config.STRATEGIST_ROOT).stdout.split()[0]
    except (subprocess.CalledProcessError, IndexError) as e:
        return [("deploy_check", f"Couldn't compare deployed code with main: {e}", False)]
    if deployed == remote:
        state.pop("undeployed_since", None)
        return []
    since = state.setdefault("undeployed_since", {"sha": remote, "t": time.time()})
    if since["sha"] != remote:
        since.update(sha=remote, t=time.time())
    hours = (time.time() - since["t"]) / 3600
    if hours >= UNDEPLOYED_HOURS:
        return [("undeployed", f"main has changes merged {hours / 24:.0f} days ago that aren't deployed. Run: sudo -u runner trading-deploy", False)]
    return []


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
