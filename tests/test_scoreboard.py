import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from test_engine import Always, run, spec
from trader import dashboard, golive
from trader import scoreboard as SB
from trader.classifier import ClassifierSpec


def trade(time, cid, sym, side, notional, pnl="", pnl_pct="", spec_hash=""):
    return {
        "time": time,
        "book": "paper",
        "classifier": cid,
        "symbol": sym,
        "side": side,
        "qty": "1",
        "price": "1",
        "notional": str(notional),
        "reason": "",
        "pnl": str(pnl),
        "pnl_pct": str(pnl_pct),
        "spec_hash": spec_hash,
    }


def round_trip(day, cid, pnl_pct, notional=50.0, sym="SPY", spec_hash=""):
    pnl = notional * pnl_pct / 100
    return [
        trade(f"{day}T10:00-04:00", cid, sym, "buy", notional, spec_hash=spec_hash),
        trade(f"{day}T11:00-04:00", cid, sym, "sell", notional + pnl, f"{pnl:.2f}", f"{pnl_pct:.3f}", spec_hash),
    ]


# ---- the family label ------------------------------------------------------------


def test_bad_family_never_invalidates_the_file_but_is_reported():
    """A label must not stop trading: the model accepts anything, `family_problem` reports it."""
    assert spec(family=None).family_label == "unlabelled"
    assert "family must be one of" in spec(family=None).family_problem()
    assert spec(family="Novel").family_label == "unlabelled" and spec(family="Novel").family_problem()
    assert "control classifiers have no family" in spec(id="control_x", control=True, family="novel").family_problem()
    assert spec(family="novel").family_label == "novel" and spec(family="novel").family_problem() is None
    assert spec(id="control_x", control=True, family=None).family_label == "control"


def test_load_specs_accepts_a_missing_family(tmp_path):
    from trader.classifier import load_specs

    p = tmp_path / "c.yaml"
    p.write_text("""
classifiers:
  - id: unlabelled_one
    symbols: [SPY]
    features: [ret_1m_pct]
    entry: {instructions: "go", criteria: {ENTER: "a", WAIT: "b"}}
    exit: {instructions: "hold", criteria: {HOLD: "a", EXIT: "b"}}
""")
    (s,) = load_specs(p, {"ret_1m_pct"}, {"SPY"})
    assert s.family_problem()


def test_spec_hash_unchanged_by_this_change():
    """Pinned to the hash computed on main before `family` existed: deploying must not restart records."""
    s = ClassifierSpec(
        id="t",
        symbols=["SPY"],
        features=["ret_1m_pct"],
        entry={"instructions": "?", "criteria": {"ENTER": "a", "WAIT": "b"}},
        exit={"instructions": "?", "criteria": {"HOLD": "a", "EXIT": "b"}},
    )
    assert golive.spec_hash(s) == "0ae294b57e0b26b4"
    assert golive.spec_hash(s.model_copy(update={"family": "novel"})) == "0ae294b57e0b26b4"


def test_relabelling_does_not_restart_the_promotion_record():
    assert golive.spec_hash(spec(family="novel")) == golive.spec_hash(spec(family="conventional"))
    assert golive.spec_hash(spec(family="novel")) != golive.spec_hash(spec(family="novel", stop_pct=0.6))


def test_promotion_record_remembers_family(tmp_path):
    state = tmp_path / "promotion.json"
    golive.enforce_promotion(
        [spec(id="idea", family="novel")],
        lambda *a: None,
        account_live=False,
        today=dt.date(2026, 10, 6),
        book_dir=tmp_path,
        state_file=state,
        custom_dir=tmp_path,
    )
    rec = json.loads(state.read_text())["idea"]
    since = rec["since"]
    assert rec["family"] == "novel"
    golive.enforce_promotion(
        [spec(id="idea", family="conventional")],
        lambda *a: None,
        account_live=False,
        today=dt.date(2026, 10, 9),
        book_dir=tmp_path,
        state_file=state,
        custom_dir=tmp_path,
    )
    rec = json.loads(state.read_text())["idea"]
    assert rec == {**rec, "family": "conventional", "since": since}


