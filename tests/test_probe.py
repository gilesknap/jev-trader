import datetime as dt
import gzip
import json
import math

import numpy as np
import pandas as pd
import pytest

from trader import probe
from trader.broker import SimBroker
from trader.classifier import ClassifierSpec
from trader.data import ET
from trader.engine import Book, Engine
from trader.jev import Decision, DecisionError

from test_engine import Always, spec


def probe_spec(**kw):
    base = dict(
        id="p",
        mode="probe",
        family="novel",
        symbols=["SPY"],
        window=("09:35", "15:30"),
        cadence_min=5,
        features=["ret_1m_pct"],
        entry={"instructions": "?", "criteria": {"ENTER": "a", "STAND_DOWN": "b"}},
    )
    return ClassifierSpec(**(base | kw))


def run(tmp_path, bars, specs, decider):
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine(specs, {"live": book, "shadow": book}, decider, {"SPY"}, tmp_path)
    day = bars.index[0].date()
    eng.start_day(day, {})
    close = dt.datetime.combine(day, dt.time(16), ET)
    for ts in bars.index:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    eng.end_day(close)
    with gzip.open(tmp_path / "decisions" / f"{day}.jsonl.gz", "rt") as f:
        rows = [json.loads(line) for line in f]
    return book, rows


def test_probe_needs_no_exit_but_others_do():
    assert probe_spec().exit is None
    with pytest.raises(ValueError, match="exit question is required"):
        ClassifierSpec(**probe_spec().model_dump(exclude={"exit"}) | {"mode": "shadow"})
    with pytest.raises(ValueError, match="can't be probes"):
        probe_spec(id="control_x", control=True, family=None)


def test_probe_logs_but_never_orders_or_stands_down(tmp_path, session):
    book, rows = run(tmp_path, session(), [probe_spec()], Always(entry="STAND_DOWN"))
    assert not (tmp_path / "sim" / "trades.csv").exists()
    probes = [r for r in rows if r["q"] == "probe"]
    assert len(probes) > 60  # every 5 minutes from 09:35 to 15:30: standing down didn't retire it
    assert all("px" in r for r in probes)
    book, rows = run(tmp_path / "b", session(), [probe_spec()], Always(entry="ENTER"))
    assert not (tmp_path / "b" / "sim" / "trades.csv").exists()


def test_probe_runs_beside_a_trading_classifier(tmp_path, session):
    book, rows = run(tmp_path, session(path=[100.0] * 390), [spec(), probe_spec()], Always())
    trades = pd.read_csv(tmp_path / "sim" / "trades.csv")
    assert list(trades.classifier) == ["t", "t"]  # the probe on the same symbol didn't block or trade
    assert {r["q"] for r in rows} >= {"entry", "probe"}


class ProbesFail:
    """Fails only the probe's question, so we can see a probe outage leave trading alone."""

    def __init__(self):
        self.calls, self.total_cost = 0, 0.0

    def decide(self, state, instructions, criteria):
        self.calls += 1
        if "STAND_DOWN" in criteria:
            raise DecisionError("HTTP 503: probe")
        pick = "ENTER" if "ENTER" in criteria else "HOLD"
        return Decision(pick, {k: 0.9 if k == pick else 0.1 for k in criteria})


def test_probe_failures_dont_pause_trading(tmp_path, session):
    alerts = []
    bars = session(path=[100.0] * 390)
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine(
        [probe_spec(cadence_min=1), spec(window=("09:40", "15:30"))],
        {"live": book, "shadow": book},
        ProbesFail(),
        {"SPY"},
        tmp_path,
        alert=lambda lvl, m: alerts.append(m),
    )
    eng.start_day(bars.index[0].date(), {})
    close = dt.datetime.combine(bars.index[0].date(), dt.time(16), ET)
    for ts in bars.index[:20]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    assert "SPY" in book.entries  # the trading classifier entered at 09:40 despite the failing probe
    assert eng.decisions_paused_until is None and eng.probes_paused_until is not None
    assert eng.decision_errors == 0 and eng.probe_errors >= 2  # retried after each cooldown
    assert len(alerts) == 1 and "Trading is unaffected" in alerts[0]  # one info alert, not one per failure


class Recorder:
    def __init__(self):
        self.asked, self.calls, self.total_cost = [], 0, 0.0

    def decide(self, state, instructions, criteria):
        self.calls += 1
        self.asked.append("probe" if "STAND_DOWN" in criteria else "trade")
        return Decision("WAIT" if "WAIT" in criteria else "STAND_DOWN", {k: 0.5 for k in criteria})


