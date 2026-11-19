"""Push alerts via ntfy (config.yaml alerts.ntfy_server), with a local log as fallback and audit trail.

notify must work when config.yaml is malformed (#146): it's how that failure, and STOP, reach the human.
So it reads the server on use, and when the settings won't load it pushes nowhere: the alert goes to the local
log only, marked "not pushed". NTFY_TOKEN (optional, in the secrets file) is sent as a bearer token to the
configured server, and is never logged.

The log is RUNTIME_DIR/alerts.log. The strategist (user trader) can't write there, by design (the runtime dir is
read-only to it), so its alerts go to config.STRATEGIST_ALERTS instead: in its own checkout, 0640, which runner
and the human (group trading) can read but not write.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from trader import config

ET = ZoneInfo("America/New_York")


def _ntfy_server() -> str | None:
    """The configured push server, or None when the settings won't load."""
    try:
        return config.SETTINGS.alerts.ntfy_server
    except Exception:  # whatever broke the settings, notify must still log the alert
        return None


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)  # a new file: owner rw, group read
    try:
        f = os.fdopen(fd, "a")
    except BaseException:
        os.close(fd)
        raise
    with f:
        f.write(line)


def notify(level: str, message: str, title: str = "trader") -> None:
    """level: 'urgent' (high priority push) or 'info'."""
    stamp = dt.datetime.now(ET).isoformat(timespec="seconds")  # New York time with its offset, like trades.csv
    server = _ntfy_server()
    unpushed = "" if server else " (not pushed: config.yaml unreadable)"
    line = f"{stamp} {level.upper()} {message}{unpushed}\n"
    try:
        _append(config.RUNTIME_DIR / "alerts.log", line)
    except OSError:  # read-only to this user, or a full disk: the push below matters more, and callers mustn't crash
        where = "alerts.log not writable"
        try:
            _append(config.STRATEGIST_ALERTS, line)
            where += f"; logged to {config.STRATEGIST_ALERTS}"
        except OSError:
            pass
        try:
            print(f"{line.rstrip()} ({where})", file=sys.stderr)  # the journal keeps it
        except Exception:
            pass  # stderr gone too: nothing left to write to
    if not server:
        return
    secrets = config.load_secrets()
    topic = secrets.get("NTFY_TOPIC")
    if not topic:
        return
    headers = {
        "Title": title,
        "Priority": "high" if level == "urgent" else "default",
        "Tags": "rotating_light" if level == "urgent" else "chart_with_upwards_trend",
    }
    token = secrets.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        httpx.post(f"{server.rstrip('/')}/{topic}", content=message.encode(), headers=headers, timeout=10)
    except httpx.HTTPError:
        pass  # the local log still has it; watchdog covers a dead daemon