def test_existing_record_without_family_keeps_its_since(tmp_path):
    state = tmp_path / "promotion.json"
    s = spec(id="idea", family="novel")
    state.write_text(
        json.dumps(
            {"idea": {"hash": golive.spec_hash(s, golive.custom_features_digest(tmp_path)), "since": "2026-10-06"}}
        )
    )
    golive.enforce_promotion(
        [s],
        lambda *a: None,
        account_live=False,
        today=dt.date(2026, 10, 20),
        book_dir=tmp_path,
        state_file=state,
        custom_dir=tmp_path,
    )
    assert json.loads(state.read_text())["idea"] == {
        **json.loads(state.read_text())["idea"],
        "since": "2026-10-06",
        "family": "novel",
    }


def test_a_spec_that_cant_be_hashed_fails_closed_alone(tmp_path, monkeypatch):
    """One rule's broken promotion check never aborts the session: that rule runs in shadow (if it
    asked for live) with an alert, the others are checked and recorded as usual."""
    state = tmp_path / "promotion.json"
    state.write_text(json.dumps({"bad": {"hash": "h0", "since": "2026-10-01", "family": "novel"}}))
    real = golive.spec_hash

    def flaky(s, digest=""):
        if s.id == "bad":
            raise TypeError("boom")
        return real(s, digest)

    monkeypatch.setattr(golive, "spec_hash", flaky)
    notes = []
    specs = [spec(id="bad", mode="live"), spec(id="good", mode="live"), spec(id="meh", mode="shadow")]
    out = golive.enforce_promotion(
        specs,
        lambda level, msg: notes.append((level, msg)),
        account_live=True,
        today=dt.date(2026, 10, 6),
        book_dir=tmp_path,
        state_file=state,
        custom_dir=tmp_path,
    )
    modes = {s.id: s.mode for s in out}
    assert modes["bad"] == "shadow" and modes["good"] == "shadow"  # good: no paper record yet, the usual gate
    assert next(level for level, msg in notes if msg.startswith("bad:")) == "urgent"
    rec = json.loads(state.read_text())
    assert rec["bad"] == {"hash": "h0", "since": "2026-10-01", "family": "novel"}  # untouched
    assert rec["good"]["since"] == "2026-10-06" and rec["meh"]["since"] == "2026-10-06"


def test_corrupt_promotion_record_fails_closed_without_aborting(tmp_path):
    """An unreadable promotion.json never stops the session: it's set aside, every record restarts
    today (so nothing can go live on it) and the human is told."""
    state = tmp_path / "promotion.json"
    state.write_text("{not json")
    notes = []
    s = spec(id="idea", mode="live")
    out = golive.enforce_promotion(
        [s],
        lambda level, msg: notes.append((level, msg)),
        account_live=True,
        today=dt.date(2026, 10, 6),
        book_dir=tmp_path,
        state_file=state,
        custom_dir=tmp_path,
    )
    assert out[0].mode == "shadow"
    assert notes[0][0] == "urgent" and "unreadable" in notes[0][1] and "restarts today" in notes[0][1]
    assert json.loads(state.read_text())["idea"]["since"] == "2026-10-06"
    assert len(list(tmp_path.glob("promotion.json.corrupt-*"))) == 1


def test_unsaveable_promotion_record_does_not_abort(tmp_path):
    notes = []
    state = tmp_path / "nodir" / "promotion.json"
    (tmp_path / "nodir").write_text("a file, not a directory")
    out = golive.enforce_promotion(
        [spec(id="idea")],
        lambda level, msg: notes.append((level, msg)),
        account_live=False,
        today=dt.date(2026, 10, 6),
        book_dir=tmp_path,
        state_file=state,
        custom_dir=tmp_path,
    )
    assert [s.id for s in out] == ["idea"] and notes and "couldn't be saved" in notes[0][1]