def test_probes_ask_after_trading_classifiers_and_only_within_their_budget(tmp_path, session, monkeypatch):
    import trader.engine as E

    bars = session(path=[100.0] * 390)
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    rec = Recorder()
    eng = Engine(
        [probe_spec(cadence_min=1), spec(entry={"instructions": "?", "criteria": {"ENTER": "a", "WAIT": "b"}})],
        {"live": book, "shadow": book},
        rec,
        {"SPY"},
        tmp_path,
    )
    eng.start_day(bars.index[0].date(), {})
    close = dt.datetime.combine(bars.index[0].date(), dt.time(16), ET)
    tick = lambda i: eng.tick(
        (bars.index[i] + pd.Timedelta(minutes=1)).to_pydatetime(), {"SPY": bars.loc[: bars.index[i]]}, 300
    )
    tick(10)
    assert rec.asked == ["trade", "probe"]  # the probe was listed first but asked last
    monkeypatch.setattr(E, "PROBE_TICK_BUDGET_S", -1.0)  # probes' share of the tick is used up
    rec.asked.clear()
    tick(11)
    assert rec.asked == ["trade"]


def test_engine_logs_score_end_to_end(tmp_path, session):
    days = [dt.date(2026, 9, d) for d in (14, 15, 16, 17, 18, 21)]
    sessions = {"SPY": {d: session(day=d, seed=i) for i, d in enumerate(days)}}
    for d in days:
        run(tmp_path / d.isoformat(), sessions["SPY"][d], [probe_spec()], Always(entry="STAND_DOWN"))
    files = [tmp_path / d.isoformat() / "decisions" / f"{d}.jsonl.gz" for d in days]
    rows = probe.forward_returns(probe.load_rows(files), sessions, [15])
    assert rows.attrs["skipped"] == 0 and rows.fwd_15.notna().mean() > 0.9
    assert {"x:minutes_since_open", "x:ret_1m_lag1", "x:ret_1m_lag10"} <= set(rows.columns)
    h = probe.score(rows, [15])["p"]["horizons"][15]
    assert h["by_p_enter"] is None  # constant P(ENTER): no fake quintiles out of ties
    assert h["jev_ic"]["mean"] is None
    assert h["wf_inputs_only"]["days"] == 3  # days 4-6, each fitted on the days before it


def test_bins_are_labelled_by_their_values_and_keep_rare_confident_rows():
    y = pd.Series(np.r_[np.zeros(95), np.ones(5)])
    bins = probe._bins(pd.Series([0.1] * 95 + [0.9] * 5), y)
    assert [(b["p_enter_min"], b["p_enter_max"], b["n"]) for b in bins] == [(0.1, 0.1, 95), (0.9, 0.9, 5)]
    assert bins[1]["mean_gross_bps"] == 100.0
    p = pd.Series(np.linspace(0, 1, 100))
    bins = probe._bins(p, p)
    assert len(bins) == 5 and all(b["p_enter_min"] <= b["p_enter_max"] for b in bins)
    assert bins[0]["p_enter_min"] == 0.0 and bins[-1]["p_enter_max"] == 1.0


def test_unreadable_log_lines_are_skipped(tmp_path):
    f = tmp_path / "2026-09-01.jsonl"
    f.write_text(
        json.dumps({"t": "10:00", "c": "p", "s": "SPY", "q": "probe", "p": {"ENTER": 0.7}, "f": {}}) + '\n{"t": "10:0'
    )
    rows = probe.load_rows([f])
    assert len(rows) == 1 and rows.attrs["skipped"] == 1


