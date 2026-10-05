"""Optional decision-model inputs (#73): news headlines and the pre-market daily note."""

import datetime as dt
import gzip
import json

import pandas as pd
import pytest

from test_engine import spec
from trader import golive
from trader import jev_inputs as N
from trader.broker import SimBroker
from trader.classifier import ClassifierSpec
from trader.data import ET
from trader.engine import Book, Engine, note_hash
from trader.jev import Decision
from trader.replay import _replay_news

DAY = dt.date(2026, 9, 21)


def et(h, m, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m), ET)


def item(t: dt.datetime, headline="h", summary=""):
    return {
        "created_at": t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        "headline": headline,
        "summary": summary,
    }


class Recorder:
    """Answers ENTER/HOLD and keeps every state it was sent."""

    def __init__(self):
        self.states, self.calls, self.total_cost = [], 0, 0.0

    def decide(self, state, instructions, criteria):
        self.calls += 1
        self.states.append(json.loads(json.dumps(state)))
        pick = "ENTER" if "ENTER" in criteria else "HOLD"
        return Decision(pick, {k: 0.9 if k == pick else 0.1 for k in criteria})


class FakeNews:
    def __init__(self, fail=False):
        self.fail, self.asked = fail, []

    def headlines(self, sym, now):
        self.asked.append((sym, now))
        if self.fail:
            raise N.NewsError("down")
        return N.as_of([item(now - dt.timedelta(minutes=10), f"{sym} news")], now)


def run(tmp_path, bars, specs, decider, news=None, note=None, playbooks=None, minutes=60):
    alerts = []
    book = Book("sim", SimBroker(250), tmp_path / "sim")
    eng = Engine(
        specs, {"live": book, "shadow": book}, decider, {"SPY"}, tmp_path, alert=lambda *a: alerts.append(a), news=news
    )
    eng.start_day(DAY, {}, daily_note=note, playbooks=playbooks)
    close = et(16, 0)
    for ts in bars.index[:minutes]:
        now = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        eng.tick(now, {"SPY": bars.loc[:ts]}, (close - now).total_seconds() / 60)
    errors = eng.news_errors
    eng.end_day(close)
    with gzip.open(tmp_path / "decisions" / f"{DAY}.jsonl.gz", "rt") as f:
        rows = [json.loads(line) for line in f]
    return rows, alerts, errors


def asks(rows):
    """The decision-log rows that are answers, in the order the decider was asked."""
    return [r for r in rows if r["q"] in ("entry", "exit", "probe")]


# ---- the as-of cut ------------------------------------------------------------------------


def test_as_of_never_shows_a_later_item_and_keeps_the_newest():
    now = et(10, 0)
    items = [
        item(now + dt.timedelta(minutes=1), "future"),
        item(now, "exactly now"),
        item(now - dt.timedelta(minutes=5), "recent", "x " * 300),
        item(now - N.LOOKBACK - dt.timedelta(minutes=1), "too old"),
        {"created_at": "garbage", "headline": "bad time"},
    ] + [item(now - dt.timedelta(minutes=30 + i), f"older {i}") for i in range(10)]
    got = N.as_of(items, now)
    assert [g["headline"] for g in got][:2] == ["exactly now", "recent"]
    assert len(got) == N.MAX_ITEMS
    assert all(g["headline"] not in ("future", "too old", "bad time") for g in got)
    assert got[1]["minutes_ago"] == 5 and got[1]["at"] == "2026-09-21 09:55"
    assert len(got[1]["summary"]) <= N.SUMMARY_CHARS and got[1]["summary"].endswith("…")
    assert "summary" not in got[0]  # an empty summary isn't sent


def test_live_news_refreshes_at_most_every_period_and_backs_off_after_a_failure():
    class API:
        def __init__(self):
            self.calls, self.fail = 0, False

        def fetch(self, symbol, start, end, pages=1):
            self.calls += 1
            if self.fail:
                raise N.NewsError("down")
            return [item(end - dt.timedelta(minutes=1), f"call {self.calls}")]

    t = [0.0]
    api = API()
    live = N.LiveNews(api, refresh_s=180, clock=lambda: t[0])
    assert live.headlines("SPY", et(10, 0))[0]["headline"] == "call 1"
    t[0] = 100
    live.headlines("SPY", et(10, 2))
    assert api.calls == 1  # cached
    t[0] = 200
    api.fail = True
    with pytest.raises(N.NewsError):
        live.headlines("SPY", et(10, 4))
    t[0] = 250
    assert live.headlines("SPY", et(10, 5))[0]["headline"] == "call 1"  # backing off: the old items, no call
    assert live.headlines("QQQ", et(10, 5)) == []  # and no other symbol is fetched meanwhile
    assert api.calls == 2
    t[0] = 400
    api.fail = False
    assert live.headlines("QQQ", et(10, 8))[0]["headline"] == "call 3"