# ---- the numbers -----------------------------------------------------------------


def test_closed_trades_charge_slippage_on_entry_notional():
    rows = round_trip("2026-10-06", "a", 1.0, notional=100.0)
    (t,) = SB.closed_trades(rows)
    assert t["net_pct"] == pytest.approx(0.9)
    assert t["net_usd"] == pytest.approx(1.0 - 0.1)
    (t,) = SB.closed_trades(rows, slippage_per_side_pct=0)  # replays: sim fills already include it
    assert t["net_pct"] == pytest.approx(1.0)


def test_closed_trades_unsorted_missing_buy_and_malformed_rows():
    rows = list(reversed(round_trip("2026-10-06", "a", 1.0, notional=100.0)))
    rows.append(trade("2026-10-07T11:00-04:00", "a", "QQQ", "sell", 101, "1.00", "1.000"))  # no buy row
    rows.append({"time": None, "side": "sell"})
    rows.append({"time": "2026-10-07T12:00-04:00", "side": "sell", "pnl_pct": "oops"})
    ts = SB.closed_trades(rows)
    assert [t["day"] for t in ts] == ["2026-10-06", "2026-10-07"]
    assert ts[1]["net_usd"] == pytest.approx(1.0 - 100 * 0.1 / 100)


def test_closed_trades_ignore_trades_before_the_start():
    rows = round_trip("2026-09-25", "a", 1.0) + round_trip("2026-10-06", "a", 1.0)
    assert [t["day"] for t in SB.closed_trades(rows, since="2026-10-05")] == ["2026-10-06"]


def test_scale_out_legs_are_part_of_one_trade():
    rows = [
        trade("2026-10-06T10:00-04:00", "a", "SPY", "buy", 100),
        trade("2026-10-06T10:30-04:00", "a", "SPY", "sell_part", 50.5, "0.50", "1.000"),
        trade("2026-10-06T11:00-04:00", "a", "SPY", "sell", 51, "1.50", "1.500"),
    ]
    (t,) = SB.closed_trades(rows)
    assert t["net_usd"] == pytest.approx(1.5 - 0.1)


def closes(xs, per_day=1):
    return [{"day": f"2026-10-{6 + i // per_day:02d}", "net_pct": x} for i, x in enumerate(xs)]


def test_verdicts_are_cautious():
    assert SB.summarise([])["verdict"] == "no trades yet"
    assert SB.summarise(closes([1.0] * 5))["verdict"].startswith("too few")
    assert SB.summarise(closes([0.5, -0.4] * 10))["verdict"] == "can't tell from luck yet"
    assert SB.summarise(closes([0.3, 0.2, 0.25, 0.35] * 5))["verdict"].startswith("ahead")
    assert SB.summarise(closes([-0.3, -0.2, -0.25, -0.35] * 5))["verdict"].startswith("losing")
    # 20 good trades on only 2 days: not enough distinct days to judge
    assert SB.summarise(closes([0.3, 0.2] * 10, per_day=10))["verdict"].startswith("too few")


def test_intervals_widen_when_trades_cluster_on_days():
    """Same returns, but grouped as whole good and bad days: the evidence is weaker."""
    spread = closes([0.6, -0.2] * 10)  # each day has one trade
    clumped = closes([0.6] * 10 + [-0.2] * 10, per_day=5)  # 2 good days, 2 bad days
    assert SB.summarise(clumped)["ci_pct"] > SB.summarise(spread)["ci_pct"]


