"""The trial ledger (logs/trials.csv): written by replay, probe-report and the runner; read by `trader trials`."""

import csv
import datetime as dt
import json

import pandas as pd
import pytest
from test_engine import Always, spec
from test_probe import probe_spec
from test_probe import run as probe_run

from trader import cli, compact, config, golive, trials

DAY = dt.date(2026, 9, 21)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STRATEGIST_ROOT", tmp_path / "strategist")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(config, "REPLAY_DIR", tmp_path / "replays")
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    return tmp_path


def ledger():
    return trials.read()


def test_append_writes_one_header_and_never_rewrites(tmp_path):
    p = tmp_path / "logs" / "trials.csv"
    trials.append([{"time": "t1", "kind": "replay", "classifier_id": "a"}], p)
    with p.open("a") as f:
        f.write("t2,replay,,b")  # a row someone left without its newline
    trials.append([{"time": "t3", "kind": "replay", "classifier_id": "c"}], p)
    lines = p.read_text().splitlines()
    assert lines[0] == ",".join(trials.COLS) and lines.count(lines[0]) == 1
    assert [r["classifier_id"] for r in trials.read(p)] == ["a", "b", "c"]
    assert trials.append([], p) == 0


def test_a_file_that_is_not_a_ledger_is_refused(roots, capsys):
    p = trials.ledger_path()
    p.parent.mkdir(parents=True)
    p.write_text("time,kind\nt1,replay\n")  # someone else's header
    with pytest.raises(OSError):
        trials.append([{"kind": "replay"}])
    with pytest.raises(ValueError):
        trials.read()
    assert p.read_text() == "time,kind\nt1,replay\n"
    with pytest.raises(SystemExit, match="can't read the trial ledger"):
        cli.main(["trials"])
    (roots / "runtime").mkdir()
    (roots / "runtime" / "trials.csv").write_text(trials.HEADER + "t,shadow_start,d,a,h1,novel,,,,,,0\n")
    assert compact.archive()["trials_added"].startswith("failed")  # never re-appended run after run


def test_append_refuses_a_symlink(tmp_path):
    target = tmp_path / "elsewhere.csv"
    target.write_text("x\n")
    (tmp_path / "trials.csv").symlink_to(target)
    with pytest.raises(OSError):
        trials.append([{"kind": "replay"}], tmp_path / "trials.csv")
    assert trials.record(lambda: [{"kind": "replay"}], tmp_path / "trials.csv") == 0  # best-effort: no error
    assert target.read_text() == "x\n"


def test_read_leaves_out_a_row_still_being_written(tmp_path):
    p = tmp_path / "trials.csv"
    p.write_text(",".join(trials.COLS) + "\nt1,replay,r,a\nt2,repl")
    assert [r["classifier_id"] for r in trials.read(p)] == ["a"]
    assert trials.read(tmp_path / "missing.csv") == []


def _replay(monkeypatch, session, *argv):
    import trader.replay

    sessions = {"SPY": {DAY: session(path=[100.0] * 390)}}
    monkeypatch.setattr(trader.replay, "load_sessions", lambda *a, **k: sessions)
    monkeypatch.setattr(cli, "_specs", lambda path, source="alpaca": [spec(), spec(id="u", family="novel")])
    monkeypatch.setattr(cli, "_decider", lambda name, secrets: Always())
    cli.main(["replay", "--start", str(DAY), "--end", str(DAY), *argv])


def test_replay_appends_a_row_per_classifier(roots, monkeypatch, session):
    _replay(monkeypatch, session, "--name", "r1")
    rows = ledger()
    assert [(r["kind"], r["run_name"], r["classifier_id"]) for r in rows] == [
        ("replay", "r1", "t"),
        ("replay", "r1", "u"),
    ]
    h = golive.spec_hash(spec(), golive.custom_features_digest())
    t = rows[0]
    assert t["spec_hash"] == h and t["family"] == "conventional" and t["stub"] == "0"
    assert (t["days"], t["start"], t["end"]) == ("1", str(DAY), str(DAY))
    closed = [
        r
        for r in csv.DictReader((roots / "replays" / "r1" / "sim" / "trades.csv").open())
        if r["side"] == "sell" and r["classifier"] == "t"
    ]
    assert int(t["trades"]) == len(closed) > 0
    assert float(t["net_pct_after_slip"]) == pytest.approx(
        sum(float(r["pnl_pct"]) for r in closed) / len(closed), abs=1e-4
    )

    _replay(monkeypatch, session, "--name", "r2", "--decider", "stub")
    assert [r["stub"] for r in ledger()] == ["0", "0", "1", "1"]


def test_a_ledger_failure_never_fails_the_replay(roots, monkeypatch, session, capsys):
    (roots / "strategist" / "logs").mkdir(parents=True)
    (roots / "strategist" / "logs" / "trials.csv").mkdir()  # can't be appended to
    _replay(monkeypatch, session, "--name", "r1")
    out = capsys.readouterr()
    assert "run dir:" in out.out and "trial ledger not updated" in out.err