def test_historical_news_fetches_a_day_once_and_caches_finished_days(tmp_path):
    class API:
        calls = 0

        def fetch(self, symbol, start, end, pages=1):
            API.calls += 1
            assert pages > 1 and start < et(9, 30, end.date()) and end == et(16, 0, end.date())
            return [item(et(9, 0, end.date()), "pre"), item(et(11, 0, end.date()), "late")]

    hist = N.HistoricalNews(API(), tmp_path, today=DAY + dt.timedelta(days=1))
    assert [h["headline"] for h in hist.headlines("SPY", et(10, 0))] == ["pre"]  # 11:00 is still the future
    assert [h["headline"] for h in hist.headlines("SPY", et(12, 0))] == ["late", "pre"]
    assert API.calls == 1 and (tmp_path / DAY.isoformat() / "SPY.json").is_file()
    again = N.HistoricalNews(API(), tmp_path, today=DAY + dt.timedelta(days=1))
    again.headlines("SPY", et(12, 0))
    assert API.calls == 1  # read from the cache
    today = N.HistoricalNews(API(), tmp_path / "t", today=DAY)
    today.headlines("SPY", et(12, 0))
    assert not (tmp_path / "t").exists()  # today's news isn't finished: not cached


# ---- identity ------------------------------------------------------------------------------


def test_no_inputs_keeps_the_old_identity_and_turning_one_on_changes_it():
    old = spec()
    # The hash as computed before `inputs` existed.
    import hashlib

    d = old.model_dump(exclude={"mode", "enabled", "family", "inputs"})
    for k in ("trail_pct", "max_hold_min", "entry_order", "risk_pct", "stop_atr_mult", "scale_out"):
        d.pop(k, None) if d.get(k) is None else None
    before = hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]
    assert golive.spec_hash(old) == golive.spec_hash(spec(inputs=[])) == before
    on = golive.spec_hash(spec(inputs=["headlines", "daily_note"]))
    assert on != before
    assert golive.spec_hash(spec(inputs=["daily_note", "headlines", "headlines"])) == on  # order, repeats
    assert golive.spec_hash(spec(inputs=["headlines"])) not in (on, before)


def test_unknown_input_is_rejected():
    with pytest.raises(ValueError):
        spec(inputs=["twitter"])
    assert ClassifierSpec.model_validate(spec(inputs=["daily_note"]).model_dump()).inputs == ["daily_note"]


# ---- what the engine sends and logs --------------------------------------------------------


def test_headlines_and_note_reach_jev_only_for_specs_that_ask(tmp_path, session):
    dec, news = Recorder(), FakeNews()
    # A probe beside the trading spec: it's asked on the same symbol while the other holds it.
    h = spec(id="h", mode="probe", inputs=["headlines", "daily_note"], cadence_min=5)
    specs = [spec(), h]
    rows, alerts, errors = run(tmp_path, session(), specs, dec, news=news, note="# 2026-09-21\nISM at 10:00")
    plain = [s for s, r in zip(dec.states, asks(rows), strict=True) if r["c"] == "t"]
    asked = [s for s, r in zip(dec.states, asks(rows), strict=True) if r["c"] == "h"]
    assert plain and asked
    assert all("headlines" not in s and "daily_note" not in s for s in plain)
    assert all(s["headlines"][0]["headline"] == "SPY news" and s["daily_note"].endswith("ISM at 10:00") for s in asked)
    assert all("hl" not in r and "nh" not in r for r in rows if r["c"] == "t")
    assert all(r["hl"] == 1 and r["nh"] == note_hash("# 2026-09-21\nISM at 10:00") for r in rows if r["c"] == "h")
    assert not errors and not alerts


def test_no_note_or_playbook_today_sends_empty_fields_and_logs_it(tmp_path, session):
    dec = Recorder()
    rows, _, _ = run(tmp_path, session(), [spec(inputs=["daily_note", "playbook"])], dec, playbooks={"other": "x"})
    assert dec.states and all(s["daily_note"] == "" and s["playbook"] == "" for s in dec.states)
    assert all(r["nh"] is None and r["pb"] is None for r in rows)


def test_inputs_reach_exit_questions_too_with_the_thesis(tmp_path, session):
    dec, news = Recorder(), FakeNews()
    s = spec(id="pb", inputs=["headlines", "daily_note", "playbook", "thesis"], max_trades=1)
    rows, _, _ = run(
        tmp_path,
        session(path=[100.0] * 390),
        [s],
        dec,
        news=news,
        note="# 2026-09-21\nnote",
        playbooks={"pb": "Plan A"},
    )
    exits = [st for st, r in zip(dec.states, asks(rows), strict=True) if r["q"] == "exit"]
    assert exits
    for st in exits:
        assert st["headlines"] and st["daily_note"].endswith("note") and st["playbook"] == "Plan A"
        th = st["position"]["thesis"]
        assert th["entry_question"] == "?" and th["p_enter"] == 0.9 and th["playbook"] == "Plan A" and th["opened"]
    entries = [st for st, r in zip(dec.states, asks(rows), strict=True) if r["q"] == "entry"]
    assert entries and all(st["position"] is None for st in entries)