def test_build_groups_by_family_and_keeps_retired():
    rows = []
    for i, d in enumerate(["2026-10-06", "2026-10-07", "2026-10-08"]):
        rows += round_trip(d, "idea_a", 0.8 if i % 2 else 0.4)
        rows += round_trip(d, "old_idea", -0.5, sym="QQQ")
        rows += round_trip(d, "control_orb", 0.1, sym="IWM")
    families = {"idea_a": "novel", "old_idea": "conventional"}
    b = SB.build(
        rows, families, current={"idea_a", "control_orb", "fresh"}, start_equity=250.0, since={"idea_a": "2026-10-07"}
    )
    by = {r["id"]: r for r in b["classifiers"]}
    assert b["days"] == ["2026-10-06", "2026-10-07", "2026-10-08"]
    assert by["old_idea"]["active"] is False and by["old_idea"]["family"] == "conventional"
    assert by["fresh"]["n"] == 0 and by["fresh"]["family"] == "unlabelled"
    assert by["idea_a"]["promotion"] == {"n": 2, "of": golive.MIN_TRADES, "since": "2026-10-07"}
    assert "promotion" not in by["control_orb"]
    # cumulative, as % of starting equity, one point per day
    assert len(by["old_idea"]["curve"]) == 3
    assert by["old_idea"]["curve"][-1] == pytest.approx(3 * (-0.25 - 0.05) / 250 * 100, abs=1e-3)
    fams = {f["family"]: f for f in b["families"]}
    assert fams["novel"]["n"] == 3 and fams["control"]["n"] == 3
    assert b["novel_vs_conventional"]["diff_pct"] > 0
    assert b["novel_vs_conventional"]["verdict"].startswith("too few")
    assert SB.build(rows, families, set(), 250.0)["classifiers"][0].get("promotion") is None
    # novel first, then conventional, control; retired after active within a family
    assert [r["family"] for r in b["classifiers"]][:2] == ["novel", "conventional"]