def test_probe_report_on_a_replay_appends_a_row_per_probe(roots, monkeypatch, session):
    import trader.data

    days = [dt.date(2026, 9, d) for d in (14, 15, 16)]
    sessions = {d: session(day=d, seed=i) for i, d in enumerate(days)}
    replay = roots / "replays" / "r1"
    for d in days:
        probe_run(replay / d.isoformat(), sessions[d], [probe_spec()], Always(entry="STAND_DOWN"))
        (replay / "decisions").mkdir(parents=True, exist_ok=True)
        (replay / d.isoformat() / "decisions" / f"{d}.jsonl.gz").rename(replay / "decisions" / f"{d}.jsonl.gz")
    monkeypatch.setattr(
        trader.data, "fetch", lambda syms, t0, t1, secrets, source: {"SPY": pd.concat(sessions.values())}
    )
    # The replay's own row is where the probe's spec hash comes from.
    trials.append(
        [
            {
                "time": "2026-09-17T00:00:00+00:00",
                "kind": "replay",
                "run_name": "r1",
                "classifier_id": "p",
                "spec_hash": "abc",
                "family": "novel",
                "stub": 0,
            }
        ]
    )
    out = roots / "report.json"
    cli.main(
        ["probe-report", "--replay", "r1", "--horizons", "15,30", "--file", str(roots / "none.yaml"), "--out", str(out)]
    )
    rep = json.loads(out.read_text())["probes"]["p"]["horizons"]["15"]
    (row,) = [r for r in ledger() if r["kind"] == "probe_report"]
    assert (row["run_name"], row["classifier_id"], row["spec_hash"], row["family"], row["stub"]) == (
        "r1",
        "p",
        "abc",
        "novel",
        "0",
    )
    assert (row["days"], row["start"], row["end"]) == ("3", "2026-09-14", "2026-09-16")
    assert int(row["trades"]) == rep["enter_n"]

    # The runner's own logs (no --replay) are not a trial.
    monkeypatch.setattr(config, "RUNTIME_DIR", replay)
    cli.main(
        [
            "probe-report",
            "--start",
            "2026-09-14",
            "--horizons",
            "15",
            "--file",
            str(roots / "none.yaml"),
            "--out",
            str(out),
        ]
    )
    assert len(ledger()) == 2


def test_runner_logs_each_new_spec_once_and_archive_copies_it(roots):
    a, b = spec(id="a"), spec(id="b", family="novel")
    hashes = {"a": "h1", "b": "h2"}
    assert trials.note_shadow_starts([a, b], hashes, DAY, stub=False) == ["a", "b"]
    assert trials.note_shadow_starts([a, b], hashes, DAY + dt.timedelta(days=1), stub=False) == []
    assert trials.note_shadow_starts([a, b], {"a": "h1", "b": "h3"}, DAY, stub=False) == ["b"]  # b was edited
    assert trials.note_shadow_starts([a], {}, DAY, stub=False) == []  # no hash: nothing to log
    assert trials.note_shadow_starts([spec(id="c")], {"c": "h1"}, DAY, stub=False) == ["c"]  # a's spec under a new id
    assert not ledger()  # the runner never writes the strategist's checkout

    assert compact.archive()["trials_added"] == 4
    assert compact.archive()["trials_added"] == 0  # idempotent
    rows = ledger()
    assert [(r["kind"], r["classifier_id"], r["spec_hash"], r["run_name"]) for r in rows] == [
        ("shadow_start", "a", "h1", str(DAY)),
        ("shadow_start", "b", "h2", str(DAY)),
        ("shadow_start", "b", "h3", str(DAY)),
        ("shadow_start", "c", "h1", str(DAY)),
    ]


def test_a_broken_runtime_ledger_raises_for_the_runner_to_report(roots):
    (roots / "runtime" / "trials.csv").mkdir(parents=True)
    with pytest.raises(OSError):
        trials.note_shadow_starts([spec(id="a")], {"a": "h1"}, DAY, stub=False)


def test_archive_survives_a_broken_runtime_ledger(roots):
    (roots / "runtime" / "trials.csv").mkdir(parents=True)
    assert compact.archive()["trials_added"].startswith("failed")


def test_trials_report(roots, capsys):
    def row(day, kind, cid, h, family="novel", stub=0):
        return {
            "time": f"{day}T12:00:00+00:00",
            "kind": kind,
            "run_name": "r",
            "classifier_id": cid,
            "spec_hash": h,
            "family": family,
            "stub": stub,
        }

    trials.append(
        [
            row("2026-09-01", "replay", "a", "h1"),
            row("2026-09-01", "replay", "a", "h2"),
            row("2026-09-02", "probe_report", "a", "h2"),
            row("2026-09-03", "shadow_start", "a", "h2"),
            row("2026-09-02", "replay", "b", "h3"),
            row("2026-09-02", "replay", "c", "h4", "conventional"),
            row("2026-09-04", "replay", "a", "h9", stub=1),
        ]
    )
    cli.main(["trials", "--id", "a"])
    out = capsys.readouterr().out
    assert "a: 2 distinct spec(s) tried on 3 different day(s), 3 evaluation(s)" in out
    assert "family novel: 3 distinct spec(s) across 2 id(s), tried on 3 different day(s)" in out
    assert "h9" not in out and "1 stub-decider row(s) not counted" in out
    cli.main(["trials"])
    out = capsys.readouterr().out
    assert "family conventional: 1 distinct spec(s)" in out and [ln.split()[0] for ln in out.splitlines()[1:4]] == [
        "a",
        "b",
        "c",
    ]
    cli.main(["trials", "--hash", "h2"])
    out = capsys.readouterr().out
    assert "h2" in out and "yes" in out and "h1" not in out
    cli.main(["trials", "--id", "a", "--include-stub"])
    assert "3 distinct spec(s)" in capsys.readouterr().out
    cli.main(["trials", "--id", "nope"])
    assert "no matching trials" in capsys.readouterr().out