def test_the_thesis_is_kept_with_the_position_but_sent_only_when_asked_for(tmp_path, session):
    dec = Recorder()
    run(tmp_path, session(path=[100.0] * 390), [spec()], dec, minutes=10)
    exits = [s for s in dec.states if s["position"]]
    assert exits and all("thesis" not in s["position"] for s in exits)  # an old spec sees what it saw before
    saved = json.loads((tmp_path / "sim" / "entries.json").read_text())
    assert saved["SPY"]["thesis"]["entry_question"] == "?"
    again = Book("sim", SimBroker(250), tmp_path / "sim")  # a restart reads it back
    assert again.entries["SPY"].thesis == saved["SPY"]["thesis"]


def test_a_news_failure_sends_no_headlines_and_never_stops_a_decision(tmp_path, session):
    dec, news = Recorder(), FakeNews(fail=True)
    rows, alerts, errors = run(tmp_path, session(), [spec(inputs=["headlines"])], dec, news=news)
    assert dec.states and all(s["headlines"] == [] for s in dec.states)
    assert {r["q"] for r in rows} >= {"entry", "exit"}  # it still entered and was asked to exit
    assert errors == len(news.asked) and errors > 1
    assert len([a for a in alerts if "News headlines failed" in a[1]]) == 1 and alerts[0][0] == "info"


# ---- the daily note file -------------------------------------------------------------------


def test_read_daily_note(tmp_path):
    p = tmp_path / "daily_note.md"
    assert N.read_daily_note(p, DAY) == (None, "no daily_note.md")
    p.write_text("# Daily note 2026-09-18\nold\n")
    note, why = N.read_daily_note(p, DAY)
    assert note is None and "stale" in (why or "")
    p.write_text("# Daily note 2026-09-21\nISM 10:00 ET\n")
    assert N.read_daily_note(p, DAY) == ("# Daily note 2026-09-21\nISM 10:00 ET", None)
    p.write_text("# 2026-09-21\n" + "x" * N.NOTE_MAX_CHARS)
    note, why = N.read_daily_note(p, DAY)
    assert note is not None and len(note) == N.NOTE_MAX_CHARS and "cut" in (why or "")
    link = tmp_path / "link.md"
    link.symlink_to(p)
    note, why = N.read_daily_note(link, DAY)
    assert note is None and why  # symlinks are refused, like the strategist's other files


def test_read_playbooks(tmp_path):
    p = tmp_path / "playbook.yaml"
    assert N.read_playbooks(p, DAY) == ({}, "no playbook.yaml")
    p.write_text("")
    assert N.read_playbooks(p, DAY)[0] == {}
    p.write_text("date: 2026-09-18\nplaybooks: {a: old}\n")
    books, why = N.read_playbooks(p, DAY)
    assert books == {} and "stale" in (why or "")
    p.write_text("date: [unclosed\n")
    books, why = N.read_playbooks(p, DAY)
    assert books == {} and "unreadable" in (why or "")
    p.write_text(
        "date: 2026-09-21\nplaybooks:\n  a: |\n    Plan A\n  b: ''\n  c: " + "y" * (N.NOTE_MAX_CHARS + 5) + "\n"
    )
    books, why = N.read_playbooks(p, DAY)
    assert books["a"] == "Plan A" and "b" not in books and len(books["c"]) == N.NOTE_MAX_CHARS and "c" in (why or "")


# ---- replays -------------------------------------------------------------------------------


class Offline:
    offline = True


def test_replays_send_no_note_or_playbook_and_the_stub_fetches_no_news():
    news, notes = _replay_news([spec(inputs=["headlines", "daily_note", "playbook"])], Offline(), {})
    assert news is None and "stub" in notes["headlines"]
    assert "not available in replays" in notes["daily_note"] and "not available in replays" in notes["playbook"]
    news, notes = _replay_news([spec()], object(), {})
    assert news is None and notes == {}
    news, notes = _replay_news([spec(inputs=["headlines"])], object(), {})  # no keys: none, said so
    assert news is None and "unavailable" in notes["headlines"]
    keys = {"ALPACA_PAPER_KEY": "k", "ALPACA_PAPER_SECRET": "s"}
    news, notes = _replay_news([spec(inputs=["headlines"])], object(), keys)
    assert isinstance(news, N.HistoricalNews) and notes == {}