def test_promotion_count_skips_closes_from_an_earlier_spec(tmp_path):
    """The board counts exactly what golive.shadow_record counts: closes since the record's start,
    minus any stamped with another spec's hash (opened under the old spec, closed after the edit);
    unstamped closes (before provenance) go by date alone."""
    import csv

    rows = (
        round_trip("2026-10-06", "idea", 0.5, spec_hash="old")  # before the record: never counted
        + round_trip("2026-10-07", "idea", 0.5, spec_hash="old")  # opened under the old spec
        + round_trip("2026-10-07", "idea", 0.5, sym="QQQ")  # unstamped: date rule
        + round_trip("2026-10-08", "idea", 0.5, spec_hash="new")
        + round_trip("2026-10-08", "idea", 0.5, sym="QQQ", spec_hash="new")
    )
    since, hashes = {"idea": "2026-10-07"}, {"idea": "new"}
    b = SB.build(rows, {"idea": "novel"}, {"idea"}, 250.0, since=since, spec_hashes=hashes)
    assert b["classifiers"][0]["promotion"]["n"] == 3
    # without the hash (an old promotion record) it falls back to the date rule
    assert SB.build(rows, {"idea": "novel"}, {"idea"}, 250.0, since=since)["classifiers"][0]["promotion"]["n"] == 4
    with (tmp_path / "trades.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert golive.shadow_record("idea", "2026-10-07", tmp_path, "new")[0] == 3
    assert golive.shadow_record("idea", "2026-10-07", tmp_path)[0] == 4


# ---- engine and API --------------------------------------------------------------


def test_engine_records_spy_benchmark(tmp_path, session):
    bars = session(n=120, seed=3)
    run(tmp_path, bars, [spec()], Always("WAIT"))
    lines = (tmp_path / "benchmark.csv").read_text().splitlines()
    assert lines[0] == "date,spy_open,spy_close"
    day, o, c = lines[1].split(",")
    assert day == "2026-09-21"
    assert float(o) == pytest.approx(bars.open.iloc[0], abs=1e-3)
    assert float(c) == pytest.approx(bars.close.iloc[-1], abs=1e-3)
    run(tmp_path, bars, [spec()], Always("WAIT"))  # the same day again replaces its row
    assert len((tmp_path / "benchmark.csv").read_text().splitlines()) == 2


def test_benchmark_accumulates_days_and_skips_when_no_spy(tmp_path, session):
    run(tmp_path, session(day=dt.date(2026, 9, 22), n=30), [spec()], Always("WAIT"))
    run(tmp_path, session(day=dt.date(2026, 9, 21), n=30), [spec()], Always("WAIT"))
    days = [line.split(",")[0] for line in (tmp_path / "benchmark.csv").read_text().splitlines()[1:]]
    assert days == ["2026-09-21", "2026-09-22"]
    from trader.broker import SimBroker
    from trader.data import ET
    from trader.engine import Book, Engine

    other = tmp_path / "other"
    book = Book("sim", SimBroker(250), other / "sim")
    eng = Engine([spec()], {"live": book, "shadow": book}, Always("WAIT"), {"SPY"}, other)
    eng.start_day(dt.date(2026, 9, 23), {})
    eng.end_day(dt.datetime(2026, 9, 23, 16, tzinfo=ET))  # SPY never ticked
    assert not (other / "benchmark.csv").exists()


def test_benchmark_write_failure_does_not_break_end_day(tmp_path, session):
    (tmp_path / "benchmark.csv").mkdir()  # unwritable as a file
    alerts = []
    from trader.broker import SimBroker
    from trader.data import ET
    from trader.engine import Book, Engine

    bars = session(n=30)
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine(
        [spec()],
        {"live": book, "shadow": book},
        Always("WAIT"),
        {"SPY"},
        tmp_path,
        alert=lambda level, msg: alerts.append(msg),
    )
    eng.start_day(bars.index[0].date(), {})
    for ts in bars.index:
        eng.tick((ts + dt.timedelta(minutes=1)).to_pydatetime(), {"SPY": bars.loc[:ts]}, 300)
    assert "sim" in eng.end_day(dt.datetime.combine(bars.index[0].date(), dt.time(16), ET))
    assert any("SPY benchmark" in a for a in alerts)


def test_status_carries_family(tmp_path, session):
    run(tmp_path, session(n=30), [spec(family="novel")], Always("WAIT"))
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["classifiers"][0]["family"] == "novel"


def test_scoreboard_endpoint_for_live(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    book = runtime / "books" / "paper"
    book.mkdir(parents=True)
    rows = round_trip("2026-10-06", "gone", 0.5) + round_trip("2026-10-06", "idea", 0.3, sym="QQQ")
    cols = list(rows[0])
    (book / "trades.csv").write_text(
        ",".join(cols) + "\n" + "\n".join(",".join(r[c] for c in cols) for r in rows) + "\n"
    )
    (book / "equity.csv").write_text("time,equity,nav,hwm\n2026-10-06T09:35-04:00,250.00,1.00000,1.00000\n")
    (runtime / "status.json").write_text(
        json.dumps({"classifiers": [{"id": "idea", "family": "novel", "symbols": {}}]})
    )
    (runtime / "promotion.json").write_text(
        json.dumps(
            {
                "gone": {"hash": "x", "since": "2026-10-05", "family": "conventional"},
                "idea": {"hash": "y", "since": "2026-10-06", "family": "conventional"},
            }
        )
    )  # today's status wins
    c = _client(monkeypatch, runtime)
    b = c.get("/api/scoreboard?source=live").json()["books"]["paper"]
    by = {r["id"]: r for r in b["classifiers"]}
    assert by["idea"]["family"] == "novel" and by["idea"]["active"]
    assert by["gone"]["family"] == "conventional" and not by["gone"]["active"]
    assert by["idea"]["promotion"]["n"] == 1
    assert b["units"] == "% of starting equity"


def _client(monkeypatch, runtime, replays=None):
    monkeypatch.setattr(dashboard.config, "RUNTIME_DIR", runtime)
    if replays:
        monkeypatch.setattr(dashboard.config, "REPLAY_DIR", replays)
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    monkeypatch.setattr(SB, "EXPERIMENT_START", dt.date(2026, 1, 1))
    return TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})