def _rows(n_days=6, per_day=40, informative=True, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for d in range(n_days):
        day = (dt.date(2026, 9, 1) + dt.timedelta(days=d)).isoformat()
        for i in range(per_day):
            x = rng.normal()
            fwd = 0.2 * x + rng.normal(0, 0.1)
            p = 1 / (1 + math.exp(-x)) if informative else rng.uniform()
            out.append(
                {"day": day, "t": "10:00", "c": "p", "s": "SPY", "p_enter": p, "f:noise": rng.normal(), "fwd_15": fwd}
            )
    return pd.DataFrame(out)


def test_score_sees_an_informative_probe_and_an_uninformative_one():
    good = probe.score(_rows(), [15])["p"]["horizons"][15]
    assert good["jev_ic"]["mean"] > 0.7 and good["jev_ic"]["days"] == 6
    assert good["wf_inputs_plus_jev"]["mean"] > 0.7
    assert abs(good["wf_inputs_only"]["mean"] or 0) < 0.4  # noise alone predicts nothing
    bins = good["by_p_enter"]
    assert len(bins) == 5 and bins[-1]["mean_gross_bps"] > bins[0]["mean_gross_bps"]
    bad = probe.score(_rows(informative=False), [15])["p"]["horizons"][15]
    assert abs(bad["jev_ic"]["mean"]) < 0.2


def test_forward_returns_are_cut_at_the_flatten(session):
    bars = session(path=list(np.linspace(100, 139, 390)))
    day = bars.index[0].date()
    rows = pd.DataFrame(
        [
            {"day": day.isoformat(), "t": "10:01", "c": "p", "s": "SPY", "p_enter": 0.5},
            {"day": day.isoformat(), "t": "15:30", "c": "p", "s": "SPY", "p_enter": 0.5},
        ]
    )
    out = probe.forward_returns(rows, {"SPY": {day: bars}}, [15, 60])
    close = bars.close
    at = lambda hhmm: float(close[close.index.strftime("%H:%M") == hhmm].iloc[0])
    assert out.fwd_15[0] == pytest.approx((at("10:15") / at("10:00") - 1) * 100)
    assert out.fwd_60[1] == pytest.approx((at("15:44") / at("15:29") - 1) * 100)  # not 16:29


def test_decision_files_one_per_day_prefer_gz(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "2026-09-01.jsonl").write_text("")
    (b / "2026-09-01.jsonl.gz").write_bytes(b"")
    (a / "2026-09-02.jsonl").write_text("")
    files = probe.decision_files([a, b], None, None)
    assert sorted(f.name for f in files) == ["2026-09-01.jsonl.gz", "2026-09-02.jsonl"]


def test_rows_without_p_enter_are_left_out():
    rows = _rows()
    rows.loc[:3, "p_enter"] = np.nan
    h = probe.score(rows, [15])["p"]["horizons"][15]
    assert h["n"] == len(rows) - 4 and len(h["by_p_enter"]) == 5


def test_status_counts_each_rules_answers(tmp_path, session):
    run(tmp_path, session(), [probe_spec()], Always(entry="WAIT"))
    (c,) = json.loads((tmp_path / "status.json").read_text())["classifiers"]
    assert c["mode"] == "probe" and c["calls"] > 60 and c["threshold"] == 0.6


def test_probe_report_out_writes_a_file(tmp_path, session, monkeypatch):
    from trader import cli, config
    import trader.data

    days = [dt.date(2026, 9, d) for d in (14, 15, 16)]
    sessions = {d: session(day=d, seed=i) for i, d in enumerate(days)}
    replay = tmp_path / "replays" / "r1"
    for d in days:
        run(replay / d.isoformat(), sessions[d], [probe_spec()], Always(entry="STAND_DOWN"))
        (replay / "decisions").mkdir(parents=True, exist_ok=True)
        (replay / d.isoformat() / "decisions" / f"{d}.jsonl.gz").rename(replay / "decisions" / f"{d}.jsonl.gz")
    monkeypatch.setattr(config, "REPLAY_DIR", tmp_path / "replays")
    monkeypatch.setattr(config, "load_secrets", lambda: {})
    monkeypatch.setattr(
        trader.data, "fetch", lambda syms, t0, t1, secrets, source: {"SPY": pd.concat(sessions.values())}
    )
    out = tmp_path / "logs" / "probe_report.json"
    cli.main(
        [
            "probe-report",
            "--replay",
            "r1",
            "--horizons",
            "15,30",
            "--file",
            str(tmp_path / "none.yaml"),
            "--out",
            str(out),
        ]
    )
    rep = json.loads(out.read_text())
    assert rep["first_day"] == "2026-09-14" and rep["last_day"] == "2026-09-16" and rep["horizons"] == [15, 30]
    assert rep["probes"]["p"]["days"] == 3 and set(rep["probes"]["p"]["horizons"]) == {"15", "30"}


def test_dashboard_serves_the_latest_probe_report(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trader import dashboard

    monkeypatch.setattr(dashboard.config, "STRATEGIST_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "USERS", {"me@example.com"})
    c = TestClient(dashboard.app, headers={"Tailscale-User-Login": "me@example.com"})
    assert c.get("/api/probes").json() == {"report": None, "age_s": None}
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "probe_report.json").write_text("{not json")
    assert c.get("/api/probes").json()["report"] is None
    (tmp_path / "logs" / "probe_report.json").write_text(
        json.dumps({"first_day": "2026-10-05", "probes": {"p": {"rows": 3}}})
    )
    r = c.get("/api/probes").json()
    assert r["report"]["probes"]["p"]["rows"] == 3 and r["age_s"] is not None


def test_report_is_strict_json():
    rows = _rows(n_days=2).iloc[:1]  # one row: no spread, no bins, no ICs
    out = probe.finite(probe.score(rows, [15]))
    text = json.dumps(out, allow_nan=False)
    assert json.loads(text)["p"]["p_enter_spread"] is None
    assert probe.finite({"a": [float("nan"), 1.0, float("inf")]}) == {"a": [None, 1.0, None]}


def _ticker_rows(n_days=10, n_syms=24, per_sym=6, n_inputs=12, seed=1):
    """Forward returns driven only by which stock it is; Jev's P(ENTER) knows the stock and nothing
    else. Many symbols and a realistic dozen inputs, so a baseline that shrinks a rare symbol's
    dummy harder than it shrinks P(ENTER) would be caught."""
    rng = np.random.default_rng(seed)
    effect = {f"S{k:02d}": e for k, e in enumerate(np.linspace(-0.3, 0.3, n_syms))}
    view = {
        s: e + rng.normal(0, 0.1) for s, e in effect.items()
    }  # Jev's fixed opinion of each stock: right-ish, not exact
    out = []
    for d in range(n_days):
        day = (dt.date(2026, 9, 1) + dt.timedelta(days=d)).isoformat()
        for s, e in effect.items():
            for _ in range(per_sym):
                out.append(
                    {
                        "day": day,
                        "t": "10:00",
                        "c": "p",
                        "s": s,
                        "p_enter": 0.5 + view[s] + rng.normal(0, 0.01),
                        **{f"f:n{i}": rng.normal() for i in range(n_inputs)},
                        "fwd_15": e + rng.normal(0, 0.2),
                    }
                )
    return pd.DataFrame(out)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_ticker_effect_earns_jev_no_incremental_credit(seed):
    h = probe.score(_ticker_rows(seed=seed), [15])["p"]["horizons"][15]
    assert h["jev_ic"]["mean"] > 0.4  # Jev does rank the returns...
    assert h["wf_inputs_only"]["mean"] > 0.4  # ...but so does a baseline that knows the symbol
    inc = h["jev_increment"]
    assert inc["days"] == 7 and inc["mean"] < 0.01 and not inc["verdict"].startswith("Jev adds")


def test_increment_handles_identical_deltas():
    inc = probe._increment([0.1] * 6)
    assert inc == {"mean": 0.1, "ci95": None, "days": 6, "verdict": "inconclusive: no spread across days"}


def test_increment_is_paired_and_can_be_inconclusive():
    good = probe.score(_rows(n_days=10), [15])["p"]["horizons"][15]["jev_increment"]
    assert good["days"] == 7 and good["verdict"] == "Jev adds to its inputs: 95% interval above zero"
    bad = probe.score(_rows(n_days=10, informative=False), [15])["p"]["horizons"][15]["jev_increment"]
    assert bad["verdict"].startswith("inconclusive") and bad["ci95"] is not None
    few = probe.score(_rows(), [15])["p"]["horizons"][15]["jev_increment"]
    assert few == {**few, "days": 3, "ci95": None, "verdict": "inconclusive: too few days (3)"}


def test_missing_inputs_drop_the_row_from_both_models():
    rows = _rows(n_days=10)
    holes = rows.copy()
    holes.loc[holes.index % 7 == 0, "f:noise"] = np.nan
    a = probe.score(holes, [15])["p"]["horizons"][15]
    b = probe.score(rows[rows.index % 7 != 0], [15])["p"]["horizons"][15]
    for k in ("wf_inputs_only", "wf_inputs_plus_jev", "jev_increment"):
        assert a[k] == b[k]
    assert a["wf_inputs_only"]["days"] == a["wf_inputs_plus_jev"]["days"] == a["jev_increment"]["days"]
