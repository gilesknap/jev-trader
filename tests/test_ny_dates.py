"""Date defaults are New York dates, not the host's (UTC on the server, a day ahead on US evenings)."""

import datetime as dt
import types

from test_engine import spec
from trader import alerts, cli, config


class LateEvening(dt.datetime):
    """21:30 in New York on 2026-11-03: already 2026-11-04 in UTC."""

    @classmethod
    def now(cls, tz=None):
        t = dt.datetime(2026, 11, 4, 2, 30, tzinfo=dt.UTC)
        return t.astimezone(tz) if tz else t.replace(tzinfo=None)


def late_evening(monkeypatch, module):
    monkeypatch.setattr(
        module, "dt", types.SimpleNamespace(datetime=LateEvening, date=dt.date, timedelta=dt.timedelta, UTC=dt.UTC)
    )


def test_ny_today_is_the_new_york_date(monkeypatch):
    late_evening(monkeypatch, config)
    assert config.ny_today() == dt.date(2026, 11, 3)


def test_alert_log_lines_are_stamped_in_new_york_time(tmp_path, monkeypatch):
    late_evening(monkeypatch, alerts)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    alerts.notify("info", "hello")
    assert (tmp_path / "alerts.log").read_text().startswith("2026-11-03T21:30:00-05:00 INFO hello")


def test_replay_and_probe_report_default_to_new_york_dates(tmp_path, monkeypatch):
    import trader.replay as R

    monkeypatch.setattr(config, "ny_today", lambda: dt.date(2026, 11, 3))
    monkeypatch.setattr(config, "REPLAY_DIR", tmp_path / "replays")
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    monkeypatch.setattr(cli, "_specs", lambda *a, **k: [spec()])
    got = {}
    monkeypatch.setattr(R, "replay", lambda specs, start, end, *a, **k: got.update(start=start, end=end) or {})
    cli.main(["replay", "--decider", "stub", "--name", "t"])
    assert got == {"start": dt.date(2026, 10, 28), "end": dt.date(2026, 11, 2)}


def test_compact_defaults_to_the_new_york_date(monkeypatch):
    from trader import compact

    seen = []
    monkeypatch.setattr(config, "ny_today", lambda: seen.append(1) or dt.date(2026, 11, 3))
    compact.compact(dry_run=True, scope="runtime")
    assert seen