def _write_trades(path, rows):
    cols = list(rows[0])
    path.write_text(",".join(cols) + "\n" + "\n".join(",".join(r[c] for c in cols) for r in rows) + "\n")


def test_scoreboard_endpoint_for_a_replay(tmp_path, monkeypatch):
    replays = tmp_path / "replays"
    sim = replays / "r1" / "sim"
    sim.mkdir(parents=True)
    _write_trades(sim / "trades.csv", round_trip("2026-09-21", "idea", 1.0, notional=100.0))
    (replays / "r1" / "status.json").write_text(
        json.dumps({"classifiers": [{"id": "idea", "family": "novel", "symbols": {}}]})
    )
    c = _client(monkeypatch, tmp_path / "runtime", replays)
    b = c.get("/api/scoreboard?source=r1").json()["books"]["sim"]
    (row,) = b["classifiers"]
    assert row["family"] == "novel" and "promotion" not in row
    assert row["mean_pct"] == pytest.approx(1.0)  # no second slippage charge
    assert b["units"] == "$" and b["slippage_per_side_pct"] == 0


def test_scoreboard_endpoint_tolerates_odd_files(tmp_path, monkeypatch):
    book = tmp_path / "runtime" / "books" / "paper"
    book.mkdir(parents=True)
    (book / "trades.csv").write_text("time,side\n2026-10-06T10:00-04:00,sell\n")  # missing columns
    (tmp_path / "runtime" / "status.json").write_text(json.dumps({"classifiers": "junk"}))
    (tmp_path / "runtime" / "promotion.json").write_text(json.dumps({"x": "junk"}))
    c = _client(monkeypatch, tmp_path / "runtime")
    r = c.get("/api/scoreboard?source=live")
    assert r.status_code == 200 and all(row["n"] == 0 for row in r.json()["books"]["paper"]["classifiers"])


def test_scoreboard_before_the_open_uses_the_classifiers_file(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    (runtime / "books" / "paper").mkdir(parents=True)
    (runtime / "status.json").write_text(json.dumps({"phase": "waiting for open", "classifiers": []}))
    cf = tmp_path / "classifiers.yaml"
    cf.write_text(
        "classifiers:\n  - {id: idea, family: novel}\n  - {id: control_orb, control: true}\n"
        "  - {id: off, family: novel, enabled: false}\n"
        "  - {id: probe_x, family: novel, mode: probe}\n"
    )
    monkeypatch.setattr(dashboard.config, "CLASSIFIERS_FILE", cf)
    c = _client(monkeypatch, runtime)
    by = {r["id"]: r for r in c.get("/api/scoreboard?source=live").json()["books"]["paper"]["classifiers"]}
    assert set(by) == {"idea", "control_orb"} and by["idea"]["active"] and by["idea"]["family"] == "novel"


def test_classifier_spec_schema_example_in_charter_validates():
    """The charter's schema example must stay valid now that family is required."""
    import re

    from trader import config

    text = (config.CODE_ROOT / "CLAUDE.md").read_text()
    block = re.search(r"```yaml\n(date:.*?)```", text, re.S).group(1)
    import yaml

    raw = yaml.safe_load(block)
    for c in raw["classifiers"]:
        assert ClassifierSpec.model_validate(c).family_problem() is None


def test_probes_get_no_promotion_record_and_no_scoreboard_row(tmp_path, monkeypatch):
    state = tmp_path / "promotion.json"
    p = spec(id="probe_x", family="novel", mode="probe", exit=None)
    golive.enforce_promotion(
        [p, spec(id="idea", family="novel")],
        lambda *a: None,
        account_live=False,
        today=dt.date(2026, 10, 6),
        book_dir=tmp_path,
        state_file=state,
        custom_dir=tmp_path,
    )
    assert set(json.loads(state.read_text())) == {"idea"}
    runtime = tmp_path / "runtime"
    (runtime / "books" / "paper").mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "classifiers": [
                    {"id": "idea", "family": "novel", "mode": "shadow", "symbols": {}},
                    {"id": "probe_x", "family": "novel", "mode": "probe", "symbols": {}},
                ]
            }
        )
    )
    c = _client(monkeypatch, runtime)
    ids = {r["id"] for r in c.get("/api/scoreboard?source=live").json()["books"]["paper"]["classifiers"]}
    assert "probe_x" not in ids


