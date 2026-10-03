"""Push alerts via ntfy (config.yaml alerts.ntfy_server), with a local log as fallback and audit trail.

notify must work when config.yaml is malformed (#146): it's how that failure, and STOP, reach the human.
So it reads the server on use and falls back to DEFAULT_NTFY_SERVER when the settings won't load.

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
DEFAULT_NTFY_SERVER = "https://ntfy.sh"  # the template's alerts.ntfy_server


def _ntfy_server() -> str:
    try:
        return config.SETTINGS.alerts.ntfy_server
    except Exception:  # whatever broke the settings, the push is the last resort: it must still go out
        return DEFAULT_NTFY_SERVER


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
    line = f"{stamp} {level.upper()} {message}\n"
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
    topic = config.load_secrets().get("NTFY_TOPIC")
    if not topic:
        return
    try:
        httpx.post(
            f"{_ntfy_server().rstrip('/')}/{topic}",
            content=message.encode(),
            headers={
                "Title": title,
                "Priority": "high" if level == "urgent" else "default",
                "Tags": "rotating_light" if level == "urgent" else "chart_with_upwards_trend",
            },
            timeout=10,
        )
    except httpx.HTTPError:
        pass  # the local log still has it; watchdog covers a dead daemon