def test_live_board_can_include_the_pre_start_test_sessions(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    book = runtime / "books" / "paper"
    book.mkdir(parents=True)
    _write_trades(book / "trades.csv", round_trip("2026-09-29", "test_x", 0.5) + round_trip("2026-10-06", "idea", 0.3))
    (runtime / "status.json").write_text(
        json.dumps({"classifiers": [{"id": "idea", "family": "novel", "symbols": {}}]})
    )
    c = _client(monkeypatch, runtime)
    monkeypatch.setattr(SB, "EXPERIMENT_START", dt.date(2026, 10, 5))
    r = c.get("/api/scoreboard?source=live&all_days=0").json()
    assert r["experiment_start"] == "2026-10-05" and r["books"]["paper"]["days"] == ["2026-10-06"]
    assert r["books"]["paper"]["hidden_before_start"] == 1
    r = c.get("/api/scoreboard?source=live&all_days=1").json()
    assert r["all_days"] and r["books"]["paper"]["days"] == ["2026-09-29", "2026-10-06"]
    assert r["books"]["paper"]["hidden_before_start"] == 0
    assert {x["id"]: x["n"] for x in r["books"]["paper"]["classifiers"]} == {"idea": 1, "test_x": 1}


def test_pre_start_trades_show_by_default_only_until_the_start(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    book = runtime / "books" / "paper"
    book.mkdir(parents=True)
    _write_trades(book / "trades.csv", round_trip("2020-01-06", "test_x", 0.5))
    (runtime / "status.json").write_text(json.dumps({"classifiers": []}))
    c = _client(monkeypatch, runtime)
    monkeypatch.setattr(SB, "EXPERIMENT_START", dt.date(2999, 1, 1))  # not started: nothing else to show
    r = c.get("/api/scoreboard?source=live").json()
    assert r["all_days"] and r["books"]["paper"]["days"] == ["2020-01-06"]
    monkeypatch.setattr(SB, "EXPERIMENT_START", dt.date(2020, 2, 3))  # started: tests hidden
    r = c.get("/api/scoreboard?source=live").json()
    assert not r["all_days"] and r["books"]["paper"]["days"] == [] and r["books"]["paper"]["hidden_before_start"] == 1


def test_promotion_column_never_counts_trades_before_the_start_even_with_all_days_shown():
    """Matches golive.shadow_record: the pre-start toggle shows the earlier trades but they never
    count towards promotion, whenever the classifier's current spec started."""
    start = SB.EXPERIMENT_START
    before = (start - dt.timedelta(days=2)).isoformat()
    rows = round_trip(before, "idea", 1.0) + round_trip(start.isoformat(), "idea", 1.0)
    b = SB.build(rows, {"idea": "novel"}, {"idea"}, 250.0, since={"idea": before}, from_date=None)
    (row,) = b["classifiers"]
    assert row["n"] == 2 and row["promotion"] == {"n": 1, "of": golive.MIN_TRADES, "since": start.isoformat()}
    later = (start + dt.timedelta(days=1)).isoformat()  # a spec edited after the start keeps its own date
    b = SB.build(rows, {"idea": "novel"}, {"idea"}, 250.0, since={"idea": later}, from_date=None)
    assert b["classifiers"][0]["promotion"]["n"] == 0
